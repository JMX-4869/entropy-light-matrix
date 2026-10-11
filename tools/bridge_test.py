"""GUI 桥接自动化测试。

用 pywebview 真的打开界面，然后从页面里通过 ``window.pywebview.api``
调用后端方法，把返回值读回来校验。

这一步验证的是真实链路：JS 代理 -> pywebview -> WebBridge -> App -> JSON。
比单纯跑 Python 单元测试更能发现桥接层的问题（方法名大小写、
参数类型、返回值序列化等）。

用法::

    python tools/bridge_test.py            # 只测只读接口
    python tools/bridge_test.py --scan     # 额外跑一次真实扫描
"""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import webview  # noqa: E402

from smd.app import App  # noqa: E402
from smd.ui import INDEX, WINDOW_KWARGS, WINDOW_TITLE  # noqa: E402
from smd.webui import WebBridge  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []
DONE = threading.Event()


def js_eval(window, expr: str, timeout: float = 30.0):
    """执行 JS 表达式并等待结果。

    ``pywebview.api.*`` 返回的是 Promise，直接返回会被序列化成 ``{}``，
    所以统一走 async 求值：把结果写进一个全局槽位，再轮询读回。
    """
    slot = f"__smd_{int(time.time() * 1000000) % 10**9}"
    wrapped = (
        "(function(){"
        f"  window.{slot} = undefined;"
        "  Promise.resolve(" + expr + ")"
        f"    .then(function(v) {{ window.{slot} = {{ok:true, v:v}}; }})"
        f"    .catch(function(e) {{ window.{slot} = {{ok:false, e:String(e)}}; }});"
        "  return 'started';"
        "})()"
    )
    try:
        window.evaluate_js(wrapped)
    except Exception as exc:
        return {"__error__": repr(exc)}
    deadline = time.time() + timeout
    while time.time() < deadline:
        raw = window.evaluate_js(f"JSON.stringify(window.{slot} || null)")
        if raw and raw != "null":
            window.evaluate_js(f"delete window.{slot}")
            try:
                boxed = json.loads(raw)
            except (TypeError, ValueError):
                return {"__error__": f"非 JSON: {str(raw)[:200]}"}
            if not boxed.get("ok"):
                return {"__error__": boxed.get("e", "unknown")}
            return boxed.get("v")
        time.sleep(0.12)
    return {"__error__": "超时"}


def call(window, method: str, *args):
    """从页面里调用 pywebview.api.<method>(...)，返回解析后的 Python 对象。"""
    arg_src = ", ".join(json.dumps(str(a)) for a in args)
    res = js_eval(window, f"window.pywebview.api.{method}({arg_src})")
    # 桥接层返回的是 JSON 字符串，这里统一解成 dict
    if isinstance(res, str):
        try:
            return json.loads(res)
        except (TypeError, ValueError):
            return {"__error__": f"非 JSON 返回: {res[:200]}"}
    return res


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"  [{'OK' if ok else '!!'}] {name}" + (f" — {detail}" if detail else ""))


