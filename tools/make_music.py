r"""为宣传片合成一段太空环境音乐（程序生成，无版权问题）。

为什么不找免版权素材
--------------------
素材库的许可条款参差不齐（有的要署名、有的禁止商用、有的禁止再分发），
而这段音乐要跟着仓库一起开源。**自己合成则版权完全归项目所有**，
也不会有"这段曲子被谁用过"的隐患。

而且程序生成能**精确对齐 78.5 秒**：素材库的曲子长度是固定的，
硬剪会在乐句中间断掉，一听就廉价。

音乐设计
--------
* 调性：A 小调。太空/科技片的标准选择 —— 小调自带辽阔与克制，
  又不像大调那样"喜庆"，不抢画面
* 和弦：Am → F → C → G，每和弦约 5.6 秒，循环铺满全片
* 四个层次由弱到强：**持续低音**（sub drone）+ **铺底垫音**（pad）
  + **高频微光**（shimmer）+ **脉冲**（pulse，只在后段进来）
* 整体做成"缓慢起伏"而不是"有节奏的曲子"：
  宣传片配乐一旦节奏明显，观众会去听音乐而不看画面

用法::

    python tools/make_music.py                 # 生成 promo/music.wav
    python tools/make_music.py --seconds 90
"""
from __future__ import annotations

import argparse
import io
import sys
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "promo" / "music.wav"
SR = 44100
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                              errors="replace")

# A 小调：A=220Hz 为基准
NOTE = {
    "A": 110.00, "C": 130.81, "D": 146.83, "E": 164.81, "F": 174.61,
    "G": 196.00, "A4": 220.00, "C5": 261.63, "D5": 293.66, "E5": 329.63,
}

# (根音, 和弦音, 色彩) —— 和弦音用相对半音，避免写一堆频率
# Am, F, C, G 循环；每个和弦 5.6 秒 → 全片约 3.5 轮
CHORDS = [
    ("A", ["A4", "C5", "E5"]),
    ("F", ["A4", "C5", "F"]),
    ("C", ["C5", "E5", "G"]),
    ("G", ["D5", "G", "A4"]),
]
CHORD_SEC = 5.6


def smoothstep(x: np.ndarray) -> np.ndarray:
    """把 0~1 的斜坡变成 S 形，用于渐入渐出，避免"咔"的爆音。"""
    x = np.clip(x, 0.0, 1.0)
    return x * x * (3.0 - 2.0 * x)


def adsr(n: int, a: float, d: float, s: float, r: float,
         sr: int = SR) -> np.ndarray:
    """标准 ADSR 包络。

    垫音必须用**慢起音**（这里 a 通常 1.5 秒以上）：直接方波式起音
    会"啪"一声，铺底音色要像从远处浮上来。
    """
    a_n, d_n, r_n = int(a * sr), int(d * sr), int(r * sr)
    s_n = max(0, n - a_n - d_n - r_n)
    env = np.concatenate([
        np.linspace(0.0, 1.0, a_n, endpoint=False) if a_n else np.empty(0),
        np.linspace(1.0, s, d_n, endpoint=False) if d_n else np.empty(0),
        np.full(s_n, s),
        np.linspace(s, 0.0, r_n) if r_n else np.empty(0),
    ])
    if env.size < n:                      # 浮点取整的零头
        env = np.pad(env, (0, n - env.size))
    return env[:n]


def detuned_pad(freq: float, n: int, sr: int = SR,
                voices: int = 5, spread: float = 0.006,
                bright: float = 0.0) -> np.ndarray:
    """多个微失谐正弦叠加成"厚"的垫音。

    单正弦听着像测试音；±0.6% 的失谐加轻微相位差，才有合唱般的宽度。
    这是合成 pad 最省事也最有效的做法。

    ``bright`` **很关键**：纯正弦没有泛音，能量全堆在基频，
    听起来发闷发糊。实测第一版高频段（>3kHz）能量占比是 **0.0%**，
    过零率只有 400~600Hz —— 就是这个问题。
    给一点奇次泛音（3/5/7 次）能把音色"点亮"，同时不改变基频音高。
    """
    t = np.arange(n, dtype=np.float64) / sr
    out = np.zeros(n)
    rng = np.random.default_rng(int(freq) % 9973)
    for i in range(voices):
        k = (i - (voices - 1) / 2) / max(1, (voices - 1) / 2)
        f = freq * (1.0 + spread * k)
        ph = rng.random() * 2 * np.pi
        # 基频 + 八度
        out += np.sin(2 * np.pi * f * t + ph)
        out += 0.22 * np.sin(2 * np.pi * f * 2 * t + ph * 1.7)
        # 泛音层：按 bright 强度叠加 3/5/7 次谐波，幅度递减
        if bright > 0:
            for h, g in ((3, 0.16), (5, 0.085), (7, 0.05), (11, 0.025)):
                out += bright * g * np.sin(2 * np.pi * f * h * t
                                           + ph * (h % 5 + 1))
    return out / (voices * 1.22)


