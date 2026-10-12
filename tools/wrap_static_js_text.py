"""把 JS 模板里剩余的静态界面文字包上 t()。

与之前失败的那版的关键区别：**只处理不含 `{}` 的片段**。

为什么这条规则能避坑：
* 模板表达式一律形如 `${…}`，含大括号 → 被排除，
  不会出现"把 `${pend}` 当成文本包进 t('…')"那种语法错误。
* 嵌套模板（`${cond?`a${x}`:'b'}`）也含大括号 → 同样被排除。
  实测两轮失败都是踩在这两类上。
* 代价是"同一句里既有文字又有变量"的情况只能整句跳过，
  但那类本来就该人工处理（拆句或改用 {n} 占位符）。

安全性：改完跑 Node 语法校验 + 标签平衡检查，任一不过就整体放弃。
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
Q = "'"
BT = chr(96)
D = "$" + "{"

CJK = re.compile(r"[\u4e00-\u9fff]")
# 数据类与专名：不翻译
SKIP_TEXT = (
    "dlc_load.json", "launcher-v2.sqlite", "logkb.py", "Python 3",
    "Paradox Interactive", "steamapps", "ugc:", "l_simp", "l_english",
    "熵光矩阵", "群星知识库", "星系热症天灾宝典", "Steam ",
    "mod ", ".mod", ".xlsx", ".json", ".sqlite",
)

# 只在"看起来像界面文字"的上下文里动手：紧跟在 HTML 标签之后，
# 或者整段就是一句提示。这样避开代码与注释。
ANCHORS = (
    r"<h1>", r"<h2>", r"<h3[^>]*>", r"<p[^>]*>", r"<div[^>]*>",
    r"<span[^>]*>", r"<option[^>]*>", r"<b>", r"<li>", r"<small>",
)


def inline_ok(text: str) -> tuple[bool, str]:
    parts = re.findall(r"<script\b[^>]*>(.*?)</script>", text, re.S)
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                     encoding="utf-8", newline="\n") as fh:
        fh.write("\n;\n".join(parts))
        p = fh.name
    try:
        r = subprocess.run(["node", "--check", p], capture_output=True,
                           text=True, encoding="utf-8", errors="replace",
                           timeout=120)
        return (r.returncode == 0), (r.stderr or "")[:300]
    finally:
        Path(p).unlink(missing_ok=True)


def main() -> int:
    html = INDEX.read_text(encoding="utf-8")
    spans = [(m.start(1), m.end(1))
             for m in re.finditer(r"<script\b[^>]*>(.*?)</script>", html, re.S)]

    def in_script(pos: int) -> bool:
        return any(a <= pos < b for a, b in spans)

    def in_template(pos: int) -> bool:
        """pos 是否在**反引号模板字符串**内（按行判断）。

        必须区分模板与普通字符串：模板里可以写 `${t('…')}`，
        单引号字符串里不行 —— 后者要先改外层引号，那是另一类改动
        （`wrap_mod_tags.py` 负责，它有专门的成对替换逻辑）。
        这里只做模板，风险最小。
        按行判断的原因：嵌套模板会让"全文件数反引号"失准。
        """
        line_start = html.rfind("\n", 0, pos) + 1
        return html.count(BT, line_start, pos) % 2 == 1

    # 收集候选：HTML 标签之后紧跟的、不含 {} 的中文片段
    #
    # 模式匹配到**行尾**为止（用 `[^\n]*` 而不是要求以引号结束）。
    # 原因：模板里的文本常常"后面直接换行"，例如
    #     `<h3>筛选与搜索\n    <span class="sub">…`
    # 要求以引号收尾会漏掉这类。而多行文本会在换行处自然停止 ——
    # 正是我们要的（跨行的片段本来就该跳过，见下面 "\n" in text 的判断）。
    cands: list[tuple[int, str]] = []
    for anc in ANCHORS:
        for m in re.finditer(anc + r"([^\n]{2,120})", html):
            raw = m.group(1)
            # 在行尾的引号/反引号之前截断
            mm = re.match(r"[^<>{}" + BT + Q + r'"]{2,120}', raw)
            if not mm:
                continue
            text = mm.group(0)
            pos = m.start(1)
            if not CJK.search(text):
                continue
            if not in_script(pos):
                continue               # 只改脚本里的模板
            if not in_template(pos):
                # 单引号/双引号字符串里的 `>文字` 很常见（如
                # `'<div>浏览器预览模式…'`），在那种上下文里写 ${…}
                # 是语法错误。这类交给 wrap_mod_tags.py 处理。
                continue
            t = text.strip()
            if len(t) < 2 or any(s in t for s in SKIP_TEXT):
                continue
            if "\n" in text:
                continue
            # 已包 t()？向前看
            before = html[max(0, pos - 6):pos]
            if before.endswith(f"t({Q}") or before.endswith(f"{D}t({Q}"):
                continue
            cands.append((pos, t))

    # 去重（同一文本只包一次，避免改到重复片段）
    seen: set[str] = set()
    uniq: list[tuple[int, str]] = []
    for pos, text in cands:
        if text in seen:
            continue
        seen.add(text)
        uniq.append((pos, text))
    print(f"  候选 {len(uniq)} 处")
    for _, t in uniq[:12]:
        print(f"    {t[:52]!r}")

    out = html
    n = 0
    for pos, text in sorted(uniq, key=lambda x: -x[0]):
        if out[pos:pos + len(text)] != text:
            continue
        out = out[:pos] + D + "t(" + Q + text + Q + ")}" + out[pos + len(text):]
        n += 1
    print(f"  替换 {n} 处")

    ok, why = inline_ok(out)
    if not ok:
        print(f"  ！语法不合法，已放弃\n    {why}")
        return 1
    bad = [t for t in ("div", "span", "button", "h3", "p", "section", "b")
           if len(re.findall(rf"<{t}[\s>]", out))
           != len(re.findall(rf"</{t}>", out))]
    if bad:
        print(f"  ！标签不平衡 {bad}，已放弃")
        return 1
    print("  语法 OK / 标签平衡 OK")
    INDEX.write_text(out, encoding="utf-8", newline="\n")
    print(f"  已写入 {INDEX}")

    # 缺翻译的列出来
    d = json.loads(TRANS.read_text(encoding="utf-8")) if TRANS.is_file() else {}
    miss = [t for _, t in uniq if t not in d]
    print(f"  缺英文翻译: {len(miss)} 条")
    Path(ROOT / "tools" / "_need_en.json").write_text(
        json.dumps(miss, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
