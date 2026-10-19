"""测 WebView2 启动参数对内存的影响。

候选参数（Chromium 系）：
* ``--js-flags=--max-old-space-size=N``  限制 V8 老生代堆上限
* ``--renderer-process-limit=1``         限制渲染进程数
* ``--disable-gpu``                      不起 GPU 进程
* ``--disable-features=...``             关掉不用的特性

用 `WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS` 环境变量注入
—— WebView2 官方支持这个变量，但**前提是代码没显式设置**
`AdditionalBrowserArguments`。pywebview 显式设置了，所以要实测它到底生效不。

每组跑一次，量「本程序进程树」的工作集与私有内存。
"""
from __future__ import annotations

import io
import os
import subprocess
import sys
import time

out = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

EXE = (r"C:\Users\asus\AppData\Local\Programs\StellarisModDoctor"
       r"\StellarisModDoctor.exe")

PS_COUNT = r"""
$procs = Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId
$app = Get-Process -Name StellarisModDoctor | Select-Object -First 1
if (-not $app) { "0 0 0"; exit }
$byParent = @{}
foreach ($p in $procs) {
  $k = [int]$p.ParentProcessId
  if (-not $byParent.ContainsKey($k)) {
    $byParent[$k] = New-Object System.Collections.ArrayList
  }
  [void]$byParent[$k].Add($p.ProcessId)
}
$ids = New-Object System.Collections.ArrayList
[void]$ids.Add($app.Id)
$q = New-Object System.Collections.Queue
$q.Enqueue($app.Id)
while ($q.Count -gt 0) {
  $cur = $q.Dequeue()
  if (-not $byParent.ContainsKey([int]$cur)) { continue }
  foreach ($c in $byParent[[int]$cur]) {
    if ($c -eq $cur) { continue }
    [void]$ids.Add($c)
    $q.Enqueue($c)
  }
}
$ws = 0; $pv = 0
foreach ($i in $ids) {
  $p = Get-Process -Id $i -ErrorAction SilentlyContinue
  if ($p) { $ws += $p.WorkingSet64; $pv += $p.PrivateMemorySize64 }
}
"{0:N1} {1:N1} {2}" -f ($ws / 1MB), ($pv / 1MB), $ids.Count
"""


def sample() -> tuple[float, float, int]:
    r = subprocess.run(["powershell", "-NoProfile", "-Command", PS_COUNT],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=180)
    t = (r.stdout or "").strip().split()
    if len(t) >= 3:
        try:
            return float(t[0]), float(t[1]), int(t[2])
        except ValueError:
            pass
    return 0.0, 0.0, 0


def run_one(label: str, args: str) -> tuple[float, float, int]:
    subprocess.run(["taskkill", "/F", "/IM", "StellarisModDoctor.exe"],
                   capture_output=True)
    time.sleep(1.5)
    env = dict(os.environ)
    env.pop("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS", None)
    if args:
        env["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = args
    subprocess.Popen([EXE], env=env, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL)
    time.sleep(16)
    ws, pv, n = sample()
    print(f"  {label:34} {ws:7.1f} MB  {pv:7.1f} MB  {n} 进程", file=out)
    out.flush()
    return ws, pv, n


def main() -> int:
    print("=" * 74, file=out)
    print("WebView2 启动参数对内存的影响（每次重新启动程序）", file=out)
    print("=" * 74, file=out)

    cases = [
        ("基线（无额外参数）", ""),
        ("--js-flags max-old-space 128", "--js-flags=--max-old-space-size=128"),
        ("--renderer-process-limit=1", "--renderer-process-limit=1"),
        ("--disable-gpu", "--disable-gpu"),
        ("三者合用", "--js-flags=--max-old-space-size=128 "
                     "--renderer-process-limit=1 --disable-gpu"),
    ]
    results = []
    for label, args in cases:
        results.append((label,) + run_one(label, args))

    subprocess.run(["taskkill", "/F", "/IM", "StellarisModDoctor.exe"],
                   capture_output=True)

    print("", file=out)
    base = results[0][1]
    print("  相对基线的变化:", file=out)
    for label, ws, pv, n in results[1:]:
        d = ws - base
        pct = (d / base * 100) if base else 0
        mark = "↓" if d < -10 else ("↑" if d > 10 else "≈")
        print(f"    {label:34} {d:+7.1f} MB ({pct:+5.1f}%) {mark}",
              file=out)
    out.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
