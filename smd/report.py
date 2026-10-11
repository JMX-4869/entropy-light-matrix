"""崩溃报告生成：把原始日志变成「看得懂」的中文报告。

产物
----
``崩溃报告_<时间戳>.md``    纯文本，任何编辑器可读，适合贴到论坛/群
``崩溃报告_<时间戳>.html``  带样式的中文报告，浏览器直接打开

报告结构
--------
1. **一句话结论** —— 崩溃最可能的原因（自动归因）
2. **崩溃现场** —— exception.txt 的异常码翻译、调用栈解读
3. **本次会话的模组清单** —— 从崩溃的 meta.yml 里提取
4. **错误日志分析** —— 按知识库归类、翻译、给出中文注释与修复建议
5. **冲突检测摘要** —— 与模组冲突分析结果联动
6. **修复清单** —— 按优先级排列的可执行步骤
"""

from __future__ import annotations

import html
import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from .config import Paths, app_data_dir
from .conflict import ConflictReport, SEV_LABEL as C_SEV_LABEL, Finding
from .logkb import (KB, SEV_ERROR, SEV_FATAL, SEV_LABEL, SEV_NOISE, SEV_WARN,
                    LogAnalysis, analyze_log, iter_log_files)
from .paths import exists, human_size, is_dir, is_file, long_path, read_text, write_text

REPORT_DIR_NAME = "崩溃报告"

# 异常码翻译表
EXCEPTION_CODES: dict[str, tuple[str, str, str]] = {
    "C0000005": (
        "访问违例 (EXCEPTION_ACCESS_VIOLATION)",
        "程序试图读写一块不属于它的内存。这是群星崩溃最常见的异常类型，"
        "占比通常在 80% 以上。",
        "可能原因：① 模组脚本引用了已被删除/改名的游戏对象；"
        "② 内存耗尽后访问了空指针；③ 某个模组的 DLL/资源文件损坏；"
        "④ 显卡驱动问题。",
    ),
    "C0000094": (
        "整数除零 (EXCEPTION_INT_DIVIDE_BY_ZERO)",
        "脚本里做了除以零的运算。",
        "几乎一定是模组的脚本错误：某个权重/概率字段算出了 0 作为除数。"
        "结合崩溃前刚触发的事件定位模组。",
    ),
    "C00000FD": (
        "栈溢出 (EXCEPTION_STACK_OVERFLOW)",
        "函数调用层数过多，通常是脚本无限递归。",
        "模组的 scripted_effect 自引用，或 on_action 形成无限循环。"
        "这类崩溃会在同一时间点稳定复现。",
    ),
    "C0000090": (
        "浮点无效操作 (EXCEPTION_FLT_INVALID_OPERATION)",
        "数学运算出现了非法值（如 0/0、负数开方）。",
        "模组的数值定义有问题，通常是修正量计算。",
    ),
    "E06D7363": (
        "C++ 异常 (C++ Exception)",
        "程序内部抛出了未捕获的 C++ 异常。",
        "通常是资源加载失败（文件损坏、路径过长、磁盘空间不足）。",
    ),
    "80000003": (
        "断点异常 (BREAKPOINT)",
        "命中了调试断点指令。",
        "多见于使用了修改器/注入类工具，或反作弊机制触发。",
    ),
}

# 栈帧函数名翻译（PhysFS 是群星的虚拟文件系统层）
STACK_HINTS: dict[str, str] = {
    "PHYSFS": "PhysFS 虚拟文件系统 —— 游戏正在读取模组文件。"
              "崩在这里通常意味着某个模组文件损坏、路径过长或磁盘读取出错。",
    "BaseThreadInitThunk": "Windows 线程入口，说明崩在某个工作线程上"
                           "（而非主线程）。",
    "RtlUserThreadStart": "Windows 用户线程启动例程，栈底。",
    "function-name not available": "符号信息缺失（发行版未带调试符号），无法确定具体函数。",
}


@dataclass
class CrashReport:
    """一份完整的崩溃报告。"""

    title: str = ""
    generated_at: str = ""
    game_version: str = ""
    exception: dict[str, str] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)
    mods_in_session: list[str] = field(default_factory=list)
    dlcs: list[str] = field(default_factory=list)
    analysis: LogAnalysis | None = None
    conflicts: ConflictReport | None = None
    conclusion: str = ""
    causes: list[dict[str, str]] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)
    md_path: str = ""
    html_path: str = ""

    def to_dict(self) -> dict:
        """序列化为可写 JSON 的字典（原始栈文本会剔除）。"""
        return {
            "title": self.title,
            "generated_at": self.generated_at,
            "game_version": self.game_version,
            "exception": {k: v for k, v in self.exception.items() if k != "raw"},
            "meta": self.meta,
            "mods_in_session": self.mods_in_session,
            "dlc_count": len(self.dlcs),
            "conclusion": self.conclusion,
            "causes": self.causes,
            "actions": self.actions,
            "analysis": self.analysis.to_dict() if self.analysis else None,
            "conflicts": self.conflicts.to_dict() if self.conflicts else None,
            "md_path": self.md_path,
            "html_path": self.html_path,
        }


# ---------------------------------------------------------------------------
# 采集
# ---------------------------------------------------------------------------

