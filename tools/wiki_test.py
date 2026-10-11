"""时之虫档案：待送队列可编辑 + 检索 + 分类浏览的界面测试。

队列是这次改动的核心：
* 放在页面**最上面**（用户要求的）
* 每一行**可以直接编辑**
* 加了/删了/改了都要立刻反映到计数与送出的内容上
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
    print("时之虫档案")
    print("=" * 70)

    # ---------- 后端 ----------
    app = App()
    print("\n--- 1. 档案数据 ---")
    h = app.wiki_home()
    m = h["meta"]
    check("档案已加载", m.get("entries", 0) > 1000, f"{m.get('entries')} 条")
    check("分类齐全", len(h["categories"]) >= 10, f"{len(h['categories'])} 类")
    check("带出可送命令", len(h["commands"]) > 100, f"{len(h['commands'])} 条")
    check("标注了数据来源", bool(m.get("source")), str(m.get("source"))[:40])
    check("标注了适配版本", m.get("version") == "4.2.4", str(m.get("version")))

    print("\n--- 2. 检索 ---")
    r = app.wiki_search("energy")
    check("检索有结果", r["total"] > 0, f"energy → {r['total']} 条")
    check("结果带分类名", all(x.get("section_title") for x in r["hits"][:5]))
    check("完全匹配排最前", r["hits"][0]["score"] >= 40,
          f"score={r['hits'][0]['score']}")
    r2 = app.wiki_search("巨构 完成")
    check("多关键词是 AND（全部命中）",
          all(all(t in " ".join(x["cells"]).lower() for t in ("巨构", "完成"))
              for x in r2["hits"][:5]) if r2["hits"] else False,
          f"{r2['total']} 条")
    r3 = app.wiki_search("")
    check("空查询不返回结果", r3["total"] == 0)

    print("\n--- 3. 分类分页 ---")
    sec = app.wiki_section("modifier", 0, 50)
    check("能取到分类行", sec["ok"] and len(sec["rows"]) == 50,
          f"{len(sec.get('rows') or [])} / {sec.get('total')}")
    check("分页标记正确", sec["has_more"] is True)
    sec2 = app.wiki_section("modifier", sec["total"] - 5, 50)
    check("末页 has_more 为假", sec2["has_more"] is False,
          f"offset={sec2['offset']} 取到 {len(sec2['rows'])}")
    check("不存在的分类报错",
          app.wiki_section("nope")["ok"] is False)

    # ---------- 界面 ----------
    print("\n--- 4. 界面：队列在页面最上面且可编辑 ---")
    bridge = WebBridge(app)
    window = webview.create_window(WINDOW_TITLE, str(INDEX), js_api=bridge,
                                   width=1400, height=950, **WINDOW_KWARGS)
    bridge._window = window
    done = threading.Event()

    def js(expr, timeout=60):
        slot = f"__a{int(time.time() * 1e6) % 10**9}"
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
            js("(function(){goPage('console'); return 1;})()")
            time.sleep(3.0)

            check("页面标题是「时之虫档案」",
                  js("Promise.resolve(document.querySelector("
                     "'#p-console h1').innerText)") == "时之虫档案")
            sub = js("Promise.resolve(document.getElementById('wikiSub')"
                     ".innerText)")
            check("副标题显示条目数与来源",
                  "8689" in str(sub) and "4.2.4" in str(sub), str(sub)[:70])

            # 队列必须是页面上第一个 card
            first = js("Promise.resolve(document.querySelector("
                       "'#p-console .card').id)")
            check("待送队列是页面第一个卡片", first == "conQueueCard",
                  str(first))

            check("队列在无命令时也可见（不是隐藏的）",
                  js("Promise.resolve(document.getElementById("
                     "'conQueueCard').style.display!=='none')") is True)

            print("  · 加入命令")
            js("(function(){queueAdd('energy 100000');"
               "queueAdd('debugtooltip');return 1;})()")
            time.sleep(0.8)
            check("队列出现两行",
                  js("Promise.resolve(document.querySelectorAll("
                     "'#conQueueList .qRow').length)") == 2)
            check("每行都是输入框（可编辑）",
                  js("Promise.resolve(document.querySelectorAll("
                     "'#conQueueList .qIn').length)") == 2)
            check("计数跟着变",
                  js("Promise.resolve(document.getElementById('conCount')"
                     ".textContent)") == "2")

            print("  · 编辑第一行")
            js("(function(){var i=document.querySelector("
               "'#conQueueList .qIn');i.value='unity 999';"
               "i.dispatchEvent(new Event('input'));return 1;})()")
            time.sleep(0.5)
            check("编辑后输入框内容已变",
                  js("Promise.resolve(document.querySelector("
                     "'#conQueueList .qIn').value)") == "unity 999")
            check("编辑后队列数据同步",
                  "unity 999" in str(js("Promise.resolve(WIKI.queue.join('|'))")),
                  str(js("Promise.resolve(WIKI.queue.join('|'))"))[:50])

            print("  · 删除第二行")
            js("(function(){document.querySelectorAll('#conQueueList .qDel')"
               "[1].click();return 1;})()")
            time.sleep(0.6)
            check("删除后只剩一行",
                  js("Promise.resolve(document.querySelectorAll("
                     "'#conQueueList .qRow').length)") == 1)
            check("计数跟着减",
                  js("Promise.resolve(document.getElementById('conCount')"
                     ".textContent)") == "1")

            print("  · 手动加一条")
            js("(function(){var e=document.getElementById('conAdd');"
               "e.value='own';consoleAddManual();return 1;})()")
            time.sleep(0.6)
            check("手动添加成功",
                  js("Promise.resolve(document.querySelectorAll("
                     "'#conQueueList .qRow').length)") == 2)
            check("输入框已清空",
                  js("Promise.resolve(document.getElementById('conAdd')"
                     ".value)") == "")

            print("  · 空行不计入送出")
            js("(function(){var i=document.querySelectorAll("
               "'#conQueueList .qIn')[1];i.value='   ';"
               "i.dispatchEvent(new Event('input'));return 1;})()")
            time.sleep(0.5)
            check("全空白的行会被过滤掉",
                  js("Promise.resolve(queueCommands().length)") == 1,
                  str(js("Promise.resolve(queueCommands().join('|'))")))

            print("  · 检索")
            js("(function(){queueClearAll();"
               "document.getElementById('wikiQuery').value='energy';"
               "wikiDoSearch();return 1;})()")
            time.sleep(2.5)
            hit = js("Promise.resolve(document.getElementById('wikiHit')"
                     ".innerText)")
            check("检索有命中提示", "命中" in str(hit), str(hit))
            n = js("Promise.resolve(document.querySelectorAll("
                   "'#wikiResults .opt').length)")
            check("结果已渲染", isinstance(n, int) and n > 0, f"{n} 条")
            b = js("Promise.resolve(document.querySelectorAll("
                   "'#wikiResults .btn').length)")
            check("结果行带「加入」按钮", isinstance(b, int) and b > 0,
                  f"{b} 个")

            print("  · 从检索结果加入队列")
            js("(function(){var b=document.querySelector("
               "'#wikiResults .btn'); b.click(); return 1;})()")
            time.sleep(0.8)
            check("可以从检索结果直接加入队列",
                  js("Promise.resolve(WIKI.queue.length)") == 1,
                  str(js("Promise.resolve(WIKI.queue.join('|'))"))[:50])

            print("  · 分类浏览")
            js("(function(){wikiOpen('tech'); return 1;})()")
            time.sleep(2.5)
            check("分类行已渲染",
                  js("Promise.resolve(document.querySelectorAll("
                     "'#wikiRows .opt').length)") > 0)
            check("分类说明已显示",
                  isinstance(js("Promise.resolve(document.getElementById("
                                "'wikiSectionHead').innerText)"), str))

            err = js("Promise.resolve(String(window.__jserr||''))")
            check("无未捕获 JS 错误", err in ("", None), str(err)[:120])
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
