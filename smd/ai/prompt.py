"""提示词构建：把冲突证据整理成模型能用的上下文。

关键约束
--------
发给模型的内容要**又准又省**：

* 只发冲突真正涉及的文件，不把整个模组塞进去（不然几十 MB 没法发）
* 单个文件超过阈值时，用「围绕冲突定义名」的窗口裁剪，而不是从头截断
* 明确告诉模型群星的覆盖语义，避免它写出「两个定义都保留」这种无效补丁

裁剪策略
--------
对一个脚本文件，先找到冲突定义（例如 ``my_trigger``）出现的行，
以它为中心取前后 ``window`` 行。同时保留文件头（便于模型理解上下文风格）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Iterable

from ..paths import is_file, long_path, read_text

# 单个文件最多发给模型的字符数（按字符而非字节，中文模组也适用）
MAX_CHARS_PER_FILE = 24_000
# 每个冲突最多带几个文件
MAX_FILES_PER_CONFLICT = 6
# 所有文件合计上限
MAX_TOTAL_CHARS = 120_000
# 围绕冲突行的窗口半径
WINDOW_RADIUS = 90


@dataclass
class FileExcerpt:
    """一个文件发给模型时的片段。"""

    mod_name: str
    mod_key: str
    rel: str
    text: str
    truncated: bool = False
    total_lines: int = 0
    sent_lines: int = 0
    around: str = ""

    def header(self) -> str:
        """生成发给模型时该文件的标题行（含是否被裁剪）。"""
        flag = "（已裁剪）" if self.truncated else ""
        return (f"----- 模组「{self.mod_name}」的文件 {self.rel}{flag} -----")


@dataclass
class ConflictContext:
    """一个冲突的完整上下文，用于构建提示词。"""

    kind: str = ""
    severity: str = ""
    title: str = ""
    detail: str = ""
    target: str = ""
    category: str = ""
    mods: list[str] = field(default_factory=list)
    mod_names: list[str] = field(default_factory=list)
    excerpts: list[FileExcerpt] = field(default_factory=list)
    definitions: list[dict] = field(default_factory=list)
    current_winner: str = ""
    load_order: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    # 本地化冲突专用：{模组名: {键: 译文}}
    localisation: dict[str, dict[str, str]] = field(default_factory=dict)

    def to_dict(self) -> dict:
        """序列化为界面用的字典。

        ``localisation`` 只带前 12 条，避免预览面板过大。
        """
        return {
            "kind": self.kind, "severity": self.severity, "title": self.title,
            "detail": self.detail, "target": self.target,
            "category": self.category, "mods": self.mods,
            "mod_names": self.mod_names, "current_winner": self.current_winner,
            "files": [
                {"mod": e.mod_name, "rel": e.rel,
                 "lines": e.total_lines, "sent_lines": e.sent_lines,
                 "truncated": e.truncated, "around": e.around}
                for e in self.excerpts
            ],
            "definitions": self.definitions,
            "notes": self.notes,
            "localisation_keys": len(self.localisation),
            "localisation": {
                name: dict(list(entries.items())[:12])
                for name, entries in self.localisation.items()
            },
        }


def _read_window(path: str, around: str, *, radius: int = WINDOW_RADIUS,
                 max_chars: int = MAX_CHARS_PER_FILE) -> tuple[str, bool, int, int]:
    """读取文件（必要时围绕 ``around`` 裁剪）。

    返回 ``(文本, 是否裁剪, 总行数, 实发行数)``。
    """
    text = read_text(path)
    lines = text.splitlines()
    total = len(lines)

    if len(text) <= max_chars:
        return text, False, total, total

    # 找冲突定义所在行
    hit = -1
    if around:
        needle = f"{around} "          # 避免匹配到子串
        for i, ln in enumerate(lines):
            s = ln.lstrip()
            if s.startswith(around + " ") or s.startswith(around + "=") \
                    or s.startswith(around + "\t") or needle in ln:
                hit = i
                break

    if hit < 0:
        # 找不到就取头部（含文件头，模型能理解风格）
        budget = max_chars
        out: list[str] = []
        used = 0
        for ln in lines:
            if used + len(ln) + 1 > budget:
                break
            out.append(ln)
            used += len(ln) + 1
        return "\n".join(out), True, total, len(out)

    lo = max(0, hit - radius)
    hi = min(total, hit + radius)
    out = lines[lo:hi]
    body = "\n".join(out)
    if len(body) > max_chars:
        body = body[:max_chars]
    head = (f"// 以下为第 {lo + 1}~{hi} 行"
            f"（冲突定义在第 {hit + 1} 行），共 {total} 行\n")
    return head + body, True, total, len(out)


def _loc_text_of(mod: Any, keys: list[str]) -> dict[str, str]:
    """从一个模组里取出指定本地化键的文本。

    用于本地化冲突：把双方对同一批键的译文并排摆给模型看，
    它才能真正判断该怎么取舍，而不是空谈。
    """
    out: dict[str, str] = {}
    root = mod.resolve_content_path() if mod is not None else ""
    if not root:
        return out
    from ..paths import decode_text, is_file as _is_file, long_path as _lp
    from ..scanner import _parse_localisation
    want = set(keys)
    # 只扫 localisation 目录，且限制文件数，避免大模组拖慢
    scanned = 0
    for dirpath, dirnames, filenames in os.walk(_lp(root)):
        low = dirpath.replace("\\", "/").lower()
        if "localisation" not in low:
            # 跳过明显无关的大目录
            dirnames[:] = [d for d in dirnames
                           if d.lower() not in ("gfx", "sound", "music",
                                                "fonts", "flags", "map",
                                                "interface")]
            continue
        for fn in filenames:
            if not fn.lower().endswith((".yml", ".yaml")):
                continue
            if scanned > 40:
                return out
            scanned += 1
            p = os.path.join(dirpath, fn)
            if not _is_file(p):
                continue
            try:
                with open(p, "rb") as fh:
                    entries = _parse_localisation(decode_text(fh.read()))
            except OSError:
                continue
            for k, v in entries.items():
                if k in want and k not in out:
                    out[k] = v
            if len(out) >= len(want):
                return out
    return out


def gather_conflict_context(finding: Any, library: Any, *, mods_limit: int = 6,
                            load_order: list[str] | None = None
                            ) -> ConflictContext:
    """从一条 :class:`smd.conflict.Finding` 收集上下文。

    只读取冲突涉及的文件，并按需裁剪。
    """
    ctx = ConflictContext(
        kind=getattr(finding, "kind", ""),
        severity=getattr(finding, "severity", ""),
        title=getattr(finding, "title", ""),
        detail=getattr(finding, "detail", ""),
        target=getattr(finding, "target", ""),
        category=getattr(finding, "category", ""),
        mods=list(getattr(finding, "mods", []) or []),
        mod_names=list(getattr(finding, "mod_names", []) or []),
        current_winner=getattr(finding, "winner", ""),
        load_order=list(load_order or []),
    )
    evidence = getattr(finding, "evidence", {}) or {}
    ctx.definitions = list(evidence.get("definitions") or [])

    # ---- 本地化冲突：把双方对同一批键的译文取出来对照 ----
    if ctx.kind == "L1" and library is not None:
        keys = [str(k) for k in (evidence.get("sample_keys") or [])][:24]
        if keys:
            table: dict[str, dict[str, str]] = {}
            for k in ctx.mods[:4]:
                mod = library.get(k)
                if mod is None:
                    continue
                table[getattr(mod, "name", k)] = _loc_text_of(mod, keys)
            if any(table.values()):
                ctx.localisation = table
                ctx.notes.append(
                    f"已取出 {len(keys)} 个冲突键的双方译文用于对照")

    # 需要读取的文件：(mod_key, rel, 冲突定义名)
    targets: list[tuple[str, str, str]] = []
    for d in ctx.definitions:
        if d.get("owner") and d.get("rel"):
            targets.append((str(d["owner"]), str(d["rel"]),
                            str(d.get("name") or ctx.target)))
    # 文件级冲突：目标就是那个相对路径
    if not targets and ctx.target and "/" in ctx.target:
        for key in ctx.mods:
            targets.append((key, ctx.target, ""))

    seen: set[tuple[str, str]] = set()
    budget = MAX_TOTAL_CHARS
    for key, rel, around in targets:
        if len(ctx.excerpts) >= min(mods_limit, MAX_FILES_PER_CONFLICT):
            break
        if (key, rel) in seen:
            continue
        seen.add((key, rel))
        mod = library.get(key) if library is not None else None
        root = mod.resolve_content_path() if mod is not None else ""
        if not root:
            continue
        full = os.path.join(long_path(root), rel.replace("/", os.sep))
        if not is_file(full):
            continue
        try:
            text, trunc, total, sent = _read_window(
                full, around, max_chars=min(MAX_CHARS_PER_FILE, max(2000, budget)))
        except OSError:
            continue
        budget -= len(text)
        ctx.excerpts.append(FileExcerpt(
            mod_name=(mod.name if mod is not None else key),
            mod_key=key, rel=rel, text=text, truncated=trunc,
            total_lines=total, sent_lines=sent, around=around,
        ))
        if budget <= 2000:
            break

    if not ctx.excerpts and not ctx.localisation:
        ctx.notes.append(
            "未能读取到冲突文件的原文（可能文件缺失或路径异常），"
            "补丁将仅依据文字描述与定义清单生成。")
    elif not ctx.excerpts and ctx.localisation:
        ctx.notes.append(
            "这是本地化文本冲突，已直接提供双方译文对照，"
            "无需再读取脚本文件。")
    return ctx


# ---------------------------------------------------------------------------
# 提示词
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """你是一位资深的《群星》(Stellaris) 模组兼容补丁作者。
你精通 Clausewitz 脚本语法与其覆盖（override）语义。

