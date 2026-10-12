"""安装程序：把「熵光矩阵」装成正常的 Windows 应用。

安装内容
--------
* 程序本体复制到安装目录（默认 ``%LOCALAPPDATA%\\Programs\\StellarisModDoctor``，
  无需管理员权限）
* **桌面快捷方式** + **开始菜单快捷方式**（可选，安装时能勾掉）
* 注册表写入卸载信息，于是「设置 → 应用」里能看到它并能正常卸载
* 卸载脚本 ``uninstall.exe`` 一并放置
* 顺手清理改名前（「群星模组医生」）留下的旧快捷方式

关于内部标识
------------
安装目录名、注册表键、exe 名都沿用历史值 ``StellarisModDoctor``，
**不跟着显示名改**。原因：它们同时决定了用户数据目录
（``%LOCALAPPDATA%\\StellarisModDoctor``，里面是配置、主题、备份），
一改就等于让老用户的设置"消失"，旧卸载入口也会失效。
显示名与内部标识分开是这类工具的常规做法。

用法::

    StellarisModDoctor-Setup.exe                # 图形向导
    StellarisModDoctor-Setup.exe /S             # 静默安装（默认选项）
    StellarisModDoctor-Setup.exe /S /D=C:\\App  # 静默装到指定目录
    StellarisModDoctor-Setup.exe /uninstall     # 静默卸载

设计取舍
--------
选 ``%LOCALAPPDATA%\\Programs`` 作为默认位置，是因为写 ``Program Files``
需要管理员权限，会弹出 UAC。装到用户目录可以做到「双击就装好」，
而且卸载时能干净删掉。想装到别处可以在向导里改，或静默模式用 ``/D=``。
"""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import sys
import tempfile
import winreg
from pathlib import Path

APP_NAME = "熵光矩阵"
APP_NAME_EN = "Entropy Light Matrix"
APP_EXE = "StellarisModDoctor.exe"
UNINST_EXE = f"{APP_NAME_EN.replace(' ', '')}-Uninstall.exe"
# 内部标识保持不变：它决定注册表键与数据目录，
# 改了会让老用户的配置/主题/备份读不到，旧的卸载入口也会失效。
APP_ID = "StellarisModDoctor"
VERSION = "1.0.1"
PUBLISHER = "Entropy Light Matrix"

# 改名前用过的显示名与卸载程序名，安装时顺手清理掉，
# 否则桌面上会同时躺着新旧两个快捷方式，开始菜单里也有两份。
LEGACY_NAMES = ("群星模组医生", "Stellaris Mod Doctor", "StellarisModDoctor")
LEGACY_UNINST = ("StellarisModDoctor-Uninstall.exe",)

UNINSTALL_KEY = rf"Software\Microsoft\Windows\CurrentVersion\Uninstall\{APP_ID}"

# 关机时的收尾
DEFAULT_DIR_NAME = APP_ID


def log_to_file(msg: str) -> None:
    """把安装/卸载过程写进日志文件。

    打包后的安装器是**窗口模式**（没有控制台），``print`` 会静默丢失，
    出问题时用户和开发者都看不到原因。所以关键流程一律落盘。
    """
    try:
        base = Path(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"))
        d = base / APP_ID
        d.mkdir(parents=True, exist_ok=True)
        with open(d / "installer.log", "a", encoding="utf-8") as f:
            f.write(f"[{__import__('datetime').datetime.now():%Y-%m-%d %H:%M:%S}] "
                    f"{msg}\n")
    except OSError:
        pass


def _setup_console() -> None:
    """尽量配置控制台输出；窗口模式下没有控制台，失败也无所谓。"""
    try:
        if sys.platform.startswith("win"):
            import ctypes

            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
            ctypes.windll.kernel32.SetConsoleCP(65001)
    except Exception:
        pass
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream is not None:
                stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def _resource_dir() -> Path:
    """安装程序自身所在的目录（即“发行包”目录）。"""
    if _is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _find_asset(name: str) -> Path | None:
    """在发行包目录与开发布局里找一个随附文件（图标、README…）。

    打包后资源就在 exe 旁边；开发模式下则散落在仓库各处，
    所以这里把所有可能的位置都探一遍。
    """
    here = _resource_dir()
    for cand in (
        here / name,                          # 打包后：exe 同级
        here / "dist" / name,                 # 开发：仓库/dist
        here / "smd" / "web" / name,          # 开发：源码里的图标
        here.parent / "smd" / "web" / name,
        here.parent / "dist" / name,
    ):
        if cand.is_file():
            return cand
    return None


def payload_exe() -> Path:
    """找到要安装的程序本体。"""
    here = _resource_dir()
    for cand in (here / APP_EXE,
                 here / "dist" / APP_EXE,          # 开发模式：仓库根目录
                 here.parent / "dist" / APP_EXE):
        if cand.is_file():
            return cand
    raise FileNotFoundError(
        f"找不到 {APP_EXE}。请把它和安装程序放在同一个目录，"
        f"或先运行 PyInstaller 打包。")


def default_install_dir() -> Path:
    """默认安装目录（用户目录下，因此不需要管理员权限、不弹 UAC）。"""
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return Path(base) / "Programs" / DEFAULT_DIR_NAME


def is_installed() -> str:
    """已安装则返回安装目录，否则返回空串。"""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY) as k:
            v, _ = winreg.QueryValueEx(k, "InstallLocation")
            return str(v)
    except OSError:
        return ""


