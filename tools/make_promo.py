"""熵光矩阵 · 宣传片帧生成器（2560×1440 超采样 → 1080p）。

设计说明
--------
**为什么不用 AI 生成"软件界面"**：生成模型画的界面全是乱码假字，
懂行的人一眼看穿。这软件最大的卖点就是真实界面本身够漂亮，
所以素材全部用**真实截图**，AI 只负责它擅长的部分。

**运动从哪来**：静态截图拼视频最容易做成"PPT 轮播"，很廉价。
这里给每张截图加**缓慢推拉 + 平移**（Ken Burns），
再叠一层会呼吸的光晕，画面就活了。

本轮（v2）四项优化
------------------
1. **超采样**：渲 2560×1440 再降到 1920×1080。所有文字和细线边缘
   会明显更锐利 —— 直接渲 1080p 的话，字号小的地方会发虚。
2. **场景交叉淡入淡出**：v1 是硬切，观感廉价。现在相邻场景有
   0.5 秒叠化。
3. **按素材比例分别裁切**：`92_ai_takeover` / `90_crash_ai` 是
   1384×911（1.52），其余是 1384×861（1.61）。v1 统一按 16:10 裁，
   对前两张会切掉底部；现在对窄图改**顶部优先**，把标题区保住。
4. **截图轻微锐化**：降采样后细节会变软，补一点 unsharp 找回来。

用法::

    python tools/make_promo.py            # 生成全部帧到 promo/frames/
    python tools/make_promo.py --quick    # 只渲每个场景首/中/末帧，快速看排版
"""
from __future__ import annotations

import argparse
import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parent.parent
SHOTS = ROOT / "tools" / "shots"
OUT = ROOT / "promo"
FRAMES = OUT / "frames"

# 输出规格
W, H = 1920, 1080
FPS = 30
# 超采样倍率：渲得更大再缩小，文字边缘会明显更锐利。
# 1.33 倍（2560×1440）是性价比拐点——渲染时间可控，输出 1080p 时
# 文字已经是"过分清晰"了。再往上肉眼看不出来，时间却线性增长。
SS = 4 / 3
RW, RH = int(W * SS), int(H * SS)

# 主题色取自软件自身的 stellaris 主题，保证片子和软件是同一套视觉
BG = (5, 8, 15)
ACC = (56, 189, 248)
ACC2 = (167, 139, 250)
FG = (230, 237, 247)
DIM = (159, 176, 204)
WARN = (251, 191, 36)
CRIT = (248, 113, 113)
OK = (52, 211, 153)

XFADE = 0.5          # 场景交叉淡入淡出秒数


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    """找一个能显示中文的字体（按超采样比例放大字号）。

    Windows 自带微软雅黑，一定有；列全候选是因为**Pillow 自带的默认
    位图字体不含汉字**，找不到中文会显示成方块。
    """
    size = int(size * SS)
    names = (["msyhbd.ttc", "msyh.ttc"] if bold else ["msyh.ttc", "msyhl.ttc"])
    for n in names:
        for base in (r"C:\Windows\Fonts",):
            p = Path(base) / n
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


def s(v: float) -> int:
    """把设计尺寸换算成超采样尺寸。"""
    return int(round(v * SS))


def ease(t: float) -> float:
    """平滑缓动（smoothstep），让运动不生硬。"""
    t = max(0.0, min(1.0, t))
    return t * t * (3 - 2 * t)


_BACKDROP: Image.Image | None = None
_GLOW_CACHE: dict = {}


