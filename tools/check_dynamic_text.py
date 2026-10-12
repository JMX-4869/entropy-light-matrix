"""找出「静态默认文本是中文、但文本由 JS 动态设置」的元素。

这类元素去掉了 `data-i18n`（否则 applyI18n 会把动态内容重置回静态值），
于是它们的静态默认值会原样显示 —— 英文用户会看到中文，
除非代码里用 `t()` 设置文本。

本脚本列出这些元素及其当前默认文本，并把「默认文本是中文」的挑出来，
人工核对代码里有没有 `t()`；代码里没有的，靠 `t()` 包一层默认值兜住。
"""
from __future__ import annotations

import io
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                              errors="replace")

html = (ROOT / "smd" / "web" / "index.html").read_text(encoding="utf-8")

# 找所有元素，看它的 id、静态文本、以及是否有 data-i18n
ELEM = re.compile(
    r"<(\w+)([^>]*?\bid=\"(\w+)\"([^>]*?))>([^<]{0,80})", re.S)
rows = []
for m in ELEM.finditer(html):
    attrs, eid, text = m.group(2), m.group(3), m.group(5).strip()
    if not text or not re.search(r"[\u4e00-\u9fff]", text):
        continue
    if "data-i18n" in attrs:
        continue                       # 有标注，applyI18n 负责
    # 是否由 JS 动态设置
    dyn = bool(re.search(
        rf"\$\('{re.escape(eid)}'\)\.(textContent|innerText|innerHTML)\s*=", html)
        or re.search(
        rf"getElementById\('{re.escape(eid)}'\)\.(textContent|innerText)\s*=",
        html))
    if not dyn:
        continue
    # 代码里有没有用 t() 设置（那就没问题）
    uses_t = bool(re.search(
        rf"\$\('{re.escape(eid)}'\)\.(textContent|innerText)\s*=[^;]{{0,120}}t\(",
        html))
    rows.append((eid, text[:40], uses_t))

print(f"  中文默认值 + 动态设置的元素: {len(rows)} 个\n")
ok = [r for r in rows if r[2]]
bad = [r for r in rows if not r[2]]
print(f"  代码里已用 t() 的: {len(ok)} 个")
for eid, t, _ in ok[:10]:
    print(f"    {eid:16} 默认={t!r}")
print(f"\n  **代码里没有 t() 的: {len(bad)} 个**（英文环境会显示中文）")
for eid, t, _ in bad:
    print(f"    {eid:16} 默认={t!r}")
