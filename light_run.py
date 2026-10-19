"""轻量版入口 —— 这个文件只属于轻量版。

**两个产品彻底分开**

* 完整版：``run.py`` → ``smd.cli`` → ``smd.ui``（pywebview + Edge WebView2）
* 轻量版：``light_run.py`` → ``smd.ui_light``（Tkinter）

各自打出来的包里都不含对方的界面代码：
* 完整版的 CLI 里**没有** ``--light`` 开关，也不引用 ``ui_light``
* 轻量版的 spec 把 ``smd.ui`` / ``smd.webui`` / ``webview`` / ``clr``
  全部 exclude 掉，包里连网页版的影子都没有

**为什么要在这里自己开窗口，而不是转交 ``cli.main()``**

``cli.main([])`` 会走到 ``cmd_gui``，那里 ``from .ui import run_gui`` ——
而轻量版的包里没有 ``smd.ui``，会直接 ImportError。所以无参数时**必须**
在这里直接调 ``smd.ui_light``，不能绕道 CLI。

有参数时（``--version`` / ``--scan`` / ``--optimize`` …）才交给 CLI ——
那些是纯命令行功能，跟界面无关。
"""

from __future__ import annotations

import sys
from pathlib import Path

# 开发模式下确保包可被导入（打包后由 PyInstaller 处理）
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))


def _fix_console_encoding() -> None:
    """把控制台切到 UTF-8。窗口模式下没用，命令行调用时会用上。"""
    try:
        if sys.platform.startswith("win"):
            import ctypes

            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
            ctypes.windll.kernel32.SetConsoleCP(65001)
        if sys.stdout is not None:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        if sys.stderr is not None:
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def _show_fatal(exc: BaseException) -> None:
    """无控制台环境下的兜底错误提示。

    打包后 ``console=False``，异常只会打到看不见的 stderr —— 用户看到的
    就是"双击没反应"。必须弹框把原因说出来。

    **这条是血泪教训**：第一次打轻量版时入口写成了 ``smd/light_main.py``
    （相对导入），PyInstaller 把它当顶层脚本执行，直接抛
    ``attempted relative import with no known parent package``。
    而因为是 GUI 程序，崩溃表现为一个对话框、进程还活着 ——
    我据此报了"启动成功，内存 84 MB"，实际屏幕上是个错误框。
    """
    import traceback

    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    print(tb, file=sys.stderr)
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(
            None,
            f"程序启动失败：\n\n{exc}\n\n详细信息：\n{tb[-1500:]}",
            "熵光矩阵 轻量版 — 启动失败",
            0x10,
        )
    except Exception:
        pass


def _run_light() -> int:
    """开轻量窗口。**不 import 任何 pywebview 相关模块。**"""
    from smd.app import App
    from smd.ui_light import run as run_light_ui

    app = App()
    try:
        app.load_mods()
    except Exception:                          # noqa: BLE001
        pass
    run_light_ui(app)
    return 0


def main() -> int:
    """入口：有参数走命令行，没参数开轻量界面。"""
    _fix_console_encoding()
    argv = sys.argv[1:]
    try:
        if argv:
            # --version / --scan / --optimize 等等，纯命令行，与界面无关
            from smd.cli import main as cli_main

            return cli_main(argv)
        return _run_light()
    except SystemExit:
        raise
    except BaseException as exc:  # noqa: BLE001 - 兜底，保证用户能看到原因
        _show_fatal(exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
