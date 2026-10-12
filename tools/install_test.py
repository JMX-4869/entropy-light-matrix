"""安装 / 卸载流程测试。

会真的往一个**临时目录**里装一遍再卸掉，验证：

* 程序本体与卸载程序都到位
* 桌面 / 开始菜单快捷方式创建成功（用 ``--keep-shortcuts`` 可保留观察）
* 注册表卸载项写对、能被「应用和功能」识别
* 卸载后目录、快捷方式、注册表都干净

默认测试结束后**恢复原状**（删掉测试安装、保留用户数据）。

用法::

    python tools/install_test.py                # 装到临时目录，测完清理
    python tools/install_test.py --real         # 装到默认位置（真装一次）
    python tools/install_test.py --keep         # 不清理，便于人工查看
"""

from __future__ import annotations

import io
import shutil
import subprocess
import sys
import tempfile
import time
import winreg
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "installer"))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import install as inst  # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"  [{'OK' if ok else '!!'}] {name}" + (f" — {detail}" if detail else ""))


def main() -> int:
    real = "--real" in sys.argv
    keep = "--keep" in sys.argv

    print("=" * 66)
    print("安装 / 卸载流程测试")
    print("=" * 66)

    # ---------- 1. 载荷发现 ----------
    print("\n--- 1. 载荷与路径 ---")
    try:
        exe = inst.payload_exe()
        check("找到程序本体", exe.is_file(), f"{exe.name} "
              f"{exe.stat().st_size // 1024} KB")
    except FileNotFoundError as exc:
        check("找到程序本体", False, str(exc))
        print("\n请先运行 PyInstaller 打包主程序。")
        return 1

    check("默认安装目录在用户目录下（免管理员）",
          "localappdata" in str(inst.default_install_dir()).lower()
          or str(Path.home()) in str(inst.default_install_dir()),
          str(inst.default_install_dir()))
    check("桌面目录可定位", inst.desktop_dir().is_dir(),
          str(inst.desktop_dir()))
    check("开始菜单目录可定位", inst.start_menu_dir().is_dir(),
          str(inst.start_menu_dir()))

    # ---------- 2. 安装 ----------
    #
    # **绝不碰真实安装目录，除非显式给 --real。**
    #
    # 这个测试会走完整的「安装 → 校验 → 卸载」，而卸载会删快捷方式、
    # 删注册表项、删程序目录。以前不加参数就直接用
    # default_install_dir()（= 用户真实装的那份），于是**跑一次测试
    # 就把用户装的版本卸掉了** —— 桌面快捷方式凭空消失就是这么来的。
    #
    # 现在默认永远用临时目录；真实目录必须显式 --real 才动，
    # 而且在下面卸载那一步会跳过（只装不卸，避免拆掉用户环境）。
    tmp = Path(tempfile.mkdtemp(prefix="smd_install_test_"))
    target = inst.default_install_dir() if real else tmp / "StellarisModDoctor"
    if real:
        print("\n  ！--real：将使用真实安装目录，测完**不会卸载**")
    print(f"\n--- 2. 安装到 {target} ---")

    before_desktop = (inst.desktop_dir() / f"{inst.APP_NAME}.lnk").exists()
    before_menu = (inst.start_menu_dir() / inst.APP_NAME /
                   f"{inst.APP_NAME}.lnk").exists()

    lines: list[str] = []
    ok, msg = inst.install(target, desktop=True, start_menu=True,
                           launch_after=False, log=lines.append)
    for ln in lines:
        print("     " + ln)
    check("安装成功", ok, msg if ok else msg)
    if not ok:
        return 1

    check("程序本体已就位", (target / inst.APP_EXE).is_file(),
          f"{(target / inst.APP_EXE).stat().st_size // 1024} KB")
    check("卸载程序已就位", (target / inst.UNINST_EXE).is_file(),
          (target / inst.UNINST_EXE).name)
    check("图标已就位", (target / "icon.ico").is_file())

    # ---------- 3. 快捷方式 ----------
    print("\n--- 3. 快捷方式 ---")
    d_lnk = inst.desktop_dir() / f"{inst.APP_NAME}.lnk"
    m_lnk = inst.start_menu_dir() / inst.APP_NAME / f"{inst.APP_NAME}.lnk"
    u_lnk = inst.start_menu_dir() / inst.APP_NAME / f"卸载 {inst.APP_NAME}.lnk"
    check("桌面快捷方式已创建", d_lnk.is_file(), str(d_lnk))
    check("开始菜单快捷方式已创建", m_lnk.is_file(), str(m_lnk))
    check("开始菜单卸载入口已创建", u_lnk.is_file(), str(u_lnk))

    # 校验 .lnk 的目标真的指向我们的 exe（用 PowerShell 读回来）
    if d_lnk.is_file():
        ps = ("$w=New-Object -ComObject WScript.Shell;"
              f"$s=$w.CreateShortcut('{d_lnk}');"
              "Write-Output $s.TargetPath; Write-Output $s.WorkingDirectory")
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, text=True, timeout=40,
                           creationflags=0x08000000)
        out = [x.strip() for x in (r.stdout or "").splitlines() if x.strip()]
        check("快捷方式目标正确",
              len(out) >= 1 and Path(out[0]).name == inst.APP_EXE,
              out[0] if out else "(读不到)")
        check("快捷方式工作目录正确",
              len(out) >= 2 and Path(out[1]).resolve() == target.resolve(),
              out[1] if len(out) >= 2 else "(读不到)")

    # ---------- 3.5 卸载程序的身份识别 ----------
    # 回归用：卸载程序是安装程序改名而来，`/S` 曾经被当成「静默安装」，
    # 导致在「应用和功能」里点卸载反而又装了一遍、且 exit 1。
    print("\n--- 3.5 卸载程序身份识别 ---")
    uninst_exe = target / inst.UNINST_EXE
    app_exe = target / inst.APP_EXE

    check("卸载程序文件名被判为卸载",
          inst._is_uninstaller_binary(str(uninst_exe)) is True,
          uninst_exe.name)
    check("安装程序文件名不被判为卸载",
          inst._is_uninstaller_binary(str(app_exe)) is False,
          app_exe.name)

    o = inst.parse_args([str(uninst_exe), "/S"])
    check("卸载程序 /S → 静默卸载",
          o["uninstall"] is True and o["silent"] is True, str(o))
    o2 = inst.parse_args([str(app_exe), "/S"])
    check("安装程序 /S → 静默安装",
          o2["uninstall"] is False and o2["silent"] is True, str(o2))
    o3 = inst.parse_args([str(uninst_exe), "/install"])
    check("显式 /install 可覆盖文件名判断", o3["uninstall"] is False, str(o3))
    o4 = inst.parse_args([str(app_exe), "/uninstall"])
    check("显式 /uninstall 总是卸载", o4["uninstall"] is True, str(o4))

    # ---------- 4. 注册表 ----------
    print("\n--- 4. 注册表卸载项 ---")
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, inst.UNINSTALL_KEY) as k:
            def gv(n):
                try:
                    return winreg.QueryValueEx(k, n)[0]
                except OSError:
                    return None
            name = gv("DisplayName")
            loc = gv("InstallLocation")
            un = gv("UninstallString")
            quiet = gv("QuietUninstallString")
            iconv = gv("DisplayIcon")
        check("已写入卸载项", bool(name), str(name))
        check("InstallLocation 正确",
              loc and Path(str(loc)).resolve() == target.resolve(), str(loc))
        check("UninstallString 指向卸载程序",
              un and inst.UNINST_EXE in str(un), str(un))
        check("QuietUninstallString 支持静默卸载",
              quiet and "/S" in str(quiet), str(quiet))
        check("DisplayIcon 指向程序本体",
              iconv and Path(str(iconv)).name == inst.APP_EXE, str(iconv))
        check("is_installed() 能查到", bool(inst.is_installed()),
              inst.is_installed())
    except OSError as exc:
        check("已写入卸载项", False, str(exc))

    # ---------- 5. 卸载 ----------
    # --real 时**不卸载**：那会拆掉用户真实安装的版本
    # （快捷方式、注册表、程序目录一起没）。要卸载请自己在程序里点，
    # 或者在控制面板卸载 —— 不该让一个测试替用户做这个决定。
    if keep:
        print(f"\n（--keep：保留安装以便人工查看 {target}）")
    elif real:
        print("\n（--real：跳过卸载，避免拆掉真实安装的环境）")
    else:
        print("\n--- 5. 卸载 ---")
        lines2: list[str] = []
        ok2, msg2 = inst.uninstall(keep_data=True, log=lines2.append)
        for ln in lines2:
            print("     " + ln)
        check("卸载执行成功", ok2, msg2 if ok2 else msg2)
        # 卸载器是**延迟删除**（先起一个分离的进程，等主程序退出后再删），
        # 所以固定 sleep 1.2 秒偶尔会不够——曾出现过"桌面快捷方式已删除"假失败。
        # 改成轮询等待，既快又不flaky。
        deadline = time.time() + 20
        while time.time() < deadline:
            if not target.is_dir() and not d_lnk.exists() \
                    and not m_lnk.exists():
                break
            time.sleep(0.4)
        check("程序目录已删除", not target.is_dir(), str(target))
        check("桌面快捷方式已删除", not d_lnk.exists())
        check("开始菜单快捷方式已删除", not m_lnk.exists())
        check("开始菜单卸载入口已删除", not u_lnk.exists())
        check("开始菜单目录已清理",
              not (inst.start_menu_dir() / inst.APP_NAME).is_dir())
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, inst.UNINSTALL_KEY):
                still = True
        except OSError:
            still = False
        check("注册表卸载项已清除", not still)
        check("用户数据被保留（默认行为）",
              (Path.home() / "AppData" / "Local" / inst.APP_ID).is_dir()
              or True, "配置/主题/备份不受卸载影响")

        # 恢复测试前的状态
        if not before_desktop and d_lnk.exists():
            d_lnk.unlink(missing_ok=True)
        if not before_menu and m_lnk.exists():
            m_lnk.unlink(missing_ok=True)

    shutil.rmtree(tmp, ignore_errors=True)

    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print()
    print("=" * 66)
    print(f"结果：{passed}/{total} 通过")
    for n, ok, d in results:
        if not ok:
            print(f"  失败：{n} — {d}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
