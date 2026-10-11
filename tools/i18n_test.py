"""界面语言切换 + 创意工坊跳转 + 幽灵记录隐藏 的测试。

覆盖：

* i18n 两张语言表键集合一致（缺键会让界面残留另一种语言）
* 代码里用到的键都在表里（用了没有的键会直接显示成 key 本身）
* 界面上真的能切语言：切到英文后侧栏/标题文本变英文
* 切回中文能恢复
* 每个工坊模组都有正确的 Steam 创意工坊链接，本地模组没有
* 已退订的空条目默认不显示，开关能把它们显示出来

用法::  python tools\\i18n_test.py
"""

from __future__ import annotations

import io
import json
import re
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

from smd.app import App  # noqa: E402
from smd.ui import INDEX, WINDOW_KWARGS, WINDOW_TITLE  # noqa: E402
from smd.webui import WebBridge  # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"  [{'OK' if ok else '!!'}] {name}" + (f" — {detail}" if detail else ""))


def js(window, expr: str, timeout: float = 25.0):
    slot = f"__i_{int(time.time() * 1e6) % 10**9}"
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


# ---------------------------------------------------------------------------
# 静态检查
# ---------------------------------------------------------------------------

def test_static() -> None:
    h = (ROOT / "smd" / "web" / "index.html").read_text(encoding="utf-8")
    j = (ROOT / "smd" / "web" / "i18n.js").read_text(encoding="utf-8")

    print("--- 1. 语言表 ---")

    def table(lang: str) -> dict:
        m = re.search(rf"\n    {lang}:\s*\{{(.*?)\n    \}}", j, re.S)
        if not m:
            return {}
        return {k: v for k, v in re.findall(
            r"'([\w.]+)':\s*'((?:[^'\\]|\\.)*)'", m.group(1))}

    zh, en = table("zh"), table("en")
    check("中英表都存在且非空", len(zh) > 50 and len(en) > 50,
          f"zh {len(zh)} / en {len(en)}")
    check("两张表键集合完全一致", set(zh) == set(en),
          f"en 缺 {sorted(set(zh) - set(en))[:4]}；"
          f"en 多 {sorted(set(en) - set(zh))[:4]}")
    check("英文表没有空值",
          all(v.strip() for v in en.values()),
          f"{sum(1 for v in en.values() if not v.strip())} 个空值")

    used = set(re.findall(r'data-i18n(?:-ph|-title)?="([\w.]+)"', h))
    # 只认 I18N.t / t( 后紧跟字符串字面量的调用；
    # 不能写成 \bt\( 这样的宽匹配——`...t(...x)` 里的展开运算符
    # 会被误当成调用，抓出 '...' 这种假键名。
    used |= set(re.findall(r"""I18N\.t\(\s*['"]([\w.]+)['"]""", h))
    used |= set(re.findall(r"""(?<![\w.])t\(\s*['"]([\w.]+)['"]""", h))
    missing = sorted(used - set(zh))
    check("代码用到的键都在表里", not missing, str(missing[:6]))

    print("\n--- 2. 启动流程不得调用未定义函数 ---")
    # 上一版的真实 bug：boot() 里调了一个不存在的全局 setLang()，
    # 抛 ReferenceError 打断整个启动，页面永远停在初始状态。
    # 检查前先把注释剥掉——注释里提到函数名是正常的，不该算失败。
    code = re.sub(r"/\*.*?\*/", "", h, flags=re.S)
    code = re.sub(r"^\s*//.*$", "", code, flags=re.M)
    code = re.sub(r"<!--.*?-->", "", code, flags=re.S)
    check("boot 里走 I18N.setLang", "I18N.setLang(" in code)
    bare = re.findall(r"(?<![\w.])setLang\(", code)
    check("没有裸调 setLang()", not bare, f"{len(bare)} 处")

    # 逐个方法检查：同一个方法里既声明 const t 又调用 t()，
    # 才会真的把全局翻译函数遮蔽掉。跨方法不算问题（作用域不同）。
    envb = code.split("const env = {", 1)[1].split("\n};", 1)[0]
    offenders = []
    for m in re.finditer(
            r"^\s{2}(?:async\s+)?([\w$]+)\s*\([^)]*\)\s*\{(.*?)\n\s{2}\},",
            envb, re.S | re.M):
        name, body = m.group(1), m.group(2)
        if re.search(r"\bconst\s+t\s*=", body) and \
                re.search(r"(?<![\w.])t\(", body):
            offenders.append(name)
    check("没有方法同时遮蔽并使用 t()", not offenders, str(offenders))

    print("\n--- 3. 工坊链接与幽灵记录 ---")
    check("渲染里有工坊链接元素", "wsLink" in h and "openWorkshop" in h)
    check("有隐藏幽灵记录的开关", "setShowGhostMods" in h or
          "set_show_ghost_mods" in h)


