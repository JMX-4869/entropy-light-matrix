"""量现有主题的对比度 —— 看 AI 生成的主题到底能不能看清字。

WCAG 相对亮度：
    L = 0.2126R + 0.7152G + 0.0722B   （R/G/B 先做 sRGB 线性化）
对比度：
    (L_亮 + 0.05) / (L_暗 + 0.05)

判据（WCAG AA）：
    正文      ≥ 4.5
    次要文字  ≥ 3.0
    图形/边框 ≥ 3.0
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
out = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")


def rel_lum(hex_color: str) -> float:
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))

    def lin(c: float) -> float:
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)


def contrast(a: str, b: str) -> float:
    la, lb = rel_lum(a), rel_lum(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


# 关键配对：(前景令牌, 背景令牌, 最低对比度, 说明)
PAIRS = [
    ("fg", "bg", 4.5, "正文"),
    ("fg", "panel", 4.5, "正文在面板上"),
    ("dim", "bg", 3.0, "次要文字"),
    ("dim2", "bg", 3.0, "更弱的提示"),
    ("dim", "panel", 3.0, "次要文字在面板上"),
    ("acctext", "acc", 4.5, "按钮文字"),
    ("ok", "bg", 3.0, "状态色"),
    ("warn", "bg", 3.0, "状态色"),
    ("crit", "bg", 3.0, "状态色"),
    ("info", "bg", 3.0, "状态色"),
    ("acc", "bg", 3.0, "强调色"),
    ("line2", "panel", 1.3, "分隔线（能看出即可）"),
]


def main() -> int:
    from smd import theme as T

    themes = T.list_themes() or []
    builtin = [t for t in themes if t.get("builtin")]
    user = [t for t in themes if not t.get("builtin")]

    print(f"  内置主题 {len(builtin)} 个，自定义 {len(user)} 个\n", file=out)

    for group, items in (("内置", builtin), ("自定义", user)):
        for meta in items:
            tid = meta.get("id") or ""
            try:
                th = T.get_theme(tid)
            except Exception as exc:                  # noqa: BLE001
                print(f"  {tid}: 读取失败 {exc}", file=out)
                continue
            toks = th.get("tokens") or th
            scheme = str(toks.get("scheme") or "?")
            print(f"  [{group}] {meta.get('name') or tid}  (scheme={scheme})",
                  file=out)
            bad = 0
            for fg_k, bg_k, need, label in PAIRS:
                fg, bg = toks.get(fg_k), toks.get(bg_k)
                if not isinstance(fg, str) or not isinstance(bg, str):
                    continue
                try:
                    c = contrast(fg, bg)
                except Exception:                     # noqa: BLE001
                    continue
                flag = "OK " if c >= need else "低!"
                if c < need:
                    bad += 1
                print(f"      {flag} {label:<16} {fg_k}/{bg_k:<7} "
                      f"{c:5.2f} (需 {need})", file=out)
            if bad:
                print(f"      → {bad} 项不达标", file=out)
            print("", file=out)
    out.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
