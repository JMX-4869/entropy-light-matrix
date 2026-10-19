"""临时改 pywebview 源码，测 `--enable-low-end-device-mode` 的真实收益。

为什么要动 venv：`props.AdditionalBrowserArguments` 在
`self.webview.CreationProperties = props` 之后就被 WebView2 读走并固化了，
事后再改它无效（实测：patch 打印成功，但命令行里没有）。

要生效只能改**那行赋值本身**。所以：
1. 备份 pywebview 的 edgechromium.py
2. 在第一个参数串后面插 `--enable-low-end-device-mode`
3. 起窗口、量内存
4. 还原

流程跑一遍拿数字，再决定值不值得在应用里做（比如整体替换 __init__）。
"""
from __future__ import annotations

import io
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
out = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

TARGET = (ROOT / ".venv" / "Lib" / "site-packages" / "webview" / "platforms"
          / "edgechromium.py")
BACKUP = TARGET.with_suffix(".py.smd_bak")

ORIG = ("props.AdditionalBrowserArguments = "
        "'--disable-features=ElasticOverscroll'")

PS_PROBE = r"""
$p = Get-CimInstance Win32_Process -Filter "Name='msedgewebview2.exe'"
foreach ($x in $p) {
  if ($x.CommandLine -notmatch '--type=') { Write-Output $x.CommandLine; break }
}
"""

PS_PY = r"""
$procs = Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId
$app = Get-Process -Name python,StellarisModDoctor -ErrorAction SilentlyContinue |
       Where-Object { $_.MainWindowTitle -like "*熵光*" } |
       Select-Object -First 1
if (-not $app) { "NOPROC"; exit }
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
$ws = 0
foreach ($i in $ids) {
  $p = Get-Process -Id $i -ErrorAction SilentlyContinue
  if ($p) { $ws += $p.WorkingSet64 }
}
"{0:N1} {1}" -f ($ws / 1MB), $ids.Count
"""


def apply_patch(extra: str) -> bool:
    if not TARGET.is_file():
        print(f"  ！找不到 {TARGET}", file=out)
        return False
    if not BACKUP.is_file():
        shutil.copy2(TARGET, BACKUP)
    src = TARGET.read_text(encoding="utf-8")
    if ORIG not in src:
        print("  ！源码里找不到那行赋值，pywebview 版本可能变了", file=out)
        return False
    new = ORIG if not extra else (
        "props.AdditionalBrowserArguments = "
        f"'{'--disable-features=ElasticOverscroll'} {extra}'")
    TARGET.write_text(src.replace(ORIG, new), encoding="utf-8")
    return True


def restore() -> None:
    if BACKUP.is_file():
        shutil.copy2(BACKUP, TARGET)
        BACKUP.unlink()
        print("  已还原 pywebview 源码", file=out)


def measure(label: str) -> tuple[float, int, bool]:
    """起窗口 → 量内存 → 查参数 → 关。"""
    env = dict(os.environ)
    env["SMD_MEM_MODE"] = "base"          # 不走 patch，靠改过的源码
    proc = subprocess.Popen([sys.executable,
                             str(ROOT / "tools" / "probe_lowend.py")],
                            env=env, cwd=str(ROOT),
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    time.sleep(22)
    got = False
    r = subprocess.run(["powershell", "-NoProfile", "-Command", PS_PROBE],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=90)
    cmd = (r.stdout or "").strip()
    if "enable-low-end-device-mode" in cmd:
        got = True
    ws, n = 0.0, 0
    r2 = subprocess.run(["powershell", "-NoProfile", "-Command", PS_PY],
                        capture_output=True, text=True, encoding="utf-8",
                        errors="replace", timeout=120)
    t = (r2.stdout or "").strip().split()
    if len(t) >= 2 and t[0] != "NOPROC":
        try:
            ws, n = float(t[0]), int(t[1])
        except ValueError:
            pass
    print(f"  {label:36} {ws:7.1f} MB  {n} 进程  参数生效={got}",
          file=out)
    out.flush()
    try:
        proc.terminate()
    except Exception:                          # noqa: BLE001
        pass
    time.sleep(2)
    subprocess.run(["taskkill", "/F", "/IM", "python.exe"],
                   capture_output=True)
    return ws, n, got


def main() -> int:
    print("=" * 76, file=out)
    print("--enable-low-end-device-mode 的真实收益（临时改 pywebview 源码）",
          file=out)
    print("=" * 76, file=out)

    try:
        if not apply_patch(""):
            restore()
            return 1
        b_ws, b_n, b_ok = measure("基线（原参数）")

        if not apply_patch("--enable-low-end-device-mode"):
            restore()
            return 1
        l_ws, l_n, l_ok = measure("+ enable-low-end-device-mode")

        if not apply_patch("--enable-low-end-device-mode --disable-gpu "
                           "--disable-software-rasterizer"):
            restore()
            return 1
        g_ws, g_n, g_ok = measure("+ low-end + disable-gpu")
    finally:
        restore()

    print("", file=out)
    if b_ws and l_ws:
        d = l_ws - b_ws
        print(f"  low-end mode 相对基线: {d:+.1f} MB ({d / b_ws * 100:+.1f}%)",
              file=out)
    if b_ws and g_ws:
        d = g_ws - b_ws
        print(f"  low-end + 无GPU      : {d:+.1f} MB ({d / b_ws * 100:+.1f}%)",
              file=out)
    if not (l_ok or g_ok):
        print("  ！参数仍未生效，收益无从判断", file=out)
    out.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
