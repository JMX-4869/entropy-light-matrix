"""AI 分析层：让模型读懂崩溃、日志、加载顺序与界面主题。

为什么单独一层
--------------
崩溃报告、日志分析、加载顺序、外观生成、时之虫档案都要"问 AI"，
但它们的**输入形态完全不同**（崩溃栈 / 日志行 / 模组清单 / 主题令牌 / 知识条目）。

统一的地方在于三件事，所以抽到这里：

1. 统一的**请求入口**（``ask`` / ``ask_json``），含超时、重试、JSON 容错
2. 统一的**隐私闸门**：没开 ``allow_send_mod_content`` 一律拒绝，
   不靠调用方自觉
3. 统一的**结构化解析**：把模型回复尽量解析成 JSON，
   解析不了就退化成纯文本，而不是直接报错

设计取向：宁可拿到"不太规整但有用"的结果，也不要在格式上卡死。
模型不总是听话，所以每个入口都要能在 JSON 失败时降级。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from .client import build_client  # noqa: F401  (保留给调用方按需引用)


class AiNotReady(Exception):
    """AI 未配置或未授权发送内容。

    ``reason`` 是给用户看的一句话，``kind`` 供界面区分处理方式：
    ``consent`` 要去 AI 接入页开开关，``config`` 要去填 Key / 模型。
    """

    def __init__(self, reason: str, kind: str = "config") -> None:
        """``kind`` 决定界面怎么引导用户：``consent`` 去开隐私开关，
        ``config`` 去填 Key / 模型，``error`` 只是普通失败。"""
        super().__init__(reason)
        self.reason = reason
        self.kind = kind


def ensure_ready(settings: Any) -> None:
    """检查 AI 是否可用；不可用就抛 :class:`AiNotReady`。

    **隐私闸门放在这里**，而不是每个调用点各写一遍——
    漏一处就意味着模组内容会在用户不知情的情况下被发出去。
    """
    ai = settings.ai
    if not getattr(ai, "allow_send_mod_content", False):
        raise AiNotReady(
            "「允许发送模组内容给 AI」未打开。这个功能需要把数据发给服务商，"
            "请先到「AI 接入」页显式开启。", "consent")
    if not getattr(ai, "configured", False):
        raise AiNotReady(
            "AI 尚未配置完成（缺少 API Key 或模型名）。", "config")


def _strip_fence(text: str) -> str:
    """去掉 ```json ... ``` 围栏。模型很爱加，不加处理会解析失败。"""
    s = (text or "").strip()
    m = re.match(r"^```[a-zA-Z]*\s*\n(.*?)\n?```\s*$", s, re.S)
    if m:
        return m.group(1).strip()
    return s


def parse_json_loose(text: str) -> Any:
    """尽量从模型回复里挖出 JSON。

    依次尝试：整段解析 → 去围栏 → 截取第一个 ``{...}`` / ``[...]``。
    全都失败返回 ``None``（调用方应降级处理，而不是报错终止）。
    """
    for cand in (text, _strip_fence(text)):
        try:
            return json.loads(cand)
        except (TypeError, ValueError):
            pass
    s = _strip_fence(text)
    for open_ch, close_ch in (("{", "}"), ("[", "]")):
        i = s.find(open_ch)
        j = s.rfind(close_ch)
        if 0 <= i < j:
            try:
                return json.loads(s[i:j + 1])
            except ValueError:
                continue
    return None


def ask(settings: Any, system: str, user: str, *,
        temperature: float = 0.2,
        should_stop: Callable[[], bool] | None = None) -> str:
    """发一次请求，返回模型原始文本。

    * 未授权 / 未配置 → 抛 :class:`AiNotReady`（界面据此引导用户去开启）
    * 请求本身失败 → 抛 :class:`RuntimeError`，消息是给用户看的一句话

    ``client.chat`` 是**阻塞**的，没有中途回调，所以 ``should_stop``
    只能在发请求前后检查——这是接口本身的限制，不假装能中途取消。
    """
    from .client import AIError, ChatMessage, build_client

    ensure_ready(settings)
    if should_stop and should_stop():
        raise RuntimeError("已取消")

    cfg = settings.ai.to_dict()
    # 分析类任务要的是稳定复现，温度默认压低；
    # 生成主题这种要发散的场景由调用方自己调高。
    cfg["temperature"] = temperature
    try:
        client = build_client(cfg)
    except AIError as exc:
        raise RuntimeError(str(exc)) from exc

    try:
        text = client.chat([
            ChatMessage("system", system),
            ChatMessage("user", user),
        ])
    except AIError as exc:
        raise RuntimeError(str(exc)) from exc
    except Exception as exc:                       # noqa: BLE001
        raise RuntimeError(f"调用 AI 时发生未预期错误：{exc!r}") from exc

    if should_stop and should_stop():
        raise RuntimeError("已取消")
    return text or ""


def ask_json(settings: Any, system: str, user: str, *,
             temperature: float = 0.2,
             should_stop: Callable[[], bool] | None = None) -> Any:
    """发一次请求并解析成 JSON；解析不了返回 ``None``（不抛异常）。

    **未授权 / 请求失败仍然抛**：调用方需要知道"是没结果还是没资格问"，
    否则界面只能显示一个空列表，用户完全不知道要先去开开关。
    """
    return parse_json_loose(ask(settings, system, user,
                                temperature=temperature,
                                should_stop=should_stop))


def _fail(exc: Exception) -> dict:
    """把异常转成统一的失败返回，带上 ``kind`` 供界面分流处理。"""
    return {"ok": False, "error": str(exc),
            "kind": getattr(exc, "kind", "error")}


# ---------------------------------------------------------------------------
# 结构化结论
# ---------------------------------------------------------------------------

@dataclass
class AiIssue:
    """AI 分析出来的一条问题。"""

    title: str = ""
    detail: str = ""
    severity: str = "medium"        # critical | high | medium | low
    cause: str = ""                 # 错误原因
    fix: str = ""                   # 建议怎么修
    mods: list[str] = field(default_factory=list)
    target: str = ""                # 关联的模组/对象/文件
    source: str = ""                # crash | log | order | manual
    source_ref: str = ""            # 来源标识（崩溃目录名 / 日志名）
    ai_patchable: bool = True       # 是否适合用补丁解决
    files: list[dict] = field(default_factory=list)  # AI 给出的补丁文件

    def key(self) -> str:
        """稳定标识，用于"已解决"去重。"""
        base = self.target or self.title
        return f"{self.source}:{self.source_ref}:{base}".lower()[:200]

    def to_dict(self) -> dict:
        """序列化，供界面渲染问题卡片与传给补丁生成。"""
        return {
            "key": self.key(), "title": self.title, "detail": self.detail,
            "severity": self.severity, "cause": self.cause, "fix": self.fix,
            "mods": self.mods, "target": self.target, "source": self.source,
            "source_ref": self.source_ref, "ai_patchable": self.ai_patchable,
            "files": self.files,
            "severity_label": SEV_LABEL.get(self.severity, self.severity),
        }


SEV_LABEL = {"critical": "致命", "high": "严重", "medium": "中等",
             "low": "轻微"}

_ISSUE_SYSTEM = """你是《群星》(Stellaris) 模组与脚本专家。
用户会给你一份诊断数据（崩溃栈 / 游戏日志 / 模组冲突 / 加载顺序）。
请只输出 JSON，不要任何解释文字，格式：

