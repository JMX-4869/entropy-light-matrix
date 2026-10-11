"""熵光矩阵 · 宣传片帧生成器（1920×1080 · 约 60 秒 · 无配音）。

设计说明
--------
**为什么不用 AI 生成"软件界面"**：生成模型画的界面全是乱码假字，
懂行的人一眼看穿。这软件最大的卖点就是真实界面本身够漂亮，
所以素材全部用**真实截图**，AI 只负责它擅长的部分。

**运动从哪来**：静态截图拼视频最容易做成"PPT 轮播"，很廉价。
这里给每张截图加**缓慢推拉 + 平移**（Ken Burns），
再叠一层会呼吸的光晕，画面就活了。

**为什么只输出图片序列**：ffmpeg 负责编码，Pillow 负责合成。
分开的好处是帧可以单独检查——某一帧排版崩了能立刻看出来，
而不是等编码完再逐秒拖动找问题。

用法::

    python tools/make_promo.py            # 生成全部帧到 promo/frames/
    python tools/make_promo.py --sheet    # 另外拼一张缩略图便于快速检查
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parent.parent
SHOTS = ROOT / "tools" / "shots"
OUT = ROOT / "promo"
FRAMES = OUT / "frames"

# 注意：**不要**在这里替换 sys.stdout。
# 这个脚本是设计成能被 import 的（tools/_promo_check.py 就 import 它），
# 而 `sys.stdout = io.TextIOWrapper(sys.stdout.buffer, ...)` 会在原包装
# 被回收时把底层 buffer 一起关掉，调用方之后再 print 就炸
# "I/O operation on closed file"。要 UTF-8 输出请设环境变量
# PYTHONIOENCODING=utf-8（本项目的运行方式已经这么做了）。

W, H = 1920, 1080
FPS = 30

# 主题色取自软件自身的 stellaris 主题，保证片子和软件是同一套视觉
BG = (5, 8, 15)
ACC = (56, 189, 248)        # 主强调色（青）
ACC2 = (167, 139, 250)      # 次强调色（紫）
FG = (230, 237, 247)
DIM = (159, 176, 204)
WARN = (251, 191, 36)
CRIT = (248, 113, 113)
OK = (52, 211, 153)


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    """找一个能显示中文的字体。

    Windows 自带微软雅黑，一定有；找不到就退回 Pillow 的默认位图字体——
    那个**不含汉字**，会显示成方块，所以这里把候选列全一点。
    """
    names = (["msyhbd.ttc", "msyh.ttc"] if bold
             else ["msyh.ttc", "msyhl.ttc"])
    for n in names:
        for base in (r"C:\Windows\Fonts",):
            p = Path(base) / n
            if p.is_file():
                try:
                    return ImageFont.truetype(str(p), size)
                except OSError:
                    continue
    # 退路：等线 / 黑体
    for n in ("Deng.ttf", "simhei.ttf", "simsun.ttc"):
        p = Path(r"C:\Windows\Fonts") / n
        if p.is_file():
            try:
                return ImageFont.truetype(str(p), size)
            except OSError:
                continue
    return ImageFont.load_default()


def ease(t: float) -> float:
    """平滑缓动（smoothstep），让运动不生硬。"""
    t = max(0.0, min(1.0, t))
    return t * t * (3 - 2 * t)


def backdrop() -> Image.Image:
    """深空背景：底色 + 星点 + 一点中心辉光。

    软件本体就是这个调子（深蓝黑 + 星空纹理），片头沿用才像同一个产品。

    **缓存结果**：星点是 340 次椭圆的确定性伪随机绘制，辉光还要一次
    180px 高斯模糊 —— 每帧重算的话光这一个函数就要几百毫秒，
    1800 帧下来白等十几分钟。背景是静态的，算一次就够。
    """
    global _BACKDROP
    if _BACKDROP is not None:
        return _BACKDROP.copy()
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img, "RGBA")
    # 用确定性伪随机，保证每次生成的星星位置一致（便于逐帧比对）
    seed = 20260110
    for i in range(340):
        seed = (seed * 1103515245 + 12345) & 0x7FFFFFFF
        x = seed % W
        seed = (seed * 1103515245 + 12345) & 0x7FFFFFFF
        y = seed % H
        seed = (seed * 1103515245 + 12345) & 0x7FFFFFFF
        b = 60 + seed % 150
        r = 1 if seed % 7 else 2
        d.ellipse([x, y, x + r, y + r], fill=(b, b, min(255, b + 30), 200))
    # 中心辉光
    glow = Image.new("RGB", (W, H), BG)
    gd = ImageDraw.Draw(glow)
    gd.ellipse([W // 2 - 700, H // 2 - 420, W // 2 + 700, H // 2 + 420],
               fill=(14, 26, 46))
    glow = glow.filter(ImageFilter.GaussianBlur(180))
    _BACKDROP = Image.blend(img, glow, 0.55)
    return _BACKDROP.copy()


_BACKDROP: Image.Image | None = None


def glow_ring(img: Image.Image, cx: int, cy: int, r: int,
              color: tuple[int, int, int], alpha: int = 90) -> None:
    """在指定位置叠一圈柔光，用于强调卡片 / 数字。

    按 (半径档位, 颜色, alpha) 缓存模糊结果：呼吸动画只在小范围内改半径，
    量化到 16px 一档就够平滑，却能把 1800 次高斯模糊降到几十次。
    """
    key = (cx // 32, cy // 32, r // 16, color, alpha)
    layer = _GLOW_CACHE.get(key)
    if layer is None:
        layer = Image.new("RGB", (W, H), (0, 0, 0))
        gd = ImageDraw.Draw(layer)
        gd.ellipse([cx - r, cy - r, cx + r, cy + r], fill=color)
        layer = layer.filter(ImageFilter.GaussianBlur(max(8, r // 2)))
        if len(_GLOW_CACHE) < 64:
            _GLOW_CACHE[key] = layer
    img.paste(Image.blend(img, layer, alpha / 255.0), (0, 0))


_GLOW_CACHE: dict = {}


def center_text(d: ImageDraw.ImageDraw, text: str, y: int,
                f: ImageFont.FreeTypeFont, fill=FG) -> int:
    """居中画一行字，返回它的高度（便于接着排版）。"""
    box = d.textbbox((0, 0), text, font=f)
    d.text(((W - (box[2] - box[0])) // 2, y), text, font=f, fill=fill)
    return box[3] - box[1]


def ken_burns(src: Path, t: float, *, zoom: float = 0.09,
              drift: tuple[int, int] = (0, 0),
              aspect: float | None = None) -> Image.Image:
    """把截图按进度 t 做缓慢推近 + 平移，返回一帧。

    ``aspect`` 是目标宽高比：给了就按这个比例裁剪（中心偏上），
    没给就铺满 1920×1080。

    为什么要按比例裁：原始截图是 1384×911，而画布是 16:9。
    直接缩放会留黑边或拉伸变形；裁一刀反而让界面主体更大更清楚。
    """
    im = Image.open(src).convert("RGB")
    if aspect is None:
        target_w, target_h = W, H
    else:
        target_w, target_h = 1000.0, 1000.0 / aspect

    # 先缩放到"至少覆盖目标框"，再居中裁剪
    s = max(target_w / im.width, target_h / im.height)
    nw, nh = int(im.width * s + 0.5), int(im.height * s + 0.5)
    im = im.resize((nw, nh), Image.LANCZOS)

    k = 1.0 + zoom * ease(t)
    cw = max(1, int(nw / k))
    ch = max(1, int(nh / k))
    cw = min(cw, nw)
    ch = min(ch, nh)
    max_x = max(0, nw - cw)
    max_y = max(0, nh - ch)
    cx = int(max_x * (0.15 + 0.7 * ease(t))) + drift[0]
    cy = int(max_y * (0.30 + 0.3 * ease(t))) + drift[1]
    cx = max(0, min(max_x, cx))
    cy = max(0, min(max_y, cy))
    return im.crop((cx, cy, cx + cw, cy + ch))


def dim_overlay(img: Image.Image, amount: float,
                region: tuple[int, int, int, int] | None = None) -> None:
    """压暗一块区域，让叠在上面的文字读得清。

    ``region`` 为 None 时压暗整幅。宣传片里文字压在图上是常事，
    不压暗的话字和界面细节糊在一起，很业余。
    """
    d = ImageDraw.Draw(img, "RGBA")
    a = int(190 * amount)
    box = region or (0, 0, W, H)
    d.rectangle(box, fill=(3, 5, 10, a))


def card(d: ImageDraw.ImageDraw, x: int, y: int, w: int, h: int,
         *, fill=(17, 24, 39, 225), line=ACC, lw: int = 2,
         radius: int = 14) -> None:
    """画一个半透明卡片（带描边），用于承载文字。"""
    d.rounded_rectangle([x, y, x + w, y + h], radius=radius,
                        fill=fill, outline=line, width=lw)


# ---------------------------------------------------------------------------
# 分镜
# ---------------------------------------------------------------------------

def scene_title(t: float) -> Image.Image:
    """① 开场：品牌 + 一句话说清它是什么。"""
    img = backdrop()
    d = ImageDraw.Draw(img, "RGBA")
    a = ease(min(1.0, t / 0.35))
    # 聚焦环随开场呼吸
    glow_ring(img, W // 2, 430, int(230 + 40 * math.sin(t * 6)), ACC, 70)
    d = ImageDraw.Draw(img, "RGBA")

    f_logo = font(96, bold=True)
    f_sub = font(40)
    f_tag = font(30)

    y = 360
    box = d.textbbox((0, 0), "熵光矩阵", font=f_logo)
    d.text(((W - (box[2] - box[0])) // 2, y), "熵光矩阵", font=f_logo,
           fill=(int(230 * a), int(237 * a), int(247 * a)))
    y += box[3] - box[1] + 18
    box = d.textbbox((0, 0), "ENTROPY  LIGHT  MATRIX", font=f_sub)
    d.text(((W - (box[2] - box[0])) // 2, y), "ENTROPY  LIGHT  MATRIX",
           font=f_sub, fill=(int(ACC[0] * a), int(ACC[1] * a),
                             int(ACC[2] * a)))
    y += 96
    tag = "《群星》模组冲突诊断 · 智能排序 · 崩溃翻译"
    box = d.textbbox((0, 0), tag, font=f_tag)
    d.text(((W - (box[2] - box[0])) // 2, y), tag, font=f_tag,
           fill=(int(DIM[0] * a), int(DIM[1] * a), int(DIM[2] * a)))
    return img


def scene_problem(t: float) -> Image.Image:
    """② 痛点：94 个模组，你不知道谁和谁打架。"""
    img = backdrop()
    d = ImageDraw.Draw(img, "RGBA")
    f_big = font(150, bold=True)
    f_mid = font(52)
    f_sm = font(34)

    n = int(94 * ease(min(1.0, t / 0.5)))
    glow_ring(img, W // 2, 400, 260, CRIT, 60)
    d = ImageDraw.Draw(img, "RGBA")
    txt = str(n)
    box = d.textbbox((0, 0), txt, font=f_big)
    d.text(((W - (box[2] - box[0])) // 2, 300), txt, font=f_big, fill=CRIT)
    box = d.textbbox((0, 0), "个已启用模组", font=f_mid)
    d.text(((W - (box[2] - box[0])) // 2, 480), "个已启用模组", font=f_mid,
           fill=FG)

    # 后半段才出现"你不知道谁和谁打架"
    if t > 0.45:
        a = ease((t - 0.45) / 0.3)
        msg = "你根本不知道谁和谁打架"
        box = d.textbbox((0, 0), msg, font=f_mid)
        d.text(((W - (box[2] - box[0])) // 2, 600), msg, font=f_mid,
               fill=(int(FG[0] * a), int(FG[1] * a), int(FG[2] * a)))
        sub = "文件互相覆盖 · 脚本对象重复定义 · 汉化键撞车 · 版本不匹配"
        box = d.textbbox((0, 0), sub, font=f_sm)
        d.text(((W - (box[2] - box[0])) // 2, 690), sub, font=f_sm,
               fill=(int(DIM[0] * a), int(DIM[1] * a), int(DIM[2] * a)))
    return img


def scene_shot(t: float, src: str, title: str, bullets: list[str],
               *, side: str = "right") -> Image.Image:
    """③~⑦ 通用：真实界面截图 + 标题 + 要点。

    排版要点（都是试出来的）：

    * 文字放截图**旁边**而不是压上去 —— 压上去必然挡住界面细节，
      而界面正是要展示的东西
    * 截图按 16:10 裁一刀再放大。原始截图 1384×911 直接铺会缩得很小、
      界面看不清；裁掉暗色留白后主体几乎大一倍
    * 截图带一个 1px 描边 + 圆角，像"贴在深色背景上的一块屏幕"，
      比裸贴一张图更整
    """
    shot_w = int(W * 0.60)
    shot_h = int(shot_w * 10 / 16)
    img = Image.new("RGB", (W, H), BG)

    # ---- 截图（带缓慢推拉）----
    crop = ken_burns(SHOTS / src, t, zoom=0.07, aspect=16 / 10)
    crop = crop.resize((shot_w, shot_h), Image.LANCZOS)

    # 圆角遮罩
    mask = Image.new("L", (shot_w, shot_h), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, shot_w - 1, shot_h - 1], radius=12, fill=255)

    x_img = 60
    y_img = (H - shot_h) // 2
    # 截图背后一圈外发光，把它从背景里"托"起来
    halo = Image.new("RGB", (W, H), BG)
    hd = ImageDraw.Draw(halo)
    hd.rounded_rectangle(
        [x_img - 18, y_img - 18, x_img + shot_w + 18, y_img + shot_h + 18],
        radius=26, fill=(22, 40, 68))
    halo = halo.filter(ImageFilter.GaussianBlur(46))
    img.paste(Image.blend(img, halo, 0.85), (0, 0))
    img.paste(crop, (x_img, y_img), mask)

    # 描边
    d = ImageDraw.Draw(img, "RGBA")
    d.rounded_rectangle([x_img - 1, y_img - 1,
                         x_img + shot_w, y_img + shot_h],
                        radius=13, outline=(60, 92, 140, 190), width=2)

    # ---- 右侧信息栏 ----
    x0 = x_img + shot_w + 56
    avail = W - x0 - 56
    f_t = font(56, bold=True)
    f_b = font(30)
    fade_in = ease(min(1.0, t / 0.25))
    d.text((x0, 318), title, font=f_t,
           fill=(int(ACC[0] * fade_in), int(ACC[1] * fade_in),
                 int(ACC[2] * fade_in)))
    d.rectangle([x0, 398, x0 + int(130 * fade_in), 403], fill=ACC2)

    y = 452
    for i, b in enumerate(bullets):
        start = 0.15 + i * 0.13
        if t < start:
            break
        a = ease((t - start) / 0.22)
        d.ellipse([x0, y + 13, x0 + 9, y + 22],
                  fill=(int(ACC[0] * a), int(ACC[1] * a), int(ACC[2] * a)))
        line, yy = "", y
        for chx in b:
            if d.textlength(line + chx, font=f_b) > avail - 30:
                d.text((x0 + 26, yy), line, font=f_b,
                       fill=(int(FG[0] * a), int(FG[1] * a), int(FG[2] * a)))
                line, yy = chx, yy + 42
            else:
                line += chx
        d.text((x0 + 26, yy), line, font=f_b,
               fill=(int(FG[0] * a), int(FG[1] * a), int(FG[2] * a)))
        y = yy + 42 + 20
    return img


def scene_stats(t: float) -> Image.Image:
    """⑧ 收尾前一屏：把最有说服力的数字一次摆出来。"""
    img = backdrop()
    d = ImageDraw.Draw(img, "RGBA")
    f_big = font(80, bold=True)
    f_sm = font(30)
    f_h = font(46, bold=True)

    box = d.textbbox((0, 0), "它到底做了什么", font=f_h)
    d.text(((W - (box[2] - box[0])) // 2, 150), "它到底做了什么",
           font=f_h, fill=FG)

    items = [
        (str(int(510 * ease(min(1.0, t / 0.3)))), "处冲突一次检出", ACC),
        (str(int(46715 * ease(min(1.0, t / 0.35)))), "个文件深度索引", ACC2),
        (str(int(8689 * ease(min(1.0, t / 0.4)))), "条内置知识库", OK),
        (str(int(735 * ease(min(1.0, t / 0.45)))), "项自动化测试", WARN),
    ]
    bw, gap = 400, 30
    total = len(items) * bw + (len(items) - 1) * gap
    x = (W - total) // 2
    for i, (val, label, color) in enumerate(items):
        d.rounded_rectangle([x, 330, x + bw, 620], radius=18,
                            fill=(17, 24, 39, 210), outline=color, width=2)
        box = d.textbbox((0, 0), val, font=f_big)
        d.text((x + (bw - (box[2] - box[0])) // 2, 380), val,
               font=f_big, fill=color)
        box = d.textbbox((0, 0), label, font=f_sm)
        d.text((x + (bw - (box[2] - box[0])) // 2, 500), label,
               font=f_sm, fill=DIM)
        x += bw + gap
    return img


def scene_outro(t: float) -> Image.Image:
    """⑨ 收尾：名称 + 最核心的承诺。"""
    img = backdrop()
    glow_ring(img, W // 2, 470, 300, ACC2, 70)
    d = ImageDraw.Draw(img, "RGBA")
    f_l = font(84, bold=True)
    f_m = font(40)
    f_s = font(30)
    a = ease(min(1.0, t / 0.4))

    y = 330
    box = d.textbbox((0, 0), "模组开关，永远由你决定", font=f_l)
    d.text(((W - (box[2] - box[0])) // 2, y), "模组开关，永远由你决定",
           font=f_l, fill=(int(ACC2[0] * a), int(ACC2[1] * a),
                           int(ACC2[2] * a)))
    y += 150
    box = d.textbbox((0, 0), "求解器只负责说清「为什么建议关」", font=f_m)
    d.text(((W - (box[2] - box[0])) // 2, y),
           "求解器只负责说清「为什么建议关」", font=f_m,
           fill=(int(FG[0] * a), int(FG[1] * a), int(FG[2] * a)))
    y += 70
    box = d.textbbox((0, 0), "AI 内容必须你显式授权，绝不擅自发送", font=f_m)
    d.text(((W - (box[2] - box[0])) // 2, y),
           "AI 内容必须你显式授权，绝不擅自发送", font=f_m,
           fill=(int(FG[0] * a), int(FG[1] * a), int(FG[2] * a)))
    y += 140
    box = d.textbbox((0, 0), "熵光矩阵  ·  ENTROPY LIGHT MATRIX", font=f_s)
    d.text(((W - (box[2] - box[0])) // 2, y),
           "熵光矩阵  ·  ENTROPY LIGHT MATRIX", font=f_s,
           fill=(int(DIM[0] * a), int(DIM[1] * a), int(DIM[2] * a)))
    return img


# (生成函数, 帧数) —— 30fps，帧数 = 秒数 * 30
SCENE_IMAGES = [
    "1_dashboard.png", "4_conflicts.png", "6_order.png",
    "92_ai_takeover.png", "42_patch_batch.png", "90_crash_ai.png",
    "80_archive.png",
]

SCENES = [
    (lambda t: scene_title(t), 4.5 * FPS),
    (lambda t: scene_problem(t), 6.0 * FPS),
    (lambda t: scene_shot(t, "1_dashboard.png", "一眼看清全局",
                          ["健康分、致命冲突、加载顺序调整项",
                           "自动探测用户目录与游戏本体",
                           "启动游戏前自动开启后台监控"]), 8.0 * FPS),
    (lambda t: scene_shot(t, "4_conflicts.png", "6 类冲突，逐条查清",
                          ["文件覆盖 · 脚本对象重定义 · 本地化键冲突",
                           "版本不兼容 · 文件完整性 · 被大量覆盖",
                           "每条都能点开看「谁覆盖了谁」"]), 8.5 * FPS),
    (lambda t: scene_shot(t, "6_order.png", "一键最优排序",
                          ["按内容分层求解，不动任何一个模组",
                           "只改先后顺序，风险最低",
                           "写回前自动备份，随时回滚"]), 7.5 * FPS),
    (lambda t: scene_shot(t, "92_ai_takeover.png", "AI 接管",
                          ["深度解析 46715 个文件（模组 + 原版 + DLC）",
                           "先出方案，你确认后才写盘",
                           "关闭模组需本地与 AI 双重判定"]), 9.0 * FPS),
    (lambda t: scene_shot(t, "42_patch_batch.png", "补丁工坊",
                          ["本地规则引擎：不联网、免费、快",
                           "AI 引擎：处理需要理解语义的冲突",
                           "所有补丁统一存成一个补丁模组"]), 8.0 * FPS),
    (lambda t: scene_shot(t, "90_crash_ai.png", "崩溃也不用怕",
                          ["几万行英文日志自动归类、翻译、注释",
                           "AI 直接告诉你错误原因与修法",
                           "结论进补丁工坊，可勾选生成补丁"]), 8.5 * FPS),
    (lambda t: scene_shot(t, "80_archive.png", "时之虫档案",
                          ["8689 条群星知识库，16 个分类",
                           "搜索即查，支持中文提问",
                           "命令一键送进游戏控制台"]), 7.5 * FPS),
    (lambda t: scene_stats(t), 5.5 * FPS),
    (lambda t: scene_outro(t), 5.5 * FPS),
]


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    FRAMES.mkdir(parents=True, exist_ok=True)

    # 先确认素材齐全再动手：生成到一半才发现某张图不存在，
    # 前面的帧就白生成了（而且报错会埋在几百行输出里）。
    missing = [s for s in SCENE_IMAGES if not (SHOTS / s).is_file()]
    if missing:
        print("  缺少素材截图：")
        for m in missing:
            print(f"    {SHOTS / m}")
        return 1

    for p in FRAMES.glob("*.png"):
        p.unlink()
    _GLOW_CACHE.clear()

    idx = 0
    manifest = []
    for si, (fn, n) in enumerate(SCENES):
        n = int(n)
        manifest.append((si, idx, n))
        for i in range(n):
            t = i / max(1, n - 1)
            img = fn(t)
            img.save(FRAMES / f"f{idx:05d}.png")
            idx += 1
            if i in (0, n - 1):
                img.save(OUT / f"scene{si:02d}_"
                               f"{'first' if i == 0 else 'last'}.png")
        print(f"  场景 {si:2d}: {n:3d} 帧  (累计 {idx})")

    total = idx
    print(f"\n  合计 {total} 帧 / {total / FPS:.1f} 秒 @ {FPS}fps")
    (OUT / "manifest.txt").write_text(
        "\n".join(f"{s}\t{st}\t{n}" for s, st, n in manifest),
        encoding="utf-8")
    print(f"  帧目录: {FRAMES}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
