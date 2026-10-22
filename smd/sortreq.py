"""模组排序要求解析。

## 为什么需要这个模块

群星的加载顺序里有一条「作者说了算」的规则，但它**只写在工坊页面的描述里**，
不在 ``descriptor.mod`` 里（那个文件只有 name/version/tags）。

真实样本（``!伞の框架!``，工坊 2466607238）::

    排序
    必须在【UI Overhaul Dynamic】后面
    必须在【暗蓝科技UI】后面
    其他mod无排序需求

旧版 solver 用「名字里有没有"前置"」这类启发式定层，把它排到第 0 位，
而作者明确要求排在那两个 UI mod **之后** —— 用户实报「把它放最上面没绷住」。

## 这个模块只做一件事

从**描述全文**里提取「谁必须在谁后面/前面」，返回一堆约束边。
**不做排序**（那是 solver 的事），**也不猜**（拿不准就不出约束）。

## 防误判（这是本模块最关键的部分）

描述里 95% 的篇幅是功能介绍，里面会出现大量 mod 名字
（「设置面板支持 --- [Gigastructural Engineering]」这种）。**那些不是排序要求。**

三道闸：
1. **只认「排序」小节**。先定位小标题（``排序`` / ``Load Order`` / ``排序要求``），
   只解析它到下一个空行/下一个小标题之间的内容。整篇乱扫必然误判。
2. **句子里必须有排序词**（``后面`` / ``前面`` / ``after`` / ``before`` / ``above`` / ``below``）。
   光出现 mod 名不算。
3. **否定词直接丢弃**（``无排序需求`` / ``无需排序`` / ``无所谓顺序`` / ``no load order``）。

提取出来的每条都**带上原文行**，界面上可以让用户核对 —— 宁可让他一眼看穿，
也不要静默算错。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


# --------------------------------------------------------------------------
# 小标题的写法
# --------------------------------------------------------------------------
# 「排序」这一节可能叫这些名字。用**整行**匹配（允许前后空白和装饰符号），
# 不做子串搜索 —— 「排序」两个字出现在正文句子里太常见了。
_SECTION_HEADS = (
    "排序", "加载顺序", "排序要求", "载入顺序", "加载排序",
    "load order", "loadorder", "sorting", "order of loading",
)

# 下一节的开始：另一个小标题（用「行很短且不含句号」粗判），或者空行。
_SECTION_END_MARKERS = (
    "modmenu", "mod menu", "设置面板支持", "独立功能", "功能重叠",
    "功能集成", "credits", "预览视频", "介绍", "features", "known issues",
)

# 排序词
_AFTER_WORDS = (
    r"后面", r"之后", r"下面", r"下方", r"后",
    r"after", r"below", r"under", r"lower than",
)
_BEFORE_WORDS = (
    r"前面", r"之前", r"上面", r"上方", r"前",
    r"before", r"above", r"over", r"higher than",
)

# 否定：出现这些就当「这条不是排序要求」
_NEGATIVE = (
    r"无排序需求", r"无需排序", r"不需要排序", r"无顺序要求", r"无所谓",
    r"不要求", r"任意顺序", r"no load order", r"no sorting", r"doesn'?t matter",
)

# mod 名可能被这些括号包起来
_BRACKETS = ("【", "】", "[", "]", "「", "」", "『", "』", "《", "》", "(", ")", "（）")


def _norm(name: str) -> str:
    """归一化 mod 名用于比较：去装饰前缀、去空白、小写。"""
    s = name.strip().lstrip("!！").strip()
    for ch in _BRACKETS:
        s = s.replace(ch, "")
    s = re.sub(r"\s+", "", s)
    return s.lower()


@dataclass
class SortConstraint:
    """一条排序要求。"""

    kind: str           # "after" = 我必须排在 target 后面
    target: str         # 参照物（作者写的原始名字）
    target_norm: str
    evidence: str       # 原文那一行 —— 让用户能核对，也方便我们事后 debug
    confidence: float   # 0.0~1.0。人工确认过的可以抬到 1.0

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "target": self.target,
            "evidence": self.evidence,
            "confidence": round(self.confidence, 2),
        }


@dataclass
class ParseResult:
    constraints: list[SortConstraint] = field(default_factory=list)
    section: str = ""            # 命中的「排序」小节原文（空 = 没找到这一节）
    rejected: list[str] = field(default_factory=list)   # 被否定的行

    @property
    def found_section(self) -> bool:
        return bool(self.section)


def _find_section(text: str) -> str:
    """定位「排序」小节，返回它的正文（不含标题行）。找不到返回空串。"""
    lines = text.splitlines()
    lowered = [ln.strip().lower() for ln in lines]

    start = -1
    for i, ln in enumerate(lowered):
        # 整行就是标题（去掉装饰符号后）
        bare = re.sub(r"^[#=\-\*\s~]+|[#=\-\*\s~：:]+$", "", ln).strip()
        if bare in _SECTION_HEADS or any(bare == h for h in _SECTION_HEADS):
            start = i + 1
            break
    if start < 0:
        return ""

    out: list[str] = []
    blank_streak = 0
    for ln in lines[start:]:
        stripped = ln.strip()

        if not stripped:
            # **空行不立刻收工**。Steam 的 `[*]` 列表去掉标签后每条之间都是
            # 空行，早先「遇到空行就 break」，结果只抓到第一条
            # （实测：伞框架的两条要求只出来 UI Overhaul，Dark Blue UI 丢了）。
            # 现在允许连续空行，靠「下一个小标题」判断边界。
            blank_streak += 1
            if blank_streak >= 2 and out:
                # 连续两个空行 = 段落结束
                break
            continue
        blank_streak = 0

        low = stripped.lower()
        if any(m in low for m in _SECTION_END_MARKERS):
            break
        # 另一个标题：短、不含标点、**且不含排序词**。排序语句本身常常
        # 也只有十来个字（「必须放在【B】前面」），不能只按长度判。
        has_order_word = any(re.search(w, low)
                             for w in _AFTER_WORDS + _BEFORE_WORDS)
        if (not has_order_word and len(stripped) <= 24
                and not re.search(r"[，。,.；;]", stripped) and out):
            break
        out.append(stripped)
    return "\n".join(out)


def _extract_name(line: str, kind: str) -> str:
    """从一行排序语句里抠出参照物的名字。

    **中英文语序相反**，必须分开处理::

        中文：必须在【X】后面      → X 在排序词**前面**
        英文：must be AFTER X      → X 在排序词**后面**

    早先只按中文语序取，英文样本被抠成 "Must be loaded" —— 括号里那道
    单字符限制又让【B】这种短名字整条丢掉（用户实报的那两个 mod 名不短，
    但短名字在中文 mod 圈并不少见）。
    """
    # ① 括号写法，最可靠；前后都找（中文在前、英文在后都可能有括号）
    m = re.search(r"[【\[「『《(（]([^】\]」』》()）]{1,80})[】\]」』》()）]", line)
    if m:
        return m.group(1).strip()

    # ② 中文语序：排序词**前面**那段
    if kind == "after" or kind == "before":
        zh = re.search(
            r"(?:必须|请|需要|要|应)?\s*(?:放|排|置)?\s*(?:在|于)?\s*(.+?)\s*"
            r"(?:后面|之后|下面|下方|前面|之前|上面|上方)",
            line)
        if zh:
            cand = zh.group(1).strip(" ：:，,、-—")
            cand = re.sub(r"^(必须|请|需要|要|应|放在|排在|置于|放|排)+", "", cand).strip()
            if 1 <= len(cand) <= 80:
                return cand

    # ③ 英文语序：排序词**后面**那段
    en = re.search(
        r"(?:after|below|under|lower than|before|above|over|higher than)\s+(.+)",
        line, re.IGNORECASE)
    if en:
        cand = en.group(1).strip()
        # 掐掉尾部的解释（"... 后面" / "(optional)" 之类）
        cand = re.split(r"\s*[（(]", cand)[0].strip(" ：:，,、-—[]")
        if 1 <= len(cand) <= 80:
            return cand
    return ""


def parse(text: str, *, own_name: str = "") -> ParseResult:
    """解析描述全文，返回排序约束。

    ``own_name`` 只用于日志/调试，不参与判断。
    """
    res = ParseResult()
    section = _find_section(text)
    res.section = section
    if not section:
        return res

    for raw in section.splitlines():
        line = raw.strip()
        if not line:
            continue
        low = line.lower()

        # 闸 3：否定直接丢弃
        if any(re.search(p, low) for p in _NEGATIVE):
            res.rejected.append(line)
            continue

        kind = ""
        if any(re.search(w, low) for w in _AFTER_WORDS):
            kind = "after"
        elif any(re.search(w, low) for w in _BEFORE_WORDS):
            kind = "before"
        else:
            # 闸 2：没有排序词 —— 不是排序要求（「其他mod无排序需求」也会落到这里）
            res.rejected.append(line)
            continue

        name = _extract_name(line, kind)
        if not name:
            res.rejected.append(line)
            continue

        # 自己写自己没意义
        if own_name and _norm(name) == _norm(own_name):
            res.rejected.append(line)
            continue

        res.constraints.append(SortConstraint(
            kind=kind,
            target=name,
            target_norm=_norm(name),
            evidence=line,
            # 在「排序」小节里、句子结构完整 —— 但仍是机器读的，留 0.8
            confidence=0.8,
        ))

    return res


# --------------------------------------------------------------------------
# 把约束套到实际的 mod 列表上
# --------------------------------------------------------------------------

def match_targets(constraint: SortConstraint, names: list[str]) -> list[str]:
    """在候选 mod 名里找出约束指向的**实际** mod。

    三轮，从严到松：

    1. 归一化后**相等** —— 最可信
    2. 归一化后**互相包含** —— 处理「UI Overhaul Dynamic」对
       「UI Overhaul Dynamic + 汉化」这种情况
    3. **词集重合** —— 处理词序被打乱的情况。

    第三轮是实测逼出来的：``! Universal Game Rules Patch`` 的描述里写
    「Must be after 【Kasako's Core Framework】」，而工坊标题是
    ``! Core Framework of Kasako`` —— 词完全相同、顺序相反，
    前两轮一条都不中，约束静默丢失。
    """
    t = constraint.target_norm
    if not t:
        return []
    exact = [n for n in names if _norm(n) == t]
    if exact:
        return exact
    loose = [n for n in names if t in _norm(n) or _norm(n) in t]
    if loose:
        return loose

    # 第三轮：按词集比。只对**足够长**的名字用，避免 "UI" 这种短词乱撞。
    t_words = _words(constraint.target)
    if len(t_words) < 2:
        return []
    best: list[str] = []
    best_score = 0.0
    for n in names:
        w = _words(n)
        if len(w) < 2:
            continue
        inter = t_words & w
        if not inter:
            continue
        # Jaccard：两边词集的交并比
        score = len(inter) / len(t_words | w)
        if score > best_score:
            best_score, best = score, [n]
        elif score == best_score and score > 0:
            best.append(n)
    # 阈值 0.6：词要大部分重合才算同一个 mod。
    # 低了这个会张冠李戴（"UI Overhaul Dynamic" 与 "UI Overhaul Dynamic +"
    # 之外的第三个名字也可能撞进来），高了那对词序反了的就又漏了。
    return best if best_score >= 0.6 else []


def _words(name: str) -> set[str]:
    """把名字切成词集（中文按字符连写、英文按空格与符号切）。"""
    s = name.strip().lstrip("!！")
    for ch in _BRACKETS:
        s = s.replace(ch, " ")
    s = s.lower()
    # 英文词 + 连续的中文串各算一个词
    parts = re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]+", s)
    # 去掉无信息量的填充词
    stop = {"the", "of", "a", "an", "and", "for", "mod", "patch", "updated",
            "的", "版", "包"}
    return {p for p in parts if p not in stop and len(p) > 1}
