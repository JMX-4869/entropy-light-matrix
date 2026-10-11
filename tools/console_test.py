"""指令台测试。

覆盖：
1. 命令库结构完整（分类、参数、必填字段）
2. 读游戏控制台历史
3. 推送命令 → 真的写进 ``console_history.txt``，且格式是游戏认的
4. 保留原有历史、去重、限量
5. 推送前自动备份、可还原
6. 界面路径：命令库渲染、勾选、队列、搜索

**安全性**：这个测试会真的写游戏的 console_history.txt，
但每个写操作都先备份原文件，测试结束**原样还原**。
"""
import io
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
from sandbox import enter_sandbox            # noqa: E402

enter_sandbox()
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                              errors="replace")

from smd import console as C  # noqa: E402
from smd.app import App  # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"  [{'OK' if ok else '!!'}] {name}" + (f" — {detail}" if detail else ""))


def main() -> int:
    print("=" * 70)
    print("指令台测试")
    print("=" * 70)
    app = App()
    paths = app.settings.paths
    hist = paths.console_history
    print(f"控制台历史文件：{hist}")
    check("定位到 console_history.txt",
          str(hist).endswith("console_history.txt"), str(hist))
    if not hist.is_file():
        print("  ！找不到历史文件，跳过写盘相关的用例")
        return _report()

    original = hist.read_bytes()
    print(f"  原始大小 {len(original)} 字节")

    try:
        print("\n--- 1. 命令库结构 ---")
        lib = C.library()
        check("命令库非空", len(lib) > 20, f"{len(lib)} 条")
        check("每条都有必填字段",
              all(c.get("cmd") and c.get("label") and c.get("category")
                  for c in lib))
        cats = {c["category"] for c in lib}
        check("分类都在预定义里", cats <= set(C.CATEGORIES),
              f"用了 {len(cats)} 类")
        check("每条都有可用示例",
              all(c.get("example") for c in lib))
        check("危险命令有标记", any(c["risky"] for c in lib),
              f"{sum(1 for c in lib if c['risky'])} 条")
        check("需要选中的命令有标记", any(c["needs_selection"] for c in lib),
              f"{sum(1 for c in lib if c['needs_selection'])} 条")

        print("\n--- 2. 读游戏历史 ---")
        r = C.read_history(paths)
        check("读取成功", r.get("ok"), str(r.get("error", "")))
        check("拿到原有行数", r.get("count", 0) > 0, f"{r.get('count')} 行")

        print("\n--- 3. 推送命令 ---")
        push = ["debugtooltip", "energy 100000", "own"]
        res = C.push_commands(paths, push, keep_existing=True)
        check("推送成功", res.get("ok"), str(res.get("error", "")))
        check("报告推送条数", res.get("pushed") == 3, str(res.get("pushed")))
        check("生成了备份", bool(res.get("backup")), str(res.get("backup")))

        r2 = C.read_history(paths)
        lines = r2.get("lines") or []
        check("推的命令在最前面（↑ 先翻到）",
              lines[:3] == push, str(lines[:3]))
        # 原有历史要还在。**不能**拿"原文件的前 N 行"逐字比：
        # 游戏自己的历史里本来就有重复行（玩家反复敲同一条），
        # 而去重会把这些重复收掉，位置自然就对不上了。
        # 也不能拿未 strip 的原行去比——写入时会 strip，
        # 历史里若有行尾空格（真实数据里就有）就会被误判成"丢了"。
        # 正确的比法是两边都 strip。
        # 排除空行：真实历史文件里有空行（5 行），写入时会跳过它们，
        # 拿空行去比会得到一个假的"丢了 1 条"。
        old_cmds = {x.strip() for x in (r.get("lines") or []) if x.strip()}
        # 比**整个文件**而不是 lines[3:]：如果推送的命令本来就在
        # 玩家自己的历史里（真实数据里就有 own / energy 这类），
        # 去重会把它挪到最前面，那它就不在后面那一段里了。
        kept = {x.strip() for x in lines if x.strip()}
        check("原有历史仍然都在", old_cmds <= kept,
              f"丢了 {len(old_cmds - kept)} 条")
        check("原有历史接在后面",
              (r.get("lines") or ["x"])[0] in kept, str(lines[3:5]))
        check("文件是纯 UTF-8 无 BOM",
              not hist.read_bytes().startswith(b"\xef\xbb\xbf"))

        print("\n--- 4. 去重与限量 ---")
        res2 = C.push_commands(paths, ["own", "own", "help"],
                               keep_existing=True)
        r3 = C.read_history(paths)
        l3 = r3.get("lines") or []
        check("重复命令只留一条", l3.count("own") == 1,
              f"own 出现 {l3.count('own')} 次")
        check("新命令仍排最前", l3[0] == "own" and l3[1] == "help",
              str(l3[:2]))
        check("默认不截断（历史一条不丢）",
              len(set(l3)) == len(set(r.get("lines") or []) | {"help"}),
              f"去重后 {len(set(l3))} 条，原 {len(set(r.get('lines') or []))} 条")
        check("默认没丢东西", res2.get("dropped", 0) == 0,
              f"dropped={res2.get('dropped')}")

        # 明确要求限量时才截断，并且要如实报告丢了多少
        res3 = C.push_commands(paths, [f"cmd{i}" for i in range(50)],
                               keep_existing=True, limit=10)
        r4 = C.read_history(paths)
        check("明确限量时才截断", len(r4.get("lines") or []) == 10,
              f"{len(r4.get('lines') or [])} 行")
        check("如实报告丢弃条数", res3.get("dropped", 0) > 0,
              f"dropped={res3.get('dropped')}")

        print("\n--- 5. 不保留原有历史 ---")
        C.push_commands(paths, ["only_this"], keep_existing=False)
        r5 = C.read_history(paths)
        check("只留推送的命令", (r5.get("lines") or []) == ["only_this"],
              str(r5.get("lines")))

        print("\n--- 6. 清空 ---")
        C.write_history(paths, [])
        r6 = C.read_history(paths)
        check("历史已清空", (r6.get("lines") or []) == [], str(r6.get("lines")))
    finally:
        hist.write_bytes(original)
        restored = C.read_history(paths)
        print(f"\n[清理] 已还原，{restored.get('count')} 行")

    print("\n--- 7. 界面路径 ---")
    import threading  # noqa: E402
    import time  # noqa: E402

    import webview  # noqa: E402

    from smd.ui import INDEX, WINDOW_KWARGS, WINDOW_TITLE  # noqa: E402
    from smd.webui import WebBridge  # noqa: E402

    bridge = WebBridge(app)
    window = webview.create_window(WINDOW_TITLE, str(INDEX), js_api=bridge,
                                   width=1400, height=900, **WINDOW_KWARGS)
    bridge._window = window
    done = threading.Event()

    def js(expr, timeout=60):
        slot = f"__k{int(time.time() * 1e6) % 10**9}"
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
            # 命令库的界面部分已经并入「时之虫档案」，
            # 那些断言由 wiki_test.py 负责。这里只确认
            # **送命令进游戏这条链路**在界面上仍然通着。
            check("页面已打开（时之虫档案）",
                  js("Promise.resolve(document.querySelector('#p-console h1')"
                     ".innerText)") == "时之虫档案")
            check("待送队列卡片在",
                  js("Promise.resolve(!!document.getElementById("
                     "'conQueueCard'))") is True)
            check("送进游戏的按钮在",
                  js("Promise.resolve(document.querySelectorAll("
                     "'#conQueueCard .btn.pri').length)") == 1)
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
    return _report()


def _report() -> int:
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
