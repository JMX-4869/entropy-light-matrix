"""审计界面里在英文模式下会残留的裸中文。

判断方式：把 HTML 里所有**带 data-i18n 的元素**临时打上标记，
然后在浏览器里切到英文，扫描可见文本中是否还有 CJK 字符。
静态分析不可靠（onclick 里的参数、动态拼接的值都会误判），
所以用真实 DOM 实测。

用法::  python tools/i18n_audit.py
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
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                              errors="replace")

from smd.app import App  # noqa: E402
from smd.ui import INDEX, WINDOW_KWARGS, WINDOW_TITLE  # noqa: E402
from smd.webui import WebBridge  # noqa: E402

# 在页面里跑：遍历所有可见元素，收集含 CJK 的文本，
# 但跳过**自身或祖先带 data-i18n** 的节点（那些会被翻译机制接管）。
PROBE = r"""
(function(){
  var out = [];
  var seen = new Set();
  function hasCJK(s){ return /[\u4e00-\u9fff]/.test(s||''); }
  function owned(el){
    // 自己或任一祖先是 data-i18n 元素 → 归翻译机制管
    for(var n = el; n && n.nodeType === 1; n = n.parentElement){
      if(n.hasAttribute && n.hasAttribute('data-i18n')) return true;
    }
    return false;
  }
  var walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  var node;
  while(node = walker.nextNode()){
    var txt = (node.nodeValue||'').trim();
    if(!txt || !hasCJK(txt)) continue;
    var el = node.parentElement;
    if(!el) continue;
    if(owned(el)) continue;                      // 翻译机制负责
    var st = getComputedStyle(el);
    if(st.display === 'none' || st.visibility === 'hidden') continue;
    var page = el.closest('.page');
    var key = txt.slice(0, 40) + '|' + (page ? page.id : '?');
    if(seen.has(key)) continue;
    seen.add(key);
    out.push({
      text: txt.slice(0, 60),
      page: page ? page.id : '(全局)',
      tag: el.tagName.toLowerCase(),
      cls: (el.className||'').toString().slice(0, 40)
    });
  }
  return JSON.stringify(out);
})()
"""


def main() -> int:
    import webview
    app = App()
    app.load_mods()                     # 让各页有内容可渲染
    bridge = WebBridge(app)
    window = webview.create_window(WINDOW_TITLE, str(INDEX), js_api=bridge,
                                   width=1400, height=950, **WINDOW_KWARGS)
    bridge._window = window
    done = threading.Event()
    found: list[dict] = []

    def run():
        try:
            for _ in range(80):
                if js(window, "!!(window.pywebview && window.pywebview.api)") is True:
                    break
                time.sleep(0.25)
            time.sleep(3.5)
            js(window, "env.applyLanguage('en')")
            time.sleep(2.5)

            # 逐页扫描：只切页面（不走会因为数据未就绪而报错的入口）
            pages = js(window, "Promise.resolve(JSON.stringify("
                               "[...document.querySelectorAll('.page')]"
                               ".map(function(p){return p.id;})))")
            try:
                page_ids = json.loads(pages) if isinstance(pages, str) else pages
            except (TypeError, ValueError):
                page_ids = []
            print(f"  共 {len(page_ids)} 个页面")

            for pid in page_ids or []:
                js(window, f"(function(){{var n=document.querySelector("
                           f"'.nav[data-p=\"{pid[2:]}\"]'); if(n)n.click(); return 1;}})()")
                time.sleep(0.7)
                raw = js(window, PROBE)
                try:
                    rows = json.loads(raw) if isinstance(raw, str) else (raw or [])
                except (TypeError, ValueError):
                    rows = []
                for r in rows:
                    if isinstance(r, dict):
                        found.append(r)
            print(f"  英文模式下残留的裸中文：{len(found)} 处")
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

    # 汇总
    by_page: dict[str, list[str]] = {}
    for r in found:
        by_page.setdefault(r.get("page", "?"), [])
        if r.get("text") not in by_page[r["page"]]:
            by_page[r["page"]].append(r.get("text", ""))
    print()
    for page in sorted(by_page):
        items = by_page[page]
        print(f"  [{page}] {len(items)} 处")
        for s in items[:12]:
            print(f"      {s}")
        if len(items) > 12:
            print(f"      …还有 {len(items) - 12} 处")
    out = ROOT / "tools" / "_i18n_audit.json"
    out.write_text(json.dumps(found, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    print(f"\n  明细已写入 {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
