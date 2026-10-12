"""把标在含子元素标签上的 data-i18n 挪到纯文本的 span 上。

为什么必须修：`applyI18n` 是 `el.textContent = t(key)`，会**清空子节点**。
标在含子元素的标签上时，那些子元素（包括带 id、被 JS 取用的）会被删掉。
实测症状：崩溃页 `#crashSub` 消失 → `renderCrashes()` 抛 TypeError →
整张崩溃记录表空白。代码看上去没问题，很难反推。

改法：把外层标签的 data-i18n 去掉，给纯文本部分套一个 inner span。

    <h3 data-i18n="crash.x0004">崩溃记录 <span id="crashSub"></span></h3>
    ↓
    <h3><span data-i18n="crash.x0004">崩溃记录</span> <span id="crashSub"></span></h3>

只处理"**文本在前、子元素在后**"这种最常见的形式（全项目 16 处都是）。
其它形态（子元素在前、或文本夹在中间）打印出来让人工处理 ——
不猜，猜错会悄悄破坏结构。

用法: python tools/fix_i18n_nesting.py [--check]
"""
from __future__ import annotations

import argparse
import io
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                              errors="replace")

INDEX = ROOT / "smd" / "web" / "index.html"
VOID = {"br", "img", "hr", "input", "meta", "link", "source", "area"}


def inline_ok(text: str) -> tuple[bool, str]:
    parts = re.findall(r"<script\b[^>]*>(.*?)</script>", text, re.S)
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                     encoding="utf-8", newline="\n") as fh:
        fh.write("\n;\n".join(parts))
        p = fh.name
    try:
        r = subprocess.run(["node", "--check", p], capture_output=True,
                           text=True, encoding="utf-8", errors="replace",
                           timeout=120)
        return (r.returncode == 0), (r.stderr or "")[:200]
    finally:
        Path(p).unlink(missing_ok=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()

    html = INDEX.read_text(encoding="utf-8")
    out = html
    fixed = manual = 0

    # 从后往前改，位置不失效
    for m in reversed(list(re.finditer(
            r"<(\w+)([^>]*?)\sdata-i18n=\"([\w.]+)\"([^>]*)>", html))):
        tag = m.group(1)
        if tag in VOID:
            continue
        close = re.compile(rf"</{tag}\s*>", re.I).search(html, m.end())
        if not close:
            continue
        inner = html[m.end():close.start()]
        # 只处理"文本在前、紧跟着子元素"的形式
        mm = re.match(r"^([^<]+?)(\s*)(<.*)$", inner, re.S)
        if not mm:
            if re.search(r"<\w+", inner):
                manual += 1
                print(f"    [跳过·需人工] L{html[:m.start()].count(chr(10)) + 1}"
                      f" <{tag}> {inner[:60]!r}")
            continue
        text, gap, rest = mm.group(1), mm.group(2), mm.group(3)
        if not re.search(r"[\u4e00-\u9fff]", text):
            continue
        key = m.group(3)
        attrs = (m.group(2) + m.group(4)).strip()
        new_open = (f"<{tag}" + (f" {attrs}" if attrs else "") + ">"
                    + f'<span data-i18n="{key}">{text.strip()}</span>')
        out = (out[:m.start()] + new_open + gap + rest
               + out[close.start():])
        fixed += 1

    print(f"  修正 {fixed} 处，需人工 {manual} 处")

    ok, why = inline_ok(out)
    if not ok:
        print(f"  ！脚本语法不合法，已放弃\n    {why}")
        return 1
    bad = [t for t in ("div", "span", "button", "h3", "p", "section", "b",
                       "h1", "code")
           if len(re.findall(rf"<{t}[\s>]", out))
           != len(re.findall(rf"</{t}>", out))]
    if bad:
        print(f"  ！标签不平衡 {bad}，已放弃")
        return 1
    print("  标签平衡正常、脚本语法正常")

    if args.check:
        print("  （--check，未写入）")
        return 0
    INDEX.write_text(out, encoding="utf-8", newline="\n")
    print(f"  已写入 {INDEX}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
