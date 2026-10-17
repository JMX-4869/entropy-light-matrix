"""渲染验证：模组页的播放集选择器与重命名。

省时间只截模组页，并额外把接口的真实返回打出来 ——
截图只能看出"有没有渲染"，看不出数据对不对。
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

SHOTS = ROOT / "tools" / "shots"


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
            time.sleep(3.0)
            js(win, "(function(){var n=document.querySelector('.nav[data-p=\"mods\"]'); if(n)n.click(); return 1;})()")
            time.sleep(2.0)

            # 选择器的真实内容
            res["options"] = js(win, r"""
Promise.resolve((function(){
  var s=document.getElementById('playsetSel');
  if(!s) return 'NO SELECT';
  return JSON.stringify({
    disabled: s.disabled,
    count: s.options.length,
    opts: [...s.options].map(o=>({v:o.value, t:o.textContent})),
    note: (document.getElementById('playsetBar')||{}).textContent || '',
    noteShown: (document.getElementById('playsetBar')||{}).style.display
  });
})())""")

            # 直接问后端接口
            res["api"] = js(win, "window.pywebview.api.listPlaysets()")

            js(win, "(function(){window.__shot=1; return 1;})()")
            SHOTS.mkdir(parents=True, exist_ok=True)
            js(win, "1")
            time.sleep(0.6)
        except Exception as exc:              # noqa: BLE001
            print("异常:", exc, file=out)
        finally:
            done.set()

    def watch():
        done.wait(timeout=300)
        time.sleep(0.4)
        try:
            # pywebview 没有截图 API —— 复用 shot_pages 里的 Win32 抓窗口
            from shot_pages import shot as _shot
            _shot(win, "playset_ui")
        except Exception as exc:              # noqa: BLE001
            print("截图失败:", exc, file=out)
        try:
            win.destroy()
        except Exception:
            pass

    win.events.loaded += lambda: threading.Thread(
        target=run, daemon=True).start()
    threading.Thread(target=watch, daemon=True).start()
    webview.start(gui="edgechromium" if sys.platform.startswith("win") else None)

    print("=" * 68, file=out)
    print("播放集界面验证", file=out)
    print("=" * 68, file=out)
    try:
        opts = json.loads(res.get("options") or "{}")
        print(f"  选择器: {opts.get('count')} 项  禁用={opts.get('disabled')}",
              file=out)
        for o in opts.get("opts") or []:
            print(f"      {o['t']}   (value={o['v'][:8]}…)", file=out)
        print(f"  提示行显示: {opts.get('noteShown')}", file=out)
        print(f"  提示行内容: {opts.get('note')}", file=out)
    except Exception as exc:                  # noqa: BLE001
        print(f"  解析选择器失败: {exc}\n  {res.get('options')!r}", file=out)
    print(file=out)
    api_raw = res.get("api")
    try:
        d = json.loads(api_raw) if isinstance(api_raw, str) else api_raw
        print(f"  接口 listPlaysets: {len(d.get('playsets') or [])} 个，"
              f"选中={str(d.get('selected'))[:8]}… 活动={str(d.get('active'))[:8]}…",
              file=out)
    except Exception:                         # noqa: BLE001
        print(f"  接口返回: {str(api_raw)[:200]}", file=out)
    print(f"\n  截图: {SHOTS / 'playset_ui.png'}", file=out)
    out.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
