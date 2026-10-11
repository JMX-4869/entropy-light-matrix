r"""验证 onclick 属性里的路径转义（崩溃报告"打开文件夹"打不开的根因）。

根因：`onclick="env.openPath(${JSON.stringify(path)})"` 里的反斜杠
在 HTML 属性解析时被吃掉，`C:\\Users\\a` 变成 JS 源码里的
`"C:\Users\a"`，`\U`、`\a` 都不是合法转义 → 路径变成 `C:Usersa`，
资源管理器找不到，而且**不报错**。
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
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                              errors="replace")

from smd.app import App  # noqa: E402
from smd.ui import INDEX, WINDOW_KWARGS, WINDOW_TITLE  # noqa: E402
from smd.webui import WebBridge  # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"  [{'OK' if ok else '!!'}] {name}" + (f" — {detail}" if detail else ""))


def main() -> int:
    import webview
    print("=" * 70)
    print("属性转义 / 打开文件夹")
    print("=" * 70)
    app = App()
    bridge = WebBridge(app)
    window = webview.create_window(WINDOW_TITLE, str(INDEX), js_api=bridge,
                                   width=1300, height=850, **WINDOW_KWARGS)
    bridge._window = window
    done = threading.Event()

    def js(expr, timeout=60):
        slot = f"__p{int(time.time() * 1e6) % 10**9}"
        window.evaluate_js(
            "(function(){window.%s=undefined;Promise.resolve(%s)"
            ".then(function(v){window.%s={ok:1,v:v};})"
            ".catch(function(e){window.%s={ok:0,e:String(e)};});return 1;})()"
            % (slot, expr, slot, slot))
        end = time.time() + timeout
        while time.time() < end:
            raw = window.evaluate_js(f"JSON.stringify(window.{slot}||null)")
            if raw and raw != "null":
                window.evaluate_js(f"delete window.{slot}")
                d = json.loads(raw)
                return d.get("v") if d.get("ok") else {"__error__": d.get("e")}
            time.sleep(0.1)
        return {"__error__": "timeout"}

    # 用一个**通用**的 Windows 路径做样本，不要写开发机上的真实路径：
    # 这个测试要验证的是"反斜杠在属性里会被吃掉"，
    # 与具体是哪台机器无关，而真实路径会泄露个人信息。
    WIN_PATH = r"C:\Users\someone\Documents\Paradox Interactive\Stellaris\logs"

    def work():
        try:
            for _ in range(80):
                if js("!!(window.pywebview && window.pywebview.api)") is True:
                    break
                time.sleep(0.25)
            time.sleep(2.5)

            print("\n--- 1. jsAttr 本身 ---")
            check("jsAttr 已定义",
                  js("Promise.resolve(typeof jsAttr)") == "function")
            out = js(f"Promise.resolve(jsAttr({json.dumps(WIN_PATH)}))")
            check("jsAttr 输出不含裸反斜杠引号",
                  isinstance(out, str) and '\\"' not in out, str(out)[:70])
            back = js("Promise.resolve((function(){"
                      "var d=document.createElement('div');"
                      f"d.innerHTML='<button onclick=\"f('+jsAttr({json.dumps(WIN_PATH)})"
                      "+')\"></button>';"
                      "var b=d.firstChild;"
                      "var src=b.getAttribute('onclick');"
                      "var got=null;"
                      "window.__capture=function(x){got=x;};"
                      "try{ (new Function('f', src))(window.__capture); }"
                      "catch(e){ return 'ERR:'+e; }"
                      "return got;})())")
            check("经过 HTML 属性解析后路径仍是原值", back == WIN_PATH,
                  f"{back!r}")

            print("\n--- 2. 老写法确实会坏（对照） ---")
            bad = js("Promise.resolve((function(){"
                     "var d=document.createElement('div');"
                     f"d.innerHTML='<button onclick=\"f('+JSON.stringify({json.dumps(WIN_PATH)})"
                     "+')\"></button>';"
                     "var b=d.firstChild;"
                     "var src=b.getAttribute('onclick');"
                     "var got=null;"
                     "window.__capture2=function(x){got=x;};"
                     "try{ (new Function('f', src))(window.__capture2); }"
                     "catch(e){ return 'ERR'; }"
                     "return got;})())")
            check("旧的 JSON.stringify 写法确实会把路径弄坏",
                  bad != WIN_PATH, f"{str(bad)[:60]!r}")

            print("\n--- 3. 崩溃报告列表的按钮真的能打开 ---")
            js("(function(){goPage('crash'); return 1;})()")
            time.sleep(2.0)
            n = js("Promise.resolve(document.querySelectorAll("
                   "'#crashList .btn, #p-crash .btn').length)")
            check("崩溃报告页有按钮", isinstance(n, int) and n > 0, f"{n} 个")
            # 直接调后端接口（不真的弹资源管理器）。
            # 空路径必须被**拒绝**：Path("") 会退化成 Path(".")，
            # 于是"打开"的是当前工作目录——看着成功，实则莫名弹一个窗口。
            r = js("api('openPath', '')")
            check("openPath 拒绝空路径并说明原因",
                  isinstance(r, str) and '"ok": false' in r.replace("False", "false")
                  or "没有指定路径" in str(r), str(r)[:70])

            print("\n--- 4. 后端 open_path 对不存在的路径如实报错 ---")
            res = app.open_path(r"C:\definitely\not\here\nope.txt")
            check("不存在时返回 ok=False", res.get("ok") is False,
                  str(res)[:70])
            check("错误信息里带原因",
                  "不存在" in str(res.get("message", "")),
                  str(res.get("message"))[:60])

            err = js("Promise.resolve(String(window.__jserr||''))")
            check("无未捕获 JS 错误", err in ("", None), str(err)[:100])
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
        target=work, daemon=True).start()
    threading.Thread(target=watch, daemon=True).start()
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
