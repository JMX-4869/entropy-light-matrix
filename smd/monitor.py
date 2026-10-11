"""后台运行时监控。

在群星运行期间持续盯住这些东西：

* **进程存活** —— 用 ``stellaris.exe`` 是否还在，判断游戏是正常退出还是崩溃
* **崩溃目录** —— ``crashes/`` 下是否冒出新目录（这是崩溃的铁证）
* **error.log 增量** —— 只读新增部分，实时识别「正在恶化的错误」
* **内存占用** —— 群星加模组崩溃的第一大原因是内存，提前预警
* **模组文件变动** —— 工坊模组在游戏中被更新会导致存档不兼容

监控完全在后台线程里跑，不阻塞界面；所有事件推给回调。
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from .config import Paths, Settings
from .logkb import (SEV_ERROR, SEV_FATAL, SEV_LABEL, SEV_NOISE, SEV_WARN,
                    LogLine, annotate, parse_line, rendered)
from .paths import exists, is_dir, is_file, long_path, read_text

GAME_PROCESS = "stellaris.exe"

# 事件类型
EV_START = "game_started"
EV_EXIT = "game_exited"
EV_CRASH = "game_crashed"
EV_ERROR = "log_error"
EV_ERROR_BURST = "error_burst"
EV_MEMORY = "memory_warning"
EV_MOD_CHANGED = "mod_changed"
EV_LOG_ROTATED = "log_rotated"
EV_INFO = "info"


@dataclass
class MonitorEvent:
    """一条监控事件。"""

    kind: str
    severity: str = "info"       # info | warn | error | fatal
    title: str = ""
    detail: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    time: float = field(default_factory=time.time)

    @property
    def time_str(self) -> str:
        """事件发生时刻的 ``HH:MM:SS`` 文本。"""
        return datetime.fromtimestamp(self.time).strftime("%H:%M:%S")

    def to_dict(self) -> dict:
        """序列化，供界面事件流展示。"""
        d = dict(self.__dict__)
        d["time_str"] = self.time_str
        return d


# ---------------------------------------------------------------------------
# 进程探测
# ---------------------------------------------------------------------------

def find_game_pids() -> list[int]:
    """返回所有 ``stellaris.exe`` 的 PID。"""
    pids: list[int] = []
    try:
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        out = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {GAME_PROCESS}", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=15,
            startupinfo=si, creationflags=0x08000000,
        ).stdout
    except Exception:
        return pids
    for line in out.splitlines():
        parts = [p.strip('" ') for p in line.split('","')]
        if len(parts) >= 2 and parts[0].lower() == GAME_PROCESS:
            try:
                pids.append(int(parts[1]))
            except ValueError:
                continue
    return pids


def process_memory_mb(pid: int) -> int:
    """读取进程工作集（MB）。"""
    try:
        import ctypes
        from ctypes import wintypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return 0
        try:
            class PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
                _fields_ = [
                    ("cb", wintypes.DWORD),
                    ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                    ("PrivateUsage", ctypes.c_size_t),
                ]

            c = PROCESS_MEMORY_COUNTERS_EX()
            c.cb = ctypes.sizeof(c)
            psapi = ctypes.WinDLL("psapi", use_last_error=True)
            if psapi.GetProcessMemoryInfo(h, ctypes.byref(c), c.cb):
                return int(c.WorkingSetSize // (1024 * 1024))
        finally:
            k32.CloseHandle(h)
    except Exception:
        return 0
    return 0


def total_ram_mb() -> int:
    """本机物理内存总量（MB）；取不到时返回 0。"""
    try:
        import ctypes

        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        m = MEMORYSTATUSEX()
        m.dwLength = ctypes.sizeof(m)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m)):
            return int(m.ullTotalPhys // (1024 * 1024))
    except Exception:
        pass
    return 0


# ---------------------------------------------------------------------------
# 监控器
# ---------------------------------------------------------------------------

class GameMonitor:
    """群星运行时后台监控。"""

    def __init__(self, paths: Paths, settings: Settings,
                 on_event: Callable[[MonitorEvent], None] | None = None) -> None:
        self.paths = paths
        self.settings = settings
        self.on_event = on_event
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.events: deque[MonitorEvent] = deque(maxlen=2000)
        self.running = False
        self.game_seen = False
        self.game_started_at: float = 0.0
        self.game_ended_at: float = 0.0
        self.crash_detected = False
        self.last_crash_dir: str = ""
        self.peak_memory_mb = 0
        self._log_offsets: dict[str, int] = {}
        self._error_counts: dict[str, int] = {}
        self._recent_error_times: deque[float] = deque(maxlen=400)
        self._burst_reported = False
        self._memory_warned = False
        self._known_crash_dirs: set[str] = set()
        self._mod_snapshot: dict[str, float] = {}
        self._mod_checked = False
        self._stats = {
            "errors_total": 0, "fatal_total": 0, "warn_total": 0,
            "polls": 0, "new_errors": 0,
        }

    # -- 生命周期 ---------------------------------------------------
    def start(self) -> bool:
        """启动后台监控线程。

        返回是否成功启动（已在运行时返回 ``False``）。
        启动时会先给崩溃目录与日志偏移拍一次快照，作为增量判断的基线。
        """
        if self.running:
            return False
        self._stop.clear()
        self._snapshot_state()
        self._thread = threading.Thread(target=self._loop, name="smd-monitor",
                                        daemon=True)
        self._thread.start()
        self.running = True
        self._emit(EV_INFO, "info", "后台监控已启动",
                   f"轮询间隔 {self.settings.monitor_poll_seconds}s；"
                   f"正在等待 {GAME_PROCESS} 启动。")
        return True

    def stop(self, wait: bool = True) -> None:
        """停止监控线程；``wait`` 控制是否等待线程退出。"""
        self._stop.set()
        if wait and self._thread is not None:
            self._thread.join(timeout=5)
        self.running = False
        self._emit(EV_INFO, "info", "后台监控已停止", "")

    @property
    def stopped(self) -> bool:
        """是否已收到停止信号。"""
        return self._stop.is_set()

    # -- 内部 -------------------------------------------------------
    def _emit(self, kind: str, severity: str, title: str, detail: str = "",
              **data: Any) -> None:
        """推送一条事件。

        注意：``data`` 全部走关键字参数。以前这里额外声明了 ``data`` 形参，
        导致调用方写 ``detail=...`` 时抛
        ``TypeError: got multiple values for argument 'detail'``，
        而外层 ``except Exception`` 会把它吞掉——崩溃通知就此静默失效。
        现在改为 ``**data`` 收集，从结构上杜绝这类冲突。
        """
        ev = MonitorEvent(kind=kind, severity=severity, title=title,
                          detail=detail, data=data)
        self.events.append(ev)
        if self.on_event:
            try:
                self.on_event(ev)
            except Exception:
                # 回调是外部代码，不能让它拖垮监控线程；
                # 但要把异常记进事件流，避免再次「静默失败」
                try:
                    import traceback
                    self.events.append(MonitorEvent(
                        kind=EV_INFO, severity="warn",
                        title="事件回调异常",
                        detail=traceback.format_exc()[-600:]))
                except Exception:
                    pass

    def _snapshot_state(self) -> None:
        """记录初始状态：已知的崩溃目录、日志大小。"""
        self._known_crash_dirs = self._list_crash_dirs()
        for name in ("error.log", "game.log", "system.log", "debug.log"):
            p = self.paths.logs_dir / name
            if is_file(p):
                try:
                    self._log_offsets[name] = os.path.getsize(long_path(p))
                except OSError:
                    self._log_offsets[name] = 0
        self._mod_snapshot = self._snapshot_mods()

    def _list_crash_dirs(self) -> set[str]:
        d = self.paths.crashes_dir
        out: set[str] = set()
        if not is_dir(d):
            return out
        try:
            for e in os.scandir(long_path(d)):
                if e.is_dir():
                    out.add(e.name)
        except OSError:
            pass
        return out

    def _snapshot_mods(self) -> dict[str, float]:
        """记录所有 .mod 描述符的修改时间，用于侦测工坊自动更新。"""
        out: dict[str, float] = {}
        md = self.paths.mod_dir
        if not is_dir(md):
            return out
        try:
            for e in os.scandir(long_path(md)):
                if e.is_file() and e.name.lower().endswith(".mod"):
                    try:
                        out[e.name] = e.stat().st_mtime
                    except OSError:
                        continue
        except OSError:
            pass
        return out

    def _loop(self) -> None:
        interval = max(0.5, float(self.settings.monitor_poll_seconds))
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as exc:  # 监控线程绝不能死
                import traceback
                self._emit(EV_INFO, "warn", "监控轮询异常",
                           f"{exc!r}\n{traceback.format_exc()[-900:]}")
            self._stop.wait(interval)

    def _tick(self) -> None:
        self._stats["polls"] += 1
        pids = find_game_pids()
        now = time.time()

        # ---- 进程状态迁移 ----
        if pids and not self.game_seen:
            self.game_seen = True
            self.game_started_at = now
            self.crash_detected = False
            self.peak_memory_mb = 0
            self._memory_warned = False
            self._burst_reported = False
            self._recent_error_times.clear()
            # 游戏启动时会重建日志，重新对齐偏移
            self._snapshot_state()
            self._emit(EV_START, "info", "检测到《群星》已启动",
                       "开始实时监控模组运行状态。", pids=pids)

        if not pids and self.game_seen:
            # 游戏退出：判断是否崩溃
            self.game_ended_at = now
            self.game_seen = False
            self._handle_exit()

        # ---- 运行中检查 ----
        if pids:
            self._check_crash_dirs()
            self._check_logs()
            self._check_memory(pids)
            self._check_mods()
        else:
            # 没在跑也偶尔看看有没有手动启动的崩溃
            self._check_crash_dirs()

    # -- 崩溃 -------------------------------------------------------
    def _check_crash_dirs(self) -> str:
        cur = self._list_crash_dirs()
        new = cur - self._known_crash_dirs
        if not new:
            return ""
        newest = sorted(new)[-1]
        self._known_crash_dirs = cur
        full = str(self.paths.crashes_dir / newest)
        self.last_crash_dir = full
        self.crash_detected = True
        exc = self._read_exception(full)
        self._emit(
            EV_CRASH, "fatal",
            "检测到游戏崩溃",
            f"崩溃转储目录：{newest}\n"
            + (f"异常类型：{exc.get('exception', '未知')}\n" if exc else "")
            + "已自动开始生成崩溃分析报告。",
            crash_dir=full,
            exception=exc.get("exception", ""),
            crash_detail=exc,
        )
        return full

    def _read_exception(self, crash_dir: str) -> dict[str, str]:
        out: dict[str, str] = {}
        p = os.path.join(crash_dir, "exception.txt")
        if not is_file(p):
            return out
        try:
            txt = read_text(p)
        except OSError:
            return out
        m = re.search(r"Unhandled Exception\s+([0-9A-Fa-f]+)\s*\(([^)]+)\)\s*at address\s*(\S+)",
                      txt)
        if m:
            out["code"] = m.group(1)
            out["exception"] = m.group(2)
            out["address"] = m.group(3)
        m2 = re.search(r"Version:\s*(\S+)", txt)
        if m2:
            out["version"] = m2.group(1)
        m3 = re.search(r"Date/Time:\s*(.+)", txt)
        if m3:
            out["datetime"] = m3.group(1).strip()
        # 栈顶几帧
        frames = re.findall(r"^\s*\d+\s+(.+?)(?:\s*\(\+\s*(\d+)\))?\s*$", txt, re.M)
        out["frames"] = " | ".join(f[0].strip() for f in frames[:8])
        out["raw"] = txt
        return out

    def _handle_exit(self) -> None:
        # 退出后给系统几秒落盘
        deadline = time.time() + 6
        while time.time() < deadline and not self._stop.is_set():
            if self._check_crash_dirs():
                return
            time.sleep(1.0)
        # 再补一次日志读取
        self._check_logs()
        if self.crash_detected:
            return
        dur = self.game_ended_at - self.game_started_at
        self._emit(
            EV_EXIT, "info", "《群星》已退出（未检测到崩溃）",
            f"本次运行时长 {_fmt_duration(dur)}；"
            f"峰值内存 {self.peak_memory_mb} MB。",
            duration=dur, peak_mb=self.peak_memory_mb,
        )

    # -- 日志 -------------------------------------------------------
    def _check_logs(self) -> None:
        for name, sev_floor in (("error.log", SEV_WARN), ("game.log", None),
                                ("system.log", None), ("debug.log", None)):
            p = self.paths.logs_dir / name
            if not is_file(p):
                continue
            try:
                size = os.path.getsize(long_path(p))
            except OSError:
                continue
            off = self._log_offsets.get(name, 0)
            if size < off:
                # 日志被重建（游戏重启）
                self._log_offsets[name] = 0
                off = 0
                self._emit(EV_LOG_ROTATED, "info", f"{name} 已重建",
                           "游戏重新开始了日志，监控偏移已重置。")
            if size == off:
                continue
            try:
                with open(long_path(p), "r", encoding="utf-8",
                          errors="replace") as f:
                    f.seek(off)
                    chunk = f.read(max(0, size - off))
            except OSError:
                continue
            self._log_offsets[name] = size
            if name != "error.log":
                continue
            self._process_error_chunk(chunk)

    def _process_error_chunk(self, chunk: str) -> None:
        lines = chunk.splitlines()
        interesting: list[LogLine] = []
        for raw in lines:
            raw = raw.strip()
            if not raw or raw in "{}":
                continue
            # 跳过脚本 dump 的块内容（error.log 里大量此类噪音）
            if raw.startswith(("type=", "id=", "opener_id=", "random=",
                               "random_allowed=", "from=", "prev=", "}")):
                continue
            ll = parse_line(raw)
            self._stats["errors_total"] += 1
            if ll.severity == SEV_NOISE:
                continue
            if ll.severity == SEV_FATAL:
                self._stats["fatal_total"] += 1
            elif ll.severity == SEV_ERROR:
                self._stats["new_errors"] += 1
            else:
                self._stats["warn_total"] += 1

            key = ll.entry.id if ll.entry else "raw"
            self._error_counts[key] = self._error_counts.get(key, 0) + 1
            now = time.time()
            self._recent_error_times.append(now)
            if ll.severity in (SEV_FATAL, SEV_ERROR):
                interesting.append(ll)
            elif len(interesting) < 30 and ll.severity == SEV_WARN:
                interesting.append(ll)

        # 只推送「真正值得看的」前若干条，避免刷屏
        for ll in interesting[:12]:
            r = rendered(ll) if ll.entry else {}
            self._emit(
                EV_ERROR,
                "fatal" if ll.severity == SEV_FATAL else (
                    "error" if ll.severity == SEV_ERROR else "warn"),
                r.get("title") or ll.message[:80],
                (r.get("meaning", "") + "\n" +
                 (f"成因：{r['cause']}\n" if r.get("cause") else "") +
                 (f"建议：{r['fix']}" if r.get("fix") else "")).strip(),
                rule=ll.entry.id if ll.entry else "",
                category=r.get("category", ""),
                source=ll.source, message=ll.message, raw=ll.raw,
            )

        # 错误爆发检测：1 分钟内超过 300 条
        self._detect_burst()

    def _detect_burst(self) -> None:
        if self._burst_reported or len(self._recent_error_times) < 50:
            return
        now = time.time()
        recent = [t for t in self._recent_error_times if now - t <= 60]
        if len(recent) >= 300:
            self._burst_reported = True
            worst = sorted(self._error_counts.items(), key=lambda kv: -kv[1])[:5]
            self._emit(
                EV_ERROR_BURST, "error",
                f"错误爆发：1 分钟内新增 {len(recent)} 条错误",
                "错误数量突然飙升通常意味着某个模组开始大量报错，"
                "或游戏状态已经损坏。建议尽快存档并检查下面的高频错误。\n"
                + "\n".join(f"  · {k}：{v} 次" for k, v in worst),
                count=len(recent), top=worst,
            )

    # -- 内存 -------------------------------------------------------
    def _check_memory(self, pids: list[int]) -> None:
        mb = max((process_memory_mb(p) for p in pids), default=0)
        if mb <= 0:
            return
        if mb > self.peak_memory_mb:
            self.peak_memory_mb = mb
        threshold = int(self.settings.monitor_memory_warn_mb)
        if threshold > 0 and mb >= threshold and not self._memory_warned:
            self._memory_warned = True
            total = total_ram_mb()
            ratio = (mb / total * 100) if total else 0
            self._emit(
                EV_MEMORY, "warn",
                f"内存占用偏高：{mb} MB"
                + (f"（占物理内存 {ratio:.0f}%）" if ratio else ""),
                "群星是原生 64 位程序，但在大量模组 + 巨型星系下仍会耗尽内存，"
                "表现为突然卡死然后崩溃。\n"
                "建议：① 立刻手动存档；② 关闭不用的模组；"
                "③ 降低星系规模与贴图质量；④ 重启游戏释放碎片。",
                mb=mb, total=total, ratio=ratio,
            )

    # -- 模组变动 ---------------------------------------------------
    def _check_mods(self) -> None:
        if not self.settings.monitor_workshop_stale_check:
            return
        if self._mod_checked and self._stats["polls"] % 30 != 0:
            return
        self._mod_checked = True
        cur = self._snapshot_mods()
        changed = [k for k, v in cur.items()
                   if k in self._mod_snapshot and abs(v - self._mod_snapshot[k]) > 1]
        added = [k for k in cur if k not in self._mod_snapshot]
        removed = [k for k in self._mod_snapshot if k not in cur]
        if changed or added or removed:
            self._mod_snapshot = cur
            parts = []
            if changed:
                parts.append(f"更新了 {len(changed)} 个：" + "、".join(changed[:5]))
            if added:
                parts.append(f"新增 {len(added)} 个：" + "、".join(added[:5]))
            if removed:
                parts.append(f"移除 {len(removed)} 个：" + "、".join(removed[:5]))
            self._emit(
                EV_MOD_CHANGED, "warn",
                "游戏运行期间模组文件发生了变化",
                "\n".join(parts) +
                "\n\n在游戏运行时更新/订阅模组是**存档损坏与崩溃的高危操作**。"
                "当前存档可能已不兼容，建议退出后重启游戏再继续。",
                changed=changed[:20], added=added[:20], removed=removed[:20],
            )

    # -- 对外查询 ---------------------------------------------------
    def status(self) -> dict:
        """当前监控状态快照（进程、内存峰值、错误计数等）。"""
        pids = find_game_pids()
        return {
            "running": self.running,
            "game_running": bool(pids),
            "pids": pids,
            "game_seen": self.game_seen,
            "started_at": self.game_started_at,
            "peak_memory_mb": self.peak_memory_mb,
            "crash_detected": self.crash_detected,
            "last_crash_dir": self.last_crash_dir,
            "stats": dict(self._stats),
            "error_counts": dict(sorted(self._error_counts.items(),
                                        key=lambda kv: -kv[1])[:20]),
        }


def _fmt_duration(sec: float) -> str:
    sec = max(0, int(sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h} 小时 {m} 分"
    if m:
        return f"{m} 分 {s} 秒"
    return f"{s} 秒"