你必须遵守的第一原则：
**只输出能真正修复冲突的最小补丁。宁可少写，绝不编造。**

硬性规则：
1. 群星对「具名对象」采用**整体覆盖**语义，不是字段合并。
   同名对象后加载者完全替换先加载者。因此补丁里必须写出该对象的**完整定义**，
   不能只写想改的那一个字段。
2. 只允许使用输入中出现过的对象名、触发器、修正量、本地化键。
   **绝对禁止**发明新的 scripted_trigger / scripted_effect / 变量名 /
   modifier 名 / 事件 id。如果信息不足，就在说明里讲清楚缺什么，不要猜。
3. 不要输出任何解释性文字到代码块里。代码块内必须是纯净的脚本。
4. 文件路径必须落在这些目录之一（游戏会读取它们）：
   common/scripted_triggers/、common/scripted_effects/、
   common/scripted_variables/、common/static_modifiers/、
   common/component_templates/、common/section_templates/、
   common/technology/、common/buildings/、common/pop_jobs/、
   common/traits/、common/megastructures/、common/edicts/、
   common/policies/、events/、interface/
5. 补丁不会被自动信任。它会经过语法解析与引用校验，请确保能通过。

输出格式（严格遵守）：

### 说明
（中文，说明这个冲突的本质、你选择了哪种解法、以及为什么。
  并指出你无法确定、需要玩家自行判断的地方。）

