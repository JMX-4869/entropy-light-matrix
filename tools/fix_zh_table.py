"""把 i18n.js 的 **zh 表**里"值=键名"的条目改回中文原文。

背景：`rebuild_i18n.py` 给 zh 表写的是 `js_str(k)`（键名当值）。
这对中文键（`t('汉化')`，键本身就是中文）是对的，
但对 `page.xNNNN` 这类键名就错了 —— 中文界面会显示 "settings.x0049"。
测试 `task_test.py` 就是这么抓到的。

中文原文从 index.html 的 `data-i18n` 标注反查：
    <span data-i18n="settings.x0049">准备中…</span>
                        ↑ 键                          ↑ 中文

含子标签的情况（`>text<span>`）用非贪婪匹配补上。
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

I18N = ROOT / "smd" / "web" / "i18n.js"
INDEX = ROOT / "smd" / "web" / "index.html"


def table_range(src: str, lang: str) -> tuple[int, int]:
    m = re.search(rf"\b{lang}\s*:\s*\{{", src)
    start = m.end() - 1
    depth = 0
    for i in range(start, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return start, i
    return -1, -1


def main() -> int:
    src = I18N.read_text(encoding="utf-8")
    html = INDEX.read_text(encoding="utf-8")

    # 从 HTML 反查：键 → 中文原文
    zh_of: dict[str, str] = {}
    for m in re.finditer(r'data-i18n="([\w.]+)"[^>]*>([^<]{1,300})',
                         html):
        key, text = m.group(1), m.group(2).strip()
        if text and re.search(r"[\u4e00-\u9fff]", text):
            zh_of.setdefault(key, text)

    a, b = table_range(src, "zh")
    body = src[a:b]
    pairs = list(re.finditer(
        r"'([\w.]+)':\s*'((?:[^'\\]|\\.)*)'", body))
    fixed = 0
    for m in reversed(pairs):
        key, val = m.group(1), m.group(2)
        if val != key or not re.search(r"\.x\d{4}$", key):
            continue
        zh = zh_of.get(key)
        if not zh:
            continue
        # 换行必须转义成 \n —— 反查到的多行文案里带真实换行，
        # 直接塞进 JS 单引号字面量会断行，整个文件语法错误
        # （第一版就是这么错的，Node 校验拦住了）。
        esc = (zh.replace("\\", "\\\\").replace("'", "\\'")
                 .replace("\r", "").replace("\n", "\\n"))
        new = "'" + key + "': '" + esc + "'"
        body = body[:m.start()] + new + body[m.end():]
        fixed += 1
    print(f"  修正 zh 表条目 {fixed} 个")
    src = src[:a] + body + src[b:]

    # 剩余未修的
    left = [k for k, v in re.findall(r"'([\w.]+)':\s*'((?:[^'\\]|\\.)*)'",
                                     body)
            if k == v and re.search(r"\.x\d{4}$", k)]
    print(f"  仍为键名的 {len(left)} 个（HTML 里也查不到原文）")
    for k in left[:8]:
        print(f"    {k}")

    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                     encoding="utf-8", newline="\n") as fh:
        fh.write(src)
        tmp = fh.name
    r = subprocess.run(["node", "--check", tmp], capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    Path(tmp).unlink(missing_ok=True)
    if r.returncode != 0:
        print(f"  ！语法不合法，已放弃\n    {(r.stderr or '')[:280]}")
        return 1
    I18N.write_text(src, encoding="utf-8", newline="\n")
    print(f"  已写入 {I18N}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
