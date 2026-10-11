"""批量保存补丁 + 已被补丁解决的冲突不再显示。

完整走一遍真实链路（沙箱里跑，不碰用户真实配置和游戏文件）：
   扫描 → 本地批量生成 → 批量保存 → 冲突列表里那些条目应当消失

关键在于"消失"这件事必须**真的少掉对应条目**，而不是只把数字改小。
"""
import io
import json
import os
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
from sandbox import enter_sandbox            # noqa: E402

# game_dir=True：补丁模组写进 `<user_dir>/mod/`，那不是 LOCALAPPDATA，
# 只切 LOCALAPPDATA 挡不住。之前漏了这一步，测试往用户真实游戏目录里
# 写进了 89 个补丁模组。
_SANDBOX = enter_sandbox(game_dir=True)
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
    print("批量保存补丁 / 已解决冲突不再显示")
    print("=" * 70)
    app = App()

    # 扫描阶段需要**真实的 user_dir**：模组的 .mod 描述符就在那里，
    # 指到空的沙箱目录会扫不到模组，只剩一堆"文件完整性"误报。
    # 保存阶段再切到沙箱，保证补丁写进沙箱而不是用户的游戏目录。
    real_user = _real_user_dir(app)
    sandbox_user = os.environ.get("SMD_SANDBOX_USER_DIR", "")
    print(f"\n  读取用 user_dir = {real_user}")
    print(f"  写入用 user_dir = {sandbox_user}")

    # App() 已经按沙箱配置把 library 建好了（指向空目录），
    # 这里用真实 user_dir 重新建一份，让扫描能看到真实模组。
    from dataclasses import replace as _dc_replace

    from smd.modinfo import load_library
    app.settings.paths = _dc_replace(app.settings.paths, user_dir=real_user)
    app.library = load_library(app.settings.paths)
    app.load_mods()

    print("\n--- 1. 先跑一次真实扫描 ---")
    # 真实 mod 目录里若有残留补丁，扫描会预先隐藏一批冲突，
    # 后面的"隐藏数变多/列表变短"断言就会因为起点不同而假失败。
    from smd.patch.service import PATCH_PREFIX, patch_root
    real_root = patch_root(app.settings.paths)
    stale = ([d.name for d in real_root.iterdir()
              if d.is_dir() and d.name.startswith(PATCH_PREFIX)]
             if real_root.is_dir() else [])
    check("真实 mod 目录没有残留补丁（有残留则起点不同）",
          not stale, f"{len(stale)} 个：{stale[:2]}")

    app.start_scan(semantic=True)
    t0 = time.time()
    while app.task.running and time.time() - t0 < 400:
        time.sleep(0.3)
    check("扫描完成", app.report is not None,
          f"{app.report.count() if app.report else 0} 条问题")
    if app.report is None:
        return _report()

    print("\n--- 2. 生成前：候选与冲突数 ---")
    before_cands = app.patch_candidates(100000)
    before_findings = app.findings("all", "all", 100000)
    note0 = app.findings_filter_note()
    check("有可修复的候选", len(before_cands) > 0, f"{len(before_cands)} 个")
    # 不假设从 0 开始：沙箱里可能已经残留着上一次跑出来的补丁。
    # 要断言的是"保存之后隐藏数变多"，而不是"现在一条都没隐藏"。
    hidden0 = note0["hidden_patched"]
    print(f"  （进来时已隐藏 {hidden0} 条，来自已有补丁）")

    print("\n--- 3. 本地批量生成 ---")
    app.start_batch_patches(None, "local")
    t0 = time.time()
    while app.task.running and time.time() - t0 < 400:
        time.sleep(0.3)
    drafts = getattr(app, "_auto_drafts", None) or []
    check("本地生成了草稿", len(drafts) > 0, f"{len(drafts)} 份")

    print("\n--- 4. 批量保存（写到沙箱） ---")
    # 切到沙箱 user_dir：从这里开始所有写盘都落在沙箱里
    if sandbox_user:
        app.settings.paths.user_dir = sandbox_user
    plan = app.batch_save_plan("local")
    check("保存计划有内容", plan["savable"] > 0,
          f"可保存 {plan['savable']} / 共 {plan['total']}")
    res = app.batch_save_patches("local")
    check("批量保存成功", res.get("ok") and res["saved"] > 0,
          f"成功 {res.get('saved')} 失败 {res.get('failed')}")
    check("保存结果逐条可见", len(res.get("results") or []) == plan["savable"],
          f"{len(res.get('results') or [])} 条")
    # 所有补丁应当**合并进同一个模组**，而不是一个补丁一个模组
    from smd.patch.service import PATCH_PREFIX, patch_root
    all_patch_dirs = [d.name for d in patch_root(app.settings.paths).iterdir()
                      if d.is_dir() and d.name.startswith(PATCH_PREFIX)]
    check("只生成一个补丁模组", len(all_patch_dirs) == 1,
          f"{len(all_patch_dirs)} 个：{all_patch_dirs[:3]}")
    check("补丁确实写在沙箱目录里",
          str(patch_root(app.settings.paths)).startswith(str(_SANDBOX)),
          str(patch_root(app.settings.paths)))

    print("\n--- 5. 已保存的不会再次出现在待保存里（幂等） ---")
    plan2 = app.batch_save_plan("local")
    check("再查待保存为空", plan2["savable"] == 0,
          f"还有 {plan2['savable']} 个（应为 0）")
    res2 = app.batch_save_patches("local")
    check("重复保存不会重复建模组", res2.get("saved", 0) == 0,
          f"saved={res2.get('saved')}")

    print("\n--- 6. 被补丁解决的冲突不再显示 ---")
    note = app.findings_filter_note()
    after = app.findings("all", "all", 100000)
    check("保存后隐藏数变多", note["hidden_patched"] > hidden0,
          f"{hidden0} → {note['hidden_patched']} 条")
    check("冲突列表确实变短了", len(after) < len(before_findings),
          f"{len(before_findings)} → {len(after)}")
    check("隐藏数 + 剩余数 = 总数",
          note["hidden_patched"] + len(after) == note["total"],
          f"{note['hidden_patched']} + {len(after)} = {note['total']}")
    # show_patched=True 应当又能看全
    shown = app.findings("all", "all", 100000, show_patched=True)
    check("可以强行显示回来", len(shown) >= len(after),
          f"显示 {len(shown)} 条")

    print("\n--- 7. 补丁候选也同步过滤 ---")
    after_cands = app.patch_candidates(100000)
    check("候选列表也变短", len(after_cands) < len(before_cands),
          f"{len(before_cands)} → {len(after_cands)}")
    # 序号必须仍能正确指回同一条
    if after_cands:
        c = after_cands[0]
        f = app._find_finding(c["index"])
        check("候选序号仍能正确指回冲突",
              f is not None and f.title == c["title"],
              f"index={c['index']} {(f.title if f else None)!r}")

    print("\n--- 8. 界面路径 ---")
    bridge = WebBridge(app)
    window = webview.create_window(WINDOW_TITLE, str(INDEX), js_api=bridge,
                                   width=1400, height=900, **WINDOW_KWARGS)
    bridge._window = window
    done = threading.Event()

    def js(expr, timeout=90):
        slot = f"__s{int(time.time() * 1e6) % 10**9}"
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

            print("  · 补丁工坊：批量保存按钮存在")
            check("批量保存按钮已挂上",
                  js('Promise.resolve(!!document.querySelector('
                     '".btn[onclick=\\"env.batchSave()\\"]"))') is True)
            check("保存提示已显示",
                  isinstance(js('Promise.resolve(document.getElementById('
                                '"bsHint").textContent)'), str))

            print("  · 冲突页：被解决条目的开关")
            js("(function(){goPage('conflict'); return 1;})()")
            time.sleep(2.5)
            chips = js('Promise.resolve(document.getElementById("kindChips")'
                       '.innerText)')
            check("冲突页显示了「已由补丁解决」入口",
                  isinstance(chips, str) and "补丁解决" in chips,
                  str(chips)[:80].replace("\n", " "))
            n_before = js('Promise.resolve((S.findingsRaw||[]).length)')
            js("togglePatched()")
            time.sleep(2.5)
            n_after = js('Promise.resolve((S.findingsRaw||[]).length)')
            check("切换后列表条数变化",
                  isinstance(n_before, int) and isinstance(n_after, int)
                  and n_after > n_before,
                  f"{n_before} → {n_after}")
            err = js('Promise.resolve(String(window.__jserr||""))')
            check("无未捕获 JS 错误", err in ("", None), str(err)[:120])
        except Exception as exc:
            import traceback
            print("异常:", exc)
            traceback.print_exc()
        finally:
            done.set()

    def watch():
        done.wait(timeout=300)
        time.sleep(0.6)
        try:
            window.destroy()
        except Exception:
            pass

    window.events.loaded += lambda: threading.Thread(
        target=work, daemon=True).start()
    threading.Thread(target=watch, daemon=True).start()
    webview.start(gui="edgechromium" if sys.platform.startswith("win") else None)
    return _report()


def _real_user_dir(app) -> str:
    """从沙箱配置里取回**真实**的 user_dir（沙箱把它改掉了）。

    真实值存在 ``_SMD_REAL_LOCALAPPDATA`` 指向的配置里。
    """
    import json as _json
    import os as _os
    from pathlib import Path as _P
    base = _os.environ.get("_SMD_REAL_LOCALAPPDATA") or ""
    cfg = _P(base) / "StellarisModDoctor" / "config.json"
    try:
        d = _json.loads(cfg.read_text(encoding="utf-8"))
        v = (d.get("paths") or {}).get("user_dir") or ""
        if v:
            return v
    except (OSError, ValueError):
        pass
    return str(app.settings.paths.user_dir)


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
