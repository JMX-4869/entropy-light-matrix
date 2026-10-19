"""对比两套界面的真实内存。

轻量版：`python -m smd --light`（Tkinter）
网页版：`python -m smd`（pywebview + Edge WebView2）

各起一次、等加载完、量**本进程自己的**内存。
注意别用 `Get-Process msedgewebview2` —— 那是全系统的，会量出假数。
"""
from __future__ import annotations

import io
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
out = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

PS = r"""
$procs = Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId
$app = Get-Process -Name python -ErrorAction SilentlyContinue |
       Where-Object { $_.MainWindowTitle -like '*熵光*' } |
       Select-Object -First 1
if (-not $app) { "0 0"; exit }
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


def sample() -> tuple[float, float, int]:
    r = subprocess.run(["powershell", "-NoProfile", "-Command", PS],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=180)
    t = (r.stdout or "").strip().split()
    if len(t) >= 3:
        try:
            return float(t[0]), float(t[1]), int(t[2])
        except ValueError:
            pass
    return 0.0, 0.0, 0


def kill_all() -> None:
    subprocess.run(["powershell", "-NoProfile", "-Command",
                    "Get-Process python -ErrorAction SilentlyContinue | "
                    "Where-Object { $_.MainWindowTitle -like '*熵光*' } | "
                    "Stop-Process -Force"], capture_output=True)


def run_one(label: str, args: list[str]) -> tuple[float, float, int]:
    kill_all()
    time.sleep(2)
    proc = subprocess.Popen([sys.executable, "-m", "smd", *args],
                            cwd=str(ROOT),
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    time.sleep(20)
    ws, pv, n = sample()
    print(f"  {label:22} {ws:8.1f} MB  {pv:8.1f} MB   {n} 进程", file=out)
    out.flush()
    try:
        proc.terminate()
    except Exception:                          # noqa: BLE001
        pass
    kill_all()
    time.sleep(1.5)
    return ws, pv, n


def main() -> int:
    print("=" * 68, file=out)
    print("两套界面的内存（只算本程序自己的进程树）", file=out)
    print("=" * 68, file=out)
    print(f"  {'界面':22} {'工作集':>10} {'私有':>10}   进程", file=out)
    light = run_one("轻量版（Tkinter）", ["--light"])
    web = run_one("网页版（WebView2）", [])
    print("", file=out)
    if light[0] and web[0]:
        saved = web[0] - light[0]
        print(f"  轻量版省 {saved:.0f} MB（{saved / web[0] * 100:.0f}%）",
              file=out)
    out.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
