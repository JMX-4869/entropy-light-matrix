# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：安装程序。

产物 ``dist/StellarisModDoctor-Setup.exe``。

安装程序**必须**是独立的小体积可执行文件（不能拖上 WebView2 那一堆），
所以这里排除掉 webview / pythonnet 等运行期依赖，只保留标准库 + tkinter
（图形向导要用）。
"""

from pathlib import Path

# SPECPATH 是 .spec 所在目录（<仓库>/installer）。
# PyInstaller 会把 spec 里的相对路径按当前工作目录解析，所以统一用绝对路径。
SPEC_DIR = Path(SPECPATH)          # noqa: F821
ROOT = SPEC_DIR.parent             # 仓库根目录
WEB = ROOT / "smd" / "web"
ENTRY = SPEC_DIR / "install.py"

datas = []
# 图标：同时随包分发，安装时复制给快捷方式用
for name in ("icon.ico", "icon.png"):
    p = WEB / name
    if p.is_file():
        datas.append((str(p), "."))

# 程序本体：**必须打进安装包**，否则用户只下载 Setup.exe 时会报
# "找不到 StellarisModDoctor.exe"。而这个错误在开发机上复现不出来 ——
# dist/ 里正好躺着主程序，`payload_exe()` 顺手就找到了。
#
# 代价是安装包从 ~10MB 涨到 ~23MB（主程序压缩后嵌进去），
# 但"下载一个文件就能装"比体积重要。
PAYLOAD = ROOT / "dist" / "StellarisModDoctor.exe"
if not PAYLOAD.is_file():
    raise SystemExit(
        f"打包失败：找不到主程序 {PAYLOAD}\n"
        f"请先构建主程序（PyInstaller StellarisModDoctor.spec），"
        f"再构建安装包 —— 安装包要把主程序嵌进去。")
datas.append((str(PAYLOAD), "."))
print(f"[setup.spec] 嵌入程序本体：{PAYLOAD.name} "
      f"({PAYLOAD.stat().st_size / 1048576:.2f} MB)")

hiddenimports = [
    "tkinter",
    "tkinter.filedialog",
    "tkinter.messagebox",
    "tkinter.ttk",
]

excludes = [
    # 这些是主程序才需要的，安装器一个都不要
    "webview", "clr", "pythonnet", "bottle", "proxy_tools",
    # 主程序的业务模块也不需要（安装器不 import 它们）
    "smd",
    # 常见大件
    "numpy", "pandas", "scipy", "matplotlib", "PIL", "lxml",
    "PyQt5", "PyQt6", "PySide2", "PySide6", "wx",
    "pytest", "setuptools", "pip", "IPython", "notebook",
    "unittest", "pydoc_data", "lib2to3",
    "pyinstaller", "PyInstaller",
]

icon = str(WEB / "icon.ico") if (WEB / "icon.ico").is_file() else None

a = Analysis(  # noqa: F821
    [str(ENTRY)],
    pathex=[str(SPEC_DIR), str(ROOT)],
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
    name="StellarisModDoctor-Setup",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,          # 向导模式不需要控制台
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=icon,
)
