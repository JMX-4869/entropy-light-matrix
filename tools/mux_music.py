"""把背景音乐混进宣传片。

单独一个脚本，和 make_video.py 分开：视频编码一次要一两分钟，
只是换音乐或调音量时不该重编一遍画面。这里用 ``-c:v copy``
**直接复制视频流**，只重编音频，几秒钟就好。

用法::

    python tools/mux_music.py                 # 用默认音量
    python tools/mux_music.py --music-vol 0.6 # 音乐更小声
    python tools/mux_music.py --no-music      # 去掉音乐（还原纯画面版）

音量默认 0.55：这是**背景**音乐，盖过画面文字的信息量就本末倒置了。
真人和音乐的比例一般在 0.3~0.6 之间，0.55 偏保守但适合无解说的片子。
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
VIDEO = PROMO / "熵光矩阵-宣传片-1080p.mp4"
MUSIC = PROMO / "music.wav"
OUT = PROMO / "熵光矩阵-宣传片-1080p-含音乐.mp4"
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                              errors="replace")


def find_ffmpeg() -> str:
    """找 ffmpeg：先 PATH，再 winget 的安装位置。

    winget 装完后软链不一定立刻进当前进程的 PATH，所以要主动翻一下，
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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--music-vol", type=float, default=0.55)
    ap.add_argument("--no-music", action="store_true")
    ap.add_argument("--video", default=str(VIDEO))
    ap.add_argument("--music", default=str(MUSIC))
    ap.add_argument("--out", default=str(OUT))
    args = ap.parse_args()

    ff = find_ffmpeg()
    if not ff:
        print("  找不到 ffmpeg。装一个：winget install Gyan.FFmpeg")
        return 1

    video = Path(args.video)
    if not video.is_file():
        print(f"  找不到视频：{video}")
        print("  先跑：python tools/make_video.py")
        return 1
    out = Path(args.out)

    if args.no_music:
        print("  还原纯画面版（丢掉音频流）")
        cmd = [ff, "-y", "-i", str(video), "-c:v", "copy", "-an", str(out)]
    else:
        music = Path(args.music)
        if not music.is_file():
            print(f"  找不到音乐：{music}")
            print("  先跑：python tools/make_music.py")
            return 1
        print(f"  混音：音乐音量 {args.music_vol}")
        # -shortest 让输出跟着较短的流结束；
        # apad 把音乐补到画面长度，避免最后几帧静音造成听感断层
        cmd = [
            ff, "-y",
            "-i", str(video),
            "-i", str(music),
            "-filter_complex",
            f"[1:a]volume={args.music_vol},apad[a]",
            "-map", "0:v", "-map", "[a]",
            "-c:v", "copy",              # 画面一眼不动，几秒出片
            "-c:a", "aac", "-b:a", "192k",
            "-shortest",
            "-movflags", "+faststart",
            str(out),
        ]

    r = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0:
        print("  失败：")
        print("   " + (r.stderr or "")[-1200:].replace("\n", "\n   "))
        return r.returncode

    mb = out.stat().st_size / 1048576
    print(f"\n  完成: {out}")
    print(f"  大小: {mb:.1f} MB")

    # 校验：确认音视频轨都在、时长对齐
    probe = cmd[0].replace("ffmpeg", "ffprobe")
    q = subprocess.run(
        [probe, "-v", "error",
         "-show_entries", "format=duration",
         "-show_entries", "stream=codec_type,codec_name,channels",
         "-of", "default=noprint_wrappers=1", str(out)],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    lines = [x for x in q.stdout.splitlines() if x.strip()]
    print("  校验: " + "  ".join(lines))
    has_a = any(x.startswith("codec_type=audio") for x in lines)
    if not args.no_music and not has_a:
        print("  ！没有音频轨，混音失败了")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