{
  "summary": "一句话总体判断（中文，不超过 60 字）",
  "issues": [
    {
      "title": "问题标题（中文，一句话）",
      "severity": "critical|high|medium|low",
      "cause": "根本原因（中文，说清机制，不要泛泛而谈）",
      "fix": "怎么修（中文，可操作）",
      "mods": ["相关模组名"],
      "target": "具体的文件名 / 对象名 / 模组名",
      "ai_patchable": true
    }
  ]
}

要求：
- 只报**你从数据里真能看出来**的问题，不要编造；看不出来就少报几条
- `target` 必须来自数据里真实出现过的名字，方便程序去定位
- `ai_patchable` 表示"能否通过加载一个补丁模组解决"：
  脚本对象重定义、文件覆盖、本地化缺失 → true；
  模组缺失依赖、版本不符、硬件问题 → false
- 最多 8 条，按严重程度从高到低排
"""


_AUDIT_SYSTEM = """你是《群星》(Stellaris) 模组兼容与启动失败的诊断专家。

用户会给你一份**全面排查报告**，由八类互相独立的数据汇总而成：
① 模组冲突检测（6 类）② 崩溃报告（上次为什么崩）
③ 游戏日志（error.log 归类的报错）④ 模组对成就的影响
⑤ 联机校验影响 ⑥ 运行时监控（内存峰值、崩溃次数）
⑦ 已有补丁与未解决项 ⑧ DLC 启用状态

## 你的核心任务：交叉关联，而不是逐条复述

单看某一类数据只能得到表层结论（"有 512 处冲突"）。真正的病因在**交叉点**上：

