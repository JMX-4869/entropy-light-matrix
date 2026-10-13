"""给语义化页键补中文值。

`rebuild_i18n.py` 会把**表里已有的键**写进去，但两边的值它都从
`_i18n_trans.json` 取 —— 那张表只有英文。所以中文侧会被写成键名
（`ai.effort`），中文界面就会显示出键名而不是"思考深度"。

`fix_zh_table.py` 能从 HTML 反查中文，但它只认 `page.xNNNN` 这种
**自动生成**的键（靠"键名 → HTML 出现顺序"对应）。语义化键
（`ai.effort`）没有这层对应关系，它不处理。

这个脚本补上一环：**从 HTML 里按 data-i18n 属性名反查中文原文**，
写回 zh 表。适用于任何手写的语义化键。
"""
from __future__ import annotations

import io
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                              errors="replace")

INDEX = ROOT / "smd" / "web" / "index.html"
I18N = ROOT / "smd" / "web" / "i18n.js"
Q = chr(39)
CJK = re.compile(r"[\u4e00-\u9fff]")


def js_str(s: str) -> str:
    """转成 JS 单引号字面量。"""
    out = (s.replace("\\", "\\\\").replace(Q, "\\" + Q)
           .replace("\n", "\\n").replace("\r", "\\r"))
    return Q + out + Q


def main() -> int:
    html = INDEX.read_text(encoding="utf-8")
    src = I18N.read_text(encoding="utf-8")

    # 从 HTML 收集 data-i18n="key" 的中文原文
    found: dict[str, str] = {}
    for m in re.finditer(r'data-i18n="([\w.]+)"\s*>([^<]{1,400})', html):
        key, text = m.group(1), m.group(2).strip()
        text = re.sub(r"\s+", " ", text)
        if text and CJK.search(text):
            found[key] = text
    print(f"  HTML 里带中文的标注键: {len(found)} 个")

    # 找出 zh 表里"值是键名"或"值为空"的条目
    zh_block = re.search(r"\bzh\s*:\s*\{", src)
    if not zh_block:
        print("  ！找不到 zh 表")
        return 1
    body = src[zh_block.end():]
    body_end = body.find("\n    },")
    zh_src = body[:body_end if body_end > 0 else len(body)]

    # 找出 zh 表里"值是键名"的条目，直接把那个值换掉。
    #
    # 按**键**定位而不是按偏移量：偏移量要叠加好几层（表体起点、
    # 正则匹配位置、字面量边界），错一层就改错地方。
    # 用 `'键': '键'` 这种完整形态做替换，目标唯一、不依赖偏移。
    fixed = 0
    for key, zh_text in found.items():
        if zh_text == key:
            continue
        pair = f"{Q}{key}{Q}: {Q}{key}{Q}"
        if pair in src:
            src = src.replace(pair, f"{Q}{key}{Q}: {js_str(zh_text)}")
            fixed += 1
    print(f"  已替换 {fixed} 处")

    if not fixed:
        print("  无需修改")
        return 0

    I18N.write_text(src, encoding="utf-8", newline="\n")

    # 校验语法
    r = subprocess.run(["node", "--check", str(I18N)], capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        print(f"  ！语法不合法\n    {(r.stderr or '')[:250]}")
        return 1
    print("  i18n.js 语法 OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
