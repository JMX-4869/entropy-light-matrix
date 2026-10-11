"""检查合成音乐：音量、频谱、动态曲线。

音乐没法靠读代码判断对错，用客观指标代替耳朵：
* 分段 RMS —— 看动态起伏是否合理（该有强弱对比，不能一条平线）
* 频段能量 —— 看配器分层（低频该有底但不该占主导）
* 削波 / 峰值 —— 硬伤检查
"""
import io
import sys
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                              errors="replace")

src = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "promo" / "music.wav"
with wave.open(str(src), "rb") as w:
    sr, nch, n = w.getframerate(), w.getnchannels(), w.getnframes()
    x = np.frombuffer(w.readframes(n), "<i2").astype(np.float64) / 32768.0
x = x.reshape(-1, nch)
mono = x.mean(axis=1)

print(f"  {src.name}: {n / sr:.1f}s  {sr}Hz  {nch}ch")
print(f"  峰值       : {np.max(np.abs(x)):.4f}")
print(f"  削波样本数 : {int(np.sum(np.abs(x) >= 0.9999))}")
print(f"  整体 RMS   : {np.sqrt(np.mean(mono ** 2)):.4f}")

# 立体声宽度：左右相关性越低越宽
if nch == 2:
    corr = float(np.corrcoef(x[:, 0], x[:, 1])[0, 1])
    print(f"  左右相关性 : {corr:.3f}  "
          f"（1.0 = 单声道，0.5~0.9 属于宽而自然）")

print("\n  分段 RMS（每 5 秒）：")
seg = int(5 * sr)
rows = []
for i in range(0, len(mono), seg):
    c = mono[i:i + seg]
    if len(c) < sr:
        continue
    r = float(np.sqrt(np.mean(c ** 2)))
    rows.append(r)
    print(f"    {i / sr:5.1f}s  {r:.4f}  {'█' * int(r * 320)}")

if rows:
    lo, hi = min(rows), max(rows)
    print(f"\n  最小/最大段 RMS: {lo:.4f} / {hi:.4f}  动态范围 {hi / max(lo, 1e-9):.2f}x")
    if hi / max(lo, 1e-9) < 1.8:
        print("  ！太平了，缺动态起伏（群星配乐该有明显强弱对比）")
    elif hi / max(lo, 1e-9) > 12:
        print("  ！起伏过猛，可能有段落几近无声")
    else:
        print("  OK 有强弱对比且不突兀")

spec = np.abs(np.fft.rfft(mono * np.hanning(len(mono))))
fr = np.fft.rfftfreq(len(mono), 1 / sr)
print("\n  频段能量占比：")
tot = float(np.sum(spec ** 2)) or 1.0
for name, f0, f1 in (("低频 <200Hz", 20, 200), ("中低 200-800", 200, 800),
                     ("中频 800-3k", 800, 3000),
                     ("中高 3k-8k", 3000, 8000),
                     ("高频 >8k", 8000, sr / 2)):
    m = (fr >= f0) & (fr < f1)
    pct = float(np.sum(spec[m] ** 2)) / tot * 100
    print(f"    {name:14} {pct:5.1f}%  {'█' * int(pct / 2)}")
