"""把模组列表里硬编码的状态标签包上 t()，让它们跟随语言。

这些标签每个模组都会显示（74 个模组 × 若干标签），是英文界面里
残留中文最多的地方 —— 之前一件也没翻译。

实现要求（踩过坑）：
* 这些标签在 `tags.push('…')` 里，是**单引号字符串**，不能直接写
  `${t('…')}`（单引号字符串不做插值）。必须把外层的单引号换成反引号。
* 换的时候**两头都要换**。只换开头会得到
  `` `<span>${t('x')}</span>' `` —— 引号不配对，整个脚本语法错误。
  第一版就是这么错的，靠 Node 校验才发现。
* 改完必须跑语法校验。这个文件的脚本一旦语法错，界面直接白屏。

用法: python tools/wrap_mod_tags.py [--check]
"""
from __future__ import annotations

import argparse
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
BT = chr(96)          # 反引号
Q = "'"               # 单引号

# 逐个精确配对：(原文, 替换后)
# 左边是完整的 JS 实参（含外层引号），右边把外层换成反引号、内层包 t()
PAIRS = [
    (f"{Q}<span class=\"pill p-acc\">汉化</span>{Q}",
     BT + f"<span class=\"pill p-acc\">${{t({Q}汉化{Q})}}</span>" + BT),
    (f"{Q}<span class=\"pill p-ok\">修复</span>{Q}",
     BT + f"<span class=\"pill p-ok\">${{t({Q}修复{Q})}}</span>" + BT),
    (f"{Q}<span class=\"pill p-low\">前置</span>{Q}",
     BT + f"<span class=\"pill p-low\">${{t({Q}前置{Q})}}</span>" + BT),
    (f"{Q}<span class=\"pill p-crit\">缺文件</span>{Q}",
     BT + f"<span class=\"pill p-crit\">${{t({Q}缺文件{Q})}}</span>" + BT),
    (f"{Q}<span class=\"pill p-high\">有问题</span>{Q}",
     BT + f"<span class=\"pill p-high\">${{t({Q}有问题{Q})}}</span>" + BT),
    (f"{Q}<span class=\"pill p-info\">未启用</span>{Q}",
     BT + f"<span class=\"pill p-info\">${{t({Q}未启用{Q})}}</span>" + BT),
    # `启用` 在别处也出现在 enabled: 的映射里，用更长的上下文区分
    (f"{Q}<span class=\"pill p-ok\">启用</span>{Q}",
     BT + f"<span class=\"pill p-ok\">${{t({Q}启用{Q})}}</span>" + BT),
    # 标题提示
    # 标题提示：这两个出现在 `${on?'…':'…'}` 的**三元表达式里**，
    # 本身已经是模板插值上下文，不能再套 `${{ }}` —— 会变成
    # `${on?${t(…)}:t(${t(…)}))}`，语法错误。只把字面量包 t() 即可。
    (f"title=\"${{on?{Q}点击关闭{Q}:t({Q}点击启用{Q})}}\"",
     f"title=\"${{on?t({Q}点击关闭{Q}):t({Q}点击启用{Q})}}\""),
    (f"{Q}磁盘上没有内容，无法启用{Q}",
     f"t({Q}磁盘上没有内容，无法启用{Q})"),
    # 注意：**不要**写宽泛的 (Q + "未找到" + Q) —— 那份字面量在
    # `t('未找到')` 里已经出现过，会被二次包裹成 `t('t('未找到')')`。
    # 上一版就是这么错的，靠语法校验拦下了。
]


def inline_ok(text: str) -> tuple[bool, str]:
    """把内联脚本抽出来交给 Node 校验语法。"""
    parts = re.findall(r"<script\b[^>]*>(.*?)</script>", text, re.S)
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                     encoding="utf-8", newline="\n") as fh:
        fh.write("\n;\n".join(parts))
        path = fh.name
    try:
        r = subprocess.run(["node", "--check", path], capture_output=True,
                           text=True, encoding="utf-8", errors="replace",
                           timeout=120)
        return (r.returncode == 0), (r.stderr or r.stdout or "")[:300]
    finally:
        try:
            Path(path).unlink()
        except OSError:
            pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()

    src = INDEX.read_text(encoding="utf-8")
    ok, why = inline_ok(src)
    print(f"  改动前语法: {'OK' if ok else 'FAIL'}")
    if not ok:
        print(f"    起点就不合法，先修好再说：{why[:160]}")
        return 1

    out = src
    for a, b in PAIRS:
        n = out.count(a)
        if n:
            out = out.replace(a, b)
        print(f"    {'替换' if n else '未命中'} {n} 处: {a[:46]}")

    ok2, why2 = inline_ok(out)
    print(f"  改动后语法: {'OK' if ok2 else 'FAIL'}")
    if not ok2:
        print(f"    {why2}")
        print("  已放弃写入")
        return 1

    # 标签平衡
    bad = [t for t in ("div", "span", "button", "h3", "p", "section", "b")
           if len(re.findall(rf"<{t}[\s>]", out))
           != len(re.findall(rf"</{t}>", out))]
    print(f"  标签平衡: {bad or '正常'}")
    if bad:
        print("  标签不平衡，已放弃写入")
        return 1

    if args.check:
        print("  （--check，未写入）")
        return 0
    INDEX.write_text(out, encoding="utf-8", newline="\n")
    print(f"  已写入 {INDEX}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
