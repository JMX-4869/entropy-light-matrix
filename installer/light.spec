# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：轻量版。

产物 ``dist/StellarisModDoctor-Light.exe``（单文件，免安装）。

**和完整版的差别只有一个：不要 WebView2。**

完整版 12.4 MB 里有相当一部分是 pythonnet / clr / webview 那套 ——
而轻量版从头到尾只用 tkinter，那些东西一个 import 都不需要。所以这里：

* ``hiddenimports`` 只列 ``smd.*``，不列 clr / pythonnet / webview / System
* ``excludes`` 把 ``webview`` / ``clr`` / ``pythonnet`` / ``System`` 全部踢掉
* **保留** tkinter（完整版反而要排除它）

界面资源仍然要显式列出：图标要给窗口和任务栏用。

``smd/ui.py`` / ``smd/webui.py`` 还会被静态分析带进来（``cli.py`` 的
``cmd_gui`` 引用了它们），但它们的顶层 ``import webview`` 被 exclude 掉后
永远不会执行 —— 轻量版走的是 ``--light`` 分支。
"""

from pathlib import Path

SPEC_DIR = Path(SPECPATH)  # noqa: F821  (installer/)
ROOT = SPEC_DIR.parent     # 仓库根
WEB = ROOT / "smd" / "web"

datas = []
for name in ("icon.ico", "icon.png"):
    p = WEB / name
    if p.is_file():
        datas.append((str(p), "smd/web"))

# 知识库数据 —— 轻量版也有「知识库」页签，漏了就是空列表
WIKI = ROOT / "smd" / "data"
if (WIKI / "wiki.json").is_file():
    datas.append((str(WIKI / "wiki.json"), "smd/data"))

hiddenimports = [
    "tkinter",
    "tkinter.ttk",
    "tkinter.filedialog",
    "tkinter.messagebox",
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
    "smd.theme",
    "smd.ui_light",
    "smd.theme",
]


excludes = [
    # ── 轻量版的全部意义就在这几行
    "webview",
    "clr",
    "pythonnet",
    "System",
    "System.Windows.Forms",
    "bottle",
    "proxy_tools",
    # 网页版的两个界面模块 —— 轻量版一行都用不到，
    # 打进去只是白占体积和误导人
    "smd.ui",
    "smd.webui",
    # ── 其余大件
    "matplotlib", "numpy", "pandas", "scipy", "PIL",
    "PyQt5", "PyQt6", "PySide2", "PySide6", "wx",
    "pytest", "setuptools", "pip", "IPython", "notebook",
    "sqlite3.test", "unittest", "pydoc_data", "lib2to3",
    "pyinstaller", "PyInstaller",
]

icon = str(WEB / "icon.ico") if (WEB / "icon.ico").is_file() else None

a = Analysis(  # noqa: F821
    [str(ROOT / "light_run.py")],
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
    name="StellarisModDoctor-Light",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=icon,
)