def run_tests(window, app, bridge, do_scan: bool) -> None:
    # 等前端 boot() 完成（它会先调 getVersion / getStatus）
    for _ in range(80):
        ready = js_eval(window, "Promise.resolve(!!(window.pywebview && window.pywebview.api))")
        if ready is True:
            break
        time.sleep(0.25)
    time.sleep(2.0)

    print("\n--- 桥接接口测试 ---")

    v = call(window, "getVersion")
    check("getVersion", isinstance(v, dict) and bool(v.get("version")),
          f"v{v.get('version')} / Python {v.get('python')}")

    st = call(window, "getStatus")
    check("getStatus", isinstance(st, dict) and "paths" in st,
          f"user_dir={(st.get('paths') or {}).get('user_dir', '')[:44]}")
    check("getStatus.paths.game_dir",
          bool((st.get("paths") or {}).get("game_dir")),
          (st.get("paths") or {}).get("game_dir", ""))
    check("getStatus.settings", isinstance(st.get("settings"), dict),
          f"shutdown={((st.get('settings') or {}).get('shutdown_enabled'))}")

    se = call(window, "getSettings")
    check("getSettings", isinstance(se, dict) and "paths" in se)

    lm = call(window, "loadMods")
    mods = (lm or {}).get("mods") or []
    check("loadMods", len(mods) > 0, f"{len(mods)} 个模组")
    check("loadMods.summary", bool((lm or {}).get("summary")),
          str((lm or {}).get("summary")))

    if mods:
        md = call(window, "getModDetail", mods[0]["key"])
        check("getModDetail", isinstance(md, dict) and "mod" in md,
              (md.get("mod") or {}).get("name", "")[:40])

    lf = call(window, "getLogFiles")
    check("getLogFiles", isinstance(lf, dict), f"{len((lf or {}).get('files') or [])} 个日志")

    cd = call(window, "getCrashDirs")
    crashes = (cd or {}).get("crashes") or []
    check("getCrashDirs", isinstance(cd, dict), f"{len(crashes)} 次崩溃记录")

    rp = call(window, "getReports")
    check("getReports", isinstance(rp, dict),
          f"{len((rp or {}).get('reports') or [])} 份报告")

    bk = call(window, "listBackups")
    check("listBackups", isinstance(bk, dict))

    ms = call(window, "getMonitorStatus")
    check("getMonitorStatus", isinstance(ms, dict))

    pr = call(window, "poll", "0")
    check("poll", isinstance(pr, dict) and "messages" in pr,
          f"cursor={pr.get('cursor')}")

    # 设置往返
    s1 = call(window, "saveSettings", json.dumps({"monitor_memory_warn_mb": 7777}))
    ok_back = isinstance(s1, dict) and s1.get("monitor_memory_warn_mb") == 7777
    check("saveSettings 往返", ok_back,
          f"读回={s1.get('monitor_memory_warn_mb') if isinstance(s1, dict) else '?'}")
    call(window, "saveSettings", json.dumps({"monitor_memory_warn_mb": 7000}))

    # 报告读取
    if (rp or {}).get("reports"):
        first = next((r for r in rp["reports"] if r["kind"] == "md"), None)
        if first:
            rr = call(window, "readReport", first["path"])
            check("readReport", isinstance(rr, dict) and len(rr.get("content") or "") > 100,
                  f"{len(rr.get('content') or '')} 字符")

    # 非法输入健壮性
    bad = call(window, "getFindings", "bogus", "bogus", "notanumber")
    check("非法参数不崩", isinstance(bad, dict) and "__error__" not in bad)

    if do_scan:
        print("\n--- 真实扫描（会花 20~60 秒）---")
        r = call(window, "startScan")
        check("startScan", bool((r or {}).get("started")), str(r))
        # 轮询任务进度
        last = ""
        for i in range(400):
            time.sleep(0.5)
            pr = call(window, "poll", "0")
            for m in (pr or {}).get("messages", []):
                if m.get("channel") == "task":
                    p = m["payload"]
                    last = f"{p.get('message')} {p.get('progress', 0) * 100:.0f}%"
                    if not p.get("running"):
                        break
            if "100%" in last or "完成" in last:
                break
        f = call(window, "getFindings", "all", "all", "50")
        n = len((f or {}).get("findings") or [])
        check("扫描完成并取回结果", n > 0,
              f"{n} 条问题，健康分 {(f or {}).get('health')}")
        print(f"      最后进度：{last}")

        sol = call(window, "computeSolution")
        check("computeSolution", isinstance(sol, dict) and "order" in sol,
              f"调整 {(sol or {}).get('change_count')} 项，"
              f"禁用 {len((sol or {}).get('disabled') or [])} 个")

        # 关键：确认整包方案真的能穿过桥接层到达 JS 并被渲染。
        # 方案载荷约 18KB，状态载荷约 460KB，都可能撞到序列化上限。
        # 这里走真实前端路径：getSolution -> applySolution -> 渲染。
        js_eval(window, "env.loadSolution()")
        time.sleep(1.5)
        rendered = js_eval(
            window,
            "Promise.resolve(document.getElementById('solBody')"
            ".innerText.length)")
        check("方案面板已渲染内容",
              isinstance(rendered, int) and rendered > 200,
              f"{rendered} 字符")

        # 用 DOM 而不是 window.S 做断言：S 是 <script> 里的顶层 const，
        # 挂在全局词法环境而非 window 上，跨作用域读取不可靠。
        js_sol = js_eval(
            window,
            "Promise.resolve((function(){"
            "var rows=document.querySelectorAll('#solBody table tbody tr');"
            "return rows.length;})())")
        check("方案面板列出加载顺序", isinstance(js_sol, int) and js_sol > 0,
              f"{js_sol} 行")

        tiers = js_eval(
            window,
            "Promise.resolve((function(){"
            "var t=document.getElementById('solBody').innerText;"
            "var hits=['前置框架','大型机制','常规内容','界面 UI','本地化'].filter("
            "function(k){return t.indexOf(k)>=0;});"
            "return hits.length;})())")
        check("方案按分层归位", isinstance(tiers, int) and tiers >= 3,
              f"命中 {tiers} 个分层标签")

        # ---- 模组开关：必须由用户决定 --------------------------------
        pend0 = call(window, "getPendingDecisions")
        check("查询待决策列表", isinstance(pend0, dict),
              f"待决策 {pend0.get('count')} 项")
        dis = [p for p in (sol.get("plans") or [])
               if p.get("action") == "disable"]
        check("求解器未自动确认任何禁用",
              not any(p.get("approved") for p in dis),
              f"{len(dis)} 条建议，approved 全为 False")
        if dis:
            key = dis[0]["key"]
            r1 = call(window, "decideDisable", key, "true")
            check("界面可确认关闭单个模组",
                  bool(r1.get("ok")) and r1.get("approved") == 1,
                  dis[0]["name"])
            r2 = call(window, "decideDisable", key, "false")
            check("界面可撤销为保留",
                  bool(r2.get("ok")) and r2.get("approved") == 0,
                  dis[0]["name"])
            r3 = call(window, "resetDecisions")
            check("可重置全部决策", bool(r3.get("ok")),
                  f"重置 {r3.get('count')} 项")
            btns = js_eval(
                window,
                "Promise.resolve((function(){"
                "var t=document.getElementById('solBody').innerText;"
                "return (t.indexOf('关闭')>=0?1:0)"
                "     + (t.indexOf('保留')>=0?1:0);})())")
            check("方案页提供「关闭/保留」按钮",
                  isinstance(btns, int) and btns == 2, f"命中 {btns}/2")

        st_payload = call(window, "getStatus")
        st_len = len(json.dumps(st_payload, ensure_ascii=False))
        check("大载荷 getStatus 完整传输", st_len > 100_000,
              f"{st_len:,} 字节")

        rep = call(window, "generateReport", "")
        # generateReport 是异步启动的，等一下再取报告列表
        time.sleep(1)
        check("generateReport 已启动", isinstance(rep, dict), str(rep)[:80])

        # ---- AI 接入 -------------------------------------------------
        print("\n--- AI 接入 ---")
        cfg = call(window, "getAiConfig")
        check("读取 AI 配置", isinstance(cfg, dict) and "providers" in cfg,
              f"{len(cfg.get('providers') or [])} 个供应商")
        check("默认未开启发送模组内容",
              not cfg.get("allow_send_mod_content"),
              "隐私默认值正确（False）")
        check("密钥不下发明文", cfg.get("api_key") == "",
              "api_key 字段为空")
        check("供应商含本地模型",
              any(p["key"] == "ollama" and not p["needs_key"]
                  for p in (cfg.get("providers") or [])),
              "Ollama 无需密钥")

        # 保存往返测试会**写用户真实配置**（saveAiConfig 直接落盘）。
        # 这个测试刻意不整体沙箱（它要真的读写配置来验证往返），
        # 所以必须先把 AI 段备份下来，测完原样写回——
        # 否则用户的模型名会被这里写死成 qwen2.5:14b。
        #
        # 而且还原必须放在 finally 里：早先没有 try/finally，
        # 中间任何一条断言失败都会**跳过还原**，用户的 AI 配置就留在
        # 测试值上了。这个坑真实发生过好几次，找了很久才定位到是本文件。
        _ai_backup = dict(app.settings.ai.to_dict())
        # 从磁盘再读一份做比对基准：内存里的可能已经被别的用例改过
        _disk_backup = None
        try:
            from smd.config import Settings as _S
            _disk_backup = dict(_S.load().ai.to_dict())
        except Exception:
            pass

        try:
            saved = call(window, "saveAiConfig", json.dumps(
                {"provider": "ollama", "model": "qwen2.5:14b",
                 "temperature": 0.1}))
            check("保存 AI 配置往返",
                  isinstance(saved, dict)
                  and saved.get("provider") == "ollama",
                  f"provider={saved.get('provider')} "
                  f"model={saved.get('model')}")
        finally:
            for _k, _v in _ai_backup.items():
                if hasattr(app.settings.ai, _k):
                    setattr(app.settings.ai, _k, _v)
            app.settings.save()
        restored = app.settings.ai
        check("AI 配置已原样还原",
              restored.provider == _ai_backup.get("provider")
              and restored.model == _ai_backup.get("model"),
              f"{restored.provider} / {restored.model}")
        if _disk_backup is not None:
            try:
                from smd.config import Settings as _S2
                on_disk = _S2.load().ai
                same = all(
                    getattr(on_disk, k, None) == v
                    for k, v in _disk_backup.items())
                check("磁盘上的 AI 配置也已还原（不留测试值）", same,
                      f"磁盘：{on_disk.provider}/{on_disk.model}，"
                      f"期望：{_disk_backup.get('provider')}/"
                      f"{_disk_backup.get('model')}")
            except Exception as _exc:
                check("磁盘上的 AI 配置也已还原（不留测试值）", False,
                      str(_exc)[:60])

        # 未同意发送时，生成必须被**同步**拒绝。
        # 断言要盯住 need_consent，不能只写 started != True——
        # 那种写法在同意开着的时候会假通过（实际启动成功了，
        # 只是 started 恰好不是 True 的某些取值），这个坑真踩过。
        blocked = call(window, "aiGeneratePatch", "0", "", "merge")
        check("未同意发送时同步拒绝并说明原因",
              isinstance(blocked, dict)
              and blocked.get("started") is False
              and blocked.get("need_consent") is True,
              str(blocked)[:80])
        check("拒绝时不留下后台任务",
              call(window, "getTask", "") is not None
              and (call(window, "getTask", "") or {}).get("running") is not True,
              "未启动任务")

        cases = call(window, "getPatchCandidates", "100")
        cands = (cases or {}).get("candidates") or []
        check("列出可修复的冲突", isinstance(cases, dict),
              f"{len(cands)} 个候选")

        pv = call(window, "aiPatchPreview", "0", "", "merge")
        check("提示词预览可用", isinstance(pv, dict) and pv.get("ok"),
              f"约 {pv.get('tokens_estimate')} tokens / "
              f"{pv.get('payload_chars')} 字符")
        check("预览含实际内容",
              bool(pv.get("user")) and len(pv.get("user") or "") > 200,
              "请求体非空")

        # ---- 手写补丁 -------------------------------------------------
        print("\n--- 手写补丁 ---")
        tpl = call(window, "getPatchTemplates")
        check("读取补丁模板", len((tpl or {}).get("templates") or []) >= 6,
              f"{len((tpl or {}).get('templates') or [])} 个")

        good = "my_trigger = {\n\talways = yes\n}"
        v1 = call(window, "validatePatch", good,
                  "common/scripted_triggers/zzz_smd_test.txt", "")
        check("校验合法补丁", bool(v1.get("ok")),
              f"阻塞 {v1.get('blocking')} 个")

        v2 = call(window, "validatePatch", "my_trigger = {",
                  "common/scripted_triggers/zzz_smd_test.txt", "")
        check("拦住语法错误补丁", not v2.get("ok"),
              f"阻塞 {v2.get('blocking')} 个")

        patches = call(window, "getPatches")
        check("列出补丁模组", isinstance(patches, dict),
              f"{len((patches or {}).get('patches') or [])} 个")

        # ---- 手动排序 -------------------------------------------------
        print("\n--- 手动排序 ---")
        ov = call(window, "getOrderView")
        rows = (ov or {}).get("rows") or []
        check("读取顺序视图", isinstance(ov, dict) and len(rows) > 0,
              f"{len(rows)} 个模组")
        if len(rows) >= 4:
            key = rows[3]["key"]
            mv = call(window, "moveMod", key, "-2", "")
            check("上移模组", bool(mv.get("ok")) and mv.get("moved") is True,
                  f"位置 {mv.get('from')} → {mv.get('to')}")
            ov2 = call(window, "getOrderView")
            check("顺序变化被记录", (ov2 or {}).get("dirty") is True,
                  f"与自动结果差异 {(ov2 or {}).get('changed')} 处")
            rs = call(window, "resetOrder")
            check("恢复自动排序",
                  ((rs or {}).get("view") or {}).get("dirty") is False,
                  "已复原")

        # ---- 冲突全量可见 ---------------------------------------------
        print("\n--- 冲突全量可见 ---")
        f2 = call(window, "getFindings", "all", "all", "100000")
        n2 = len((f2 or {}).get("findings") or [])
        check("可取回全部冲突（无截断）",
              n2 == len((sol.get("plans") or [])) or n2 > 100,
              f"{n2} 条（health={(f2 or {}).get('health')}）")
        kinds = (f2 or {}).get("kinds") or []
        check("分类统计齐全", len(kinds) >= 5, f"{len(kinds)} 类")

    # 前端渲染检查
    print("\n--- 前端渲染检查 ---")
    for sel, name in (("modsBody", "模组表格行数"),
                      ("findings", "冲突列表"),
                      ("solBody", "方案面板"),
                      ("pathList", "环境路径列表")):
        n = js_eval(window, f"Promise.resolve(document.getElementById('{sel}').children.length)")
        check(f"{name} 已渲染", isinstance(n, int) and n > 0, f"{n} 个节点")

    toast_ok = js_eval(window, "Promise.resolve(document.querySelectorAll('.toast').length)")
    check("Toast 组件可用", isinstance(toast_ok, int), f"{toast_ok} 个")

    # ---- 自绘标题栏与窗口控制 ----
    # 窗口在 ui.py 里是 frameless 的，标题栏由界面自己画。
    # 这类改动只有真的开一次无边框窗口才验证得到。
    print("\n--- 自绘标题栏 / 窗口控制 ---")
    has_bar = js_eval(window, "Promise.resolve(!!document.getElementById('tbar'))")
    check("标题栏存在", has_bar is True)
    drag = js_eval(window, "Promise.resolve(document.querySelectorAll("
                            "'.pywebview-drag-region').length)")
    check("有可拖拽区域", isinstance(drag, int) and drag >= 1, f"{drag} 个")
    # easy_drag 必须是关的：开着会让**整个窗口**任意位置都能拖动，
    # 用户点内容区也会把窗口拖走。关掉后只有标注了
    # pywebview-drag-region 的元素能拖。
    check("easy_drag 已关闭", WINDOW_KWARGS.get("easy_drag") is False,
          f"easy_drag={WINDOW_KWARGS.get('easy_drag')}")
    on_drag = js_eval(window, "Promise.resolve("
                              "document.querySelector('.tbar')"
                              ".classList.contains('pywebview-drag-region'))")
    check("标题栏本身是拖拽区", on_drag is True)
    # 内容区不能是拖拽区
    on_body = js_eval(window, "Promise.resolve("
                              "document.querySelector('.app')"
                              ".classList.contains('pywebview-drag-region'))")
    check("内容区不是拖拽区", on_body is False)
    btns = js_eval(window, "Promise.resolve(document.querySelectorAll("
                            "'.tbar .tbBtn').length)")
    check("三个窗口按钮", btns == 3, f"{btns} 个")
    nm = js_eval(window, "Promise.resolve(document.querySelector("
                         "'.tbar .tbName').innerText.trim())")
    check("标题栏显示新名字", "熵光矩阵" in str(nm), str(nm))

    # 最大化 → 还原。
    # **不要**断言 ``window.state``：Windows/EdgeChromium 后端实测返回空字典，
    # 拿它比较只会得到无意义的 "" -> ""（曾经因此白报两处失败）。
    # 改为断言返回值 + 窗口尺寸这两个真实可观测的量。
    w0 = (window.width, window.height)
    r1 = call(window, "toggleMaximize")
    time.sleep(1.5)
    w1 = (window.width, window.height)
    check("最大化切换返回成功", bool(r1.get("ok")), str(r1)[:60])
    check("返回里带 maximized=True", r1.get("maximized") is True, str(r1)[:60])
    check("最大化后窗口确实变大", w1[0] * w1[1] > w0[0] * w0[1],
          f"{w0} → {w1}")

    r2 = call(window, "toggleMaximize")
    time.sleep(1.5)
    w2 = (window.width, window.height)
    check("能还原回去", r2.get("maximized") is False, str(r2)[:60])
    check("还原后尺寸回到初始", abs(w2[0] - w0[0]) < 80,
          f"{w0} → {w1} → {w2}")

    st = call(window, "getSwitchState")
    check("窗口控制不影响桥接", r2.get("ok") is True and "rows" in st)

    err = js_eval(window, "Promise.resolve(String(window.__jserr || ''))")
    check("无未捕获 JS 错误", err in ("", None), str(err)[:160])

    DONE.set()