def parse_meta_yml(path) -> dict[str, Any]:
    """解析崩溃目录里的 meta.yml，提取模组与 DLC 清单。"""
    out: dict[str, Any] = {"mods": [], "dlcs": [], "info": {}}
    if not is_file(path):
        return out
    try:
        txt = read_text(path)
    except OSError:
        return out
    for line in txt.splitlines():
        line = line.rstrip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        k, v = k.strip(), v.strip().strip('"')
        if k.startswith("Mod_"):
            out["mods"].append(v)
        elif k.startswith("DLC_"):
            out["dlcs"].append(v)
        else:
            out["info"][k] = v
    return out


def read_exception(crash_dir) -> dict[str, str]:
    """解析崩溃目录里的 ``exception.txt``。

        提取异常码、异常名、崩溃地址、游戏版本、时间与调用栈帧。
        """
    p = Path(crash_dir) / "exception.txt"
    out: dict[str, str] = {}
    if not is_file(p):
        return out
    try:
        txt = read_text(p)
    except OSError:
        return out
    out["raw"] = txt
    m = re.search(r"Unhandled Exception\s+([0-9A-Fa-f]+)\s*\(([^)]+)\)\s*at address\s*(\S+)", txt)
    if m:
        code = m.group(1).upper()
        out["code"] = code
        out["exception"] = m.group(2)
        out["address"] = m.group(3)
        out["code_key"] = code.lstrip("0") or code
    m2 = re.search(r"Version:\s*(\S+)", txt)
    if m2:
        out["version"] = m2.group(1)
    m3 = re.search(r"Date/Time:\s*(.+)", txt)
    if m3:
        out["datetime"] = m3.group(1).strip()
    frames = re.findall(r"^\s*\d+\s+(.+?)(?:\s*\(\+\s*(\d+)\))?\s*$", txt, re.M)
    out["frames"] = "\n".join(f.strip() for f in (x[0] for x in frames) if f.strip())
    return out


def find_crash_dirs(paths: Paths, limit: int = 30) -> list[dict]:
    """列出崩溃目录（新→旧）。"""
    d = paths.crashes_dir
    out: list[dict] = []
    if not is_dir(d):
        return out
    try:
        entries = [e for e in os.scandir(long_path(d)) if e.is_dir()]
    except OSError:
        return out
    for e in entries:
        try:
            st = e.stat()
        except OSError:
            continue
        files = []
        try:
            files = sorted(x.name for x in os.scandir(e.path) if x.is_file())
        except OSError:
            pass
        out.append({
            "name": e.name, "path": e.path, "mtime": st.st_mtime,
            "time_str": datetime.fromtimestamp(st.st_mtime).strftime(
                "%Y-%m-%d %H:%M:%S"),
            "files": files,
            "has_dump": any(f.endswith(".dmp") for f in files),
            "dump_size": _dump_size(e.path, files),
        })
    out.sort(key=lambda r: -r["mtime"])
    return out[:limit]


def _dump_size(d: str, files: list[str]) -> int:
    for f in files:
        if f.endswith(".dmp"):
            try:
                return os.path.getsize(long_path(os.path.join(d, f)))
            except OSError:
                return 0
    return 0


# ---------------------------------------------------------------------------
# 归因
# ---------------------------------------------------------------------------

