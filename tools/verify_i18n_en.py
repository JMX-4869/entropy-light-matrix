"""切到英文，截图 + 统计还剩多少中文。

这是 i18n 工作的最终验收：不看注入数量，看**实际界面**。
注入再多，只要浏览器里还显示中文就没完成任务。
"""
from __future__ import annotations

import io
import json
import re
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

# 每个页面里可见的中文文本节点数 + 样例。
# 排除「数据」类：模组名、路径、知识库条目、日志内容 —— 那些本来就该是中文。
PROBE = r"""
(function(){
  var out = [], n = 0;
  var skip = /(C:|\\|\.txt|\.json|\.log|\.mod|ugc:|steamapps|Paradox Interactive)/;
  var w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  var t;
  while (t = w.nextNode()) {
    var s = (t.nodeValue || '').trim();
    if (!s || !/[\u4e00-\u9fff]/.test(s)) continue;
    var el = t.parentElement;
    if (!el) continue;
    var st = getComputedStyle(el);
    if (st.display === 'none' || st.visibility === 'hidden') continue;
    if (skip.test(s)) continue;
    n++;
    if (out.length < 40) {
      var page = el.closest('.page');
      out.push((page ? page.id : '?') + ' :: ' + s.slice(0, 44));
    }
  }
  return JSON.stringify({n: n, sample: out});
})()
"""


def main() -> int:
    import webview
    app = App()
    app.load_mods()
    bridge = WebBridge(app)
    window = webview.create_window(WINDOW_TITLE, str(INDEX), js_api=bridge,
                                   width=1440, height=960, **WINDOW_KWARGS)
    bridge._window = window
    done = threading.Event()
    result: dict = {}

    def run():
        try:
            for _ in range(80):
                if js(window, "!!(window.pywebview && window.pywebview.api)") is True:
                    break
                time.sleep(0.25)
            time.sleep(3.5)
            js(window, "env.applyLanguage('en')")
            time.sleep(3.0)
            result["lang"] = js(window, "Promise.resolve(document.documentElement.lang)")
            result["zh"] = js(window, "Promise.resolve(I18N.t('nav.dash'))")

            pages = js(window, "Promise.resolve(JSON.stringify("
                               "[...document.querySelectorAll('.page')]"
                               ".map(function(p){return p.id;})))")
            ids = json.loads(pages) if isinstance(pages, str) else []
            total = 0
            allsamp: list[str] = []
            for pid in ids:
                js(window, f"(function(){{var n=document.querySelector("
                           f"'.nav[data-p=\"{pid[2:]}\"]'); if(n)n.click();"
                           f" return 1;}})()")
                time.sleep(0.6)
                raw = js(window, PROBE)
                d = json.loads(raw) if isinstance(raw, str) else {"n": 0, "sample": []}
                total += d.get("n", 0)
                allsamp.extend(d.get("sample", []))
            result["remain"] = total
            result["sample"] = allsamp

            # 截几张关键页
            for pid, name in (("dash", "97_en_dash"), ("settings", "98_en_set"),
                              ("solution", "99_en_sol")):
                js(window, f"(function(){{var n=document.querySelector("
                           f"'.nav[data-p=\"{pid}\"]'); if(n)n.click();"
                           f" return 1;}})()")
                time.sleep(1.2)
                shot(window, name)
        except Exception as exc:                  # noqa: BLE001
            import traceback
            print("异常:", exc)
            traceback.print_exc()
        finally:
            done.set()

    def watch():
        done.wait(timeout=420)
        time.sleep(0.5)
        try:
            window.destroy()
        except Exception:
            pass

    window.events.loaded += lambda: threading.Thread(
        target=run, daemon=True).start()
    threading.Thread(target=watch, daemon=True).start()
    webview.start(gui="edgechromium" if sys.platform.startswith("win") else None)

    print("=" * 70)
    print("英文界面验收")
    print("=" * 70)
    print(f"  html lang = {result.get('lang')!r}")
    print(f"  nav.dash  = {result.get('zh')!r}")
    print(f"  可见中文文本节点: {result.get('remain', '?')} 处")
    print()
    print("  按页面统计样例：")
    byp: dict[str, list[str]] = {}
    for s in result.get("sample", []):
        p, _, t = s.partition(" :: ")
        byp.setdefault(p, []).append(t)
    for p in sorted(byp, key=lambda x: -len(byp[x])):
        print(f"    [{p}] {len(byp[p])} 处")
        for t in byp[p][:4]:
            print(f"        {t}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