# ---------------------------------------------------------------------------
# 快捷方式
# ---------------------------------------------------------------------------

def _powershell_shortcut(lnk: Path, target: Path, *, args: str = "",
                         workdir: Path | None = None,
                         icon: Path | None = None,
                         desc: str = "") -> tuple[bool, str]:
    """用 WScript.Shell 创建 .lnk。

    走 PowerShell 而不是 pywin32：少一个依赖，装机体积更小。
    """
    lnk.parent.mkdir(parents=True, exist_ok=True)
    ps = (
        "$ErrorActionPreference='Stop';"
        "$w=New-Object -ComObject WScript.Shell;"
        f"$s=$w.CreateShortcut('{lnk}');"
        f"$s.TargetPath='{target}';"
        f"$s.Arguments='{args}';"
        f"$s.WorkingDirectory='{workdir or target.parent}';"
        f"$s.Description='{desc}';"
        + (f"$s.IconLocation='{icon}';" if icon else "")
        + "$s.Save();"
    )
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive",
             "-ExecutionPolicy", "Bypass", "-Command", ps],
            capture_output=True, text=True, timeout=60,
            creationflags=0x08000000,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"调用 PowerShell 失败：{exc}"
    if r.returncode != 0:
        return False, (r.stderr or r.stdout or "").strip()[:200]
    return lnk.exists(), ""


def desktop_dir() -> Path:
    """桌面目录（考虑 OneDrive 重定向）。"""
    try:
        import ctypes
        from ctypes import wintypes

        buf = ctypes.create_unicode_buffer(260)
        CSIDL_DESKTOPDIRECTORY = 0x0010
        if ctypes.windll.shell32.SHGetFolderPathW(
                None, CSIDL_DESKTOPDIRECTORY, None, 0, buf) == 0:
            p = Path(buf.value)
            if p.is_dir():
                return p
    except Exception:
        pass
    return Path(os.path.expanduser("~")) / "Desktop"


def start_menu_dir() -> Path:
    """当前用户的开始菜单「程序」目录。"""
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return Path(base) / "Microsoft" / "Windows" / "Start Menu" / "Programs"


def create_shortcuts(install_dir: Path, exe: Path, *, desktop: bool = True,
                     start_menu: bool = True,
                     icon: Path | None = None) -> list[tuple[str, bool, str]]:
    """创建桌面与开始菜单快捷方式。返回 ``[(名称, 成功, 说明)]``。"""
    out: list[tuple[str, bool, str]] = []
    if desktop:
        lnk = desktop_dir() / f"{APP_NAME}.lnk"
        ok, err = _powershell_shortcut(
            lnk, exe, workdir=install_dir, icon=icon,
            desc=f"{APP_NAME} — 群星模组冲突检测与智能排序")
        out.append((f"桌面快捷方式 → {lnk}", ok, err))
    if start_menu:
        d = start_menu_dir() / APP_NAME
        lnk = d / f"{APP_NAME}.lnk"
        ok, err = _powershell_shortcut(
            lnk, exe, workdir=install_dir, icon=icon,
            desc=f"{APP_NAME} — 群星模组冲突检测与智能排序")
        out.append((f"开始菜单 → {lnk}", ok, err))
        # 开始菜单里再放一个卸载入口
        un = install_dir / UNINST_EXE
        if un.is_file():
            lnk2 = d / f"卸载 {APP_NAME}.lnk"
            ok2, err2 = _powershell_shortcut(
                lnk2, un, workdir=install_dir, icon=icon,
                desc=f"卸载 {APP_NAME}")
            out.append((f"开始菜单 → 卸载入口", ok2, err2))
    return out