def diagnose(exc: dict[str, str], analysis: LogAnalysis | None,
             conflicts: ConflictReport | None,
             mods_in_session: list[str]) -> tuple[str, list[dict], list[str]]:
    """给出崩溃归因、可能原因排序、修复动作清单。"""
    causes: list[dict[str, str]] = []
    actions: list[str] = []

    code = (exc.get("code") or "").upper().lstrip("0")
    info = EXCEPTION_CODES.get(code)
    frames = (exc.get("frames") or "")
    physfs = "PHYSFS" in frames.upper()
    threaded = "BaseThreadInitThunk" in frames

    if info:
        note = info[2]
        # 用调用栈缩小范围：这是本工具相对「看异常码猜原因」的关键增量
        if physfs:
            note = (
                "调用栈全部落在 PhysFS（游戏的虚拟文件系统）里，"
                "说明崩溃发生在**读取模组文件的过程中**。\n"
                "这把范围收窄到：① 某个模组文件损坏或读写失败"
                "（硬盘坏道、下载不完整、路径过长）；"
                "② 内存不足导致文件缓存分配失败；"
                "③ 模组数量太多、同时打开的文件句柄耗尽。\n"
                "建议优先做一次模组文件完整性校验（Steam → 属性 → "
                "已安装文件 → 验证完整性），并检查磁盘健康。"
            )
        if threaded:
            note += ("\n崩溃发生在工作线程而非主线程，"
                     "通常对应后台的资源异步加载。")
        causes.append({"title": info[0], "desc": info[1], "note": note})

    # 日志里的致命项
    fatal_groups: list[dict] = []
    error_groups: list[dict] = []
    if analysis:
        for g in analysis.sorted_groups():
            if g["severity"] == SEV_FATAL:
                fatal_groups.append(g)
            elif g["severity"] == SEV_ERROR:
                error_groups.append(g)

    for g in fatal_groups[:3]:
        causes.append({
            "title": f"日志中的致命错误：{g['title']}",
            "desc": f"{g['meaning']}（出现 {g['count']} 次）",
            "note": g.get("fix", ""),
        })

    # 冲突
    if conflicts:
        crit = [f for f in conflicts.findings if f.severity == "critical"]
        high = [f for f in conflicts.findings if f.severity == "high"]
        if crit:
            causes.append({
                "title": f"存在 {len(crit)} 处致命模组冲突",
                "desc": "、".join(f.title for f in crit[:3]),
                "note": "这是崩溃最可能的根因。请运行「一键最优处理」自动修复。",
            })
            actions.append("运行「一键最优处理」，自动调整加载顺序并生成兼容补丁")
        if high and len(high) > 20:
            causes.append({
                "title": f"存在 {len(high)} 处严重冲突",
                "desc": "大量模组互相覆盖同一批文件或定义。",
                "note": "冲突密度过高会让游戏状态变得不稳定，增加崩溃概率。",
            })

    # 版本问题
    if conflicts:
        old = [f for f in conflicts.findings
               if f.kind == "V1" and f.severity in ("critical", "high")]
        if old:
            causes.append({
                "title": f"{len(old)} 个模组与当前游戏版本不符",
                "desc": "、".join(f.title for f in old[:3]),
                "note": "跨版本使用模组是最容易导致崩溃的原因之一。",
            })
            actions.append("更新或禁用版本不匹配的模组（见冲突检测的「版本兼容」分类）")

    # 内存
    if analysis:
        memg = [g for g in analysis.sorted_groups()
                if g.get("category") == "性能与内存"]
        if memg:
            causes.append({
                "title": "检测到内存相关问题",
                "desc": memg[0]["meaning"],
                "note": memg[0].get("fix", ""),
            })
            actions.append("降低星系规模 / 贴图质量，并减少同时启用的重型模组")

    # 兜底
    if not causes:
        causes.append({
            "title": "未能在日志中找到明确的崩溃原因",
            "desc": "错误日志中没有出现已知的致命模式，可能是内存抖动、"
                    "驱动问题或偶发的竞态条件。",
            "note": "建议：① 先用「一键最优处理」理顺模组；"
                    "② 降低画质与星系规模；③ 若稳定复现，记录崩溃前最后的操作。",
        })

    # 通用动作（按已获得的证据排序，最相关的排前面）
    if physfs:
        actions.append("在 Steam 里验证游戏文件完整性"
                       "（属性 → 已安装文件 → 验证游戏文件完整性），"
                       "模组文件损坏是 PhysFS 崩溃的常见原因")
    actions.extend([
        "把本次报告的 HTML 文件发给模组作者或发到社区求助"
        "（已翻译成中文，方便对照）",
        "在 Paradox 启动器中把崩溃前的 playset 完整导出备份",
        "若崩溃稳定复现，用二分法：先禁用一半模组，逐步缩小范围",
    ])

    if severity_rank(causes) == 0:
        conclusion = "崩溃原因不明确，建议按下方清单逐步排查。"
    else:
        top = causes[0]
        # 只取第一句，避免把整段成因说明塞进结论里
        note = (top.get("note") or "").strip()
        first = note.split("。")[0] + "。" if note else ""
        conclusion = f"最可能的原因：{top['title']}。"
        if first and len(first) <= 90:
            conclusion += " " + first
        elif top.get("desc"):
            conclusion += " " + top["desc"].split("。")[0] + "。"

    # 去重
    seen: set[str] = set()
    uniq_actions: list[str] = []
    for a in actions:
        if a not in seen:
            seen.add(a)
            uniq_actions.append(a)
    return conclusion, causes, uniq_actions


def severity_rank(causes: list[dict]) -> int:
    """把归因条目数量当作排序权重（当前实现直接用条数）。"""
    return len(causes)


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------

def _bar(count: int, total: int, width: int = 28) -> str:
    if total <= 0:
        return ""
    n = max(1, int(round(count / total * width)))
    return "█" * n


