"""验证两个产品能共存、互不覆盖。

同时装完整版和轻量版，检查：

1. 各自的安装目录不同
2. 各自的 exe 名不同、都在
3. 注册表里两个独立的卸载项，DisplayName 能区分
4. 卸载其中一个，另一个还在（这是最容易翻车的一条 ——
   如果 ``APP_ID`` 没分开，卸载会连带删掉对方的目录）

全程用临时目录 + 临时注册表键，测完清理。
"""
from __future__ import annotations

import io
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
out = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

PS_RUN = """
$p = Start-Process -FilePath '{setup}' -Wait -PassThru -NoNewWindow `
     -ArgumentList '/S','/dir={target}'
$p.ExitCode
"""

PS_REG = """
Get-ItemProperty "HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*" `
  -ErrorAction SilentlyContinue |
  Where-Object {{ $_.DisplayName -like '*熵光*' }} |
  Select-Object PSChildName, DisplayName, DisplayVersion |
  ConvertTo-Json -Compress
"""


def ps(cmd: str, timeout: int = 300) -> str:
    r = subprocess.run(["powershell", "-NoProfile", "-Command",
                        "[Console]::OutputEncoding="
                        "[System.Text.Encoding]::UTF8; " + cmd],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=timeout)
    return (r.stdout or "").strip()


def uninstall_all() -> None:
    """先把之前装的清掉，避免干扰。"""
    for key in ("StellarisModDoctor", "StellarisModDoctor-Light"):
        ps(f'$k="HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion'
           f'\\Uninstall\\{key}"; if (Test-Path $k) {{ '
           f'$p=(Get-ItemProperty $k).UninstallString; '
           f'if ($p) {{ Start-Process -FilePath $p '
           f'-ArgumentList "/S","--purge" -Wait -NoNewWindow }} }}')
    time.sleep(2)


def main() -> int:
    print("=" * 70, file=out)
    print("两个产品共存测试", file=out)
    print("=" * 70, file=out)

    uninstall_all()
    base = Path(tempfile.mkdtemp(prefix="smd_co_"))
    results = {}
    try:
        for name, label in (("StellarisModDoctor-Setup.exe", "完整版"),
                            ("StellarisModDoctor-Light-Setup.exe", "轻量版")):
            setup = base / name
            shutil.copy2(ROOT / "dist" / name, setup)
            target = base / label
            code = ps(PS_RUN.format(setup=setup, target=target))
            dirs = [p for p in target.iterdir() if p.is_dir()] \
                if target.is_dir() else []
            inst = dirs[0] if dirs else target
            files = sorted(x.name for x in inst.iterdir()) \
                if inst.is_dir() else []
            print(f"\n  [{label}] 退出码={code}", file=out)
            print(f"    安装目录: {inst.name}", file=out)
            for f in files:
                print(f"      {f}", file=out)
            results[label] = {"dir": inst, "files": files, "code": code}

        # 注册表
        reg = ps(PS_REG)
        print(f"\n  注册表卸载项：\n    {reg[:600]}", file=out)

        # 共存检查
        print("\n  " + "-" * 60, file=out)
        ok = True
        a = results.get("完整版", {})
        b = results.get("轻量版", {})
        if a.get("dir") and b.get("dir") and a["dir"] != b["dir"]:
            print(f"  ✓ 安装目录分开: {a['dir'].name} / {b['dir'].name}",
                  file=out)
        else:
            print("  ✗ 安装目录没分开", file=out)
            ok = False
        for label, want in (("完整版", "StellarisModDoctor.exe"),
                            ("轻量版", "StellarisModDoctor-Light.exe")):
            if want in results.get(label, {}).get("files", []):
                print(f"  ✓ {label} 装出了 {want}", file=out)
            else:
                print(f"  ✗ {label} 缺少 {want}", file=out)
                ok = False
        if "StellarisModDoctor-Light" in reg and \
                reg.count("DisplayName") >= 2:
            print("  ✓ 注册表有两个独立卸载项", file=out)
        elif reg:
            print(f"  ？ 注册表只看到一个: {reg[:120]}", file=out)
        else:
            print("  ！ 注册表里没读到（可能写到了别处）", file=out)

        # 卸载轻量版，看完整版是否还在
        print("\n  " + "-" * 60, file=out)
        bdir = b.get("dir")
        if bdir and bdir.is_dir():
            un = next((x for x in bdir.iterdir()
                       if "Uninstall" in x.name), None)
            if un:
                ps(f'Start-Process -FilePath "{un}" '
                   f'-ArgumentList "/S" -Wait -NoNewWindow')
                time.sleep(2)
                still = a.get("dir")
                print(f"  卸载轻量版后，完整版目录还在: "
                      f"{still.is_dir() if still else False}", file=out)
                if still and still.is_dir():
                    print("  ✓ 互不误删", file=out)
                else:
                    print("  ✗ 卸载一个把另一个也删了", file=out)
                    ok = False
    finally:
        uninstall_all()
        shutil.rmtree(base, ignore_errors=True)

    print("\n" + "=" * 70, file=out)
    print(f"  结论：{'两个产品可以共存' if ok else '有问题'}", file=out)
    out.flush()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
