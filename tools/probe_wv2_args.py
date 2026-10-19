"""确认两件事：

1. `WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS` 到底有没有传进 WebView2
   —— 上面那组实测全 ±1%，我怀疑被 pywebview 的显式赋值覆盖了
2. 页面切换会不会让 DOM / JS 堆累积（真·内存泄漏方向）

第 2 条才是可能有收益的地方：550 MB 里 WebView2 的架构成本占大头，
但如果切页面会让 DOM 一直涨，那部分是可以修的。
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

# 看 webview2 的 browser 进程命令行里有没有我们传的参数
PS_ARGS = r"""
$p = Get-CimInstance Win32_Process -Filter "Name='msedgewebview2.exe'"
foreach ($x in $p) {
  if ($x.CommandLine -match "--type=([\w-]+)") {
    $t = $matches[1]
  } else { $t = "browser" }
  if ($t -eq "browser") { Write-Output $x.CommandLine }
}
"""


def main() -> int:
    subprocess.run(["taskkill", "/F", "/IM", "StellarisModDoctor.exe"],
                   capture_output=True)
    time.sleep(1.5)

    env = dict(os.environ)
    env["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = (
        "--js-flags=--max-old-space-size=128 --renderer-process-limit=1")
    subprocess.Popen([EXE], env=env, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL)
    time.sleep(14)

    r = subprocess.run(["powershell", "-NoProfile", "-Command", PS_ARGS],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=120)
    cmd = (r.stdout or "").strip()
    print("=" * 72, file=out)
    print("1) 环境变量注入的参数有没有进到 WebView2 命令行", file=out)
    print("=" * 72, file=out)
    if not cmd:
        print("  没读到 browser 进程命令行", file=out)
    else:
        has_js = "max-old-space-size" in cmd
        has_rpl = "renderer-process-limit" in cmd
        print(f"  含 --js-flags      : {has_js}", file=out)
        print(f"  含 --renderer-limit: {has_rpl}", file=out)
        print("", file=out)
        if not (has_js or has_rpl):
            print("  → 参数**没传进去**，被 pywebview 的显式赋值覆盖了。", file=out)
            print("    所以上一组实测全 ±1% 是「参数根本没生效」，"
                  "不是「参数无效」。", file=out)
        # 打印关键片段
        for kw in ("--disable-features", "--allow-file-access",
                   "--js-flags", "--renderer-process-limit"):
            i = cmd.find(kw)
            if i >= 0:
                print(f"    找到 {kw}: …{cmd[i:i + 60]}…", file=out)
        if "--disable-features" in cmd and "--js-flags" not in cmd:
            j = cmd.find("--disable-features")
            print(f"    实际生效的参数段: {cmd[max(0, j - 60):j + 80]}",
                  file=out)

    subprocess.run(["taskkill", "/F", "/IM", "StellarisModDoctor.exe"],
                   capture_output=True)
    out.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
