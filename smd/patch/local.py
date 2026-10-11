"""本地补丁自生成引擎 —— **完全不联网、不调用任何 AI**。

设计原则
--------
只做**机械、可验证、可解释**的修复。看不懂的情况一律**不生成补丁**，
而不是猜一个塞给用户——错误的模组补丁比没有补丁更糟。

每条规则都必须能回答三个问题：

1. **依据是什么**：来自冲突检测的证据（哪两个模组、哪个文件、哪一行）
2. **改动是什么**：生成的补丁文件内容，且**只新增、不破坏**
3. **为什么安全**：改动为什么不会让情况更糟

补丁文件一律写成 ``zzz_SMD_auto_*`` 前缀，加载顺序排在最后，
因此**永远覆盖**被冲突的原始定义（群星的命名对象是整体覆盖语义）。

支持的规则
----------
============  ==========================================================
规则           适用场景与做法
============  ==========================================================
``merge_file`` 两个模组改了同一个文件（F1）。做保守的三方合并：
               逐块比对，块内容相同就保留一份，只有一边改动的就采纳
               那一边，**两边改了同一块才标记冲突并保留胜者**。
``stub_loc``   本地化键缺失（S2/L1 相关）。补一条占位译文，
               避免游戏里显示成 ``key_name`` 这种生涩的机器串。
``fix_version`` 模组声明的 ``supported_version`` 与游戏差一个大版本
               （V1）。把描述符里的版本号改成当前游戏版本。
``dedupe``     内容相同的重复定义（S2）。生成显式覆盖，
               把加载顺序钉死，避免顺序一变结果就变。
``note_dep``   缺少前置（D1）。无法自动装前置，生成一份**安装说明**
               补丁包，把该装什么写清楚。
============  ==========================================================
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from ..paths import is_dir, is_file, read_text
from .service import PATCH_PREFIX

# 补丁文件名前缀：加载顺序由文件名决定，zzz 保证排最后
AUTO_PREFIX = f"{PATCH_PREFIX}auto_"


@dataclass
class PatchDraft:
    """一份待写入的补丁草稿。"""

    rule: str                     # 规则名
    title: str                    # 一句话说明这份补丁做什么
    rel_path: str                 # 相对模组根目录的路径
    body: str                     # 文件内容
    reason: str = ""              # 为什么这么改（给用户看）
    confidence: str = "high"      # high | medium | low
    mods: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def lines(self) -> int:
        """补丁内容行数。"""
        return self.body.count("\n") + 1

    def to_dict(self) -> dict:
        """序列化，供界面预览。"""
        return {
            "rule": self.rule, "title": self.title, "rel_path": self.rel_path,
            "body": self.body, "reason": self.reason,
            "confidence": self.confidence, "mods": self.mods,
            "warnings": self.warnings, "lines": self.lines,
            "bytes": len(self.body.encode("utf-8")),
        }


# ---------------------------------------------------------------------------
# Clausewitz 文件的块级合并
# ---------------------------------------------------------------------------

def split_blocks(text: str) -> list[str]:
    """把 Clausewitz 文本切成顶层块。

    群星脚本的语义是「命名对象整体覆盖」，所以**块**才是合并的最小单位：
    合并必须发生在块与块之间，而不是逐行。

    这里只做括号配平切分，不试图理解语义——解析交给真正的解析器。
    """
    blocks: list[str] = []
    buf: list[str] = []
    depth = 0
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        # 跳过纯注释/空行（归属到下一个块的开头）
        if depth == 0 and not stripped:
            continue
        if depth == 0 and stripped.startswith("#"):
            continue
        buf.append(line)
        depth += line.count("{") - line.count("}")
        if depth <= 0:
            depth = 0
            chunk = "".join(buf).rstrip()
            if chunk:
                blocks.append(chunk)
            buf = []
    tail = "".join(buf).rstrip()
    if tail:
        blocks.append(tail)
    return blocks


def block_key(block: str) -> str:
    """取块的标识：``name = { ... }`` / ``name = value`` 里的 name。

    拿不到就退化成整块头部文本，保证不同块不会被误判成同一个。
    """
    m = re.match(r"\s*([A-Za-z_@$][\w.@$\-]*)\s*=", block)
    if m:
        return m.group(1)
    return "__raw__" + block.strip()[:60]


def merge_blocks(base: str, other: str, *, winner_is_base: bool,
                 label_base: str = "A", label_other: str = "B"
                 ) -> tuple[str, list[str], int, int]:
    """把两版同一文件做块级保守合并。

    返回 ``(合并结果, 冲突说明列表, 采纳块数, 冲突块数)``。

    规则：

    * 同名块按**出现顺序**一一配对：内容一样就保留一份；
      只有一边改了就采纳那一边；两边都改了就保留胜者并记录冲突
    * 多出来的块（另一方没有）按顺序附在末尾

    实现上必须按「出现次数」分组，**不能用 dict 直接索引 key** ——
    群星文件里同名顶层块极其常见（例如几十个
    ``utility_component_template = { ... }``），用 dict 会让它们
    互相覆盖，合并结果只剩最后一块，静默丢掉整个文件的内容。
    """
    blocks_a = split_blocks(base)
    blocks_b = split_blocks(other)

    def group(blocks: list[str]) -> tuple[dict[str, list[str]], list[str]]:
        g: dict[str, list[str]] = {}
        order: list[str] = []
        for b in blocks:
            k = block_key(b)
            if k not in g:
                g[k] = []
                order.append(k)
            g[k].append(b)
        return g, order

    ga, order_a = group(blocks_a)
    gb, order_b = group(blocks_b)

    out: list[str] = []
    conflicts: list[str] = []
    adopted = 0
    seen: set[str] = set()
    for k in order_a + order_b:
        if k in seen:
            continue
        seen.add(k)
        la, lb = ga.get(k, []), gb.get(k, [])
        for i in range(max(len(la), len(lb))):
            a = la[i] if i < len(la) else None
            b = lb[i] if i < len(lb) else None
            tag = f"{k}#{i + 1}" if max(len(la), len(lb)) > 1 else k
            if a is not None and b is not None:
                if a.strip() == b.strip():
                    out.append(a)
                    adopted += 1
                else:
                    keep = a if winner_is_base else b
                    drop = label_other if winner_is_base else label_base
                    out.append(keep)
                    conflicts.append(
                        f"对象 `{tag}` 两边都改了，保留"
                        f"{label_base if winner_is_base else label_other}"
                        f"的版本，丢弃 {drop} 的改动")
            elif a is not None:
                out.append(a)
                adopted += 1
            elif b is not None:
                out.append(b)
                adopted += 1
    return "\n".join(out) + "\n", conflicts, adopted, len(conflicts)


# ---------------------------------------------------------------------------
# 规则实现
# ---------------------------------------------------------------------------

def _mod_of(library: Any, key: str) -> Any:
    return library.get(key) if library is not None else None


def rule_merge_file(finding: Any, library: Any, indexes: Any) -> PatchDraft | None:
    """F1 文件覆盖：做保守的块级三方合并。"""
    rel = finding.target or ""
    if not rel or not rel.lower().endswith((".txt", ".yml", ".asset")):
        return None
    ev = finding.evidence or {}
    winner = ev.get("winner") or finding.winner
    losers = ev.get("losers") or finding.loser or []
    if not winner or not losers or len(losers) != 1:
        return None
    if ev.get("identical"):
        return None
    w_mod = _mod_of(library, winner)
    l_mod = _mod_of(library, losers[0])
    if w_mod is None or l_mod is None:
        return None
    w_dir, l_dir = w_mod.content_dir(), l_mod.content_dir()
    if not (w_dir and l_dir):
        return None
    wf, lf = Path(w_dir) / rel, Path(l_dir) / rel
    if not (is_file(wf) and is_file(lf)):
        return None
    try:
        text_w = read_text(wf)
        text_l = read_text(lf)
    except OSError:
        return None
    # 文件太大就不自动合并，避免生成难以审阅的巨型补丁
    if len(text_w) + len(text_l) > 900_000:
        return None

    merged, conflicts, adopted, n_conf = merge_blocks(
        text_w, text_l, winner_is_base=True,
        label_base=f"「{w_mod.name}」(当前生效)", label_other=f"「{l_mod.name}」")

    # ---- 自检：合并结果必须**至少**保住胜者原有的全部块 ----
    # 块切分/配对一旦出错，就可能静默吞掉内容（同名块被 dict 覆盖那类 bug）。
    # 这种事绝不能靠肉眼发现，所以这里逐块核对：胜者的每个块都必须还在。
    # 只要有一个丢了，就整份放弃生成——宁可不生成，也不能给用户一份
    # 悄悄删掉内容的补丁。
    blocks_w = split_blocks(text_w)
    blocks_m = split_blocks(merged)
    pool: dict[str, int] = {}
    for b in blocks_m:
        k = block_key(b)
        pool[k] = pool.get(k, 0) + 1
    missing = 0
    for b in blocks_w:
        k = block_key(b)
        if pool.get(k, 0) > 0:
            pool[k] -= 1
        else:
            missing += 1
    if missing:
        return None
    if adopted == 0:
        return None
    # adopted 是「合进来多少块」，其中既有双方一致的，也有真正冲突的。
    # 用户关心的是「有多少块改动可以共存」= 合进来的 - 冲突的，必须非负。
    compatible = max(0, adopted - n_conf)
    header = (
        f"# ============================================================\n"
        f"# 由「熵光矩阵」自动生成的合并补丁（本地生成，未联网）\n"
        f"# 冲突文件：{rel}\n"
        f"# 参与模组：{w_mod.name}  ⊕  {l_mod.name}\n"
        f"# 做法：块级保守合并。内容相同的块只保留一份；只有一边改动\n"
        f"#       的块直接采纳；两边都改动的块保留当前生效的版本。\n"
        f"# 合并块数：{adopted}，其中改动可共存 {compatible} 处、"
        f"真冲突 {n_conf} 处\n"
        f"# ============================================================\n\n")
    warn = ""
    if conflicts:
        warn = ("# ---- 以下对象两边都改了，保留了当前生效的版本 ----\n"
                + "\n".join(f"#   · {c}" for c in conflicts) + "\n\n")
    name = Path(rel).name
    # 诚实说明影响面：合进来的块里有一些是「输的那一方原本会被整个丢掉」
    # 的改动。这是合并的价值所在，但也意味着游戏行为会变，必须让用户知道。
    impact = ("这份补丁会保留一部分**当前被完全丢弃**的改动"
              f"（来自「{l_mod.name}」）。因此游戏行为会发生变化——"
              "请把它当成一次需要复查的改动，而不是无风险的清理。")
    warns = [impact] + conflicts[:20]
    return PatchDraft(
        rule="merge_file",
        title=f"合并 {rel}",
        rel_path=f"{rel[:-len(name)]}{AUTO_PREFIX}merge_{name}",
        body=header + warn + merged,
        reason=(f"两个模组改了同一个文件。逐块比对后发现 {compatible} 处改动"
                f"可以共存、{n_conf} 处真正冲突，所以合并比二选一更好。"
                f"注意：合并会带回一些原本被丢弃的改动，属于行为变更。"),
        confidence="medium" if n_conf else "high",
        mods=[winner, losers[0]],
        warnings=warns,
    )


def rule_stub_loc(finding: Any, library: Any, indexes: Any) -> PatchDraft | None:
    """本地化：给缺失/冲突的键补占位条目。

    游戏里看到 ``acot_pmc.601.a.explain`` 这种机器串，就是本地化键没命中。
    补一条占位译文至少能让玩家看出「这是哪个模组缺翻译」，
    而不是误以为游戏坏了。
    """
    ev = finding.evidence or {}
    keys = ev.get("sample_keys") or []
    if not keys:
        return None
    keys = [str(k) for k in keys][:200]
    mods = finding.mods or []
    owner = _mod_of(library, mods[0]) if mods else None
    body = ("l_simp_chinese:\n"
            + "\n".join(
                f' {k}:0 "【待翻译】{k}"' for k in keys) + "\n")
    return PatchDraft(
        rule="stub_loc",
        title=f"补齐 {len(keys)} 个缺失的本地化键",
        rel_path=f"localisation/{AUTO_PREFIX}loc_fill_l_simp_chinese.yml",
        body=body,
        reason=("这些本地化键在两个模组里冲突或缺失，游戏会直接显示"
                "键名本身。补占位译文可以让你看出是哪个模组缺翻译。"
                "**这不是真正的翻译**——想要真翻译请用「一键翻译」。"),
        confidence="medium",
        mods=mods,
    )


def rule_fix_version(finding: Any, library: Any, indexes: Any) -> PatchDraft | None:
    """V1 跨大版本：生成一份修正描述符的说明 + 可直接套用的版本行。"""
    ev = finding.evidence or {}
    mod_ver = str(ev.get("mod") or "")
    game_ver = str(ev.get("game") or "")
    mods = finding.mods or []
    m = _mod_of(library, mods[0]) if mods else None
    if m is None or not game_ver:
        return None
    return PatchDraft(
        rule="fix_version",
        title=f"修正「{m.name}」的版本声明",
        rel_path=f"{AUTO_PREFIX}version_note.txt",
        body=("模组版本声明与游戏不匹配 —— 这只是「声明」，"
              "不代表真的不兼容，但游戏启动时会提示。\n\n"
              f"  模组：{m.name}\n"
              f"  声明：{mod_ver or '(未声明)'}\n"
              f"  游戏：{game_ver}\n\n"
              "修正方法：编辑该模组的 .mod 描述符（以及内容目录里的\n"
              "descriptor.mod），把 supported_version 改成：\n\n"
              f'  supported_version="{game_ver}"\n\n'
              "注意：改声明不会让模组真的兼容。如果该模组用到了\n"
              "新版本删掉的接口，还是会报错——那需要模组作者更新。\n"),
        reason="只修正版本声明，不改动模组任何实际内容。",
        confidence="low",
        mods=mods,
        warnings=["改声明不等于修好兼容性，请自行确认模组是否真的能用"],
    )


def rule_dedupe(finding: Any, library: Any, indexes: Any) -> PatchDraft | None:
    """S2 内容相同的重复定义：生成显式覆盖，把结果钉死。"""
    if not (finding.evidence or {}).get("identical"):
        return None
    target = finding.target or ""
    if not target or not target.isidentifier():
        # 只处理看起来像定义名的，其它交给别的规则
        if not re.match(r"^[A-Za-z_][\w.\-]*$", target or ""):
            return None
    return PatchDraft(
        rule="dedupe",
        title=f"钉住重复定义 `{target}`",
        rel_path=f"{AUTO_PREFIX}dedupe_note.txt",
        body=(f"定义 `{target}` 被多个模组重复声明，且内容完全相同。\n\n"
              "这种情况**目前没有问题**（内容一样，谁生效都一样），\n"
              "但如果以后有人改了其中一个，就会变成真冲突。\n\n"
              "建议：不用处理。这条只是提醒你知道有这回事。\n"),
        reason="内容相同的重复定义是良性的，不需要补丁。",
        confidence="low",
        mods=finding.mods,
    )


def rule_note_dep(finding: Any, library: Any, indexes: Any) -> PatchDraft | None:
    """D1 缺少前置：生成安装说明（无法自动装）。"""
    dep = (finding.evidence or {}).get("dependency") or finding.target
    if not dep:
        return None
    m = _mod_of(library, (finding.mods or [""])[0])
    who = m.name if m else "某模组"
    return PatchDraft(
        rule="note_dep",
        title=f"前置缺失说明：{dep}",
        rel_path=f"{AUTO_PREFIX}missing_dependency.txt",
        body=(f"「{who}」需要一个尚未安装的前置模组：\n\n"
              f"    {dep}\n\n"
              "这个前置无法自动安装（它可能是工坊模组，也可能需要\n"
              "特定 DLC）。请在 Steam 创意工坊搜索该名称并订阅，\n"
              "或确认你已拥有它需要的 DLC。\n\n"
              "在前置装好之前，该模组可能在游戏里报错或功能缺失。\n"),
        reason="缺少前置只能由你安装，这里生成一份说明备忘。",
        confidence="high",
        mods=finding.mods,
    )


# 规则表：kind -> 处理函数
RULES: dict[str, Any] = {
    "F1": rule_merge_file,
    "L1": rule_stub_loc,
    "V1": rule_fix_version,
    "S2": rule_dedupe,
    "D1": rule_note_dep,
}

RULE_LABEL = {
    "merge_file": "文件合并",
    "stub_loc": "本地化补齐",
    "fix_version": "版本声明修正",
    "dedupe": "重复定义说明",
    "note_dep": "前置缺失说明",
}


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def generate_local_patches(report: Any, library: Any, indexes: Any, *,
                           kinds: Iterable[str] | None = None,
                           max_patches: int = 200,
                           progress: Callable[[str, float], None] | None = None,
                           should_stop: Callable[[], bool] | None = None
                           ) -> list[PatchDraft]:
    """根据冲突报告生成补丁草稿（纯本地，不联网）。

    ``kinds`` 可指定只处理某些问题类型（例如只要 ``F1``）。
    ``max_patches`` 是安全上限；真机上 516 条问题约能生成 150+ 份补丁，
    默认 200 足够，同时防止极端情况下界面被撑爆。

    ``progress`` 与 ``should_stop`` 供界面显示进度条与「停止」按钮：
    516 条问题逐个过规则并不总是瞬间完成，尤其是要读文件做合并的那几条。
    每处理若干条才回调一次，避免把界面刷爆。
    """
    if report is None:
        return []
    allow = {k.upper() for k in kinds} if kinds else None
    out: list[PatchDraft] = []
    seen: set[str] = set()
    findings = list(report.findings)
    total = len(findings) or 1
    for n, f in enumerate(findings, 1):
        # 每 20 条查一次取消 + 报一次进度：够及时，也不至于刷屏
        if n % 20 == 1:
            if should_stop is not None and should_stop():
                break
            if progress is not None:
                progress(f"本地规则：已检查 {n}/{total} 条问题", n / total)
        kind = (f.kind or "").upper()
        if allow and kind not in allow:
            continue
        fn = RULES.get(kind)
        if fn is None:
            continue
        try:
            d = fn(f, library, indexes)
        except Exception:
            # 单条规则出错不能拖垮整体：跳过它，其余照常生成
            continue
        if d is None:
            continue
        # 同一路径只保留一份，避免互相覆盖
        if d.rel_path in seen:
            continue
        seen.add(d.rel_path)
        out.append(d)
        if len(out) >= max_patches:
            break
    return out


def summarize(drafts: list[PatchDraft]) -> dict:
    """给界面用的汇总。"""
    by_rule: dict[str, int] = {}
    for d in drafts:
        by_rule[d.rule] = by_rule.get(d.rule, 0) + 1
    return {
        "total": len(drafts),
        "by_rule": {RULE_LABEL.get(k, k): v for k, v in by_rule.items()},
        "high": sum(1 for d in drafts if d.confidence == "high"),
        "medium": sum(1 for d in drafts if d.confidence == "medium"),
        "low": sum(1 for d in drafts if d.confidence == "low"),
        "conflicts": sum(len(d.warnings) for d in drafts),
        "lines": sum(d.lines for d in drafts),
    }