def build_markdown(rep: CrashReport) -> str:
    """把报告渲染成 Markdown 文本。"""
    L: list[str] = []
    A = L.append
    A(f"# {rep.title}")
    A("")
    A(f"> 生成时间：{rep.generated_at}　|　"
      f"游戏版本：{rep.game_version or '未知'}　|　"
      f"由「熵光矩阵」自动生成")
    A("")
    A("---")
    A("")

    # 1. 结论
    A("## 一、崩溃结论")
    A("")
    A(f"**{rep.conclusion}**")
    A("")
    for i, c in enumerate(rep.causes, 1):
        A(f"### 原因 {i}：{c['title']}")
        A("")
        A(f"- **说明**：{c['desc']}")
        if c.get("note"):
            A(f"- **处理**：{c['note']}")
        A("")

    # 2. 崩溃现场
    A("## 二、崩溃现场（原始数据翻译）")
    A("")
    exc = rep.exception
    if exc.get("exception"):
        code_key = (exc.get("code") or "").upper().lstrip("0")
        info = EXCEPTION_CODES.get(code_key)
        A("| 字段 | 原始值 | 中文解释 |")
        A("| --- | --- | --- |")
        A(f"| 异常类型 | `{exc.get('code','')} ({exc.get('exception','')})` | "
          f"{info[0] if info else '未知异常类型'} |")
        A(f"| 崩溃地址 | `{exc.get('address','—')}` | 崩溃发生的内存地址 |")
        A(f"| 游戏版本 | `{exc.get('version','—')}` | 崩溃时的游戏版本 |")
        A(f"| 发生时间 | `{exc.get('datetime','—')}` | — |")
        A("")
        if info:
            A(f"**这意味着什么**：{info[1]}")
            A("")
            A(f"**常见成因**：{info[2]}")
            A("")
    else:
        A("未找到 `exception.txt`（可能不是标准崩溃，或崩溃转储未生成）。")
        A("")

    if exc.get("frames"):
        A("### 调用栈解读")
        A("")
        A("```")
        A(exc["frames"])
        A("```")
        A("")
        A("**如何看这个栈**（从下往上读，最下面是程序入口）：")
        A("")
        seen_hint: set[str] = set()
        for fn in exc["frames"].splitlines():
            for key, hint in STACK_HINTS.items():
                if key.lower() in fn.lower() and key not in seen_hint:
                    seen_hint.add(key)
                    A(f"- `{fn.strip()}` → {hint}")
        if not seen_hint:
            A("- 栈帧符号缺失，无法进一步解读。这属于正常现象"
              "（发行版不带调试符号）。")
        A("")

    # 3. 模组清单
    A("## 三、崩溃时的模组清单")
    A("")
    A(f"本次会话共加载 **{len(rep.mods_in_session)}** 个模组、"
      f"**{len(rep.dlcs)}** 个 DLC。")
    A("")
    if rep.meta:
        A("| 系统信息 | 值 |")
        A("| --- | --- |")
        for k in ("AppVersion", "BuildType", "Platform", "Store",
                  "OperatingSystem", "SystemLanguage", "CPUModel",
                  "SystemMemory", "ChecksumStatus", "LaunchArguments"):
            if k in rep.meta:
                v = rep.meta[k]
                if k == "SystemMemory":
                    try:
                        v = f"{int(v)} MB（约 {int(v)//1024} GB）"
                    except (ValueError, TypeError):
                        pass
                if k == "ChecksumStatus":
                    v = ("Modified（校验和被修改 —— 说明装了模组，属正常）"
                         if str(v).lower().startswith("modif") else v)
                A(f"| {k} | {v} |")
        A("")
    if rep.mods_in_session:
        A("<details><summary>展开模组列表（按加载顺序）</summary>")
        A("")
        for i, m in enumerate(rep.mods_in_session, 1):
            A(f"{i:3d}. {m}")
        A("")
        A("</details>")
        A("")

    # 4. 日志分析
    A("## 四、错误日志分析（翻译 + 注释）")
    A("")
    an = rep.analysis
    if an and an.parsed_lines:
        sc = an.severity_counts
        A(f"分析了 `{Path(an.path).name}`：共 **{an.total_lines:,}** 行、"
          f"{human_size(an.size_bytes)}。")
        A("")
        A("| 严重度 | 条数 | 说明 |")
        A("| --- | --- | --- |")
        for sev, desc in (
            (SEV_FATAL, "会导致崩溃，必须处理"),
            (SEV_ERROR, "功能已损坏，应当处理"),
            (SEV_WARN, "潜在问题，建议关注"),
            (SEV_NOISE, "无害噪音，可以完全忽略"),
        ):
            A(f"| {SEV_LABEL[sev]} | {sc.get(sev, 0):,} | {desc} |")
        A("")

        groups = an.sorted_groups()
        actionable = [g for g in groups if g["severity"] in (SEV_FATAL, SEV_ERROR)]
        if actionable:
            A("### 需要处理的错误（按严重度排序）")
            A("")
            for g in actionable[:25]:
                A(f"#### [{g['severity_label']}] {g['title']}")
                A("")
                A(f"- **出现次数**：{g['count']:,}　|　"
                  f"**类别**：{g['category']}")
                if g.get("meaning"):
                    A(f"- **这句话在说什么**：{g['meaning']}")
                if g.get("cause"):
                    A(f"- **为什么会这样**：{g['cause']}")
                if g.get("fix"):
                    A(f"- **怎么办**：{g['fix']}")
                srcs = sorted(g.get("sources", {}).items(), key=lambda kv: -kv[1])[:4]
                if srcs:
                    A(f"- **报错来源**："
                      + "、".join(f"`{s}`({n}次)" for s, n in srcs))
                if g.get("samples"):
                    s0 = g["samples"][0]
                    A("")
                    A("<details><summary>原始日志示例</summary>")
                    A("")
                    A("```")
                    A(s0.get("raw", "")[:500])
                    A("```")
                    A("")
                    A("</details>")
                A("")
        else:
            A("### 未发现明确的功能性错误")
            A("")
            A("日志中没有出现知识库认定的「致命/错误」级模式。"
              "这通常意味着崩溃不是由脚本错误直接引起的，"
              "更可能是内存、驱动或竞态问题。")
            A("")

        # 高频但无害的
        noise = [g for g in groups if g["severity"] == SEV_NOISE][:8]
        if noise:
            A("### 高频但无害的噪音（不用管）")
            A("")
            A("| 消息 | 次数 | 为什么可以忽略 |")
            A("| --- | --- | --- |")
            for g in noise:
                note = g.get("cause") or "属于原版游戏自身的历史遗留输出，与模组无关。"
                A(f"| {g['title']} | {g['count']:,} | {note[:90]} |")
            A("")

        if an.top_files:
            A("### 报错最多的源文件")
            A("")
            A("| 源文件 | 条数 |")
            A("| --- | --- |")
            for f, n in an.top_files[:15]:
                A(f"| `{f}` | {n:,} |")
            A("")

        if an.crash_tail:
            A("### 崩溃前最后发生的事")
            A("")
            A("```")
            for ll in an.crash_tail[-25:]:
                A(ll.raw[:200])
            A("```")
            A("")
    else:
        A("未能读取到 error.log。")
        A("")

    # 5. 冲突摘要
    A("## 五、模组冲突检测摘要")
    A("")
    if rep.conflicts:
        c = rep.conflicts
        A(f"**健康分：{c.health_score()} / 100**"
          f"（满分 100，越低越危险）")
        A("")
        A("| 严重度 | 数量 |")
        A("| --- | --- |")
        for sev in ("critical", "high", "medium", "low", "info"):
            A(f"| {C_SEV_LABEL[sev]} | {c.count(sev)} |")
        A("")
        top = [f for f in c.findings if f.severity in ("critical", "high")][:12]
        if top:
            A("### 最严重的冲突")
            A("")
            for f in top:
                A(f"- **[{C_SEV_LABEL[f.severity]}] {f.title}**")
                if f.advice:
                    A(f"  - 建议：{f.advice.splitlines()[0]}")
            A("")
    else:
        A("未做冲突检测（可在主界面点「开始检测」后再生成报告）。")
        A("")

    # 6. 修复清单
    A("## 六、建议的修复步骤（按优先级）")
    A("")
    for i, a in enumerate(rep.actions, 1):
        A(f"{i}. {a}")
    A("")
    A("---")
    A("")
    A("*本报告由「熵光矩阵」自动生成。英文报错已翻译为中文并附上成因解释，"
      "原始日志未被修改。*")
    A("")
    return "\n".join(L)