def remove_shortcuts() -> list[str]:
    """删除本程序创建的快捷方式（含改名前的旧名字）。"""
    removed: list[str] = []
    targets = [
        desktop_dir() / f"{APP_NAME}.lnk",
        start_menu_dir() / APP_NAME / f"{APP_NAME}.lnk",
        start_menu_dir() / APP_NAME / f"卸载 {APP_NAME}.lnk",
    ]
    for t in targets:
        try:
            if t.is_file():
                t.unlink()
                removed.append(str(t))
        except OSError:
            pass
    d = start_menu_dir() / APP_NAME
    try:
        if d.is_dir() and not any(d.iterdir()):
            d.rmdir()
            removed.append(str(d))
    except OSError:
        pass
    # 改名前的旧快捷方式也要清掉，否则卸载后桌面还留着死链接
    removed.extend(cleanup_legacy(log=lambda *_: None))
    return removed


def cleanup_legacy(*, log=print) -> list[str]:
    """清理改名前留下的快捷方式与卸载程序。

    软件从「群星模组医生」改名为「熵光矩阵」之后，旧的桌面/开始菜单
    快捷方式仍然指着老 exe，开始菜单里也会同时出现两个文件夹。
    安装时顺手清掉，用户不需要自己手动删。

    **只删快捷方式与旧卸载程序**，不碰用户数据目录（配置、主题、备份），
    也不碰安装目录里的其它文件——那些可能正被正在运行的程序占用。
    """
    done: list[str] = []
    dt = desktop_dir()
    sm = start_menu_dir()
    for old in LEGACY_NAMES:
        for p in (dt / f"{old}.lnk",
                  sm / old / f"{old}.lnk",
                  sm / old / f"卸载 {old}.lnk"):
            try:
                if p.is_file():
                    p.unlink()
                    done.append(str(p))
            except OSError:
                pass
        d = sm / old
        try:
            if d.is_dir() and not any(d.iterdir()):
                d.rmdir()
                done.append(str(d))
        except OSError:
            pass
    if done:
        log("清理旧版本的快捷方式：")
        for x in done:
            log(f"  ✓ {x}")
    return done


# ---------------------------------------------------------------------------
# 注册表
# ---------------------------------------------------------------------------

def write_uninstall_registry(install_dir: Path, exe: Path,
                             uninst: Path) -> tuple[bool, str]:
    """写入「应用和功能」里显示的卸载信息。"""
    size_kb = 0
    try:
        for f in install_dir.rglob("*"):
            if f.is_file():
                size_kb += f.stat().st_size // 1024
    except OSError:
        pass
    values = {
        "DisplayName": f"{APP_NAME} ({APP_NAME_EN})",
        "DisplayVersion": VERSION,
        "Publisher": PUBLISHER,
        "DisplayIcon": str(exe),
        "InstallLocation": str(install_dir),
        "UninstallString": f'"{uninst}"',
        "QuietUninstallString": f'"{uninst}" /S',
        "NoModify": 1,
        "NoRepair": 1,
        "EstimatedSize": size_kb,
        "InstallDate": __import__("datetime").datetime.now().strftime("%Y%m%d"),
    }
    try:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY, 0,
                                winreg.KEY_SET_VALUE) as k:
            for name, v in values.items():
                winreg.SetValueEx(k, name, 0,
                                  winreg.REG_DWORD if isinstance(v, int)
                                  else winreg.REG_SZ, v)
    except OSError as exc:
        return False, str(exc)
    return True, ""


