"""界面截图工具：启动界面、跑一次扫描、逐个页面截图。

用于验收界面渲染效果（不参与运行时功能）。

用法::

    python tools/shot_pages.py [输出目录]
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

OUT = Path(sys.argv[1] if len(sys.argv) > 1 else ROOT / "tools" / "shots")
OUT.mkdir(parents=True, exist_ok=True)
DONE = threading.Event()


def js(window, expr: str, timeout: float = 40.0):
    slot = f"__shot_{int(time.time() * 1e6) % 10**9}"
    # 注意用换行分隔，避免 expr 以 `//` 注释结尾时把后续代码注释掉
    window.evaluate_js(
        "(function(){\n"
        f"window.{slot}=undefined;\n"
        f"Promise.resolve({expr})\n"
        f".then(function(v){{window.{slot}={{ok:1,v:v}};}})\n"
        f".catch(function(e){{window.{slot}={{ok:0,e:String(e)}};}});\n"
        "return 1;\n"
        "})()"
    )
    end = time.time() + timeout
    while time.time() < end:
        raw = window.evaluate_js(f"JSON.stringify(window.{slot}||null)")
        if raw and raw != "null":
            window.evaluate_js(f"delete window.{slot}")
            box = json.loads(raw)
            return box.get("v") if box.get("ok") else {"__error__": box.get("e")}
        time.sleep(0.1)
    return {"__error__": "timeout"}


def shot(window, name: str) -> None:
    time.sleep(1.6)
    # pywebview 没有截图 API，改用 Win32 抓窗口。
    # -Foreground 是必要的：PrintWindow 抓 DirectComposition 表面时，
    # 窗口必须处于可见/未最小化状态，否则客户区尺寸会是 0x0。
    import subprocess
    r = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
         "-File", str(ROOT / "tools" / "screenshot.ps1"),
         "-TitleMatch", "熵光矩阵",
         "-Out", str(OUT / f"{name}.png"),
         "-Foreground"],
        capture_output=True, text=True, timeout=90,
    )
    print(f"  {name}: {r.stdout.strip()[:90]}")


def worker(window) -> None:
    for _ in range(80):
        if js(window, "!!(window.pywebview&&window.pywebview.api)") is True:
            break
        time.sleep(0.25)
    time.sleep(2.5)

    print("跑一次扫描…")
    js(window, "window.pywebview.api.startScan()")
    for _ in range(500):
        time.sleep(0.6)
        t = js(window, "JSON.stringify((window.S&&S.task)||{})")
        if isinstance(t, str):
            try:
                d = json.loads(t)
            except ValueError:
                continue
            if d and not d.get("running") and d.get("progress", 0) >= 1:
                break
    time.sleep(2.0)

    print("计算方案…")
    js(window, "window.pywebview.api.computeSolution()")
    time.sleep(3.0)

    for page, name in (("dash", "1_dashboard"), ("mods", "2_mods"),
                       ("dlc", "3_dlc"), ("conflict", "4_conflicts"),
                       ("solution", "5_solution"), ("order", "6_order"),
                       ("ai", "7_ai"), ("patch", "8_patch"),
                       ("monitor", "9_monitor"), ("crash", "10_crash"),
                       ("logs", "11_logs"), ("appearance", "12_appearance"),
                       ("settings", "13_settings")):
        js(window, f"goPage('{page}')")
        shot(window, name)

    # 模组列表：切到「磁盘无内容」筛选，展示灰掉的开关与原因提示
    js(window,
       "(function(){goPage('mods'); return 1;})()")
    time.sleep(1.5)
    js(window, "env.showUnusable()")
    shot(window, "22_mods_unusable")

    # 模组列表：勾掉两个开关，展示「待写入」提示条与高亮行
    js(window,
       "(function(){"
       "goPage('mods');"
       "var f=document.getElementById('modFilter'); f.value='all';"
       "renderMods();"
       "var ins=document.querySelectorAll('#modsBody .sw input:not([disabled])');"
       "for(var i=0;i<2 && i<ins.length;i++){"
       "  ins[i].checked=!ins[i].checked;"
       "  ins[i].dispatchEvent(new Event('change'));"
       "}"
       "return 1;})()")
    shot(window, "20_mods_switch_pending")
    js(window, "env.switchBulk('reset')")
    time.sleep(0.8)

    # DLC 页：关掉一个看提示条
    js(window,
       "(function(){goPage('dlc');"
       "var i=document.querySelector('#dlcList .sw input');"
       "if(i){i.checked=false; i.dispatchEvent(new Event('change'));}"
       "return 1;})()")
    shot(window, "21_dlc_pending")
    js(window, "env.dlcBulk('reset')")
    time.sleep(0.8)

    # 逐个内置主题截图，便于对比视觉效果
    for i, tid in enumerate(("stellaris", "dark", "daylight",
                             "highcontrast", "void")):
        js(window, f"(function(){{goPage('dash');"
                   f"var t=(window.TH&&TH.list||[]).find(x=>x.id==='{tid}');"
                   f"if(t) applyTheme(t); return 1;}})()")
        shot(window, f"20_theme_{i+1}_{tid}")

    # 回到群星主题，展开一条冲突
    js(window,
       "(function(){"
       "var t=(window.TH&&TH.list||[]).find(x=>x.id==='stellaris');"
       "if(t) applyTheme(t);"
       "goPage('conflict');"
       "var e=document.querySelector('.fnd');"
       "if(e) e.classList.add('open');"
       "return 1;})()")
    shot(window, "30_conflict_expanded")

    # 补丁工坊：切到「我自己写」并载入模板
    js(window,
       "(function(){"
       "goPage('patch');"
       "patchMode('manual');"
       "var s=document.getElementById('tplSel');"
       "if(s && s.options.length>1){ s.value='0'; applyTemplate(); }"
       "var v=document.getElementById('mText');"
       "if(v) env.validateManual();"
       "return 1;})()")
    shot(window, "31_patch_manual")

    # 补丁工坊：本地自动补丁（只生成草稿，**绝不写盘**）
    #
    # 这里只调 generateAuto（生成草稿、留在内存里），
    # **不要**调 applyAuto —— 那会真的往 `<user_dir>/mod/` 写补丁模组。
    # 截图工具没有沙箱，真写过一次：89 个补丁目录落进了用户的游戏目录。
    js(window,
       "(function(){goPage('patch'); patchMode('auto'); return 1;})()")
    time.sleep(2.5)
    js(window, "env.generateAuto()")
    time.sleep(3.0)
    shot(window, "40_patch_auto")

    # 补丁工坊：一键翻译
    js(window,
       "(function(){patchMode('trans'); return 1;})()")
    time.sleep(1.5)
    js(window, "env.loadTranslatePlan()")
    time.sleep(5.0)
    shot(window, "41_patch_translate")

    # 补丁工坊：批量生成（本地 / AI 两种引擎）。
    # 这个页面会先拉候选列表（没检测过还会自动跑一次检测），是异步的；
    # 只 sleep 容易截到空态，所以轮询等候选真的出现。
    js(window, "(function(){patchMode('batch'); return 1;})()")
    time.sleep(1.5)
    js(window, "env.loadBatchPlan()")
    for _ in range(60):
        n = js(window, "Promise.resolve("
                       "document.querySelectorAll('#batchList .opt').length)")
        if isinstance(n, int) and n > 0:
            break
        time.sleep(1.0)
    time.sleep(1.5)
    shot(window, "42_patch_batch")

    # 补丁工坊：自己翻译（工作表）
    js(window,
       "(function(){patchMode('trans'); return 1;})()")
    time.sleep(1.5)
    js(window, "env.loadTranslationSheet()")
    time.sleep(7.0)
    shot(window, "43_manual_translate")

    # 任务进度浮层：起一次真实扫描，在跑的过程中截图
    js(window, "(function(){goPage('dash'); return 1;})()")
    time.sleep(1.5)
    js(window, "env.scan()")
    # 等面板出现且进度推进到有一点宽度，再截
    for _ in range(50):
        vis = js(window, "Promise.resolve("
                         "!document.getElementById('taskPanel').hidden)")
        if vis is True:
            break
        time.sleep(0.4)
    time.sleep(6.0)
    shot(window, "60_task_progress")
    # 顺手验证「停止」真的能停。
    # 面板在任务结束后约 3.2 秒自动收起，截图要赶在那之前——
    # 所以这里不 sleep，点完立刻截。
    js(window, "(function(){document.getElementById('tpStop').click();"
               "return 1;})()")
    for _ in range(40):
        m = js(window, "Promise.resolve(document.getElementById('tpMsg')"
                       ".textContent)")
        if isinstance(m, str) and "停止" in m:
            break
        time.sleep(0.15)
    shot(window, "61_task_stopped")

    # 冲突页：被补丁解决的条目入口
    js(window, "(function(){goPage('conflict'); return 1;})()")
    time.sleep(3.0)
    js(window, "loadPatchedFilter()")
    time.sleep(4.0)
    shot(window, "70_conflict_patched")

    # 时之虫档案（只读：检索 + 浏览，**不推送**）
    js(window, "(function(){goPage('console'); return 1;})()")
    time.sleep(1.5)
    js(window, "env.loadConsole()")
    time.sleep(4.0)
    js(window, "(function(){queueAdd('debugtooltip');"
               "queueAdd('energy 100000');"
               "queueAdd('research_all_technologies');"
               "queueAdd('instant_build');return 1;})()")
    time.sleep(1.0)
    js(window, "(function(){document.getElementById('wikiQuery')"
               ".value='energy';wikiDoSearch();return 1;})()")
    time.sleep(2.5)
    shot(window, "80_archive")

    js(window, "(function(){wikiClear();wikiOpen('tech');return 1;})()")
    time.sleep(2.5)
    shot(window, "81_archive_browse")

    # 界面语言切到英文
    js(window,
       "(function(){goPage('mods'); env.applyLanguage('en'); return 1;})()")
    time.sleep(2.5)
    shot(window, "50_english_mods")
    js(window,
       "(function(){goPage('patch'); patchMode('auto'); return 1;})()")
    time.sleep(1.5)
    shot(window, "51_english_patch")
    js(window, "env.applyLanguage('zh')")
    time.sleep(1.5)

    print(f"\n截图已保存到 {OUT}")
    DONE.set()


def main() -> int:
    app = App()
    bridge = WebBridge(app)
    window = webview.create_window(WINDOW_TITLE, str(INDEX), js_api=bridge,
                                   width=1400, height=900,
                                   **WINDOW_KWARGS)
    bridge._window = window
    window.events.loaded += lambda: threading.Thread(
        target=worker, args=(window,), daemon=True).start()

    def watch():
        DONE.wait(timeout=900)
        time.sleep(0.8)
        try:
            window.destroy()
        except Exception:
            pass

    threading.Thread(target=watch, daemon=True).start()
    webview.start(gui="edgechromium" if sys.platform.startswith("win") else None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
