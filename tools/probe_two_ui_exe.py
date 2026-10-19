"""量打包后两套界面的内存（递归整棵进程树）。

PyInstaller onefile 有两个进程：引导器（很小）+ 真正的程序。
只看主进程会量出 9.7 MB 这种假数 —— 实际内存全在子进程里。

网页版同理：主进程 + WebView2 的 6~7 个子进程。
"""
from __future__ import annotations

import io
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
out = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
EXE = ROOT / "dist" / "StellarisModDoctor.exe"

PS = r"""
$RootPid = [int]$env:SMD_ROOT_PID
$procs = Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId,Name
$byParent = @{}
foreach ($p in $procs) {
  $k = [int]$p.ParentProcessId
  if (-not $byParent.ContainsKey($k)) {
    $byParent[$k] = New-Object System.Collections.ArrayList
  }
  [void]$byParent[$k].Add($p.ProcessId)
}
$ids = New-Object System.Collections.ArrayList
[void]$ids.Add($RootPid)
$q = New-Object System.Collections.Queue
$q.Enqueue($RootPid)
while ($q.Count -gt 0) {
  $cur = $q.Dequeue()
  if (-not $byParent.ContainsKey([int]$cur)) { continue }
  foreach ($c in $byParent[[int]$cur]) {
    if ($c -eq $cur) { continue }
    [void]$ids.Add($c); $q.Enqueue($c)
  }
}
$ws = 0; $pv = 0
foreach ($i in $ids) {
  $p = Get-Process -Id $i -ErrorAction SilentlyContinue
  if ($p) { $ws += $p.WorkingSet64; $pv += $p.PrivateMemorySize64 }
}
"{0:N1} {1:N1} {2}" -f ($ws / 1MB), ($pv / 1MB), $ids.Count
"""


def tree_mem(pid: int) -> tuple[float, float, int]:
    env = dict(os.environ)
    env["SMD_ROOT_PID"] = str(pid)
    r = subprocess.run(["powershell", "-NoProfile", "-Command", PS],
                       env=env, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=120)
    t = (r.stdout or "").strip().split()
    if len(t) >= 3:
        try:
            return float(t[0]), float(t[1]), int(t[2])
        except ValueError:
            pass
    return 0.0, 0.0, 0


def run_one(label: str, args: list[str], wait: int) -> tuple[float, float, int]:
    subprocess.run(["taskkill", "/F", "/IM", EXE.name], capture_output=True)
    time.sleep(2)
    p = subprocess.Popen([str(EXE), *args], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    time.sleep(wait)
    ws, pv, n = tree_mem(p.pid)
    print(f"  {label:24} {ws:8.1f} MB  {pv:8.1f} MB   {n} 进程",
          file=out)
    out.flush()
    subprocess.run(["taskkill", "/F", "/IM", EXE.name], capture_output=True)
    time.sleep(1.5)
    return ws, pv, n


def main() -> int:
    if not EXE.is_file():
        print(f"  ！找不到 {EXE}", file=out)
        return 1
    print("=" * 70, file=out)
    print("打包后两套界面的内存（递归整棵进程树）", file=out)
    print("=" * 70, file=out)
    print(f"  {'界面':24} {'工作集':>11} {'私有':>11}   进程", file=out)
    light = run_one("轻量版 --light", ["--light"], 14)
    web = run_one("网页版", [], 20)
    print("", file=out)
    if light[0] and web[0]:
        saved = web[0] - light[0]
        print(f"  轻量版省 {saved:.0f} MB（{saved / web[0] * 100:.0f}%）",
              file=out)
    out.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
