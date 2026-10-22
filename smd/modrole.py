"""模组定位分析 —— 判断一个 mod 在加载顺序里该站在哪一层。

## 为什么单独一个模块

旧版 ``solver.classify_tier()`` 是一串 ``if/elif``，**先命中先算**：

    if "framework" in 名字:      return 前置层      # 命中了
    if "ui" in 名字:             return UI 层       # 永远轮不到

于是「UI Overhaul Dynamic」名字里有 ``framework`` 之外的词还不要紧，
但「! Core Framework of Kasako」既叫 framework、又是个 UI 框架 ——
一条规则定生死，另一条永远没机会说话。用户实报它被排在第 0 位、
而作者要求排在两个 UI mod 之后。

## 这里的做法

**多信号加权，每个信号都留证据，输出置信度。**

信号优先级（实测下来最能说明问题的排前面）：

| 信号 | 权重 | 为什么可信 |
| --- | --- | --- |
| 目录结构 | 1.5 | 唯一骗不了人的 —— 只有 ``localisation/`` 就一定是汉化 |
| 名称关键词 | 1.0 | 作者自己的命名习惯 |
| descriptor 的 tags | 0.8 | 作者自己填的分类 |
| 依赖关系 | 1.2 | 依赖别人的不可能是最底层；被一堆人依赖的多半是底座 |

**没有一个信号能单独定案。** 目录结构够强就先信它，但它和 tags 打架时
置信度会掉下来 —— 掉下来的那些正是该让人看一眼的。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# 分层（和 solver 的常量保持一致；这里不 import 以免循环依赖）
TIER_BASE = 10
TIER_OVERHAUL = 20
TIER_CONTENT = 30
TIER_UI = 40
TIER_PATCH = 50
TIER_MINOR = 60
TIER_TRANSLATION = 70
TIER_OVERRIDE = 80

TIER_LABEL = {
    TIER_BASE: "前置框架 / 基础库",
    TIER_OVERHAUL: "大型机制重做",
    TIER_CONTENT: "常规内容",
    TIER_UI: "界面 UI",
    TIER_PATCH: "兼容补丁 / Bug 修复",
    TIER_MINOR: "小型调整",
    TIER_TRANSLATION: "本地化 / 汉化",
    TIER_OVERRIDE: "最终覆盖层",
}

# 名称里出现这些 → 倾向对应层
# 注意 ``core framework`` 的权重**故意低**：自称 framework 的 mod 很多，
# 真正必须垫在加载顺序最底下的只有 URP / shared library 那一类。
# 「! Core Framework of Kasako」自称 framework，但它的工坊描述明确写着
# 「必须在 UI Overhaul Dynamic 后面」—— 它是功能框架，不是加载底座。
_NAME_HINTS: tuple[tuple[tuple[str, ...], int, float, str], ...] = (
    (("translation", "localisation", "localization", "汉化", "本地化", "中文补丁"), TIER_TRANSLATION, 1.0, "名称含本地化词"),
    (("override", "覆盖层"), TIER_OVERRIDE, 1.0, "名称含覆盖层词"),
    (("hotfix", " bug fix", "bugfix", "patcher", "compatibility patch", "兼容补丁"), TIER_PATCH, 1.0, "名称含补丁/修复词"),
    (("universal resource patch", "shared library", "common library",
      "基础库", "前置框架", "base mod"), TIER_BASE, 1.4, "名称含加载底座词"),
    (("core framework", "framework"), TIER_CONTENT, 0.6, "名称自称 framework（未必是加载底座）"),
    (("ui overhaul", "dynamic ui", "interface mod", "uitools", "界面"), TIER_UI, 1.0, "名称含界面词"),
    (("voice", "语音", "顾问", "advisor"), TIER_MINOR, 0.7, "名称含语音/顾问词（纯资源，顺序无关）"),
    (("music", "音乐", "电台", "radio"), TIER_MINOR, 0.7, "名称含音乐/电台词（纯资源，顺序无关）"),
)

_TAG_HINTS: tuple[tuple[tuple[str, ...], int, str], ...] = (
    (("localisation", "translation"), TIER_TRANSLATION, "tag 标明本地化"),
    (("fixes", "bug fix"), TIER_PATCH, "tag 标明修复"),
    (("graphics", "ui"), TIER_UI, "tag 标明界面/图形"),
    (("overhaul", "total conversion", "megastructures"), TIER_OVERHAUL, "tag 标明大型重做"),
    (("utilities",), TIER_MINOR, "tag 仅标 Utilities（工具类）"),
)

# 纯资源目录 —— 语音/音乐 mod 常见，它们对加载顺序没有要求
_RESOURCE_ONLY_DIRS = {"sound", "music", "fonts", "flags", "thumbnail",
                       "descriptor", "localisation", "localization"}

# **决定性名称**：这些是社区里约定俗成的专有名词，命中就不该再被别的信号拉低。
# ``!!!Universal Resource Patch`` 只有 interface/（像 UI mod）、tag 还是 Fixes（像补丁），
# 但它就是底座 —— 三个信号一投票，置信度被稀释到 0.45，界面会把它标成"低置信"，
# 而它恰恰是最不该被怀疑的那个。
_DECISIVE_NAMES: tuple[tuple[tuple[str, ...], int, str], ...] = (
    (("universal resource patch",), TIER_BASE, "Universal Resource Patch 是社区公认的加载底座"),
    (("shared library", "common library"), TIER_BASE, "共享库类模组，必须最先加载"),
)

# 目录结构 → 层。顺序是从「最窄」到「最宽」，先匹配到的更可信。
_STRUCTURE_RULES: tuple[tuple[tuple[str, ...], int, str], ...] = (
    (("localisation", "localization"), TIER_TRANSLATION, "只带本地化目录"),
    (("interface",), TIER_UI, "只带界面目录"),
)


@dataclass
class Signal:
    """一条判定依据。"""

    source: str      # "目录" / "名称" / "tag" / "依赖"
    tier: int
    weight: float
    evidence: str

    def to_dict(self) -> dict:
        return {"source": self.source, "tier": self.tier,
                "weight": round(self.weight, 2), "evidence": self.evidence}


@dataclass
class ModRole:
    tier: int = TIER_CONTENT
    label: str = ""
    confidence: float = 0.35         # 默认低置信：什么都没读到
    signals: list[Signal] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "tier": self.tier,
            "label": self.label,
            "confidence": round(self.confidence, 2),
            "signals": [s.to_dict() for s in self.signals],
            "notes": self.notes,
        }


def _top_dirs(path: str) -> set[str]:
    """mod 内容目录的顶层子目录名（小写）。读不到返回空集。"""
    if not path:
        return set()
    try:
        with os.scandir(path) as it:
            return {e.name.lower() for e in it
                    if e.is_dir(follow_symlinks=False)}
    except OSError:
        return set()


def _structure_signal(dirs: set[str]) -> Signal | None:
    """按目录结构判层。

    关键洞察：**只有 localisation/** 的模组不可能是本体 —— 它没有内容可定义。
    这一条比任何名字都可靠。

    覆盖顺序按「窄 → 宽」：越窄的目录组合越说明问题，先判掉。
    """
    if not dirs:
        return None

    # ① 只有本地化（可能还带 gfx/interface 的小图标，那也算汉化补丁的一部分）
    content_dirs = dirs - {"localisation", "localization", "gfx", "interface",
                           "fonts", "flags", "thumbnail", "descriptor"}
    if dirs & {"localisation", "localization"} and not content_dirs:
        return Signal("目录", TIER_TRANSLATION, 1.5,
                      f"顶层只有本地化相关目录：{sorted(dirs)[:4]}")

    has_content = bool(dirs & {"common", "events", "common_scripted"})
    has_models = "gfx" in dirs
    has_ui = "interface" in dirs

    # ② 只有界面（没有 common/events 那种内容骨架）
    #    权重**故意压到 0.9**：很多补丁/底座模组也只带一个 interface/ 目录。
    #    「!!!Universal Resource Patch」就是这个形状 —— 只有 interface/，
    #    但它是全社区公认的加载底座。给它 1.3 会把 BASE 挤掉（实测 0.4 置信）。
    if has_ui and not has_content:
        return Signal("目录", TIER_UI, 0.9,
                      f"有 interface/ 但没有内容目录：{sorted(dirs)[:4]}")

    # ③ 有模型 + 内容骨架 —— 大型模组（改模型是重活）
    if has_models and has_content:
        return Signal("目录", TIER_OVERHAUL, 1.1,
                      "同时带 gfx/ 与内容目录，通常是有模型的大型模组")

    # ④ 有内容骨架但没模型 —— 常规内容。这条覆盖了绝大多数 mod，
    #    早先漏了它，于是「common+events+localisation」这种最常见的结构
    #    一条规则都不命中，全部退回 0.35 的默认值（实测 96 个里至少 18 个）。
    if has_content:
        # 只有 common/ 没有 events/ —— 多半是数值/定义类的小调整
        if not (dirs & {"events"}) and dirs <= {"common", "localisation",
                                                "localization", "flags",
                                                "thumbnail", "descriptor"}:
            return Signal("目录", TIER_MINOR, 0.8,
                          f"只有 common/ 与本地化，像小型调整：{sorted(dirs)[:4]}")
        return Signal("目录", TIER_CONTENT, 1.0,
                      f"带内容目录（{sorted(dirs & {'common','events'})}），常规模组")

    # ⑤ 纯资源目录（语音包 / 音乐包 / 字体）—— 顺序无所谓。
    #    这些 mod 里没有 common/events，只有 sound/ 或 fonts/，
    #    早先全部落到默认 0.35。
    if dirs and dirs <= _RESOURCE_ONLY_DIRS | {"gfx", "interface"}:
        return Signal("目录", TIER_MINOR, 1.0,
                      f"只有纯资源目录（{sorted(dirs)[:4]}），加载顺序无关")

    return None


def _name_signal(name: str) -> Signal | None:
    n = name.lower()
    for words, tier, weight, why in _NAME_HINTS:
        for w in words:
            if w in n:
                return Signal("名称", tier, weight, f"{why}（命中「{w}」）")
    return None


def _tag_signal(tags: list[str]) -> Signal | None:
    lowered = {str(t).lower() for t in tags}
    for words, tier, why in _TAG_HINTS:
        if lowered & set(words):
            return Signal("tag", tier, 0.8, why)
    return None


def analyze(mod, *, dependents: int = 0,
            dependencies: int = 0) -> ModRole:
    """分析一个模组的定位。

    ``dependents``  —— 有多少个已启用的 mod 声明依赖它
    ``dependencies`` —— 它自己声明了多少依赖
    这两个由调用方从整份 mod 库统计后传进来（单个 mod 看不到全局）。
    """
    role = ModRole()
    dirs = _top_dirs(getattr(mod, "path", "") or "")
    name = getattr(mod, "name", "") or ""

    # ⓪ 决定性名称 —— 命中即定案，不再投票。
    #    这是唯一允许「一个信号定生死」的例外，因为它们是专有名词，
    #    不像 "framework" 那样谁都能往自己名字里塞。
    nlow = name.lower()
    for words, tier, why in _DECISIVE_NAMES:
        if any(w in nlow for w in words):
            role.tier = tier
            role.label = TIER_LABEL[tier]
            role.signals = [Signal("名称", tier, 2.0, why)]
            role.confidence = 0.9
            role.notes.append("名称命中社区约定俗成的专有名词，直接定案")
            return role

    sig: list[Signal] = []
    s = _structure_signal(dirs)
    if s:
        sig.append(s)
    s = _name_signal(name)
    if s:
        sig.append(s)
    s = _tag_signal(list(getattr(mod, "tags", []) or []))
    if s:
        sig.append(s)

    # 依赖关系：**依赖别人的不可能是最底层**
    if dependencies > 0:
        sig.append(Signal("依赖", TIER_CONTENT, 1.2,
                          f"自身声明了 {dependencies} 个依赖，不可能是前置底座"))
    if dependents >= 3:
        sig.append(Signal("依赖", TIER_BASE, 1.2,
                          f"被 {dependents} 个模组依赖，按底座处理"))

    if not sig:
        role.notes.append("没有拿到任何信号，按常规内容层处理（置信度低）")
        role.tier = TIER_CONTENT
        role.label = TIER_LABEL[TIER_CONTENT]
        role.confidence = 0.35
        return role

    # 加权投票：同层累加权重，取最高
    score: dict[int, float] = {}
    for x in sig:
        score[x.tier] = score.get(x.tier, 0.0) + x.weight
    best = max(score, key=lambda t: score[t])
    total = sum(score.values())

    role.tier = best
    role.label = TIER_LABEL[best]
    role.signals = sig
    # 置信度 = 该层得票 / 总票。信号全一致时接近 1.0，互相打架时掉下来。
    role.confidence = round(score[best] / total, 2) if total else 0.35

    conflict = [x for x in sig if x.tier != best]
    if conflict:
        role.notes.append(
            "信号不一致：" + "；".join(f"{x.source}说要放{TIER_LABEL[x.tier]}"
                                      for x in conflict))
    return role
