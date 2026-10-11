"""主题系统端到端测试。

分两层验证：

1. **后端**（不需要界面）：校验器能拦住坏主题、CRUD 与导入导出往返、
   内置主题令牌齐全且颜色合法。
2. **前端**（开真实界面）：通过 JS 接口切换主题，读回 :root 上的
   CSS 变量，确认颜色**真的变了**，并确认形态令牌（切角/发光/纹理）
   正确反映到 data 属性上。

用法::  python tools/theme_test.py
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

# --- 测试沙箱：必须在 import smd 之前生效，否则配置会被测试写脏 ---
sys.path.insert(0, str(Path(__file__).resolve().parent))
from sandbox import enter_sandbox          # noqa: E402
enter_sandbox()

from smd import theme as th  # noqa: E402
from smd.app import App  # noqa: E402
from smd.ui import INDEX, WINDOW_KWARGS, WINDOW_TITLE  # noqa: E402
from smd.webui import WebBridge  # noqa: E402

results: list[tuple[str, bool, str]] = []
created: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"  [{'OK' if ok else '!!'}] {name}" + (f" — {detail}" if detail else ""))


# ---------------------------------------------------------------------------
# 后端
# ---------------------------------------------------------------------------

def test_backend() -> None:
    print("--- 1. 主题校验 ---")
    cases = [
        ("非对象", [1, 2], False),
        ("完全为空", {}, False),
        ("坏颜色", {"tokens": {"bg": "not-a-color"}}, False),
        ("好颜色 #rgb", {"tokens": {"bg": "#abc"}}, True),
        ("好颜色 #rrggbb", {"tokens": {"bg": "#aabbcc"}}, True),
        ("好颜色 #rrggbbaa", {"tokens": {"bg": "#aabbcc80"}}, True),
        ("好颜色 rgba()", {"tokens": {"shadow": "rgba(0,0,0,.5)"}}, True),
        ("坏枚举", {"flags": {"texture": "sparkle"}}, False),
        ("好枚举", {"flags": {"texture": "hex"}}, True),
        ("坏尺寸", {"flags": {"radius": "abc"}}, False),
        ("好尺寸", {"flags": {"radius": "6px"}}, True),
        ("数字越界截断", {"flags": {"glow": 5}}, True),
        ("未知令牌只警告",
         {"tokens": {"bg": "#123456", "bogus": "#fff"}}, True),
    ]
    for name, raw, want_ok in cases:
        norm, issues = th.validate_theme(
            dict(raw, id="t", name="t") if isinstance(raw, dict) else raw)
        got_ok = norm is not None
        check(f"校验：{name}", got_ok == want_ok,
              "接受" if got_ok else
              (next((i.message for i in issues if i.level == "error"), "拒绝"))[:44])

    print("\n--- 2. 内置主题完整性 ---")
    for tid, b in th.BUILTIN.items():
        miss = [k for k in th.COLOR_TOKENS if k not in b["tokens"]]
        bad = [k for k, v in b["tokens"].items()
               if k in th.COLOR_TOKENS and not th._valid_color(v)]
        norm, issues = th.validate_theme({"id": tid, **b})
        errs = [i for i in issues if i.level == "error"]
        check(f"内置「{b['name']}」令牌齐全", not miss and not bad and not errs,
              f"缺 {len(miss)}，非法 {len(bad)}，错误 {len(errs)}")
        # 关键令牌必须与默认值不同，否则主题看起来一样
        diffs = sum(1 for k in th.COLOR_TOKENS
                    if b["tokens"].get(k) != th.BUILTIN[th.DEFAULT_THEME]
                    ["tokens"].get(k))
        check(f"内置「{b['name']}」与默认有区分度",
              tid == th.DEFAULT_THEME or diffs >= 10, f"{diffs} 个令牌不同")

    print("\n--- 3. CRUD 与导入导出 ---")
    t = {"id": "tt_roundtrip", "name": "往返测试", "desc": "自动化",
         "author": "test",
         "tokens": {"bg": "#112233", "acc": "#445566"},
         "flags": {"clip": True, "glow": 0.6, "texture": "hex"}}
    r = th.save_user_theme(t)
    created.append("tt_roundtrip")
    check("保存主题", r.get("ok"), str(r.get("error", ""))[:40])
    check("出现在列表", any(x["id"] == "tt_roundtrip" for x in th.list_themes()))
    got = th.get_theme("tt_roundtrip")
    check("读回令牌生效", got["tokens"]["bg"] == "#112233",
          got["tokens"]["bg"])
    check("读回形态生效", got["flags"].get("clip") is True and
          abs(float(got["flags"].get("glow", 0)) - 0.6) < 0.01,
          f"clip={got['flags'].get('clip')} glow={got['flags'].get('glow')}")
    check("未指定的令牌从基座补齐",
          got["tokens"].get("crit") == th.BUILTIN[th.DEFAULT_THEME]
          ["tokens"]["crit"], got["tokens"].get("crit", ""))

    ex = th.export_theme("tt_roundtrip")
    check("导出主题", ex.get("ok"), str(ex.get("path", ""))[-28:])
    if ex.get("ok"):
        payload = json.loads(Path(ex["path"]).read_text(encoding="utf-8"))
        check("导出内容含令牌与形态",
              "tokens" in payload and "flags" in payload,
              f"{len(payload.get('tokens', {}))} 令牌")

    im = th.import_theme(ex["path"])
    if im.get("ok"):
        created.append(im["theme"]["id"])
    check("导入主题", im.get("ok"), str(im.get("error", ""))[:40])
    check("id 冲突自动改名", im.get("renamed") is True,
          im.get("theme", {}).get("id", ""))

    # 坏文件必须被拒
    bad = ROOT / "tools" / "_bad_theme.json"
    bad.write_text('{"id":"x","tokens":{"bg":"oops"}}', encoding="utf-8")
    r2 = th.import_theme(str(bad))
    check("拒绝导入坏主题", not r2.get("ok"), str(r2.get("error", ""))[:40])
    bad.unlink(missing_ok=True)

    check("内置主题不可删", not th.delete_user_theme("stellaris").get("ok"),
          th.delete_user_theme("stellaris").get("error", ""))
    check("自定义可删", th.delete_user_theme("tt_roundtrip").get("ok"))
    created.remove("tt_roundtrip")

    print("\n--- 4. 应用侧 API ---")
    app = App()
    p = app.themes_payload()
    check("themes_payload 结构完整",
          all(k in p for k in ("themes", "current", "color_tokens",
                               "shape_tokens", "builtin_ids")),
          f"{len(p['themes'])} 主题 / {len(p['color_tokens'])} 颜色令牌 / "
          f"{len(p['shape_tokens'])} 形态令牌")
    check("默认主题是群星风", p["current"] == th.DEFAULT_THEME, p["current"])


# ---------------------------------------------------------------------------
# 前端
# ---------------------------------------------------------------------------

def js(window, expr: str, timeout: float = 25.0):
    slot = f"__th_{int(time.time() * 1e6) % 10**9}"
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


def test_frontend() -> None:
    import webview

    app = App()
    bridge = WebBridge(app)
    window = webview.create_window(WINDOW_TITLE, str(INDEX), js_api=bridge,
                                   width=1360, height=860,
                                   **WINDOW_KWARGS)
    bridge._window = window
    done = threading.Event()

    def worker():
        try:
            for _ in range(80):
                if js(window, "!!(window.pywebview && window.pywebview.api)") is True:
                    break
                time.sleep(0.25)
            time.sleep(2.5)

            print("\n--- 5. 界面主题切换 ---")
            # 主题列表由 env.boot() 里的 loadThemes() 异步填充，
            # 这里必须轮询等待，不能立刻断言（否则是测试自己的竞态）
            n = 0
            for _ in range(60):
                n = js(window, "Promise.resolve((TH&&TH.list||[]).length)")
                if isinstance(n, int) and n >= 5:
                    break
                time.sleep(0.3)
            check("界面已加载主题列表", isinstance(n, int) and n >= 5,
                  f"{n} 个")

            seen = {}
            for tid in th.BUILTIN:
                js(window,
                   f"(function(){{var t=TH.list.find(x=>x.id==='{tid}');"
                   f"if(t) applyTheme(t); return 1;}})()")
                time.sleep(0.35)
                vals = js(window,
                          "Promise.resolve(JSON.stringify({"
                          "bg:getComputedStyle(document.documentElement)"
                          ".getPropertyValue('--t-bg').trim(),"
                          "acc:getComputedStyle(document.documentElement)"
                          ".getPropertyValue('--t-acc').trim(),"
                          "clip:document.documentElement.dataset.clip,"
                          "tex:document.documentElement.dataset.tex,"
                          "glow:document.documentElement.dataset.glow}))")
                if isinstance(vals, str):
                    seen[tid] = json.loads(vals)

            check("所有主题都能应用",
                  len(seen) == len(th.BUILTIN), f"{len(seen)}/{len(th.BUILTIN)}")
            for tid, v in seen.items():
                want = th.BUILTIN[tid]["tokens"]["bg"].lower()
                got = (v.get("bg") or "").lower()
                check(f"「{tid}」背景色已生效", got == want,
                      f"期望 {want} 实得 {got}")

            bgs = {v.get("bg", "").lower() for v in seen.values()}
            check("各主题背景色互不相同", len(bgs) == len(seen),
                  f"{len(bgs)} 种不同背景")

            # 形态令牌
            js(window, "(function(){var t=TH.list.find(x=>x.id==='stellaris');"
                       "if(t) applyTheme(t); return 1;})()")
            time.sleep(0.3)
            d = js(window, "Promise.resolve(document.documentElement.dataset.clip)")
            check("群星主题开启切角嵌板", d == "1", f"clip={d}")
            d2 = js(window, "Promise.resolve(document.documentElement.dataset.glow)")
            check("群星主题开启描边发光", d2 == "1", f"glow={d2}")
            tex = js(window, "Promise.resolve(document.documentElement.dataset.tex)")
            check("群星主题使用网格纹理", tex == "grid", str(tex))

            js(window, "(function(){var t=TH.list.find(x=>x.id==='daylight');"
                       "if(t) applyTheme(t); return 1;})()")
            time.sleep(0.3)
            d3 = js(window, "Promise.resolve(document.documentElement.dataset.clip)")
            check("浅色主题不切角", d3 == "0", f"clip={d3}")
            st = js(window, "Promise.resolve(getComputedStyle("
                            "document.getElementById('stars')).opacity)")
            check("浅色主题下星图几乎不可见",
                  isinstance(st, str) and float(st) < 0.1, f"opacity={st}")

            # 编辑器实时预览
            print("\n--- 6. 主题编辑器 ---")
            js(window, "openThemeEditor()")
            time.sleep(0.8)
            n2 = js(window, "Promise.resolve(document.getElementById('editColors')"
                            ".children.length)")
            check("编辑器渲染出全部颜色令牌",
                  isinstance(n2, int) and n2 == len(th.COLOR_TOKENS),
                  f"{n2}/{len(th.COLOR_TOKENS)}")
            n3 = js(window, "Promise.resolve(document.getElementById('editShapes')"
                            ".children.length)")
            check("编辑器渲染出全部形态令牌",
                  isinstance(n3, int) and n3 == len(th.SHAPE_TOKENS),
                  f"{n3}/{len(th.SHAPE_TOKENS)}")

            js(window, "setTok('bg','#ff0000')")
            time.sleep(0.3)
            bg = js(window, "Promise.resolve(getComputedStyle("
                            "document.documentElement)"
                            ".getPropertyValue('--t-bg').trim())")
            check("改动令牌即时预览", str(bg).lower() == "#ff0000", str(bg))

            js(window, "setFlag('clip',false)")
            time.sleep(0.25)
            dc = js(window, "Promise.resolve(document.documentElement.dataset.clip)")
            check("切换形态即时生效", dc == "0", f"clip={dc}")

            # 保存一份自制主题再删掉
            js(window, "(function(){"
                       "document.getElementById('editName').value='自动化测试主题';"
                       "document.getElementById('editId').value='tt_ui';"
                       "return 1;})()")
            js(window, "themeSave()")
            time.sleep(1.2)
            exists = js(window, "Promise.resolve((TH.list||[])"
                                ".some(x=>x.id==='tt_ui'))")
            check("界面保存主题成功", exists is True, f"exists={exists}")
            if exists is True:
                created.append("tt_ui")
            js(window, "themeDelete()")
            time.sleep(1.0)
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
    print("=" * 66)
    print("主题系统测试")
    print("=" * 66)
    try:
        test_backend()
        test_frontend()
    finally:
        for tid in created:
            try:
                th.delete_user_theme(tid)
            except Exception:
                pass
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
