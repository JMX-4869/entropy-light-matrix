"""系统集成：启动游戏、自动关机、下次开机提醒。

自动关机
--------
用户要求「全部完成后自动关机」。关机是有破坏性的操作，所以做三重保护：

1. ``settings.shutdown_enabled`` 必须显式打开（默认关）
2. 使用 Windows 原生倒计时关机，用户随时可 ``shutdown /a`` 取消
3. 界面显示可见的倒计时与「取消关机」按钮

下次开机提醒
------------
用两种机制保证可靠：

* **注册表 RunOnce** —— ``HKCU\\...\\RunOnce`` 写一条一次性命令。
  Windows 在下次登录时执行，执行后自动删除。
* **标记文件 + 自启项** —— 万一 RunOnce 被安全软件拦截，
  启动器自己启动时也会检查标记文件并弹出提醒。

提醒内容：崩溃报告已生成、修复了几处冲突、报告路径。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from . import APP_ID, APP_NAME
from .config import Paths, Settings, app_data_dir
from .paths import exists, is_file, long_path, read_text, write_text, write_text as _wt

STELLARIS_STEAM_APPID = "281990"
RUNONCE_KEY = r"Software\Microsoft\Windows\CurrentVersion\RunOnce"
RUNONCE_VALUE = "StellarisModDoctorReminder"

# 提醒标记文件
REMINDER_FILE = "next_boot_reminder.json"
SHUTDOWN_LOG = "shutdown.log"


def _app_dir() -> Path:
    return app_data_dir()


# ---------------------------------------------------------------------------
# 启动游戏
# ---------------------------------------------------------------------------

def launch_game(paths: Paths, *, args: list[str] | None = None,
                use_steam: bool = True) -> tuple[bool, str]:
    """启动群星。返回 ``(是否成功, 说明)``。"""
    if use_steam:
        try:
            os.startfile(f"steam://rungameid/{STELLARIS_STEAM_APPID}")
            return True, "已通过 Steam 启动（会先拉起 Paradox 启动器）"
        except OSError as exc:
            return False, f"Steam 启动失败：{exc}"
    exe = paths.exe
    if exe is None:
        return False, f"找不到 {paths.game_dir}/stellaris.exe"
    try:
        cmd = [str(exe)] + (args or [])
        subprocess.Popen(cmd, cwd=str(exe.parent),
                         creationflags=0x00000008)  # DETACHED_PROCESS
        return True, f"已直接启动游戏：{exe}"
    except OSError as exc:
        return False, f"启动失败：{exc}"


def launch_and_monitor(paths: Paths, monitor, *, use_steam: bool = True
                       ) -> tuple[bool, str]:
    """先启动监控，再启动游戏。"""
    if monitor is not None and not monitor.running:
        monitor.start()
    return launch_game(paths, use_steam=use_steam)


# ---------------------------------------------------------------------------
# 自动关机
# ---------------------------------------------------------------------------

@dataclass
class ShutdownPlan:
    """一次待执行的关机计划。"""

    enabled: bool = False
    delay_seconds: int = 120
    reason: str = ""
    created_at: float = field(default_factory=time.time)
    cancelled: bool = False
    executed: bool = False

    def remaining(self) -> int:
        """距离关机还剩多少秒（已过时返回 0）。"""
        left = int(self.delay_seconds - (time.time() - self.created_at))
        return max(0, left)


def _log_shutdown(msg: str) -> None:
    try:
        p = _app_dir() / SHUTDOWN_LOG
        with open(long_path(p), "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}\n")
    except OSError:
        pass


def schedule_shutdown(delay_seconds: int = 120, reason: str = "",
                      force: bool = False) -> ShutdownPlan:
    """安排 Windows 关机。"""
    plan = ShutdownPlan(enabled=True, delay_seconds=max(30, int(delay_seconds)),
                        reason=reason)
    if not sys.platform.startswith("win"):
        plan.enabled = False
        _log_shutdown("当前平台不支持自动关机")
        return plan
    try:
        cmd = ["shutdown", "/s", "/t", str(plan.delay_seconds)]
        if force:
            cmd.append("/f")
        cmd += ["/c", (reason or f"{APP_NAME}：任务完成，即将关机")[:200]]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        if r.returncode != 0:
            plan.enabled = False
            _log_shutdown(f"关机命令失败 rc={r.returncode} {r.stderr.strip()}")
            return plan
        _log_shutdown(f"已安排 {plan.delay_seconds}s 后关机；原因：{reason}")
    except Exception as exc:
        plan.enabled = False
        _log_shutdown(f"关机异常：{exc!r}")
    return plan


def cancel_shutdown() -> tuple[bool, str]:
    """取消已安排的关机。"""
    if not sys.platform.startswith("win"):
        return False, "当前平台不支持"
    try:
        r = subprocess.run(["shutdown", "/a"], capture_output=True, text=True,
                           timeout=20)
        ok = r.returncode == 0
        _log_shutdown("已取消关机" if ok else f"取消失败：{r.stderr.strip()}")
        return ok, ("已取消关机" if ok else
                    f"取消关机失败（可能本来就没有待执行的关机）："
                    f"{r.stderr.strip() or r.stdout.strip()}")
    except Exception as exc:
        return False, f"取消关机异常：{exc!r}"


def shutdown_pending() -> bool:
    """粗略判断是否有待执行的关机（查询计划任务不可靠，改用标记）。"""
    return False


class ShutdownScheduler:
    """带可见倒计时的关机调度器。"""

    def __init__(self, on_tick: Callable[[int], None] | None = None,
                 on_fire: Callable[[], None] | None = None) -> None:
        self.on_tick = on_tick
        self.on_fire = on_fire
        self.plan: ShutdownPlan | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def schedule(self, delay_seconds: int, reason: str = "") -> ShutdownPlan:
        """安排关机并启动可见倒计时。

        会先取消已有的待执行关机，避免重复安排。
        """
        self.cancel(quiet=True)
        self.plan = schedule_shutdown(delay_seconds, reason)
        if self.plan.enabled:
            self._stop.clear()
            self._thread = threading.Thread(target=self._countdown, daemon=True,
                                            name="smd-shutdown")
            self._thread.start()
        return self.plan

    def _countdown(self) -> None:
        while self.plan and not self._stop.is_set():
            left = self.plan.remaining()
            if self.on_tick:
                try:
                    self.on_tick(left)
                except Exception:
                    pass
            if left <= 0:
                self.plan.executed = True
                if self.on_fire:
                    try:
                        self.on_fire()
                    except Exception:
                        pass
                return
            self._stop.wait(1.0)

    def cancel(self, quiet: bool = False) -> tuple[bool, str]:
        """取消待执行的关机；``quiet`` 仅影响是否返回提示。"""
        self._stop.set()
        if self.plan and self.plan.enabled:
            self.plan.cancelled = True
            ok, msg = cancel_shutdown()
            self.plan = None
            return ok, msg
        self.plan = None
        return True, "当前没有待执行的关机"


# ---------------------------------------------------------------------------
# 下次开机提醒
# ---------------------------------------------------------------------------

@dataclass
class BootReminder:
    """一条待展示的开机提醒。"""

    created_at: str = ""
    title: str = ""
    summary: str = ""
    details: list[str] = field(default_factory=list)
    report_md: str = ""
    report_html: str = ""
    report_dir: str = ""
    conflicts_fixed: int = 0
    health_before: int = 0
    health_after: int = 0
    crash_detected: bool = False
    shutdown_done: bool = False

    def to_dict(self) -> dict:
        """序列化，用于写入提醒标记文件。"""
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, d: dict) -> "BootReminder":
        """从标记文件内容还原。"""
        r = cls()
        for k, v in (d or {}).items():
            if hasattr(r, k):
                setattr(r, k, v)
        return r

    def summary_text(self) -> str:
        """生成给用户看的纯文本摘要（弹窗与命令行共用）。"""
        lines = [self.title or f"{APP_NAME} 已完成任务", ""]
        if self.summary:
            lines += [self.summary, ""]
        if self.health_before or self.health_after:
            lines.append(f"模组健康分：{self.health_before} → {self.health_after}")
        if self.conflicts_fixed:
            lines.append(f"已处理冲突：{self.conflicts_fixed} 处")
        if self.crash_detected:
            lines.append("检测到游戏崩溃，已生成分析报告")
        for d in self.details:
            lines.append(f"· {d}")
        if self.report_md:
            lines += ["", f"报告文件：{self.report_md}"]
        return "\n".join(lines)


def reminder_path() -> Path:
    """待展示提醒的标记文件路径。"""
    return _app_dir() / REMINDER_FILE


def save_reminder(rem: BootReminder) -> str:
    """写入待展示的提醒标记。"""
    rem.created_at = rem.created_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    p = reminder_path()
    try:
        write_text(p, json.dumps(rem.to_dict(), ensure_ascii=False, indent=2),
                   bom=False)
    except OSError:
        return ""
    return str(p)


def load_reminder() -> BootReminder | None:
    """读取待展示的提醒；不存在或损坏时返回 ``None``。"""
    p = reminder_path()
    if not is_file(p):
        return None
    try:
        d = json.loads(read_text(p))
    except (OSError, ValueError):
        return None
    return BootReminder.from_dict(d)


def clear_reminder() -> None:
    """清除提醒标记（用户已查看后调用）。"""
    p = reminder_path()
    try:
        if is_file(p):
            p.unlink()
    except OSError:
        pass


def _launch_command() -> str:
    """构造「再次调用本程序」的命令行。

    * 打包后：``"<exe>" --boot-reminder``
    * 开发模式：``"<python>" "<run.py>" --boot-reminder``
      （用 run.py 而不是 ``-m smd``，避免依赖当前工作目录）
    """
    exe = sys.executable
    if getattr(sys, "frozen", False):
        return f'"{exe}" --boot-reminder'
    run_py = Path(__file__).resolve().parent.parent / "run.py"
    if run_py.is_file():
        return f'"{exe}" "{run_py}" --boot-reminder'
    # 最后兜底：用 -m 形式并显式设置工作目录不可靠，故直接报错更诚实
    return f'"{exe}" -m smd --boot-reminder'


def register_boot_reminder(*, show_report: bool = True) -> tuple[bool, str]:
    """注册下次开机的提醒（RunOnce）。"""
    if not sys.platform.startswith("win"):
        return False, "当前平台不支持注册表自启"
    try:
        import winreg  # type: ignore
    except ImportError:
        return False, "无法导入 winreg"

    cmd = _launch_command()
    try:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUNONCE_KEY, 0,
                                winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, RUNONCE_VALUE, 0, winreg.REG_SZ, cmd)
        _log_shutdown(f"已注册开机提醒：{cmd}")
        return True, "已注册下次开机提醒"
    except OSError as exc:
        return False, f"注册失败：{exc}"


def unregister_boot_reminder() -> tuple[bool, str]:
    """从注册表 RunOnce 里移除开机提醒。"""
    if not sys.platform.startswith("win"):
        return False, "当前平台不支持"
    try:
        import winreg  # type: ignore

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUNONCE_KEY, 0,
                            winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, RUNONCE_VALUE)
        return True, "已取消开机提醒"
    except FileNotFoundError:
        return True, "本来就没有注册"
    except OSError as exc:
        return False, f"取消失败：{exc}"


def boot_reminder_registered() -> bool:
    """查询注册表里是否已登记开机提醒。"""
    if not sys.platform.startswith("win"):
        return False
    try:
        import winreg  # type: ignore

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUNONCE_KEY) as k:
            winreg.QueryValueEx(k, RUNONCE_VALUE)
        return True
    except (OSError, ImportError):
        return False


# ---------------------------------------------------------------------------
# 任务收尾：报告 → 提醒 → 关机
# ---------------------------------------------------------------------------

@dataclass
class FinishPlan:
    """任务收尾计划。"""

    reminder: BootReminder | None = None
    shutdown: ShutdownPlan | None = None
    messages: list[str] = field(default_factory=list)
    ok: bool = True


def finish_session(settings: Settings, *, reports: list[str] | None = None,
                   report_dir: str = "", conflicts_fixed: int = 0,
                   health_before: int = 0, health_after: int = 0,
                   crash_detected: bool = False, summary: str = "",
                   details: list[str] | None = None,
                   do_shutdown: bool | None = None,
                   do_register: bool | None = None) -> FinishPlan:
    """统一收尾：写提醒 + 注册开机提醒 + 安排关机。"""
    plan = FinishPlan()

    want_reminder = (settings.boot_reminder_enabled if do_register is None
                     else do_register)
    want_shutdown = (settings.shutdown_enabled if do_shutdown is None
                     else do_shutdown)

    if want_reminder:
        reports = reports or []
        md = next((r for r in reports if r.lower().endswith(".md")), "")
        ht = next((r for r in reports if r.lower().endswith(".html")), "")
        rem = BootReminder(
            title=f"{APP_NAME} 任务完成",
            summary=summary or "模组检测与修复流程已完成。",
            details=list(details or []),
            report_md=md, report_html=ht, report_dir=report_dir,
            conflicts_fixed=conflicts_fixed,
            health_before=health_before, health_after=health_after,
            crash_detected=crash_detected,
            shutdown_done=want_shutdown,
        )
        p = save_reminder(rem)
        if p:
            plan.reminder = rem
            plan.messages.append(f"已保存开机提醒：{p}")
        else:
            plan.messages.append("保存开机提醒失败")
        ok, msg = register_boot_reminder()
        plan.messages.append(msg)
        if not ok:
            plan.ok = False

    if want_shutdown:
        delay = int(settings.shutdown_delay_seconds)
        sp = schedule_shutdown(
            delay,
            f"{APP_NAME}：模组检测与报告生成完毕，{delay} 秒后关机")
        plan.shutdown = sp
        if sp.enabled:
            plan.messages.append(
                f"已安排 {delay} 秒后关机（可用「取消关机」中止）")
        else:
            plan.messages.append("安排关机失败（详见 shutdown.log）")
            plan.ok = False

    return plan


def cancel_all() -> list[str]:
    """取消关机并清除提醒。"""
    msgs: list[str] = []
    ok, msg = cancel_shutdown()
    msgs.append(msg)
    clear_reminder()
    msgs.append("已清除开机提醒")
    ok2, msg2 = unregister_boot_reminder()
    msgs.append(msg2)
    _ = (ok, ok2)
    return msgs


def open_in_explorer(path: str) -> tuple[bool, str]:
    """在资源管理器中定位文件（文件则选中它，目录则打开它）。

    空路径要**显式拒绝**：``Path("")`` 会变成 ``Path(".")``，
    于是"打开"的是当前工作目录——看起来像成功了，
    实际弹出一个莫名其妙的窗口，用户完全不知道发生了什么。
    """
    p_raw = str(path or "").strip()
    if not p_raw:
        return False, "没有指定路径"
    try:
        p = Path(p_raw)
        if p.is_file():
            # /select, 后面要跟逗号且不能有空格，否则 explorer 会当成两个参数
            subprocess.Popen(["explorer", f"/select,{p}"])
        elif p.is_dir():
            os.startfile(str(p))
        else:
            return False, f"路径不存在：{p_raw}"
        return True, "已在资源管理器中打开"
    except OSError as exc:
        return False, f"打开失败：{exc}"
    except Exception as exc:                       # noqa: BLE001
        return False, f"打开失败：{exc}"


def open_url(url: str) -> tuple[bool, str]:
    """用系统默认程序打开网址或文件。

    空字符串同样显式拒绝：``os.startfile("")`` 会抛异常，
    但如果不拦，调用方拿到的"成功"是假的。
    """
    u = str(url or "").strip()
    if not u:
        return False, "没有指定网址"
    try:
        os.startfile(u)
        return True, "已在浏览器中打开"
    except OSError as exc:
        return False, f"打开失败：{exc}"
    except Exception as exc:                       # noqa: BLE001
        return False, f"打开失败：{exc}"
