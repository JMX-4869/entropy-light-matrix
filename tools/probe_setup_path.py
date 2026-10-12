"""行为验证：安装包的 /S 静默安装会不会自动补程序文件夹。

不能只看源码（install.py 在 PyInstaller 的 PYZ 里），
直接跑一遍安装包、传一个**父目录**，再检查文件落在哪。

全程只碰临时目录，绝不触发真实安装。
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


def main() -> int:
    if not SETUP.is_file():
        print(f"  找不到安装包 {SETUP}", file=out)
        return 1

    base = Path(tempfile.mkdtemp(prefix="smd_setup_probe_"))
    print(f"  临时父目录: {base}", file=out)
    print(f"  传给安装包的 --dir: {base}", file=out)
    print("  （这是**父目录**，不是最终安装目录）", file=out)
    print("", file=out)

    # 静默安装；不建快捷方式、不建开始菜单 —— 这是行为验证，
    # 不该在用户桌面上留东西
    r = subprocess.run(
        [str(SETUP), "/S", "--dir", str(base),
         "--no-desktop", "--no-menu"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=300)
    print(f"  安装返回码: {r.returncode}", file=out)
    if r.stdout:
        for ln in r.stdout.strip().splitlines()[-4:]:
            print(f"    {ln}", file=out)
    print("", file=out)

    named = base / APP_DIR_NAME
    root_exe = base / "StellarisModDoctor.exe"
    print("  检查文件落点：", file=out)
    print(f"    {base.name}/{APP_DIR_NAME}/ 存在: {named.is_dir()}", file=out)
    if named.is_dir():
        for f in sorted(named.iterdir()):
            print(f"        {f.name}  {f.stat().st_size / 1048576:.2f} MB",
                  file=out)
    print(f"    父目录根下直接有 exe（应为 False）: {root_exe.is_file()}",
          file=out)
    print("", file=out)

    ok = named.is_dir() and not root_exe.is_file()
    print(f"  结论: {'修复生效 —— 自动补了程序文件夹' if ok else '！仍然散落'}", file=out)

    # 清理（先跑卸载器，再强删，避免残留注册表项）
    uninst = named / "EntropyLightMatrix-Uninstall.exe"
    if uninst.is_file():
        try:
            subprocess.run([str(uninst), "/S"],
                           capture_output=True, timeout=180)
        except Exception:                     # noqa: BLE001
            pass
    shutil.rmtree(base, ignore_errors=True)
    print(f"  临时目录已清理", file=out)
    out.flush()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