def remove_uninstall_registry() -> None:
    """清除「应用和功能」里的卸载登记项。"""
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# 安装 / 卸载
# ---------------------------------------------------------------------------

def normalize_install_dir(chosen: Path) -> Path:
    """把"用户选的位置"换算成**实际安装目录**。

    安装器界面上写的是"安装位置"，用户会填 ``D:/``、``D:\\Games``
    这种**父目录**。若直接拿它当落地目录，程序文件会散落在 D 盘根目录 ——
    既难看，也和"卸载"要删哪个目录对不上。

    所以统一在这里补一层以程序名命名的文件夹：

        D:/          →  D:/StellarisModDoctor
        D:/Games     →  D:/Games/StellarisModDoctor
        D:/StellarisModDoctor      →  原样（已经是目标目录，不重复套）
        %LOCALAPPDATA%/Programs/StellarisModDoctor → 原样

    判据是"最后一段是否已经等于目录名"，而不是简单拼接 ——
    否则重装时会变成 ``…/StellarisModDoctor/StellarisModDoctor``。
    """
    p = Path(chosen)
    # 盘根 / 空路径：最后一段为空或就是盘符
    if p.name == DEFAULT_DIR_NAME:
        return p
    return p / DEFAULT_DIR_NAME


def install(install_dir: Path, *, desktop: bool = True,
            start_menu: bool = True, launch_after: bool = False,
            log=print) -> tuple[bool, str]:
    """执行安装。返回 ``(是否成功, 说明)``。

    ``install_dir`` 是**最终落地目录**；调用方应先过一遍
    :func:`normalize_install_dir`，把"用户选的位置"换算过来。
    这里再兜一次底，避免任何调用路径把文件倒进盘根。
    """
    install_dir = normalize_install_dir(Path(install_dir))
    try:
        src_exe = payload_exe()
    except FileNotFoundError as exc:
        return False, str(exc)

    # 先把安装程序自己身上的图标取出来给快捷方式用
    icon = _find_asset("icon.ico")

    try:
        install_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return False, f"无法创建安装目录 {install_dir}：{exc}"

    # 若程序正在运行，先请用户关掉
    if _is_running(install_dir / APP_EXE):
        return False, "程序正在运行，请先退出再安装。"

    log(f"安装目录：{install_dir}")
    log(f"复制 {src_exe.name} …")
    dst_exe = install_dir / APP_EXE
    try:
        shutil.copy2(src_exe, dst_exe)
        if icon:
            shutil.copy2(icon, install_dir / "icon.ico")
            icon = install_dir / "icon.ico"
        # README 之类的随附文件也一起放过去
        for extra in ("README.md", "README.txt"):
            p = _find_asset(extra)
            if p is not None:
                shutil.copy2(p, install_dir / extra)
    except OSError as exc:
        return False, f"复制文件失败：{exc}"

    # 生成卸载程序：把安装程序自身复制一份改名
    uninst = install_dir / UNINST_EXE
    me = Path(sys.executable).resolve() if _is_frozen() else Path(__file__).resolve()
    try:
        shutil.copy2(me, uninst)
    except OSError as exc:
        log(f"（警告）卸载程序复制失败：{exc}")

    log("创建快捷方式 …")
    # 先清掉改名前的旧快捷方式，免得桌面上新旧两个都躺着
    cleanup_legacy(log=log)
    for name, ok, err in create_shortcuts(
            install_dir, dst_exe, desktop=desktop,
            start_menu=start_menu, icon=icon):
        log(f"  {'✓' if ok else '✗'} {name}" + (f" — {err}" if err else ""))

    log("登记卸载信息 …")
    ok, err = write_uninstall_registry(install_dir, dst_exe, uninst)
    if not ok:
        log(f"  （警告）写入注册表失败：{err}")

    # 首次运行会把主题导出一份，便于用户直接改
    if launch_after:
        try:
            subprocess.Popen([str(dst_exe)], cwd=str(install_dir),
                             creationflags=0x00000008)
        except OSError:
            pass
    return True, str(install_dir)