- 崩溃栈里出现的模组 → 回头看它是否在冲突名单里
- 日志里反复报错的**对象名/文件** → 去冲突列表里找谁在覆盖它
- 内存峰值逼近上限 → 和"大型模组同时启用"关联，而不是只说"内存高"
- 某个模组同时出现在「冲突」「成就影响」「校验影响」→ 它是优先级最高的嫌疑
- 崩溃时段的加载顺序 → 排在崩溃相关模组**前面**的那个才是覆盖方

**先做这个交叉推理，再写结论。** 只罗列数据等于没做。

## 输出格式（只输出 JSON，不要任何解释文字）

{
  "summary": "一句话总体判断（中文，不超过 80 字，必须点出最关键的那一个病因）",
  "issues": [
    {
      "title": "问题标题（中文，一句话）",
      "severity": "critical|high|medium|low",
      "cause": "根本原因（中文，说清机制与证据链：你从哪两类数据对上了）",
      "fix": "怎么修（中文，可操作；能靠补丁模组解决就说清补什么）",
      "mods": ["相关模组名"],
      "target": "具体的文件名 / 对象名 / 模组名",
      "ai_patchable": true
    }
  ]
}

## 硬性要求

- **只报数据里真看得出来的**。看不出来就少报几条，宁可 3 条准的，不要 8 条套话。
- `cause` 里要写出**证据来源**（例如"崩溃栈的 X 与冲突 #12 的覆盖方是同一个模组"）。
- `target` 必须是数据里真实出现过的名字，程序要靠它去定位。
- `ai_patchable`：脚本对象重定义 / 文件覆盖 / 本地化缺失 → true；
  模组缺失依赖 / 版本不符 / 内存不足 / 硬件或存档损坏 → false。
- **按"能不能让游戏正常启动"排序**，不是按冲突数量排。
- 最多 10 条。
- 如果数据里**没有**崩溃和日志（用户没玩过或没留存），就明确基于冲突与排序给结论，
  不要编造崩溃原因。

## 关于调用栈：别硬读

发布版游戏的调用栈**符号是混淆的** —— 你会看到同一个内部函数名
（如 `PHYSFS_swapSLE64`）重复十几遍，这不是线索，是符号表缺失。

真正有诊断价值的是：
1. **异常码**（报告里已给出人话含义）—— 它指向原因**类别**
2. **崩溃时加载的模组清单** —— 哪些模组当时在场
3. **error.log 里崩溃前的报错** —— 具体哪个对象/文件出问题

所以：拿到访问违例 + 一堆混淆帧时，不要浪费时间逐帧解读，
直接去做「异常码类别 × 当时加载的模组 × 日志报错」的交叉推理。
"""


def analyze(settings: Any, *, kind: str, payload: str,
            should_stop: Callable[[], bool] | None = None,
            source_ref: str = "", system: str = "") -> dict:
    """通用分析入口：把一段诊断数据交给 AI，拿回结构化结论。

    ``payload`` 由调用方拼好（崩溃栈、日志片段、模组清单…），
    这里只负责提示词框架与结果解析。

    ``system`` 是**可选的自定义系统提示词**，默认用通用的
    ``_ISSUE_SYSTEM``。「AI 接管 · 全面排查」要喂八类数据、还要求做
    交叉关联 —— 用通用提示词时模型只会盯最显眼的那一类（通常是冲突），
    把崩溃与日志里的线索放过，所以那条路径传自己的 ``_AUDIT_SYSTEM``。
    """
    sys_prompt = system or _ISSUE_SYSTEM
    user = f"【诊断数据类型】{kind}\n\n{payload}"
    try:
        data = ask_json(settings, sys_prompt, user, temperature=0.15,
                        should_stop=should_stop)
    except (AiNotReady, RuntimeError) as exc:
        return _fail(exc)
    if not isinstance(data, dict):
        # 模型没给 JSON：退化成纯文本，仍然把内容带回去给用户看
        try:
            raw = ask(settings, sys_prompt, user, temperature=0.15,
                      should_stop=should_stop)
        except (AiNotReady, RuntimeError) as exc2:
            return _fail(exc2)
        return {"ok": True, "summary": "", "issues": [],
                "raw": raw, "parsed": False}
    issues = []
    for it in (data.get("issues") or [])[:12]:
        if not isinstance(it, dict):
            continue
        sev = str(it.get("severity") or "medium").lower()
        if sev not in SEV_LABEL:
            sev = "medium"
        try:
            patchable = bool(it.get("ai_patchable", True))
        except Exception:
            patchable = True
        mods = it.get("mods") or []
        issues.append(AiIssue(
            title=str(it.get("title") or "")[:200],
            detail=str(it.get("detail") or "")[:2000],
            severity=sev,
            cause=str(it.get("cause") or "")[:2000],
            fix=str(it.get("fix") or "")[:2000],
            mods=[str(x) for x in mods if x][:20],
            target=str(it.get("target") or "")[:200],
            source=kind, source_ref=source_ref,
            ai_patchable=patchable,
        ))
    return {"ok": True, "summary": str(data.get("summary") or "")[:300],
            "issues": [i.to_dict() for i in issues], "parsed": True}


# ---------------------------------------------------------------------------
# 让 AI 直接产出补丁文件
# ---------------------------------------------------------------------------

_PATCH_SYSTEM = """你是《群星》(Stellaris) 模组补丁专家。
用户会给你一个具体问题。请给出能解决它的**补丁文件**，只输出 JSON：

