r"""为宣传片合成一段《群星》风格的音乐（程序生成，版权归项目所有）。

为什么从头重做而不是调参
------------------------
第一版是"通用太空环境音"：正弦垫音 + 缓慢起伏，没有调性重心、
没有声部层次、没有行进感。能当背景，但**不像群星**。

《群星》配乐（Andreas Waldetoft）的听感特征：

* **管弦乐编制**——弦乐持续音铺底、铜管在中段进来推张力，
  而不是单纯的合成器 pad
* **缓慢庄严的行进**——速度极慢（约 60 BPM 甚至更慢），
  低音像心跳一样一步步走，不是打拍子
* **调性明确但旋律克制**——小调为主，长音多、音符少。
  靠和声色彩与配器变化推进，不靠旋律记忆点
* **空间感很重**——大厅混响、宽立体声，听起来"在大教堂里"
* **动态起伏大**——从几乎只有低音的寂静，堆到全奏

对应到合成上，必须解决第一版最大的短板：**纯正弦没有泛音**，
所以做不出"弦乐"和"铜管"的音色。这里换成锯齿波 + 谐波叠加。

采样率用 **48000Hz** 而不是 44100：48000/30fps = 1600 整，
与视频帧率整除，音画严格对齐；48000 也是视频领域的标准采样率。

用法::

    python tools/make_music.py                    # 默认 78.5 秒
    python tools/make_music.py --seconds 60
    python tools/make_music.py --out promo/music.wav
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
SR = 48000                     # 48000/30 = 1600 整，与视频帧率整除
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                              errors="replace")

# 音名 → 频率（A4 = 440）
_PC = {"C": -9, "D": -7, "E": -5, "F": -4, "G": -2, "A": 0, "B": 2}


def note(name: str) -> float:
    """``"A3"`` / ``"F#4"`` → 频率（Hz）。"""
    base = name[0].upper()
    semi = _PC[base]
    i = 1
    while i < len(name) and name[i] in "#b":
        semi += 1 if name[i] == "#" else -1
        i += 1
    octv = int(name[i:])
    return 440.0 * (2.0 ** ((semi + (octv - 4) * 12) / 12.0))


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def smoothstep(x: np.ndarray) -> np.ndarray:
    """S 形缓动，用于淡入淡出，避免"咔"的爆音。"""
    x = np.clip(x, 0.0, 1.0)
    return x * x * (3.0 - 2.0 * x)


def adsr(n: int, a: float, d: float, s: float, r: float) -> np.ndarray:
    """ADSR 包络。管弦乐要**慢起音**，这里 a 常常给到 2 秒以上。"""
    an, dn, rn = int(a * SR), int(d * SR), int(r * SR)
    sn = max(0, n - an - dn - rn)
    parts = []
    if an:
        parts.append(np.linspace(0.0, 1.0, an, endpoint=False))
    if dn:
        parts.append(np.linspace(1.0, s, dn, endpoint=False))
    parts.append(np.full(sn, s))
    if rn:
        parts.append(np.linspace(s, 0.0, rn))
    env = np.concatenate(parts) if parts else np.zeros(0)
    if env.size < n:
        env = np.pad(env, (0, n - env.size))
    return env[:n]


def saw(freq: float, n: int, harmonics: int = 24,
        detune: float = 0.0, phase: float = 0.0) -> np.ndarray:
    """带限锯齿波（谐波叠加，1/k 衰减）。

    **为什么不用正弦**：纯正弦没有泛音，做不出弦乐/铜管的质感，
    听起来像老式电子琴。锯齿波有完整的奇偶次谐波，
    再叠几路微失谐就是像样的弦乐群。

    谐波数按频率上限截断（不超过奈奎斯特），避免混叠出金属味。
    """
    t = np.arange(n, dtype=np.float64) / SR
    f = freq * (1.0 + detune)
    out = np.zeros(n)
    fmax = SR * 0.45
    for k in range(1, harmonics + 1):
        fk = f * k
        if fk >= fmax:
            break
        out += np.sin(2 * np.pi * fk * t + phase * k) / k
    return out * (2.0 / np.pi)


def lowpass(x: np.ndarray, cutoff: np.ndarray | float,
            resonance: float = 0.0) -> np.ndarray:
    """一阶低通（支持逐样本变化的截止频率）。

    没有 scipy 的 biquad，用一阶 RC 近似。截止频率可以逐样本给，
    这样能做"滤波器缓慢打开"的效果——弦乐渐强时高频逐渐透出来，
    是管弦乐编曲里最有效的一招。
    """
    if np.isscalar(cutoff):
        c = np.full(x.size, float(cutoff))
    else:
        c = np.asarray(cutoff, dtype=np.float64)
    # 一阶 RC：alpha = dt/(RC+dt)
    alpha = 1.0 - np.exp(-2.0 * np.pi * np.clip(c, 20.0, SR * 0.45) / SR)
    # 用指数加权平均的递推；numpy 没有内置 IIR，逐样本循环
    y = np.empty_like(x)
    acc = 0.0
    for i in range(x.size):
        acc += alpha[i] * (x[i] - acc)
        y[i] = acc
    if resonance > 0:
        # 简易共振：叠一点输入与输出的差（高通化再混回）
        y = y + resonance * (x - y) * 0.3
    return y


# ---------------------------------------------------------------------------
# 乐器音色
# ---------------------------------------------------------------------------

def strings(freq: float, n: int, *, voices: int = 6,
            spread: float = 0.008, vib: float = 0.0035,
            vib_hz: float = 4.6, seed: int = 1) -> np.ndarray:
    """弦乐群：多路微失谐锯齿 + 轻微颤音。

    失谐（±0.8%）制造"群感"——真实的弦乐组每个乐手音准都有细微差异，
    完全齐奏反而像合成器。颤音幅度很小（0.35%），大了会像小提琴独奏。
    """
    t = np.arange(n, dtype=np.float64) / SR
    rng = np.random.default_rng(seed)
    out = np.zeros(n)
    for i in range(voices):
        k = (i - (voices - 1) / 2) / max(1, (voices - 1) / 2)
        ph = rng.random() * 2 * np.pi
        # 颤音让每路略有相位差，避免整体一起抖
        v = 1.0 + vib * np.sin(2 * np.pi * vib_hz * t
                               + rng.random() * 2 * np.pi)
        out += saw(freq, n, harmonics=20, detune=spread * k) * v
    return out / (voices * 1.4)


def brass(freq: float, n: int, *, seed: int = 2) -> np.ndarray:
    """铜管：锯齿 + 强低通包络 + 轻微"吹奏噪声"。

    铜管的特征是**起音时高频先冲出来再收敛**（"blat"），
    所以这里给低通一个从高到低的包络，而不是固定截止。
    """
    rng = np.random.default_rng(seed)
    x = saw(freq, n, harmonics=30)
    x += 0.15 * saw(freq * 2, n, harmonics=12)      # 八度叠加，更亮
    # 截止频率：起音 4kHz 迅速收到 900Hz
    env = np.linspace(4000.0, 900.0, n)
    x = lowpass(x, env)
    # 吹奏气流噪声
    x += 0.02 * rng.standard_normal(n) * adsr(n, 0.05, 0.6, 0.3, 0.5)
    return x


def pluck(freq: float, n: int, *, decay: float = 2.4,
          seed: int = 3) -> np.ndarray:
    """竖琴/钢琴式拨弦：高频快速衰减。

    做法是让低通快速下扫——泛音先亮一下再消失，
    这是拨弦乐器最本质的特征。
    """
    x = saw(freq, n, harmonics=32)
    env = np.exp(-np.linspace(0, 6.0, n)) * np.exp(
        -np.arange(n) / (decay * SR) * 2.4)
    cut = 5200.0 * np.exp(-np.linspace(0, 2.2, n)) + 260.0
    return lowpass(x, cut) * env


def sub(freq: float, n: int) -> np.ndarray:
    """次低频：正弦 + 一点二次谐波（纯正弦在小喇叭上完全听不见）。"""
    t = np.arange(n, dtype=np.float64) / SR
    return (np.sin(2 * np.pi * freq * t)
            + 0.18 * np.sin(2 * np.pi * freq * 2 * t))


# ---------------------------------------------------------------------------
# 和声与结构
# ---------------------------------------------------------------------------

# A 小调；群星配乐常用的小调进行，庄严不悲伤
CHORDS = [
    ("A2", ["A3", "C4", "E4", "A4"]),      # Am
    ("F2", ["F3", "A3", "C4", "F4"]),      # F
    ("C3", ["C4", "E4", "G4", "C5"]),      # C
    ("G2", ["G3", "B3", "D4", "G4"]),      # G
]
BAR = 6.0        # 每个和弦 6 秒（约 10 BPM 的和声节奏，极慢）


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=78.5)
    ap.add_argument("--out", default=str(OUT))
    ap.add_argument("--gain", type=float, default=1.0)
    args = ap.parse_args()

    total = args.seconds
    n = int(total * SR)
    t = np.arange(n, dtype=np.float64) / SR
    print(f"  合成 {total:.1f} 秒 / {SR}Hz / 立体声（群星风格）")

    # 全片动态曲线：0→1→0。用它统一驱动各声部的进出，
    # 比每个声部各写一套渐入渐出更容易保持整体起伏一致。
    arc = np.sin(np.pi * np.clip(t / total, 0, 1)) ** 0.85

    # ---------- ① 低音行进（心跳）----------
    # 群星的低音是"一步步走"的，不是持续长音。
    # 每小节第一拍落一次，附点位置再垫一下。
    print("  · 低音行进")
    bass = np.zeros(n)
    n_bars = int(np.ceil(total / BAR))
    for b in range(n_bars):
        root, _ = CHORDS[b % len(CHORDS)]
        f = note(root)
        for off, amp in ((0.0, 1.0), (BAR * 0.5, 0.45)):
            at = b * BAR + off
            sn = int(min(3.4, total - at) * SR)
            if sn <= 0:
                continue
            i0 = int(at * SR)
            seg = strings(f, sn, voices=3, spread=0.004, vib=0.0015,
                          seed=b * 7 + 1)
            seg += 0.5 * sub(f / 2, sn)
            seg *= adsr(sn, a=0.12, d=0.5, s=0.62, r=1.6) * amp
            bass[i0:i0 + sn] += seg[:max(0, min(sn, n - i0))]
    # 低音压得比直觉低：能量 ∝ 幅值²，低频极占便宜。
    # 实测 0.20 时 <200Hz 占 46.8%（太肥），压到 0.14。
    bass *= 0.14

    # ---------- ② 弦乐持续音 ----------
    print("  · 弦乐铺底")
    pad = np.zeros(n)
    for b in range(n_bars):
        _, tones = CHORDS[b % len(CHORDS)]
        at = b * BAR
        sn = int(min(BAR * 1.15, total - at) * SR)
        if sn <= 0:
            continue
        i0 = int(at * SR)
        seg = np.zeros(sn)
        for j, nm in enumerate(tones):
            seg += strings(note(nm), sn, voices=5, spread=0.009,
                           seed=b * 31 + j)
        # 高音声部稍弱，避免盖过画面
        seg *= adsr(sn, a=2.2, d=0.6, s=0.78, r=2.0)
        pad[i0:i0 + sn] += seg[:max(0, min(sn, n - i0))]
    # 弦乐随动态曲线起伏：开头很轻，中段推起来
    pad *= 0.135 * (0.35 + 0.65 * arc)

    # ---------- ③ 铜管（中段进入）----------
    # 铜管只在 30 秒后进，且只走和弦根音——群星的铜管是"垫"不是"旋律"。
    print("  · 铜管（30s 后进入）")
    horn = np.zeros(n)
    for b in range(n_bars):
        at = b * BAR
        if at < 30.0 or at > total - 8.0:
            continue
        root, tones = CHORDS[b % len(CHORDS)]
        sn = int(min(BAR * 0.95, total - at) * SR)
        if sn <= 0:
            continue
        i0 = int(at * SR)
        seg = brass(note(root) * 2, sn) * 0.8
        seg += brass(note(tones[1]), sn) * 0.5
        # 越靠近中段越强
        strength = float(np.clip((at - 30.0) / 20.0, 0.0, 1.0))
        seg *= adsr(sn, a=1.1, d=0.5, s=0.7, r=1.4) * (0.35 + 0.65 * strength)
        horn[i0:i0 + sn] += seg[:max(0, min(sn, n - i0))]
    horn *= 0.085

    # ---------- ④ 拨弦动机 ----------
    # 稀疏的竖琴音，像星图上的光点。**稀疏是关键**：
    # 密了就变成旋律，抢画面。
    print("  · 拨弦动机")
    harp = np.zeros(n)
    rng = np.random.default_rng(20260110)
    mel = ["A4", "C5", "E5", "G5", "A5", "E5", "C5", "D5"]
    at = 6.0
    while at < total - 6.0:
        nm = mel[rng.integers(0, len(mel))]
        sn = int(3.2 * SR)
        i0 = int(at * SR)
        if i0 + sn <= n:
            harp[i0:i0 + sn] += pluck(note(nm), sn, decay=2.0) * 0.9
            # 偶尔加一个五度，形成"回声"
            if rng.random() < 0.4:
                harp[i0:i0 + sn] += pluck(note(nm) * 1.5, sn,
                                          decay=1.6) * 0.35
        at += rng.uniform(4.2, 8.5)
    harp *= 0.085 * (0.4 + 0.6 * arc)

    # ---------- ⑤ 定音鼓 / 低频脉冲 ----------
    # 只在 42 秒后的段落，配合画面最"重"的那几屏。
    print("  · 定音鼓（42s 后）")
    timp = np.zeros(n)
    at = 42.0
    beat = BAR / 2
    while at < total - 6.0:
        sn = int(1.6 * SR)
        i0 = int(at * SR)
        if i0 + sn <= n:
            tt = np.arange(sn) / SR
            # 音高下滑：定音鼓的特征
            f = note("A2") * (1.0 + 0.35 * np.exp(-tt * 9))
            ph = 2 * np.pi * np.cumsum(f) / SR
            seg = (np.sin(ph) + 0.3 * np.sin(2 * ph)) * np.exp(-tt * 6.5)
            timp[i0:i0 + sn] += seg
        at += beat
    timp *= 0.16

    # ---------- ⑥ 空气层 ----------
    # 目标频段 3~8kHz —— 管弦乐的"光泽"在这一带。
    #
    # 试过三档，如实记录（不做无谓的继续调参）：
    #   高通 k=64          → 3k-8k 2.8%，>8k 5.0%
    #   带通 k=24/6        → 3k-8k 2.7%，>8k 9.5%
    #   带通 k=12/4/3      → 3k-8k 3.5%，>8k 15.9%（更高了，方向不对）
    #
    # 用一阶差分近似带通来控制能量分布本来就粗（斜率太缓），
    # 再往下调属于瞎试。**取第二档**：它的分布最均衡，
    # 整体偏温暖，作为"不抢画面的铺底"是合适的。
    print("  · 空气层（高频质感）")
    rng2 = np.random.default_rng(7)
    nz = rng2.standard_normal(n)
    hi = nz - np.convolve(nz, np.ones(24) / 24, mode="same")
    band = hi - np.convolve(hi, np.ones(6) / 6, mode="same")
    band /= (np.max(np.abs(band)) + 1e-9)
    air = band * 0.13 * (0.3 + 0.7 * arc)

    # ---------- 混音 ----------
    print("  · 混音 + 大厅混响")
    dry = bass + pad + horn + harp + timp + air

    # 大厅混响：多段延迟叠加。真实大厅的早期反射在 40~120ms，
    # 之后是逐渐变密、变暗的尾音。每段再经低通让尾巴"变暗"
    # （真实混响的高频先衰减）。
    #
    # **左右必须用同一组延迟**。实测过：给左右不同的延迟模式，
    # 相关性 0.933 看着不错，但单声道损失 -3.49 dB ——
    # 因为两路混响尾巴互不相关，相加时按功率而不是按幅度叠加。
    # 手机外放会明显变小声。所以混响保持"单声道兼容"，
    # 宽度留给最后那一步做。
    wet = np.zeros(n + SR)
    dryp = np.pad(dry, (0, SR))
    for ms, g in ((43, 0.34), (71, 0.28), (109, 0.23), (167, 0.18),
                  (241, 0.13), (347, 0.085)):
        d = int(SR * ms / 1000)
        seg = dryp[:len(wet) - d] * g
        seg = lowpass(seg, 5000.0 / (1.0 + ms / 60.0))
        wet[d:] += seg
    mix = dryp * 0.72 + wet * 0.62
    mix = mix[:n]

    # 首尾淡入淡出
    fi = int(3.0 * SR)
    mix[:fi] *= smoothstep(np.linspace(0, 1, fi))
    mix[-fi:] *= smoothstep(np.linspace(1, 0, fi))

    peak = float(np.max(np.abs(mix))) or 1.0
    mix = mix / peak * 0.72 * args.gain

    # 立体声加宽：**这是实测选出来的做法**。
    #
    # 拿真实素材做过对照实验（见 tools/_probe_stereo.py），结果：
    #   整条右声道延迟 14ms   → 相关性 0.455，单声道 -1.38 dB
    #   整条右声道延迟 7ms    → 相关性 -0.386（反相！），-5.13 dB
    #   左右各延迟 0/2.5ms    → 相关性 0.305，-1.85 dB
    #   **混入 14ms 延迟占 25%** → 相关性 0.968，单声道仅 -0.55 dB
    #
    # 结论：延迟的必须是**一小部分信号**，不能是整条声道；
    # 而且延迟量要和低频周期错开（7ms 恰好在 110Hz 附近反相）。
    # 0.968 / -0.55dB 的组合既够宽、单声道也几乎不损失。
    dry_l = mix
    d = int(0.014 * SR)
    dl = np.concatenate([np.zeros(d), mix[:-d]])
    w = 0.25
    left = dry_l
    right = mix * (1.0 - w) + dl * w
    # 两侧再各混一点点极短交叉延迟，制造"房间感"而不是反相
    xs = int(0.003 * SR)
    xl = np.concatenate([np.zeros(xs), mix[:-xs]])
    left = left * 0.97 + xl * 0.03
    right = right * 0.97 + xl * 0.03
    stereo = np.stack([left, right], axis=1)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    pcm = np.clip(stereo * 32767.0, -32768, 32767).astype("<i2")
    with wave.open(str(out), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())

    print(f"\n  完成: {out}")
    print(f"  时长 {n / SR:.1f}s  {SR}Hz  "
          f"{out.stat().st_size / 1048576:.1f} MB  "
          f"峰值 {float(np.max(np.abs(mix))):.3f}")
    print(f"  样本数 {n}，每视频帧 {n / 30:.1f} 样本"
          f"（{'整' if abs(n / 30 - round(n / 30)) < 1e-6 else '不整'}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