_CSS = """
:root{
  --bg:#0d1117; --panel:#161b22; --panel2:#1c2330; --line:#2b3444;
  --fg:#e6edf3; --dim:#8b949e; --accent:#7c6cff; --accent2:#4dd4ac;
  --crit:#ff5d5d; --err:#ff8f3f; --warn:#e3b341; --noise:#6e7681;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
  font:15px/1.75 -apple-system,"Segoe UI","Microsoft YaHei UI","Microsoft YaHei",sans-serif;}
.wrap{max-width:1080px;margin:0 auto;padding:40px 24px 90px}
h1{font-size:30px;line-height:1.35;margin:0 0 8px;
  background:linear-gradient(92deg,#a78bfa,#4dd4ac 60%,#60a5fa);
  -webkit-background-clip:text;background-clip:text;color:transparent;}
h2{font-size:21px;margin:46px 0 14px;padding-bottom:9px;
  border-bottom:1px solid var(--line);color:#c9d1d9}
h3{font-size:17px;margin:26px 0 8px;color:#d2d8e0}
h4{font-size:15px;margin:20px 0 6px;color:#c9d1d9}
.meta{color:var(--dim);font-size:13px;margin-bottom:26px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:11px;
  padding:18px 20px;margin:14px 0}
.concl{background:linear-gradient(135deg,#1d2340,#161b22);
  border:1px solid #3b3a6b;border-left:4px solid var(--accent)}
.cause{border-left:3px solid var(--err);background:var(--panel2)}
table{border-collapse:collapse;width:100%;margin:12px 0;font-size:14px}
th,td{border:1px solid var(--line);padding:8px 11px;text-align:left;
  vertical-align:top}
th{background:var(--panel2);color:#c9d1d9;font-weight:600}
code,pre{font-family:"Cascadia Mono",Consolas,"Courier New",monospace}
code{background:#1f2632;padding:2px 6px;border-radius:5px;font-size:13px;
  color:#a5d6ff}
pre{background:#0b0f16;border:1px solid var(--line);border-radius:9px;
  padding:14px 16px;overflow-x:auto;font-size:12.5px;line-height:1.6;
  color:#adbac7;max-height:460px}
details{background:var(--panel2);border:1px solid var(--line);border-radius:9px;
  padding:10px 14px;margin:10px 0}
summary{cursor:pointer;color:#a5b4fc;font-weight:600;font-size:14px}
.badge{display:inline-block;padding:2px 9px;border-radius:99px;font-size:12px;
  font-weight:700;margin-right:7px;vertical-align:1px}
.b-fatal{background:#4a1414;color:#ff9b9b;border:1px solid #7d2020}
.b-error{background:#4a2a10;color:#ffc08a;border:1px solid #7d451a}
.b-warn{background:#453a10;color:#ffe08a;border:1px solid #7d6a1a}
.b-noise{background:#242a33;color:#9aa4b2;border:1px solid #333c48}
.b-crit{background:#4a1414;color:#ff9b9b}
.b-high{background:#4a2a10;color:#ffc08a}
.b-med{background:#453a10;color:#ffe08a}
.b-low{background:#1d2c3a;color:#8fc7ff}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));
  gap:12px;margin:16px 0}
.stat{background:var(--panel);border:1px solid var(--line);border-radius:11px;
  padding:14px 16px}
.stat .v{font-size:25px;font-weight:700;color:#fff}
.stat .k{font-size:12px;color:var(--dim);margin-top:3px}
.kv{color:var(--dim)}
.kv b{color:var(--fg);font-weight:600}
.bar{height:7px;border-radius:4px;background:#2b3444;overflow:hidden;
  margin-top:5px}
.bar>i{display:block;height:100%;background:linear-gradient(90deg,#7c6cff,#4dd4ac)}
ol,ul{padding-left:24px}
li{margin:5px 0}
.foot{color:var(--dim);font-size:12.5px;margin-top:46px;padding-top:18px;
  border-top:1px solid var(--line);text-align:center}
.health{font-size:44px;font-weight:800;line-height:1}
"""


