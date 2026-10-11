"""把 promo/frames 里的帧序列编码成 MP4。

单独一个脚本而不是塞进 make_promo.py：帧渲染和视频编码是两件事，
分开才能只重渲帧、或只用已有帧重编码（改码率/格式时很常见）。

用法::

    python tools/make_video.py                # 编码成 H.264 MP4
    python tools/make_video.py --crf 16       # 更高质量

编码参数为什么这么选
--------------------
* **H.264 (libx264) + yuv420p** —— 兼容性最好的一档。
  yuv420p 是必须的：不加的话 libx264 可能输出 yuv444p，
  很多播放器（含 Windows 自带的和手机）会直接黑屏。
* **-profile:v high -level 4.1** —— 1080p30 的通用档，
  老设备也吃得下。
* **+faststart** —— 把索引表挪到文件头。不加的话网页上必须
  整段下完才能播，分享出去体验很差。
* **CRF 18** —— 视觉无损附近。宣传片要给人看，不值当为省几 MB 降质。
"""
from __future__ import annotations

import io
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FRAMES = ROOT / "promo" / "frames"
OUT = ROOT / "promo" / "熵光矩阵-宣传片-1080p.mp4"
FPS = 30
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                              errors="replace")


def find_ffmpeg() -> str:
    """找 ffmpeg：先 PATH，再 winget 的安装位置。

    winget 装完后软链不一定立刻进当前进程的 PATH，
    所以要主动去 Packages 目录翻一下，否则会误报"没装"。
    """
    p = shutil.which("ffmpeg")
    if p:
        return p
    roots = [
        Path.home() / "AppData" / "Local" / "Microsoft" / "WinGet" / "Links",
        Path.home() / "AppData" / "Local" / "Microsoft" / "WinGet" / "Packages",
        Path(r"C:\ffmpeg\bin"),
        Path(r"C:\Program Files\ffmpeg\bin"),
    ]
    for r in roots:
        if not r.is_dir():
            continue
        hit = next(r.rglob("ffmpeg.exe"), None)
        if hit:
            return str(hit)
    return ""


def main() -> int:
    ff = find_ffmpeg()
    if not ff:
        print("  找不到 ffmpeg。装一个：winget install Gyan.FFmpeg")
        return 1
    print(f"  ffmpeg: {ff}")

    frames = sorted(FRAMES.glob("f*.png"))
    if not frames:
        print(f"  没有帧可编码。先跑：python tools/make_promo.py")
        return 1
    expect = f"f{frames[0].stem[1:]}-f{frames[-1].stem[1:]}"
    print(f"  帧数: {len(frames)}  ({expect})  "
          f"约 {len(frames) / FPS:.1f} 秒")

    crf = "18"
    if "--crf" in sys.argv:
        try:
            crf = sys.argv[sys.argv.index("--crf") + 1]
        except IndexError:
            pass

    OUT.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        ff, "-y",
        "-framerate", str(FPS),
        "-i", str(FRAMES / "f%05d.png"),
        "-c:v", "libx264",
        "-preset", "medium",
        "-crf", crf,
        "-pix_fmt", "yuv420p",          # 不加这个很多播放器黑屏
        "-profile:v", "high",
        "-level", "4.1",
        "-movflags", "+faststart",      # 网页分享要能边下边播
        "-an",                          # 本片无配音
        str(OUT),
    ]
    print("  编码中…（1080p 约一两分钟）")
    r = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0:
        print("  编码失败：")
        print("   " + (r.stderr or "")[-1500:].replace("\n", "\n   "))
        return r.returncode

    mb = OUT.stat().st_size / 1048576
    print(f"\n  完成: {OUT}")
    print(f"  大小: {mb:.1f} MB   {len(frames) / FPS:.1f} 秒   "
          f"1920x1080 @ {FPS}fps")
    return 0


if __name__ == "__main__":
    sys.exit(main())