def main() -> int:
    do_scan = "--scan" in sys.argv
    app = App()
    bridge = WebBridge(app)

    # 与 smd/ui.py 的生产窗口保持一致（共用 WINDOW_KWARGS）：
    # 只有配置相同，标题栏与窗口控制的测试才有意义。
    window = webview.create_window(
        WINDOW_TITLE, str(INDEX), js_api=bridge,
        width=1280, height=840, **WINDOW_KWARGS,
    )
    bridge._window = window

    def worker() -> None:
        try:
            run_tests(window, app, bridge, do_scan)
        except Exception as exc:
            import traceback
            print("测试线程异常：", exc)
            traceback.print_exc()
            DONE.set()

    def on_loaded() -> None:
        threading.Thread(target=worker, daemon=True).start()

    window.events.loaded += on_loaded

    def watcher() -> None:
        DONE.wait(timeout=900)
        time.sleep(0.6)
        try:
            window.destroy()
        except Exception:
            pass

    threading.Thread(target=watcher, daemon=True).start()

    webview.start(gui="edgechromium" if sys.platform.startswith("win") else None)

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print(f"\n{'=' * 58}")
    print(f"桥接测试结果：{passed}/{total} 通过")
    for name, ok, detail in RESULTS:
        if not ok:
            print(f"  失败：{name} — {detail}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