{
  "explanation": "这个补丁做什么（中文）",
  "risk": "风险提示（中文，没有就留空）",
  "files": [
    {"rel": "common/scripted_triggers/zzz_fix.txt",
     "text": "文件内容（完整的 Clauzewitz 脚本）"}
  ]
}

硬性要求：
- 文件内容必须是**完整可加载**的脚本，不要写省略号或伪代码
- 路径必须落在这些目录之一：common/ events/ localisation/ interface/
  gfx/ flags/ sound/ map/ prescripted_countries/ history/
- 补丁模组**最后加载**，所以想覆盖同名对象时，直接重写该对象的完整定义即可
- 本地化文件用 UTF-8 BOM + `l_simp_chinese:` 开头，`key:0 "文本"`
- 不确定就少给文件，不要编造不存在的对象名
"""


def make_patch(settings: Any, *, title: str, detail: str,
               should_stop: Callable[[], bool] | None = None) -> dict:
    """针对一条问题，让 AI 直接产出补丁文件。"""
    user = f"【问题】{title}\n\n【详情】\n{detail}\n\n请给出补丁。"
    try:
        data = ask_json(settings, _PATCH_SYSTEM, user, temperature=0.1,
                        should_stop=should_stop)
    except (AiNotReady, RuntimeError) as exc:
        return _fail(exc)
    if not isinstance(data, dict):
        return {"ok": False, "error": "模型没有返回可解析的 JSON",
                "raw": ""}
    files = []
    for f in (data.get("files") or [])[:20]:
        if not isinstance(f, dict):
            continue
        rel = str(f.get("rel") or "").strip().replace("\\", "/").lstrip("/")
        text = f.get("text")
        if rel and isinstance(text, str) and text.strip():
            files.append({"rel": rel, "text": text})
    if not files:
        return {"ok": False, "error": "模型没有产出任何补丁文件",
                "explanation": str(data.get("explanation") or "")}
    return {"ok": True, "files": files,
            "explanation": str(data.get("explanation") or "")[:2000],
            "risk": str(data.get("risk") or "")[:2000]}


# ---------------------------------------------------------------------------
# 界面主题：用提示词生成外观
# ---------------------------------------------------------------------------

_THEME_SYSTEM = """你是 UI 设计师，为《群星》(Stellaris) 模组工具的界面设计配色主题。
用户会用一句话描述想要的风格。请只输出 JSON 主题令牌：

{
  "name": "主题名（中文，2~6 字）",
  "tokens": {
    "bg": "#0a0e17", "bg2": "#0d1220", "panel": "#111827",
    "panel2": "#151d2e", "panel3": "#1b2438",
    "fg": "#e6edf7", "dim": "#9fb0cc", "dim2": "#6b7d99",
    "line": "#1e293b", "line2": "#28354d",
    "acc": "#38bdf8", "acc2": "#a78bfa", "acctext": "#04121f",
    "ok": "#34d399", "warn": "#fbbf24", "high": "#fb923c",
    "crit": "#f87171", "info": "#60a5fa",
    "scheme": "dark"
  }
}

要求：
- 所有颜色必须是 `#rrggbb` 十六进制
- `scheme` 只能是 `dark` 或 `light`；选 `light` 时 `bg`/`panel` 必须真的是浅色
- 半透明一律不要，用实色
- 令牌名必须与上面完全一致，一个不多一个不少

