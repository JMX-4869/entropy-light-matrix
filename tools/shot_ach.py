"""截一张「影响成就的模组」卡片的图，确认排版。"""
import io
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
from sandbox import enter_sandbox            # noqa: E402
from shot_pages import js, shot              # noqa: E402

enter_sandbox()
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                              errors="replace")

from smd.app import App  # noqa: E402
from smd.ui import INDEX, WINDOW_KWARGS, WINDOW_TITLE  # noqa: E402
from smd.webui import WebBridge  # noqa: E402


def main() -> int:
    import webview
    app = App()
    app.load_mods()
    # 真扫一遍，让界面有真实数据（不造假数据 —— 假数据看不出真问题）
    app.achievement_scan()
    t0 = time.time()
    while app.task.running and time.time() - t0 < 300:
        time.sleep(0.3)
    print(f"  扫描完成，检出 {len(app.achievement_risk_rows())} 个")

    bridge = WebBridge(app)
    window = webview.create_window(WINDOW_TITLE, str(INDEX), js_api=bridge,
                                   width=1400, height=950, **WINDOW_KWARGS)
    bridge._window = window
    done = threading.Event()

    def run():
        try:
            for _ in range(80):
                if js(window, "!!(window.pywebview && window.pywebview.api)") is True:
                    break
                time.sleep(0.25)
            time.sleep(3.0)
            js(window, "(function(){goPage('solution'); return 1;})()")
            time.sleep(2.0)
            js(window, "loadAch()")
            time.sleep(2.5)
            shot(window, "95_achievement")
            # 勾两个，看重选取状态
            js(window, "(function(){var c=document.querySelectorAll("
                       "'#achList input[type=checkbox]:not([disabled])');"
                       "if(c[0])c[0].click(); if(c[1])c[1].click(); return 1;})()")
            time.sleep(1.2)
            shot(window, "96_achievement_picked")
            err = js(window, "Promise.resolve(String(window.__jserr||''))")
            print(f"  JS 错误: {err!r}"[:120])
        except Exception as exc:
            import traceback
            print("异常:", exc)
            traceback.print_exc()
        finally:
            done.set()

    def watch():
        done.wait(timeout=300)
        time.sleep(0.5)
        try:
            window.destroy()
        except Exception:
            pass

    window.events.loaded += lambda: threading.Thread(
        target=run, daemon=True).start()
    threading.Thread(target=watch, daemon=True).start()
    webview.start(gui="edgechromium" if sys.platform.startswith("win") else None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