### 补丁文件
```path: 相对路径/文件名.txt
（完整的文件内容）
```
（可以有多个代码块）

### 风险评估
- 置信度：高 / 中 / 低
- 影响面：（这个补丁会不会改变游戏平衡？会影响哪些模组？）
- 回退方式：（出问题怎么办）
"""


def _fmt_localisation(ctx: ConflictContext) -> str:
    """把双方译文并排渲染，便于模型对比取舍。"""
    if not ctx.localisation:
        return ""
    names = list(ctx.localisation.keys())
    keys: list[str] = []
    for entries in ctx.localisation.values():
        for k in entries:
            if k not in keys:
                keys.append(k)
    if not keys:
        return ""
    lines = ["", "## 本地化译文对照（同一个键，各模组分别写成了什么）", ""]
    lines.append("| 键 | " + " | ".join(names) + " |")
    lines.append("| --- | " + " | ".join("---" for _ in names) + " |")
    for k in keys[:20]:
        cells = []
        for n in names:
            v = (ctx.localisation.get(n) or {}).get(k, "（该模组没有这个键）")
            v = v.replace("|", "\\|")[:80]
            cells.append(v)
        lines.append(f"| `{k}` | " + " | ".join(cells) + " |")
    lines.append("")
    lines.append("注意：这些键是**同一个游戏文本**的不同译法，"
                 "不是可以叠加的内容。你需要判断该保留哪一种，"
                 "或给出统一后的译文。")
    return "\n".join(lines)


def _fmt_excerpts(ctx: ConflictContext) -> str:
    if not ctx.excerpts:
        return "（没有可用的文件原文）"
    parts: list[str] = []
    for e in ctx.excerpts:
        parts.append(e.header())
        parts.append(e.text)
        parts.append("")
    return "\n".join(parts)


def _fmt_definitions(ctx: ConflictContext) -> str:
    if not ctx.definitions:
        return "（无结构化定义信息）"
    lines = []
    for d in ctx.definitions:
        lines.append(f"- 模组「{d.get('name', '')}」"
                     f"（key={d.get('owner', '')}）"
                     f"在 {d.get('rel', '')} 第 {d.get('line', '?')} 行"
                     f"定义了 {ctx.target or d.get('name', '')}")
    return "\n".join(lines)


def build_patch_prompt(ctx: ConflictContext, *,
                       user_hint: str = "",
                       goal: str = "merge") -> list[Any]:
    """构建发给模型的完整消息列表。

    ``goal``:

    * ``merge``   —— 尽量让两个模组的功能**都保留**
    * ``prefer``  —— 保留某一个模组（在 ``user_hint`` 里说明是谁）
    * ``fix``     —— 只修掉语法/引用错误，不改变设计意图
    """
    from .client import ChatMessage

    goal_text = {
        "merge": "目标是**让双方的功能都尽量保留**。"
                 "例如把两个模组的触发条件用 OR 组合、把各自的字段合并到一份定义里。",
        "prefer": "目标是**保留其中一个模组的效果**，"
                  "按玩家在下面说明的偏好来写。",
        "fix": "目标是**只修复语法错误或失效引用**，"
               "不要改变原作者的设计意图与平衡性。",
    }.get(goal, "让双方功能都尽量保留。")

    user = f"""# 待解决的模组冲突

