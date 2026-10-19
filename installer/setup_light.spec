# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：**轻量版**安装程序。

产物 ``dist/StellarisModDoctor-Light-Setup.exe``。

和完整版安装包的区别：

* 嵌进去的是 ``StellarisModDoctor-Light.exe``，不是完整版主程序
* 多嵌一个 ``light.flag`` —— ``install.py`` 靠它判断"我要装的是轻量版"，
  于是 exe 名、安装目录、卸载程序名、注册表键全部跟着换一套

这样两个产品可以同时装在一台机器上，互不覆盖、互不误删。
用户数据（``%LOCALAPPDATA%\\StellarisModDoctor``）仍然是共用的 ——
它们是同一个后端的两套界面，配置没必要分家。
"""

from pathlib import Path

SPEC_DIR = Path(SPECPATH)          # noqa: F821  (installer/)
ROOT = SPEC_DIR.parent             # 仓库根目录
WEB = ROOT / "smd" / "web"
ENTRY = SPEC_DIR / "install.py"

datas = []
for name in ("icon.ico", "icon.png"):
    p = WEB / name
    if p.is_file():
        datas.append((str(p), "."))

# 标记文件：告诉 install.py「这是轻量版那份」
FLAG = SPEC_DIR / "light.flag"
FLAG.write_text("light\n", encoding="utf-8")
datas.append((str(FLAG), "."))

# 要装进去的主程序 —— 轻量版
PAYLOAD = ROOT / "dist" / "StellarisModDoctor-Light.exe"
if not PAYLOAD.is_file():
    raise SystemExit(
        f"打包失败：找不到轻量版主程序 {PAYLOAD}\n"
        f"请先构建轻量版（PyInstaller installer/light.spec），"
        f"再构建安装包 —— 安装包要把主程序嵌进去。")
datas.append((str(PAYLOAD), "."))
print(f"[setup_light.spec] 嵌入轻量版主程序：{PAYLOAD.name} "
      f"({PAYLOAD.stat().st_size / 1048576:.2f} MB)")

hiddenimports = ["winreg"]

excludes = [
    # 安装程序自己不需要任何界面框架的 web 部分
    "webview", "clr", "pythonnet", "System", "System.Windows.Forms",
    "bottle", "proxy_tools",
    "smd", "smd.ui", "smd.webui", "smd.app", "smd.ui_light", "smd.ai",
    "matplotlib", "numpy", "pandas", "scipy", "PIL",
    "PyQt5", "PyQt6", "PySide2", "PySide6", "wx",
    "pytest", "setuptools", "pip", "IPython", "notebook",
    "sqlite3.test", "unittest", "pydoc_data", "lib2to3",
    "pyinstaller", "PyInstaller",
]

icon = str(WEB / "icon.ico") if (WEB / "icon.ico").is_file() else None

a = Analysis(  # noqa: F821
    [str(ENTRY)],
    pathex=[str(SPEC_DIR)],
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
    name="StellarisModDoctor-Light-Setup",
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
