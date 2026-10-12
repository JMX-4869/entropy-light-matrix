"""生成 render* 函数名单，供 applyLanguage 在切语言时统一重渲染。

为什么需要：`data-i18n` 只覆盖**静态 HTML 元素**（靠属性找元素、
替换 textContent）。界面里另一半文本是 JS 用 innerHTML 拼的
（renderDash 的路径列表、render* 的表格与统计行），切语言时不重跑
这些函数就一直是旧语言。

实测：`t('用户数据目录')` 在英文下返回 `'User data directory'`，
但 `#pathList` 的 DOM 里仍是中文 —— 因为 renderDash() 只在启动时跑过。
applyLanguage 原来只重渲染了 renderMods()，其余十几个渲染函数都没管。

名单规则：函数名以 render/load 开头，且**不含写操作**（保存/应用/删除/
写入/开关）。渲染函数只读状态、更新 DOM，重跑没有副作用。
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
OUT = ROOT / "tools" / "_render_list.json"

# 名字里带这些词的**不是**纯渲染函数：它们会写盘或改状态，重跑有副作用
DANGEROUS = ("save", "apply", "remove", "delete", "write", "commit",
             "reset", "clear", "cancel", "start", "run", "do", "set",
             "submit", "install", "create", "add", "toggle", "switch",
             "open", "close", "copy", "download", "import")

PREFIX = ("render", "load", "refresh", "fill", "draw", "paint", "update")


def main() -> int:
    h = INDEX.read_text(encoding="utf-8")
    src = "\n".join(m.group(1) for m in
                    re.finditer(r"<script\b[^>]*>(.*?)</script>", h, re.S))

    names: set[str] = set()
    # function 形式
    for m in re.finditer(r"(?:async\s+)?function\s+(\w+)\s*\(", src):
        names.add(m.group(1))
    # 对象方法形式（obj 里的 `  async renderX(){`）
    for m in re.finditer(r"^\s{2}(?:async\s+)?(\w+)\s*\([^)]*\)\s*\{", src,
                         re.M):
        names.add(m.group(1))

    picked = []
    for n in sorted(names):
        low = n.lower()
        if not low.startswith(PREFIX):
            continue
        if any(d in low for d in DANGEROUS):
            continue
        picked.append(n)

    print(f"  扫到函数 {len(names)} 个")
    print(f"  纯渲染/加载函数 {len(picked)} 个:")
    for n in picked:
        print(f"    {n}")
    OUT.write_text(json.dumps(picked, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    print()
    print(f"  → {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
