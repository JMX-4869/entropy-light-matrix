"""检查 i18n 表里"zh 值等于键名"的条目 —— 那些是没填中文的。

`rebuild_i18n.py` 对 zh 表写的是 `js_str(k)`，也就是**把键名当值**。
对中文键（`t('汉化')`）来说这是对的（键就是中文原文），
但对 `page.xNNNN` 这类键名来说完全错了 —— 中文界面会显示 "settings.x0049"。

出现原因：这批键只在英文表里有值（翻译来自 HTML 标注），
反查中文时没能从 HTML 里拿到原文（标注结构变了、或文本含子元素），
于是中文侧留了键名。
"""
from __future__ import annotations

import io
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                              errors="replace")

I18N = ROOT / "smd" / "web" / "i18n.js"
INDEX = ROOT / "smd" / "web" / "index.html"


def table(src: str, lang: str) -> str:
    m = re.search(rf"\b{lang}\s*:\s*\{{", src)
    start = m.end() - 1
    depth = 0
    for i in range(start, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start:i]
    return ""


def main() -> int:
    src = I18N.read_text(encoding="utf-8")
    zh = table(src, "zh")
    pairs = re.findall(r"'([^']+)':\s*'((?:[^'\\]|\\.)*)'", zh)
    bad = [(k, v) for k, v in pairs if k == v and re.search(r"\.x\d{4}$", k)]
    print(f"  zh 表条目 {len(pairs)}，其中「值=键名」的页面键 {len(bad)} 个")

    # 从 HTML 里反查这些键对应的中文原文
    html = INDEX.read_text(encoding="utf-8")
    fixed = {}
    for k, _ in bad:
        m = re.search(rf'data-i18n="{re.escape(k)}"[^>]*>([^<]{{1,200}})<', html)
        if m and re.search(r"[\u4e00-\u9fff]", m.group(1)):
            fixed[k] = m.group(1).strip()
    print(f"  能从 HTML 反查到中文的: {len(fixed)} 个")
    for k, v in list(fixed.items())[:8]:
        print(f"    {k} = {v!r}")
    miss = [k for k, _ in bad if k not in fixed]
    print(f"  仍查不到原文的: {len(miss)} 个")
    for k in miss[:8]:
        print(f"    {k}")
    Path(ROOT / "tools" / "_zh_fix.json").write_text(
        __import__("json").dumps(fixed, ensure_ascii=False, indent=1),
        encoding="utf-8")
    print("  → tools/_zh_fix.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