- 冲突类型：{ctx.kind}（{ctx.severity}）
- 标题：{ctx.title}
- 涉及的游戏系统：{ctx.category or '未标注'}
- 冲突对象：`{ctx.target}`
- 当前加载顺序下实际生效者：{ctx.current_winner or '未知'}

## 冲突详情
{ctx.detail or '（无补充说明）'}

## 涉及的定义位置
{_fmt_definitions(ctx)}

## 相关文件原文
{_fmt_excerpts(ctx)}
{_fmt_localisation(ctx)}

## 玩家补充说明
{user_hint.strip() or '（无）'}

# 你的任务
{goal_text}

请输出一份**兼容补丁模组**的内容。补丁会作为独立模组排在加载顺序最后，
用来覆盖上面这些互相冲突的定义。

注意：
- 如果这个冲突**无法安全合并**（例如两个模组对同一机制有互斥的设计），
  请直接说明「不建议自动合并」，并给出「保留哪一个 + 理由」的建议，
  不要强行捏造一个可能破坏存档的补丁。
- 如果信息不足以写出正确补丁，请明确说出缺少什么。这比写错更有价值。
"""
    messages = [ChatMessage("system", SYSTEM_PROMPT),
                ChatMessage("user", user)]

    # 总长度保护：超出就通知调用方（由调用方决定是否进一步裁剪）
    total = len(SYSTEM_PROMPT) + len(user)
    ctx.notes.append(f"提示词长度约 {total} 字符")
    return messages


def estimate_tokens(messages: Iterable[Any]) -> int:
    """粗略估算 token 数（中文按 1 字≈1 token，英文按 4 字符≈1 token）。"""
    total = 0
    for m in messages:
        text = getattr(m, "content", "") or ""
        cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
        other = len(text) - cjk
        total += cjk + max(1, other // 4)
    return total
