r"""生成 GitHub 社交预览图（Social preview）1280×640。

设计要点
--------
社交卡片在信息流里会被缩到**约 400px 宽**（微信/QQ/Twitter 卡片），
所以：

* **字要少、要大**。缩到 400px 时，正文小于约 24px 就糊成一团 ——
  这张图上最小的字是 30px（设计尺寸），缩完仍有约 9.4px，
  勉强可读；再小就完全没意义。
* **不放界面截图**。截图里的界面细节在 400px 下全是马赛克。
  卡片的目的是**让人点进来**，不是让人读功能。
* **四周留 72px 安全边**。部分平台会对卡片做轻微裁切，
  贴边的字会被切掉。
* **视觉锚点用软件图标**（深空圆环）而不是截图 —— 它本身就是
  为小尺寸设计的，缩到 96px 仍然清晰。

配色沿用软件内置的 stellaris 主题，保证和界面、宣传片是同一套语言。

用法::

    python tools/make_social.py

输出 ``docs/social-preview.png``，上传位置：
仓库 → Settings → General → Social preview → Edit
"""
from __future__ import annotations

import io
import math
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parent.parent
ICON = ROOT / "smd" / "web" / "icon.png"
OUT = ROOT / "docs" / "social-preview.png"
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                              errors="replace")

W, H = 1280, 640
PAD = 72                       # 安全边

BG = (5, 8, 15)
ACC = (56, 189, 248)           # 青
ACC2 = (167, 139, 250)         # 紫
FG = (230, 237, 247)
DIM = (159, 176, 204)
OK = (52, 211, 153)


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    """找中文字体。列全候选是因为 Pillow 自带位图字体**不含汉字**。"""
    names = (["msyhbd.ttc", "msyh.ttc"] if bold else ["msyh.ttc", "msyhl.ttc"])
    for n in names:
        p = Path(r"C:\Windows\Fonts") / n
        if p.is_file():
            try:
                return ImageFont.truetype(str(p), size)
            except OSError:
                continue
    for n in ("Deng.ttf", "simhei.ttf", "simsun.ttc"):
        p = Path(r"C:\Windows\Fonts") / n
        if p.is_file():
            try:
                return ImageFont.truetype(str(p), size)
            except OSError:
                continue
    return ImageFont.load_default()


def backdrop() -> Image.Image:
    """深空背景：底色 + 星点 + 左侧偏紫、右侧偏青的双色辉光。

    双色辉光是刻意做的：左紫右青正好呼应软件主题的两个强调色，
    让纯色背景不至于死板。
    """
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img, "RGBA")
    seed = 20261012
    for _ in range(240):
        seed = (seed * 1103515245 + 12345) & 0x7FFFFFFF
        x = seed % W
        seed = (seed * 1103515245 + 12345) & 0x7FFFFFFF
        y = seed % H
        seed = (seed * 1103515245 + 12345) & 0x7FFFFFFF
        b = 55 + seed % 140
        r = 1 if seed % 6 else 2
        d.ellipse([x, y, x + r, y + r], fill=(b, b, min(255, b + 30), 190))

    glow = Image.new("RGB", (W, H), BG)
    gd = ImageDraw.Draw(glow)
    gd.ellipse([-260, -180, 460, 540], fill=(30, 20, 58))     # 左：紫
    gd.ellipse([W - 420, H - 460, W + 200, H + 160],
               fill=(10, 38, 62))                             # 右：青
    glow = glow.filter(ImageFilter.GaussianBlur(150))
    return Image.blend(img, glow, 0.62)


def glow_ring(img: Image.Image, cx: int, cy: int, r: int,
              color: tuple[int, int, int], alpha: int = 80) -> None:
    """叠一圈柔光，把图标从背景里"托"起来。"""
    layer = Image.new("RGB", (W, H), (0, 0, 0))
    ImageDraw.Draw(layer).ellipse([cx - r, cy - r, cx + r, cy + r],
                                  fill=color)
    layer = layer.filter(ImageFilter.GaussianBlur(r // 2))
    img.paste(Image.blend(img, layer, alpha / 255.0), (0, 0))


def main() -> int:
    if not ICON.is_file():
        print(f"  找不到图标：{ICON}")
        return 1
    OUT.parent.mkdir(parents=True, exist_ok=True)

    img = backdrop()
    d = ImageDraw.Draw(img, "RGBA")

    # ---------- 右侧：软件图标 ----------
    # 尺寸 240（第一版做了 320，结果挤压文字区、缩到 400px 时正文糊了）。
    # 卡片的目的是**让人点进来**，所以文字清晰 > 图标气派。
    ir = 240
    icx, icy = W - PAD - ir // 2, H // 2
    glow_ring(img, icx, icy, int(ir * 0.82), ACC2, 95)
    glow_ring(img, icx, icy, int(ir * 0.55), ACC, 60)
    icon = Image.open(ICON).convert("RGBA").resize((ir, ir), Image.LANCZOS)
    img.paste(icon, (icx - ir // 2, icy - ir // 2), icon)

    # ---------- 左侧：文字 ----------
    # 字号全部按"缩到 400px 后仍可读"反推：400/1280 = 0.3125，
    # 所以设计稿上 38px 的字缩完是 11.9px —— 这是可读下限。
    x = PAD
    y = 122

    f_title = font(100, bold=True)
    d.text((x, y), "熵光矩阵", font=f_title, fill=FG)
    y += 122

    f_en = font(26)
    d.text((x, y), "ENTROPY  LIGHT  MATRIX", font=f_en, fill=ACC)
    y += 56

    # 标语砍到 16 字。原句 19 字已经贴近右边距，再加字号就要撞图标了。
    f_tag = font(40)
    d.text((x, y), "《群星》模组冲突诊断与排序", font=f_tag, fill=DIM)
    y += 84

    d.rectangle([x, y, x + 104, y + 4], fill=ACC2)
    y += 38

    f_pt = font(34)
    for txt, col in (("6 类冲突检测", ACC),
                     ("一键最优排序", ACC2),
                     ("崩溃日志中文化", OK)):
        d.ellipse([x, y + 14, x + 10, y + 24], fill=col)
        d.text((x + 28, y), txt, font=f_pt, fill=FG)
        y += 50

    # ---------- 右下角：语言标识 ----------
    f_sm = font(22)
    label = "Python · Windows · MIT"
    box = d.textbbox((0, 0), label, font=f_sm)
    d.text((W - PAD - (box[2] - box[0]), H - PAD + 8), label,
           font=f_sm, fill=(108, 124, 148))

    img.save(OUT)
    print(f"  完成: {OUT}")
    print(f"  尺寸: {img.width}×{img.height}  "
          f"（GitHub 要求 1280×640，{OUT.stat().st_size / 1024:.0f} KB）")
    print("\n  缩到 400px 宽时的实际字号（<11px 基本读不清）：")
    k = 400 / W
    for name, sz in (("主标题", 100), ("英文名", 26), ("标语", 40),
                     ("要点", 34), ("角标", 22)):
        px = sz * k
        flag = "可读" if px >= 11 else ("勉强" if px >= 9 else "太小")
        print(f"    {name}  {sz}px → {px:.1f}px   {flag}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