def build_html(rep: CrashReport) -> str:
    """生成带样式的中文 HTML 报告。"""
    e = html.escape

    def esc(s: Any) -> str:
        return e(str(s)) if s is not None else ""

    parts: list[str] = []
    A = parts.append
    A("<!DOCTYPE html><html lang='zh-CN'><head><meta charset='utf-8'>")
    A("<meta name='viewport' content='width=device-width,initial-scale=1'>")
    A(f"<title>{esc(rep.title)}</title>")
    A(f"<style>{_CSS}</style></head><body><div class='wrap'>")

    A(f"<h1>{esc(rep.title)}</h1>")
    A(f"<div class='meta'>生成时间 {esc(rep.generated_at)} ｜ "
      f"游戏版本 {esc(rep.game_version or '未知')} ｜ "
      f"由「熵光矩阵」自动生成</div>")

    # 结论
    A("<div class='card concl'><h3 style='margin-top:0'>崩溃结论</h3>")
    A(f"<p style='font-size:16px'>{esc(rep.conclusion)}</p></div>")

    an = rep.analysis
    cf = rep.conflicts
    health = cf.health_score() if cf else 0
    A("<div class='grid'>")
    A(f"<div class='stat'><div class='health' style='color:"
      f"{'#4dd4ac' if health>=70 else ('#e3b341' if health>=40 else '#ff5d5d')}'>"
      f"{health if cf else '—'}</div><div class='k'>模组健康分 / 100</div></div>")
    A(f"<div class='stat'><div class='v'>{len(rep.mods_in_session)}</div>"
      f"<div class='k'>崩溃时加载的模组</div></div>")
    A(f"<div class='stat'><div class='v'>{an.total_lines:,}</div>"
      f"<div class='k'>分析的日志行数</div></div>" if an else
      "<div class='stat'><div class='v'>—</div><div class='k'>日志</div></div>")
    if an:
        act = len([g for g in an.sorted_groups()
                   if g["severity"] in (SEV_FATAL, SEV_ERROR)])
        A(f"<div class='stat'><div class='v' style='color:"
          f"{'#ff5d5d' if act else '#4dd4ac'}'>{act}</div>"
          f"<div class='k'>需处理的错误种类</div></div>")
    A("</div>")

    # 原因
    A("<h2>一、可能的原因（按可能性排序）</h2>")
    for i, c in enumerate(rep.causes, 1):
        A("<div class='card cause'>")
        A(f"<h4 style='margin-top:0'>原因 {i}：{esc(c['title'])}</h4>")
        A(f"<p>{esc(c['desc'])}</p>")
        if c.get("note"):
            A(f"<p class='kv'><b>处理建议：</b>{esc(c['note'])}</p>")
        A("</div>")

    # 崩溃现场
    A("<h2>二、崩溃现场</h2>")
    exc = rep.exception
    if exc.get("exception"):
        code_key = (exc.get("code") or "").upper().lstrip("0")
        info = EXCEPTION_CODES.get(code_key)
        A("<table><tr><th>字段</th><th>原始值</th><th>中文解释</th></tr>")
        A(f"<tr><td>异常类型</td><td><code>{esc(exc.get('code'))} "
          f"({esc(exc.get('exception'))})</code></td>"
          f"<td>{esc(info[0] if info else '未知')}</td></tr>")
        A(f"<tr><td>崩溃地址</td><td><code>{esc(exc.get('address','—'))}</code></td>"
          f"<td>崩溃发生的内存地址</td></tr>")
        A(f"<tr><td>游戏版本</td><td><code>{esc(exc.get('version','—'))}</code></td>"
          f"<td>—</td></tr>")
        A(f"<tr><td>发生时间</td><td><code>{esc(exc.get('datetime','—'))}</code></td>"
          f"<td>—</td></tr></table>")
        if info:
            A(f"<div class='card'><b>这意味着什么：</b>{esc(info[1])}"
              f"<br><br><b>常见成因：</b>{esc(info[2])}</div>")
    else:
        A("<p>未找到 <code>exception.txt</code>。</p>")

    if exc.get("frames"):
        A("<h3>调用栈解读</h3>")
        A(f"<pre>{esc(exc['frames'])}</pre>")
        A("<p class='kv'>从下往上读，最下面是程序入口。逐帧含义：</p><ul>")
        seen: set[str] = set()
        for fn in exc["frames"].splitlines():
            for key, hint in STACK_HINTS.items():
                if key.lower() in fn.lower() and key not in seen:
                    seen.add(key)
                    A(f"<li><code>{esc(fn.strip())}</code> → {esc(hint)}</li>")
        A("</ul>")

    # 系统信息
    if rep.meta:
        A("<h3>系统与游戏环境</h3><table><tr><th>项目</th><th>值</th></tr>")
        label = {
            "AppVersion": "游戏版本", "BuildType": "构建类型",
            "Platform": "平台", "Store": "发行平台",
            "OperatingSystem": "操作系统", "SystemLanguage": "系统语言",
            "CPUModel": "CPU", "SystemMemory": "内存(MB)",
            "ChecksumStatus": "校验和状态", "LaunchArguments": "启动参数",
            "SCMBranch": "分支",
        }
        for k, v in rep.meta.items():
            if k in label:
                A(f"<tr><td>{esc(label[k])}</td><td>{esc(v)}</td></tr>")
        A("</table>")

    # 模组
    A("<h2>三、崩溃时的模组清单</h2>")
    A(f"<p>共加载 <b>{len(rep.mods_in_session)}</b> 个模组、"
      f"<b>{len(rep.dlcs)}</b> 个 DLC。</p>")
    if rep.mods_in_session:
        A("<details><summary>展开模组列表（按加载顺序）</summary><table>"
          "<tr><th>#</th><th>模组</th></tr>")
        for i, m in enumerate(rep.mods_in_session, 1):
            A(f"<tr><td>{i}</td><td>{esc(m)}</td></tr>")
        A("</table></details>")

    # 日志分析
    A("<h2>四、错误日志分析（翻译 + 注释）</h2>")
    if an and an.parsed_lines:
        A(f"<p>分析了 <code>{esc(Path(an.path).name)}</code>：共 "
          f"<b>{an.total_lines:,}</b> 行、{human_size(an.size_bytes)}。</p>")
        sc = an.severity_counts
        A("<table><tr><th>严重度</th><th>条数</th><th>说明</th></tr>")
        for sev, desc in ((SEV_FATAL, "会导致崩溃，必须处理"),
                          (SEV_ERROR, "功能已损坏，应当处理"),
                          (SEV_WARN, "潜在问题，建议关注"),
                          (SEV_NOISE, "无害噪音，可以完全忽略")):
            cls = {SEV_FATAL: "b-fatal", SEV_ERROR: "b-error",
                   SEV_WARN: "b-warn", SEV_NOISE: "b-noise"}[sev]
            A(f"<tr><td><span class='badge {cls}'>{SEV_LABEL[sev]}</span></td>"
              f"<td>{sc.get(sev,0):,}</td><td>{desc}</td></tr>")
        A("</table>")

        groups = an.sorted_groups()
        actionable = [g for g in groups if g["severity"] in (SEV_FATAL, SEV_ERROR)]
        A("<h3>需要处理的错误</h3>")
        if not actionable:
            A("<p>未发现明确的功能性错误。崩溃更可能来自内存、驱动或竞态问题。</p>")
        for g in actionable[:25]:
            cls = "b-fatal" if g["severity"] == SEV_FATAL else "b-error"
            A("<div class='card'>")
            A(f"<h4 style='margin-top:0'><span class='badge {cls}'>"
              f"{esc(g['severity_label'])}</span>{esc(g['title'])}</h4>")
            A(f"<p class='kv'>出现 <b>{g['count']:,}</b> 次 ｜ "
              f"类别：<b>{esc(g['category'])}</b></p>")
            if g.get("meaning"):
                A(f"<p><b>这句话在说什么：</b>{esc(g['meaning'])}</p>")
            if g.get("cause"):
                A(f"<p><b>为什么会这样：</b>{esc(g['cause'])}</p>")
            if g.get("fix"):
                A(f"<p style='color:#4dd4ac'><b>怎么办：</b>{esc(g['fix'])}</p>")
            srcs = sorted(g.get("sources", {}).items(), key=lambda kv: -kv[1])[:4]
            if srcs:
                A("<p class='kv'>报错来源：" + "、".join(
                    f"<code>{esc(s)}</code>({n}次)" for s, n in srcs) + "</p>")
            if g.get("samples"):
                A("<details><summary>原始日志示例</summary><pre>"
                  + esc(g["samples"][0].get("raw", "")[:600]) + "</pre></details>")
            A("</div>")

        noise = [g for g in groups if g["severity"] == SEV_NOISE][:8]
        if noise:
            A("<h3>高频但无害的噪音（不用管）</h3>")
            A("<table><tr><th>消息</th><th>次数</th><th>为什么可以忽略</th></tr>")
            for g in noise:
                note = g.get("cause") or "原版游戏自身的历史遗留输出。"
                A(f"<tr><td>{esc(g['title'])}</td><td>{g['count']:,}</td>"
                  f"<td>{esc(note[:100])}</td></tr>")
            A("</table>")

        if an.top_files:
            A("<h3>报错最多的源文件</h3><table><tr><th>源文件</th><th>条数</th>"
              "<th>占比</th></tr>")
            tot = sum(n for _, n in an.top_files[:15]) or 1
            for f, n in an.top_files[:15]:
                A(f"<tr><td><code>{esc(f)}</code></td><td>{n:,}</td>"
                  f"<td><div class='bar'><i style='width:"
                  f"{min(100, n/tot*100):.1f}%'></i></div></td></tr>")
            A("</table>")

        if an.crash_tail:
            A("<h3>崩溃前最后发生的事</h3><pre>")
            for ll in an.crash_tail[-25:]:
                A(esc(ll.raw[:200]))
            A("</pre>")
    else:
        A("<p>未能读取到 error.log。</p>")

    # 冲突
    A("<h2>五、模组冲突检测摘要</h2>")
    if cf:
        A(f"<p>健康分 <b>{cf.health_score()}</b> / 100</p>")
        rows = "".join(
            f"<tr><td>{esc(C_SEV_LABEL[s])}</td><td>{cf.count(s)}</td></tr>"
            for s in ("critical", "high", "medium", "low", "info"))
        A(f"<table><tr><th>严重度</th><th>数量</th></tr>{rows}</table>")
        top = [f for f in cf.findings if f.severity in ("critical", "high")][:12]
        if top:
            A("<h3>最严重的冲突</h3><ul>")
            for f in top:
                A(f"<li><b>[{esc(C_SEV_LABEL[f.severity])}] {esc(f.title)}</b>"
                  + (f"<br><span class='kv'>{esc(f.advice.splitlines()[0])}</span>"
                     if f.advice else "") + "</li>")
            A("</ul>")
    else:
        A("<p>本次未做冲突检测。</p>")

    # 修复清单
    A("<h2>六、建议的修复步骤（按优先级）</h2><ol>")
    for a in rep.actions:
        A(f"<li>{esc(a)}</li>")
    A("</ol>")

    A("<div class='foot'>本报告由「熵光矩阵」自动生成 — "
      "英文报错已翻译为中文并附成因解释，原始日志未被修改。</div>")
    A("</div></body></html>")
    return "".join(parts)


