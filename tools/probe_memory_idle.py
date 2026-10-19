"""量本程序长期挂着时的内存（看会不会回落）。"""
import io
import subprocess
import sys
import time

out = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

EXE = (r"C:\Users\asus\AppData\Local\Programs\StellarisModDoctor"
       r"\StellarisModDoctor.exe")

PS = r"""
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


def main() -> int:
    subprocess.run(["taskkill", "/F", "/IM", "StellarisModDoctor.exe"],
                   capture_output=True)
    time.sleep(1)
    subprocess.Popen([EXE], stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL)
    time.sleep(16)

    print("  时间          工作集      私有内存    进程数", file=out)
    print("  " + "-" * 50, file=out)
    for label, wait in (("刚加载完", 0), ("+30s 静置", 30),
                        ("+60s 静置", 30), ("+120s 静置", 60)):
        if wait:
            time.sleep(wait)
        ws, pv, n = sample()
        print(f"  {label:12} {ws:8.1f} MB {pv:8.1f} MB    {n}", file=out)

    subprocess.run(["taskkill", "/F", "/IM", "StellarisModDoctor.exe"],
                   capture_output=True)
    out.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