**对比度（按 WCAG 算，不合格会被自动修正，但请一次给对）：**
- `fg` 与 `bg` 的对比度 ≥ 4.5（正文要看得清）
- `dim`、`dim2` 与 `bg` 的对比度 ≥ 3.0（次要文字也不能糊）
- `acctext` 与 `acc` 的对比度 ≥ 4.5（这是按钮上的字，必须压得住底色）
- `ok`/`warn`/`high`/`crit`/`info` 与 `bg` 的对比度 ≥ 3.0（状态色要一眼可辨）
- `acc` 与 `bg` 的对比度 ≥ 3.0

**层级（这是好看与否的关键）：**
- `bg` → `bg2` → `panel` → `panel2` → `panel3` 是**由远及近**的五层，
  每层的亮度差要小（相邻两层差 2~6%），整串加起来能看出层次但不能断裂
- `fg` 最亮、`dim` 次之、`dim2` 最弱，三者要有明确梯度，不能挤在一起
- `acc` 是点缀色，用量少、饱和度高；`acc2` 是它的邻近色（不做撞色主调）
"""

THEME_TOKEN_KEYS = (
    "bg", "bg2", "panel", "panel2", "panel3", "fg", "dim", "dim2",
    "line", "line2", "acc", "acc2", "acctext", "ok", "warn", "high",
    "crit", "info", "scheme",
)

_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")


def make_theme(settings: Any, prompt: str, *, base: dict | None = None,
               should_stop: Callable[[], bool] | None = None) -> dict:
    """按提示词生成一套界面主题令牌。

    ``base`` 是当前主题（作为兜底）：模型漏给某个令牌时用它补上，
    这样**永远不会因为少一个字段而生成出坏主题**。
    """
    user = f"【想要的风格】{prompt}\n"
    if base:
        user += ("\n【当前主题（参考，可全部改掉）】\n"
                 + json.dumps({k: base.get(k) for k in THEME_TOKEN_KEYS
                               if k in base}, ensure_ascii=False))
    try:
        data = ask_json(settings, _THEME_SYSTEM, user, temperature=0.7,
                        should_stop=should_stop)
    except (AiNotReady, RuntimeError) as exc:
        return _fail(exc)
    if not isinstance(data, dict):
        return {"ok": False, "error": "模型没有返回可解析的主题 JSON"}
    raw = data.get("tokens")
    if not isinstance(raw, dict):
        raw = data                     # 有些模型会直接把令牌放在顶层

    tokens: dict[str, str] = {}
    bad: list[str] = []
    for k in THEME_TOKEN_KEYS:
        v = raw.get(k)
        if k == "scheme":
            tokens[k] = "light" if str(v).lower() == "light" else "dark"
            continue
        if isinstance(v, str) and _HEX.match(v.strip()):
            tokens[k] = v.strip().lower()
        elif base and k in base:
            tokens[k] = base[k]        # 兜底
            bad.append(k)
        else:
            bad.append(k)
    missing = [k for k in THEME_TOKEN_KEYS if k not in tokens]
    if missing:
        return {"ok": False,
                "error": "模型给的颜色不完整或格式不对："
                         + "、".join(missing[:6])}

    # ---- 可用性校验：格式对 ≠ 看得清 ----
    #
    # 模型经常交出"每项都是合法 #rrggbb、但文字和背景几乎同色"的主题。
    # 光验格式会把它当成功收下，用户拿到一套**字都看不见**的界面，
    # 还以为是自己不会调。
    #
    # 所以这里按 WCAG 算对比度，不达标的**自动修**而不是报错：
    # 让用户对着"生成失败"反复重试是最糟的体验，而对比度是可以算出来、
    # 也可以推到达标的（往背景的反方向提亮/压暗前景）。
    from .. import theme as _theme

    fixed, changed = _theme.auto_fix_contrast(tokens)
    issues: list[dict] = []
    if changed:
        issues.append({
            "level": "warn",
            "message": "已自动调整对比度：" + "、".join(changed[:6])
                       + ("…" if len(changed) > 6 else ""),
            "field": ",".join(changed[:6]),
        })
    remaining = _theme.check_theme_contrast(fixed)
    if remaining:
        worst = min(remaining, key=lambda r: r[2] / r[3])
        issues.append({
            "level": "warn",
            "message": f"{len(remaining)} 处对比度仍偏低"
                       f"（最低 {worst[0]}/{worst[1]} = {worst[2]:.2f}，"
                       f"建议 {worst[3]}）",
            "field": worst[0],
        })
    return {"ok": True, "name": str(data.get("name") or "AI 主题")[:20],
            "tokens": fixed, "fallback": bad, "issues": issues}
