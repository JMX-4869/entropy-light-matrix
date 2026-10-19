"""测 `--enable-low-end-device-mode` —— Chromium 专门给低内存设备的开关。

之前那些零散参数（--js-flags / --renderer-process-limit / --disable-gpu）
实测全 ±1%，而且**根本没传进去**（被 pywebview 的显式赋值覆盖）。

这个开关不一样：它是 Chromium 里"整包降低内存策略"的总闸，
会同时压低图片解码缓存、渲染缓存、预读、合成器内存池。理论收益比零散参数大。

前提是先让参数真的进去。做法：包一层 `EdgeChrome.__init__`，
在原始初始化之后往 `CreationProperties.AdditionalBrowserArguments` 追加。
（原函数里 `self.webview.CreationProperties = props` 之后 props 对象还在，
   只要 WebView2 还没真正初始化，改它仍然有效。）

实验：同样加载 index.html，比较三种配置的内存 + 验证参数是否进了命令行。
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
out = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

MODE = os.environ.get("SMD_MEM_MODE", "base")   # base / lowend / lowend_gpu

EXTRA = {
    "base": "",
    "lowend": "--enable-low-end-device-mode",
    "lowend_gpu": "--enable-low-end-device-mode --disable-gpu "
                  "--disable-software-rasterizer",
}.get(MODE, "")


def main() -> int:
    import webview
    from webview.platforms import edgechromium as E

    if EXTRA:
        orig = E.EdgeChrome.__init__

        def patched(self, *a, **kw):
            orig(self, *a, **kw)
            try:
                props = self.webview.CreationProperties
                props.AdditionalBrowserArguments = (
                    (props.AdditionalBrowserArguments or "") + " " + EXTRA)
                print(f"  [patch] 已追加: {EXTRA}", file=out)
            except Exception as exc:          # noqa: BLE001
                print(f"  [patch] 失败: {exc}", file=out)

        E.EdgeChrome.__init__ = patched

    from smd.app import App
    from smd.ui import INDEX, WINDOW_KWARGS, WINDOW_TITLE
    from smd.webui import WebBridge
    from sandbox import enter_sandbox
    enter_sandbox()

    app = App()
    app.load_mods()
    bridge = WebBridge(app)
    win = webview.create_window(WINDOW_TITLE, str(INDEX), js_api=bridge,
                                width=1400, height=950, **WINDOW_KWARGS)
    bridge._window = win
    done = threading.Event()

    def run():
        try:
            from shot_pages import js
            for _ in range(80):
                if js(win, "!!(window.pywebview && window.pywebview.api)") is True:
                    break
                time.sleep(0.25)
            time.sleep(8)
            # 量 DOM 节点（确认页面真的渲染了）
            n = js(win, "String(document.getElementsByTagName('*').length)")
            # 查命令行有没有我们的参数
            r = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "$p=Get-CimInstance Win32_Process -Filter "
                 "\"Name='msedgewebview2.exe'\"; "
                 "foreach($x in $p){ if($x.CommandLine -notmatch "
                 "'--type='){ Write-Output $x.CommandLine; break } }"],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=90)
            cmd = (r.stdout or "").strip()
            print(f"  DOM 节点: {n}", file=out)
            print(f"  参数已进命令行: "
                  f"{'--enable-low-end-device-mode' in cmd}", file=out)
        except Exception as exc:              # noqa: BLE001
            print("异常:", exc, file=out)
        finally:
            done.set()

    def watch():
        done.wait(timeout=200)
        time.sleep(0.5)
        try:
            win.destroy()
        except Exception:
            pass

    win.events.loaded += lambda: threading.Thread(
        target=run, daemon=True).start()
    threading.Thread(target=watch, daemon=True).start()
    webview.start(gui="edgechromium" if sys.platform.startswith("win") else None)
    out.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
