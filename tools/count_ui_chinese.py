"""直接问浏览器：英文模式下当前页面还剩多少可见的界面中文。

不靠静态分析（正则和词法都会误判），只看渲染结果。
排除数据类文本（路径、文件名、模组名、知识库条目）。
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
from shot_pages import js                    # noqa: E402

enter_sandbox()
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                              errors="replace")

from smd.app import App  # noqa: E402
from smd.ui import INDEX, WINDOW_KWARGS, WINDOW_TITLE  # noqa: E402
from smd.webui import WebBridge  # noqa: E402

PROBE = r"""
(function(){
  var NAMES = new Set();
  // 收集模组名与 DLC 名当白名单 —— 它们是数据，出现中文是正常的
  try{
    (S.library.mods||[]).forEach(function(m){ NAMES.add(m.name); });
  }catch(e){}
  var out=[], n=0, data_n=0;
  var w=document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  var t;
  var skip = /[:\\\/]|\.(txt|json|log|mod|exe|png|sqlite|dds|ogg|yml)/;
  while(t=w.nextNode()){
    var s=(t.nodeValue||'').trim();
    if(!s || !/[\u4e00-\u9fff]/.test(s)) continue;
    var el=t.parentElement; if(!el) continue;
    var st=getComputedStyle(el);
    if(st.display==='none'||st.visibility==='hidden') continue;
    var page=el.closest('.page');
    if(page && !page.classList.contains('on')) continue;
    // 数据判定：路径/扩展名，或整段就是某个模组名
    if(skip.test(s) || NAMES.has(s)) { data_n++; continue; }
    n++;
    if(out.length<60) out.push((page?page.id:'?')+' :: '+s.slice(0,46));
  }
  return JSON.stringify({n:n, data:data_n, sample:out});
})()
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
    res: dict = {}

    def run():
        try:
            for _ in range(80):
                if js(win, "!!(window.pywebview && window.pywebview.api)") is True:
                    break
                time.sleep(0.25)
            time.sleep(3.5)
            js(win, "env.applyLanguage('en')")
            time.sleep(3.0)
            ids = js(win, "Promise.resolve(JSON.stringify("
                          "[...document.querySelectorAll('.page')]"
                          ".map(p=>p.id)))")
            ids = json.loads(ids) if isinstance(ids, str) else []
            tot = 0
            data_tot = 0
            samp = []
            for pid in ids:
                js(win, "(function(){var n=document.querySelector("
                        "'.nav[data-p=\"%s\"]'); if(n)n.click(); return 1;})()"
                   % pid[2:])
                time.sleep(0.9)
                raw = js(win, PROBE)
                d = json.loads(raw) if isinstance(raw, str) else {
                    "n": 0, "data": 0, "sample": []}
                tot += d.get("n", 0)
                data_tot += d.get("data", 0)
                samp += d.get("sample", [])
            res["ui"] = tot
            res["data"] = data_tot
            res["sample"] = samp
        except Exception as exc:              # noqa: BLE001
            import traceback
            print("异常:", exc)
            traceback.print_exc()
        finally:
            done.set()

    def watch():
        done.wait(timeout=420)
        time.sleep(0.5)
        try:
            win.destroy()
        except Exception:
            pass

    win.events.loaded += lambda: threading.Thread(
        target=run, daemon=True).start()
    threading.Thread(target=watch, daemon=True).start()
    webview.start(gui="edgechromium" if sys.platform.startswith("win") else None)

    print("=" * 72)
    print("英文模式下的界面中文（浏览器实测）")
    print("=" * 72)
    print(f"  界面中文: {res.get('ui', '?')} 处")
    print(f"  数据中文（模组名/路径，正常）: {res.get('data', '?')} 处")
    print()
    byp: dict[str, list[str]] = {}
    for s in res.get("sample", []):
        p, _, txt = s.partition(" :: ")
        byp.setdefault(p, []).append(txt)
    for p in sorted(byp, key=lambda x: -len(byp[x])):
        print(f"  [{p}] {len(byp[p])} 处")
        for t_ in byp[p][:8]:
            print(f"      {t_}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
