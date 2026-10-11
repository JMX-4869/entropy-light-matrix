"""AI 分析链路测试（崩溃 / 日志 / 加载顺序 / 补丁工坊）。

不调用真实 AI：本机没开隐私开关，真调用会被闸门拦住。
这里验证的是**链路完整性**：

1. 隐私闸门对三个入口一致生效（返回 kind=consent，不是静默失败）
2. 崩溃数据能被正确整理成可分析的文本
3. 问题库能分组、能标记已解决、已解决的不再显示
4. 界面：四个页面的 AI 按钮与结论容器都在，且不报 JS 错
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

from smd.ai import analyze as A  # noqa: E402
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
    print("AI 分析链路")
    print("=" * 70)
    app = App()

    print("\n--- 1. 隐私闸门三入口一致 ---")
    for name, fn in (("analyze", lambda: A.analyze(app.settings, kind="t",
                                                   payload="x")),
                     ("make_patch", lambda: A.make_patch(app.settings,
                                                         title="t",
                                                         detail="d")),
                     ("make_theme", lambda: A.make_theme(app.settings, "dark"))):
        r = fn()
        check(f"{name} 未授权时明确拒绝",
              r.get("ok") is False and r.get("kind") in ("consent", "config"),
              f"ok={r.get('ok')} kind={r.get('kind')}")

    print("\n--- 2. JSON 容错 ---")
    for t, want in (('{"a":1}', {"a": 1}),
                    ('```json\n{"a":2}\n```', {"a": 2}),
                    ('前言 {"a":3} 后语', {"a": 3}),
                    ("完全不是 JSON", None)):
        check(f"解析 {t[:22]!r}", A.parse_json_loose(t) == want,
              str(A.parse_json_loose(t))[:30])

    print("\n--- 3. 崩溃数据整理 ---")
    p = app.crash_payload()
    check("能定位到崩溃目录", p.get("ok") is True, str(p.get("error", "")))
    if p.get("ok"):
        txt = p.get("text", "")
        check("载荷非空且有内容", len(txt) > 200, f"{len(txt)} 字符")
        check("载荷里带模组清单或调用栈",
              ("模组" in txt) or ("调用栈" in txt), txt[:50].replace("\n", " "))

    print("\n--- 4. 问题库：分组 / 去重 / 已解决隐藏 ---")
    fake = {
        "ok": True, "summary": "测试", "parsed": True,
        "issues": [
            {"key": "crash:x:a", "title": "问题 A", "severity": "critical",
             "cause": "因为 X", "fix": "改成 Y", "target": "a",
             "ai_patchable": True, "source": "崩溃报告",
             "source_ref": "crash1"},
            {"key": "crash:x:b", "title": "问题 B", "severity": "low",
             "cause": "因为 Z", "fix": "", "target": "b",
             "ai_patchable": False, "source": "崩溃报告",
             "source_ref": "crash1"},
        ],
    }
    n = app._ai_merge(fake, group="崩溃 · crash1")
    check("新增 2 条", n == 2, f"{n} 条")
    groups = app.ai_issue_groups_visible()
    check("按来源分成 1 组", len(groups) == 1, f"{len(groups)} 组")
    check("组内按严重度排序",
          groups[0]["issues"][0]["title"] == "问题 A",
          str([x["title"] for x in groups[0]["issues"]]))
    check("统计出可补丁条数", groups[0]["patchable"] == 1,
          f"{groups[0]['patchable']} 条")

    # 重复合并应当去重
    n2 = app._ai_merge(fake, group="崩溃 · crash1")
    check("重复分析不重复计数", n2 == 0, f"新增 {n2}")
    check("总数仍是 2", len(app._ai_issues()) == 2)

    r = app.ai_resolve_issues(["crash:x:a"])
    check("能标记已解决", r.get("resolved") == 1, str(r.get("resolved")))
    left = app.ai_issue_groups_visible()
    check("已解决的不再显示",
          all(x["title"] != "问题 A" for g in left for x in g["issues"]),
          f"{sum(g['count'] for g in left)} 条可见")
    check("但底层记录还在（可复查）", len(app._ai_issues()) == 2)

    print("\n--- 5. 界面上四个页面的入口 ---")
    bridge = WebBridge(app)
    window = webview.create_window(WINDOW_TITLE, str(INDEX), js_api=bridge,
                                   width=1400, height=950, **WINDOW_KWARGS)
    bridge._window = window
    done = threading.Event()

    def js(expr, timeout=60):
        slot = f"__g{int(time.time() * 1e6) % 10**9}"
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
            time.sleep(3.0)

            js("(function(){goPage('crash'); return 1;})()")
            time.sleep(2.0)
            check("崩溃页有 AI 分析按钮",
                  js("Promise.resolve(!!document.querySelector("
                     "'#p-crash .btn[onclick*=\"aiAnalyzeCrash\"]'))") is True)
            check("崩溃页有结论容器",
                  js("Promise.resolve(!!document.getElementById("
                     "'aiIssueBox'))") is True)
            n_row_btn = js("Promise.resolve(document.querySelectorAll("
                           "'#crashBody .btn').length)")
            check("崩溃行有「打开文件夹」按钮",
                  isinstance(n_row_btn, int) and n_row_btn >= 2,
                  f"{n_row_btn} 个按钮")

            js("(function(){goPage('order'); return 1;})()")
            time.sleep(1.8)
            check("加载顺序页有 AI 排序按钮",
                  js("Promise.resolve(!!document.querySelector("
                     "'#p-order .btn[onclick*=\"aiAnalyzeOrder\"]'))") is True)
            check("加载顺序页有结论容器",
                  js("Promise.resolve(!!document.getElementById("
                     "'aiIssueBoxOrder'))") is True)

            js("(function(){goPage('logs'); return 1;})()")
            time.sleep(1.8)
            check("日志页有 AI 分析按钮",
                  js("Promise.resolve(!!document.querySelector("
                     "'#p-logs .btn[onclick*=\"aiAnalyzeLog\"]'))") is True)

            js("(function(){goPage('patch'); patchMode('batch'); return 1;})()")
            time.sleep(2.5)
            check("补丁工坊有 AI 结论容器",
                  js("Promise.resolve(!!document.getElementById("
                     "'aiIssueBoxPatch'))") is True)
            # 之前标记为已解决的那条不该出现在界面上
            txt = js("Promise.resolve(document.getElementById('aiIssueBoxPatch')"
                     ".innerText)")
            check("界面不显示已解决的问题",
                  isinstance(txt, str) and "问题 A" not in txt,
                  str(txt)[:60].replace("\n", " "))
            check("但显示未解决的问题",
                  isinstance(txt, str) and "问题 B" in txt,
                  str(txt)[:60].replace("\n", " "))

            err = js("Promise.resolve(String(window.__jserr||''))")
            check("无未捕获 JS 错误", err in ("", None), str(err)[:100])
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
