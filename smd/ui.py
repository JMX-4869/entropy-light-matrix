"""图形界面宿主：pywebview + Edge WebView2。

为什么用 WebView 而不是 tkinter
-------------------------------
界面需要展示大量结构化信息（冲突列表、日志分组、表格、进度条、
可折叠面板），用 HTML/CSS 实现远比 tkinter 高效且好看。
Windows 10/11 自带 Edge WebView2 运行时，无需额外安装。

降级策略
--------
若 WebView2 不可用，自动退回系统默认浏览器打开本地界面，
保证在任何环境下都能用。
"""

from __future__ import annotations

import os
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import Any

from . import APP_NAME, APP_NAME_EN, __version__
from .app import App
from .webui import WebBridge

def _resource_root() -> Path:
    """定位随程序分发的静态资源目录。

    * 开发模式：``<项目>/smd/web``
    * PyInstaller onefile：解包到 ``sys._MEIPASS/smd/web``
    * PyInstaller onedir：``<exe目录>/_internal/smd/web``
    """
    candidates: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / "smd" / "web")
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        candidates.append(exe_dir / "_internal" / "smd" / "web")
        candidates.append(exe_dir / "smd" / "web")
    candidates.append(Path(__file__).resolve().parent / "web")
    for c in candidates:
        if (c / "index.html").is_file():
            return c
    return candidates[-1]


WEB_DIR = _resource_root()
INDEX = WEB_DIR / "index.html"
# 界面依赖的静态资源。缺任何一个都会让窗口变成一片黑
# （样式表缺失 → 布局塌陷 → 内容不可见；i18n.js 缺失 → 启动即报错），
# 而开发模式下直接读磁盘是看不出问题的。所以启动时先自检，缺了就明确报错。
REQUIRED_ASSETS = ("index.html", "theme.css", "i18n.js")


def missing_assets() -> list[str]:
    """返回缺失的界面资源文件名（空列表表示齐全）。"""
    return [n for n in REQUIRED_ASSETS if not (WEB_DIR / n).is_file()]

WINDOW_TITLE = f"{APP_NAME} · {APP_NAME_EN}"

# 主窗口的创建参数。
#
# 集中放在这里，是因为**测试与截图工具也要开一模一样的窗口**：
# 之前 shot_pages / bridge_test 各自写了一份 create_window 参数，
# 结果界面改成无边框后它们还在开带原生标题栏的窗口，
# 截出来的图多了一条白框，差点被当成没修好。
#
# 要点：
# * ``frameless`` —— 原生标题栏是系统配色，深色主题下就是一条白框；
#   界面里自绘了 .tbar，拖拽靠 pywebview-drag-region 类。
# * ``shadow=False`` —— 无边框窗口的系统投影按矩形算，
#   深色界面上会露出一圈浅色描边，改用 .app 自己的 1px 描边。
WINDOW_KWARGS: dict = {
    "frameless": True,
    # easy_drag 必须关掉：它让**整个窗口任意位置**长按都能拖动。
    # 关掉之后只有带 pywebview-drag-region 类的元素能拖
    # （界面里的 .tbar 已标注），标题栏以外的区域恢复正常交互。
    "easy_drag": False,
    # 拖拽条高度：pywebview 只认类名，尺寸由元素自身决定。
    "shadow": False,
    "background_color": "#05080f",
    "text_select": True,
    "confirm_close": False,
}


def _icon_path() -> str:
    p = WEB_DIR / "icon.ico"
    return str(p) if p.is_file() else ""


def _check_webview() -> tuple[bool, str]:
    try:
        import webview  # noqa: F401
    except ImportError as exc:
        return False, f"未安装 pywebview：{exc}"
    try:
        from webview.platforms import edgechromium  # noqa: F401
    except Exception:
        try:
            import webview
            gui = webview.guilib
            if gui not in ("edgechromium", "cef", "qt", "gtk"):
                return True, f"使用 {gui} 后端"
        except Exception:
            return True, "后端未知"
    return True, "Edge WebView2 (EdgeChromium)"


def _print_boot() -> None:
    print(f"{APP_NAME} v{__version__}")
    print("=" * 58)


