"""把 promo/frames 里的帧序列编码成 MP4，场景之间做交叉淡化。

v1 的问题：场景是**硬切**的，观感廉价。这里用 ffmpeg 的 ``xfade``
在相邻场景之间做 0.5 秒叠化。

实现方式值得说明
----------------
直接把 2355 张图当一路 ``-i`` 输入没法做转场——转场需要"两段画面
同时存在"，而单路输出只有一个时间轴。

所以做法是：把同一路帧输入拆成 11 份（一路对应一个场景），
各自 ``trim`` 出自己的帧区间，再用 ``xfade`` 串起来。
好处是**每个场景的帧区间可以精确到帧**（由 manifest 给出），
不会因为浮点累加而漂移到半帧。

用法::

    python tools/make_video.py                # 默认 CRF 18
    python tools/make_video.py --crf 16       # 更高质量
    python tools/make_video.py --no-xfade     # 硬切（对比用）
"""
from __future__ import annotations

import argparse
import io
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROMO = ROOT / "promo"
FRAMES = PROMO / "frames"
MANIFEST = PROMO / "manifest.txt"
OUT = PROMO / "熵光矩阵-宣传片-1080p.mp4"
FPS = 30
XFADE_FRAMES = 15          # 0.5 秒 @ 30fps
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                              errors="replace")


def find_ffmpeg() -> str:
    """找 ffmpeg：先 PATH，再 winget 的安装位置。

    winget 装完后软链不一定立刻进当前进程的 PATH，主动翻一下，
    否则会误报"没装"。
    """
    p = shutil.which("ffmpeg")
    if p:
        return p
    for r in (Path.home() / "AppData" / "Local" / "Microsoft" / "WinGet"
              / "Links",
              Path.home() / "AppData" / "Local" / "Microsoft" / "WinGet"
              / "Packages",
              Path(r"C:\ffmpeg\bin"), Path(r"C:\Program Files\ffmpeg\bin")):
        if r.is_dir():
            hit = next(r.rglob("ffmpeg.exe"), None)
            if hit:
                return str(hit)
    return ""


def read_manifest() -> list[tuple[int, int]]:
    """读场景表 → [(起始帧, 帧数)]。

    manifest.txt 每行是 ``场景号\\t起始帧\\t帧数``，由 make_promo.py 写出。
    """
    out = []
    for line in MANIFEST.read_text(encoding="utf-8").splitlines():
        parts = line.split("\t")
        if len(parts) == 3:
            out.append((int(parts[1]), int(parts[2])))
    return out


def build_filter(scenes: list[tuple[int, int]], xf: int,
                 input_label: str = "0:v") -> tuple[str, int]:
    """构造 trim + xfade 的滤镜链，返回 (滤镜串, 输出总帧数)。"""
    if len(scenes) <= 1:
        return f"[{input_label}]null[vout]", sum(n for _, n in scenes)

    parts = []
    for i, (start, n) in enumerate(scenes):
        parts.append(f"[{input_label}]trim=start_frame={start}:"
                     f"end_frame={start + n},setpts=PTS-STARTPTS[s{i}]")
    prev, acc = "s0", scenes[0][1]
    for i in range(1, len(scenes)):
        label = f"x{i}" if i < len(scenes) - 1 else "vout"
        parts.append(f"[{prev}][s{i}]xfade=transition=fade:"
                     f"duration={xf / FPS:.4f}:"
                     f"offset={(acc - xf) / FPS:.4f}[{label}]")
        acc = acc - xf + scenes[i][1]
        prev = label
    return ";".join(parts), acc


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--crf", default="18")
    ap.add_argument("--no-xfade", action="store_true")
    ap.add_argument("--out", default=str(OUT))
    args = ap.parse_args()

    ff = find_ffmpeg()
    if not ff:
        print("  找不到 ffmpeg。装一个：winget install Gyan.FFmpeg")
        return 1
    print(f"  ffmpeg: {ff}")

    frames = sorted(FRAMES.glob("f*.png"))
    if not frames:
        print("  没有帧可编码。先跑：python tools/make_promo.py")
        return 1
    scenes = read_manifest()
    if not scenes:
        print(f"  读不到场景表：{MANIFEST}")
        print("  它由 make_promo.py 生成，重跑一次即可。")
        return 1

    xf = 0 if args.no_xfade else XFADE_FRAMES
    print(f"  帧数 {len(frames)}  场景 {len(scenes)}  "
          f"转场 {'硬切' if xf == 0 else f'{xf / FPS:.2f}s 叠化'}")

    filt, total = build_filter(scenes, xf)
    cmd = [ff, "-y", "-framerate", str(FPS),
           "-i", str(FRAMES / "f%05d.png"),
           "-filter_complex", filt,
           "-map", "[vout]",
           "-c:v", "libx264", "-preset", "medium", "-crf", args.crf,
           "-pix_fmt", "yuv420p",        # 不加很多播放器会黑屏
           "-profile:v", "high", "-level", "4.1",
           "-movflags", "+faststart",    # 网页分享要能边下边播
           "-an", str(args.out)]

    print("  编码中…（1080p 带转场，约两三分钟）")
    r = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0:
        print("  编码失败：")
        print("   " + (r.stderr or "")[-1800:].replace("\n", "\n   "))
        return r.returncode

    out = Path(args.out)
    mb = out.stat().st_size / 1048576
    expect = total / FPS
    print(f"\n  完成: {out}")
    print(f"  大小 {mb:.1f} MB   预计时长 {expect:.2f}s "
          f"({total} 帧 @ {FPS}fps)")

    probe = ff.replace("ffmpeg", "ffprobe")
    q = subprocess.run([probe, "-v", "error", "-show_entries",
                        "format=duration", "-of",
                        "default=noprint_wrappers=1", str(out)],
                       capture_output=True, text=True)
    got = q.stdout.strip()
    print(f"  实际 {got}")
    try:
        real = float(got.split("=")[1])
        if abs(real - expect) > 0.2:
            print(f"  ！时长对不上：预期 {expect:.2f}s，实际 {real:.2f}s")
            return 1
    except (IndexError, ValueError):
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
