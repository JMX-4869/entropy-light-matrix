"""新功能的界面截图：AI 接管、AI 问答、AI 生成外观。"""
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


def seed(app):
    """塞一份假的接管方案 + AI 结论，好让界面有东西可截。"""
    app._takeover = {
        "ok": True, "allow_disable": True, "allow_reorder": True,
        "summary": "整体可控：主要问题是 3 个模组重复定义了同一批国策，"
                   "以及一个汉化补丁缺少前置。调整顺序即可解决大部分。",
        "issues": 6, "conflicts": 510, "critical": 64,
        "indexed_files": 46715, "reorder_count": 23,
        "disable": [
            {"key": "k1", "name": "Old UI Overhaul", "severity": "critical",
             "reason": "它的界面定义被后加载的模组整块覆盖，本身已完全不生效",
             "local_reason": "文件覆盖冲突且排序无法解决"},
            {"key": "k2", "name": "Duplicate Civics Pack", "severity": "high",
             "reason": "与 Core Framework 重复定义同一批国策对象",
             "local_reason": "重复定义：同名对象被更晚的模组覆盖"},
        ],
        "applied": None,
    }
    app._ai_merge({
        "ok": True, "summary": "x", "parsed": True,
        "issues": [{
            "key": "crash:s9:a", "title": "on_action 钩子被重复定义",
            "severity": "critical",
            "cause": "两个模组都重写了 FW_on_CNTR_save_loaded，"
                     "后加载者整块覆盖前者，前者的初始化逻辑不执行。",
            "fix": "合并成一份完整定义，由补丁模组最后加载覆盖。",
            "mods": ["Better Performance & Utilities"],
            "target": "FW_on_CNTR_save_loaded", "ai_patchable": True,
            "source": "崩溃报告", "source_ref": "stellaris_20261009_211744",
        }],
    }, group="崩溃 · stellaris_20261009_211744")


def main() -> int:
    import webview
    app = App()
    seed(app)
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

            js(window, "(function(){goPage('ai'); return 1;})()")
            time.sleep(2.0)
            js(window, "loadTakeoverPlan()")
            time.sleep(2.0)
            shot(window, "92_ai_takeover")

            js(window, "(function(){goPage('appearance'); return 1;})()")
            time.sleep(2.0)
            js(window, "(function(){var e=document.getElementById('aiThemePrompt');"
                       "if(e){e.value='赛博朋克霓虹，紫青撞色，高对比';}return 1;})()")
            time.sleep(1.0)
            shot(window, "93_ai_theme")

            js(window, "(function(){goPage('console'); return 1;})()")
            time.sleep(2.5)
            js(window, "(function(){var e=document.getElementById('wikiAskInput');"
                       "if(e){e.value='怎么快速解锁全部科技？';}"
                       "var b=document.getElementById('wikiAnswer');"
                       "if(b){b.innerHTML='<div class=\"hint ok sm\">档案里检索到 250 条相关条目（用了前 40 条）</div>"
                       "<div class=\"aiAnswer\">用 <b>research_all_technologies</b> 一次研究完当前所有可研究科技。"
                       "<br>想全解锁（含未满足前置的）用 <b>research_technologies</b> 配合科技 key。"
                       "<br>注意：已开启的科技会正常完成，但不会自动补上缺失的前置。</div>"
                       "<div class=\"hint\" style=\"margin-top:8px\">从回答里认出 2 条命令，可以直接加进队列：</div>"
                       "<div class=\"row\" style=\"gap:6px;flex-wrap:wrap\">"
                       "<button class=\"btn sm mono\">＋ research_all_technologies</button>"
                       "<button class=\"btn sm mono\">＋ research_technologies</button></div>';}"
                       "return 1;})()")
            time.sleep(1.2)
            shot(window, "94_archive_ai")
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
