"""量前端 DOM 规模，以及切页面会不会累积。

方向：550 MB 里 WebView2 架构成本占大头（改不了），但 DOM 节点数是
**应用自己能控制的**。76 个模组一次性渲染成表格，每行多个 pill/span，
节点可能上千。如果能改成懒渲染，renderer 内存能降一块。

先量清楚再动手：
* 每个页面的 DOM 节点数
* 来回切 30 次后是否累积（真泄漏）
* JS 堆（performance.memory 可用时）
"""
from __future__ import annotations

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
from shot_pages import js                    # noqa: E402

enter_sandbox()
out = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from smd.app import App  # noqa: E402
from smd.ui import INDEX, WINDOW_KWARGS, WINDOW_TITLE  # noqa: E402
from smd.webui import WebBridge  # noqa: E402

PROBE = r"""
Promise.resolve((function(){
  var all = document.getElementsByTagName('*').length;
  var page = document.querySelector('.page.on');
  var inPage = page ? page.getElementsByTagName('*').length : 0;
  var heap = '';
  if (performance.memory) {
    heap = (performance.memory.usedJSHeapSize/1048576).toFixed(1);
  }
  return JSON.stringify({
    page: page ? page.id : '?',
    all: all, inPage: inPage, heap: heap
  });
})())
"""


def main() -> int:
    import webview
    app = App()
    app.load_mods()
    bridge = WebBridge(app)
    win = webview.create_window(WINDOW_TITLE, str(INDEX), js_api=bridge,
                                width=1400, height=950, **WINDOW_KWARGS)
    bridge._window = win
    done = threading.Event()
    rows: list[dict] = []
    loops: list[dict] = []

    def run():
        try:
            for _ in range(80):
                if js(win, "!!(window.pywebview && window.pywebview.api)") is True:
                    break
                time.sleep(0.25)
            time.sleep(3.5)

            ids = js(win, "Promise.resolve(JSON.stringify("
                          "[...document.querySelectorAll('.page')].map(p=>p.id)))")
            ids = json.loads(ids) if isinstance(ids, str) else []
            for pid in ids:
                js(win, "(function(){var n=document.querySelector("
                        "'.nav[data-p=\"%s\"]'); if(n)n.click(); return 1;})()"
                   % pid[2:])
                time.sleep(0.9)
                raw = js(win, PROBE)
                try:
                    rows.append(json.loads(raw) if isinstance(raw, str) else raw)
                except ValueError:
                    pass

            # 来回切 30 次，看累计增长（切"模组"和"总览"两个最大页）
            js(win, "(function(){var n=document.querySelector('.nav[data-p=\"mods\"]');"
                    " if(n)n.click(); return 1;})()")
            time.sleep(1.0)
            base_raw = js(win, PROBE)
            base = json.loads(base_raw) if isinstance(base_raw, str) else {}
            for i in range(30):
                js(win, "(function(){var n=document.querySelector("
                        "'.nav[data-p=\"dash\"]'); if(n)n.click(); return 1;})()")
                time.sleep(0.05)
                js(win, "(function(){var n=document.querySelector("
                        "'.nav[data-p=\"mods\"]'); if(n)n.click(); return 1;})()")
                time.sleep(0.05)
            time.sleep(1.2)
            after_raw = js(win, PROBE)
            after = json.loads(after_raw) if isinstance(after_raw, str) else {}
            loops.append({"before": base, "after": after})
        except Exception as exc:              # noqa: BLE001
            print("异常:", exc, file=out)
        finally:
            done.set()

    def watch():
        done.wait(timeout=420)
        time.sleep(0.4)
        try:
            win.destroy()
        except Exception:
            pass

    win.events.loaded += lambda: threading.Thread(
        target=run, daemon=True).start()
    threading.Thread(target=watch, daemon=True).start()
    webview.start(gui="edgechromium" if sys.platform.startswith("win") else None)

    print("=" * 68, file=out)
    print("前端 DOM 规模（每页）", file=out)
    print("=" * 68, file=out)
    print(f"  {'页面':14} {'页内节点':>10} {'全站节点':>10} {'JS堆MB':>8}",
          file=out)
    for r in sorted(rows, key=lambda x: -x.get("inPage", 0)):
        print(f"  {r.get('page', '?'):14} {r.get('inPage', 0):>10} "
              f"{r.get('all', 0):>10} {r.get('heap', '') or '-':>8}", file=out)

    print("", file=out)
    print("=" * 68, file=out)
    print("来回切 30 次（mods ↔ dash）后是否累积", file=out)
    print("=" * 68, file=out)
    for x in loops:
        b, a = x["before"], x["after"]
        d_all = a.get("all", 0) - b.get("all", 0)
        d_heap = ""
        try:
            d_heap = f"{float(a.get('heap')) - float(b.get('heap')):+.1f}"
        except (TypeError, ValueError):
            pass
        print(f"  切换前: 全站 {b.get('all')} 节点  堆 {b.get('heap') or '-'} MB",
              file=out)
        print(f"  切换后: 全站 {a.get('all')} 节点  堆 {a.get('heap') or '-'} MB",
              file=out)
        print(f"  变化  : {d_all:+d} 节点  堆 {d_heap or '-'} MB", file=out)
        print(f"  → {'没有累积，不是泄漏' if abs(d_all) < 200 else '！节点在累积，可能泄漏'}",
              file=out)
    out.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
