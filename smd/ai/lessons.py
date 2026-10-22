"""AI 补丁的**自我学习 / 记错迭代**。

## 要解决的问题

``generate_patch_with_ai`` 的旧流程是**一次性的**：

    构建提示词 → 调 AI → 解析 → 校验 → 返回

校验没过就结束，那个错误**从来没告诉过 AI**。于是同一个错它每次都犯 ——
用户看到的就是「AI 生成补丁」反复产出同样的坏结果，还得自己改。

## 做法

两件事，都靠**校验器的错误信息**驱动：

### ① 自我修正循环（当次）

校验失败时，把 ``PatchIssue`` 结构化地喂回去，让模型自己改::

    你上一版补丁有 2 个阻塞问题，请只修正这些，其余保持不动：
    · [error] 引用了不存在的定义 acot_fe_juggernaut_se —— 在 acot_ships.txt
      提示：ACOT 里没有这个定义，请改用游戏里实际存在的
    · [error] 缺少右花括号 —— 在 zzz_SMD_patch.txt 第 42 行

**必须给具体的 where 和 hint** —— 只说「校验失败」模型只会换个姿势再错一遍。

### ② 跨次记错（长期）

把每次的阻塞问题**归类**（去掉文件名、变量名这类具体信息，只留模式），
统计频次存到 ``<data_dir>/ai_patch_lessons.json``。
下次生成时，把高频前几条作为「已知易错点」注入提示词。

归类是关键：``引用了不存在的定义 acot_xxx`` 和 ``引用了不存在的定义 foo_bar``
是同一个毛病，不归并的话每条都只出现一次，学不到东西。

## 边界

· 只记**阻塞级**（fatal/error）。warn 太多太杂，记了会污染。
· 同类错误出现 **≥2 次**才注入提示词 —— 只犯过一次的可能是偶发。
· 记的是**模式**不是原文 —— 原文含用户 mod 名，不适合塞回提示词。
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

FILE_NAME = "ai_patch_lessons.json"

# 一条错误最多保留多久（秒）。太老的教训可能已经不适用（游戏更新了）
MAX_AGE = 90 * 24 * 3600
# 注入提示词的条数上限。太多会把模型的注意力从当前任务上拽走。
MAX_INJECT = 6
# 至少出现过几次才算「教训」
MIN_HITS = 2


@dataclass
class Lesson:
    """一类错误的统计。"""

    pattern: str          # 归类后的模式（不含具体名字）
    sample: str = ""      # 一条原始消息（供人看，不塞提示词）
    hint: str = ""        # 校验器给的提示
    count: int = 0
    first_seen: float = 0.0
    last_seen: float = 0.0


def _path(data_dir: Path) -> Path:
    return Path(data_dir) / FILE_NAME


def load(data_dir: Path) -> dict[str, Lesson]:
    """读历史教训。坏了就当空的，绝不因为这个问题挡住主流程。"""
    try:
        raw = json.loads(_path(data_dir).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    out: dict[str, Lesson] = {}
    now = time.time()
    for key, ent in (raw.get("lessons") or {}).items():
        if not isinstance(ent, dict):
            continue
        if now - float(ent.get("last_seen") or 0) > MAX_AGE:
            continue
        out[key] = Lesson(
            pattern=str(ent.get("pattern") or key),
            sample=str(ent.get("sample") or ""),
            hint=str(ent.get("hint") or ""),
            count=int(ent.get("count") or 0),
            first_seen=float(ent.get("first_seen") or 0),
            last_seen=float(ent.get("last_seen") or 0),
        )
    return out


def save(data_dir: Path, lessons: dict[str, Lesson]) -> None:
    try:
        p = _path(data_dir)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({
            "version": 1,
            "lessons": {
                k: {"pattern": v.pattern, "sample": v.sample[:300],
                    "hint": v.hint[:200], "count": v.count,
                    "first_seen": v.first_seen, "last_seen": v.last_seen}
                for k, v in lessons.items()
            },
        }, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass


# --------------------------------------------------------------------------
# 归类：把「具体的一条错误」压成「一类错误」
# --------------------------------------------------------------------------

# 顺序有讲究：越具体的越先匹配，否则会被通用规则抢走
_RULES: tuple[tuple[str, str], ...] = (
    (r"不存在|找不到|未找到|missing|not found|unknown",
     "引用了不存在的对象"),
    (r"花括号|括号|brace|bracket|unbalanced",
     "括号不配平"),
    (r"引号|quote|unterminated",
     "引号未闭合"),
    (r"编码|encoding|utf-?8|bom",
     "文件编码有问题"),
    (r"本地化|localisation|localization|yml",
     "本地化格式问题"),
    (r"越权|不属于|未涉及|超出|outside",
     "改了冲突范围外的对象"),
    (r"重复|duplicate|already defined",
     "重复定义"),
    (r"空|empty|没有任何内容",
     "生成内容为空"),
)


def classify(message: str) -> str:
    """把一条错误消息归成一类。

    认不出来就退化成「首 30 字」——通用规则抢不到时至少还能聚一聚。
    """
    low = (message or "").lower()
    for pat, label in _RULES:
        if re.search(pat, low):
            return label
    # 兜底：去掉具体名字（英文标识符、数字、路径）后取前 30 字
    s = re.sub(r"[A-Za-z_][A-Za-z0-9_.\-]{3,}", "X", message or "")
    s = re.sub(r"\d+", "N", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:30] or "未分类"


def record(issues: list, data_dir: Path) -> dict[str, Lesson]:
    """把一次校验的阻塞问题记进去，返回更新后的全量教训。"""
    lessons = load(data_dir)
    now = time.time()
    for it in issues:
        level = getattr(it, "level", "")
        if level not in ("fatal", "error"):
            continue
        key = classify(getattr(it, "message", ""))
        ent = lessons.get(key)
        if ent is None:
            ent = Lesson(pattern=key, sample=getattr(it, "message", ""),
                         hint=getattr(it, "hint", ""),
                         first_seen=now, last_seen=now)
        ent.count += 1
        ent.last_seen = now
        if getattr(it, "hint", "") and not ent.hint:
            ent.hint = it.hint
        lessons[key] = ent
    save(data_dir, lessons)
    return lessons


def lesson_prompt(lessons: dict[str, Lesson]) -> str:
    """把高频教训拼成一段注入提示词的文字。没有够格的教训就返回空串。"""
    hot = [l for l in lessons.values() if l.count >= MIN_HITS]
    if not hot:
        return ""
    hot.sort(key=lambda l: -l.count)
    lines = [
        "",
        "## 你以前在这些地方栽过（同类错误反复出现，这次请避开）",
        "",
    ]
    for l in hot[:MAX_INJECT]:
        tip = f"（{l.hint}）" if l.hint else ""
        lines.append(f"· 出现过 {l.count} 次：**{l.pattern}**{tip}")
    lines.append("")
    return "\n".join(lines)


def feedback_prompt(issues: list) -> str:
    """把这次的校验失败拼成「让模型自己改」的指令。

    **必须带 where 和 hint**。只说「校验失败」模型只会换个姿势再错一遍，
    实测看到的就是同一类错误换个变量名重来。
    """
    blocking = [i for i in issues if getattr(i, "is_blocking", False)]
    if not blocking:
        return ""
    lines = [
        f"你上一版补丁有 {len(blocking)} 个**阻塞**问题，请修正后重新输出完整补丁。",
        "",
        "只改这些问题指出的地方，其余内容保持不动。",
        "",
        "问题清单：",
    ]
    for n, it in enumerate(blocking[:12], 1):
        where = f" —— 在 {it.where}" if getattr(it, "where", "") else ""
        hint = f"\n     提示：{it.hint}" if getattr(it, "hint", "") else ""
        lines.append(f"{n}. [{it.level}] {it.message}{where}{hint}")
    lines += [
        "",
        "注意：",
        "· 修不了的就**去掉**，不要为了凑数编造定义名 —— 编出来的名字一定不存在，",
        "  这正是最常见的那类错误。",
        "· 输出格式保持不变（文件名 + 完整内容）。",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 供界面展示
# --------------------------------------------------------------------------

def summary(lessons: dict[str, Lesson]) -> list[dict]:
    """按频次排好的列表，给界面用。"""
    items = sorted(lessons.values(), key=lambda l: -l.count)
    return [{
        "pattern": l.pattern,
        "count": l.count,
        "hint": l.hint,
        "sample": l.sample,
        "days_ago": round((time.time() - l.last_seen) / 86400, 1),
    } for l in items]


def clear(data_dir: Path) -> None:
    """清空教训（用户可能想重置）。"""
    try:
        _path(data_dir).unlink()
    except OSError:
        pass