# ---------------------------------------------------------------------------
# 顶层生成
# ---------------------------------------------------------------------------

def report_output_dir(paths: Paths) -> Path:
    """报告输出目录（群星数据目录下的「崩溃报告」）。"""
    d = Path(paths.user_dir) / REPORT_DIR_NAME
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        d = app_data_dir() / REPORT_DIR_NAME
        d.mkdir(parents=True, exist_ok=True)
    return d


def generate_crash_report(paths: Paths, crash_dir: str | None = None, *,
                          conflicts: ConflictReport | None = None,
                          progress=None,
                          log_name: str = "error.log") -> CrashReport:
    """生成一份完整崩溃报告。"""
    def tick(msg: str, frac: float) -> None:
        if progress:
            progress(msg, frac)

    rep = CrashReport()
    rep.generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 选崩溃目录
    if not crash_dir:
        dirs = find_crash_dirs(paths, limit=1)
        crash_dir = dirs[0]["path"] if dirs else ""
        if dirs:
            rep.title = f"群星崩溃分析报告 — {dirs[0]['name']}"
    else:
        rep.title = f"群星崩溃分析报告 — {Path(crash_dir).name}"

    tick("读取崩溃转储信息", 0.05)
    if crash_dir and is_dir(crash_dir):
        rep.exception = read_exception(crash_dir)
        meta = parse_meta_yml(os.path.join(crash_dir, "meta.yml"))
        rep.mods_in_session = meta["mods"]
        rep.dlcs = meta["dlcs"]
        rep.meta = meta["info"]
        rep.game_version = rep.meta.get("AppVersion", "") or rep.exception.get(
            "version", "")
    else:
        rep.game_version = rep.exception.get("version", "")

    if not rep.title:
        rep.title = "群星运行日志分析报告"
    if not rep.game_version:
        # 从 game.log 兜底
        gl = paths.logs_dir / "game.log"
        if is_file(gl):
            try:
                m = re.search(r"Game Version:\s*(.+)", read_text(gl))
                if m:
                    rep.game_version = m.group(1).strip()
            except OSError:
                pass

    # 选日志：优先崩溃目录里的（那是崩溃瞬间的快照）
    log_path = ""
    if crash_dir and is_file(os.path.join(crash_dir, log_name)):
        log_path = os.path.join(crash_dir, log_name)
    else:
        cand = paths.logs_dir / log_name
        if is_file(cand):
            log_path = str(cand)

    tick("分析错误日志", 0.15)
    if log_path:
        rep.analysis = analyze_log(log_path, progress=progress)

    tick("归因分析", 0.9)
    rep.conclusion, rep.causes, rep.actions = diagnose(
        rep.exception, rep.analysis, conflicts, rep.mods_in_session)

    if conflicts is None:
        rep.actions.insert(0, "在主界面运行「开始检测」，把模组冲突一起纳入报告")

    # 写出
    tick("生成报告文件", 0.95)
    out = report_output_dir(paths)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = f"崩溃报告_{stamp}"
    md = out / f"{base}.md"
    ht = out / f"{base}.html"
    try:
        write_text(md, build_markdown(rep))
        rep.md_path = str(md)
    except OSError:
        pass
    try:
        write_text(ht, build_html(rep))
        rep.html_path = str(ht)
    except OSError:
        pass

    # 同步一份 JSON，便于程序再读
    try:
        write_text(out / f"{base}.json",
                   json.dumps(rep.to_dict(), ensure_ascii=False, indent=2),
                   bom=False)
    except (OSError, TypeError, ValueError):
        pass

    tick("完成", 1.0)
    return rep
