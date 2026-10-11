"""Clausewitz (Paradox) 脚本解析器。

群星的所有游戏数据都是这种格式。要做「语义级冲突检测」就必须真正把文件
解析成语法树，而不是用正则去猜——否则无法区分「同一个键被两个模组重复定义」
和「两个模组各自定义了不同内容」。

实现要点
--------
* 保留键的插入顺序（群星里后定义覆盖先定义，顺序即语义）。
* 记录每个节点的行号，便于把冲突定位到具体行。
* 兼容 ``key = value`` / ``key = { ... }`` / ``key < value`` / ``key > value``。
* 宽容模式：任何解析异常都退化为「尽力而为」，绝不因为某个模组写错了语法
  就把整个扫描流程弄崩——毕竟模组语法错误本身就是要报告的问题之一。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator

__all__ = ["Node", "parse", "parse_string", "ParseError", "iter_leaf_keys"]

# 标点中会被当作「单字符运算符」的符号
_OPERATORS = set("=<>!?")

_NUMBER_RE = re.compile(r"^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$")


class ParseError(Exception):
    """语法错误，附带行号。"""

    def __init__(self, message: str, line: int = 0, col: int = 0) -> None:
        super().__init__(f"{message} (行 {line}, 列 {col})")
        self.message = message
        self.line = line
        self.col = col


@dataclass
class Node:
    """语法树节点。

    ``value`` 为 ``dict[str, list[Node]]`` 时表示一个作用域（block）。
    用 list 而不是单值，因为群星允许同名键重复出现（例如多个 ``modifier``）。
    """

    key: str | None = None
    value: Any = None
    line: int = 0
    col: int = 0
    # 运算符号，用于区分 `key = x` 与 `key < x`
    op: str = "="

    # ---- 便捷访问 -------------------------------------------------
    @property
    def is_block(self) -> bool:
        """本节点是否为作用域（``{ ... }``）。"""
        return isinstance(self.value, dict)

    def get(self, key: str) -> Node | None:
        """取同名键中的第一个（群星里通常第一个即生效的那个）。"""
        if not self.is_block:
            return None
        lst = self.value.get(key)
        return lst[0] if lst else None

    def get_all(self, key: str) -> list["Node"]:
        """取同名键的**全部**节点（群星允许同名键重复出现）。"""
        if not self.is_block:
            return []
        return list(self.value.get(key, ()))

    def scalar(self, key: str, default: Any = None) -> Any:
        """取某个键的标量值；键不存在或是块时返回 ``default``。"""
        n = self.get(key)
        if n is None:
            return default
        return n.value if not n.is_block else default

    def keys(self) -> Iterable[str]:
        """本作用域内的所有键名（非块节点返回空）。"""
        if self.is_block:
            return self.value.keys()
        return ()

    def items(self) -> Iterator[tuple[str, "Node"]]:
        """遍历 ``(键, 节点)`` 对，同名键会出现多次。"""
        if not self.is_block:
            return iter(())
        for k, lst in self.value.items():
            for n in lst:
                yield k, n

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        if self.is_block:
            return f"Node({self.key!r}, block[{len(self.value)}], line={self.line})"
        return f"Node({self.key!r}, {self.value!r}, line={self.line})"


def _tokenize(text: str) -> Iterator[tuple[str, str, int, int]]:
    """产出 ``(kind, text, line, col)``。

    kind ∈ {``word``, ``string``, ``op``, ``{``, ``}``}
    """
    i = 0
    n = len(text)
    line = 1
    line_start = 0

    while i < n:
        ch = text[i]

        # 换行 / 空白
        if ch == "\n":
            line += 1
            i += 1
            line_start = i
            continue
        if ch in " \t\r\v\f":
            i += 1
            continue

        # 注释
        if ch == "#":
            j = text.find("\n", i)
            i = n if j < 0 else j
            continue

        # 字符串
        if ch == '"':
            start_line, start_col = line, i - line_start + 1
            j = i + 1
            buf: list[str] = []
            while j < n:
                c = text[j]
                if c == "\\" and j + 1 < n:
                    buf.append(text[j + 1])
                    j += 2
                    continue
                if c == '"':
                    break
                if c == "\n":
                    # 未闭合字符串——群星本身会报
                    # "Expected quotation mark (") at end of line"
                    line += 1
                    line_start = j + 1
                buf.append(c)
                j += 1
            yield ("string", "".join(buf), start_line, start_col)
            i = j + 1
            continue

        # 块定界符
        if ch == "{":
            yield ("{", ch, line, i - line_start + 1)
            i += 1
            continue
        if ch == "}":
            yield ("}", ch, line, i - line_start + 1)
            i += 1
            continue

        # 运算符（含 ==、>=、<=、!=、?=）
        # 注意 `!` 和 `?` 只有在后面紧跟 `=` 时才算运算符；
        # 否则它们是普通词的一部分（例如文件名 `foo!.txt`、
        # 或脚本里的 `NOT` 写法），必须并入词元，不然会死循环。
        if ch in _OPERATORS:
            start_col = i - line_start + 1
            nxt = text[i + 1] if i + 1 < n else ""
            if ch == "=":
                if nxt == "=":
                    yield ("op", "==", line, start_col)
                    i += 2
                else:
                    yield ("op", "=", line, start_col)
                    i += 1
                continue
            if ch in "<>" or nxt == "=":
                if nxt == "=":
                    yield ("op", ch + "=", line, start_col)
                    i += 2
                else:
                    yield ("op", ch, line, start_col)
                    i += 1
                continue
            # `!` / `?` 后面不是 `=`：并入普通词处理
        # 普通词
        start_line, start_col = line, i - line_start + 1
        j = i
        while j < n:
            c = text[j]
            if c in ' \t\r\n\v\f"#{}=' or c == "{":
                break
            if c in "<>":
                break
            if c == "=":
                break
            if c in "!?" and not (j + 1 < n and text[j + 1] == "="):
                # 孤立的 ! ? 属于词的一部分
                j += 1
                continue
            if c in "!?":
                break
            j += 1
        if j == i:
            # 无法识别的字符，跳过以免死循环
            j = i + 1
        yield ("word", text[i:j], start_line, start_col)
        i = j


def _coerce(word: str) -> Any:
    """把裸词转成合适类型：``yes``/``no`` -> bool，数字 -> int/float。"""
    if word == "yes":
        return True
    if word == "no":
        return False
    if _NUMBER_RE.match(word):
        try:
            if any(c in word for c in ".eE"):
                return float(word)
            return int(word)
        except ValueError:
            return word
    return word


# 解析状态机
#
# 群星脚本有两种合法写法，必须同时支持：
#
#   key = value          —— 显式赋值（最常见）
#   a b c                —— 省略 `=` 的列表（例如 tags = { "A" "B" }）
#
# 难点在于 `foo bar` 到底表示「键 foo、值 bar」还是「列表里的两个元素」，
# 只有读到**下一个 token** 才能确定：
#   * 下一个是运算符 `=` → `foo` 是键
#   * 下一个又是词/块     → `foo` 只是列表元素
#
# 所以用一个「挂起 token」的模型：读到词先挂起，等下一个 token 来裁决。
_S_WANT_TOKEN = 0    # 期待任意 token
_S_WANT_VALUE = 1    # 刚读到 `key <op>`，下一个 token 必须是值


@dataclass
class _Frame:
    """解析栈的一层。"""

    container: dict[str, list[Node]]
    state: int = _S_WANT_TOKEN
    # 挂起的词：可能是键，也可能是列表元素
    pend: str = ""
    pend_line: int = 0
    pend_col: int = 0
    pend_is_string: bool = False
    # 已确认的键与运算符
    key: str = ""
    key_line: int = 0
    key_col: int = 0
    op: str = "="
    # 该层对应的 '{' 位置（用于「未闭合」报错定位）
    open_line: int = 0
    open_col: int = 0


def _put(fr: _Frame, key: str, node: Node) -> None:
    fr.container.setdefault(key, []).append(node)


def parse_string(text: str, *, strict: bool = False) -> Node:
    """解析一段脚本，返回一个虚拟根节点（见模块顶部的说明）。"""
    root = Node(key=None, value={}, line=1, col=1)
    stack: list[_Frame] = [_Frame(root.value)]
    errors: list[ParseError] = []

    try:
        for kind, tok, ln, col in _tokenize(text):
            fr = stack[-1]

            # ================= 块开始 =================
            if kind == "{":
                if fr.state == _S_WANT_VALUE:
                    # `key = { ... }` 或 `key <op> { ... }`
                    key, op = fr.key, fr.op
                    kln, kcol = fr.key_line, fr.key_col
                elif fr.pend:
                    # `foo { ... }` —— 挂起的词其实是这个块的键
                    key, op = fr.pend, "="
                    kln, kcol = fr.pend_line, fr.pend_col
                else:
                    # 纯匿名块：列表元素
                    key, op, kln, kcol = "", "=", ln, col
                node = Node(key=key, value={}, line=kln, col=kcol, op=op)
                _put(fr, key, node)
                # 该层消费完毕
                fr.state = _S_WANT_TOKEN
                fr.pend = ""
                fr.key = ""
                fr.op = "="
                stack.append(_Frame(node.value, open_line=ln, open_col=col))
                continue

            # ================= 块结束 =================
            if kind == "}":
                if len(stack) > 1:
                    # 闭合前若还有挂起的词，它是一个无值元素
                    if fr.pend:
                        _put(fr, "", Node(key="", value=fr.pend, line=fr.pend_line,
                                          col=fr.pend_col))
                        fr.pend = ""
                    stack.pop()
                else:
                    errors.append(ParseError("多余的 '}'", ln, col))
                continue

            # ================= 运算符 =================
            if kind == "op":
                if fr.pend:
                    # 挂起的词确认为键
                    fr.key = fr.pend
                    fr.key_line = fr.pend_line
                    fr.key_col = fr.pend_col
                    fr.pend = ""
                    fr.op = tok
                    fr.state = _S_WANT_VALUE
                    continue
                if fr.state == _S_WANT_VALUE:
                    # `a = = b` 之类畸形
                    errors.append(ParseError(f"意外的运算符 {tok!r}", ln, col))
                    continue
                errors.append(ParseError(f"缺少键的运算符 {tok!r}", ln, col))
                continue

            # ================= 词 / 字符串 =================
            val = tok if kind == "string" else _coerce(tok)

            if fr.state == _S_WANT_VALUE:
                # 这是键对应的值
                node = Node(key=fr.key, value=val, line=fr.key_line,
                            col=fr.key_col, op=fr.op)
                _put(fr, fr.key, node)
                fr.state = _S_WANT_TOKEN
                fr.key = ""
                fr.op = "="
                continue

            # state == _S_WANT_TOKEN
            if fr.pend:
                # 前一个挂起的词没有跟运算符 → 它只是列表元素
                _put(fr, "", Node(key="", value=fr.pend, line=fr.pend_line,
                                  col=fr.pend_col))
            fr.pend = tok
            fr.pend_line = ln
            fr.pend_col = col
            fr.pend_is_string = (kind == "string")
            continue

        # 收尾：把最外层挂起的词也算进去
        for fr in stack:
            if fr.pend:
                _put(fr, "", Node(key="", value=fr.pend, line=fr.pend_line,
                                  col=fr.pend_col))
                fr.pend = ""

        # 括号配对检查 —— 必须做，否则「少一个 }」这种最常见的模组
        # 语法错误会被静默接受，导致补丁看起来没问题却让整个文件失效。
        if len(stack) > 1:
            unclosed = len(stack) - 1
            opener = stack[-1]
            errors.append(ParseError(
                f"有 {unclosed} 个 '{{' 没有闭合",
                getattr(opener, "open_line", 0) or 0,
                getattr(opener, "open_col", 0) or 0))
    except RecursionError:  # pragma: no cover
        errors.append(ParseError("解析深度过大"))
    except Exception as exc:  # pragma: no cover - 防御性
        errors.append(ParseError(str(exc)))

    if strict and errors:
        raise errors[0]
    if errors:
        root.value.setdefault("__parse_errors__", []).extend(
            Node(key="__error__", value=str(e), line=e.line, col=e.col) for e in errors
        )
    return root


def parse(path, *, strict: bool = False) -> Node:
    """解析文件。"""
    from .paths import read_text

    return parse_string(read_text(path), strict=strict)


def iter_leaf_keys(node: Node, prefix: str = "") -> Iterator[tuple[str, Node]]:
    """深度遍历，产出 ``(点分路径, 叶子节点)``。

    用于比较两个模组是否定义了同一批「唯一标识符」（trigger / 静态修正量等）。
    """
    if not node.is_block:
        yield prefix, node
        return
    for key, lst in node.value.items():
        for child in lst:
            p = f"{prefix}.{key}" if prefix else key
            if child.is_block:
                yield from iter_leaf_keys(child, p)
            else:
                yield p, child


def flatten_definitions(node: Node, prefix: str = "") -> dict[str, Node]:
    """顶层作用域内的定义集合：``{名字: 节点}``。

    群星数据文件基本都是 ``name = { ... }`` 形式（例如
    ``common/scripted_triggers`` 里的 ``my_trigger = { ... }``）。
    同一个名字在两个模组里出现就是「重定义」，是冲突的第一大类。
    """
    out: dict[str, Node] = {}
    if not node.is_block:
        return out
    for key, lst in node.value.items():
        if key.startswith("__"):
            continue
        for child in lst:
            out[key] = child
    return out


def canonical_text(node: Node) -> str:
    """把节点重排成规范化文本，用于判断两个定义是否「内容相同」。

    内容完全相同的重定义属于安全覆盖（覆盖顺序无关紧要），
    内容不同的才是真正需要用户决策的冲突。
    """
    parts: list[str] = []

    def walk(n: Node) -> None:
        if not n.is_block:
            v = n.value
            if isinstance(v, bool):
                v = "yes" if v else "no"
            parts.append(f"{v!r}")
            return
        # 排序键但保留重复值的多重集，避免顺序差异造成误判
        entries = sorted(
            f"{k}={canonical_text(c)}" for k, c in n.items()
        )
        parts.append("{" + ",".join(entries) + "}")

    walk(node)
    return "".join(parts)