def _acquire_single_instance() -> tuple[Any, bool]:
    """确保同时只跑一个实例。

    为什么必须做：两个实例会**同时改同一批游戏文件**
    （``dlc_load.json``、启动器数据库、``.mod`` 描述符）。
    这类写入没有跨进程锁，两边各自"备份-改-写回"交错执行时，
    后写的那个会把前一个的改动整个盖掉——
    用户看到的是"我明明点了写入，模组却没变"。

    做法用 Windows 命名互斥体：第二个实例创建同名互斥体会拿到
    ``ERROR_ALREADY_EXISTS``，据此判断。

    返回 ``(handle, is_first)``。``handle`` 要一直持有到进程结束，
    提前释放会让保护失效；非 Windows 上直接返回 ``(None, True)``。
    """
    if not sys.platform.startswith("win"):
        return None, True
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32
        # Local\ 前缀：只在当前登录会话内互斥，多用户各自开自己的
        name = f"Local\\{APP_NAME}_single_instance_mutex"
        handle = kernel32.CreateMutexW(None, False, name)
        if not handle:
            return None, True
        ERROR_ALREADY_EXISTS = 183
        if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
            return handle, False
        return handle, True
    except Exception:
        # 拿不到互斥体就别拦着用户启动
        return None, True


def _warn_already_running() -> None:
    """第二个实例的处理：**把已有窗口调到前台**，而不是弹框。

    为什么不用 MessageBox：
    1. 它是**模态阻塞**的，没人点它进程就一直挂着。自动化里调用会直接卡死，
       用户从开始菜单误点两次也会留一个关不掉的隐藏进程。
    2. 用户真正的意图是"我想用这个软件"，不是"我想读一段说明"。
       直接把已经开着的窗口切到前面，正好满足这个意图。

    找不到已有窗口时才退回打印一行提示（有控制台时能看到）。
    """
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        found: list[int] = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def _cb(hwnd, _lparam):
            if not user32.IsWindowVisible(hwnd):
                return True
            n = user32.GetWindowTextLengthW(hwnd)
            if n <= 0:
                return True
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            title = buf.value
            # 标题形如「熵光矩阵 · Entropy Light Matrix」
            if APP_NAME in title:
                found.append(hwnd)
                return False
            return True

        user32.EnumWindows(_cb, 0)
        if found:
            hwnd = found[0]
            # SW_RESTORE：窗口若是最小化，先还原
            user32.ShowWindow(hwnd, 9)
            user32.SetForegroundWindow(hwnd)
            print(f"{APP_NAME} 已经在运行，已把它的窗口切到前台。")
            return
    except Exception:
        pass
    print(f"{APP_NAME} 已经在运行了。请切换到已打开的窗口。"
          f"（同时开两个会互相覆盖对游戏文件的改动）")


def run_gui(debug: bool = False, port: int = 0, page: str = "") -> int:
    """启动图形界面。

    ``page`` 可指定启动时直接打开的页面（见 ``--page``），留空则停在总览。
    """
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    # 单实例保护要在**建 App、读配置之前**做：
    # App() 会构造 Settings，越早拦住越好，免得第二个实例
    # 已经写了一轮配置才发现不该启动。
    _mutex, is_first = _acquire_single_instance()
    if not is_first:
        _warn_already_running()
        return 0

    _print_boot()
    if not INDEX.is_file():
        print(f"错误：找不到界面文件 {INDEX}")
        return 2

    # 资源自检：宁可在启动时报清楚，也不要开出一个黑窗口让用户猜
    missing = missing_assets()
    if missing:
        msg = (f"界面资源缺失：{'、'.join(missing)}\n"
               f"查找位置：{WEB_DIR}\n\n"
               f"这通常是打包时漏了文件。若你用的是源码版，"
               f"请确认 smd/web/ 下的文件完整。")
        print("错误：" + msg)
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(
                None, msg, f"{APP_NAME} — 界面资源缺失", 0x10)
        except Exception:
            pass
        return 2

    ok, backend = _check_webview()
    app = App()
    bridge = WebBridge(app)

    st = app.status()
    print(f"用户数据目录：{st['paths']['user_dir'] or '(未找到)'}")
    print(f"游戏本体目录：{st['paths']['game_dir'] or '(未找到)'}")
    print(f"工坊目录　　：{', '.join(st['paths']['workshop_dirs']) or '(未找到)'}")
    for p in st["path_problems"]:
        print(f"  ! {p}")
    print()

    if not ok:
        print(f"WebView 不可用（{backend}），改用系统浏览器打开界面。")
        return _run_browser_mode(app, bridge, debug)

    import webview

    window = webview.create_window(
        WINDOW_TITLE,
        str(INDEX),
        js_api=bridge,
        width=1280,
        height=840,
        min_size=(1040, 660),
        **WINDOW_KWARGS,
    )
    bridge._window = window

    VALID_PAGES = {
        "dash", "mods", "dlc", "conflict", "solution", "order", "ai",
        "patch", "monitor", "crash", "logs", "appearance", "settings",
    }
    want_page = (page or "").strip().lower()
    if want_page and want_page not in VALID_PAGES:
        print(f"（--page 的值 {page!r} 不是已知页面，已忽略；"
              f"可选：{'、'.join(sorted(VALID_PAGES))}）")
        want_page = ""
    if want_page:
        print(f"启动页面：{want_page}")

    def on_loaded() -> None:
        print("界面已加载。")
        # --page：界面 boot 完成后切到指定页
        if want_page:
            try:
                window.evaluate_js(
                    f"(function(){{ if(typeof goPage==='function') "
                    f"goPage('{want_page}'); return 1; }})()")
            except Exception as exc:
                print(f"（切换到 {want_page} 页失败：{exc}）")
        # 若上次任务留下提醒，界面上立即提示
        rem = app.pending_reminder()
        if rem:
            app.emit("monitor_event", {
                "kind": "info", "severity": "info", "time_str": "",
                "title": "发现上次任务的开机提醒",
                "detail": (rem.get("summary") or "") +
                          "\n报告：" + (rem.get("report_md") or ""),
                "data": {},
            })

    window.events.loaded += on_loaded

    gui = None
    if sys.platform.startswith("win"):
        gui = "edgechromium"
    try:
        webview.start(gui=gui, debug=debug, private_mode=False,
                      storage_path=str(Path(os.environ.get("LOCALAPPDATA",
                                                           str(Path.home())))
                                       / "StellarisModDoctor" / "webview"))
    except Exception as exc:
        print(f"WebView 启动失败：{exc}")
        print("改用系统浏览器模式。")
        return _run_browser_mode(app, bridge, debug)

    # 窗口关闭：停掉后台线程
    if app.monitor:
        app.monitor.stop(wait=False)
    print("界面已关闭。")
    return 0


