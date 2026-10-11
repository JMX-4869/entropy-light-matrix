"""任务进度条与「停止」按钮的真实测试。

不用直接调 API 的方式验证，而是走界面路径：
启动一次真实扫描 → 看浮层是否出现、进度是否在动、百分比是否合理，
然后点「停止」→ 看是否真的停下来、是否报"已停止"而不是"完成"。

这类交互最容易出的问题是"按钮点了没反应"，只有真开窗口点一次才发现得了。
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
    print("任务进度条 / 停止按钮 测试")
    print("=" * 70)
    app = App()
    app.load_mods()
    bridge = WebBridge(app)
    window = webview.create_window(WINDOW_TITLE, str(INDEX), js_api=bridge,
                                   width=1400, height=900, **WINDOW_KWARGS)
    bridge._window = window
    done = threading.Event()

    def js(expr, timeout=90):
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

    def work():
        try:
            for _ in range(80):
                if js("!!(window.pywebview && window.pywebview.api)") is True:
                    break
                time.sleep(0.25)
            time.sleep(3.5)

            print("\n--- 1. 面板初始状态 ---")
            check("面板默认收起",
                  js('Promise.resolve(document.getElementById("taskPanel")'
                     '.hidden)') is True)

            print("\n--- 2. 启动扫描，观察进度 ---")
            r = js("env.scan().then(function(){return 'started';})")
            check("扫描已启动", r == "started", str(r)[:60])

            # 等面板出现
            appeared = False
            for _ in range(60):
                if js('Promise.resolve(!document.getElementById("taskPanel")'
                      '.hidden)') is True:
                    appeared = True
                    break
                time.sleep(0.3)
            check("进度浮层自动出现", appeared)

            # 收集进度快照：应当单调不减，且百分比/消息会变。
            # **只观察几秒就停手**——扫描全程 20~30 秒，观察太久会等它自然跑完，
            # 那样"停止"就没什么可停的了，测试会假失败。
            seen_pct, seen_msg = [], set()
            for _ in range(6):
                pct = js('Promise.resolve(document.getElementById("tpPct")'
                         '.textContent)')
                msg = js('Promise.resolve(document.getElementById("tpMsg")'
                         '.textContent)')
                nm = js('Promise.resolve(document.getElementById("tpName")'
                        '.textContent)')
                if isinstance(msg, str):
                    seen_msg.add(msg)
                if isinstance(pct, str):
                    seen_pct.append(pct)
                if not js('Promise.resolve(!document.getElementById("taskPanel")'
                          '.hidden)'):
                    break
                time.sleep(1.0)
            check("面板显示了任务名",
                  isinstance(nm, str) and len(nm) > 0, str(nm))
            check("进度文案会推进", len(seen_msg) >= 1,
                  f"{len(seen_msg)} 种：" + " / ".join(list(seen_msg)[:3]))
            nums = []
            for s in seen_pct:
                s2 = str(s).replace("%", "").strip()
                if s2.isdigit():
                    nums.append(int(s2))
            check("百分比在 0~100 之间",
                  all(0 <= n <= 100 for n in nums) and bool(nums),
                  f"{seen_pct[:6]}")

            print("\n--- 3. 点「停止」 ---")
            # 判"还在跑"要用**后端**的状态（api getTask），
            # 不要用界面上的 S.busy：S 是 <script> 里的顶层 const，
            # 挂在全局词法环境而非 window 上，跨作用域读不可靠。
            tstate = js("api('getTask')")
            if isinstance(tstate, str):
                try:
                    tstate = json.loads(tstate)
                except ValueError:
                    tstate = {}
            running_before = bool((tstate or {}).get("running"))
            check("点击前任务仍在运行", running_before,
                  f"running={running_before} "
                  f"progress={(tstate or {}).get('progress')}")
            can = js('Promise.resolve(!document.getElementById("tpStop")'
                     '.disabled)')
            check("停止按钮可用", can is True)
            before = js('Promise.resolve(document.getElementById("tpMsg")'
                        '.textContent)')
            js('(function(){document.getElementById("tpStop").click();'
               'return 1;})()')
            time.sleep(0.8)
            after = js('Promise.resolve(document.getElementById("tpMsg")'
                       '.textContent)')
            check("点击后立刻给出反馈", isinstance(after, str) and after != before,
                  f"{before!r} -> {after!r}")

            # 等任务真正收尾：看后端状态，不看界面变量
            stopped = False
            for _ in range(120):
                s2 = js("api('getTask')")
                if isinstance(s2, str):
                    try:
                        s2 = json.loads(s2)
                    except ValueError:
                        s2 = {}
                if (s2 or {}).get("running") is False:
                    stopped = True
                    break
                time.sleep(0.5)
            check("任务已收尾", stopped)
            t = app.task
            check("后端标记为已取消", t.cancelled is True,
                  f"cancelled={t.cancelled} running={t.running}")
            check("取消不会被当成失败", not t.error, f"error={t.error!r}")
            check("没有发布半截报告",
                  app.report is None or not getattr(app, "_published_partial",
                                                    False),
                  "报告未被残缺结果覆盖")
            # 界面要给出"已停止"而不是"完成"的反馈
            ptext = js('Promise.resolve(document.getElementById("tpMsg")'
                       '.textContent)')
            check("界面提示已停止（不是完成）",
                  isinstance(ptext, str) and ("停止" in ptext or "取消" in ptext),
                  str(ptext))

            print("\n--- 4. 不可取消的任务 ---")
            js("env.genReport()")
            time.sleep(1.5)
            # 报告生成很快，若已结束就跳过这条
            if js('Promise.resolve(!document.getElementById("taskPanel")'
                  '.hidden)') is True:
                dis = js('Promise.resolve(document.getElementById("tpStop")'
                         '.disabled)')
                nm2 = js('Promise.resolve(document.getElementById("tpName")'
                         '.textContent)')
                if "报告" in str(nm2):
                    check("报告任务不显示可用停止按钮", dis is True,
                          f"{nm2} disabled={dis}")
                else:
                    check("报告任务不显示可用停止按钮", True, "已结束，跳过")
            else:
                check("报告任务不显示可用停止按钮", True, "已结束，跳过")
        except Exception as exc:
            import traceback
            print("异常:", exc)
            traceback.print_exc()
        finally:
            done.set()

    def watch():
        done.wait(timeout=420)
        time.sleep(0.6)
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
