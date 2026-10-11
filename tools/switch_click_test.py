"""真实点击测试：用 DOM click 模拟用户点开关，而不是直接调 API。

背景：``.sw input{display:none}`` 让 ``<i>`` 成了 input 的**兄弟**节点，
点 ``<i>`` 不会转发给被隐藏的 checkbox。之前的测试都是直接调
``env.aiSet(...)`` / ``env.toggleMod(...)``，绕过了真实点击，
所以这个 bug 一直没被抓到。
"""
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

enter_sandbox()
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from smd.app import App  # noqa: E402
from smd.ui import INDEX, WINDOW_KWARGS, WINDOW_TITLE  # noqa: E402
from smd.webui import WebBridge  # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"  [{'OK' if ok else '!!'}] {name}" + (f" — {detail}" if detail else ""))


def js(window, expr, timeout=30.0):
    slot = f"__c_{int(time.time() * 1e6) % 10**9}"
    window.evaluate_js(
        "(function(){\n"
        f"window.{slot}=undefined;\n"
        f"Promise.resolve({expr})\n"
        f".then(function(v){{window.{slot}={{ok:1,v:v}};}})\n"
        f".catch(function(e){{window.{slot}={{ok:0,e:String(e)}};}});\n"
        "return 1;\n})()")
    end = time.time() + timeout
    while time.time() < end:
        raw = window.evaluate_js(f"JSON.stringify(window.{slot}||null)")
        if raw and raw != "null":
            window.evaluate_js(f"delete window.{slot}")
            b = json.loads(raw)
            return b.get("v") if b.get("ok") else {"__error__": b.get("e")}
        time.sleep(0.1)
    return {"__error__": "timeout"}


def main() -> int:
    print("=" * 70)
    print("开关真实点击测试")
    print("=" * 70)
    app = App()
    app.load_mods()
    # 这个测试会点「允许发送模组内容」开关，而那是**会写盘**的。
    #
    # 原值一律还原成 False，而不是"还原成进来时的样子"：
    # 隐私开关的正确默认值就是关闭，一旦被哪个用例泄漏成 True，
    # 「原样还原」会把这个错误值继续保留下去，
    # 后面的 bridge_test 断言"隐私默认关闭"就会一直失败（真实发生过）。
    # 固定收敛到 False 才能保证测试之间互不影响。
    _allow_before = app.settings.ai.allow_send_mod_content
    print(f"（进来时 允许发送模组内容 = {_allow_before}，结束时统一还原为 False）")
    try:
        return _run(app)
    finally:
        try:
            app.settings.ai.allow_send_mod_content = False
            app.settings.save()
            print("\n[清理] 允许发送模组内容 已还原为 False")
        except Exception as exc:
            print(f"\n[清理] 还原失败：{exc}")


def _run(app) -> int:
    bridge = WebBridge(app)
    import webview
    window = webview.create_window(WINDOW_TITLE, str(INDEX), js_api=bridge,
                                   width=1400, height=880, **WINDOW_KWARGS)
    bridge._window = window
    done = threading.Event()

    def worker():
        try:
            for _ in range(80):
                if js(window, "!!(window.pywebview && window.pywebview.api)") is True:
                    break
                time.sleep(0.25)
            for _ in range(60):
                if js(window, "Promise.resolve((S.mods||[]).length)") not in (0, None):
                    break
                time.sleep(0.5)

            print("\n--- 1. AI 隐私开关：点可见的滑块 ---")
            js(window, "(function(){goPage('ai'); return 1;})()")
            time.sleep(2.0)
            # 先把开关**归零**再测，否则结果取决于进来时的状态，
            # 断言就会时对时错（"翻一下"和"翻到 True"是两回事）。
            js(window, "(function(){var c=document.getElementById('aiAllow');"
                       "if(c.checked){c.click();} return 1;})()")
            time.sleep(1.0)
            before = js(window, "Promise.resolve(document.getElementById('aiAllow').checked)")
            # 关键：点用户真正会点的那个可见滑块。
            # .sw 里的 input 现在铺满整块并自绘成滑块，
            # 所以它既是可见外观也是命中区域。
            r = js(window, "(function(){"
                          "var inp=document.getElementById('aiAllow');"
                          "if(!inp) return 'no-input';"
                          "inp.click();"
                          "return 'clicked';})()")
            time.sleep(1.2)
            after = js(window, "Promise.resolve(document.getElementById('aiAllow').checked)")
            check("点滑块能切换开关", r == "clicked" and before is False
                  and after is True,
                  f"点击={r} 之前={before} 之后={after}")

            # 再点一次关掉
            js(window, "(function(){document.getElementById('aiAllow').click();"
                       "return 1;})()")
            time.sleep(1.2)
            after2 = js(window, "Promise.resolve(document.getElementById('aiAllow').checked)")
            check("再点一次能关掉", after2 is False, f"现在={after2}")

            print("\n--- 2. 模组列表滑块 ---")
            js(window, "(function(){goPage('mods'); return 1;})()")
            time.sleep(2.0)
            res = js(window, "(function(){"
                            "var inp=document.querySelector("
                            "'#modsBody .sw input:not([disabled])');"
                            "if(!inp) return 'none';"
                            "var st=inp.checked; inp.click();"
                            "return JSON.stringify({before:st, after:inp.checked});"
                            "})()")
            time.sleep(1.2)
            try:
                d = json.loads(res) if isinstance(res, str) and res.startswith("{") else {}
            except ValueError:
                d = {}
            check("点模组滑块能切换", bool(d) and d.get("before") != d.get("after"),
                  str(res)[:70])

            print("\n--- 3. 设置页开关 ---")
            js(window, "(function(){goPage('settings'); return 1;})()")
            time.sleep(1.8)
            res2 = js(window, "(function(){"
                             "var inp=document.getElementById('setGhost');"
                             "if(!inp) return 'none';"
                             "var st=inp.checked; inp.click();"
                             "return JSON.stringify({before:st, after:inp.checked});"
                             "})()")
            time.sleep(1.2)
            try:
                d2 = json.loads(res2) if isinstance(res2, str) and res2.startswith("{") else {}
            except ValueError:
                d2 = {}
            check("点设置页开关能切换",
                  bool(d2) and d2.get("before") != d2.get("after"), str(res2)[:70])
        except Exception as exc:
            import traceback
            print("异常:", exc)
            traceback.print_exc()
        finally:
            done.set()

    def watcher():
        done.wait(timeout=300)
        time.sleep(0.6)
        try:
            window.destroy()
        except Exception:
            pass

    window.events.loaded += lambda: threading.Thread(
        target=worker, daemon=True).start()
    threading.Thread(target=watcher, daemon=True).start()
    webview.start(gui="edgechromium" if sys.platform.startswith("win") else None)

    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print()
    print("=" * 70)
    print(f"结果：{passed}/{total} 通过")
    for n, ok, d in results:
        if not ok:
            print(f"  失败：{n} — {d}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
