"""统一入口。

* 双击 exe / ``python run.py``        → 打开图形界面
* ``python run.py --scan``            → 命令行扫描
* ``python run.py --boot-reminder``   → 显示开机提醒（RunOnce 调用）

打包后 ``sys.frozen`` 为真，此时把命令行参数原样交给 :func:`smd.cli.main`。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# 开发模式下确保包可被导入（打包后由 PyInstaller 处理）
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))


def _fix_console_encoding() -> None:
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
    """无控制台环境下的兜底错误提示。"""
    import traceback

    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    print(tb, file=sys.stderr)
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(
            None,
            f"程序启动失败：\n\n{exc}\n\n详细信息：\n{tb[-1500:]}",
            "熵光矩阵 — 启动失败",
            0x10,
        )
    except Exception:
        pass


def main() -> int:
    """程序总入口：修好控制台编码后转交 :func:`smd.cli.main`，并对启动失败做兜底提示。"""
    _fix_console_encoding()
    try:
        from smd.cli import main as cli_main

        return cli_main()
    except SystemExit:
        raise
    except BaseException as exc:  # noqa: BLE001 - 兜底，保证用户能看到原因
        _show_fatal(exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
