"""修「键里带换行转义」导致查不到的问题。

现象：源码写 `t('没有 AI 分析结果。\\n在「崩溃报告」点…')`，
函数传入的是真实换行（`\\n` 被 JS 解析成换行），
而 i18n.js 里存的键是**双重转义**的 `\\\\n` —— 两边对不上，
`t()` 查不到就原样返回中文。

为什么不继续纠转义：转义层级（Python 字符串 → js_str → JS 字面量 → 运行时值）
很容易在某一层多一层或少一层，而这个键本来就不需要保留换行 ——

    t() 的返回值会**整体替换**元素文本，换行与否由 CSS 的 white-space 决定。

所以直接把键里的「换行 + 缩进」归一成**一个空格**：
源码与翻译表都用同一份"空格版"文本，不存在转义层级问题。
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
TRANS = ROOT / "tools" / "_i18n_trans.json"
NL = chr(92) + "n"          # 字面量的反斜杠 + n
Q = chr(39)


def main() -> int:
    src = INDEX.read_text(encoding="utf-8")

    # 把源码里那条 t('…\n…') 的键归一成空格版
    pat = re.compile(r"t\(" + Q + r"([^']*没有 AI 分析结果。[^']*)" + Q + r"\)")
    m = pat.search(src)
    if not m:
        print("  ！没找到那条 t() 调用")
        return 1
    old_key = m.group(1)
    # 先把字面量的 \n 换成真实换行，再把所有空白折叠成单空格
    real = old_key.replace(NL, "\n")
    new_key = re.sub(r"\s+", " ", real).strip()
    src = src[:m.start()] + f"t({Q}{new_key}{Q})" + src[m.end():]
    print(f"  旧键长度 {len(old_key)} → 新键长度 {len(new_key)}")
    print(f"  新键: {new_key[:60]}…")

    parts = re.findall(r"<script\b[^>]*>(.*?)</script>", src, re.S)
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                     encoding="utf-8", newline="\n") as fh:
        fh.write("\n;\n".join(parts))
        p = fh.name
    r = subprocess.run(["node", "--check", p], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    Path(p).unlink(missing_ok=True)
    if r.returncode != 0:
        print(f"  ！语法不合法，已放弃\n    {(r.stderr or '')[:250]}")
        return 1
    print("  脚本语法 OK")
    INDEX.write_text(src, encoding="utf-8", newline="\n")

    # 翻译表：删掉旧的转义版键，写入空格版
    d = json.loads(TRANS.read_text(encoding="utf-8")) if TRANS.is_file() else {}
    removed = [k for k in list(d) if "没有 AI 分析结果" in k]
    for k in removed:
        del d[k]
    d[new_key] = ("No AI analysis yet. Click “Analyze latest crash with AI” "
                  "on the Crash Reports page, or the ✦ buttons on Log Analysis "
                  "/ Load Order; conclusions are collected here.")
    TRANS.write_text(json.dumps(d, ensure_ascii=False, indent=1),
                     encoding="utf-8")
    print(f"  翻译表: 删旧键 {len(removed)} 个，写入空格版")
    return 0


if __name__ == "__main__":
    sys.exit(main())
