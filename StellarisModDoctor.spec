# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置。

产物：``dist/StellarisModDoctor.exe``（单文件，免安装）。

要点
----
* 把 ``smd/web`` 整个打进包（界面 HTML + 图标），运行时用
  ``sys._MEIPASS`` 定位（见 ``smd/ui.py:_resource_root``）。
* ``pythonnet``/``clr`` 必须显式声明：pywebview 在 Windows 上靠它
  调 WebView2，PyInstaller 的静态分析抓不到。
* 排除 tkinter/matplotlib 等无用大件，控制体积。
"""

import sys
from pathlib import Path

ROOT = Path(SPECPATH)  # noqa: F821  (PyInstaller 注入)
WEB = ROOT / "smd" / "web"

# 界面资源必须**逐个显式列出**：漏一个就会在打包版里表现为
# 页面无样式或空白（开发模式因为直接读磁盘，看不出问题）。
WEB_ASSETS = (
    "index.html",     # 界面主体
    "theme.css",      # 主题样式表（令牌驱动）—— 漏掉会让整个界面变成黑屏
    "i18n.js",        # 界面语言表 —— 漏掉会让语言切换与启动流程报错
    "icon.png",
    "icon.ico",
)
datas = [(str(WEB / n), "smd/web") for n in WEB_ASSETS if (WEB / n).is_file()]

# 「时之虫档案」的知识库数据。同样必须显式列出：
# 漏掉的话打包版里档案是空的，而开发模式看不出问题
# （它会直接读磁盘上的 smd/data/wiki.json）。
WIKI = ROOT / "smd" / "data"
if (WIKI / "wiki.json").is_file():
    datas.append((str(WIKI / "wiki.json"), "smd/data"))

hiddenimports = [
    "clr",
    "pythonnet",
    "webview",
    "webview.platforms.edgechromium",
    "webview.platforms.winforms",
    "System",
    "System.Windows.Forms",
    "bottle",
    "proxy_tools",
    "smd",
    "smd.ai",
    "smd.ai.client",
    "smd.ai.patchgen",
    "smd.ai.prompt",
    "smd.app",
    "smd.apply",
    "smd.cli",
    "smd.config",
    "smd.conflict",
    "smd.logkb",
    "smd.modinfo",
    "smd.monitor",
    "smd.patch",
    "smd.patch.service",
    "smd.paradox",
    "smd.paths",
    "smd.report",
    "smd.scanner",
    "smd.solver",
    "smd.system",
    "smd.ui",
    "smd.webui",
]

excludes = [
    # 轻量界面属于另一个产品（light_run.py + ui_light.py），
    # 完整版里一行都不该有
    "smd.ui_light",
    "tkinter", "matplotlib", "numpy", "pandas", "scipy", "PIL",
    "PyQt5", "PyQt6", "PySide2", "PySide6", "wx",
    "pytest", "setuptools", "pip", "IPython", "notebook",
    "sqlite3.test", "unittest", "pydoc_data", "lib2to3",
    "pyinstaller", "PyInstaller",
]

icon = str(WEB / "icon.ico") if (WEB / "icon.ico").is_file() else None

a = Analysis(  # noqa: F821
    [str(ROOT / "run.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="StellarisModDoctor",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,          # GUI 程序，不弹控制台
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=icon,
)
