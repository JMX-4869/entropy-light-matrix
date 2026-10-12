"""HTML 文本节点扫描器。

**为什么不能再用正则**：原文案提取用的是
``>([^<>{}]{1,400}?)<``，它假定"两个标签之间的内容里不会出现 `<`"。
但只要 HTML 是 ``<span class="k">键</span><span class="v">值</span>``
这种紧挨着写的形式（项目里到处都是），非贪婪匹配就会在**下一个标签的
``<`` **处停下，抓到的"文本"其实是 ``键</span><span class="v"`` ——
既不是纯文本、也不含中文以外的 `<`，于是被过滤掉。

实测后果：``用户数据目录``／``游戏本体目录``／``日志目录`` 这一整排
路径标签**根本没进映射表**，英文界面里它们原样显示中文。

正确做法是逐字符扫一遍：识别标签（含引号内的 `>`）与文本节点，
给出每个文本节点的**准确起止位置**。这样：
* 位置是精确的，下游按位置注入不会偏
* 不受"相邻标签"影响
* 可以顺带识别 script / 已标注元素的范围

只做这一个项目需要的事：不建 DOM 树，不处理嵌套语义，
只要求"文本节点 + 位置 + 是否在某个范围里"是准的。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class TextNode:
    """一个文本节点：位置、原文、折叠空白后的文本。"""

    start: int
    end: int
    raw: str

    @property
    def text(self) -> str:
        """去掉首尾空白后的文本（界面文字比对用这个，不比 raw）。"""
        return self.raw.strip()


@dataclass
class Tag:
    """一个标签：位置、原文、是否闭合标签、标签名。"""

    start: int
    end: int
    raw: str
    closing: bool
    name: str


def scan(html: str) -> tuple[list[TextNode], list[Tag]]:
    """扫一遍 HTML，返回（文本节点, 标签）两个列表，按位置有序。

    识别规则：
    * ``<`` 开始一个标签，读到配对的 ``>`` 为止（引号内的 ``>`` 不算）
    * ``<!--`` 到 ``-->`` 是注释，整体跳过
    * ``<script``/``<style`` 的内容按原样跳过（不当文本节点）
    * 其余两个标签之间的内容算文本节点
    """
    nodes: list[TextNode] = []
    tags: list[Tag] = []
    i = 0
    n = len(html)
    # 记录 script/style 的内容区间，扫完后从文本节点里剔除
    raw_spans: list[tuple[int, int]] = []

    while i < n:
        lt = html.find("<", i)
        if lt < 0:
            if i < n:
                nodes.append(TextNode(i, n, html[i:n]))
            break
        # `<` 之前的是文本
        if lt > i:
            nodes.append(TextNode(i, lt, html[i:lt]))
        # 注释
        if html.startswith("<!--", lt):
            gt = html.find("-->", lt + 4)
            gt = n if gt < 0 else gt + 3
            tags.append(Tag(lt, gt, html[lt:gt], False, "!--"))
            i = gt
            continue
        # 找标签结束的 `>`，跳过引号里的内容
        k = lt + 1
        quote = ""
        while k < n:
            c = html[k]
            if quote:
                if c == quote:
                    quote = ""
            elif c in "\"'":
                quote = c
            elif c == ">":
                break
            k += 1
        if k >= n:                       # 没有闭合的 `>`：剩下的当文本
            nodes.append(TextNode(lt, n, html[lt:n]))
            break
        end = k + 1
        raw = html[lt:end]
        closing = raw.startswith("</")
        # 标签名：`<` 或 `</` 之后到空白或 `>` 之前
        m = raw[2:] if closing else raw[1:]
        name = ""
        for ch in m:
            if ch.isspace() or ch in "/>":
                break
            name += ch
        tags.append(Tag(lt, end, raw, closing, name.lower()))

        # script / style：内容整体跳过，不当文本
        if name.lower() in ("script", "style") and not closing:
            close = html.find(f"</{name.lower()}", end)
            close = n if close < 0 else close
            raw_spans.append((end, close))
            i = close
            continue
        i = end

    # 剔除 script/style 内容区里的"文本节点"
    if raw_spans:
        kept = []
        for nd in nodes:
            if any(a <= nd.start < b for a, b in raw_spans):
                continue
            kept.append(nd)
        nodes = kept
    return nodes, tags


def covered_spans(tags: list[Tag], html: str,
                  attr: str = "data-i18n") -> list[tuple[int, int]]:
    """带指定属性的元素所覆盖的区间（含其内部）。

    用栈匹配同名标签，够用且不需要完整 DOM。自闭合和 void 元素
    （img/br/input…）不参与嵌套。
    """
    VOID = {"img", "br", "hr", "input", "meta", "link", "source", "area",
            "base", "col", "embed", "param", "track", "wbr"}
    out: list[tuple[int, int]] = []
    for idx, t in enumerate(tags):
        if t.closing or attr not in t.raw or t.name in VOID:
            continue
        depth = 0
        for u in tags[idx + 1:]:
            if u.name != t.name:
                continue
            if u.closing:
                if depth == 0:
                    out.append((t.start, u.end))
                    break
                depth -= 1
            elif u.name not in VOID and not u.raw.endswith("/>"):
                depth += 1
        else:
            out.append((t.start, t.end))
    return out
