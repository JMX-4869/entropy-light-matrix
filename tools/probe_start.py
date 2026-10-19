"""启动验证：程序到底有没有把窗口画出来。

**为什么不能只看"进程还在"**：GUI 程序（``console=False``）抛异常时，
PyInstaller 的 bootloader 会弹错误对话框，进程照样活着。
第一次打轻量版就栽在这 —— 我报了"运行中 84 MB"，屏幕上其实是个
``Failed to execute script`` 错误框。

**为什么要看标题**：正常窗口标题含「熵光矩阵」，崩溃弹窗含
``Unhandled exception`` / ``Failed to execute`` / ``启动失败``。

**为什么不用 EnumWindows 回调**：试过，PowerShell 里闭包收集不到结果
（``$found +=`` 在委托回调的作用域里不生效），永远返回 0 个窗口 ——
一度让我以为程序没起来。改用 ``Get-Process`` 的主窗口标题，简单可靠。
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import time

out = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

PS = r"""
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
Get-Process -ErrorAction SilentlyContinue |
  Where-Object { $_.MainWindowTitle -ne '' } |
  Select-Object ProcessName, MainWindowTitle, Id |
  ConvertTo-Json -Compress
"""

BAD = ("Unhandled exception", "Failed to execute", "启动失败", "Traceback",
       "Error")


def titles() -> list[dict]:
    r = subprocess.run(["powershell", "-NoProfile", "-Command", PS],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=120)
    raw = (r.stdout or "").strip()
    if not raw:
        return []
    try:
        d = json.loads(raw)
    except ValueError:
        return []
    return d if isinstance(d, list) else [d]


def check(exe: str, label: str, wait: int = 22) -> bool:
    """启动并检查窗口。返回是否正常。"""
    name = os.path.basename(exe)
    subprocess.run(["taskkill", "/F", "/IM", name], capture_output=True)
    time.sleep(1.5)
    print(f"\n  [{label}] 启动 {name}", file=out)
    p = subprocess.Popen([exe], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    time.sleep(wait)

    wins = titles()
    ours = [w for w in wins
            if "熵光" in w.get("MainWindowTitle", "")
            or any(b in w.get("MainWindowTitle", "") for b in BAD)]
    ok = False
    bad = False
    for w in ours:
        t = w["MainWindowTitle"]
        if any(b in t for b in BAD):
            print(f"    ! 崩溃弹窗: {t[:70]}", file=out)
            bad = True
        elif "熵光" in t:
            print(f"    ✓ {t[:70]}", file=out)
            ok = True
    if not ours:
        print(f"    没有相关窗口（共 {len(wins)} 个可见窗口）", file=out)
        for w in wins[:6]:
            print(f"      {w['ProcessName']}: "
                  f"{w['MainWindowTitle'][:50]}", file=out)

    try:
        p.terminate()
    except Exception:                          # noqa: BLE001
        pass
    subprocess.run(["taskkill", "/F", "/IM", name], capture_output=True)
    time.sleep(1)
    return ok and not bad


def main() -> int:
    cases = []
    if len(sys.argv) > 1:
        for i in range(1, len(sys.argv), 2):
            cases.append((sys.argv[i], sys.argv[i + 1]))
    else:
        cases = [("dist/StellarisModDoctor.exe", "完整版"),
                 ("dist/StellarisModDoctor-Light.exe", "轻量版")]

    print("=" * 70, file=out)
    print("启动验证（看窗口标题，不看进程存活）", file=out)
    print("=" * 70, file=out)
    results = {}
    for exe, label in cases:
        results[label] = check(exe, label)

    print("\n" + "=" * 70, file=out)
    for label, ok in results.items():
        print(f"  {label:12} {'✓ 正常' if ok else '✗ 有问题'}", file=out)
    out.flush()
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
