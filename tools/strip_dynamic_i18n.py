"""去掉"运行时动态更新"元素上的 data-i18n 标注。

原理：`applyI18n` 对带标注的元素执行 `el.textContent = t(key)` ——
它把文本**重置成静态值**。如果该元素的文本实际由 JS 在运行时更新
（`$('tpMsg').textContent = …`），两者就会互相打架：
每次 applyI18n 跑一遍（切换语言、MutationObserver 触发）就把
动态内容冲掉。

实测症状：点「停止」后任务栏的提示不变 —— 因为刚设进去的
"正在停止"立刻被 applyI18n 重置回了静态的「准备中…」。
测试 `task_test.py` 的「点击后立刻给出反馈」就是这么红的。

判定方法：找出所有 `$('id').textContent = …` 里的 id，
凡是带 data-i18n 的都去掉标注 —— 它们的文本由代码控制，
翻译应该在代码里用 `t()` 做，而不是靠静态标注。
"""
from __future__ import annotations

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


def main() -> int:
    html = INDEX.read_text(encoding="utf-8")

    # 1. 找出所有被 JS 动态设置 textContent 的元素 id
    dynamic_ids = set(re.findall(
        r"\$\('(\w+)'\)\.textContent\s*=", html))
    dynamic_ids |= set(re.findall(
        r"getElementById\('(\w+)'\)\.textContent\s*=", html))
    dynamic_ids |= set(re.findall(
        r"\$\('(\w+)'\)\.innerText\s*=", html))
    print(f"  动态更新文本的元素: {len(dynamic_ids)} 个")

    # 2. 去掉这些元素上的 data-i18n
    fixed = 0
    out = html
    for eid in sorted(dynamic_ids, key=len, reverse=True):
        # 匹配 `<tag ... id="eid" ... data-i18n="k" ...>` 并删掉标注
        pat = re.compile(
            r'(<[^>]*\bid="' + re.escape(eid) + r'"[^>]*?)\s+'
            r'data-i18n="[\w.]+"')
        out, n = pat.subn(r"\1", out)
        if n:
            fixed += n
            print(f"    {eid}: 去掉 {n} 处标注")
    print(f"  共去掉 {fixed} 处")

    if fixed == 0:
        print("  无需改动")
        return 0

    # 3. 校验
    bad = [t for t in ("div", "span", "button", "h3", "p", "section", "b")
           if len(re.findall(rf"<{t}[\s>]", out))
           != len(re.findall(rf"</{t}>", out))]
    if bad:
        print(f"  ！标签不平衡 {bad}，已放弃")
        return 1
    parts = re.findall(r"<script\b[^>]*>(.*?)</script>", out, re.S)
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                     encoding="utf-8", newline="\n") as fh:
        fh.write("\n;\n".join(parts))
        tmp = fh.name
    r = subprocess.run(["node", "--check", tmp], capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    Path(tmp).unlink(missing_ok=True)
    if r.returncode != 0:
        print(f"  ！脚本语法不合法，已放弃\n    {(r.stderr or '')[:250]}")
        return 1
    print("  标签平衡正常、脚本语法正常")

    INDEX.write_text(out, encoding="utf-8", newline="\n")
    print(f"  已写入 {INDEX}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
