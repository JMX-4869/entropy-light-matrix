"""把 JS 里的中文字面量替换成 t('中文')，让运行时提示也跟随语言。

配套：
* `extract_js_strings.py`  提取待翻译的 JS 字符串
* `plan_js_replace.py`     生成替换点报告（含上下文，便于确认）
* 本脚本                   执行替换

三条安全措施：
1. **跳过注释**：`// 中文` 和 `/* 中文 */` 里的字面量不是界面文字。
   实测报告里有 4 处是注释（例如 `// 表现就是"切到批量生成却是一片空白"`）。
2. **跳过比较值**：`x === '启用'` 这类是逻辑判断，替换会改坏行为。
3. **替换后跑 Node 语法校验**：改错了立刻发现，不落盘。

为什么用 t('中文') 而不是英文键名：翻译表里 JS 那批的键就是中文原文，
直接当键用最省事，也避免再造一套键名映射。
"""
from __future__ import annotations

import io
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                              errors="replace")

INDEX = ROOT / "smd" / "web" / "index.html"
REPORT = ROOT / "tools" / "_js_replace_report.json"

# 这些位置说明字面量是逻辑值，不是给人看的
SKIP_BEFORE = ("===", "!==", "==", "!=", ".includes(", "indexOf(",
               "startsWith(", "endsWith(", "hasOwnProperty", "case ")


def comment_spans(text: str) -> list[tuple[int, int]]:
    """扫描出注释区间（``//`` 与 ``/* */``）。

    必须跳过：注释里的中文不是界面文字，替换了会变成
    ``// t('…')`` 这种无意义的代码，还可能引入引号不平衡。
    """
    spans = []
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c in "\"'`":                       # 字符串，跳过
            q = c
            i += 1
            while i < n:
                if text[i] == "\\":
                    i += 2
                    continue
                if text[i] == q:
                    break
                i += 1
            i += 1
            continue
        if text.startswith("//", i):
            j = text.find("\n", i)
            j = n if j < 0 else j
            spans.append((i, j))
            i = j
            continue
        if text.startswith("/*", i):
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
            spans.append((i, j))
            i = j
            continue
        i += 1
    return spans


def syntax_ok(text: str) -> tuple[bool, str]:
    """把整份 HTML 里的 script 抽出来交给 Node 校验。

    不校验整个 HTML（那是文档不是脚本），只校验脚本部分 ——
    这里出的错都是脚本层面的。
    """
    node = shutil.which("node")
    if not node:
        return True, "未安装 Node，跳过校验"
    parts = re.findall(r"<script\b[^>]*>(.*?)</script>", text, re.S)
    src = "\n;\n".join(parts)
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                     encoding="utf-8", newline="\n") as fh:
        fh.write(src)
        path = fh.name
    try:
        r = subprocess.run([node, "--check", path], capture_output=True,
                           text=True, encoding="utf-8", errors="replace",
                           timeout=120)
        return (r.returncode == 0), (r.stderr or r.stdout or "")[:300]
    finally:
        try:
            Path(path).unlink()
        except OSError:
            pass


def main() -> int:
    if not REPORT.is_file():
        print("  先跑 plan_js_replace.py 生成替换点报告")
        return 1
    rows = json.loads(REPORT.read_text(encoding="utf-8"))
    text = INDEX.read_text(encoding="utf-8")
    spans = comment_spans(text)
    print(f"  替换点 {len(rows)} 个，注释区间 {len(spans)} 处")

    # 用报告里存的**精确位置**，不做搜索。
    #
    # 早先靠"从上次位置往后搜同一个字面量"，问题有两个：
    # 同一行里出现两次时会跳过第二个；行号会随替换漂移。
    # 报告里已经有 abs_pos 和 lit_len，直接用就行。
    targets = []
    skipped_cmt = skipped_cmp = 0
    for r in rows:
        pos = r.get("pos")
        ln = r.get("lit_len")
        if not isinstance(pos, int) or not isinstance(ln, int):
            continue
        lit = text[pos:pos + ln]
        if lit.strip("'\"`") != r["zh"]:
            continue                       # 位置对不上（文件已变），跳过
        before = text[max(0, pos - 30):pos]
        if any(s in before for s in SKIP_BEFORE):
            skipped_cmp += 1
            continue
        if any(a <= pos < b for a, b in spans):
            skipped_cmt += 1
            continue
        targets.append((pos, ln, r["zh"]))

    print(f"  实际替换 {len(targets)} 处"
          f"（排除注释 {skipped_cmt}、比较值 {skipped_cmp}）")
    if not targets:
        print("  没有可替换的")
        return 0

    # 从后往前替换，位置不失效
    out = text
    for pos, ln, zh in sorted(targets, key=lambda x: -x[0]):
        lit = out[pos:pos + ln]
        out = out[:pos] + f"t({lit})" + out[pos + ln:]

    ok, why = syntax_ok(out)
    if not ok:
        print("  ！替换后脚本语法不合法，已放弃写入（原文件未改动）")
        print(f"    {why}")
        return 1
    if why:
        print(f"  （{why}）")

    shutil.copy2(INDEX, INDEX.with_suffix(".html.prejs"))
    INDEX.write_text(out, encoding="utf-8", newline="\n")
    print(f"  已写入 {INDEX}")
    print(f"  替换前备份 {INDEX.with_suffix('.html.prejs')}")

    # 复查标签平衡
    bad = [t for t in ("div", "span", "button", "h3", "p", "section")
           if len(re.findall(rf"<{t}[\s>]", out))
           != len(re.findall(rf"</{t}>", out))]
    print(f"  标签平衡: {bad or '正常'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