def backdrop() -> Image.Image:
    """深空背景：底色 + 星点 + 中心辉光（结果缓存）。

    星点是几百次椭圆绘制，辉光还要一次大半径高斯模糊 ——
    每帧重算的话光这一个函数就要几百毫秒。背景是静态的，算一次就够。
    """
    global _BACKDROP
    if _BACKDROP is not None:
        return _BACKDROP.copy()
    img = Image.new("RGB", (RW, RH), BG)
    d = ImageDraw.Draw(img, "RGBA")
    # 确定性伪随机，保证每次生成的星星位置一致（便于逐帧比对）
    seed = 20260110
    for _ in range(int(340 * SS * SS)):
        seed = (seed * 1103515245 + 12345) & 0x7FFFFFFF
        x = seed % RW
        seed = (seed * 1103515245 + 12345) & 0x7FFFFFFF
        y = seed % RH
        seed = (seed * 1103515245 + 12345) & 0x7FFFFFFF
        b = 60 + seed % 150
        r = s(1) if seed % 7 else s(2)
        d.ellipse([x, y, x + r, y + r], fill=(b, b, min(255, b + 30), 200))
    glow = Image.new("RGB", (RW, RH), BG)
    gd = ImageDraw.Draw(glow)
    gd.ellipse([RW // 2 - s(700), RH // 2 - s(420),
                RW // 2 + s(700), RH // 2 + s(420)], fill=(14, 26, 46))
    glow = glow.filter(ImageFilter.GaussianBlur(s(180)))
    _BACKDROP = Image.blend(img, glow, 0.55)
    return _BACKDROP.copy()


def glow_ring(img: Image.Image, cx: int, cy: int, r: int,
              color: tuple[int, int, int], alpha: int = 90) -> None:
    """在指定位置叠一圈柔光。

    按 (位置, 半径档位, 颜色) 缓存模糊结果：呼吸动画只在小范围改半径，
    量化到一定档位就够平滑，却能把上千次高斯模糊降到几十次。
    """
    cx, cy, r = s(cx), s(cy), s(r)
    key = (cx // 48, cy // 48, r // 24, color, alpha)
    layer = _GLOW_CACHE.get(key)
    if layer is None:
        layer = Image.new("RGB", (RW, RH), (0, 0, 0))
        ImageDraw.Draw(layer).ellipse([cx - r, cy - r, cx + r, cy + r],
                                      fill=color)
        layer = layer.filter(ImageFilter.GaussianBlur(max(s(8), r // 2)))
        if len(_GLOW_CACHE) < 64:
            _GLOW_CACHE[key] = layer
    img.paste(Image.blend(img, layer, alpha / 255.0), (0, 0))


def ken_burns(src: Path, t: float, *, zoom: float = 0.09,
              aspect: float | None = None, top_bias: float = 0.30
              ) -> Image.Image:
    """把截图按进度 t 做缓慢推近 + 平移，返回一帧。

    ``aspect`` 是目标宽高比；``top_bias`` 是垂直焦点位置
    （0 = 顶部对齐，0.5 = 居中）。

    为什么要有 ``top_bias``：这批截图比例不统一 ——
    多数是 1384×861（1.607），但 AI 接管和崩溃报告是 1384×911（1.52）。
    统一按 16:10 裁时，窄图会被切掉更多。这两张的**标题栏在最上面**，
    所以对它们用顶部优先（0.0），保证标题不被切。
    """
    im = Image.open(src).convert("RGB")
    if aspect is None:
        tw, th = float(RW), float(RH)
    else:
        tw, th = 1000.0, 1000.0 / aspect
    scale = max(tw / im.width, th / im.height)
    nw, nh = int(im.width * scale + 0.5), int(im.height * scale + 0.5)
    im = im.resize((nw, nh), Image.LANCZOS)

    k = 1.0 + zoom * ease(t)
    cw = min(nw, max(1, int(nw / k)))
    ch = min(nh, max(1, int(nh / k)))
    max_x, max_y = max(0, nw - cw), max(0, nh - ch)
    cx = int(max_x * (0.15 + 0.7 * ease(t)))
    cy = int(max_y * (top_bias + 0.22 * ease(t)))
    cx = max(0, min(max_x, cx))
    cy = max(0, min(max_y, cy))
    out = im.crop((cx, cy, cx + cw, cy + ch))
    # 降采样会损失细节，补一点锐化找回来
    return out.filter(ImageFilter.UnsharpMask(radius=2, percent=58,
                                              threshold=3))


def scene_title(t: float) -> Image.Image:
    """① 开场：品牌 + 一句话说清它是什么。"""
    img = backdrop()
    a = ease(min(1.0, t / 0.35))
    glow_ring(img, W // 2, 430, int(230 + 40 * math.sin(t * 6)), ACC, 70)
    d = ImageDraw.Draw(img, "RGBA")

    f_logo = font(96, bold=True)
    f_sub = font(40)
    f_tag = font(30)

    y = s(360)
    txt = "熵光矩阵"
    box = d.textbbox((0, 0), txt, font=f_logo)
    d.text(((RW - (box[2] - box[0])) // 2, y), txt, font=f_logo,
           fill=(int(FG[0] * a), int(FG[1] * a), int(FG[2] * a)))
    y += box[3] - box[1] + s(18)
    txt = "ENTROPY  LIGHT  MATRIX"
    box = d.textbbox((0, 0), txt, font=f_sub)
    d.text(((RW - (box[2] - box[0])) // 2, y), txt, font=f_sub,
           fill=(int(ACC[0] * a), int(ACC[1] * a), int(ACC[2] * a)))
    y += s(96)
    txt = "《群星》模组冲突诊断 · 智能排序 · 崩溃翻译"
    box = d.textbbox((0, 0), txt, font=f_tag)
    d.text(((RW - (box[2] - box[0])) // 2, y), txt, font=f_tag,
           fill=(int(DIM[0] * a), int(DIM[1] * a), int(DIM[2] * a)))
    return img


def scene_problem(t: float) -> Image.Image:
    """② 痛点：94 个模组，你不知道谁和谁打架。"""
    img = backdrop()
    glow_ring(img, W // 2, 400, 260, CRIT, 60)
    d = ImageDraw.Draw(img, "RGBA")
    f_big = font(150, bold=True)
    f_mid = font(52)
    f_sm = font(34)

    n = int(94 * ease(min(1.0, t / 0.5)))
    txt = str(n)
    box = d.textbbox((0, 0), txt, font=f_big)
    d.text(((RW - (box[2] - box[0])) // 2, s(300)), txt, font=f_big, fill=CRIT)
    txt = "个已启用模组"
    box = d.textbbox((0, 0), txt, font=f_mid)
    d.text(((RW - (box[2] - box[0])) // 2, s(480)), txt, font=f_mid, fill=FG)

    if t > 0.45:
        a = ease((t - 0.45) / 0.3)
        txt = "你根本不知道谁和谁打架"
        box = d.textbbox((0, 0), txt, font=f_mid)
        d.text(((RW - (box[2] - box[0])) // 2, s(600)), txt, font=f_mid,
               fill=(int(FG[0] * a), int(FG[1] * a), int(FG[2] * a)))
        txt = "文件互相覆盖 · 脚本对象重复定义 · 汉化键撞车 · 版本不匹配"
        box = d.textbbox((0, 0), txt, font=f_sm)
        d.text(((RW - (box[2] - box[0])) // 2, s(690)), txt, font=f_sm,
               fill=(int(DIM[0] * a), int(DIM[1] * a), int(DIM[2] * a)))
    return img


# 这两张截图比例与其它不同（1384×911 vs 861），裁切要顶部优先保住标题栏
_TOP_BIAS = {"92_ai_takeover.png": 0.0, "90_crash_ai.png": 0.0,
             "91_patch_ai_issues.png": 0.0}


def scene_shot(t: float, src: str, title: str, bullets: list[str]
               ) -> Image.Image:
    """③~⑧ 通用：真实界面截图 + 标题 + 要点。

    排版要点（都是试出来的）：

    * 文字放截图**旁边**而不是压上去 —— 压上去必然挡住界面细节，
      而界面正是要展示的东西
    * 截图按 16:10 裁一刀再放大。原始截图直接铺会缩得很小、界面看不清；
      裁掉暗色留白后主体几乎大一倍
    * 截图带圆角 + 描边 + 背后外发光，像"贴在深空里的一块屏幕"
    """
    shot_w, shot_h = s(1180), s(738)
    img = Image.new("RGB", (RW, RH), BG)

    crop = ken_burns(SHOTS / src, t, zoom=0.07, aspect=16 / 10,
                     top_bias=_TOP_BIAS.get(src, 0.30))
    crop = crop.resize((shot_w, shot_h), Image.LANCZOS)

    mask = Image.new("L", (shot_w, shot_h), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, shot_w - 1, shot_h - 1], radius=s(12), fill=255)

    x_img, y_img = s(60), (RH - shot_h) // 2
    halo = Image.new("RGB", (RW, RH), BG)
    ImageDraw.Draw(halo).rounded_rectangle(
        [x_img - s(18), y_img - s(18), x_img + shot_w + s(18),
         y_img + shot_h + s(18)], radius=s(26), fill=(22, 40, 68))
    halo = halo.filter(ImageFilter.GaussianBlur(s(46)))
    img.paste(Image.blend(img, halo, 0.85), (0, 0))
    img.paste(crop, (x_img, y_img), mask)

    d = ImageDraw.Draw(img, "RGBA")
    d.rounded_rectangle([x_img - s(1), y_img - s(1),
                         x_img + shot_w, y_img + shot_h],
                        radius=s(13), outline=(60, 92, 140, 190), width=s(2))

    x0 = x_img + shot_w + s(50)
    avail = RW - x0 - s(40)
    f_t, f_b = font(56, bold=True), font(28)
    fade_in = ease(min(1.0, t / 0.25))
    d.text((x0, s(318)), title, font=f_t,
           fill=(int(ACC[0] * fade_in), int(ACC[1] * fade_in),
                 int(ACC[2] * fade_in)))
    d.rectangle([x0, s(398), x0 + int(s(130) * fade_in), s(403)], fill=ACC2)

    y = s(452)
    for i, b in enumerate(bullets):
        start = 0.15 + i * 0.13
        if t < start:
            break
        a = ease((t - start) / 0.22)
        d.ellipse([x0, y + s(13), x0 + s(9), y + s(22)],
                  fill=(int(ACC[0] * a), int(ACC[1] * a), int(ACC[2] * a)))
        line, yy = "", y
        for chx in b:
            if d.textlength(line + chx, font=f_b) > avail - s(30):
                d.text((x0 + s(26), yy), line, font=f_b,
                       fill=(int(FG[0] * a), int(FG[1] * a), int(FG[2] * a)))
                line, yy = chx, yy + s(42)
            else:
                line += chx
        d.text((x0 + s(26), yy), line, font=f_b,
               fill=(int(FG[0] * a), int(FG[1] * a), int(FG[2] * a)))
        y = yy + s(42 + 20)
    return img


def scene_stats(t: float) -> Image.Image:
    """⑨ 数据背书：把最有说服力的数字一次摆出来。"""
    img = backdrop()
    d = ImageDraw.Draw(img, "RGBA")
    f_big, f_sm, f_h = font(80, bold=True), font(30), font(46, bold=True)

    txt = "它到底做了什么"
    box = d.textbbox((0, 0), txt, font=f_h)
    d.text(((RW - (box[2] - box[0])) // 2, s(150)), txt, font=f_h, fill=FG)

    items = [
        (int(510 * ease(min(1.0, t / 0.3))), "处冲突一次检出", ACC),
        (int(46715 * ease(min(1.0, t / 0.35))), "个文件深度索引", ACC2),
        (int(8689 * ease(min(1.0, t / 0.4))), "条内置知识库", OK),
        (int(735 * ease(min(1.0, t / 0.45))), "项自动化测试", WARN),
    ]
    bw, gap = s(400), s(30)
    total = len(items) * bw + (len(items) - 1) * gap
    x = (RW - total) // 2
    for val, label, color in items:
        d.rounded_rectangle([x, s(330), x + bw, s(620)], radius=s(18),
                            fill=(17, 24, 39, 210), outline=color, width=s(2))
        txt = str(val)
        box = d.textbbox((0, 0), txt, font=f_big)
        d.text((x + (bw - (box[2] - box[0])) // 2, s(380)), txt,
               font=f_big, fill=color)
        box = d.textbbox((0, 0), label, font=f_sm)
        d.text((x + (bw - (box[2] - box[0])) // 2, s(500)), label,
               font=f_sm, fill=DIM)
        x += bw + gap
    return img


def scene_outro(t: float) -> Image.Image:
    """⑩ 收尾：名称 + 最核心的承诺。"""
    img = backdrop()
    glow_ring(img, W // 2, 470, 300, ACC2, 70)
    d = ImageDraw.Draw(img, "RGBA")
    f_l, f_m, f_s = font(84, bold=True), font(40), font(30)
    a = ease(min(1.0, t / 0.4))

    y = s(330)
    for txt, f, col in (
        ("模组开关，永远由你决定", f_l,
         (int(ACC2[0] * a), int(ACC2[1] * a), int(ACC2[2] * a))),
        ("求解器只负责说清「为什么建议关」", f_m,
         (int(FG[0] * a), int(FG[1] * a), int(FG[2] * a))),
        ("AI 内容必须你显式授权，绝不擅自发送", f_m,
         (int(FG[0] * a), int(FG[1] * a), int(FG[2] * a))),
        ("熵光矩阵  ·  ENTROPY LIGHT MATRIX", f_s,
         (int(DIM[0] * a), int(DIM[1] * a), int(DIM[2] * a))),
    ):
        box = d.textbbox((0, 0), txt, font=f)
        d.text(((RW - (box[2] - box[0])) // 2, y), txt, font=f, fill=col)
        y += s(150 if f is f_l else (70 if f is f_m else 140))
    return img


SCENE_IMAGES = ["1_dashboard.png", "4_conflicts.png", "6_order.png",
                "92_ai_takeover.png", "42_patch_batch.png",
                "90_crash_ai.png", "80_archive.png"]

# (生成函数, 秒数)
SCENES = [
    (scene_title, 4.5),
    (scene_problem, 6.0),
    (lambda t: scene_shot(t, "1_dashboard.png", "一眼看清全局",
                          ["健康分、致命冲突、加载顺序调整项",
                           "自动探测用户目录与游戏本体",
                           "启动游戏前自动开启后台监控"]), 8.0),
    (lambda t: scene_shot(t, "4_conflicts.png", "6 类冲突，逐条查清",
                          ["文件覆盖 · 脚本对象重定义 · 本地化键冲突",
                           "版本不兼容 · 文件完整性 · 被大量覆盖",
                           "每条都能点开看「谁覆盖了谁」"]), 8.5),
    (lambda t: scene_shot(t, "6_order.png", "一键最优排序",
                          ["按内容分层求解，不动任何一个模组",
                           "只改先后顺序，风险最低",
                           "写回前自动备份，随时回滚"]), 7.5),
    (lambda t: scene_shot(t, "92_ai_takeover.png", "AI 接管",
                          ["深度解析 46715 个文件",
                           "含全部模组 + 原版数据 + DLC",
                           "先出方案，你确认后才写盘"]), 9.0),
    (lambda t: scene_shot(t, "42_patch_batch.png", "补丁工坊",
                          ["本地规则引擎：不联网、免费、快",
                           "AI 引擎：处理需要理解语义的冲突",
                           "所有补丁统一存成一个补丁模组"]), 8.0),
    (lambda t: scene_shot(t, "90_crash_ai.png", "崩溃也不用怕",
                          ["几万行英文日志自动归类、翻译、注释",
                           "AI 直接告诉你错误原因与修法",
                           "结论进补丁工坊，可勾选生成补丁"]), 8.5),
    (lambda t: scene_shot(t, "80_archive.png", "时之虫档案",
                          ["8689 条群星知识库，16 个分类",
                           "搜索即查，支持中文提问",
                           "命令一键送进游戏控制台"]), 7.5),
    (scene_stats, 5.5),
    (scene_outro, 5.5),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true",
                    help="只渲每场景首/中/末帧，用于快速检查排版")
    args = ap.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    FRAMES.mkdir(parents=True, exist_ok=True)

    missing = [x for x in SCENE_IMAGES if not (SHOTS / x).is_file()]
    if missing:
        print("  缺少素材截图：")
        for m in missing:
            print(f"    {SHOTS / m}")
        return 1

    if not args.quick:
        for p in FRAMES.glob("*.png"):
            p.unlink()
    _GLOW_CACHE.clear()

    idx = 0
    manifest = []
    for si, (fn, secs) in enumerate(SCENES):
        n = max(1, int(secs * FPS))
        manifest.append((si, idx, n))
        picks = ([0, n // 2, n - 1] if args.quick else range(n))
        for i in picks:
            t = i / max(1, n - 1)
            img = fn(t).resize((W, H), Image.LANCZOS)   # 超采样降下来
            if args.quick:
                img.save(OUT / f"check_{si:02d}_f{i:04d}.png")
            else:
                img.save(FRAMES / f"f{idx + i:05d}.png")
                if i in (0, n - 1):
                    img.save(OUT / f"scene{si:02d}_"
                                   f"{'first' if i == 0 else 'last'}.png")
        print(f"  场景 {si:2d}: {n:3d} 帧  (累计 {idx + n})")
        idx += n

    print(f"\n  合计 {idx} 帧 / {idx / FPS:.1f} 秒 @ {FPS}fps  "
          f"（渲染 {RW}×{RH} → 输出 {W}×{H}）")

    # 场景时长表：编码时要按它做交叉淡化
    (OUT / "manifest.txt").write_text(
        "\n".join(f"{s_}\t{st}\t{n}" for s_, st, n in manifest),
        encoding="utf-8")
    total_sec = idx / FPS
    print(f"  场景切换点（交叉淡化 {XFADE}s）：")
    at = 0.0
    for si, (_, secs) in enumerate(SCENES):
        print(f"    {at:6.2f}s  场景 {si:2d}  {secs:.1f}s")
        at += secs
    print(f"    {total_sec:6.2f}s  结束")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
