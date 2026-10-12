"""检查"data-i18n 标在含子元素的标签上"这种结构（会删掉子元素）。

原理：`applyI18n` 的实现是

    el.textContent = t(el.getAttribute('data-i18n'));

`textContent = ...` 会**清空该元素的全部子节点**，再放一段纯文本。
所以只要 `data-i18n` 标在一个**内部还有元素**的标签上，那些子元素
就被整个删掉了。

实测后果（真 bug）：崩溃页的

    <h3 data-i18n="crash.x0004">崩溃记录 <span class="sub" id="crashSub"></span></h3>

在切语言/启动后，`#crashSub` 从 DOM 里消失。随后 `renderCrashes()`
执行 `$('crashSub').textContent = ...` 抛 TypeError，
整张崩溃记录表渲染不出来 —— 表现为"崩溃页空白"，
而代码看上去完全正常。

正确写法是把 data-i18n 标在**纯文本的子 span** 上：

    <h3><span data-i18n="crash.x0004">崩溃记录</span>
        <span class="sub" id="crashSub"></span></h3>

用法: python tools/check_i18n_nesting.py
"""
from __future__ import annotations

import io
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                              errors="replace")

INDEX = ROOT / "smd" / "web" / "index.html"

VOID = {"br", "img", "hr", "input", "meta", "link", "source", "area"}


def main() -> int:
    html = INDEX.read_text(encoding="utf-8")
    bad = []
    # 找所有带 data-i18n 的开标签，看它的内容里有没有子元素
    for m in re.finditer(r"<(\w+)([^>]*\bdata-i18n(?:-ph|-title)?=[^>]*)>",
                         html):
        tag, attrs = m.group(1), m.group(2)
        if tag in VOID:
            continue
        # 取到配对闭合标签之间的内容
        close = re.compile(rf"</{tag}\s*>", re.I)
        e = close.search(html, m.end())
        if not e:
            continue
        inner = html[m.end():e.start()]
        # 去掉文本与空白，看还剩不剩标签
        kids = [k for k in re.findall(r"<(\w+)", inner) if k not in VOID]
        if not kids:
            continue
        # 有子元素 → 会被 textContent 覆盖掉
        line = html[:m.start()].count("\n") + 1
        key = re.search(r'data-i18n="([\w.]+)"', attrs)
        bad.append({
            "line": line,
            "tag": tag,
            "key": key.group(1) if key else "?",
            "kids": kids,
            "snippet": re.sub(r"\s+", " ",
                              html[m.start():e.end()])[:150],
        })

    print("=" * 72)
    print("data-i18n 嵌套检查（标注在含子元素的标签上 = 子元素会被删除）")
    print("=" * 72)
    if not bad:
        print("  未发现问题")
        return 0
    print(f"  发现 {len(bad)} 处：\n")
    with_id = 0
    for b in bad:
        has_id = "id=" in b["snippet"]
        if has_id:
            with_id += 1
        flag = "  <<< 含 id（会被 JS 取用，必然报错）" if has_id else ""
        print(f"  L{b['line']:<5} <{b['tag']}> key={b['key']}"
              f" 子元素={b['kids'][:4]}{flag}")
        print(f"          {b['snippet'][:130]}")
    print()
    print(f"  其中含 id 的 {with_id} 处 —— 这些是最危险的："
          f"元素消失后 JS 取不到，会抛 TypeError 打断整个渲染")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
