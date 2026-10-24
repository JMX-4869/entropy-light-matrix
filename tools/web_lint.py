"""前端静态检查 —— 只查**误报率低**的三类，能一次指出「哪一行哪一处」。

教训：第一版写了 6 类检查，跑出 720 条，99% 是误报
（把「方法定义」当成「裸调」、把全局函数当成 env 方法）。
误报多的检查器等于没有检查器 —— 人看一眼就关掉了。

所以这里只保留能给出**确定结论**的三条：
  ① 前端 api('x') 调的方法，webui.py 里必须真有
  ② HTML 里不能有重复 id
  ③ 静态 HTML 里不能写 ${t(...)}（那只有模板串里才会被求值）

再加两条语法级的：JS 语法（node --check 管，这里跳过）、Python 语法。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HTML = ROOT / "smd" / "web" / "index.html"
WEBUI = ROOT / "smd" / "webui.py"


def main() -> int:
    html = HTML.read_text(encoding="utf-8")
    scripts = "\n;\n".join(re.findall(r"<script>(.*?)</script>", html, re.S))
    problems: list[str] = []

    # ① 前端调的 api 方法，后端必须有
    called = set(re.findall(r"""api\(\s*['"]([a-zA-Z_]\w*)['"]""", scripts))
    if WEBUI.is_file():
        py = WEBUI.read_text(encoding="utf-8")
        backend = set(re.findall(r"def\s+([a-zA-Z_]\w*)\s*\(", py))
        missing = sorted(called - backend)
        for m in missing:
            problems.append(f"api('{m}') —— webui.py 里没有这个方法")
        print(f"  ① api 方法 {len(called)} 个，缺 {len(missing)} 个")

    # ② 重复 id
    ids = re.findall(r'\bid="([^"]+)"', html)
    seen: dict[str, int] = {}
    for i in ids:
        seen[i] = seen.get(i, 0) + 1
    dups = [k for k, v in seen.items() if v > 1]
    for d in dups:
        problems.append(f"重复 id: {d}")
    print(f"  ② id {len(seen)} 个，重复 {len(dups)} 个")

    # ③ <script> 之外的 ${t(...)}：那是真不会被求值。
    # **必须排除 script 内部** —— 模板字符串本来就写在 JS 里，
    # 那里 ${t(...)} 是对的（第一版没排除，报了 12 条误报）。
    outside = re.sub(r"<script>.*?</script>", "", html, flags=re.S)
    bad = 0
    for ln, line in enumerate(outside.splitlines(), 1):
        if "${t(" in line:
            problems.append(f"L{ln} <script> 之外写了 ${{t(...)}}，不会被求值")
            bad += 1
    print(f"  ③ script 外的模板串 {bad} 处")

    # 输出
    if not problems:
        print("\n  ✓ 三项全过")
        return 0
    print(f"\n  发现 {len(problems)} 个问题：")
    for p in problems:
        print(f"    · {p}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