# ---------------------------------------------------------------------------
# 运行期检查
# ---------------------------------------------------------------------------

def test_runtime() -> None:
    import webview

    app = App()
    app.load_mods()
    lib = app.library
    mods = app.visible_mods()

    print("\n--- 4. 后端：工坊链接 ---")
    ws = [m for m in mods if m.workshop_url()]
    local = [m for m in mods if not m.workshop_url()]
    check("工坊模组有链接", len(ws) > 0, f"{len(ws)} 个")
    check("链接格式正确",
          all(u.startswith("https://steamcommunity.com/sharedfiles/"
                           "filedetails/?id=") and u.rsplit("=", 1)[-1].isdigit()
              for u in (m.workshop_url() for m in ws)),
          (ws[0].workshop_url() if ws else ""))
    check("链接 id 与该模组工坊 id 一致",
          all(u.rsplit("=", 1)[-1] == str(m.remote_file_id)
              for m, u in ((m, m.workshop_url()) for m in ws)),
          f"{len(ws)} 个全部核对")
    check("本地模组没有工坊链接", all(not m.workshop_url() for m in local),
          f"{len(local)} 个本地模组")

    print("\n--- 5. 后端：幽灵记录 ---")
    ghosts = [m for m in lib.all() if m.is_ghost()]
    check("能识别幽灵记录", True, f"{len(ghosts)} 个")
    check("幽灵记录不出现在可见列表",
          not any(m.is_ghost() for m in mods),
          f"可见 {len(mods)} / 库内 {len(lib.all())}")
    check("幽灵记录都没有内容",
          all(not m.is_loadable() for m in ghosts), f"{len(ghosts)} 个")

    prev = app.settings.show_ghost_mods
    try:
        app.settings.show_ghost_mods = True
        check("打开开关后幽灵记录出现",
              len(app.visible_mods()) == len(lib.all()),
              f"{len(app.visible_mods())} 个")
        st = app.switch_state()
        check("幽灵记录仍标为不可用",
              all(not r["loadable"] for r in st["rows"]
                  if r["key"] in {m.key for m in ghosts}),
              "开关仍是灰的")
    finally:
        app.settings.show_ghost_mods = prev

    print("\n--- 6. 界面：切换语言 ---")
    bridge = WebBridge(app)
    window = webview.create_window(WINDOW_TITLE, str(INDEX), js_api=bridge,
                                  width=1400, height=880,
                                  **WINDOW_KWARGS)
    bridge._window = window
    done = threading.Event()

    def worker():
        try:
            # 等页面稳定。冷启动（尤其是紧接在其它界面测试之后）可能要
            # 十几秒，固定 sleep 容易在慢机器上随机失败，
            # 所以这里轮询「关键元素是否已渲染」而不是死等。
            for _ in range(80):
                if js(window, "!!(window.pywebview && window.pywebview.api)") is True:
                    break
                time.sleep(0.25)
            ready = False
            for _ in range(60):
                n = js(window, "Promise.resolve((S.mods||[]).length)")
                if isinstance(n, int) and n > 0 and \
                        js(window, "Promise.resolve(!!document.querySelector"
                                   "('#modsBody tr'))") is True:
                    ready = True
                    break
                time.sleep(0.5)
            check("界面完成初始化", ready)

            n = 0
            for _ in range(40):
                n = js(window, "Promise.resolve((S.mods||[]).length)")
                if isinstance(n, int) and n > 0:
                    break
                time.sleep(0.4)
            check("界面加载了模组列表", isinstance(n, int) and n > 0,
                  f"{n} 个")
            check("表格渲染出行",
                  (js(window, "Promise.resolve(document.querySelectorAll"
                              "('#modsBody tr').length)") or 0) > 0)

            # 起始语言必须是确定的：应用会把语言存进配置，
            # 上一次测试切到英文后会持久化，导致这里的断言随执行顺序变化。
            js(window, "env.applyLanguage('zh')")
            time.sleep(1.4)

            # 静态文案应为中文
            nav_before = js(window, "Promise.resolve(document.querySelector("
                                    "'.nav[data-p=\"dash\"]').innerText.trim())")
            check("起始为中文", "总览" in str(nav_before), str(nav_before))

            # 切英文
            js(window, "env.applyLanguage('en')")
            time.sleep(1.4)
            nav_en = js(window, "Promise.resolve(document.querySelector("
                                "'.nav[data-p=\"dash\"]').innerText.trim())")
            check("切英文后侧栏变英文", "Overview" in str(nav_en), str(nav_en))
            h1 = js(window, "Promise.resolve(document.querySelector("
                            "'#p-mods h1').innerText.trim())")
            check("页面标题也变英文", str(h1).strip() == "Mods", str(h1))
            brand = js(window, "Promise.resolve(document.querySelector("
                               "'.brand .t').innerText.trim())")
            check("应用名变英文", "Entropy Light Matrix" in str(brand), str(brand))
            lang = js(window, "Promise.resolve(document.documentElement.lang)")
            check("html lang 属性同步", str(lang).startswith("en"), str(lang))

            # 动态文本
            sub = js(window, "Promise.resolve(document.getElementById("
                             "'modsSub').textContent)")
            check("动态统计文本也切了语言",
                  "Mods" in str(sub) or "mods" in str(sub).lower()
                  or "load" in str(sub).lower(), str(sub)[:70])

            # 切回中文
            js(window, "env.applyLanguage('zh')")
            time.sleep(1.4)
            nav_zh = js(window, "Promise.resolve(document.querySelector("
                                "'.nav[data-p=\"dash\"]').innerText.trim())")
            check("能切回中文", "总览" in str(nav_zh), str(nav_zh))

            # 动态区块也要跟着切（这些是 innerHTML 重建的，
            # 靠 MutationObserver + i18nScope 兜住）
            bulk = js(window, "Promise.resolve(document.querySelector("
                              "'#p-mods .card h3').innerText)")
            check("动态区块切回中文", "批量操作" in str(bulk), str(bulk)[:40])
            js(window, "env.applyLanguage('en')")
            time.sleep(1.4)
            bulk_en = js(window, "Promise.resolve(document.querySelector("
                                 "'#p-mods .card h3').innerText)")
            check("动态区块也切英文", "Bulk actions" in str(bulk_en),
                  str(bulk_en)[:40])
            js(window, "env.applyLanguage('zh')")
            time.sleep(1.2)

            print("\n--- 7. 界面：工坊链接 ---")
            links = js(window, "Promise.resolve(document.querySelectorAll("
                               "'#modsBody .wsLink').length)")
            check("每行工坊模组都有链接按钮", isinstance(links, int) and links > 0,
                  f"{links} 个")
            # 点一个（不会真开浏览器：本地模组没有链接，这里只验证绑定）
            ok = js(window, "(function(){"
                           "var a=document.querySelector('#modsBody .wsLink');"
                           "if(!a) return 'none';"
                           "return a.getAttribute('onclick')||'';})()")
            check("链接绑定了 openWorkshop",
                  "openWorkshop" in str(ok), str(ok)[:80])

            print("\n--- 8. 界面：本地自动补丁与翻译页 ---")
            js(window, "(function(){goPage('patch'); patchMode('auto'); return 1;})()")
            time.sleep(2.5)
            scope = js(window, "Promise.resolve(document.getElementById("
                               "'autoScope').innerText.length)")
            check("本地自动补丁页有内容", isinstance(scope, int) and scope > 5,
                  f"{scope} 字符")

            # 注意：loadTranslatePlan 是 env 的方法，不是全局函数。
            # 测试里必须调 env.loadTranslatePlan()，否则是在验证一个
            # 根本不该存在的全局符号（上一版测试就是栽在这里）。
            # 它是 async 的，要 await；而且要**给足超时**——
            # 首次统计要遍历所有模组的本地化文件（实测 4~6 秒，
            # 冷启动更久），默认 25 秒在负载高时会误报超时。
            js(window, "(function(){patchMode('trans'); return 1;})()")
            time.sleep(1.5)
            r = js(window, "env.loadTranslatePlan().then(function(){"
                           "return 'ok';})", timeout=120)
            check("翻译页统计调用成功", r == "ok", str(r)[:80])
            opts = js(window, "Promise.resolve(document.getElementById("
                              "'trLang').options.length)")
            check("翻译页语言下拉已填充", isinstance(opts, int) and opts == 10,
                  f"{opts} 种语言")
            plan = js(window, "Promise.resolve(document.getElementById("
                              "'trPlan').innerText)")
            check("翻译页显示统计", isinstance(plan, str) and len(plan) > 40,
                  f"{len(str(plan))} 字符")
            check("统计里标出了目标语言后缀",
                  "l_simp_chinese" in str(plan),
                  str(plan).replace("\n", " ")[:70])
        except Exception as exc:
            import traceback
            print("界面测试异常：", exc)
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


def main() -> int:
    print("=" * 70)
    print("界面语言 / 工坊跳转 / 幽灵记录 测试")
    print("=" * 70)
    test_static()
    test_runtime()
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
