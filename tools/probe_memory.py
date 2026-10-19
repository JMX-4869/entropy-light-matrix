"""量本程序真实的进程内存占用。

不能直接 `Get-Process msedgewebview2` —— 那是**全系统**的 WebView2
（DSH 自己也是），会量出 1.7 GB 这种假数。

正确做法：从本程序的 PID 出发，按 ParentProcessId 建树，
只统计这棵树里的进程。

一次取全部进程（几百条）在内存里建索引，不要每层再查一次
—— 逐层 `Get-CimInstance` 会慢到十分钟都跑不完（实测踩过）。
"""
import io
import json
import subprocess
import sys
import time

out = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

PS = r"""
$ErrorActionPreference = "SilentlyContinue"
$procs = Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId,Name,CommandLine

$app = Get-Process -Name StellarisModDoctor | Select-Object -First 1
if (-not $app) { Write-Output "NOT_RUNNING"; exit }
$root = $app.Id

# 内存里建 父->子 索引
$byParent = @{}
foreach ($p in $procs) {
  $k = [int]$p.ParentProcessId
  if (-not $byParent.ContainsKey($k)) { $byParent[$k] = New-Object System.Collections.ArrayList }
  [void]$byParent[$k].Add($p)
}

# BFS 收集子孙
$tree = New-Object System.Collections.ArrayList
$queue = New-Object System.Collections.Queue
$queue.Enqueue($root)
while ($queue.Count -gt 0) {
  $cur = $queue.Dequeue()
  if (-not $byParent.ContainsKey([int]$cur)) { continue }
  foreach ($c in $byParent[[int]$cur]) {
    if ($c.ProcessId -eq $cur) { continue }
    [void]$tree.Add($c)
    $queue.Enqueue($c.ProcessId)
  }
}

$rows = @()
$rows += [pscustomobject]@{ Pid = $root; Name = "StellarisModDoctor"; Type = "main" }
foreach ($c in $tree) {
  $t = "other"
  if ($c.Name -eq "msedgewebview2") {
    $t = "webview2"
    if ($c.CommandLine -match "--type=(\w[\w-]*)") { $t = "wv:" + $matches[1] }
  }
  $rows += [pscustomobject]@{ Pid = $c.ProcessId; Name = $c.Name; Type = $t }
}

# 取内存
foreach ($r in $rows) {
  $gp = Get-Process -Id $r.Pid -ErrorAction SilentlyContinue
  if ($gp) {
    $r | Add-Member -NotePropertyName WS -NotePropertyValue ($gp.WorkingSet64 / 1MB) -Force
    $r | Add-Member -NotePropertyName PV -NotePropertyValue ($gp.PrivateMemorySize64 / 1MB) -Force
  } else {
    $r | Add-Member -NotePropertyName WS -NotePropertyValue 0 -Force
    $r | Add-Member -NotePropertyName PV -NotePropertyValue 0 -Force
  }
}

$rows | ConvertTo-Json -Depth 3
"""


def main() -> int:
    exe = (r"C:\Users\asus\AppData\Local\Programs\StellarisModDoctor"
           r"\StellarisModDoctor.exe")
    print("  启动程序…", file=out)
    proc = subprocess.Popen([exe], stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    # 等 UI 起来 + 模组清点完成（实测 15s 内完成）
    time.sleep(18)

    r = subprocess.run(["powershell", "-NoProfile", "-Command", PS],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=180)
    raw = (r.stdout or "").strip()
    if "NOT_RUNNING" in raw:
        print("  ！进程没起来", file=out)
        return 1
    try:
        rows = json.loads(raw)
    except ValueError:
        print(f"  ！解析失败\n{raw[:400]}", file=out)
        return 1
    if isinstance(rows, dict):
        rows = [rows]

    print("", file=out)
    print("=" * 64, file=out)
    print("本程序真实内存占用（只算它自己的进程树）", file=out)
    print("=" * 64, file=out)

    groups: dict[str, list] = {}
    for x in rows:
        groups.setdefault(x["Type"], []).append(x)

    total_ws = sum(x["WS"] for x in rows)
    total_pv = sum(x["PV"] for x in rows)

    for t in sorted(groups):
        items = groups[t]
        ws = sum(x["WS"] for x in items)
        pv = sum(x["PV"] for x in items)
        print(f"  {t:14} {len(items):2} 个  工作集 {ws:7.1f} MB  "
              f"私有 {pv:7.1f} MB", file=out)

    print("", file=out)
    print(f"  {'合计':14} {len(rows):2} 个  工作集 {total_ws:7.1f} MB  "
          f"私有 {total_pv:7.1f} MB", file=out)
    print("", file=out)
    # 大块头单独列出
    print("  占用最大的 6 个:", file=out)
    for x in sorted(rows, key=lambda y: -y["WS"])[:6]:
        print(f"    {x['Type']:14} {x['WS']:7.1f} MB", file=out)

    try:
        proc.terminate()
    except Exception:                          # noqa: BLE001
        pass
    subprocess.run(["taskkill", "/F", "/IM", "StellarisModDoctor.exe"],
                   capture_output=True)
    out.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