def airy_strings(freq: float, n: int, sr: int = SR) -> np.ndarray:
    """带通噪声 + 共振峰，做出"弦乐擦弦"的高频质感。

    光靠正弦泛音还是太"合成器"。真实弦乐的高频来自**噪声成分**，
    所以这里用窄带噪声当作高频层，听起来自然得多。
    """
    rng = np.random.default_rng(int(freq * 13) % 7919)
    x = rng.standard_normal(n)
    # 简易双极点谐振器（代替 scipy 的 biquad），在 freq*6 附近形成共振峰
    f0 = min(freq * 6.0, sr * 0.45)
    w0 = 2 * np.pi * f0 / sr
    r = 0.985
    a1, a2 = -2 * r * np.cos(w0), r * r
    y = np.zeros(n)
    y1 = y2 = 0.0
    # 逐样本递归；向量化不了，但 n 只有几万，够快
    for i in range(n):
        v = x[i] - a1 * y1 - a2 * y2
        y[i] = v
        y2, y1 = y1, v
    peak = np.max(np.abs(y)) or 1.0
    return y / peak


def noise_bed(n: int, sr: int = SR, seed: int = 7) -> np.ndarray:
    """一段极轻的高频"空气声"，给底噪一点质感。

    用一阶低通把白噪声压成粉噪声的感觉；直接放白噪声会像收音机静电。
    """
    rng = np.random.default_rng(seed)
    x = rng.standard_normal(n)
    # 一阶 IIR 低通（用 lfilter 的替代：累加即可，够用）
    a = 0.965
    y = np.empty(n)
    acc = 0.0
    # 逐样本循环太慢，用 reshape 分段近似
    step = 4096
    for i in range(0, n, step):
        seg = x[i:i + step]
        out = np.empty(seg.size)
        for j, v in enumerate(seg):
            acc = a * acc + (1 - a) * v
            out[j] = acc
        y[i:i + step] = out
    y /= (np.max(np.abs(y)) + 1e-9)
    return y


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=78.5,
                    help="总时长（默认与视频一致：78.5 秒）")
    ap.add_argument("--out", default=str(OUT))
    ap.add_argument("--gain", type=float, default=0.9,
                    help="总音量系数")
    args = ap.parse_args()

    total = args.seconds
    n = int(total * SR)
    t = np.arange(n, dtype=np.float64) / SR
    print(f"  合成 {total:.1f} 秒 / {SR}Hz / 立体声")

    # ---------- ① 持续低音（Sub drone）----------
    # 整片最底下的一层，一直存在。频率取主音低八度。
    #
    # 音量压得比直觉低：低频在能量上极占便宜（能量 ∝ 幅值²），
    # 一开始给 0.30，实测 <200Hz 就吃掉了 35% 的总能量，
    # 整片听起来又闷又糊。
    print("  · 持续低音")
    drone = detuned_pad(NOTE["A"] / 2, n, voices=3, spread=0.004,
                        bright=0.15) * 0.17
    # 缓慢的音量呼吸（0.045Hz ≈ 22 秒一轮），让它不像死音
    drone *= 0.75 + 0.25 * (0.5 + 0.5 * np.sin(2 * np.pi * 0.045 * t))

    # ---------- ② 铺底垫音（Pad）----------
    print("  · 铺底垫音（带泛音层）")
    pad = np.zeros(n)
    n_ch = int(np.ceil(total / CHORD_SEC))
    for ci in range(n_ch):
        root, tones = CHORDS[ci % len(CHORDS)]
        start = ci * CHORD_SEC
        seg_n = int(min(CHORD_SEC * 1.35, total - start) * SR)
        if seg_n <= 0:
            break
        # 起音 1.8s、收尾 1.4s，与下一个和弦交叠，听感连绵不断
        env = adsr(seg_n, a=1.8, d=0.4, s=0.82, r=1.4)
        seg = np.zeros(seg_n)
        for name in tones:
            seg += detuned_pad(NOTE[name], seg_n, voices=4, spread=0.005,
                               bright=0.55)
            seg += 0.35 * airy_strings(NOTE[name], seg_n)
        if root in NOTE:
            seg += 0.55 * detuned_pad(NOTE[root], seg_n, voices=3,
                                      spread=0.004, bright=0.25)
        seg *= env
        i0 = int(start * SR)
        i1 = min(n, i0 + seg_n)
        pad[i0:i1] += seg[:i1 - i0]
    pad *= 0.26

    # ---------- ③ 高频微光（Shimmer）----------
    # 随机分布的短促高音，像远处星光闪一下。稀疏是关键——
    # 密了就变成"铃铛曲"，抢戏。
    #
    # 频率要够高：第一版用基频×2（最高约 660Hz），能量全落在中频，
    # 高频段实测 0%。这里抬到 ×4~×6，真正进入 2~5kHz。
    print("  · 高频微光")
    shimmer = np.zeros(n)
    rng = np.random.default_rng(20260110)
    scale = ["A4", "C5", "D5", "E5"]
    for _ in range(52):
        at = rng.uniform(2.0, total - 2.0)
        freq = NOTE[scale[rng.integers(0, len(scale))]] * rng.choice([4, 5, 6])
        dur = rng.uniform(1.8, 3.4)
        sn = int(dur * SR)
        i0 = int(at * SR)
        if i0 + sn > n:
            continue
        env = adsr(sn, a=dur * 0.35, d=0.2, s=0.35, r=dur * 0.4)
        seg = detuned_pad(freq, sn, voices=2, spread=0.002,
                          bright=0.8) * env
        seg += 0.4 * airy_strings(freq, sn) * env
        shimmer[i0:i0 + sn] += seg
    shimmer *= 0.085

    # ---------- ④ 脉冲（Pulse）----------
    # 只在 35 秒之后进，给后半段一点推进感。
    # 前半段保持"漂浮"，一上来就打拍子会显得像广告。
    print("  · 脉冲（35s 后进入）")
    pulse = np.zeros(n)
    beat = 0.5 * CHORD_SEC            # 每个和弦两拍
    k = 0
    while True:
        at = k * beat + 35.0
        if at > total - 2.0:
            break
        # 越往后越明显，形成渐强
        amp = 0.5 + 0.5 * min(1.0, (at - 35.0) / 25.0)
        dur = 0.9
        sn = int(dur * SR)
        i0 = int(at * SR)
        if i0 + sn <= n:
            env = adsr(sn, a=0.004, d=0.35, s=0.12, r=0.5)
            # 低频"心跳"：50Hz 正弦 + 一点二次谐波
            tt = np.arange(sn) / SR
            hit = (np.sin(2 * np.pi * 50 * tt)
                   + 0.35 * np.sin(2 * np.pi * 100 * tt)) * env
            pulse[i0:i0 + sn] += hit * amp
        k += 1
    pulse *= 0.16

    # ---------- ⑤ 空气层 ----------
    # 这一层是**高频的主要来源**：噪声经过高频整形后铺满全片，
    # 给整体一点"空气感"。全片都有，只是音量极低——
    # 没有它，纯正弦垫音听起来会像老式电子琴。
    print("  · 空气层（高频质感）")
    air_raw = noise_bed(n)
    # 高通：把噪声的原始值减掉它的慢变分量，剩下的就是高频部分
    k = 48
    slow = np.convolve(air_raw, np.ones(k) / k, mode="same")
    air_hi = air_raw - slow
    air_hi /= (np.max(np.abs(air_hi)) + 1e-9)
    # 电平是试出来的：0.052 → >3kHz 只占 0.3%（等于没有），
    # 0.30 → 3.6%，0.42 → 约 6%。能量按幅值平方走，
    # 但也不能一味拉高：噪声过了头就从"空气感"变成"嘶嘶声"。
    air = air_hi * 0.42
    # 首尾再多一点，进出更自然
    fade = np.ones(n)
    fi8 = int(8 * SR)
    fade[:fi8] = np.linspace(0.2, 1, fi8)
    fade[-fi8:] = np.linspace(1, 0.2, fi8)
    air *= fade

    # ---------- 混音 ----------
    print("  · 混音 + 空间感")
    dry = drone + pad + shimmer + pulse + air

    # 简易混响：把信号延迟若干份并逐次衰减叠加。
    # 真正的卷积混响要 scipy，这里没有；多次延迟叠加已经够糊出一片空间。
    wet = np.zeros(n + SR * 2)
    dry_p = np.pad(dry, (0, SR * 2))
    for delay_ms, g in ((67, 0.30), (113, 0.22), (191, 0.16),
                        (281, 0.11), (421, 0.07)):
        d = int(SR * delay_ms / 1000)
        wet[d:] += dry_p[:len(wet) - d] * g
    mix = dry_p * 0.72 + wet * 0.55
    mix = mix[:n]

    # 首尾各 2.5 秒淡入淡出（否则开头结尾有"咔"声）
    fi = int(2.5 * SR)
    mix[:fi] *= smoothstep(np.linspace(0, 1, fi))
    mix[-fi:] *= smoothstep(np.linspace(1, 0, fi))

    # 归一化到 -3dBFS 附近，留足余量避免削波
    peak = float(np.max(np.abs(mix))) or 1.0
    mix = mix / peak * 0.71 * args.gain

    # 轻微立体声展宽：右声道延迟 12ms，制造宽度
    delay = int(0.012 * SR)
    left = mix
    right = np.concatenate([np.zeros(delay), mix[:-delay]])
    stereo = np.stack([left * 0.98, right * 0.98], axis=1)

    # ---------- 写 WAV ----------
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    pcm = np.clip(stereo * 32767.0, -32768, 32767).astype("<i2")
    with wave.open(str(out), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())

    print(f"\n  完成: {out}")
    print(f"  时长: {n / SR:.1f} 秒   大小: "
          f"{out.stat().st_size / 1048576:.1f} MB   峰值: "
          f"{float(np.max(np.abs(mix))):.3f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