def _run_browser_mode(app: App, bridge: WebBridge, debug: bool) -> int:
    """降级：起一个本地 HTTP 服务，用默认浏览器打开。"""
    import http.server
    import json
    import socketserver
    import urllib.parse

    from .paths import read_text

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):  # 静音
            pass

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            u = urllib.parse.urlparse(self.path)
            if u.path in ("/", "/index.html"):
                self._send(200, INDEX.read_bytes(), "text/html; charset=utf-8")
                return
            if u.path == "/api/poll":
                q = urllib.parse.parse_qs(u.query)
                since = (q.get("since") or ["0"])[0]
                self._send(200, bridge.poll(since).encode("utf-8"),
                           "application/json; charset=utf-8")
                return
            if u.path.startswith("/api/"):
                name = u.path[5:]
                q = {k: v[0] for k, v in
                     urllib.parse.parse_qs(u.query).items()}
                fn = getattr(bridge, name, None)
                if fn is None:
                    self._send(404, b'{"error":"no such api"}',
                               "application/json")
                    return
                try:
                    res = fn(**q)
                except Exception as exc:
                    res = json.dumps({"error": str(exc)}, ensure_ascii=False)
                self._send(200, str(res).encode("utf-8"),
                           "application/json; charset=utf-8")
                return
            # 静态资源
            rel = u.path.lstrip("/")
            target = (WEB_DIR / rel).resolve()
            try:
                target.relative_to(WEB_DIR.resolve())
            except ValueError:
                self._send(403, b"forbidden", "text/plain")
                return
            if target.is_file():
                ctype = {
                    ".html": "text/html; charset=utf-8",
                    ".css": "text/css; charset=utf-8",
                    ".js": "application/javascript; charset=utf-8",
                    ".png": "image/png", ".svg": "image/svg+xml",
                    ".ico": "image/x-icon",
                }.get(target.suffix.lower(), "application/octet-stream")
                self._send(200, target.read_bytes(), ctype)
                return
            self._send(404, b"not found", "text/plain")

        def do_POST(self) -> None:  # noqa: N802
            self.do_GET()

    class Server(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

    with Server(("127.0.0.1", port or 0), Handler) as httpd:
        real_port = httpd.server_address[1]
        url = f"http://127.0.0.1:{real_port}/"
        print(f"界面地址：{url}")
        print("（浏览器模式：关闭此窗口或按 Ctrl+C 结束）")
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        webbrowser.open(url)
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\n已退出。")
    return 0
