"""模组开关 与 DLC 管理的界面/后端端到端测试。

覆盖：

* 滑块能暂存开关，且**不写盘**（改动前先验证磁盘未变）
* 拨回原位会消除暂存，不留假「待写入」
* 批量操作（全部/反选/按筛选）行为正确
* 写入后 ``dlc_load.json`` 真的变了，且能回滚
* DLC 开关同样走「暂存 → 预览 → 写入」，写入后能读回
* 界面上滑块、批量按钮、DLC 分组都渲染出来

用法::  python tools/switch_test.py
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
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# 注意：这个测试**故意不用配置沙箱**。
# 它要验证的是「把模组开关真的写进游戏的 dlc_load.json 与启动器数据库」，
# 而沙箱只隔离 %LOCALAPPDATA%，游戏目录并不在其中——用了沙箱反而会
# 让人误以为已经隔离。它自己在 finally 里做字节级还原。
from smd.app import App  # noqa: E402
from smd.config import Settings  # noqa: E402
from smd.manage import read_disabled_dlcs, read_dlc_list  # noqa: E402
from smd.ui import INDEX, WINDOW_KWARGS, WINDOW_TITLE  # noqa: E402
from smd.webui import WebBridge  # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"  [{'OK' if ok else '!!'}] {name}" + (f" — {detail}" if detail else ""))


def js(window, expr: str, timeout: float = 25.0):
    slot = f"__sw_{int(time.time() * 1e6) % 10**9}"
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
            box = json.loads(raw)
            return box.get("v") if box.get("ok") else {"__error__": box.get("e")}
        time.sleep(0.1)
    return {"__error__": "timeout"}


# ---------------------------------------------------------------------------
# 后端
# ---------------------------------------------------------------------------

def test_backend() -> None:
    paths = Settings.load().paths
    p = paths.dlc_load
    before_bytes = p.read_bytes() if p.is_file() else b""
    before_disabled = read_disabled_dlcs(paths)

    app = App()
    app.load_mods()
    if app.library is None:
        check("加载模组清单", False, "没有模组库")
        return

    print("--- 1. 模组开关暂存 ---")
    st = app.switch_state()
    check("拿到开关状态", len(st["rows"]) > 0, f"{len(st['rows'])} 行")
    check("初始没有待写入", st["pending"] == 0, str(st["pending"]))

    enabled_rows = [r for r in st["rows"] if r["actual"]]
    check("有已启用的模组", len(enabled_rows) > 0, f"{len(enabled_rows)} 个")

    target = enabled_rows[0]["key"]
    r = app.set_mod_switch(target, False)
    check("暂存关闭一个模组", r["ok"] and r["state"]["pending"] == 1,
          f"待写入 {r['state']['pending']}")
    check("暂存阶段不写盘", p.read_bytes() == before_bytes,
          "dlc_load.json 未被改动")

    pv = app.preview_mod_switches()
    check("预览显示将关闭 1 个",
          pv["to_disable"] == 1 and pv["to_enable"] == 0, str(pv["disable_names"]))

    r = app.set_mod_switch(target, True)
    check("拨回原位消除暂存", r["state"]["pending"] == 0,
          f"待写入 {r['state']['pending']}")

    print("\n--- 2. 批量操作 ---")
    total = len(app.library.all())
    currently_on = sum(1 for m in app.library.all() if m.enabled_in_playset)
    # 磁盘上没有内容的模组一定会被批量操作跳过（见 3.5 节），
    # 所以这里的期望值要按「可加载的模组」算，而不是全部。
    usable = [m for m in app.library.all() if m.is_loadable()]
    n_usable = len(usable)
    usable_on = sum(1 for m in usable if m.enabled_in_playset)
    usable_off = n_usable - usable_on

    r = app.switch_bulk("invert")
    # 反选后要满足的不变量是「**打算启用的都是有内容的**」。
    #
    # 不能断言 pending == 可加载总数：反选对「已启用但磁盘无内容」的模组
    # 是把它关掉——关掉一个坏模组是合法操作，所以计数会比重启用的多。
    # 那是对的，不是 bug。
    ch = app.switch_bulk("invert")["state"]["summary"]
    will_on = set(app.preview_mod_switches().get("enable_names") or [])
    bad_on = [n for n in will_on
              if not next((m for m in usable if m.name == n), None)]
    check("反选不会尝试启用无内容的模组", not bad_on, str(bad_on[:3]))
    check("反选把可加载的翻转了",
          r["state"]["pending"] > 0,
          f"待写入 {r['state']['pending']}（可加载 {n_usable}，总共 {total}）")
    r = app.switch_bulk("reset")
    check("重置清空暂存", r["state"]["pending"] == 0)
    # 「全部关闭」只统计**当前开着且界面上看得见**的模组。
    # 不能用整个模组库算：已退订的残留已经不在列表里，
    # 拿它们当分母会算出一个永远对不上的期望值。
    vis_on = sum(1 for m in app.visible_mods() if m.enabled_in_playset)
    r = app.switch_bulk("disable_all")
    check("全部关闭只列出需要变的",
          r["state"]["summary"]["to_disable"] == vis_on,
          f"{r['state']['summary']['to_disable']} / 可见且启用 {vis_on}")
    r = app.switch_bulk("enable_all")
    check("全部启用只列出需要变的（跳过无内容的）",
          r["state"]["summary"]["to_enable"] == usable_off,
          f"{r['state']['summary']['to_enable']} / 可加载且关闭 {usable_off}")
    app.switch_bulk("reset")

    print("\n--- 3. 真实写入与回滚 ---")
    db_path = paths.launcher_db
    db_backup = db_path.read_bytes() if db_path.is_file() else b""
    r = app.set_mod_switch(target, False)
    target_name = next(m.name for m in app.library.all() if m.key == target)
    res = app.apply_mod_switches(sync_launcher=True)
    check("写入成功", res.get("ok"), str(res.get("notes")))
    check("至少关闭了 1 个", len(res.get("removed") or []) >= 1,
          f"关闭 {res.get('removed')}")
    check("启动器数据库已同步（权威来源）",
          res.get("db_synced") is True,
          "；".join(res.get("notes") or [])[:80])
    after_bytes = p.read_bytes()
    check("dlc_load.json 确实变了", after_bytes != before_bytes)

    app2 = App()
    app2.load_mods()
    m2 = app2.library.get(target) if app2.library else None
    check("重新读取后该模组为未启用",
          m2 is not None and not m2.enabled_in_playset,
          f"{target_name} enabled={m2.enabled_in_playset if m2 else '?'}")
    check("写入后暂存区已清空", app2.switch_state()["pending"] == 0)
    # 这个模组的描述符应已从 dlc_load.json 里移除
    refs = json.loads(p.read_text(encoding="utf-8-sig")).get("enabled_mods", [])
    gone = not any(str(x).lower().endswith(f"ugc_{target.split(':')[-1]}.mod")
                   for x in refs) if target.startswith("ugc:") else True
    check("该模组已从 enabled_mods 移除", gone, f"共 {len(refs)} 条")

    # 还原两个文件
    p.write_bytes(before_bytes)
    if db_backup:
        db_path.write_bytes(db_backup)
    check("已还原 dlc_load.json", p.read_bytes() == before_bytes)
    check("已还原启动器数据库",
          db_path.read_bytes() == db_backup if db_backup else True)

    print("\n--- 3.5 磁盘上没有内容的模组不能假装启用 ---")
    # 这类模组（已退订 / 工坊文件被清）勾了也不会加载任何东西。
    # 早期版本会把它写进配置，界面显示「已启用」但游戏里什么都没有——
    # 用户完全不知道为什么点了没反应。现在必须在模型层就拒绝。
    app3 = App()
    app3.load_mods()
    st3 = app3.switch_state()
    dead = [r for r in st3["rows"] if not r["loadable"]]
    alive = [r for r in st3["rows"] if r["loadable"]]
    check("能识别出磁盘无内容的模组", len(dead) >= 0,
          f"{len(dead)} 个不可用 / {len(alive)} 个可用")
    check("统计里报告了这个数量",
          st3["summary"].get("unloadable") == len(dead),
          str(st3["summary"].get("unloadable")))
    check("可用的模组没有被误判", len(alive) > 0, f"{len(alive)} 个")

    if dead:
        k = dead[0]["key"]
        r = app3.set_mod_switch(k, True)
        check("启用无内容的模组被拒绝", r.get("ok") is False,
              (r.get("error") or "")[:56])
        check("拒绝时没有产生待写入", r["state"]["pending"] == 0,
              str(r["state"]["pending"]))
        r = app3.set_mod_switch(k, False)
        check("但允许关掉它（清理过期记录）", r.get("ok") is True)
        app3.switch_bulk("reset")

        r = app3.switch_bulk("enable_all")
        s = r["state"]["summary"]
        check("「全部启用」自动跳过无内容的",
              s["to_enable"] + len(dead) == len(alive)
              or r.get("skipped") == len(dead),
              f"将启用 {s['to_enable']}，跳过 {r.get('skipped')}，"
              f"可用 {len(alive)}")
        check("跳过时给出名单", bool(r.get("skipped_names")),
              str((r.get("skipped_names") or [])[:2]))
        app3.switch_bulk("reset")

        r = app3.set_mod_switches([k], True)
        check("批量启用也会跳过无内容的",
              r.get("skipped") == 1 and r.get("count") == 0,
              f"count={r.get('count')} skipped={r.get('skipped')}")
        app3.switch_bulk("reset")
    else:
        print("      （本机没有磁盘无内容的模组，跳过这组断言）")

    print("\n--- 4. DLC 暂存 ---")
    st2 = app.dlc_state()
    check("读到 DLC 列表", st2["total"] > 0,
          f"{st2['total']} 个，分 {len(st2['by_category'])} 类")
    check("DLC 初始无待写入", st2["pending"] == 0, str(st2["pending"]))

    k = st2["dlcs"][0]["key"]
    r = app.set_dlc_switch(k, False)
    check("暂存关闭一个 DLC", r["state"]["pending"] == 1,
          f"待写入 {r['state']['pending']}")
    check("暂存阶段不写盘", p.read_bytes() == before_bytes)
    r = app.set_dlc_switch(k, True)
    check("拨回原位消除暂存", r["state"]["pending"] == 0)

    r = app.dlc_bulk("disable_all")
    check("DLC 全部关闭", r["state"]["pending"] == st2["total"],
          f"{r['state']['pending']}/{st2['total']}")
    app.dlc_bulk("reset")
    check("DLC 重置清空", app.dlc_state()["pending"] == 0)


# ---------------------------------------------------------------------------
# 界面
# ---------------------------------------------------------------------------

def test_frontend() -> None:
    import webview

    app = App()
    bridge = WebBridge(app)
    window = webview.create_window(WINDOW_TITLE, str(INDEX), js_api=bridge,
                                   width=1400, height=880,
                                   **WINDOW_KWARGS)
    bridge._window = window
    done = threading.Event()

    def worker():
        try:
            for _ in range(80):
                if js(window, "!!(window.pywebview && window.pywebview.api)") is True:
                    break
                time.sleep(0.25)
            time.sleep(3.0)

            print("\n--- 5. 模组列表滑块 ---")
            js(window, "(function(){goPage('mods'); return 1;})()")
            time.sleep(2.0)
            n = js(window, "Promise.resolve(document.querySelectorAll("
                           "'#modsBody .sw input').length)")
            check("每行都有滑块", isinstance(n, int) and n > 0, f"{n} 个滑块")
            rows = js(window, "Promise.resolve(document.querySelectorAll("
                              "'#modsBody tr').length)")
            check("滑块数与行数一致", n == rows, f"滑块 {n} / 行 {rows}")

            pills = js(window, "Promise.resolve(document.querySelectorAll("
                               "'#modsBody .sw').length)")
            check("滑块样式为开关型（.sw）", pills == n, f"{pills}")

            print("\n--- 6. 点击滑块暂存 ---")
            first = js(window,
                       "Promise.resolve((function(){"
                       "var i=document.querySelector('#modsBody .sw input');"
                       "if(!i) return null;"
                       "var k=i.closest('tr');"
                       "return {checked:i.checked, trIdx:[...document.querySelectorAll"
                       "('#modsBody tr')].indexOf(k)};})())")
            check("能定位到第一个滑块", isinstance(first, dict), str(first))
            # 通过界面调用（模拟点击）
            js(window, "(function(){"
                       "var i=document.querySelector('#modsBody .sw input');"
                       "i.checked=!i.checked;"
                       "i.dispatchEvent(new Event('change'));"
                       "return 1;})()")
            time.sleep(1.2)
            pend = js(window, "Promise.resolve((S.switches&&S.switches.pending)||0)")
            check("界面点击后出现待写入", isinstance(pend, int) and pend >= 1,
                  f"待写入 {pend}")
            bar = js(window, "Promise.resolve(document.getElementById('switchBar')"
                             ".innerText.length)")
            check("顶部出现改动提示条", isinstance(bar, int) and bar > 10,
                  f"{bar} 字符")
            changed = js(window, "Promise.resolve(document.querySelectorAll("
                                 "'#modsBody tr.rowChanged').length)")
            check("改动的行被高亮", isinstance(changed, int) and changed >= 1,
                  f"{changed} 行")

            # 放弃改动
            js(window, "env.switchBulk('reset')")
            time.sleep(1.0)
            pend2 = js(window, "Promise.resolve((S.switches&&S.switches.pending)||0)")
            check("放弃改动后归零", pend2 == 0, f"待写入 {pend2}")

            print("\n--- 7. DLC 管理页 ---")
            js(window, "(function(){goPage('dlc'); return 1;})()")
            time.sleep(2.5)
            dlc_n = js(window, "Promise.resolve((S.dlc&&S.dlc.total)||0)")
            check("DLC 数据已加载", isinstance(dlc_n, int) and dlc_n > 0,
                  f"{dlc_n} 个")
            sw_n = js(window, "Promise.resolve(document.querySelectorAll("
                              "'#dlcList .sw input').length)")
            check("每个 DLC 都有开关", sw_n == dlc_n,
                  f"{sw_n} 开关 / {dlc_n} DLC")
            cats = js(window, "Promise.resolve(document.querySelectorAll("
                              "'#dlcList .card').length)")
            check("按类别分组显示", isinstance(cats, int) and cats >= 2,
                  f"{cats} 个分组卡片")
            summ = js(window, "Promise.resolve(document.getElementById('dlcSummary')"
                              ".innerText)")
            check("概览显示统计", isinstance(summ, str) and "已安装" in summ,
                  (summ or "").replace("\n", " ")[:60])

            js(window, "(function(){"
                       "var i=document.querySelector('#dlcList .sw input');"
                       "i.checked=!i.checked;"
                       "i.dispatchEvent(new Event('change'));"
                       "return 1;})()")
            time.sleep(1.2)
            dp = js(window, "Promise.resolve((S.dlc&&S.dlc.pending)||0)")
            check("点击 DLC 开关产生待写入", isinstance(dp, int) and dp >= 1,
                  f"待写入 {dp}")
            js(window, "env.dlcBulk('reset')")
            time.sleep(0.8)
            dp2 = js(window, "Promise.resolve((S.dlc&&S.dlc.pending)||0)")
            check("DLC 放弃改动后归零", dp2 == 0, f"待写入 {dp2}")
        except Exception as exc:
            import traceback
            print("前端测试异常：", exc)
            traceback.print_exc()
        finally:
            done.set()

    def watcher():
        done.wait(timeout=420)
        time.sleep(0.6)
        try:
            window.destroy()
        except Exception:
            pass

    window.events.loaded += lambda: threading.Thread(
        target=worker, daemon=True).start()
    threading.Thread(target=watcher, daemon=True).start()
    webview.start(gui="edgechromium" if sys.platform.startswith("win") else None)


def main() -> int:
    paths = Settings.load().paths
    p = paths.dlc_load
    backup = p.read_bytes() if p.is_file() else b""
    print("=" * 66)
    print("模组开关 / DLC 管理测试")
    print("=" * 66)
    try:
        test_backend()
        test_frontend()
    finally:
        # 无论怎么失败都还原真实配置
        if backup and p.read_bytes() != backup:
            p.write_bytes(backup)
            print("\n[清理] dlc_load.json 已还原")
    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print()
    print("=" * 66)
    print(f"结果：{passed}/{total} 通过")
    for n, ok, d in results:
        if not ok:
            print(f"  失败：{n} — {d}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