def _is_running(exe: Path) -> bool:
    """检查目标程序是否在运行。"""
    try:
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {exe.name}",
                              "/FO", "CSV", "/NH"],
                             capture_output=True, text=True, timeout=15,
                             startupinfo=si, creationflags=0x08000000).stdout
        return exe.name.lower() in out.lower()
    except Exception:
        return False


def uninstall(*, keep_data: bool = True, log=print) -> tuple[bool, str]:
    """卸载：删快捷方式、注册表、程序目录。

    ``keep_data`` 为真时保留用户数据（配置、主题、备份、日志），
    这是更安全的默认——重装后设置还在。
    """
    if _is_running(Path(sys.executable)):
        pass  # 卸载程序自己是另一个 exe，不影响
    loc = is_installed()
    target = Path(loc) if loc else Path(sys.executable).resolve().parent

    log("删除快捷方式 …")
    for p in remove_shortcuts():
        log(f"  ✓ {p}")

    log("清除卸载信息 …")
    remove_uninstall_registry()

    data = Path(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")) / APP_ID
    if not keep_data:
        log(f"删除用户数据 {data} …")

    # 程序本体可能正被自己占用，用延迟删除保证一定成功
    me = Path(sys.executable).resolve() if _is_frozen() else None
    log(f"删除程序目录 {target} …")
    ok, err = _schedule_delete(target, skip=me)
    if not ok:
        return False, err

    if not keep_data:
        try:
            shutil.rmtree(data, ignore_errors=True)
        except OSError:
            pass
    else:
        log(f"（已保留用户数据：{data}）")
    return True, str(target)


def _schedule_delete(target: Path, *, skip: Path | None = None
                     ) -> tuple[bool, str]:
    """删除目录。正在运行的卸载程序自身无法删除，交给延迟命令。"""
    if not target.is_dir():
        return True, ""
    failed: list[str] = []
    for item in sorted(target.rglob("*"), key=lambda p: -len(p.parts)):
        try:
            if item.is_file():
                if skip and item.resolve() == skip:
                    continue
                item.unlink()
            elif item.is_dir():
                item.rmdir()
        except OSError:
            failed.append(str(item))
    try:
        target.rmdir()
    except OSError:
        failed.append(str(target))

    if failed:
        # 卸载程序自己还在运行，无法删除自身 → 交给一个延迟命令。
        #
        # 两个坑都踩过了，记在这里：
        #  1. **不能**用 `timeout /t N` 做延时 —— stdin 被重定向时它会直接报
        #     "Input redirection is not supported" 并退出，后面的 rmdir
        #     永远不执行。改用 ping 做延时，任何上下文都可靠。
        #  2. **不能**把 `cmd /c` 后面的命令写成 list —— 参数会被错误地
        #     拆分，实测 ping 返回 123、rmdir 根本不执行。必须传单个字符串。
        try:
            subprocess.Popen(
                f'cmd /c ping 127.0.0.1 -n 3 >nul & rmdir /s /q "{target}"',
                creationflags=0x08000000, close_fds=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL)
            return True, ""
        except OSError as exc:
            return False, f"删除失败：{exc}；残留：{failed[:3]}"
    return True, ""


# ---------------------------------------------------------------------------
# 图形向导
# ---------------------------------------------------------------------------

def run_gui() -> int:
    """tkinter 安装向导。"""
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    root = tk.Tk()
    root.title(f"{APP_NAME} 安装程序")
    root.geometry("560x400")
    root.resizable(False, False)
    try:
        ico = _find_asset("icon.ico")
        if ico is not None:
            root.iconbitmap(str(ico))
    except Exception:
        pass

    state = {
        # `base` 是**用户选的位置**（父目录）；实际落地目录由
        # normalize_install_dir 补上程序文件夹后得到。
        "base": tk.StringVar(value=str(Path(os.environ.get("LOCALAPPDATA")
                                            or os.path.expanduser("~"))
                                      / "Programs")),
        "desktop": tk.BooleanVar(value=True),
        "menu": tk.BooleanVar(value=True),
        "launch": tk.BooleanVar(value=True),
    }
    existing = is_installed()
    if existing:
        # 已装过：显示**它本身**（而不是它的父目录），重装就是覆盖同一处
        state["base"].set(existing)

    frm = ttk.Frame(root, padding=18)
    frm.pack(fill="both", expand=True)

    ttk.Label(frm, text=APP_NAME, font=("Microsoft YaHei UI", 16, "bold")
              ).pack(anchor="w")
    ttk.Label(frm, text="群星模组冲突检测与智能排序启动器",
              foreground="#666").pack(anchor="w", pady=(0, 14))

    ttk.Label(frm, text="安装位置（会在该位置下创建程序文件夹）").pack(anchor="w")
    row = ttk.Frame(frm)
    row.pack(fill="x", pady=(2, 4))
    ent = ttk.Entry(row, textvariable=state["base"])
    ent.pack(side="left", fill="x", expand=True)
    ttk.Button(row, text="浏览…", command=lambda: _browse(
        root, filedialog, state["base"])).pack(side="left", padx=(6, 0))

    # 实时显示"实际会装到哪里"。
    #
    # 为什么不只让人填一个框就完事：安装器把"用户选的位置"和"程序落地目录"
    # 分开了（前者会补一层以程序名命名的文件夹）。不把结果摆出来，
    # 用户填完 D:/ 根本不知道文件会落在哪，也没法判断对不对。
    target_label = ttk.Label(frm, text="", foreground="#0a6")
    target_label.pack(anchor="w", pady=(0, 12))

    def _refresh_target(*_a) -> None:
        try:
            t = normalize_install_dir(Path(state["base"].get().strip() or "."))
            target_label.configure(text=f"实际安装到：{t}")
        except Exception:                     # noqa: BLE001
            target_label.configure(text="实际安装到：—")

    state["base"].trace_add("write", _refresh_target)
    _refresh_target()

    ttk.Checkbutton(frm, text="创建桌面快捷方式",
                    variable=state["desktop"]).pack(anchor="w")
    ttk.Checkbutton(frm, text="创建开始菜单快捷方式",
                    variable=state["menu"]).pack(anchor="w")
    ttk.Checkbutton(frm, text="安装完成后立即启动",
                    variable=state["launch"]).pack(anchor="w", pady=(0, 10))

    logbox = tk.Text(frm, height=8, wrap="word", relief="solid", borderwidth=1)
    logbox.pack(fill="both", expand=True)
    logbox.configure(state="disabled")

    def log(msg: str) -> None:
        logbox.configure(state="normal")
        logbox.insert("end", str(msg) + "\n")
        logbox.see("end")
        logbox.configure(state="disabled")
        root.update_idletasks()

    btns = ttk.Frame(frm)
    btns.pack(fill="x", pady=(12, 0))

    def do_install() -> None:
        raw = state["base"].get().strip()
        if not raw:
            messagebox.showerror(APP_NAME, "请填写安装位置。")
            return
        # 换算成实际落地目录（父目录 → 父目录/程序名）
        d = normalize_install_dir(Path(raw))
        log("=" * 46)
        ok, msg = install(d, desktop=state["desktop"].get(),
                          start_menu=state["menu"].get(),
                          launch_after=False, log=log)
        if ok:
            log("=" * 46)
            log("安装完成。")
            if messagebox.askyesno(APP_NAME,
                                   "安装完成！\n\n现在就运行吗？"):
                try:
                    subprocess.Popen([str(d / APP_EXE)], cwd=str(d),
                                     creationflags=0x00000008)
                except OSError:
                    pass
            root.destroy()
        else:
            log(f"安装失败：{msg}")
            messagebox.showerror(APP_NAME, f"安装失败：\n{msg}")

    def do_uninstall() -> None:
        if not messagebox.askyesno(
                APP_NAME, "确定要卸载吗？\n\n"
                          "用户数据（配置、主题、备份、日志）会被保留。"):
            return
        log("=" * 46)
        ok, msg = uninstall(keep_data=True, log=log)
        log("卸载完成。" if ok else f"卸载失败：{msg}")
        messagebox.showinfo(APP_NAME, "卸载完成。" if ok else f"卸载失败：{msg}")
        root.destroy()

    ttk.Button(btns, text="取消", command=root.destroy).pack(side="right")
    if existing:
        ttk.Button(btns, text="卸载", command=do_uninstall).pack(
            side="right", padx=(0, 8))
    ttk.Button(btns, text="安装", command=do_install).pack(
        side="right", padx=(0, 8))

    log(f"准备安装到：{normalize_install_dir(Path(state['base'].get()))}")
    if existing:
        log(f"检测到已安装于：{existing}（将覆盖安装）")
    root.mainloop()
    return 0


def _browse(root, filedialog, var) -> None:
    """选安装位置。

    选完之后**不要**自作主张改用户填的路径 —— 用户可能就是想手写
    ``D:/StellarisModDoctor``。落地目录由 :func:`normalize_install_dir`
    统一换算，界面上那个"实际安装到："标签会实时显示结果，
    用户看一眼就知道对不对。
    """
    d = filedialog.askdirectory(title="选择安装位置（程序会在此下创建文件夹）",
                                initialdir=var.get() or str(Path.home()))
    if d:
        var.set(d)


# ---------------------------------------------------------------------------
# 静默模式
# ---------------------------------------------------------------------------

def _is_uninstaller_binary(prog: str = "") -> bool:
    """判断当前跑的是不是卸载程序。

    卸载程序就是安装程序改了个名（``*-Uninstall.exe``）。这带来一个歧义：
    ``/S`` 既可以表示「静默安装」，也可以表示「静默卸载」——

    实际踩过的坑：用户在「应用和功能」里点卸载，注册表执行的是
    ``"...-Uninstall.exe" /S``，结果被当成静默安装，于是程序又把自己
    装了一遍，还因为文件被占用而失败退出（exit 1）。

    所以这里按**文件名**判断身份：名字里带 ``uninstall`` 就默认走卸载。

    ``prog`` 可显式传入要判断的程序路径名；留空时自动取当前二进制。
    """
    if prog:
        return "uninstall" in Path(prog).name.lower()
    if _is_frozen():
        name = Path(sys.executable).name
    else:
        name = Path(sys.argv[0] if sys.argv else "").name
    return "uninstall" in name.lower()


def parse_args(argv: list[str]) -> dict:
    """解析安装程序自己的命令行。

    兼容几种常见写法：

    * ``/S`` ``-s`` ``--silent``            静默
    * ``/D=路径`` ``--dir=路径`` ``--dir 路径``  指定安装位置
    * ``/uninstall`` ``--uninstall``        卸载
    * ``/noshortcuts`` ``--no-desktop`` ``--no-menu``  不建快捷方式
    * ``/purge``                            卸载时连用户数据一起删
    * ``/keep-data`` ``--keep-data``        显式保留用户数据（默认即如此）

    ``/S`` 的含义取决于当前二进制的身份：安装程序 = 静默安装，
    卸载程序 = 静默卸载。显式传 ``/uninstall`` 则永远是卸载。

    **两种路径写法都要认**（``--dir=X`` 与 ``--dir X``）：
    只认等号形式时，``--dir C:/x`` 会被当成未知参数静默丢掉，
    然后安装到默认位置 —— 调用方以为装到了指定目录，实际没有。
    那正是"静默忽略未知参数"的典型后果，所以：
    参数处理上宁可**报错**，不要猜。

    另外把 ``unknown`` 一并返回，让调用方能对不认识的参数出声音。
    """
    prog = argv[0] if argv else ""
    opts = {"silent": False, "dir": "", "uninstall": _is_uninstaller_binary(prog),
            "desktop": True, "menu": True, "keep_data": True,
            "help": False, "unknown": []}
    args = list(argv[1:])
    i = 0
    while i < len(args):
        a = args[i]
        low = a.lower()
        if low in ("/s", "-s", "--silent"):
            opts["silent"] = True
        elif low.startswith(("/d=", "/dir=", "--dir=", "-dir=")):
            opts["dir"] = a.split("=", 1)[1].strip('"')
        elif low in ("/d", "/dir", "--dir", "-dir"):
            # 空格分隔形式：值在下一个参数里
            if i + 1 < len(args):
                opts["dir"] = args[i + 1].strip('"')
                i += 1
            else:
                opts["unknown"].append(a)     # 缺值，报给调用方
        elif low in ("/uninstall", "-uninstall", "--uninstall"):
            opts["uninstall"] = True
        elif low in ("/install", "-install", "--install"):
            opts["uninstall"] = False
        elif low in ("/noshortcuts", "--noshortcuts", "--no-shortcuts"):
            opts["desktop"] = False
            opts["menu"] = False
        elif low in ("/nodesktop", "--no-desktop", "--nodesktop"):
            opts["desktop"] = False
        elif low in ("/nostartmenu", "--no-menu", "--nostartmenu"):
            opts["menu"] = False
        elif low in ("/purge", "--purge"):
            opts["keep_data"] = False
        elif low in ("/keep-data", "--keep-data", "--keepdata"):
            # 显式写"保留用户数据"。这本来就是默认行为，但脚本与文档里
            # 常这么写 —— 拒绝它会打断自动化流程（实测踩过：
            # 清理脚本传 --keep-data，被"未知参数校验"拦下并报错退出）。
            opts["keep_data"] = True
        elif low in ("/?", "-h", "--help", "/help"):
            opts["help"] = True
        else:
            opts["unknown"].append(a)
        i += 1
    return opts


def main(argv: list[str] | None = None) -> int:
    """安装程序总入口。

    行为取决于**二进制身份**与参数：文件名带 ``uninstall`` 的走卸载，
    否则走安装；``/S`` 静默、无 ``/S`` 则弹图形向导。
    全过程写日志到 ``%LOCALAPPDATA%\\StellarisModDoctor\\installer.log``
    （打包后是窗口模式，``print`` 看不到，只能靠日志排查）。
    """
    _setup_console()
    argv = list(argv if argv is not None else sys.argv)
    opts = parse_args(argv)
    log_to_file(f"启动：{' '.join(argv)} → {opts}")

    # 不认识的参数要**出声**，不能静默忽略。
    #
    # 静默忽略的后果实测过：调用方传 `--dir C:/x`（空格形式），
    # 解析器只认 `--dir=C:/x`，于是那个参数被丢掉、装到了默认位置 ——
    # 而返回码还是 0，调用方以为装成功了、装到了指定目录。
    # 这类"成功但其实没按你说的做"最难排查，所以这里直接报错退出。
    if opts["unknown"]:
        msg = ("无法识别的参数：" + " ".join(opts["unknown"])
               + "\n支持的写法：/S  --dir <路径>（或 --dir=<路径>）  "
                 "--uninstall  --no-desktop  --no-menu  --purge")
        log_to_file(f"错误：{msg}")
        print(msg)
        if opts["silent"]:
            return 2                      # 静默模式下给非零码，便于脚本判断
        try:
            import tkinter.messagebox as mb
            mb.showerror(APP_NAME, msg)
        except Exception:                 # noqa: BLE001
            pass
        return 2

    if opts.get("help"):
        print(__doc__)
        return 0

    if opts["uninstall"]:
        ok, msg = uninstall(keep_data=opts["keep_data"])
        log_to_file(f"卸载结果 ok={ok} msg={msg}")
        print(("卸载完成：" if ok else "卸载失败：") + msg)
        return 0 if ok else 1

    try:
        src = payload_exe()
        log_to_file(f"载荷程序：{src}")
    except FileNotFoundError as exc:
        log_to_file(f"错误：{exc}")
        print(f"错误：{exc}")
        if opts["silent"]:
            return 2

    if opts["silent"]:
        # 静默安装的 ``--dir`` 也按"用户给的位置"处理：
        # 传 D:/ 就在 D:/StellarisModDoctor 下装，不散进盘根。
        # （install() 里还会再兜一次底。）
        d = normalize_install_dir(Path(opts["dir"])) if opts["dir"] \
            else default_install_dir()
        print(f"{APP_NAME} — 静默安装")
        log_to_file(f"静默安装到 {d}")
        ok, msg = install(d, desktop=opts["desktop"],
                          start_menu=opts["menu"], launch_after=False,
                          log=log_to_file)
        log_to_file(f"安装结果 ok={ok} msg={msg}")
        print(f"安装{'完成' if ok else '失败'}：{msg}")
        return 0 if ok else 1

    return run_gui()


if __name__ == "__main__":
    sys.exit(main())
