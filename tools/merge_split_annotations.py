"""修「data-i18n 只包住一段文本的一部分」这种结构 —— 会导致中英并存。

现象：成就卡片的说明里显示
    Checks actual files against … (localisation is not checksum-covered).
    纯汉化不影响成就（localisation 不在校验范围内）。
前半句被翻译了，后半句原样留着中文。

原因：标注只套住前半句，而后半句（含 `<b>`）是**外层元素里的另一个
文本节点**。applyI18n 只替换被标注元素的 textContent，
管不到它的兄弟节点。

修法：把整段中文（连同其中的 `<b>`）放进**一个**标注元素里：

    <div class="hint">
      <span data-i18n="ach.intro">前半句…</span>
      <b>纯汉化不影响成就</b>（…）。
    </div>
    ↓
    <div class="hint">
      <span data-i18n="ach.intro">前半句… <b>纯汉化不影响成就</b>（…）。</span>
    </div>

`applyI18n` 替换的是 textContent，所以 `<b>` 在翻译后不会再保留加粗 ——
这是可接受的取舍（英文那句本来就不同构），比中英并存好。

同时给 `check_i18n_nesting.py` 补一条检查：被标注元素之后若紧跟
裸文本，也提示出来（这类问题它现在漏检）。
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
CJK = re.compile(r"[\u4e00-\u9fff]")


def main() -> int:
    html = INDEX.read_text(encoding="utf-8")
    fixed = 0

    # 找「<span data-i18n="k">A</span> B」形式：A 是中文、B 也是中文
    pat = re.compile(
        r'<span data-i18n="([\w.]+)"[^>]*>([^<]*[\u4e00-\u9fff][^<]*)</span>'
        r'((?:\s*<b>[^<]*</b>)?[^<]*[\u4e00-\u9fff][^<]*)')
    for m in reversed(list(pat.finditer(html))):
        key, first, rest = m.group(1), m.group(2), m.group(3)
        tail = rest.strip()
        if not tail:
            continue
        # 把后半句并进标注内部
        merged = f'{first.rstrip()} {rest.strip()}'
        new = f'<span data-i18n="{key}">{merged}</span>'
        html = html[:m.start()] + new + html[m.end():]
        fixed += 1
        print(f"  {key}: 合并后半句 {tail[:40]!r}")

    print(f"  共修 {fixed} 处")
    if not fixed:
        return 0

    # 校验
    bad = [t for t in ("div", "span", "button", "h3", "p", "section", "b")
           if len(re.findall(rf"<{t}[\s>]", html))
           != len(re.findall(rf"</{t}>", html))]
    if bad:
        print(f"  ！标签不平衡 {bad}，已放弃")
        return 1
    parts = re.findall(r"<script\b[^>]*>(.*?)</script>", html, re.S)
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                     encoding="utf-8", newline="\n") as fh:
        fh.write("\n;\n".join(parts))
        p = fh.name
    r = subprocess.run(["node", "--check", p], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    Path(p).unlink(missing_ok=True)
    if r.returncode != 0:
        print(f"  ！脚本语法不合法，已放弃\n    {(r.stderr or '')[:250]}")
        return 1
    print("  标签平衡 OK / 脚本语法 OK")
    INDEX.write_text(html, encoding="utf-8", newline="\n")
    print(f"  已写入 {INDEX}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
