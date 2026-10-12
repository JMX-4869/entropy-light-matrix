"""扫描 JS 里仍是"界面文字"的中文（未包 t()）。

用**逐字符词法分析**而不是正则：模板字符串可以嵌套
（`${cond ? `a${x}` : 'b'}`），正则和"数反引号"都会误判。
词法分析按状态机走，能准确区分：
* 普通字符串（单引号 / 双引号）
* 模板字符串（反引号，含其中的 ${…} 表达式）
* 注释

只报告"在模板字符串的**字面文本部分**"或"在普通字符串里"的中文，
并给出是否已被 t() 包住。
"""
from __future__ import annotations

import io
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                              errors="replace")

INDEX = ROOT / "smd" / "web" / "index.html"
CJK = re.compile(r"[\u4e00-\u9fff]")
DATA = ("dlc_load.json", "launcher-v2.sqlite", "logkb.py", "Python 3",
        "Paradox Interactive", "steamapps", "ugc:", "v1.0.0",
        "l_simp", "l_english")

# 词法状态
NORMAL, SQ, DQ, TPL, LINE_C, BLOCK_C = range(6)


def scan_strings(src: str) -> list[dict]:
    """扫出所有字符串字面量，标注类型与内容，以及是否在 ${…} 里。

    返回 ``[{start, end, kind, text, in_expr}]``。
    kind: ``'sq'`` / ``'dq'`` / ``'tpl'``
    in_expr: 该字面量是否位于某个模板的 ``${…}`` 表达式内部
    """
    out = []
    i = 0
    n = len(src)
    state = NORMAL
    # 模板栈：每个元素记录"进入模板时的花括号深度"
    tpl_stack: list[int] = []
    brace = 0            # ${…} 的嵌套深度（只在模板内计数）
    start = -1
    while i < n:
        c = src[i]
        nxt = src[i + 1] if i + 1 < n else ""
        if state == NORMAL:
            if c == "/" and nxt == "/":
                state = LINE_C
                i += 2
                continue
            if c == "/" and nxt == "*":
                state = BLOCK_C
                i += 2
                continue
            if c == "'":
                state, start = SQ, i
            elif c == '"':
                state, start = DQ, i
            elif c == "`":
                state, start = TPL, i
                tpl_stack.append(brace)
                brace = 0
            i += 1
            continue
        if state == LINE_C:
            if c == "\n":
                state = NORMAL
            i += 1
            continue
        if state == BLOCK_C:
            if c == "*" and nxt == "/":
                state = NORMAL
                i += 2
                continue
            i += 1
            continue
        if state in (SQ, DQ):
            if c == "\\":
                i += 2
                continue
            if c == ("'" if state == SQ else '"'):
                out.append({"start": start, "end": i + 1,
                            "kind": "sq" if state == SQ else "dq",
                            "text": src[start + 1:i],
                            "in_expr": brace > 0})
                state = NORMAL
            i += 1
            continue
        if state == TPL:
            if c == "\\":
                i += 2
                continue
            if c == "$" and nxt == "{":
                brace += 1
                i += 2
                continue
            if c == "}" and brace > 0:
                brace -= 1
                i += 1
                continue
            if c == "`" and brace == 0:
                out.append({"start": start, "end": i + 1, "kind": "tpl",
                            "text": src[start + 1:i],
                            "in_expr": False})
                brace = tpl_stack.pop() if tpl_stack else 0
                state = NORMAL
            i += 1
            continue
    return out


def main() -> int:
    html = INDEX.read_text(encoding="utf-8")
    spans = [(m.start(1), m.end(1))
             for m in re.finditer(r"<script\b[^>]*>(.*?)</script>", html, re.S)]

    rows = []
    for a, b in spans:
        body = html[a:b]
        for s in scan_strings(body):
            text = s["text"].strip()
            if not text or not CJK.search(text):
                continue
            if any(d in text for d in DATA):
                continue
            # 已包 t()？看这个字面量前面 4 个字符
            abs_start = a + s["start"]
            before = html[max(0, abs_start - 5):abs_start]
            wrapped = before.endswith("t('") or before.endswith('t("')
            # 模板里的 ${…} 表达式内（如 ${cond?'中文':''}）也算"未包"
            rows.append({
                "text": text[:60],
                "kind": s["kind"],
                "in_expr": s["in_expr"],
                "wrapped": wrapped,
                "line": html[:abs_start].count("\n") + 1,
            })

    unwrapped = [r for r in rows if not r["wrapped"]]
    print("=" * 70)
    print("JS 里未包 t() 的界面中文")
    print("=" * 70)
    print(f"  字符串字面量总数: {len(rows)}")
    print(f"  已包 t(): {len(rows) - len(unwrapped)}")
    print(f"  未包: {len(unwrapped)}")
    print()
    by_kind: dict[str, list] = {}
    for r in unwrapped:
        k = ("模板·文本" if r["kind"] == "tpl" and not r["in_expr"]
             else "模板·表达式" if r["kind"] == "tpl"
             else "单引号" if r["kind"] == "sq" else "双引号")
        by_kind.setdefault(k, []).append(r)
    for k, items in sorted(by_kind.items(), key=lambda kv: -len(kv[1])):
        print(f"  [{k}] {len(items)} 处")
        for r in items[:10]:
            print(f"      L{r['line']:<5} {r['text']!r}")
        if len(items) > 10:
            print(f"      …还有 {len(items) - 10} 处")
        print()
    Path(ROOT / "tools" / "_js_unwrapped.json").write_text(
        json.dumps(unwrapped, ensure_ascii=False, indent=1), encoding="utf-8")
    print("  → tools/_js_unwrapped.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
