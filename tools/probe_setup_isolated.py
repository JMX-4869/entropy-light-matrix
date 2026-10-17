"""验证安装包是**自包含**的 —— 模拟用户的下载环境。

上一版探针从 `dist/` 直接跑安装包，而那个目录里同时躺着
`StellarisModDoctor.exe`，于是 `payload_exe()` 顺手找到了主程序，
把"安装包没把主程序打进去"这个致命缺陷**完全掩盖**了 ——
真实用户只下载了 Setup.exe 一个文件，装到一半报
"找不到 StellarisModDoctor.exe"，退出码 2。

所以这个脚本的第一件事是：**把 Setup.exe 单独复制到一个空目录**，
再在那里跑。这就是用户的处境。

检查项：
1. 安装包自己能装成功（不依赖旁边有任何文件）
2. 程序本体确实落到了目标目录
3. 装出来的程序能自报版本
"""
from __future__ import annotations

import io
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
out = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
SETUP = ROOT / "dist" / "StellarisModDoctor-Setup.exe"
APP_DIR_NAME = "StellarisModDoctor"
APP_EXE = "StellarisModDoctor.exe"


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", **kw)


def main() -> int:
    if not SETUP.is_file():
        print(f"  找不到安装包 {SETUP}", file=out)
        return 1

    size_mb = SETUP.stat().st_size / 1048576
    print(f"  安装包: {SETUP.name}  {size_mb:.2f} MB", file=out)

    # ── 隔离：只有 Setup.exe，就像用户下载后那样 ──
    iso = Path(tempfile.mkdtemp(prefix="smd_iso_"))
    shutil.copy2(SETUP, iso / SETUP.name)
    print(f"\n  隔离目录（只放安装包）: {iso}", file=out)
    for f in iso.iterdir():
        print(f"      {f.name}", file=out)

    target_base = iso / "install_here"
    print(f"\n  静默安装到: {target_base}", file=out)
    r = run([str(iso / SETUP.name), "/S", "--dir", str(target_base),
             "--no-desktop", "--no-menu"], timeout=600)
    print(f"  返回码: {r.returncode}", file=out)
    for line in (r.stdout or "").strip().splitlines()[-6:]:
        print(f"    {line}", file=out)

    named = target_base / APP_DIR_NAME
    exe = named / APP_EXE
    ok_install = r.returncode == 0
    ok_exe = exe.is_file()
    print(f"\n  安装成功: {ok_install}", file=out)
    print(f"  程序本体落地: {ok_exe}  ({named})", file=out)
    if named.is_dir():
        for f in sorted(named.iterdir()):
            print(f"      {f.name}  {f.stat().st_size / 1048576:.2f} MB",
                  file=out)

    # 装出来的程序能跑吗
    ok_run = False
    if ok_exe:
        v = run([str(exe), "--version"], timeout=180)
        line = (v.stdout or "").strip().splitlines()
        print(f"\n  版本自报: {line[0] if line else '(空)'}", file=out)
        ok_run = v.returncode == 0 and "1.0" in (v.stdout or "")

    ok = ok_install and ok_exe and ok_run
    print(f"\n  结论: "
          f"{'自包含 —— 只下载一个文件就能装' if ok else '！仍然依赖旁边文件'}", file=out)

    # 清理（先跑卸载器，再强删）
    uninst = named / "EntropyLightMatrix-Uninstall.exe"
    if uninst.is_file():
        run([str(uninst), "/S"], timeout=300)
    shutil.rmtree(iso, ignore_errors=True)
    print("  已清理", file=out)
    out.flush()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
