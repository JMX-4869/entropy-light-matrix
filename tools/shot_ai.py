"""改进后的崩溃报告 / AI 分析界面截图。"""
import io
import json
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
from sandbox import enter_sandbox            # noqa: E402
from shot_pages import js, shot, worker      # noqa: E402,F401

enter_sandbox()
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                              errors="replace")

from smd.app import App  # noqa: E402
from smd.ui import INDEX, WINDOW_KWARGS, WINDOW_TITLE  # noqa: E402
from smd.webui import WebBridge  # noqa: E402


def main() -> int:
    import webview
    app = App()
    # 造两条 AI 结论，好让界面有东西可截
    app._ai_merge({
        "ok": True, "summary": "x", "parsed": True,
        "issues": [
            {"key": "crash:s1:a", "title": "on_action 事件钩子被重复定义",
             "severity": "critical",
             "cause": "Better Performance 与 Core Framework 都重写了 "
                      "FW_on_CNTR_save_loaded，后加载者整块覆盖前者的定义，"
                      "导致前者的初始化逻辑完全不执行。",
             "fix": "把两者合并成一份完整定义，由补丁模组最后加载覆盖。",
             "mods": ["Better Performance & Utilities",
                      "Core Framework of Kasako"],
             "target": "FW_on_CNTR_save_loaded", "ai_patchable": True,
             "source": "崩溃报告", "source_ref": "stellaris_20261009_211744"},
            {"key": "crash:s1:b", "title": "缺少前置模组 Psionic Species Expansion",
             "severity": "high",
             "cause": "PSE Chinese patch 依赖的前置已退订，补丁本身无法加载。",
             "fix": "重新订阅前置，或退订这个汉化补丁。",
             "mods": ["PSE Chinese patch"], "target": "Psionic Species Expansion",
             "ai_patchable": False,
             "source": "崩溃报告", "source_ref": "stellaris_20261009_211744"},
        ],
    }, group="崩溃 · stellaris_20261009_211744")

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
            js(window, "(function(){goPage('crash'); return 1;})()")
            time.sleep(2.5)
            js(window, "loadAiIssues()")
            time.sleep(2.0)
            shot(window, "90_crash_ai")
            js(window, "(function(){goPage('patch'); patchMode('batch'); return 1;})()")
            time.sleep(2.5)
            js(window, "loadAiIssues()")
            time.sleep(2.0)
            shot(window, "91_patch_ai_issues")
        except Exception as exc:
            import traceback
            print("异常:", exc)
            traceback.print_exc()
        finally:
            done.set()

    def watch():
        done.wait(timeout=240)
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
