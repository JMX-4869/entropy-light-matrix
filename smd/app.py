"""应用控制器：把各模块串成完整业务流程。

职责
----
* 持有当前配置、模组库、索引、检测报告、求解方案
* 提供界面可调用的同步 / 异步方法
* 管理后台监控线程与自动关机调度
* 把耗时任务放进工作线程，通过回调把进度推给界面

界面层（``ui.py`` 的 JS Bridge）只调用这里的方法，不直接碰底层模块。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable

from . import APP_NAME, APP_NAME_EN, __version__
from .apply import (ApplyResult, apply_solution, list_backups, make_backup,
                    remove_patch_mods, rollback)
from .config import Paths, Settings, default_settings, find_game_install, find_user_dir
from .conflict import ConflictDetector, ConflictReport, SEV_LABEL, game_version_from_meta
from .logkb import analyze_log, iter_log_files
from .modinfo import ModLibrary, load_library
from .monitor import GameMonitor, MonitorEvent, find_game_pids
from .paths import human_size, is_dir, is_file, long_path, read_text, write_text
from .report import (build_html, build_markdown, find_crash_dirs,
                     generate_crash_report, parse_meta_yml, read_exception,
                     report_output_dir)
from .scanner import IndexSet, Scanner, _parse_localisation
from .solver import OrderSolver, Solution, TIER_LABEL
from .system import (BootReminder, ShutdownScheduler, cancel_all,
                     clear_reminder, finish_session, launch_game,
                     load_reminder, open_in_explorer, open_url,
                     boot_reminder_registered, register_boot_reminder)


@dataclass
class Task:
    """一个后台任务的进度状态。"""

    name: str = ""
    message: str = ""
    progress: float = 0.0
    running: bool = False
    started: float = 0.0
    finished: float = 0.0
    error: str = ""
    result: Any = None
    stage: str = ""
    cancellable: bool = True
    cancelled: bool = False

    def to_dict(self) -> dict:
        """任务的界面快照。

        把内部状态压成可直接下发给前端的 ``dict``：任务名、当前消息、三位小数
        进度、是否在跑、错误文本，以及已运行的秒数（``elapsed``：任务结束时按
        「结束 - 开始」算，未开始则为 0）。

        额外带上 ``cancellable`` / ``cancelled``：界面据此决定是否显示「停止」
        按钮、以及停下之后要不要提示"已停止"而不是"失败"。
        ``indeterminate`` 表示进度不可估算（例如等 AI 回包），
        此时进度条应画成来回扫动的形式，而不是卡在某个固定百分比假装有进度。
        """
        return {
            "name": self.name, "message": self.message,
            "progress": round(self.progress, 3), "running": self.running,
            "error": self.error,
            "stage": self.stage,
            "cancellable": bool(self.cancellable),
            "cancelled": bool(self.cancelled),
            # 进度还是 0 说明连第一阶段都没报——别画成 0% 糊弄人
            "indeterminate": bool(self.running and self.progress <= 0.001),
            "elapsed": round((self.finished or time.time()) - self.started, 1)
            if self.started else 0,
        }


class App:
    """应用控制器。"""

    def __init__(self, *, push: Callable[[str, dict], None] | None = None) -> None:
        self.settings: Settings = Settings.load()
        self.library: ModLibrary | None = None
        self.indexes: IndexSet = IndexSet()
        self.report: ConflictReport | None = None
        self.solution: Solution | None = None
        self.monitor: GameMonitor | None = None
        self.shutdown_scheduler = ShutdownScheduler(
            on_tick=self._on_shutdown_tick,
            on_fire=self._on_shutdown_fire,
        )
        self.task = Task()
        self.last_apply: ApplyResult | None = None
        self.last_report: dict | None = None
        self.log_text: list[str] = []
        self._push = push
        self._lock = threading.RLock()
        self._cancel = threading.Event()
        self._started_at = time.time()
        self._monitor_events: list[dict] = []
        self._checksum_report: dict | None = None

    # ------------------------------------------------------------------
    # 基础设施
    # ------------------------------------------------------------------
    def emit(self, channel: str, payload: dict) -> None:
        """向界面推送消息。"""
        if self._push:
            try:
                self._push(channel, payload)
            except Exception:
                pass

    def log(self, msg: str, level: str = "info") -> None:
        """追加一行运行日志并推送给界面。

        ``msg`` 会自动补上 ``HH:MM:SS`` 时间戳；``level`` 取 ``info`` / ``warn`` /
        ``error``，界面据此着色。日志同时留在内存里供日志面板回看，超过 800 行
        会丢弃最早的 200 行（不写文件）。
        """
        line = f"[{datetime.now():%H:%M:%S}] {msg}"
        self.log_text.append(line)
        if len(self.log_text) > 800:
            del self.log_text[:200]
        self.emit("log", {"line": line, "level": level})

    def _set_task(self, name: str, message: str = "", progress: float = 0.0,
                  *, cancellable: bool = True) -> None:
        self.task = Task(name=name, message=message, progress=progress,
                         running=True, started=time.time(), stage=message,
                         cancellable=cancellable)
        self.emit("task", self.task.to_dict())

    def _task_progress(self, message: str, progress: float) -> None:
        self.task.message = message
        self.task.progress = max(0.0, min(1.0, progress))
        if not self.task.stage:
            self.task.stage = message
        self.emit("task", self.task.to_dict())

    def _finish_task(self, result: Any = None, error: str = "") -> None:
        self.task.running = False
        self.task.finished = time.time()
        self.task.progress = 1.0
        self.task.error = error
        self.task.result = result
        self.task.cancelled = self._cancel.is_set()
        self.emit("task", self.task.to_dict())

    def _cancelled_result(self, what: str = "任务") -> dict:
        """任务因取消而提前收尾时的统一返回值。"""
        return {"ok": False, "cancelled": True,
                "error": f"{what}已停止（用户取消）"}

    def run_async(self, name: str, fn: Callable[[], Any],
                  on_done: Callable[[Any, str], None] | None = None,
                  *, cancellable: bool = True) -> bool:
        """在后台线程里跑一个任务。同一时刻只允许一个。"""
        with self._lock:
            if self.task.running:
                return False
            self._cancel.clear()
            self._set_task(name, "准备中…", 0.0, cancellable=cancellable)

        def worker() -> None:
            err = ""
            res = None
            try:
                res = fn()
            except Exception as exc:
                err = f"{exc}\n{traceback.format_exc()}"
                self.log(f"任务「{name}」失败：{exc}", "error")
            try:
                self._finish_task(res, err)
                # 任务因用户取消而收尾时，统一补发一个 task_cancelled 事件。
                # 放在这里而不是各个 done 里：省得每加一个任务就要记得写一次，
                # 漏掉的那个就会让界面把"半截结果"当成成功结果刷出来。
                if self.task.cancelled and not err:
                    self.emit("task_cancelled", {"task": self.task.to_dict(),
                                                 "name": name})
                if on_done:
                    on_done(res, err)
            except Exception as exc:
                self.log(f"任务收尾失败：{exc}", "error")

        threading.Thread(target=worker, name=f"smd-{name}", daemon=True).start()
        return True

    def cancel(self) -> None:
        """请求取消当前后台任务。

        只置位取消标志、不强杀线程：扫描、求解、AI 请求等耗时步骤会在自己的循环里
        查询 :attr:`cancelled` 后主动退出，正在进行的写盘不会被中断。没有任务在跑时
        调用也无害，附带记一行 ``warn`` 日志。

        置位后会**立刻推一次任务快照**：耗时的步骤可能要过一会儿才轮到它检查标志，
        界面得先有反应（按钮变成"正在停止…"），否则用户会以为点了没用而反复点。
        """
        self._cancel.set()
        if self.task.running:
            self.task.message = "正在停止…（等待当前步骤收尾）"
            self.task.stage = self.task.message
            self.emit("task", self.task.to_dict())
        self.log("已请求取消当前任务", "warn")

    def cancel_task(self) -> dict:
        """界面「停止」按钮的入口。返回当前任务状态供界面立即刷新。"""
        running = self.task.running
        if running:
            self.cancel()
        return {"ok": True, "was_running": running,
                "task": self.task.to_dict()}

    @property
    def cancelled(self) -> bool:
        """当前任务是否已收到取消请求（只读）。

        供后台任务的 ``should_stop`` 回调轮询；新任务入队时会自动清掉该标志。
        """
        return self._cancel.is_set()

    # ------------------------------------------------------------------
    # 状态查询
    # ------------------------------------------------------------------
    def status(self) -> dict:
        """汇总一份整体运行状态，供界面刷新用。

        包含版本信息、各路径与路径问题、关键配置项、模组库统计、已建索引数、
        冲突报告、当前方案、任务进度、监控状态、游戏版本、开机提醒与运行时长。
        只读，返回的全部是可直接 JSON 序列化的普通类型。
        """
        paths = self.settings.paths
        s = self.library.summary() if self.library else {}
        conflicts = self.report.to_dict() if self.report else None
        return {
            "app": {"name": APP_NAME, "name_en": APP_NAME_EN,
                    "version": __version__},
            "paths": {
                "user_dir": paths.user_dir,
                "game_dir": paths.game_dir,
                "workshop_dirs": paths.workshop_dirs,
                "mod_dir": str(paths.mod_dir),
                "logs_dir": str(paths.logs_dir),
                "crashes_dir": str(paths.crashes_dir),
                "report_dir": str(report_output_dir(paths)) if paths.user_dir else "",
                "launcher_db": str(paths.launcher_db),
            },
            "path_problems": paths.validate(),
            "settings": {
                "shutdown_enabled": self.settings.shutdown_enabled,
                "shutdown_delay_seconds": self.settings.shutdown_delay_seconds,
                "boot_reminder_enabled": self.settings.boot_reminder_enabled,
                "monitor_poll_seconds": self.settings.monitor_poll_seconds,
                "monitor_memory_warn_mb": self.settings.monitor_memory_warn_mb,
                "auto_apply_optimal_order": self.settings.auto_apply_optimal_order,
                "keep_translation_on_top": self.settings.keep_translation_on_top,
            },
            "library": s,
            "library_loaded": self.library is not None,
            "indexed": len(self.indexes.indexes),
            "conflicts": conflicts,
            "solution": self.solution.to_dict() if self.solution else None,
            "task": self.task.to_dict(),
            "monitor": self.monitor.status() if self.monitor else {
                "running": False, "game_running": bool(find_game_pids())},
            "game_version": list(game_version_from_meta(paths))
            if self.library else [0, 0, 0],
            "boot_reminder_registered": boot_reminder_registered(),
            "pending_boot_reminder": (load_reminder().to_dict()
                                      if load_reminder() else None),
            "uptime": round(time.time() - self._started_at, 0),
        }

    def get_settings(self) -> dict:
        """读取当前配置的完整快照。

        借一次 JSON 往返做深拷贝，遇到非 JSON 类型统一用 ``str`` 兜底，所以返回的
        ``dict`` 已经和内部配置对象脱钩，改它不会影响实际设置。
        """
        return json.loads(json.dumps(self.settings.__dict__, default=str))

    def save_settings(self, patch: dict) -> dict:
        """更新配置。"""
        allowed = {
            "shutdown_enabled", "shutdown_delay_seconds", "shutdown_after_report",
            "boot_reminder_enabled", "monitor_poll_seconds",
            "monitor_memory_warn_mb", "monitor_workshop_stale_check",
            "auto_apply_optimal_order", "keep_translation_on_top",
            "language", "theme",
        }
        for k, v in (patch or {}).items():
            if k in allowed:
                setattr(self.settings, k, v)
        if "paths" in (patch or {}):
            p = patch["paths"]
            for k in ("user_dir", "game_dir"):
                if k in p:
                    setattr(self.settings.paths, k, str(p[k]))
            if "workshop_dirs" in p:
                self.settings.paths.workshop_dirs = [str(x) for x in p["workshop_dirs"]]
        self.settings.save()
        self.log("配置已保存")
        return self.get_settings()

    def autodetect_paths(self) -> dict:
        """自动探测游戏安装目录与用户数据目录。

        **有副作用**：把探测到的路径写回配置对象并保存到磁盘（会覆盖旧值），
        同时记一行日志。返回 ``{"fixed": 补上的字段名, "paths": 路径表,
        "problems": 仍然存在的问题}``。
        """
        fixed = self.settings.autodetect(missing_only=False)
        self.settings.save()
        self.log(f"路径自动探测完成：{', '.join(fixed) or '无变化'}")
        return {"fixed": fixed, "paths": self.status()["paths"],
                "problems": self.settings.paths.validate()}

    # ------------------------------------------------------------------
    # 模组清单
    # ------------------------------------------------------------------
    def consolidate_old_patch_mods(self) -> dict:
        """把历史遗留的"一个补丁一个模组"合并进统一补丁模组。

        早期版本每保存一次就新建一个 ``zzz_SMD_*`` 模组，跑几次就攒出
        几十个，启动器列表被刷屏。现在统一成一个，但**旧的那些还在磁盘上**，
        不处理的话用户照样看着一堆。

        做法：把旧目录里的文件**搬进**统一模组（同名以旧文件为准会覆盖新补丁，
        所以反过来——以统一模组里已有的为准，缺什么补什么），
        然后删掉旧目录与它的 ``.mod``。

        只碰 ``zzz_SMD_`` 前缀，**绝不动用户自己的模组**。
        """
        from . import PATCH_PREFIX
        from .patch.service import UNIFIED_NAME, patch_root, unified_patch_dir

        root = patch_root(self.settings.paths)
        if not is_dir(root):
            return {"ok": True, "merged": 0, "reason": "没有 mod 目录"}
        unified = root / UNIFIED_NAME
        olds = [d for d in root.iterdir()
                if d.is_dir() and d.name.startswith(PATCH_PREFIX)
                and d.name != UNIFIED_NAME]
        if not olds:
            return {"ok": True, "merged": 0}

        moved = 0
        skipped = 0
        merged_names: list[str] = []
        for old in olds:
            for dirpath, _dirs, fnames in os.walk(long_path(old)):
                for fn in fnames:
                    if fn in ("descriptor.mod",) or fn.startswith("README"):
                        continue
                    src = Path(dirpath) / fn
                    rel = os.path.relpath(str(src), str(old))
                    dst = unified / rel
                    try:
                        if is_file(dst):
                            skipped += 1      # 统一模组里已有：保留新的
                            continue
                        dst.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(long_path(src), long_path(dst))
                        moved += 1
                    except OSError:
                        skipped += 1
            # 目录搬空了就删掉它和它的 .mod
            try:
                shutil.rmtree(long_path(old), ignore_errors=True)
                mf = root / f"{old.name}.mod"
                if is_file(mf):
                    os.remove(long_path(mf))
                merged_names.append(old.name)
            except OSError:
                pass

        if merged_names:
            # 统一模组自己的描述符要补回来：
            # 上面删 .mod 时只按旧目录名删，但万一统一模组原本没有 .mod
            # （例如它是在旧版本里建出来的），这里不补就会导致
            # "文件都在、启动器里却不出现"。
            try:
                from .patch.service import _write_descriptors
                _write_descriptors(self.settings.paths, unified_patch_dir(
                    self.settings.paths))
            except Exception:
                pass
            self.log(f"已把 {len(merged_names)} 个旧补丁模组并入统一补丁"
                     f"（搬运 {moved} 个文件，跳过 {skipped} 个重复）")
            try:
                self.library = load_library(self.settings.paths)
            except Exception:
                pass
        return {"ok": True, "merged": len(merged_names), "moved": moved,
                "skipped": skipped, "names": merged_names}

    def load_mods(self) -> dict:
        """（同步）重新读取模组清单。"""
        # **不要**在这里清空 _checksum_report。
        #
        # 原来这里有一句 `self._checksum_report = None`，后果是：
        # 用户扫完成就影响，一切到「模组列表」（会调 load_mods），
        # 结果就没了 —— 功能看起来像坏的。
        # 实测抓到的调用栈：load_mods:410 <- loadMods <- wrapper <- _call。
        #
        # 报告的有效性交给 achievement_risk_rows() 自己校验
        # （比对 game_dir，并且只取当前可见模组），
        # 不需要在这里一刀切。真正要作废时是「重新扫描」，
        # 那个在扫描任务的 job 里显式清。
        paths = self.settings.paths
        self.library = load_library(paths)
        s = self.library.summary()
        self.log(f"模组清单：共 {s['total']} 个，启用 {s['enabled']} 个")
        return {
            "summary": s,
            "mods": self.mods_list(),
            "launcher": {
                "playset": self.library.launcher_info.get("playset", ""),
                "available": self.library.launcher_info.get("available", False),
                "dlc_load_count": len(self.library.dlc_load.get("enabled_mods", [])),
            },
        }

    def visible_mods(self) -> list:
        """当前应当在界面里显示的模组。

        默认**排除「幽灵记录」**——那些已退订、但 Paradox 启动器数据库
        还留着的空条目。它们不是用户的模组，既不能加载也不能启用，
        列在列表里只会让人困惑（还会让人以为"有的模组无法启用"）。

        想看它们（比如要核对退订情况）可以在设置里打开
        ``show_ghost_mods``，那时它们会显示但开关仍然是灰的。
        """
        if self.library is None:
            return []
        mods = self.library.all()
        if getattr(self.settings, "show_ghost_mods", False):
            return mods
        return [m for m in mods if not m.is_ghost()]

    def mods_list(self) -> list[dict]:
        """列出模组清单，供界面表格渲染。

        每个模组一个 ``dict``：键名、显示名、版本与支持版本、标签、是否在 playset 里
        启用及位置、来源、内容路径是否可用、问题列表、翻译 / 修复 / UI / 前置库标记，
        以及已建索引的文件数、定义数、体积与内容标签。尚未读取模组库时返回空列表。

        默认不含幽灵记录，见 :meth:`visible_mods`。
        """
        out: list[dict] = []
        for m in self.visible_mods():
            idx = self.indexes.indexes.get(m.key)
            out.append({
                "key": m.key,
                "name": m.name,
                "dir_name": m.dir_name,
                "version": m.version,
                "supported_version": m.supported_version,
                "tags": m.tags,
                "enabled": m.enabled_in_playset,
                "position": m.playset_position,
                "source": m.source,
                "status": m.status,
                "remote_file_id": m.remote_file_id,
                # Steam 创意工坊详情页，界面点击模组名即可跳转
                "workshop_url": m.workshop_url(),
                "path": m.path,
                "path_ok": bool(m.resolve_content_path()),
                # 磁盘上有没有可加载的内容。False 表示勾了也没用，
                # 界面要把开关禁掉并说明原因，而不是给个点了没反应的开关。
                "loadable": m.is_loadable(),
                "ghost": m.is_ghost(),
                "content_dir": m.content_dir(),
                "problems": m.problems,
                "is_translation": m.is_translation,
                "is_fix": m.is_fix,
                "is_ui": m.is_ui,
                "is_base": m.is_library_or_framework,
                "file_count": idx.file_count if idx else 0,
                "def_count": sum(len(v) for v in idx.definitions.values()) if idx else 0,
                "size": idx.total_bytes if idx else m.size_hint,
                "size_human": human_size(idx.total_bytes) if idx else "—",
                "concepts": _mod_concepts(idx),
                "errors": idx.errors[:10] if idx else [],
            })
        return out

    def mod_detail(self, key: str) -> dict:
        """单个模组的详细信息。"""
        if not self.library:
            return {}
        m = self.library.get(key)
        if m is None:
            return {}
        idx = self.indexes.indexes.get(key)
        rel: list[dict] = []
        if self.report:
            for f in self.report.findings:
                if key in f.mods:
                    rel.append({"kind": f.kind, "severity": f.severity,
                                "severity_label": SEV_LABEL.get(f.severity, ""),
                                "title": f.title, "advice": f.advice})
        return {
            "mod": next((x for x in self.mods_list() if x["key"] == key), {}),
            "concepts": sorted((idx.category_counts().items()
                                if idx else []), key=lambda kv: -kv[1])[:40],
            "sample_definitions": (sorted(idx.definitions.keys())[:80]
                                   if idx else []),
            "related_findings": rel[:40],
        }

    # ------------------------------------------------------------------
    # 扫描 + 检测
    # ------------------------------------------------------------------
    def start_scan(self, *, semantic: bool = True) -> bool:
        """启动「扫描 → 检测」后台任务。"""
        paths = self.settings.paths
        problems = paths.validate()
        if any("用户数据目录" in p or "mod 目录" in p for p in problems):
            self.log("路径不完整，无法扫描：" + "；".join(problems), "error")
            return False

        def job() -> dict:
            t0 = time.time()
            self._task_progress("读取模组清单", 0.02)
            lib = load_library(paths)
            self.library = lib
            # 扫描范围 = **当前播放集里启用的模组**。
            #
            # 以前是 `lib.mods.values()` —— 「有文件的都扫」，完全不看
            # 播放集。于是用户明明只装了 20 个想玩，工具却把他订阅的
            # 九十多个一起扫、一起排、一起报冲突，排序结果还把
            # 他精心挑的那套冲掉了。实测反馈："一扫描直接就是我订阅的
            # mod 全部一起弄了"。
            #
            # 用户关心的是"这次要玩的这套"，不是"我库里有的一切"。
            # 想扫全部可以在设置里打开 scan_all_mods。
            if getattr(self.settings, "scan_all_mods", False):
                mods = [m for m in lib.mods.values() if m.resolve_content_path()]
                scope_label = "全部"
            else:
                mods = [m for m in lib.enabled() if m.resolve_content_path()]
                scope_label = "当前播放集"
            if not mods:
                # 读不到播放集启用信息（启动器数据库缺失/结构不同）时
                # 退回全部：宁可多扫，也不要给出一个空结果让用户以为
                # 软件坏了。
                mods = [m for m in lib.mods.values() if m.resolve_content_path()]
                scope_label = "全部（未读到播放集）"
            self.log(f"扫描范围：{scope_label}，{len(mods)} 个模组")
            self._task_progress(f"扫描 {len(mods)} 个模组的文件", 0.08)

            scanner = Scanner(
                progress=lambda msg, p: self._task_progress(
                    msg, 0.08 + p * 0.55),
                should_stop=lambda: self.cancelled,
            )
            idx_map = scanner.scan(mods, semantic=semantic)
            # 先把索引集合在**本地**装好，最后一次性发布。
            #
            # 不能边填边发布：界面的轮询线程会调用 status() / mods_list()，
            # 它们要遍历这个 dict。如果主线程还在往里塞键，Python 会直接抛
            # ``RuntimeError: dictionary changed size during iteration``
            # （曾经真实出现过，界面上只看到一句莫名其妙的失败提示）。
            iset = IndexSet()
            for k, i in idx_map.items():
                iset.add(i)
            self.indexes = iset        # 原子替换：读者要么看到旧的，要么看到完整的新集合

            self._task_progress("检测冲突", 0.65)
            gv = game_version_from_meta(paths)
            det = ConflictDetector(
                lib, iset, game_version=gv, scan_started=t0,
                progress=lambda msg, p: self._task_progress(
                    msg, 0.65 + p * 0.32),
                should_stop=lambda: self.cancelled,
            )
            rep = det.run()
            if self.cancelled:
                # 用户叫停：不要把这份残缺的报告当成结果发布出去，
                # 否则界面会显示一个"检测完成"的假象，数字全是半截的。
                self.log("扫描已被用户停止，未发布结果", "warn")
                return self._cancelled_result("扫描")
            self.report = rep
            self.settings.last_scan_epoch = time.time()
            # 同 start_ai_takeover：只存时间戳，别把内存里的其它改动
            # （尤其是隐私开关）一起持久化。
            self.settings.save_scan_epoch()
            self.log(
                f"检测完成：{rep.count()} 个问题"
                f"（致命 {rep.count('critical')} / 严重 {rep.count('high')}"
                f" / 中等 {rep.count('medium')}），健康分 {rep.health_score()}"
                f"，耗时 {time.time()-t0:.1f}s"
            )
            return rep.to_dict()

        def done(res: Any, err: str) -> None:
            if err:
                self.emit("scan_failed", {"error": err})
            elif isinstance(res, dict) and res.get("cancelled"):
                # 用户叫停：发一个专门的停止事件，界面据此提示
                # "已停止"而不是"检测完成"，也不要刷新那些半截数字。
                self.emit("task_cancelled", {"task": self.task.to_dict()})
            else:
                self.emit("scan_done", {"report": res,
                                        "mods": self.mods_list(),
                                        "summary": self.library.summary()
                                        if self.library else {}})

        return self.run_async("扫描与冲突检测", job, done)

    def findings(self, severity: str | None = None, kind: str | None = None,
                 limit: int = 500, *, show_patched: bool = False) -> list[dict]:
        """按条件筛选冲突检测的明细行。

        ``severity`` 与 ``kind`` 传 ``None`` 或 ``"all"`` 表示不筛选；``limit``
        限制返回条数（默认 500）。还没扫描过时返回空列表。

        **已经被补丁解决掉的冲突默认不再返回**：用户打完补丁后那条还挂着，
        看起来就像"白做了"。``show_patched=True`` 可强制显示（界面上留了入口，
        万一用户想核对补丁到底盖住了什么）。
        """
        if not self.report:
            return []
        cov = None if show_patched else self._patch_coverage()
        out = []
        for f in self.report.findings:
            if severity and severity != "all" and f.severity != severity:
                continue
            if kind and kind != "all" and f.kind != kind:
                continue
            if cov is not None and self.finding_is_patched(f, cov):
                continue
            out.append(f.to_dict())
            if len(out) >= limit:
                break
        return out

    def findings_filter_note(self) -> dict:
        """给界面用：有多少条冲突因"已被补丁解决"而没显示出来。"""
        if not self.report:
            return {"hidden_patched": 0, "total": 0, "patches": 0,
                    "files": 0, "defines": 0, "loc_keys": 0}
        cov = self._patch_coverage()
        hidden = sum(1 for f in self.report.findings
                     if self.finding_is_patched(f, cov))
        return {"hidden_patched": hidden,
                "total": len(self.report.findings),
                "patches": len(self.patch_list()),
                "defines": len(cov["defines"]),
                "loc_keys": len(cov.get("loc_keys") or ()),
                "files": len(cov["files"])}

    def finding_kinds(self) -> list[dict]:
        """列出本次检测出现过的冲突类型及各自数量（供筛选下拉框使用）。

        还没扫描过时返回空列表。
        """
        if not self.report:
            return []
        return self.report.kinds()

    # ------------------------------------------------------------------
    # 求解 + 应用
    # ------------------------------------------------------------------
    def compute_solution(self) -> dict:
        """（同步）求解最优加载顺序与兼容补丁方案。

        需要先跑过扫描；结果写进 ``self.solution`` 并记一行汇总日志。缺少模组库或
        检测报告时返回 ``{"error": ...}``，否则返回方案 ``dict``。本步骤只产出建议，
        **不写盘、不改动任何模组的启用状态**。
        """
        if not self.library or not self.report:
            return {"error": "请先运行扫描与冲突检测"}
        solver = OrderSolver(
            self.library, self.indexes, self.report,
            keep_translation_on_top=self.settings.keep_translation_on_top,
        )
        self.solution = solver.solve()
        d = self.solution.to_dict()
        self.log(
            f"已生成最优方案：调整 {d['change_count']} 项，"
            f"禁用 {len(d['disabled'])} 个，生成 {len(d['patches'])} 份补丁，"
            f"预计健康分 {d['score_before']} → {d['score_after']}"
        )
        return d

    def preview_solution(self) -> dict:
        """查看当前方案：已求解过就返回缓存结果，否则先算一次。

        返回值与 :meth:`compute_solution` 一致。
        """
        if self.solution is None:
            return self.compute_solution()
        return self.solution.to_dict()

    # ---- 用户的「关不关模组」决策 ---------------------------------
    def decide_disable(self, key: str, approved: bool) -> dict:
        """记录用户对某个「建议禁用」模组的决定。

        **这是唯一能改变模组启用状态的入口。** 求解器只给建议，
        用户点「关闭」才会把 ``approved`` 置真，应用时才真正生效。
        """
        if self.solution is None:
            return {"ok": False, "error": "尚未计算方案"}
        hit = self.solution.decide(key, approved)
        if hit:
            m = self.library.get(key) if self.library else None
            name = m.name if m else key
            self.log(f"用户决定{'关闭' if approved else '保留'}模组：{name}")
        return {
            "ok": hit,
            "solution": self.solution.to_dict(),
            "pending": len(self.solution.pending_decisions()),
            "approved": len(self.solution.approved_disable()),
        }

    def decide_all_disable(self, approved: bool) -> dict:
        """一键确认全部建议（全部关闭 或 全部保留）。"""
        if self.solution is None:
            return {"ok": False, "error": "尚未计算方案"}
        n = self.solution.decide_all(approved)
        self.log(f"用户{'同意关闭' if approved else '选择保留'}全部 {n} 个建议模组")
        return {
            "ok": True, "count": n,
            "solution": self.solution.to_dict(),
            "pending": len(self.solution.pending_decisions()),
            "approved": len(self.solution.approved_disable()),
        }

    def reset_decisions(self) -> dict:
        """把所有禁用决策退回「未表态」。"""
        if self.solution is None:
            return {"ok": False, "error": "尚未计算方案"}
        n = self.solution.reset_decisions()
        self.log(f"已重置 {n} 项模组开关决策")
        return {"ok": True, "count": n,
                "solution": self.solution.to_dict(),
                "pending": len(self.solution.pending_decisions()),
                "approved": len(self.solution.approved_disable())}

    def pending_decisions(self) -> dict:
        """当前还在等用户拍板的建议。"""
        if self.solution is None:
            return {"pending": [], "count": 0}
        rows = []
        for p in self.solution.pending_decisions():
            rows.append({
                "key": p.key, "name": p.name, "reason": p.reason,
                "hard_failure": p.hard_failure,
                "old_position": p.old_position,
            })
        return {"pending": rows, "count": len(rows),
                "approved": len(self.solution.approved_disable()),
                "kept": len(self.solution.kept()),
                "total_recommended": len(self.solution.recommended_disable())}

    def start_solve(self) -> bool:
        """启动「计算最优方案」后台任务。

        返回是否成功排队；已有任务在跑时返回 ``False``。完成后通过 ``solve_done``
        事件推送 ``{"solution": ..., "error": ...}``。只生成建议，不会自动改动模组。
        """
        def job() -> dict:
            self._task_progress("求解最优加载顺序", 0.2)
            d = self.compute_solution()
            if "error" in d:
                raise RuntimeError(d["error"])
            self._task_progress("完成", 1.0)
            return d

        def done(res: Any, err: str) -> None:
            self.emit("solve_done", {"solution": res, "error": err})
        return self.run_async("计算最优方案", job, done)

    def apply_current_solution(self, *, write_db: bool = True,
                               write_json: bool = True,
                               create_patches: bool = True,
                               force: bool = False) -> dict:
        """（同步）把当前方案真正落到群星上。

        **有副作用**：先自动备份原文件，再按 ``write_db`` / ``write_json`` 改写启动器的
        playset 数据库与 ``dlc_load.json``，按 ``create_patches`` 新建 ``zzz_SMD_``
        补丁模组，结束后重新读取模组清单。``force`` 为真时跳过「启动器或游戏正在运行」
        的保护性拒绝。**只会关闭用户在 :meth:`decide_disable` 里逐项确认过的模组**，
        求解器给出的其余建议保持启用。返回应用结果 ``dict``（``ok`` / ``actions`` /
        ``errors`` 等）。
        """
        if self.solution is None:
            self.compute_solution()
        if self.solution is None or self.library is None:
            return {"ok": False, "errors": ["没有可应用的方案"]}
        res = apply_solution(
            self.settings.paths, self.library, self.solution,
            write_db=write_db, write_json=write_json,
            create_patches=create_patches, force=force,
            progress=self._task_progress,
        )
        self.last_apply = res
        if res.ok:
            self.log(f"方案已应用：{len(res.actions)} 个动作全部成功")
        else:
            self.log(f"方案应用完成但有 {len(res.errors)} 个问题", "warn")
        # 应用后重新读取清单以反映新顺序
        try:
            self.library = load_library(self.settings.paths)
        except Exception:
            pass
        return res.to_dict()

    def start_apply(self, **kw: Any) -> bool:
        """在后台线程应用当前方案。

        ``**kw`` 原样转交给 :meth:`apply_current_solution`（如 ``write_db``、
        ``create_patches``、``force``）。返回是否成功排队；完成后通过 ``apply_done``
        事件推送结果与刷新后的模组列表。
        """
        def job() -> dict:
            self._task_progress("应用方案", 0.05)
            return self.apply_current_solution(**kw)

        def done(res: Any, err: str) -> None:
            self.emit("apply_done", {"result": res, "error": err,
                                     "mods": self.mods_list()})
        return self.run_async("应用最优方案", job, done)

    def one_click_optimize(self, *, apply: bool = True) -> bool:
        """一键：扫描 → 检测 → 求解 → 应用。"""
        paths = self.settings.paths

        def job() -> dict:
            t0 = time.time()
            self._task_progress("读取模组清单", 0.02)
            lib = load_library(paths)
            self.library = lib
            mods = [m for m in lib.mods.values() if m.resolve_content_path()]

            self._task_progress(f"扫描 {len(mods)} 个模组", 0.06)
            scanner = Scanner(
                progress=lambda m, p: self._task_progress(m, 0.06 + p * 0.5),
                should_stop=lambda: self.cancelled)
            iset = IndexSet()
            for k, i in scanner.scan(mods, semantic=True).items():
                iset.add(i)
            self.indexes = iset

            self._task_progress("检测冲突", 0.6)
            det = ConflictDetector(
                lib, iset, game_version=game_version_from_meta(paths),
                scan_started=t0,
                progress=lambda m, p: self._task_progress(m, 0.6 + p * 0.2),
                should_stop=lambda: self.cancelled)
            rep = det.run()
            self.report = rep

            self._task_progress("求解最优方案", 0.82)
            solver = OrderSolver(
                lib, iset, rep,
                keep_translation_on_top=self.settings.keep_translation_on_top)
            sol = solver.solve()
            self.solution = sol

            out: dict[str, Any] = {"report": rep.to_dict(),
                                   "solution": sol.to_dict(),
                                   "applied": None}
            # 注意：这里**不会**自动确认任何「建议禁用」。
            # 自动应用的范围仅限「加载顺序 + 兼容补丁」这类可逆调整；
            # 关不关模组必须由用户在界面上逐项决定。
            pending = len(sol.pending_decisions())
            if apply and (sol.change_count() or sol.patches or
                          sol.clean_dlc_load):
                self._task_progress("应用排序与补丁", 0.9)
                out["applied"] = self.apply_current_solution()
                if pending:
                    out["applied"]["pending_decisions"] = pending
                    self.log(f"有 {pending} 个模组等待你决定是否关闭"
                             f"（本次未改动它们的启用状态）", "warn")
            elif apply:
                out["applied"] = {"ok": True, "actions": [], "errors": [],
                                  "note": "无需改动，当前配置已是最优"}
            if pending:
                out["pending_decisions"] = pending
            self._task_progress("完成", 1.0)
            self.log(f"一键优化完成，耗时 {time.time()-t0:.1f}s")
            return out

        def done(res: Any, err: str) -> None:
            self.emit("optimize_done", {"result": res, "error": err,
                                        "mods": self.mods_list()})
        return self.run_async("一键最优处理", job, done)

    def rollback(self, backup_dir: str = "") -> dict:
        """从备份恢复顺序与配置文件（**会覆盖当前 playset**）。

        ``backup_dir`` 留空时取最近一次备份；一份备份都没有时返回
        ``{"ok": False, "errors": [...]}``。恢复后重新读取模组清单并记一行日志。
        """
        backups = list_backups()
        if not backup_dir:
            if not backups:
                return {"ok": False, "errors": ["没有可用的备份"]}
            backup_dir = backups[0]["path"]
        res = rollback(backup_dir, self.settings.paths)
        self.log(f"已从备份恢复：{backup_dir}")
        try:
            self.library = load_library(self.settings.paths)
        except Exception:
            pass
        return res.to_dict()

    def list_backups(self) -> list[dict]:
        """列出磁盘上已有的备份（新的排在前面），每项含路径、时间与体积信息。"""
        return list_backups()

    def make_backup(self) -> dict:
        """（同步）立刻打一份手动备份。

        **有副作用**：把当前 playset 与配置文件复制到备份目录，并写一行日志。
        返回 ``{"path": 备份目录}``；底层写盘失败会抛出 ``OSError``，此处不吞掉。
        """
        p = make_backup(self.settings.paths, "manual")
        self.log(f"已创建备份：{p}")
        return {"path": p}

    def remove_patches(self) -> dict:
        """删除本程序生成的补丁模组。

        **有副作用、不可撤销**：直接删掉用户 mod 目录下所有 ``zzz_SMD_`` 前缀的模组
        （AI 生成的与手写的补丁都在此列，第三方模组不受影响），并记一行日志。
        返回 ``{"removed": [被删目录名]}``。
        """
        removed = remove_patch_mods(self.settings.paths)
        self.log(f"已移除 {len(removed)} 个自动生成的补丁模组")
        return {"removed": removed}

    # ------------------------------------------------------------------
    # 监控
    # ------------------------------------------------------------------
    def start_monitor(self) -> dict:
        """启动后台运行时监控（独立线程轮询游戏进程与日志）。

        已经在跑时不重复启动。返回当前监控状态 ``dict``；游戏崩溃等事件通过
        ``monitor_event`` 与 ``crash_detected`` 事件推给界面。
        """
        if self.monitor is None or not self.monitor.running:
            self.monitor = GameMonitor(
                self.settings.paths, self.settings,
                on_event=self._on_monitor_event)
            self.monitor.start()
            self.log("后台运行时监控已启动")
        return self.monitor.status()

    def stop_monitor(self) -> dict:
        """停止后台运行时监控线程。

        返回停止后的状态；从未启动过监控时返回 ``{"running": False}``。
        """
        if self.monitor:
            self.monitor.stop()
            self.log("后台运行时监控已停止")
            return self.monitor.status()
        return {"running": False}

    def monitor_status(self) -> dict:
        """查询运行时监控状态：是否在跑、游戏是否正在运行、内存峰值与错误计数。

        未启动监控时也返回同样结构的空状态（游戏是否运行改由直接查进程判断），
        方便界面统一渲染。
        """
        if self.monitor is None:
            return {"running": False, "game_running": bool(find_game_pids()),
                    "stats": {}, "error_counts": {}, "peak_memory_mb": 0}
        return self.monitor.status()

    def monitor_events(self, limit: int = 200) -> list[dict]:
        """取最近的监控事件。

        最多返回 ``limit`` 条（默认 200），按发生先后排列；未启动监控时返回内存里
        缓存下来的事件。
        """
        if self.monitor is None:
            return list(self._monitor_events[-limit:])
        return [e.to_dict() for e in list(self.monitor.events)[-limit:]]

    def _on_monitor_event(self, ev: MonitorEvent) -> None:
        self._monitor_events.append(ev.to_dict())
        if len(self._monitor_events) > 1000:
            del self._monitor_events[:300]
        self.emit("monitor_event", ev.to_dict())
        if ev.kind == "game_crashed":
            self.emit("crash_detected", ev.to_dict())

    def _on_shutdown_tick(self, left: int) -> None:
        self.emit("shutdown_tick", {"remaining": left})

    def _on_shutdown_fire(self) -> None:
        self.emit("shutdown_fired", {})

    def launch(self, *, use_steam: bool = True, monitor: bool = True) -> dict:
        """启动群星。

        ``use_steam`` 为真走 Steam 协议拉起（否则直接运行游戏可执行文件）；
        ``monitor`` 为真会先开启后台运行时监控，再启动游戏。返回
        ``{"ok": bool, "message": 说明文本}``，并按结果记一行成功 / 失败日志。
        """
        if monitor:
            self.start_monitor()
        ok, msg = launch_game(self.settings.paths, use_steam=use_steam)
        self.log(msg, "info" if ok else "error")
        return {"ok": ok, "message": msg}

    # ------------------------------------------------------------------
    # AI 接管
    # ------------------------------------------------------------------
    def ai_takeover_plan(self) -> dict:
        """当前接管计划的快照（供界面显示与确认）。"""
        return getattr(self, "_takeover", None) or {
            "ok": False, "error": "还没有生成接管方案"}

    def _takeover_report(self, extra: str = "") -> dict:
        """接管完成后给用户的结论：做了什么、能不能开游戏。"""
        from .modinfo import read_dlc_load
        plan = self.ai_takeover_plan()
        bits: list[str] = []
        bits.append(f"扫描出 {self.report.count() if self.report else 0} 处问题")
        if plan.get("ok"):
            bits.append(f"AI 判定 {len(plan.get('disable') or [])} 个模组建议关闭、"
                        f"{len(plan.get('reorder') or [])} 项顺序调整")
        # 「保证能启动」的判据：文件可读 + 有模组被启用 + 没有致命缺失
        ok_launch = True
        try:
            dl = read_dlc_load(self.settings.paths)
            enabled = list(dl.get("enabled_mods") or [])
            bits.append(f"写回后启用模组 {len(enabled)} 个")
            if not enabled:
                ok_launch = False
                bits.append("！一个模组都没启用，这不正常")
        except Exception as exc:                    # noqa: BLE001
            ok_launch = False
            bits.append(f"！dlc_load.json 读取失败：{exc}")
        if self.report is not None:
            crit = [f for f in self.report.findings
                    if f.severity == "critical"]
            live = [f for f in crit
                    if not self.finding_is_patched(f)]
            bits.append(f"剩余致命冲突 {len(live)} 条")
        if extra:
            bits.append(extra)
        return {"ok": True, "launch_ok": ok_launch, "lines": bits,
                "plan": plan}

    # ------------------------------------------------------------------
    # AI 接管 · 全面排查
    #
    # 这一组的职责是**凑齐喂给 AI 的原料**。之所以把每个数据源拆成独立
    # 方法（而不是堆在一个大函数里）：
    #
    # * 每个源都能单独测。取崩溃和取 DLC 是八竿子打不着的逻辑，
    #   混在一起时想验证其中一个必须把整条链路跑通（要 20 多秒的索引）。
    # * 主函数只剩"按顺序凑哪些源"，一眼能看清全面排查到底纳入了什么。
    #   排查漏了某一类数据是很容易发生的事，那样 AI 的结论会有盲区
    #   却不报错。
    #
    # 共同约定：每个方法往 ``bits`` 里追加若干行文本，并往 ``stats`` 里
    # 写它的统计数字（界面要显示出来，让用户知道 AI 考虑了多少东西）。
    # ------------------------------------------------------------------
    def _audit_collect(self, mods: list, rep: Any, sol: Any,
                       gv: tuple) -> tuple[str, dict]:
        """把软件里**所有**检测能力的结果汇总成一段给 AI 的文本。

        为什么要单独做这一步：AI 接管原来只喂了「冲突 + 排序」两类数据，
        于是它给出的结论也只会围绕冲突。但用户真正的诉求是
        「别让我再崩了」—— 而崩溃原因常常不在冲突列表里，而在
        上次的崩溃栈、error.log 的报错、内存峰值这些地方。

        这里把十类互相独立的数据凑齐，让 AI 有做**交叉关联**的原料。
        单看任何一类都只能得到表层结论；真正的病因在交叉点上
        （比如：崩溃栈里的模组，是否也出现在冲突名单与成就影响名单里）。

        返回 ``(喂给 AI 的文本, 统计信息)``。
        """
        bits: list[str] = []
        stats: dict[str, Any] = {}

        def section(title: str) -> None:
            """开一个新小节。空行 + 分隔线让 AI 能分清数据边界。"""
            bits.append("")
            bits.append("=" * 60)
            bits.append(title)

        # ① 基础盘
        section("【① 基础信息】")
        self._audit_basics(bits, stats, mods, gv)
        # ② 冲突检测（本地算的一手数据，最可靠）
        section("【② 冲突检测】")
        self._audit_conflicts(bits, stats, rep)
        # ③ 崩溃记录 —— 原来完全缺失的一环
        section("【③ 最近的崩溃】")
        self._audit_crashes(bits, stats)
        # ④ 游戏日志
        section("【④ 游戏日志 error.log】")
        self._audit_logs(bits, stats)
        # ⑤ 成就影响
        section("【⑤ 影响成就的模组】")
        self._audit_achievements(bits, stats)
        # ⑥ 联机校验影响
        section("【⑥ 联机校验影响】")
        self._audit_checksum(bits, stats)
        # ⑦ 运行时监控
        section("【⑦ 运行时监控】")
        self._audit_monitor(bits, stats)
        # ⑧ 已有补丁与未解决项
        section("【⑧ 已有补丁与待处理项】")
        self._audit_patches(bits, stats, rep)
        # ⑨ DLC 状态
        section("【⑨ DLC 启用状态】")
        self._audit_dlc(bits, stats)
        # ⑩ 加载顺序
        section("【⑩ 当前加载顺序（前 60）】")
        self._audit_order(bits, stats)
        # ⑪ 求解器建议（没有建议时整节省略）
        self._audit_solver(bits, stats, sol)
        # ⑫ 知识库检索：给 AI 补上《群星》领域知识
        #
        # 这一节和前面十节性质不同 —— 前面都是"用户的机器上发生了什么"，
        # 这一节是"游戏本身的规则是什么"。AI 的通用训练知识未必准
        # （版本差异、冷门机制），用内置的八千多条资料做检索补充，
        # 能让建议落在真实的游戏机制上，而不是泛泛而谈。
        section("【⑫ 相关知识库条目】")
        self._audit_wiki(bits, stats, rep)

        return "\n".join(bits), stats

    # -- 各数据源的取数 ------------------------------------------------
    def _audit_guard(self, bits: list, label: str, fn: Any,
                     default: Any = None) -> Any:
        """跑一个取数动作；失败只记一行，不影响其它数据源。

        任何一项取不到（从没跑过监控、没有崩溃记录、日志被删了）
        都不该让整个排查失败 —— 少一类数据只是结论弱一点，
        抛出去是整个功能不可用。
        """
        try:
            return fn()
        except Exception as exc:                  # noqa: BLE001
            bits.append(f"（{label} 读取失败：{type(exc).__name__}）")
            return default

    def _audit_basics(self, bits: list, stats: dict, mods: list,
                      gv: tuple) -> None:
        """游戏版本、模组数、索引规模 —— 给 AI 一个尺度感。"""
        sumr = self.library.summary() if self.library else {}
        stats["mods_total"] = sumr.get("total")
        stats["mods_enabled"] = sumr.get("enabled")
        bits.append(f"游戏版本：{'.'.join(str(x) for x in gv)}")
        bits.append(f"模组：共 {sumr.get('total')} 个，"
                    f"启用 {sumr.get('enabled')} 个")
        bits.append(f"已索引文件数：{getattr(self, '_takeover_files', 0)}")

    def _audit_conflicts(self, bits: list, stats: dict, rep: Any) -> None:
        """冲突检测结果。致命/严重的最多列 40 条。"""
        try:
            d = rep.to_dict()
            crit = [f for f in rep.findings
                    if f.severity in ("critical", "high")]
            stats["conflicts"] = len(rep.findings)
            stats["critical"] = len(crit)
            bits.append(f"问题统计：{d.get('stats') or {}}")
            bits.append(f"致命/严重共 {len(crit)} 条，列出前 40 条：")
            for f in crit[:40]:
                bits.append(f"- [{f.kind}/{f.severity}] {f.title}"
                            + (f"｜生效方：{f.winner}" if f.winner else "")
                            + (f"｜涉及：{'、'.join(f.mod_names[:3])}"
                               if f.mod_names else ""))
        except Exception as exc:                  # noqa: BLE001
            bits.append(f"（冲突数据读取失败：{type(exc).__name__}）")

    def _audit_crashes(self, bits: list, stats: dict) -> None:
        """最近一次崩溃的诊断信息。

        这是原来完全缺失的一环 —— 软件谈"保证能正常启动"，
        却不知道上次是怎么崩的。
        """
        crashes = self._audit_guard(bits, "崩溃目录列表",
                                    self.crash_dirs, []) or []
        stats["crashes"] = len(crashes)
        if not crashes:
            bits.append("没有崩溃记录（用户可能没崩过，或没留存转储）。")
            return
        bits.append(f"共 {len(crashes)} 次崩溃记录，最近一次："
                    f"{crashes[0].get('time_str') or crashes[0].get('name')}")
        payload = self._audit_guard(bits, "崩溃栈",
                                    lambda: self.crash_payload(""), {}) or {}
        txt = (payload.get("text") or "").strip()
        if txt:
            # 崩溃栈的关键信息集中在前部（异常码 + 当时加载的模组）
            bits.append("最近一次崩溃的诊断信息（截取）：")
            bits.append(txt[:6000])
        else:
            bits.append("（最近一次崩溃没有可用的栈信息）")

    def _audit_logs(self, bits: list, stats: dict) -> None:
        """error.log 的归类结果。

        每条带上本地规则判断的原因与报错最多的来源文件 ——
        「哪个脚本文件报错 70 次」这种线索比总量有用得多。
        """
        an = self._audit_guard(bits, "日志分析",
                               lambda: self.analyze_log_file("error.log"),
                               {}) or {}
        if an.get("error"):
            bits.append(f"读不到日志：{an['error']}")
            return
        sev = an.get("severity_counts") or {}
        stats["log_fatal"] = sev.get("fatal", 0)
        stats["log_error"] = sev.get("error", 0)
        bits.append(f"总行数 {an.get('total_lines', 0)}，分级统计：{sev}")
        groups = an.get("groups") or []
        bits.append(f"归类出 {len(groups)} 类问题，列出最严重的前 25 类：")
        for g in groups[:25]:
            bits.append(f"- [{g.get('severity_label') or g.get('severity')}]"
                        f" {g.get('title')}（{g.get('count')} 次）")
            if g.get("cause"):
                bits.append(f"    本地规则判断的原因：{str(g['cause'])[:160]}")
            src = g.get("sources") or {}
            if isinstance(src, dict) and src:
                top = sorted(src.items(), key=lambda kv: -kv[1])[:3]
                bits.append("    报错最多的来源："
                            + "、".join(f"{k}×{v}" for k, v in top))
        # top_files 装的是「报错最多的游戏源文件」以及各自条数，
        # 元素是 (名称, 次数) 元组。早先误以为 top_mods 是模组名列表，
        # 按 dict 取 .get("name") 会直接抛 AttributeError。
        tm = an.get("top_files") or []
        if tm:
            pairs = []
            for x in tm[:10]:
                if isinstance(x, (tuple, list)) and len(x) >= 2:
                    pairs.append(f"{x[0]}×{x[1]}")
                else:
                    pairs.append(str(x))
            bits.append("报错最多的游戏源文件：" + "、".join(pairs))

    def _audit_achievements(self, bits: list, stats: dict) -> None:
        """会影响成就的模组（启用中的才要紧）。"""
        ach = self._audit_guard(bits, "成就影响",
                                self.achievement_risk_rows, []) or []
        on = [r for r in ach if r.get("enabled")]
        stats["achievement_risk"] = len(on)
        if not ach:
            bits.append("（尚未扫描过成就影响，或没有命中项）")
            return
        bits.append(f"启用中的命中项 {len(on)} 个（共检出 {len(ach)} 个）：")
        for r in on[:20]:
            bits.append(f"- {r['name']}｜命中 {r['hits']} 项")

    def _audit_checksum(self, bits: list, stats: dict) -> None:
        """会改变联机校验结果的模组。"""
        cs = self._audit_guard(bits, "校验报告",
                               self._checksum_risks_for_audit, []) or []
        stats["checksum_risk"] = len(cs)
        if not cs:
            bits.append("（尚未做过联机校验排查）")
            return
        bits.append(f"会改变校验结果的文件涉及的模组 {len(cs)} 个："
                    + "、".join(x[:36] for x in cs[:20]))

    def _audit_monitor(self, bits: list, stats: dict) -> None:
        """运行时监控：内存峰值与运行期错误计数。

        内存逼近上限 + 大量大型模组同时启用，是"玩着玩着就崩"的
        常见组合，所以峰值是有意义的信号。
        """
        st = self._audit_guard(bits, "监控状态", self.monitor_status, {}) or {}
        peak = st.get("peak_memory_mb") or 0
        stats["peak_memory_mb"] = peak
        bits.append(f"监控在跑：{st.get('running')}；游戏正在运行："
                    f"{st.get('game_running')}")
        bits.append(f"内存峰值：{peak} MB")
        ec = st.get("error_counts") or {}
        if ec:
            bits.append(f"运行期错误计数：{dict(list(ec.items())[:12])}")
        evs = self._audit_guard(bits, "监控事件",
                                lambda: self.monitor_events(80), []) or []
        if evs:
            bits.append(f"最近 {len(evs)} 条运行事件（新→旧）：")
            for e in evs[:25]:
                if isinstance(e, dict):
                    bits.append(f"- [{e.get('level') or e.get('kind')}]"
                                f" {str(e.get('text') or e.get('message') or e)[:140]}")

    def _audit_patches(self, bits: list, stats: dict, rep: Any) -> None:
        """已有补丁 + 尚未被补丁解决的致命冲突。

        后者才是真正还要处理的 —— 把已经被补丁覆盖的冲突也报给 AI，
        会让它建议去修一个已经修好的问题。
        """
        try:
            led = self.patch_ledger()
            saved = [e for e in led.all_entries()
                     if getattr(e, "saved", False)]
            pend = list(led.pending())
            stats["patches_saved"] = len(saved)
            stats["patches_pending"] = len(pend)
            bits.append(f"已保存补丁 {len(saved)} 个，"
                        f"待保存草稿 {len(pend)} 个")
            for e in saved[:15]:
                bits.append(f"- 已解决：{str(getattr(e, 'title', '') or '')[:70]}")
            for e in pend[:10]:
                bits.append(f"- 待保存：{str(getattr(e, 'title', '') or '')[:70]}")
        except Exception as exc:                  # noqa: BLE001
            bits.append(f"（补丁账本读取失败：{type(exc).__name__}）")
        live = self._audit_guard(bits, "未解决项统计",
                                 lambda: self._unresolved_critical(rep),
                                 []) or []
        stats["unresolved_critical"] = len(live)
        if live:
            bits.append(f"**尚未被补丁解决的致命冲突 {len(live)} 条**：")
            for f in live[:20]:
                bits.append(f"- {f.title}"
                            + (f"｜涉及：{'、'.join(f.mod_names[:2])}"
                               if f.mod_names else ""))

    def _audit_dlc(self, bits: list, stats: dict) -> None:
        """DLC 启用状态。

        缺 DLC 会让依赖它的模组在启动时直接报错，是"起不来"的常见原因，
        而这一点从冲突列表里完全看不出来。
        """
        dl = self._audit_guard(bits, "DLC 状态", self.dlc_state, {}) or {}
        stats["dlc_disabled"] = dl.get("disabled")
        bits.append(f"共 {dl.get('total')} 个，启用 {dl.get('enabled')} 个，"
                    f"关闭 {dl.get('disabled')} 个")
        off = [d for d in (dl.get("dlcs") or []) if not d.get("enabled")]
        if off:
            bits.append("关闭的 DLC（依赖它们的模组可能启动失败）："
                        + "、".join(str(d.get("name"))[:24]
                                    for d in off[:20]))

    def _audit_order(self, bits: list, stats: dict) -> None:
        """当前加载顺序前 60 项。

        「谁排在崩溃相关模组前面」是判断覆盖方的关键，所以顺序本身
        就是证据，不只是背景信息。
        """
        order_mods = self.library.load_order() if self.library else []
        stats["order_count"] = len(order_mods)
        bits.extend(f"{i + 1}. {m.name}"
                    for i, m in enumerate(order_mods[:60]))

    def _audit_solver(self, bits: list, stats: dict, sol: Any) -> None:
        """本地求解器的关闭建议。没有建议时整节省略，不留空标题。"""
        rec = self._audit_guard(bits, "求解器建议",
                                lambda: list(getattr(
                                    sol, "recommended_disable",
                                    lambda: [])()), []) or []
        stats["solver_rec"] = len(rec)
        if not rec:
            return
        bits.append("")
        bits.append("=" * 60)
        bits.append("【⑪ 本地求解器建议关闭的模组】")
        bits.extend(f"- {p.name}｜{p.reason}" for p in rec[:30])

    def _audit_wiki(self, bits: list, stats: dict, rep: Any) -> None:
        """从内置知识库检索与本次问题相关的条目，补给 AI 当领域知识。

        这一节和前面十一节性质不同：前面都是"用户机器上发生了什么"，
        这一节是"游戏规则本身是什么"。AI 的通用训练知识在群星上未必准 ——
        版本迭代快，冷门机制（巨构、飞升天赋、考古遗址）的细节容易记错。
        喂真实条目比让它自由发挥可靠。

        检索词必须用**中文**：知识库是中文人工整理的表格，
        拿英文脚本对象名（`scripted_effect`）去搜命中率为 0 —— 实测确认；
        而 `巨构` 能搜到 20 条。所以在英文机制名与中文关键词之间做映射，
        从冲突的对象名与文件路径里抽线索。

        取不到就如实说明，不硬凑。
        """
        from . import wiki as W

        # 英文机制名 → 中文关键词（知识库按中文整理，必须过这层映射）
        mech = {
            "megastructure": "巨构", "megastructures": "巨构",
            "ascension_perk": "飞升天赋", "ascension_perks": "飞升天赋",
            "tradition": "传统", "archaeological": "考古遗址",
            "technology": "科技", "technologies": "科技",
            "building": "建筑", "buildings": "建筑",
            "ship": "舰船", "ships": "舰船",
            "fleet": "舰队", "leader": "领袖", "leaders": "领袖",
            "planet": "星球", "planets": "星球",
            "empire": "帝国", "event": "事件", "events": "事件",
            "anomaly": "异常", "edict": "法令", "edicts": "法令",
            "policy": "政策", "crisis": "危机", "relic": "遗珍",
            "component": "部件", "starbase": "星港",
            "district": "区划", "job": "岗位", "jobs": "岗位",
            "civic": "国策", "civics": "国策",
            "origin": "起源", "origins": "起源",
            "pop": "人口", "pops": "人口",
        }
        terms: list[str] = []

        def add(w: str) -> None:
            """加入检索词，最多 6 个（检索太宽反而没指向性）。"""
            if w and w not in terms and len(terms) < 6:
                terms.append(w)

        try:
            for f in (rep.findings if rep else [])[:300]:
                if f.severity not in ("critical", "high"):
                    continue
                ev = getattr(f, "evidence", None)
                blob = " ".join(str(x) for x in (
                    getattr(f, "target", "") or "",
                    getattr(f, "title", "") or "",
                    (ev or {}).get("file", "") if isinstance(ev, dict) else "",
                )).lower()
                for en, zh in mech.items():
                    if en in blob:
                        add(zh)
                if len(terms) >= 4:
                    break
        except Exception:                         # noqa: BLE001
            pass

        # 兜底：给几条通用机制词，保证这一节对 AI 总有内容可参考
        for zh in ("巨构", "飞升天赋", "舰船设计", "事件"):
            if len(terms) >= 4:
                break
            add(zh)

        lines: list[str] = []
        try:
            for term in terms[:4]:
                r = W.search(term, limit=6)
                for row in (r.get("hits") or [])[:6]:
                    cells = [str(c).strip() for c in (row.get("cells") or [])]
                    # 相关性过滤：**检索词必须是某个单元格的连续子串**。
                    #
                    # 为什么必须过滤：wiki.search 对多字词会模糊拆分
                    # （「飞升天赋」能匹配到只含「天赋」的条目），
                    # 作为界面检索没问题（用户看得见、能自己判断），
                    # 但作为喂给 AI 的上下文会**误导** —— 条目与问题无关时，
                    # AI 会以为它相关，比不给更糟。
                    # 同时要求单元格不能比检索词长太多，
                    # 否则"某段长描述里恰好含这两个字"也会通过。
                    if not any(
                            term in c and len(c) <= len(term) * 4
                            for c in cells):
                        continue
                    sec = str(row.get("section_title") or "")[:24]
                    text = " ｜ ".join(c for c in cells if c)[:220]
                    if text:
                        lines.append(f"- 【{term}】{sec}：{text}")
                    if len(lines) >= 12:
                        break
                if len(lines) >= 12:
                    break
        except Exception as exc:                  # noqa: BLE001
            bits.append(f"（知识库检索失败：{type(exc).__name__}）")
            stats["wiki_hits"] = 0
            return

        stats["wiki_hits"] = len(lines)
        if not lines:
            bits.append(f"检索词：{'、'.join(terms[:4])}"
                        "（知识库里没有直接对应的条目）")
            return
        bits.append(f"检索词：{'、'.join(terms[:4])}")
        bits.extend(lines[:12])


    def _checksum_risks_for_audit(self) -> list[str]:
        """给全面排查用的：会改变校验结果的模组名（去重）。"""
        return [r["name"] for r in self._achievement_risks()]

    def _unresolved_critical(self, rep: Any) -> list:
        """致命冲突里，还没被任何已保存补丁覆盖的那些。"""
        if rep is None:
            return []
        try:
            handled = self._ledger_handled_keys("")
        except Exception:                         # noqa: BLE001
            handled = set()
        out = []
        for f in rep.findings:
            if f.severity != "critical":
                continue
            keys = set(getattr(f, "mod_keys", None) or [])
            if keys and keys <= handled:
                continue                          # 涉及方都已被补丁解决
            out.append(f)
        return out

    def start_ai_takeover(self, *, allow_disable: bool = False,
                          allow_reorder: bool = True,
                          apply: bool = False) -> bool:
        """AI 接管：深度解析全部模组 + 原版 + DLC，然后一键处理。

        ``allow_disable`` / ``allow_reorder`` 是**用户开局的选择**：

        * 都不选 → 只诊断，不改任何东西
        * 只选顺序 → 调整加载顺序，绝不关模组
        * 选关模组 → 允许 AI 建议关闭；但**仍然要求逐个确认**，
          这一步只是把"用户同意关闭"的许可传下去

        「关闭」只是改 ``dlc_load.json`` / playset 里的启用状态，
        **不删除磁盘上的模组文件**——文件留在原处，随时能在启动器里
        重新勾回来。真正会删文件的只有 :meth:`remove_auto_patches`，
        而它只动本软件自己生成的 ``zzz_SMD_*`` 补丁。

        最后一定回归到"游戏能正常启动"这个判据上（见 :meth:`_takeover_report`）。
        """
        from .ai import analyze as A

        def job() -> dict:
            # ---- 第 1 步：深度扫描（含原版与 DLC 数据）----
            self._task_progress("深度解析：读取模组清单", 0.02)
            paths = self.settings.paths
            lib = load_library(paths)
            self.library = lib
            mods = [m for m in lib.mods.values() if m.resolve_content_path()]
            self._task_progress(f"深度解析：索引 {len(mods)} 个模组的内容", 0.06)

            scanner = Scanner(
                progress=lambda msg, p: self._task_progress(
                    f"深度解析：{msg}", 0.06 + p * 0.5),
                should_stop=lambda: self.cancelled,
            )
            idx_map = scanner.scan(mods, semantic=True)
            iset = IndexSet()
            for k, i in idx_map.items():
                iset.add(i)
            self.indexes = iset
            if self.cancelled:
                return self._cancelled_result("AI 接管")

            self._task_progress("检测冲突（含原版与 DLC 数据）", 0.56)
            gv = game_version_from_meta(paths)
            det = ConflictDetector(
                lib, iset, game_version=gv, scan_started=time.time(),
                progress=lambda msg, p: self._task_progress(
                    f"检测：{msg}", 0.56 + p * 0.24),
                should_stop=lambda: self.cancelled,
            )
            rep = det.run()
            if self.cancelled:
                return self._cancelled_result("AI 接管")
            self.report = rep
            self.settings.last_scan_epoch = time.time()
            # 只落盘时间戳：用 save() 会把内存里的**所有**改动一起写进去，
            # 包括隐私开关。实测踩过——测试在内存里开隐私开关跑打桩 AI，
            # 这里顺手存了一次，开关就真写进了用户配置。
            self.settings.save_scan_epoch()

            # ---- 第 2 步：本地求解，得到可执行的排序/关闭建议 ----
            self._task_progress("求解最优加载顺序", 0.80)
            solver = OrderSolver(
                lib, iset, rep,
                keep_translation_on_top=self.settings.keep_translation_on_top,
            )
            sol = solver.solve()
            self.solution = sol
            # 顺序变更数 = 新顺序与当前顺序不同的位置数。
            # Solution 上没有单独的 order_changes 字段，只有最终 order 列表。
            old_order = [m.key for m in lib.load_order()]
            new_order = list(getattr(sol, "order", None) or [])
            moved = (sum(1 for a, b in zip(old_order, new_order) if a != b)
                     if new_order and len(new_order) == len(old_order)
                     else 0)

            # ---- 第 3 步：全面排查 —— 汇总所有检测能力的结果 ----
            #
            # 这里原来只喂「冲突 + 排序」两类数据。问题是用户真正的诉求是
            # 「别让我再崩了」，而崩溃原因常常不在冲突列表里，而在上次的
            # 崩溃栈、error.log 的报错、内存峰值里。只喂冲突，AI 就只能
            # 围绕冲突给结论。
            #
            # 现在把八类数据凑齐（见 _audit_collect），让 AI 能做交叉关联。
            self._task_progress("全面排查：汇总各检测模块的结果", 0.84)
            n_files = sum(len(getattr(ix, "files", None) or [])
                          for ix in getattr(iset, "indexes", {}).values())
            self._takeover_files = n_files
            payload, audit = self._audit_collect(
                [m for m in lib.mods.values() if m.resolve_content_path()],
                rep, sol, gv)
            self._takeover_audit = audit
            rec = []
            try:
                rec = list(getattr(sol, "recommended_disable", lambda: [])())
            except Exception:                     # noqa: BLE001
                pass

            self._task_progress("交给 AI 做交叉分析", 0.88)
            res = A.analyze(
                self.settings, kind="AI 接管 · 全面排查",
                payload=payload, should_stop=lambda: self.cancelled,
                source_ref="AI 接管", system=A._AUDIT_SYSTEM)
            if not res.get("ok"):
                return res

            # ---- 第 4 步：把结论落成一份可审阅的计划 ----
            self._task_progress("生成接管方案", 0.95)
            n = self._ai_merge(res, group="AI 接管 · 全面排查")
            disable_rows = []
            if allow_disable:
                # 只把"AI 明确说该关、且本地求解器也建议关"的交集列为可关，
                # 两个独立判断都指向同一个模组时才动它，误关概率低得多
                local_rec = {p.key: p for p in rec}
                for it in (res.get("issues") or []):
                    for mn in (it.get("mods") or []):
                        hit = next((m for m in lib.mods.values()
                                    if m.name == mn), None)
                        if hit is None or hit.key not in local_rec:
                            continue
                        if any(r["key"] == hit.key for r in disable_rows):
                            continue
                        disable_rows.append({
                            "key": hit.key, "name": hit.name,
                            "reason": it.get("cause") or it.get("title") or "",
                            "severity": it.get("severity", "medium"),
                            "local_reason": local_rec[hit.key].reason,
                        })
            plan = {
                "ok": True,
                "allow_disable": allow_disable,
                "allow_reorder": allow_reorder,
                "summary": res.get("summary", ""),
                "issues": len(res.get("issues") or []),
                "added_issues": n,
                "disable": disable_rows,
                "reorder": moved if allow_reorder else 0,
                "reorder_count": moved if allow_reorder else 0,
                "conflicts": rep.count(),
                "critical": rep.count("critical"),
                "indexed_files": n_files,
                # 全面排查覆盖了哪些数据源，界面要显示出来 ——
                # 否则用户没法判断这个结论考虑了多少东西
                "audit": audit,
            }
            self._takeover = plan
            self.log(f"AI 接管方案：{plan['issues']} 条结论、"
                     f"建议关闭 {len(disable_rows)} 个、"
                     f"顺序调整 {plan['reorder_count']} 项"
                     f"（数据源：崩溃 {audit.get('crashes', 0)} 次、"
                     f"日志致命 {audit.get('log_fatal', 0)} 条、"
                     f"成就影响 {audit.get('achievement_risk', 0)} 个）")

            if apply and (disable_rows or plan["reorder_count"]):
                self._task_progress("执行接管", 0.97)
                applied = self._takeover_apply(plan)
                return {**plan, "applied": applied,
                        "report": self._takeover_report()}
            return {**plan, "applied": None,
                    "report": self._takeover_report(
                        "尚未写入。确认无误后点「执行接管」")}

        def done(res: Any, err: str) -> None:
            self.emit("takeover_done", {"result": res, "error": err})
        return self.run_async("AI 接管", job, done)

    def _takeover_apply(self, plan: dict) -> dict:
        """把接管方案写进游戏。

        走的是和「一键最优处理」同一条落地链路（备份 → 写库 → 写
        dlc_load.json），不另起一套写盘逻辑——那是最容易出错的地方。
        """
        from .apply import apply_solution

        sol = self.solution
        if sol is None or self.library is None:
            return {"ok": False, "error": "方案或模组库缺失"}

        # 只把计划里列出的模组标记为"用户同意关闭"。
        # 这一步是**接管功能的同意闸门**：allow_disable 为假时
        # disable 列表本来就是空的，所以这里不会误关任何东西。
        for row in (plan.get("disable") or []):
            try:
                sol.decide(row["key"], True)
            except Exception:
                pass

        if not plan.get("allow_reorder"):
            # 不允许改顺序：把新顺序还原成当前顺序，只保留关闭决定。
            # 直接清空 order 会让 apply 走"按 order 重排"的分支时拿不到列表，
            # 所以还原成原样更稳妥。
            try:
                cur = [m.key for m in self.library.load_order()]
                sol.order = cur
            except Exception:
                pass

        self._task_progress("备份并写入游戏配置", 0.98)
        res = apply_solution(
            self.settings.paths, self.library, sol,
            write_db=True, write_json=True,
            create_patches=bool(plan.get("reorder_count")),
            progress=lambda msg, p: self._task_progress(msg, 0.98),
        )
        d = res.to_dict() if hasattr(res, "to_dict") else {"ok": True}
        self.last_apply = res
        self.log("AI 接管已写入：" + str(d.get("summary") or d.get("message")
                                        or "完成"))
        try:
            self.library = load_library(self.settings.paths)
        except Exception:
            pass
        return d

    def ai_takeover_apply(self) -> bool:
        """按已生成的方案执行接管。"""
        plan = self.ai_takeover_plan()
        if not plan.get("ok"):
            return False

        def job() -> dict:
            applied = self._takeover_apply(plan)
            return {**plan, "applied": applied,
                    "report": self._takeover_report()}

        def done(res: Any, err: str) -> None:
            self.emit("takeover_done", {"result": res, "error": err,
                                        "applied": True})
        return self.run_async("执行 AI 接管", job, done)

    # ------------------------------------------------------------------
    # AI 分析：崩溃 / 日志 / 加载顺序
    # ------------------------------------------------------------------
    def _ai_issues(self) -> dict:
        """AI 分析出来的问题库（内存态，按 key 去重）。

        存内存而不是磁盘：这些是"这一轮排查"的中间产物，
        用户关掉软件就没了才符合直觉；真正要保留的是由它生成的补丁。
        """
        if not hasattr(self, "_ai_store"):
            self._ai_store: dict[str, dict] = {}
            self._ai_group: dict[str, str] = {}     # key -> 分组标题
        return self._ai_store

    def ai_issue_groups(self) -> list[dict]:
        """把 AI 问题按来源分组，供补丁工坊显示。

        **按"来源"成组**（一次崩溃一个组、一个日志一个组），
        用户能一眼看出"这批问题是哪次崩溃分析出来的"，
        而不是混成一大坨。
        """
        store = self._ai_issues()
        groups: dict[str, dict] = {}
        for it in store.values():
            gk = self._ai_group.get(it["key"], it.get("source_ref") or "未分组")
            g = groups.setdefault(gk, {
                "group": gk, "source": it.get("source", ""),
                "ref": it.get("source_ref", ""), "issues": [],
            })
            g["issues"].append(it)
        order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        out = []
        for g in groups.values():
            g["issues"].sort(key=lambda x: order.get(x.get("severity"), 9))
            g["count"] = len(g["issues"])
            g["patchable"] = sum(1 for x in g["issues"] if x.get("ai_patchable"))
            out.append(g)
        out.sort(key=lambda g: -g["count"])
        return out

    def _ai_merge(self, res: dict, *, group: str) -> int:
        """把一次分析的结果并进问题库，返回新增条数。"""
        store = self._ai_issues()
        added = 0
        for it in res.get("issues") or []:
            k = it.get("key") or it.get("title")
            if not k:
                continue
            it["resolved"] = False
            if k not in store:
                added += 1
            store[k] = it
            self._ai_group[k] = group
        return added

    def crash_payload(self, crash_dir: str = "") -> dict:
        """把一次崩溃整理成喂给 AI 的文本。

        只放**真正有诊断价值**的东西：异常栈、出错时的模组清单、
        最近的报错行。把整个 error.log（动辄几十万行）塞进去
        既超长又没用。
        """
        d = crash_dir or self._latest_crash_dir()
        if not d:
            return {"ok": False, "error": "没有找到崩溃目录"}
        exc = read_exception(d)
        meta = parse_meta_yml(Path(d) / "meta.yml") if is_dir(d) else {}
        bits = [f"【崩溃目录】{Path(d).name}"]
        # 键名必须和 read_exception 的返回一致。
        #
        # 原来这里读的是 exc.get('type') 和 exc.get('message')，
        # 而 read_exception 返回的键叫 'exception' 和 'code' —— 对不上，
        # 于是「【异常类型】」「【异常信息】」永远是空标签。
        # 因为 `if exc:` 对非空字典恒为真，这个错还不会报错，只是静默
        # 产出一份没有异常信息的"崩溃诊断"给 AI —— AI 自然分析不出东西。
        if exc:
            code = exc.get("code", "")
            name = exc.get("exception", "")
            if code or name:
                bits.append(f"【异常类型】{name}（异常码 {code}）")
            if exc.get("meaning"):
                bits.append(f"【含义】{exc['meaning']}")
            if exc.get("datetime") or exc.get("version"):
                bits.append(f"【崩溃时间】{exc.get('datetime', '未知')}"
                            f"　【游戏版本】{exc.get('version', '未知')}")
            # frames 现在带帧号，靠前的帧才是崩溃点
            tb = (exc.get("frames") or exc.get("raw") or "")[:6000]
            if tb:
                bits.append(f"【调用栈（序号越小越接近崩溃点）】\n{tb}")
            else:
                bits.append("（exception.txt 里没有解析出调用栈）")
        mods = (meta or {}).get("mods") or []
        if mods:
            bits.append(f"【该次会话加载的模组（{len(mods)} 个）】\n"
                        + "\n".join(f"- {m}" for m in mods[:80]))
        dlcs = (meta or {}).get("dlcs") or []
        if dlcs:
            bits.append(f"【启用的 DLC（{len(dlcs)} 个）】"
                        + "、".join(str(x) for x in dlcs[:40]))
        # 日志尾部：崩溃前最后发生的事
        for name in ("error.log",):
            p = Path(d).parent.parent / "logs" / name
            if is_file(p):
                try:
                    tail = read_text(p)[-6000:]
                    bits.append(f"【{name} 末尾】\n{tail}")
                except OSError:
                    pass
        if self.report is not None:
            bad = [f for f in self.report.findings
                   if f.severity in ("critical", "high")][:20]
            if bad:
                bits.append("【已知的致命/严重冲突】\n" + "\n".join(
                    f"- {f.title}" for f in bad))
        return {"ok": True, "dir": d, "text": "\n\n".join(bits)}

    def _latest_crash_dir(self) -> str:
        """最近一次崩溃目录的完整路径。"""
        dirs = find_crash_dirs(self.settings.paths, limit=1)
        return str(dirs[0]["path"]) if dirs else ""

    def ai_analyze_crash(self, crash_dir: str = "") -> bool:
        """后台任务：AI 分析一次崩溃，结果进补丁工坊。"""
        from .ai import analyze as A

        def job() -> dict:
            self._task_progress("整理崩溃数据", 0.05)
            p = self.crash_payload(crash_dir)
            if not p.get("ok"):
                return p
            if not p.get("text"):
                return {"ok": False, "error": "这次崩溃没有可分析的内容"}
            self._task_progress("请求 AI 分析原因", 0.3)
            res = A.analyze(self.settings, kind="崩溃报告",
                            payload=p["text"],
                            should_stop=lambda: self.cancelled,
                            source_ref=Path(p["dir"]).name)
            if not res.get("ok"):
                return res
            self._task_progress("整理分析结论", 0.9)
            n = self._ai_merge(res, group=f"崩溃 · {Path(p['dir']).name}")
            if self.cancelled:
                return self._cancelled_result("崩溃分析")
            self.log(f"AI 崩溃分析：{len(res.get('issues') or [])} 条结论"
                     f"（新增 {n}）")
            return {"ok": True, "added": n,
                    "total": len(res.get("issues") or []),
                    "summary": res.get("summary", ""),
                    "parsed": res.get("parsed", True),
                    "raw": res.get("raw", ""),
                    "groups": self.ai_issue_groups()}

        def done(res: Any, err: str) -> None:
            self.emit("ai_issues_done", {"result": res, "error": err,
                                         "kind": "crash"})
        return self.run_async("AI 分析崩溃原因", job, done)

    def ai_analyze_log(self, name: str = "error.log") -> bool:
        """后台任务：AI 分析一个日志文件。"""
        from .ai import analyze as A

        def job() -> dict:
            self._task_progress(f"读取 {name}", 0.05)
            an = self.analyze_log_file(name)
            if an.get("error"):
                return {"ok": False, "error": an["error"]}
            bits = [f"【日志文件】{name}",
                    f"【行数】{an.get('line_count', 0)}",
                    f"【错误条数】{an.get('error_count', 0)}"]
            errs = an.get("errors") or an.get("entries") or []
            lines = []
            for e in errs[:60]:
                if isinstance(e, dict):
                    lines.append("- " + str(e.get("text") or e.get("raw")
                                            or e.get("message") or e)[:300])
                else:
                    lines.append("- " + str(e)[:300])
            if lines:
                bits.append("【检测到的错误】\n" + "\n".join(lines))
            if an.get("summary"):
                bits.append(f"【本地规则给的摘要】{an['summary']}")
            self._task_progress("请求 AI 分析", 0.35)
            res = A.analyze(self.settings, kind="游戏日志",
                            payload="\n\n".join(bits),
                            should_stop=lambda: self.cancelled,
                            source_ref=name)
            if not res.get("ok"):
                return res
            self._task_progress("整理分析结论", 0.9)
            n = self._ai_merge(res, group=f"日志 · {name}")
            if self.cancelled:
                return self._cancelled_result("日志分析")
            self.log(f"AI 日志分析 {name}："
                     f"{len(res.get('issues') or [])} 条结论（新增 {n}）")
            return {"ok": True, "added": n, "name": name,
                    "total": len(res.get("issues") or []),
                    "summary": res.get("summary", ""),
                    "parsed": res.get("parsed", True),
                    "raw": res.get("raw", ""),
                    "groups": self.ai_issue_groups()}

        def done(res: Any, err: str) -> None:
            self.emit("ai_issues_done", {"result": res, "error": err,
                                         "kind": "log"})
        return self.run_async("AI 分析日志", job, done)

    def ai_patch_group(self, keys: list[str] | None = None) -> bool:
        """后台任务：对选中的 AI 问题逐条生成补丁。

        产出登记进统一账本，和「AI 生成补丁」共用同一条保存链路——
        这样"批量保存为统一补丁模组"不用改一行就能用。
        """
        from .ai import analyze as A
        from .ai.patchgen import AiPatchOutcome, PatchFile as _PF

        store = self._ai_issues()
        want = [k for k in (keys or list(store)) if k in store]
        picked = [store[k] for k in want
                  if store[k].get("ai_patchable") and not store[k].get("resolved")]
        if not picked:
            return False

        def job() -> dict:
            out, okn = [], 0
            total = len(picked)
            for i, it in enumerate(picked, 1):
                if self.cancelled:
                    break
                self._task_progress(
                    f"生成补丁 {i}/{total}：{it.get('title', '')[:30]}",
                    0.03 + (i - 1) / max(1, total) * 0.94)
                detail = (f"标题：{it.get('title')}\n"
                          f"原因：{it.get('cause')}\n"
                          f"建议修法：{it.get('fix')}\n"
                          f"相关模组：{'、'.join(it.get('mods') or [])}\n"
                          f"定位：{it.get('target')}")
                r = A.make_patch(self.settings, title=it.get("title", ""),
                                 detail=detail,
                                 should_stop=lambda: self.cancelled)
                target = it.get("target") or it.get("title", "")
                if r.get("ok"):
                    o = AiPatchOutcome(
                        ok=True,
                        files=[_PF(rel=f["rel"], text=f["text"])
                               for f in r["files"]],
                        explanation=r.get("explanation", ""),
                        risk=r.get("risk", ""),
                        provider=self.settings.ai.provider,
                        model=self.settings.ai.model,
                        verified=True, confidence="medium",
                    )
                    # 和「AI 生成补丁」走同一个账本 —— 保存链路不需要
                    # 知道自己拿到的是哪一种 AI 补丁。
                    self._ledger_add_outcome(target, o)
                    okn += 1
                    out.append({"ok": True, "title": it.get("title"),
                                "target": target, "files": len(r["files"])})
                else:
                    out.append({"ok": False, "title": it.get("title"),
                                "target": target,
                                "error": r.get("error", "")})
            self.log(f"AI 按问题生成补丁：成功 {okn}/{total}")
            return {"ok": True, "generated": okn, "total": total,
                    "results": out,
                    "note": "补丁已生成，检查无误后点「全部保存为补丁模组」写入。"}

        def done(res: Any, err: str) -> None:
            self.emit("ai_issues_done", {"result": res, "error": err,
                                         "kind": "patch"})
        return self.run_async("AI 生成补丁", job, done)

    def ai_resolve_issues(self, keys: list[str]) -> dict:
        """把选中的 AI 问题标记为已解决（从列表里去掉）。

        用户说"解决了就不显示"——但**不自动判定**：
        只有装好补丁或用户确认后才标记，避免把没修的问题藏起来。
        """
        store = self._ai_issues()
        n = 0
        for k in (keys or []):
            if k in store:
                store[k]["resolved"] = True
                n += 1
        if n:
            self.log(f"已标记 {n} 条 AI 结论为已解决")
        return {"ok": True, "resolved": n, "groups": self.ai_issue_groups()}

    def ai_issue_groups_visible(self) -> list[dict]:
        """只返回**未解决**的分组（界面用）。"""
        groups = self.ai_issue_groups()
        out = []
        for g in groups:
            live = [x for x in g["issues"] if not x.get("resolved")]
            if not live:
                continue
            g = dict(g)
            g["issues"] = live
            g["count"] = len(live)
            g["patchable"] = sum(1 for x in live if x.get("ai_patchable"))
            out.append(g)
        return out

    # ------------------------------------------------------------------
    # AI 排序：让模型看加载顺序并给建议
    # ------------------------------------------------------------------
    def ai_analyze_order(self) -> bool:
        """后台任务：AI 分析加载顺序，给排序建议。"""
        from .ai import analyze as A

        def job() -> dict:
            if self.library is None:
                return {"ok": False, "error": "请先加载模组清单"}
            self._task_progress("整理加载顺序", 0.1)
            order = self.library.load_order()
            lines = [f"{i + 1:>3}. {m.name}"
                     for i, m in enumerate(order)]
            bits = [f"【当前加载顺序（共 {len(order)} 个，序号小的先加载、"
                    f"先加载的会被后面的覆盖）】", "\n".join(lines)]
            if self.report is not None:
                bad = [f for f in self.report.findings
                       if f.severity in ("critical", "high")][:25]
                if bad:
                    bits.append("【已知冲突】\n" + "\n".join(
                        f"- [{f.kind}] {f.title}（当前生效：{f.winner or '?'}）"
                        for f in bad))
            self._task_progress("请求 AI 分析顺序", 0.35)
            res = A.analyze(
                self.settings, kind="模组加载顺序",
                payload="\n\n".join(bits),
                should_stop=lambda: self.cancelled,
                source_ref="加载顺序")
            if not res.get("ok"):
                return res
            self._task_progress("整理建议", 0.9)
            n = self._ai_merge(res, group="加载顺序建议")
            self.log(f"AI 顺序分析：{len(res.get('issues') or [])} 条建议")
            return {"ok": True, "added": n,
                    "summary": res.get("summary", ""),
                    "total": len(res.get("issues") or []),
                    "parsed": res.get("parsed", True),
                    "raw": res.get("raw", ""),
                    "groups": self.ai_issue_groups()}

        def done(res: Any, err: str) -> None:
            self.emit("ai_issues_done", {"result": res, "error": err,
                                         "kind": "order"})
        return self.run_async("AI 分析加载顺序", job, done)

    # ------------------------------------------------------------------
    # 日志与报告
    # ------------------------------------------------------------------
    def log_files(self) -> list[dict]:
        """列出游戏日志文件（``error.log``、``game.log`` 等）。

        每项含名称、完整路径、字节数、人性化大小、修改时间与格式化时间串。目录缺失
        或单个文件读不到时跳过，返回列表可能为空。只读，不改动日志。
        """
        out = []
        for name, path in iter_log_files(self.settings.paths):
            try:
                st = os.stat(long_path(path))
                out.append({"name": name, "path": path, "size": st.st_size,
                            "size_human": human_size(st.st_size),
                            "mtime": st.st_mtime,
                            "time_str": datetime.fromtimestamp(
                                st.st_mtime).strftime("%m-%d %H:%M:%S")})
            except OSError:
                continue
        return out

    def analyze_log_file(self, name: str = "error.log") -> dict:
        """（同步）分析一个日志文件，识别报错与已知问题。

        ``name`` 是日志文件名：先按名在日志目录里匹配，找不到再试
        ``logs_dir / name``。文件不存在时返回 ``{"error": ...}``，否则返回分析结果
        ``dict``（分类后的错误、建议等）。只读。
        """
        paths = self.settings.paths
        target = None
        for n, p in iter_log_files(paths):
            if n == name:
                target = p
                break
        if not target:
            cand = paths.logs_dir / name
            if is_file(cand):
                target = str(cand)
        if not target:
            return {"error": f"找不到日志文件 {name}"}
        an = analyze_log(target, progress=self._task_progress)
        return an.to_dict()

    def start_log_analysis(self, name: str = "error.log") -> bool:
        """在后台线程分析指定日志文件。

        返回是否成功排队；完成后通过 ``log_analysis_done`` 事件推送
        ``{"analysis": ..., "error": ...}``。
        """
        def job() -> dict:
            self._task_progress(f"分析 {name}", 0.05)
            return self.analyze_log_file(name)

        def done(res: Any, err: str) -> None:
            self.emit("log_analysis_done", {"analysis": res, "error": err})
        return self.run_async(f"分析日志 {name}", job)

    def crash_dirs(self) -> list[dict]:
        """列出群星崩溃转储目录（含时间、文件数、体积），供报告页选择分析对象。"""
        return find_crash_dirs(self.settings.paths)

    def generate_report(self, crash_dir: str = "") -> dict:
        """（同步）生成崩溃 / 运行问题报告。

        ``crash_dir`` 留空则自动挑最近的崩溃目录；已有的冲突检测结果会一并写进报告。
        **有副作用**：在报告目录下写出 Markdown / HTML 文件，并把结果记入
        ``self.last_report``。返回报告信息 ``dict``。
        """
        rep = generate_crash_report(
            self.settings.paths, crash_dir or None,
            conflicts=self.report, progress=self._task_progress)
        d = rep.to_dict()
        self.last_report = d
        self.log(f"崩溃报告已生成：{d.get('html_path') or d.get('md_path')}")
        return d

    def start_generate_report(self, crash_dir: str = "") -> bool:
        """在后台线程生成崩溃报告。

        返回是否成功排队；完成后通过 ``report_done`` 事件推送
        ``{"report": ..., "error": ...}``。

        **标记为不可取消**：写报告是"最后一步落盘"型的活，中途停掉只会
        留下半份文件。它本身也很快（以解析日志为主），等它跑完更稳妥。
        界面会据此把「停止」按钮置灰并说明原因——给一个点了没用的按钮更糟。
        """
        def job() -> dict:
            self._task_progress("生成崩溃报告", 0.05)
            return self.generate_report(crash_dir)

        def done(res: Any, err: str) -> None:
            self.emit("report_done", {"report": res, "error": err})
        return self.run_async("生成崩溃报告", job, done, cancellable=False)

    def reports_list(self) -> list[dict]:
        """列出报告目录里已生成的报告（新的排在前面）。

        每项含文件名、路径、体积、修改时间与类型（``html`` / ``md`` / ``json`` /
        ``other``）；目录不存在时返回空列表。
        """
        d = report_output_dir(self.settings.paths)
        out: list[dict] = []
        if not is_dir(d):
            return out
        try:
            for e in os.scandir(long_path(d)):
                if not e.is_file():
                    continue
                try:
                    st = e.stat()
                except OSError:
                    continue
                out.append({
                    "name": e.name, "path": e.path, "size": st.st_size,
                    "size_human": human_size(st.st_size), "mtime": st.st_mtime,
                    "time_str": datetime.fromtimestamp(st.st_mtime).strftime(
                        "%Y-%m-%d %H:%M:%S"),
                    "kind": ("html" if e.name.endswith(".html") else
                             "md" if e.name.endswith(".md") else
                             "json" if e.name.endswith(".json") else "other"),
                })
        except OSError:
            return out
        out.sort(key=lambda r: -r["mtime"])
        return out

    def read_report(self, path: str) -> dict:
        """读取一份报告的文本内容供界面预览。

        ``path`` 为报告文件路径；文件不存在或读取失败时返回 ``{"error": ...}``。
        """
        if not is_file(path):
            return {"error": "文件不存在"}
        try:
            txt = read_text(path)
        except OSError as exc:
            return {"error": str(exc)}
        return {"path": path, "name": os.path.basename(path),
                "content": txt, "size": len(txt)}

    # ------------------------------------------------------------------
    # 收尾：提醒 + 关机
    # ------------------------------------------------------------------
    def finish(self, *, do_shutdown: bool | None = None,
               do_reminder: bool | None = None,
               summary: str = "", details: list[str] | None = None) -> dict:
        """任务收尾：写提醒并（可选）安排关机。"""
        reports: list[str] = []
        if self.last_report:
            for k in ("md_path", "html_path"):
                p = self.last_report.get(k)
                if p:
                    reports.append(p)
        rep_dir = (str(report_output_dir(self.settings.paths))
                   if self.settings.paths.user_dir else "")
        h_before = self.report.health_score() if self.report else 0
        h_after = 0
        fixed = 0
        if self.last_apply:
            fixed = len([a for a in self.last_apply.actions
                         if a.ok and a.kind in ("db", "patch", "dlc_load")])
        if self.solution:
            h_after = self.solution.score_after
        crash = bool(self.monitor and self.monitor.crash_detected)

        if not summary:
            bits = []
            if self.report:
                bits.append(f"检测出 {self.report.count()} 处问题")
            if self.solution:
                bits.append(f"处理 {self.solution.change_count()} 项")
            if crash:
                bits.append("并分析了 1 次崩溃")
            summary = "、".join(bits) + "。" if bits else "任务已完成。"

        plan = finish_session(
            self.settings,
            reports=reports, report_dir=rep_dir,
            conflicts_fixed=fixed, health_before=h_before, health_after=h_after,
            crash_detected=crash, summary=summary, details=details,
            do_shutdown=do_shutdown, do_register=do_reminder,
        )
        out = {
            "ok": plan.ok,
            "messages": plan.messages,
            "reminder": plan.reminder.to_dict() if plan.reminder else None,
            "shutdown": (plan.shutdown.__dict__ if plan.shutdown else None),
        }
        for m in plan.messages:
            self.log(m)
        return out

    def cancel_shutdown(self) -> dict:
        """取消已安排的自动关机。

        先让本程序的关机调度器撤销，未成功再兜底调用系统级取消。返回
        ``{"ok": bool, "message": 说明文本}``，并记一行日志。
        """
        ok, msg = self.shutdown_scheduler.cancel()
        if not ok:
            ok, msg = cancel_all()[0], msg
        self.log(msg)
        return {"ok": ok, "message": msg}

    def pending_reminder(self) -> dict | None:
        """读取上次留下、等待展示的开机提醒。

        没有提醒时返回 ``None``，否则返回提醒的 ``dict``（摘要、报告路径、健康分
        变化等）。
        """
        r = load_reminder()
        return r.to_dict() if r else None

    def dismiss_reminder(self) -> dict:
        """丢弃待展示的开机提醒。

        **有副作用**：删除提醒 JSON 文件（文件本来就不存在也算成功）。
        返回 ``{"ok": True}``。
        """
        clear_reminder()
        return {"ok": True}

    def register_reminder(self, on: bool = True) -> dict:
        """注册或取消「下次开机提醒」。

        ``on`` 为真时写当前用户的 RunOnce 注册表项，下次开机自动弹出本次结果摘要；
        为假时删除该项（本来没注册也视为成功）。**有副作用**：修改当前用户注册表。
        返回 ``{"ok": bool, "message": 说明文本}``；非 Windows 平台一律返回失败。
        """
        if on:
            ok, msg = register_boot_reminder()
        else:
            from .system import unregister_boot_reminder
            ok, msg = unregister_boot_reminder()
        return {"ok": ok, "message": msg}

    def shutdown_now(self, delay: int = 60) -> dict:
        """安排一次系统关机（**危险操作：会真的关掉电脑**）。

        ``delay`` 为倒计时秒数（默认 60），期间界面会收到 ``shutdown_tick`` 事件显示
        剩余时间，用户可用 :meth:`cancel_shutdown` 撤销。返回 ``{"ok": bool,
        "remaining": 剩余秒数, "delay": 已设延迟}``。
        """
        sp = self.shutdown_scheduler.schedule(
            delay, f"{APP_NAME}：用户手动关机，{delay} 秒后执行")
        return {"ok": sp.enabled, "remaining": sp.remaining(),
                "delay": sp.delay_seconds}

    # ------------------------------------------------------------------
    # AI 接入
    # ------------------------------------------------------------------
    def ai_config(self) -> dict:
        """返回 AI 配置（密钥做掩码处理后再给界面）。"""
        from .ai.client import available_providers
        d = self.settings.ai.to_dict()
        key = str(d.get("api_key") or "")
        d["api_key_set"] = bool(key)
        d["api_key_masked"] = (key[:4] + "…" + key[-4:]) if len(key) > 9 else (
            "已设置" if key else "")
        d["api_key"] = ""            # 绝不把明文密钥发给前端
        d["configured"] = self.settings.ai.configured
        d["providers"] = available_providers()
        return d

    def save_ai_config(self, patch: dict) -> dict:
        """更新 AI 配置。

        约定：``api_key`` 为空字符串时表示「不修改」（避免把掩码写回去），
        传 ``None`` 或 ``"__CLEAR__"`` 表示清空。
        """
        ai = self.settings.ai
        allowed = {
            "provider", "base_url", "model", "temperature", "timeout",
            "max_tokens", "verify_ssl", "allow_send_mod_content",
            "auto_verify", "stream",
        }
        for k, v in (patch or {}).items():
            if k == "api_key":
                if v == "__CLEAR__":
                    ai.api_key = ""
                elif isinstance(v, str) and v.strip():
                    ai.api_key = v.strip()
                continue
            if k in allowed:
                setattr(ai, k, v)
        self.settings.save()
        self.log(f"AI 配置已保存（供应商：{ai.provider}，"
                 f"模型：{ai.model or '默认'}）")
        return self.ai_config()

    def ai_providers(self) -> list[dict]:
        """列出内置的 AI 供应商预设（名称、默认接口地址、默认模型等）。

        纯静态数据，不联网、不返回任何密钥内容。
        """
        from .ai.client import available_providers
        return available_providers()

    def ai_config_with(self, override: dict | None = None) -> dict:
        """取 AI 配置，并用界面上的**未保存改动**覆盖。

        为什么需要：用户点了「填好密钥 → 拉取模型 → 测试连接」，
        但没点保存。此时磁盘上还没有密钥，请求会因为 401 失败——
        而旧版会静默退回内置预设，界面上就显示成
        「拉取到 2 个模型」，那些名字根本不是他账号里的。
        这里让表单值临时生效，测试与拉取都按用户眼前看到的配置走。

        ``api_key`` 为空表示沿用已保存的密钥（界面永远拿不到明文）。
        """
        cfg = self.settings.ai.to_dict()
        for k, v in (override or {}).items():
            if v is None or v == "":
                continue
            if k == "api_key":
                if v != "__CLEAR__":
                    cfg["api_key"] = str(v).strip()
                continue
            cfg[k] = v
        return cfg

    def ai_test(self, override: dict | None = None) -> dict:
        """测试 AI 连通性。

        ``override`` 是界面上尚未保存的设置（见 :meth:`ai_config_with`）。
        """
        from .ai.client import test_connection
        cfg = self.ai_config_with(override)
        self.log(f"正在测试 AI 连接：{cfg.get('provider')} / "
                 f"{cfg.get('model') or '默认模型'} …")
        res = test_connection(cfg)
        if res.get("ok"):
            self.settings.ai.last_test_ok = True
            self.settings.ai.last_test_at = datetime.now().strftime(
                "%Y-%m-%d %H:%M:%S")
            self.settings.save()
            self.log(f"AI 连接成功：{res.get('provider')} / {res.get('model')}"
                     f"（{res.get('elapsed')}s）")
        else:
            self.settings.ai.last_test_ok = False
            self.settings.save()
            self.log(f"AI 连接失败：{res.get('error')}", "error")
        return res

    def ai_list_models(self, override: dict | None = None) -> dict:
        """向当前配置的 AI 服务拉取可用模型列表（**会联网**）。

        返回 ``{"ok", "models", "source", "error", "provider"}``。
        ``source == "api"`` 才是真的问到了；``"fallback"`` 表示请求失败、
        给的是内置预设，界面必须如实说明而不是显示成「检测到 N 个模型」。
        """
        from .ai.client import list_models_ex
        cfg = self.ai_config_with(override)
        try:
            return list_models_ex(cfg)
        except Exception as exc:
            return {"ok": False, "error": str(exc), "models": [],
                    "source": "error"}

    # ------------------------------------------------------------------
    # AI 补丁生成
    # ------------------------------------------------------------------
    def patch_candidates(self, limit: int = 200) -> list[dict]:
        """列出**打补丁能修**的冲突，供补丁工坊选择。

        只挑真正能通过「重定义」解决的类型：
        S1（脚本对象重定义）、F1（文件覆盖）、L1（本地化键冲突）。
        依赖缺失、版本不符这类不是补丁能修的，也不列出来。

        过滤规则集中在 :meth:`_patch_rows`，和 :meth:`_find_finding`
        共用同一份列表——两边各写一套过滤条件的话，
        一旦有一边加了条件（比如"排除已被补丁解决的"），
        序号就会错位，界面上点第 3 条实际改的是第 4 条。
        """
        rows = self._patch_rows()
        out: list[dict] = []
        for i, f in enumerate(rows):
            ai_done = f.target in getattr(self, "_ai_patched", set())
            out.append({
                "index": i,                 # 在 _patch_rows() 里的真实下标
                "kind": f.kind,
                "severity": f.severity,
                "severity_label": SEV_LABEL.get(f.severity, f.severity),
                "title": f.title,
                "target": f.target,
                "category": f.category,
                "mod_names": list(f.mod_names),
                "mods": list(f.mods),
                "winner": f.winner,
                "detail": (f.detail or "")[:600],
                "recommend": ("AI 可合并" if f.kind == "S1"
                              else "AI 可给出取舍建议"),
                "ai_patched": ai_done,
            })
            if limit and len(out) >= limit:
                break
        return out

    def _patch_rows(self, *, show_patched: bool = False) -> list:
        """适合打补丁的冲突行（**未经 limit 截断**）。

        排除已被补丁解决的条目：那一条已经处理过了，
        再列出来用户会以为没生效，重复生成一堆一模一样的补丁。
        """
        if not self.report:
            return []
        cov = None if show_patched else self._patch_coverage()
        return [f for f in self.report.findings
                if f.kind in ("S1", "F1", "L1")
                and f.severity in ("critical", "high", "medium")
                and not (cov is not None and self.finding_is_patched(f, cov))]

    def _find_finding(self, index: int):
        """按 :meth:`patch_candidates` 的序号取回 Finding。"""
        rows = self._patch_rows()
        if 0 <= index < len(rows):
            return rows[index]
        return None

    def ai_patch_preview(self, index: int, *, user_hint: str = "",
                         goal: str = "merge") -> dict:
        """构建提示词预览（让用户看清到底会发什么出去）。"""
        from .ai.prompt import build_patch_prompt, estimate_tokens, \
            gather_conflict_context
        f = self._find_finding(index)
        if f is None:
            return {"error": "找不到该冲突"}
        if self.library is None:
            return {"error": "请先加载模组清单"}
        order = [m.key for m in self.library.load_order()]
        ctx = gather_conflict_context(f, self.library, load_order=order)
        msgs = build_patch_prompt(ctx, user_hint=user_hint, goal=goal)
        return {
            "ok": True,
            "context": ctx.to_dict(),
            "system": msgs[0].content,
            "user": msgs[1].content,
            "tokens_estimate": estimate_tokens(msgs),
            "payload_chars": sum(len(m.content) for m in msgs),
            "provider": self.settings.ai.provider,
            "model": self.settings.ai.model,
            "allow_send": self.settings.ai.allow_send_mod_content,
        }

    def ai_generate_patch(self, index: int, *, user_hint: str = "",
                          goal: str = "merge",
                          progress: Callable[[str, float], None] | None = None
                          ) -> dict:
        """调用 AI 为一个冲突生成补丁。"""
        from .ai.patchgen import generate_patch_with_ai
        from .ai.prompt import gather_conflict_context

        f = self._find_finding(index)
        if f is None:
            return {"ok": False, "error": "找不到该冲突"}
        if self.library is None:
            return {"ok": False, "error": "请先加载模组清单"}
        if not self.settings.ai.allow_send_mod_content:
            return {
                "ok": False,
                "error": "「允许发送模组内容给 AI」未打开。"
                         "请在 AI 接入页显式开启后再试——"
                         "程序不会在你不知情的情况下把模组内容发出去。",
                "need_consent": True,
            }
        if not self.settings.ai.configured:
            return {"ok": False,
                    "error": "AI 尚未配置完成（缺少 API Key 或模型名）",
                    "need_config": True}

        order = [m.key for m in self.library.load_order()]
        ctx = gather_conflict_context(f, self.library, load_order=order)
        if progress:
            progress(f"正在请求 {self.settings.ai.provider} …", 0.25)

        outcome = generate_patch_with_ai(
            ctx, cfg=self.settings.ai.to_dict(), goal=goal,
            user_hint=user_hint, indexes=self.indexes,
            should_stop=lambda: self.cancelled,
        )
        d = outcome.to_dict()

        if outcome.files:
            self._ai_patched = getattr(self, "_ai_patched", set())
            self._ai_patched.add(f.target)
            # 登记进统一账本（同时被"待保存列表"和"已处理去重"用到）
            self._ledger_add_outcome(f.target, outcome)
            self.log(
                f"AI 补丁生成：{f.target} → {len(outcome.files)} 个文件，"
                f"校验{'通过' if outcome.verified else '未通过'}"
                f"（{outcome.elapsed:.1f}s）",
                "info" if outcome.verified else "warn")
        else:
            self.log(f"AI 未生成补丁：{outcome.error or '模型只给了文字建议'}",
                     "warn")
        return d

    def ai_save_patch(self, target: str = "", *, title: str = "",
                      description: str = "") -> dict:
        """把 AI 生成的补丁保存为真正的模组。"""
        from .patch.service import save_patch
        entry = self.patch_ledger().get("ai", target or "")
        if entry is None or not entry.files:
            return {"ok": False, "error": "没有待保存的 AI 补丁（请先生成）"}
        if entry.block:
            return {"ok": False,
                    "error": f"补丁有问题，已拒绝保存：{entry.block}"}
        name = title or entry.title
        res = save_patch(
            self.settings.paths, title=name, files=list(entry.files),
            description=description or entry.description, source="ai",
            conflict=entry.conflict or target, provider=entry.provider,
            extra_readme=("【AI 风险评估】\n" + entry.risk) if entry.risk else "",
        )
        if res.get("ok"):
            self.patch_ledger().mark_saved("ai", target or "")
            self.log(f"AI 补丁已保存为模组：{res.get('name')}")
            try:
                self.library = load_library(self.settings.paths)
            except Exception:
                pass
        return res

    def batch_save_plan(self, engine: str = "ai") -> dict:
        """批量保存前的预览：有多少待保存、哪些会被拒绝。

        ``engine='ai'`` 看 AI 生成的缓存，``'local'`` 看本地规则草稿。
        提前把"会被拒绝"的挑出来告诉用户，别等点了保存才发现少了一半。
        """
        items = self._pending_patch_items(engine)
        ok = [x for x in items if x["savable"]]
        bad = [x for x in items if not x["savable"]]
        return {"ok": True, "engine": engine,
                "total": len(items), "savable": len(ok),
                "rejected": len(bad),
                "reject_reasons": [x["reason"] for x in bad[:5]],
                "items": items}

    def _pending_patch_items(self, engine: str) -> list[dict]:
        """列出"已生成但还没保存成模组"的补丁。

        数据全部来自统一账本——三个来源（本地草稿 / AI 按冲突 /
        AI 按分析结论）在这里不再有任何分支差异。
        """
        return [e.to_dict() for e in self.patch_ledger().pending(engine)]

    def batch_save_patches(self, engine: str = "ai") -> dict:
        """把已生成的补丁**一次性全部保存成模组**。

        逐个保存而不是合并成一个：每个补丁针对一个冲突，
        合并后很难单独关掉或回退某一个。任何一个失败都不影响其余的，
        最后汇总告诉你成功几个、失败几个、为什么。
        """
        from .patch.service import PatchFile, save_patch

        items = [x for x in self._pending_patch_items(engine) if x["savable"]]
        if not items:
            return {"ok": True, "saved": 0, "failed": 0, "results": [],
                    "note": "没有待保存的补丁（可能都已经保存过了）"}

        results: list[dict] = []
        saved = 0
        led = self.patch_ledger()
        for n, it in enumerate(items, 1):
            self._task_progress(
                f"保存补丁 {n}/{len(items)}：{it['title'][:30]}",
                0.02 + (n - 1) / max(1, len(items)) * 0.94)
            try:
                # 三个来源在账本里是同一种东西，保存路径只有一条。
                entry = led.get(it["engine"], it["key"])
                if entry is None:
                    results.append({**it, "ok": False,
                                    "error": "这条补丁已失效，请重新生成"})
                    continue
                res = save_patch(
                    self.settings.paths, title=entry.title,
                    files=list(entry.files),
                    description=entry.description, source=entry.engine,
                    conflict=entry.conflict or entry.target,
                    provider=entry.provider,
                    extra_readme=("【AI 风险评估】\n" + entry.risk)
                    if entry.risk else "",
                )
                if res.get("ok"):
                    led.mark_saved(it["engine"], it["key"])
                    saved += 1
                    results.append({**it, "ok": True,
                                    "name": res.get("name", "")})
                else:
                    results.append({**it, "ok": False,
                                    "error": res.get("error", "保存失败")})
            except Exception as exc:
                results.append({**it, "ok": False, "error": str(exc)})

        failed = len(results) - saved
        if saved:
            self.log(f"批量保存补丁：成功 {saved} 个"
                     + (f"，失败 {failed} 个" if failed else ""),
                     "info" if not failed else "warn")
            try:
                self.library = load_library(self.settings.paths)
            except Exception:
                pass
            # 补丁装好了，之前那些冲突就不该再烦用户了
            self.mark_patched_findings()
        return {
            "ok": True, "saved": saved, "failed": failed,
            "results": results, "total": len(results),
            "mods": self.mods_list(),
            "hidden": self.findings_filter_note().get("hidden_patched", 0),
            "note": "所有补丁都合并进同一个模组「熵光矩阵 · 兼容补丁」"
                    "（排在最后加载），后续新增的也会并进它，"
                    "不会每生成一次就多出一个模组。"
                    "被它解决的冲突已从列表里隐藏。",
        }

    # ------------------------------------------------------------------
    # 时之虫档案（群星知识库）
    # ------------------------------------------------------------------
    def wiki_home(self) -> dict:
        """档案首页：元信息 + 分类目录 + 可送进游戏的命令。"""
        from . import wiki
        return {
            "ok": True,
            "meta": wiki.meta(),
            "categories": wiki.categories(),
            "commands": wiki.console_commands(),
            "console": self.console_history(),
            "game_running": bool(find_game_pids()),
        }

    def wiki_search(self, query: str, limit: int = 200) -> dict:
        """在档案里全文检索。"""
        from . import wiki
        return wiki.search(query, limit=limit)

    def wiki_section(self, key: str, offset: int = 0,
                     limit: int = 300) -> dict:
        """按分类取行（分页）。"""
        from . import wiki
        return wiki.section_rows(key, offset=offset, limit=limit)

    def wiki_ask(self, question: str) -> dict:
        """问档案一个自然语言问题。

        先**本地检索**出最相关的若干条，再把这些条目连同问题一起发给 AI。
        为什么不把整份档案（8689 条）发过去：
        既超出上下文，又贵，而且大部分内容与问题无关。
        检索作为"缩小范围"的手段，AI 只负责读懂与作答。
        """
        from . import wiki
        from .ai import analyze as A

        q = (question or "").strip()
        if not q:
            return {"ok": False, "error": "请输入问题"}
        # 用面向"自然语言问题"的检索：整句匹配不到就退化成 OR 关键词。
        # 否则问"怎么快速解锁全部科技"会一条都检索不到，
        # 白白浪费一次 AI 调用。
        hits = wiki.search_for_question(q, limit=40)
        rows = hits.get("hits") or []
        # 检索不到就直接把问题交给 AI —— 它可能知道常识性的游戏知识，
        # 只是档案里没有对应词条。这时如实说明"档案里没检索到"。
        bits = []
        if rows:
            bits.append(f"【档案里检索到 {hits['total']} 条相关条目，"
                        f"以下是前 {len(rows)} 条】")
            for h in rows:
                cells = [c for c in (h.get("cells") or []) if c]
                bits.append(f"- [{h.get('section_title')}] "
                            + " ｜ ".join(cells))
        else:
            bits.append("【档案里没有检索到直接相关的条目】")
        bits.append(f"\n【用户问题】{q}")
        system = (
            "你是《群星》(Stellaris) 的游戏顾问，服务于一个中文模组工具。"
            "用户会带着一个问题，附上从内置知识档案里检索到的条目。\n"
            "请：\n"
            "1. 先用一两句话直接回答\n"
            "2. 需要控制台命令时，给出**可直接复制**的完整命令（含参数）\n"
            "3. 只依据检索到的条目与你的游戏知识，不要编造不存在的命令\n"
            "4. 如果档案里没有、你也不确定，就明说'不确定'\n"
            "5. 用中文，简洁，不要客套")
        try:
            text = A.ask(self.settings, system, "\n".join(bits),
                         temperature=0.3,
                         should_stop=lambda: self.cancelled)
        except (A.AiNotReady, RuntimeError) as exc:
            return {"ok": False, "error": str(exc),
                    "kind": getattr(exc, "kind", "error")}
        # 从回答里抠出看起来像控制台命令的行，供"一键加入待送队列"
        cmds = _extract_commands(text)
        return {"ok": True, "answer": text, "hits": len(rows),
                "total_hits": hits.get("total", 0), "commands": cmds}

    def wiki_write_commands(self, want: str) -> dict:
        """让 AI 按需求写控制台命令。

        和 :meth:`wiki_ask` 分开：这个只输出命令，不要解释，
        解析更严格（每行一条），拿到就能直接送进游戏。
        """
        from . import wiki
        from .ai import analyze as A

        w = (want or "").strip()
        if not w:
            return {"ok": False, "error": "请说明你想要什么效果"}
        hits = wiki.search(w, limit=25)
        ctx = []
        for h in (hits.get("hits") or []):
            cells = [c for c in (h.get("cells") or []) if c]
            ctx.append("- " + " ｜ ".join(cells))
        system = (
            "你是《群星》(Stellaris) 控制台命令专家。"
            "用户描述想要的效果，你给出**能直接在游戏控制台里执行的命令**。\n"
            "只输出命令本身，一行一条，不要编号、不要解释、不要 Markdown 代码块。\n"
            "命令可以带参数。需要先选中目标的命令，请在行尾用 ` # 需先选中` 注明。\n"
            "不确定效果能不能实现时，宁可少给几条，不要编造命令名。")
        user = f"【想要的效果】{w}\n"
        if ctx:
            user += "\n【档案里相关的命令（优先从这里挑）】\n" + "\n".join(ctx)
        try:
            text = A.ask(self.settings, system, user, temperature=0.2,
                         should_stop=lambda: self.cancelled)
        except (A.AiNotReady, RuntimeError) as exc:
            return {"ok": False, "error": str(exc),
                    "kind": getattr(exc, "kind", "error")}
        cmds = _extract_commands(text)
        if not cmds:
            return {"ok": False, "error": "AI 没有给出可用的命令",
                    "raw": text}
        return {"ok": True, "commands": cmds, "raw": text,
                "hits": len(ctx)}

    # ------------------------------------------------------------------
    # 控制台（指令台）
    # ------------------------------------------------------------------
    def console_library(self) -> dict:
        """命令库 + 游戏控制台历史的当前状态。"""
        from .console import CATEGORIES, library
        hist = self.console_history()
        return {
            "ok": True,
            "commands": library(),
            "categories": list(CATEGORIES),
            "history": hist,
            # 游戏运行中写历史有被游戏自己覆盖的风险，界面要提示
            "game_running": bool(find_game_pids()),
            "path": str(self.settings.paths.console_history),
        }

    def console_history(self) -> dict:
        """读游戏的控制台历史。"""
        from .console import read_history
        return read_history(self.settings.paths)

    def console_push(self, commands: list[str],
                     keep_existing: bool = True) -> dict:
        """把选中的命令送进游戏控制台历史。

        进游戏按 ``~`` 打开控制台、再按 ``↑`` 即可翻到并回车执行。
        **这是唯一不改游戏、不装 mod 就能让命令进游戏的办法。**
        """
        from .console import push_commands
        res = push_commands(self.settings.paths, commands,
                            keep_existing=keep_existing)
        if res.get("ok"):
            self.log(f"已把 {res.get('pushed')} 条命令送进游戏控制台"
                     f"（保留原有历史 {res.get('kept')} 条）")
            if find_game_pids():
                res["warning"] = (
                    "游戏正在运行，它可能在退出时回写自己的历史并覆盖本次推送。"
                    "建议退出游戏后再推送，或推送后立刻进游戏使用。")
        return res

    def console_clear(self) -> dict:
        """清空游戏控制台历史（会先备份）。"""
        from .console import write_history
        res = write_history(self.settings.paths, [])
        if res.get("ok"):
            self.log("已清空游戏控制台历史（原文件已备份）")
        return res

    # ------------------------------------------------------------------
    # 待保存补丁的统一账本
    # ------------------------------------------------------------------
    def _batch_handled_keys(self, engine: str) -> set[str]:
        """该来源下"已经生成过补丁"的目标，用于把它们从待处理列表里剔除。

        为什么要单独记账：补丁**生成**之后还没保存到磁盘，
        ``_patch_coverage``（靠扫补丁模组目录）自然看不到它们。
        于是用户跑完一轮本地批量，列表里那一批还在、数量也一点没减，
        看起来像什么都没做——实际上草稿已经在内存里了。

        数据来自统一账本（见 :meth:`patch_ledger`）。
        """
        return self._ledger_handled_keys(engine)

    def patch_ledger(self) -> Any:
        """待保存补丁的账本（惰性建，全局唯一一份）。

        三个来源——本地规则草稿、AI 按冲突生成、AI 按分析结论生成——
        全部登记进这里。**不要**再各存一份：补丁保存会写用户磁盘，
        在那种路径上留重复逻辑迟早出事。
        """
        led = getattr(self, "_patch_ledger", None)
        if led is None:
            from .patch.ledger import PatchLedger
            led = PatchLedger()
            self._patch_ledger = led
        return led

    def _ledger_add_local(self, drafts: list) -> int:
        """把本地规则草稿登记进账本。"""
        from .ai.patchgen import PatchFile
        from .patch.ledger import PatchEntry
        led = self.patch_ledger()
        n = 0
        for d in drafts or []:
            rel = getattr(d, "rel_path", "") or ""
            if not rel:
                continue
            led.add(PatchEntry(
                engine="local", target=rel,
                title=getattr(d, "title", "") or rel,
                conflict=rel,
                files=[PatchFile(rel=rel, text=getattr(d, "body", "") or "")],
                description=getattr(d, "reason", "") or "",
                aliases={rel, strip_patch_marks(rel.rsplit("/", 1)[-1])},
            ))
            n += 1
        return n

    def _ledger_add_outcome(self, target: str, outcome: Any) -> None:
        """把一次 AI 生成结果登记进账本。"""
        from .patch.ledger import PatchEntry
        files = list(getattr(outcome, "files", None) or [])
        blocking = list(getattr(outcome, "blocking", None) or [])
        if not files:
            block = "模型只给了文字建议，没有产出文件"
        elif blocking:
            block = (f"{len(blocking)} 个严重问题："
                     + "；".join(i.message for i in blocking[:2]))
        else:
            block = ""
        self.patch_ledger().add(PatchEntry(
            engine="ai", target=target or "(未命名)",
            title=f"AI 补丁：{(target or '未命名')[:28]}",
            conflict=getattr(outcome, "conflict", "") or target,
            files=files, block=block,
            description=getattr(outcome, "explanation", "") or "",
            provider=f"{getattr(outcome, 'provider', '')}/"
                     f"{getattr(outcome, 'model', '')}",
            risk=getattr(outcome, "risk", "") or "",
            aliases={target or "", strip_patch_marks(
                (target or "").rsplit("/", 1)[-1])},
        ))

    def _ledger_handled_keys(self, engine: str) -> set[str]:
        """该来源下"已经生成过补丁"的目标集合（供候选列表去重）。"""
        out: set[str] = set()
        for e in self.patch_ledger().all_entries():
            if engine and e.engine != engine:
                continue
            out.add(e.target.lower())
            if e.conflict:
                out.add(e.conflict.lower())
            out |= {a.lower() for a in e.aliases}
        out.discard("")
        return out

    def _candidate_batch_key(self, c: dict) -> str:
        """候选在批量记账里用的键。"""
        return str(c.get("target") or "").strip().lower()

    @staticmethod
    def _candidate_is_handled(c: dict, handled: set[str]) -> bool:
        """这个候选是不是已经被处理过了。

        记账里存了两种形式（草稿的 ``rel_path`` 与 ``title``），
        候选这边有 ``target`` 与 ``title``，对不上的情况比想象中多：

        * 文件覆盖类：候选 ``target`` 是 ``common/x/原名.txt``，
          而草稿 ``rel_path`` 是 ``common/x/zzz_SMD_auto_merge_原名.txt``
          ——**补丁文件名带前缀**，两边永远不相等
        * 对象重定义类：候选 ``target`` 是对象名，草稿标题里才带它

        所以除了精确比对，还要按"剥掉补丁前缀后的文件名"再比一次，
        并对**像路径的**键做子串匹配。子串方向是
        ``候选值 in 记账键``（记账键更长），反过来永远匹配不上。
        """
        tgt = str(c.get("target") or "").strip().lower()
        tit = str(c.get("title") or "").strip().lower()
        if tgt and tgt in handled:
            return True
        if tit and tit in handled:
            return True
        # 候选 target 的末段（文件名）
        tgt_base = tgt.rsplit("/", 1)[-1] if tgt else ""
        for h in handled:
            if not h:
                continue
            # 记账键剥掉补丁前缀后的末段
            h_base = strip_patch_marks(h.rsplit("/", 1)[-1])
            if tgt_base and "." in tgt_base and h_base == tgt_base:
                return True
            # 像路径的键：看候选值是否被它包含（记账键更长）
            if "/" in h or "." in h:
                if tgt and tgt in h:
                    return True
                if tit and tit in h:
                    return True
        return False

    def batch_patch_plan(self, indices: list[int] | None = None,
                         engine: str = "local") -> dict:
        """批量生成前的预览：会处理哪些目标、能产出多少。

        ``engine`` 为 ``local`` / ``ai``。本地引擎零成本、
        没有的能力（需要理解语义的修复）会如实说明交给 AI。

        **已经处理过的目标不再列出**：本地批量跑完后那些条目应当消失，
        否则用户会以为没生效而反复点。
        """
        from .patch.local import RULES
        if self.report is None:
            return {"ok": False, "error": "还没有检测结果，请先点「开始检测」"}
        cands = self.patch_candidates(100000)
        done = self._batch_handled_keys(engine)
        if done:
            cands = [c for c in cands if not self._candidate_is_handled(c, done)]
        want = ({int(i) for i in indices} if indices
                else {c["index"] for c in cands})
        picked = [c for c in cands if c["index"] in want]

        # 本地引擎按问题类型判断能不能处理
        local_ok, ai_only = [], []
        for c in picked:
            kind = str(c.get("kind") or "").upper()
            (local_ok if kind in RULES else ai_only).append(c)
        est = len(local_ok) if engine == "local" else len(picked)
        return {
            "ok": True, "engine": engine,
            "total_candidates": len(cands),
            "selected": len(picked),
            "local_capable": len(local_ok),
            "ai_only": len(ai_only),
            "estimate": est,
            # 让界面能提示"这些类型本地做不了"
            "ai_only_examples": [c["title"][:60] for c in ai_only[:5]],
            "allow_send": bool(self.settings.ai.allow_send_mod_content),
            "ai_configured": bool(self.settings.ai.configured),
            # 界面上要把这两个数说清楚，否则"数字变小了"看着莫名其妙
            "handled": len(done),
            "candidates": [{"index": c["index"], "title": c["title"],
                            "kind": c["kind"],
                            "severity_label": c.get("severity_label", ""),
                            "local_capable": str(c.get("kind") or "").upper()
                            in RULES}
                           for c in picked],
        }

    def start_batch_patches(self, indices: list[int] | None = None,
                            engine: str = "local", goal: str = "merge",
                            user_hint: str = "") -> bool:
        """批量生成补丁的后台任务。

        * ``engine='local'``：走本地规则引擎，**不联网、不花钱**，
          对它的能力边界外的目标会跳过并如实报告。
        * ``engine='ai'``：逐个调 AI。**会联网、按 token 计费**，
          且必须先打开「允许发送模组内容」——没有开关就直接拒绝整批。
        """
        def job() -> dict:
            plan = self.batch_patch_plan(indices, engine)
            if not plan.get("ok"):
                return plan
            cands = self.patch_candidates(100000)
            want = ({int(i) for i in indices} if indices
                    else {c["index"] for c in cands})
            picked = [c for c in cands if c["index"] in want]
            if not picked:
                return {"ok": False, "error": "没有选中任何目标"}

            out: list[dict] = []
            if engine == "local":
                from .patch.local import generate_local_patches
                self._task_progress("本地规则引擎生成中", 0.02)
                drafts = generate_local_patches(
                    self.report, self.library, self.indexes,
                    progress=lambda msg, p: self._task_progress(msg, 0.02 + p * 0.93),
                    should_stop=lambda: self.cancelled,
                )
                self._auto_drafts = drafts
                if self.cancelled:
                    self.log("批量本地补丁已被用户停止", "warn")
                    return self._cancelled_result("批量生成")
                for d in drafts:
                    out.append({"engine": "local", "title": d.title,
                                "ok": True, "rule": d.rule,
                                "lines": d.lines, "confidence": d.confidence,
                                "reason": d.reason})
                self._task_progress("批量生成完成", 1.0)
                # 登记进统一账本：界面据此把已处理的目标从待处理列表剔除。
                # 草稿的 rel_path 与 title 都作为别名记上——
                # 候选的 target 有时是文件路径、有时是对象名。
                n_ledger = self._ledger_add_local(drafts)
                res = {"ok": True, "engine": "local", "results": out,
                       "generated": len(drafts),
                       "skipped": len(picked) - len(drafts),
                       "handled": len(self._batch_handled_keys("local")),
                       "note": "本地引擎只处理它看得懂的类型；"
                               "其余类型请用 AI 引擎。"
                               "已处理的目标已从下面的列表移除。"}
                self.log(f"批量本地补丁：{len(drafts)} 份草稿"
                         f"（账本登记 {n_ledger} 条）")
                return res

            # ---- AI 批量 ----
            if not self.settings.ai.allow_send_mod_content:
                return {"ok": False, "need_consent": True,
                        "error": "未开启「允许发送模组内容」。"
                                 "AI 批量生成会把模组内容发送给服务商，"
                                 "请先到 AI 接入页开启。"}
            if not self.settings.ai.configured:
                return {"ok": False, "need_config": True,
                        "error": "AI 尚未配置完成（缺少 API Key 或模型名）"}
            total = len(picked)
            for n, c in enumerate(picked, 1):
                if self.cancelled:
                    out.append({"engine": "ai", "ok": False,
                                "title": c["title"], "error": "用户中止"})
                    break
                self._task_progress(
                    f"AI 生成第 {n}/{total} 个：{c['title'][:30]}",
                    0.05 + (n - 1) / max(1, total) * 0.9)
                r = self.ai_generate_patch(c["index"], user_hint=user_hint,
                                           goal=goal)
                ok = bool(r.get("files") or r.get("ok"))
                if ok:
                    # 成功的记下来，下一轮不再列出来
                    self._batch_handled_keys("ai").add(
                        self._candidate_batch_key(c))
                out.append({
                    "engine": "ai", "title": c["title"], "ok": ok,
                    "verified": r.get("verified"),
                    "files": len(r.get("files") or []),
                    "error": r.get("error") or "",
                })
            okn = sum(1 for x in out if x.get("ok"))
            self._task_progress("批量生成完成", 1.0)
            self.log(f"批量 AI 补丁：成功 {okn}/{total}")
            return {"ok": True, "engine": "ai", "results": out,
                    "generated": okn, "failed": total - okn,
                    "handled": len(self._batch_handled_keys("ai")),
                    "note": "AI 生成的补丁已经过语法与引用校验；"
                            "有严重问题的会被拒绝保存。"
                            "成功的目标已从下面的列表移除。"}

        def done(res: Any, err: str) -> None:
            self.emit("batch_patch_done",
                      {"result": res, "error": err, "candidates": None})
        return self.run_async("批量生成补丁", job, done)

    def start_batch_translate(self, target: str = "zh", limit: int = 0
                              ) -> bool:
        """批量翻译的后台任务（等价于「一键翻译」，供补丁工坊调用）。"""
        return self.start_translate_all(target, limit=limit)

    def translation_sheet(self, target: str = "zh", limit: int = 200
                          ) -> dict:
        """取出待翻译条目，交给用户**自己翻**。

        返回 ``{ok, lang, suffix, rows:[{key,text,mod,rel}], total}``。
        用户改完通过 :meth:`save_manual_translation` 写回。
        """
        from .translate import LANGUAGES, collect_entries
        if self.library is None:
            return {"ok": False, "error": "请先加载模组清单"}
        plan = collect_entries(self.library, target, limit=limit)
        rows = [{"key": e.key, "text": e.text, "mod": e.mod_name,
                 "rel": e.source_rel} for e in plan.entries]
        return {
            "ok": True, "target": target,
            "label": LANGUAGES.get(target, ("", target, ""))[1],
            "suffix": plan.suffix,
            "rows": rows, "total": len(rows),
            "all_total": len(collect_entries(self.library, target).entries),
        }

    def save_manual_translation(self, target: str,
                                entries: dict[str, str]) -> dict:
        """保存用户手写的译文（写进独立的翻译补丁模组）。

        ``entries`` 结构 ``{本地化键: 译文}``。空译文会被丢弃——
        写一条空字符串进去会让游戏显示出空文本，比没翻译更糟。
        """
        from .translate import LANGUAGES, write_translation_files
        if self.library is None:
            return {"ok": False, "error": "请先加载模组清单"}
        clean = {str(k): str(v).strip() for k, v in (entries or {}).items()
                 if str(v or "").strip()}
        if not clean:
            return {"ok": False, "error": "没有填写任何译文"}

        # 按模组归类：译文要写进对应模组的文件里。
        # 先建一张「本地化键 -> 拥有它的模组」索引（从各模组的英文原文提取）。
        from .translate import _loc_files
        by_mod: dict[str, dict[str, str]] = {}
        owner: dict[str, str] = {}
        for m in self.library.all():
            cdir = m.content_dir()
            if not cdir:
                continue
            for p in _loc_files(Path(cdir), "l_english"):
                try:
                    for k in _parse_localisation(read_text(p)):
                        owner.setdefault(k, m.key)
                except OSError:
                    continue
        for k, v in clean.items():
            mk = owner.get(k)
            if mk:
                by_mod.setdefault(mk, {})[k] = v
        if not by_mod:
            return {"ok": False,
                    "error": "这些键没有找到归属模组，无法写入"}

        out_root = self._translation_out_root()
        written = write_translation_files(self.library, by_mod, target,
                                          out_root=out_root)
        n = sum(w.get("count", 0) for w in written if w.get("ok"))
        self.log(f"已保存手写译文 {n} 条（{LANGUAGES.get(target,('','',''))[1]}）")
        try:
            self.library = load_library(self.settings.paths)
        except Exception:
            pass
        return {"ok": True, "saved": n, "files": written,
                "label": LANGUAGES.get(target, ("", target, ""))[1]}

    # ------------------------------------------------------------------
    # 手写补丁
    # ------------------------------------------------------------------
    def patch_templates(self) -> list[dict]:
        """返回手写补丁可用的模板列表（名称、说明与示例内容）。"""
        from .patch.service import get_templates
        return get_templates()

    def patch_validate(self, text: str, *, rel: str = "",
                       conflict_target: str = "") -> dict:
        """校验一段补丁文本：语法是否合法、会不会踩到覆盖风险。

        ``text`` 为补丁内容；``rel`` 是它在补丁模组里的落地相对路径（用于判断会覆盖
        哪些原文件）；``conflict_target`` 可关联到具体冲突做针对性检查。只读校验，
        不落盘。返回 ``{"ok": bool, "issues": [...]}``。
        """
        from .patch.service import validate_text
        return validate_text(text, indexes=self.indexes, rel=rel,
                             conflict_target=conflict_target)

    def patch_save_manual(self, *, title: str, rel: str, text: str,
                          description: str = "") -> dict:
        """把手写补丁保存成一个真正的模组。

        ``title`` 为模组名（留空时用「手写补丁」），``rel`` 为文件落地相对路径，
        ``text`` 为补丁内容，``description`` 写入 README。**有副作用**：先强制校验，
        通过后在 ``zzz_SMD_`` 补丁目录写盘并刷新模组库；校验不通过或内容 / 路径为空的
        情况**拒绝保存**，返回错误与问题列表。
        """
        from .ai.patchgen import PatchFile
        from .patch.service import save_patch
        if not text.strip():
            return {"ok": False, "error": "补丁内容为空"}
        if not rel.strip():
            return {"ok": False, "error": "请填写补丁文件的落地路径"}
        # 保存前一律校验
        v = self.patch_validate(text, rel=rel)
        if not v.get("ok"):
            return {"ok": False, "error": "校验未通过，未保存",
                    "issues": v.get("issues", [])}
        res = save_patch(
            self.settings.paths, title=title or "手写补丁",
            files=[PatchFile(rel=rel, text=text)],
            description=description, source="manual",
        )
        if res.get("ok"):
            self.log(f"手写补丁已保存：{res.get('name')}")
            try:
                self.library = load_library(self.settings.paths)
            except Exception:
                pass
        return res

    def patch_list(self) -> list[dict]:
        """列出所有 ``zzz_SMD_`` 补丁模组（AI 生成的与手写的都在内），含文件清单与
        来源信息。
        """
        from .patch.service import list_patches
        return list_patches(self.settings.paths, library=self.library)

    # ------------------------------------------------------------------
    # 「已被补丁解决」的索引
    # ------------------------------------------------------------------
    def _patch_coverage(self) -> dict:
        """扫描已安装的补丁模组，算出它们**覆盖了哪些东西**。

        为什么要算：用户打完补丁之后，冲突列表里那一条还在，
        下次看还是"这个问题没解决"，等于白做了一遍功。

        覆盖判据分两种，都要求**确实命中**才隐藏，宁可多显示也不误藏：

        * ``files`` —— 补丁里有同名文件。文件覆盖类冲突（F1）打补丁后
          就是靠"后加载的整份覆盖"解决的，路径相同即已处理。
        * `` defines`` —— 补丁文件里出现了同名顶层定义（``name = {``）。
          同名对象重定义（S1）打补丁后，补丁模组排在最末，
          它的那一份会成为最终生效版本，冲突即消解。

        结果按 (files, defines) 缓存，补丁目录变了才重算。
        """
        from .patch.service import PATCH_PREFIX, patch_root
        root = patch_root(self.settings.paths)
        try:
            stamp = (str(root), os.path.getmtime(long_path(root))
                     if is_dir(root) else 0.0)
        except OSError:
            stamp = (str(root), 0.0)
        cached = getattr(self, "_patch_cov_cache", None)
        if cached and cached[0] == stamp:
            return cached[1]

        files: set[str] = set()
        base_names: set[str] = set()
        defines: set[str] = set()
        loc_keys: set[str] = set()
        if is_dir(root):
            try:
                entries = sorted(root.iterdir())
            except OSError:
                entries = []
            for e in entries:
                if not e.name.startswith(PATCH_PREFIX) or not e.is_dir():
                    continue
                for dirpath, _dirs, fnames in os.walk(long_path(e)):
                    for fn in fnames:
                        if fn == "descriptor.mod" or fn.startswith("README") \
                                or fn == "SMD_MANIFEST.json":
                            continue
                        full = os.path.join(dirpath, fn)
                        rel = os.path.relpath(full, str(e)).replace("\\", "/")
                        files.add(rel.lower())
                        # **基础文件名**也要记：本地规则生成的补丁文件名是
                        # ``zzz_SMD_auto_merge_<原名>``，路径和冲突目标
                        # （``common/x/原名.txt``）对不上，
                        # 只比整条路径的话一条都匹配不到。
                        base_names.add(strip_patch_marks(fn))
                        if not fn.endswith((".txt", ".gui", ".yml", ".yaml")):
                            continue
                        try:
                            text = read_text(full)
                        except OSError:
                            continue
                        # 顶层定义：行首无缩进的 `name = {`
                        for nm in re.findall(
                                r"(?m)^([A-Za-z_][A-Za-z0-9_.\-]*)\s*=\s*\{",
                                text):
                            defines.add(nm.lower())
                        # 本地化键：` key:0 "文本"`（YAML 里必然有缩进）
                        for nm in re.findall(
                                r'(?m)^\s+([A-Za-z0-9_.\-]+)\s*:\s*\d*\s*"',
                                text):
                            loc_keys.add(nm.lower())
        cov = {"files": files, "base_names": base_names,
               "defines": defines, "loc_keys": loc_keys}
        self._patch_cov_cache = (stamp, cov)
        return cov

    def finding_is_patched(self, f: Any, cov: dict | None = None) -> bool:
        """这条冲突是不是已经被补丁模组处理掉了。

        几种命中方式，都要求**精确匹配**，宁可多显示也不误藏：

        * **整条路径**同名 —— 补丁直接重写了那个文件
        * **基础文件名**同名 —— 本地规则的补丁叫
          ``zzz_SMD_auto_merge_<原名>``，路径对不上但文件是同一个
        * **同名顶层定义** —— 对象重定义（S1）被补丁钉住 / 合并后即消失
        * **同名本地化键** —— 本地化冲突（L1）被补丁补齐后即消失

        ``target`` 在不同规则里含义不同（文件路径 / 对象名 / 本地化键），
        所以几种都试一遍；匹配不上就照常显示。
        """
        cov = cov if cov is not None else self._patch_coverage()
        target = str(getattr(f, "target", "") or "").strip()
        if not target:
            return False
        low = target.replace("\\", "/").lower()
        if low in cov["files"] or low in cov["defines"] \
                or low in cov["loc_keys"]:
            return True
        base = strip_patch_marks(low.rsplit("/", 1)[-1])
        if base and base in cov["base_names"]:
            # 只有"目标本身看起来就是文件"时才按文件名认，
            # 否则对象名撞上文件名的概率不低
            if "." in base:
                return True
        return False

    def mark_patched_findings(self) -> int:
        """保存补丁后刷新覆盖索引，并返回被隐藏的冲突条数。"""
        self._patch_cov_cache = None       # 强制重算
        if self.report is None:
            return 0
        cov = self._patch_coverage()
        return sum(1 for f in self.report.findings if self.finding_is_patched(f, cov))

    def patch_read(self, name: str, rel: str = "") -> dict:
        """读取补丁模组里的一个文件内容。

        ``rel`` 留空时自动取该补丁的第一个脚本文件；补丁不存在、路径越界或文件不存在
        时返回 ``{"error": ...}``。只读。
        """
        from .patch.service import read_patch
        return read_patch(self.settings.paths, name, rel)

    def patch_update(self, name: str, rel: str, text: str) -> dict:
        """覆盖补丁模组里的某个文件（编辑器保存用）。

        **有副作用**：先校验文本，通过后写盘；校验不通过则拒绝保存并返回问题列表。
        """
        from .patch.service import update_patch_file
        return update_patch_file(self.settings.paths, name, rel, text)

    def patch_delete(self, name: str) -> dict:
        """删除一个补丁模组（目录与描述符一起删掉，**不可撤销**）。

        删除成功会记一行日志。返回 ``{"ok": bool, ...}``。
        """
        from .patch.service import delete_patch
        res = delete_patch(self.settings.paths, name)
        if res.get("ok"):
            self.log(f"已删除补丁模组：{name}")
        return res

    def patch_file_source(self, mod_key: str, rel: str,
                          around: str = "") -> dict:
        """读取某个模组里某个文件的内容（供手写补丁时对照）。"""
        from .ai.prompt import _read_window
        if self.library is None:
            return {"error": "请先加载模组清单"}
        m = self.library.get(mod_key)
        if m is None:
            return {"error": "找不到该模组"}
        root = m.resolve_content_path()
        if not root:
            return {"error": "该模组文件不存在"}
        full = os.path.join(long_path(root), rel.replace("/", os.sep))
        if not is_file(full):
            return {"error": f"文件不存在：{rel}"}
        try:
            text, trunc, total, sent = _read_window(full, around)
        except OSError as exc:
            return {"error": str(exc)}
        return {"ok": True, "mod": m.name, "rel": rel, "text": text,
                "truncated": trunc, "total_lines": total, "sent_lines": sent}

    # ------------------------------------------------------------------
    # 手动排序
    # ------------------------------------------------------------------
    def reorder_move(self, key: str, delta: int = 0, *,
                     to_index: int | None = None) -> dict:
        """在「当前生效顺序」里移动一个模组。

        操作对象是 ``self.manual_order``（用户手动调过的顺序）；
        未初始化时从磁盘上的 playset 顺序复制一份。
        自动排序会整体覆盖它。
        """
        if self.library is None:
            return {"ok": False, "error": "请先加载模组清单"}
        order = self._ensure_manual_order()
        if key not in order:
            return {"ok": False, "error": "该模组不在当前顺序中"}
        i = order.index(key)
        if to_index is None:
            j = max(0, min(len(order) - 1, i + int(delta)))
        else:
            j = max(0, min(len(order) - 1, int(to_index)))
        if i == j:
            return {"ok": True, "order": order, "moved": False}
        order.pop(i)
        order.insert(j, key)
        self.manual_order = order
        self._manual_dirty = True
        return {"ok": True, "order": order, "moved": True,
                "from": i, "to": j}

    def _ensure_manual_order(self) -> list[str]:
        cur = getattr(self, "manual_order", None)
        if cur:
            return list(cur)
        if self.solution is not None:
            base = list(self.solution.order)
        elif self.library is not None:
            base = [m.key for m in self.library.load_order()]
        else:
            base = []
        self.manual_order = base
        return list(base)

    def reorder_reset(self) -> dict:
        """丢弃手动调整，回到自动排序结果（或磁盘顺序）。"""
        self.manual_order = []
        self._manual_dirty = False
        if self.solution is not None:
            base = list(self.solution.order)
        elif self.library is not None:
            base = [m.key for m in self.library.load_order()]
        else:
            base = []
        self.manual_order = base
        return {"ok": True, "order": base}

    def get_order_view(self) -> dict:
        """给「排序」页用的完整视图：当前顺序 + 自动排序建议 + 差异。"""
        auto: list[str] = []
        if self.solution is not None:
            auto = [k for k in self.solution.order]
        elif self.library is not None:
            auto = [m.key for m in self.library.load_order()]
        manual = self._ensure_manual_order()
        names = {}
        disables = {}
        if self.library is not None:
            for m in self.library.mods.values():
                names[m.key] = m.name
            for m in self.library.mods.values():
                if not m.resolve_content_path():
                    disables[m.key] = "文件缺失"
        if self.solution is not None:
            for p in self.solution.plans:
                if p.action == "disable":
                    disables[p.key] = p.reason

        rows: list[dict] = []
        auto_index = {k: i for i, k in enumerate(auto)}
        for i, k in enumerate(manual):
            m = self.library.get(k) if self.library else None
            rows.append({
                "index": i, "key": k, "name": names.get(k, k),
                "auto_index": auto_index.get(k, -1),
                "is_translation": bool(m and m.is_translation),
                "is_base": bool(m and m.is_library_or_framework),
                "is_ui": bool(m and m.is_ui),
                "problem": disables.get(k, ""),
                "size": (self.indexes.indexes[k].total_bytes
                         if k in self.indexes.indexes else 0),
            })
        # 被自动排序排除的（建议禁用且已确认关闭）
        excluded = [k for k in auto if k not in manual]
        changed = sum(1 for r in rows
                      if r["auto_index"] >= 0 and r["auto_index"] != r["index"])
        return {
            "rows": rows,
            "auto_order": auto,
            "manual_order": manual,
            "dirty": bool(getattr(self, "_manual_dirty", False)),
            "changed": changed,
            "total": len(rows),
            "excluded": [{"key": k, "name": names.get(k, k)} for k in excluded],
            "has_solution": self.solution is not None,
        }

    def reorder_apply(self, order: list[str] | None = None) -> dict:
        """把当前手动顺序写入群星（不改动模组启用状态）。"""
        from .apply import apply_solution
        from .solver import Solution
        if self.library is None:
            return {"ok": False, "error": "请先加载模组清单"}
        target = list(order) if order else self._ensure_manual_order()
        if not target:
            return {"ok": False, "error": "顺序为空"}

        if self.solution is not None:
            sol = self.solution
            sol.order = target
        else:
            sol = Solution(order=target)
        res = apply_solution(
            self.settings.paths, self.library, sol,
            create_patches=False,      # 手动排序不生成补丁
            progress=self._task_progress)
        self.last_apply = res
        self.manual_order = target
        self._manual_dirty = False
        if res.ok:
            self.log(f"已按手动顺序写入 {len(target)} 个模组")
        else:
            self.log(f"写入手动顺序时出现 {len(res.errors)} 个问题", "warn")
        try:
            self.library = load_library(self.settings.paths)
        except Exception:
            pass
        return res.to_dict()

    def start_reorder_apply(self, order: list[str] | None = None) -> bool:
        """在后台线程把手动加载顺序写入群星。

        ``order`` 为完整顺序列表，留空则用当前手动顺序。写入**只调整加载顺序，
        不会自动开启或关闭任何模组**（:meth:`reorder_apply` 也只改顺序、不生成补丁）。
        返回是否成功排队；完成后通过 ``reorder_done`` 事件推送结果与模组列表。
        """
        def job() -> dict:
            self._task_progress("写入手动加载顺序", 0.1)
            return self.reorder_apply(order)

        def done(res: Any, err: str) -> None:
            self.emit("reorder_done", {"result": res, "error": err,
                                       "mods": self.mods_list()})
        return self.run_async("应用手动排序", job, done)

    # ------------------------------------------------------------------
    # 模组启用开关
    # ------------------------------------------------------------------
    def start_checksum_scan(self) -> bool:
        """Check installed checksum rules in a cancellable background task."""
        from .checksum import scan_checksum

        def job() -> dict:
            self._checksum_report = None
            if self.library is None:
                self.load_mods()
            library = self.library
            game_dir = self.settings.paths.game_dir
            # 只扫**可见模组**，别用 library.all()。
            #
            # all() 里含 18 个「幽灵记录」——已退订但启动器数据库还留着的
            # 空条目，磁盘上根本没有内容目录。它们必然被标成 unknown，
            # 而且不可能影响校验（游戏压根不加载它们），
            # 混进结果只是噪音，还会把"未知"数量撑得很大，让人误判。
            # 实测：all() 94 个里有 18 个是幽灵。
            mods = self.visible_mods()
            result = scan_checksum(
                mods, game_dir, should_stop=lambda: self.cancelled,
                progress=lambda name, value: self._task_progress(name, value))
            if library is not self.library or game_dir != self.settings.paths.game_dir:
                return {"ok": False, "error": "清单或游戏路径已变化，请重新排查"}
            if result.get("ok"):
                self._checksum_report = result
            return result

        def done(result: Any, error: str) -> None:
            self.emit("checksum_done", {"result": result, "error": error})

        return self.run_async("联机校验排查", job, done)

    # ------------------------------------------------------------------
    # 影响成就的模组
    # ------------------------------------------------------------------
    def _achievement_risks(self) -> list[dict]:
        """从校验报告里挑出「会影响成就」的模组（内部用）。

        判断依据和「联机校验排查」**同一套** —— 都读游戏自带的
        ``checksum_manifest.txt``（本机实测是 6 条规则：
        ``common`` 的 .txt/.shader/.csv、``events`` 的 .txt、
        ``map`` 的 .shader/.txt）。

        为什么可以复用：改了这些目录下的文件，游戏就会把存档标为
        「已修改」，成就随之失效 —— 和联机校验失效是同一个原因。

        **注意 ``localisation/`` 不在规则里**，所以纯汉化不影响成就。
        这是实测确认的，不是推测。
        """
        report = self._checksum_report
        if not report or report.get("game_dir") != self.settings.paths.game_dir:
            return []
        # 排除幽灵记录：它们磁盘上没有内容，不可能影响成就
        alive = {m.key for m in self.visible_mods()}
        return [r for r in (report.get("rows") or [])
                if r.get("status") == "risk" and r.get("key") in alive]

    def achievement_scan(self) -> bool:
        """后台任务：扫描哪些模组会影响成就。"""
        from .checksum import scan_checksum

        def job() -> dict:
            self._checksum_report = None
            if self.library is None:
                self.load_mods()
            library = self.library
            game_dir = self.settings.paths.game_dir
            mods = self.visible_mods()
            result = scan_checksum(
                mods, game_dir, should_stop=lambda: self.cancelled,
                progress=lambda name, value: self._task_progress(
                    f"检查成就影响：{name}", value))
            if library is not self.library \
                    or game_dir != self.settings.paths.game_dir:
                return {"ok": False, "error": "清单或游戏路径已变化，请重新排查"}
            if not result.get("ok"):
                return result
            self._checksum_report = result
            return {"ok": True, "rows": self.achievement_risk_rows(),
                    "enabled": len(self.visible_mods())}

        def done(result: Any, error: str) -> None:
            self.emit("achievement_done", {"result": result, "error": error})

        return self.run_async("扫描成就影响", job, done)

    def achievement_risk_rows(self) -> list[dict]:
        """会影响成就的模组清单（供界面显示）。

        每条带「命中多少项」和「前几条依据」—— 只给结论不给依据的话，
        用户没法判断这个提醒靠不靠谱，只能选择信或不信。
        """
        enabled = {r["key"] for r in self.switch_state()["rows"] if r["enabled"]}
        out = []
        for r in self._achievement_risks():
            out.append({
                "key": r["key"], "name": r["name"],
                "hits": r["hits"], "files": r["files"],
                "evidence": list(r.get("evidence") or [])[:5],
                "enabled": r["key"] in enabled,
            })
        out.sort(key=lambda x: (-x["hits"], x["name"]))
        return out

    def stage_achievement_risks(self, keys: list[str] | None = None) -> dict:
        """把会破坏成就的模组**暂存**为关闭。

        ``keys`` 为空则处理全部「当前启用」的命中项；给了就只处理这些。

        和 :meth:`stage_checksum_risks` 一样**只进暂存区、不写磁盘** ——
        用户还得点「应用方案」或「写入群星」才生效，写入前有备份。
        「模组开关由用户决定」这条约束在这里同样成立。
        """
        if self.task.running:
            return {"ok": False, "error": "请等待当前任务完成"}
        rows = self.achievement_risk_rows()
        if not rows:
            return {"ok": False,
                    "error": "还没有扫描结果。请先点「扫描成就影响」"}
        want = (set(keys) if keys is not None
                else {r["key"] for r in rows if r["enabled"]})
        picked = [r["key"] for r in rows if r["key"] in want and r["enabled"]]
        if not picked:
            return {"ok": False, "error": "没有可暂存的模组（选中的都已关闭）"}
        res = self.set_mod_switches(picked, False)
        if res.get("ok"):
            self.log(f"为保住成就，已暂存关闭 {len(picked)} 个影响成就的模组"
                     f"（尚未写入，需点「应用方案」）")
        return {**res, "count": len(picked),
                "note": "只是暂存，点「应用方案」或「写入群星」才生效。"
                        "写入前会自动备份，可在备份页回滚。"}

    def stage_checksum_risks(self) -> dict:
        """Stage only currently enabled suspects through the existing switch plan."""
        report = self._checksum_report
        if self.task.running:
            return {"ok": False, "error": "请等待当前任务完成"}
        if not report or report["game_dir"] != self.settings.paths.game_dir:
            return {"ok": False, "error": "请先重新执行联机校验排查"}
        state = self.switch_state()
        enabled = {row["key"] for row in state["rows"] if row["enabled"]}
        keys = [row["key"] for row in report["rows"]
                if row["status"] == "risk" and row["key"] in enabled]
        return self.set_mod_switches(keys, False)

    def switch_state(self) -> dict:
        """当前模组开关的暂存状态（供模组列表渲染滑块）。

        改动只进暂存区，点「应用」才写盘——避免手滑就把几十个模组关掉。
        """
        from .manage import SwitchPlan
        if getattr(self, "_switch_plan", None) is None:
            self._switch_plan = SwitchPlan()
        plan = self._switch_plan
        lib = self.library
        rows = []
        if lib is not None:
            # is_loadable() 要访问磁盘，缓存一次，别在循环里反复 stat
            mods = self.visible_mods()
            loadable = {m.key: m.is_loadable() for m in mods}
            for m in mods:
                want = plan.get(m.key, m.enabled_in_playset)
                rows.append({
                    "key": m.key, "enabled": want,
                    "actual": m.enabled_in_playset,
                    "changed": want != m.enabled_in_playset,
                    "loadable": loadable[m.key],
                })
            summary = plan.summary(lib)
            bad = [m.name for m in mods if not loadable[m.key]]
            summary["unloadable"] = len(bad)
            summary["unloadable_names"] = bad[:10]
            summary["hidden_ghosts"] = sum(
                1 for m in lib.all() if m.is_ghost())
        else:
            summary = {}
        return {"rows": rows, "summary": summary,
                "pending": len(plan.changes(lib)) if lib is not None else 0}

    def set_mod_switch(self, key: str, enabled: bool) -> dict:
        """暂存一个模组的开关（不写盘）。

        **磁盘上没有内容的模组会被拒绝启用**——那种条目写进配置也没用，
        游戏读不到任何文件。允许它「假装启用」只会误导用户，
        所以这里直接拒绝并说明原因。关掉它（清理过期记录）是允许的。
        """
        from .manage import SwitchPlan
        if self.library is None:
            return {"ok": False, "error": "请先加载模组清单"}
        if getattr(self, "_switch_plan", None) is None:
            self._switch_plan = SwitchPlan()
        m = self.library.get(key)
        if m is None:
            return {"ok": False, "error": "找不到该模组"}
        if enabled and not m.is_loadable():
            return {"ok": False, "state": self.switch_state(),
                    "error": f"「{m.name}」的内容不在磁盘上，"
                             f"启用也不会有任何效果。"
                             f"请先在 Steam 创意工坊重新订阅，"
                             f"再点「刷新清单」。"}
        self._switch_plan.set(key, enabled)
        return {"ok": True, "state": self.switch_state()}

    def set_mod_switches(self, keys: list[str], enabled: bool) -> dict:
        """批量暂存指定模组的开关。

        启用时**跳过磁盘上没内容的模组**（同 :meth:`set_mod_switch`），
        并在结果里报告跳过了几个，避免「批量启用」造成虚假的成功感。
        """
        from .manage import SwitchPlan
        if self.library is None:
            return {"ok": False, "error": "请先加载模组清单"}
        if getattr(self, "_switch_plan", None) is None:
            self._switch_plan = SwitchPlan()
        n = 0
        skipped: list[str] = []
        for k in keys or []:
            m = self.library.get(k)
            if m is None:
                continue
            if enabled and not m.is_loadable():
                skipped.append(m.name)
                continue
            self._switch_plan.set(k, enabled)
            n += 1
        msg = f"暂存 {n} 个模组开关为「{'启用' if enabled else '关闭'}」"
        if skipped:
            msg += f"，跳过 {len(skipped)} 个磁盘无内容的"
        self.log(msg)
        return {"ok": True, "count": n, "skipped": len(skipped),
                "skipped_names": skipped[:10], "state": self.switch_state()}

    def switch_bulk(self, mode: str) -> dict:
        """整体批量操作：全部启用 / 全部关闭 / 反选 / 重置。

        「全部启用」「反选」都会**自动跳过磁盘上没有内容的模组**，
        否则用户会以为全都开上了，实际有十几个根本没加载。
        """
        from .manage import SwitchPlan
        if self.library is None:
            return {"ok": False, "error": "请先加载模组清单"}
        if getattr(self, "_switch_plan", None) is None:
            self._switch_plan = SwitchPlan()
        plan = self._switch_plan
        # 只对**界面上看得见**的模组批量操作。
        # 若把隐藏的幽灵记录也一起改了，用户会看到「有 N 处改动」
        # 却怎么数都对不上——所见即所得更重要。
        mods = self.visible_mods()
        skipped: list[str] = []

        def want_enable(m: Any, target: bool) -> bool:
            """把目标状态收敛到「实际能做到的」状态。"""
            if target and not m.is_loadable():
                skipped.append(m.name)
                return False
            return target

        if mode == "enable_all":
            for m in mods:
                plan.set(m.key, want_enable(m, True))
        elif mode == "disable_all":
            for m in mods:
                plan.set(m.key, False)
        elif mode == "invert":
            for m in mods:
                cur = plan.get(m.key, m.enabled_in_playset)
                plan.set(m.key, want_enable(m, not cur))
        elif mode == "reset":
            plan.clear()
        else:
            return {"ok": False, "error": f"未知操作：{mode}"}
        res = {"ok": True, "state": self.switch_state()}
        if skipped:
            res["skipped"] = len(skipped)
            res["skipped_names"] = skipped[:10]
        return res

    def preview_mod_switches(self) -> dict:
        """预览即将写入的开关变化（写盘前给用户确认）。"""
        from .manage import SwitchPlan
        if self.library is None:
            return {"ok": False, "error": "请先加载模组清单"}
        plan = getattr(self, "_switch_plan", None) or SwitchPlan()
        s = plan.summary(self.library)
        s["ok"] = True
        return s

    def apply_mod_switches(self, *, sync_launcher: bool = True) -> dict:
        """把暂存的模组开关真正写入群星。

        写 ``dlc_load.json``（游戏实际读取的清单），并尽量同步
        Paradox 启动器的数据库，这样下次打开启动器时勾选状态也是对的。
        """
        from .apply import launcher_running, make_backup
        from .manage import SwitchPlan
        from .manage import apply_mod_switches as _apply
        if self.library is None:
            return {"ok": False, "error": "请先加载模组清单"}
        plan = getattr(self, "_switch_plan", None) or SwitchPlan()
        if not plan.changes(self.library):
            return {"ok": True, "changed": 0, "notes": ["没有需要写入的变化"]}

        try:
            backup = make_backup(self.settings.paths, "modswitch")
        except OSError:
            backup = ""
        running = launcher_running()
        res = _apply(self.settings.paths, self.library, plan,
                     sync_launcher=sync_launcher,
                     launcher_running=bool(running))
        res["backup"] = backup
        if res.get("ok"):
            self.log(f"模组开关已写入：启用 {len(res.get('added') or [])} 个 / "
                     f"关闭 {len(res.get('removed') or [])} 个")
            self._switch_plan = SwitchPlan()
            try:
                self.library = load_library(self.settings.paths)
            except Exception:
                pass
            res["state"] = self.switch_state()
        else:
            self.log(f"写入模组开关失败：{res.get('error')}", "error")
        return res

    def start_apply_mod_switches(self, *, sync_launcher: bool = True) -> bool:
        """后台任务：写入模组开关，完成后推送 ``switch_done`` 事件。"""
        def job() -> dict:
            self._task_progress("写入模组开关", 0.2)
            return self.apply_mod_switches(sync_launcher=sync_launcher)

        def done(res: Any, err: str) -> None:
            self.emit("switch_done", {"result": res, "error": err,
                                      "mods": self.mods_list()})
        return self.run_async("写入模组开关", job, done)

    # ------------------------------------------------------------------
    # DLC 管理
    # ------------------------------------------------------------------
    def dlc_state(self) -> dict:
        """DLC 概览 + 当前暂存的开关改动。

        只有**与磁盘状态不同**的改动才算「待写入」——
        把开关拨回原位会消除该项改动，而不是留一条无意义的记录。
        """
        from .manage import dlc_overview, report_dlc_hint
        ov = dlc_overview(self.settings.paths)
        overrides = getattr(self, "_dlc_overrides", None) or {}
        real_changes: dict[str, bool] = {}
        for d in ov["dlcs"]:
            original = d["enabled"]          # 来自 dlc_load.json
            if d["key"] in overrides:
                want = bool(overrides[d["key"]])
                d["original"] = original
                d["changed"] = (want != original)
                d["enabled"] = want
                if d["changed"]:
                    real_changes[d["key"]] = want
            else:
                d["original"] = original
                d["changed"] = False
        # 丢弃「拨回原位」的记录，保持暂存区干净
        self._dlc_overrides = real_changes

        by_cat: dict[str, list[dict]] = {}
        for d in ov["dlcs"]:
            by_cat.setdefault(d["category_label"], []).append(d)
        ov["by_category"] = by_cat
        ov["enabled"] = sum(1 for d in ov["dlcs"] if d["enabled"])
        ov["disabled"] = sum(1 for d in ov["dlcs"] if not d["enabled"])
        ov["pending"] = len(real_changes)
        ov["to_enable"] = sum(1 for v in real_changes.values() if v)
        ov["to_disable"] = sum(1 for v in real_changes.values() if not v)
        ov["hint"] = report_dlc_hint(self.settings.paths)
        return ov

    def set_dlc_switch(self, key: str, enabled: bool) -> dict:
        """暂存一个 DLC 的开关（不写盘）。

        拨回原位会自动清除该条改动。
        """
        if getattr(self, "_dlc_overrides", None) is None:
            self._dlc_overrides = {}
        self._dlc_overrides[str(key)] = bool(enabled)
        return {"ok": True, "state": self.dlc_state()}

    def set_dlc_switches(self, keys: list[str], enabled: bool) -> dict:
        """批量暂存 DLC 开关。"""
        if getattr(self, "_dlc_overrides", None) is None:
            self._dlc_overrides = {}
        for k in keys or []:
            self._dlc_overrides[str(k)] = bool(enabled)
        return {"ok": True, "count": len(keys or []), "state": self.dlc_state()}

    def dlc_bulk(self, mode: str) -> dict:
        """批量操作：全部启用/关闭/重置，或按类别 ``enable:<类别>``。"""
        from .manage import read_dlc_list
        if getattr(self, "_dlc_overrides", None) is None:
            self._dlc_overrides = {}
        dlcs = read_dlc_list(self.settings.paths)
        if mode == "enable_all":
            for d in dlcs:
                self._dlc_overrides[d.key] = True
        elif mode == "disable_all":
            for d in dlcs:
                self._dlc_overrides[d.key] = False
        elif mode == "reset":
            self._dlc_overrides = {}
        elif mode.startswith(("enable:", "disable:")):
            want = mode.startswith("enable:")
            cat = mode.split(":", 1)[1]
            for d in dlcs:
                if d.category_label == cat:
                    self._dlc_overrides[d.key] = want
        else:
            return {"ok": False, "error": f"未知操作：{mode}"}
        return {"ok": True, "state": self.dlc_state()}

    def apply_dlc_switches(self) -> dict:
        """把 DLC 开关写入 ``dlc_load.json``。"""
        from .apply import make_backup
        from .manage import apply_dlc_switches as _apply
        from .manage import read_dlc_list
        dlcs = read_dlc_list(self.settings.paths)
        if not dlcs:
            return {"ok": False,
                    "error": "没有读到任何 DLC，请检查游戏目录是否正确"}
        overrides = getattr(self, "_dlc_overrides", None) or {}
        if not overrides:
            return {"ok": True, "changed": 0, "notes": ["没有需要写入的变化"]}

        # 目标禁用集合 = 当前禁用 + 本次改动
        target_disabled = {d.key for d in dlcs if not d.enabled}
        for k, want in overrides.items():
            if want:
                target_disabled.discard(k)
            else:
                target_disabled.add(k)

        try:
            backup = make_backup(self.settings.paths, "dlc")
        except OSError:
            backup = ""
        res = _apply(self.settings.paths, dlcs, target_disabled)
        res["backup"] = backup
        if res.get("ok"):
            self._dlc_overrides = {}
            res["state"] = self.dlc_state()
            self.log(f"DLC 开关已写入：启用 {res.get('enabled')} 个 / "
                     f"关闭 {res.get('disabled')} 个")
        else:
            self.log(f"写入 DLC 开关失败：{res.get('error')}", "error")
        return res

    def start_apply_dlc(self) -> bool:
        """后台任务：写入 DLC 开关，完成后推送 ``dlc_done`` 事件。"""
        def job() -> dict:
            self._task_progress("写入 DLC 开关", 0.2)
            return self.apply_dlc_switches()

        def done(res: Any, err: str) -> None:
            self.emit("dlc_done", {"result": res, "error": err})
        return self.run_async("写入 DLC 开关", job, done)

    def open_dlc_folder(self) -> dict:
        """在资源管理器里打开游戏的 ``dlc`` 目录。"""
        ov = self.dlc_state()
        if not ov.get("dlc_dir"):
            return {"ok": False, "error": "未找到 DLC 目录"}
        return self.open_path(ov["dlc_dir"])

    # ------------------------------------------------------------------
    # 界面主题
    # ------------------------------------------------------------------
    def themes_payload(self) -> dict:
        """主题列表 + 当前主题 + 令牌清单（供界面选择器与编辑器）。"""
        from . import theme as th
        cur = getattr(self.settings, "theme", "") or th.DEFAULT_THEME
        # 老配置里的 "dark" 是主题系统之前的占位值，迁移到新的默认主题
        if cur == "dark" and th.DEFAULT_THEME != "dark":
            cur = th.DEFAULT_THEME
            self.settings.theme = cur
            try:
                self.settings.save()
            except OSError:
                pass
        data = th.theme_for_ui(cur)
        data["themes"] = th.list_themes()
        data["current"] = cur
        return data

    def ai_make_theme(self, prompt: str, *, name: str = "") -> dict:
        """用一句提示词让 AI 生成一套界面主题，并直接保存。

        **只让 AI 改颜色，形态类令牌沿用当前主题。** 理由：
        形态（圆角、切角、密度、字体）决定了界面的"骨架"，
        让模型一起改很容易生成出排版崩坏的主题；
        颜色才是"换个外观"的主体诉求。想调形态仍可在主题编辑器里改。

        生成结果会走 ``save_user_theme`` 的完整校验——
        格式不合格就拒绝保存，不会写进一份坏主题。
        颜色**能用但看不清**（对比度不足）不算不合格：那类会被自动修正，
        并把"改了哪几个令牌"带回前端说明。
        """
        from . import theme as th
        from .ai import analyze as A

        prompt = (prompt or "").strip()
        if not prompt:
            return {"ok": False, "error": "请先描述想要的外观"}

        cur_id = getattr(self.settings, "theme", "") or th.DEFAULT_THEME
        cur = th.get_theme(cur_id)
        cur_tokens = dict(cur.get("tokens") or {})
        cur_flags = dict(cur.get("flags") or {})

        res = A.make_theme(self.settings, prompt, base=cur_tokens,
                           should_stop=lambda: self.cancelled)
        if not res.get("ok"):
            return res

        tid = _slug_theme_id(name or res.get("name") or "ai")
        theme = {
            "id": tid,
            "name": (name or res.get("name") or "AI 主题")[:20],
            "desc": f"由 AI 按提示词生成：{prompt[:60]}",
            "author": "熵光矩阵 · AI",
            "base": cur_id,
            "tokens": res["tokens"],
            # 形态沿用当前主题，只换颜色
            "flags": cur_flags,
        }
        saved = th.save_user_theme(theme)
        if saved.get("ok"):
            self.settings.theme = saved["theme"]["id"]
            self.settings.save()
            self.log(f"AI 生成了界面主题「{saved['theme']['name']}」")
            # 把"自动调了对比度"这类提示带回前端。
            # 不带上它，用户只会看到一个颜色变了、却不知道有字被改过 ——
            # 那属于"程序知道但不告诉我"。
            saved["issues"] = res.get("issues") or []
        return saved

    def save_theme(self, payload: dict) -> dict:
        """保存自制主题。校验失败会拒绝写入并返回问题清单。"""
        from . import theme as th
        res = th.save_user_theme(payload or {})
        if res.get("ok"):
            self.settings.theme = res["theme"]["id"]
            self.settings.save()
            self.log(f"已保存界面主题：{res['theme']['name']}")
        return res

    def delete_theme(self, tid: str) -> dict:
        """删除自制主题；内置主题会被拒绝。"""
        from . import theme as th
        res = th.delete_user_theme(tid)
        if res.get("ok") and getattr(self.settings, "theme", "") == tid:
            self.settings.theme = th.DEFAULT_THEME
            self.settings.save()
        return res

    def export_theme(self, tid: str = "") -> dict:
        """导出主题为 JSON 文件，便于分享给他人导入。"""
        from . import theme as th
        target = tid or getattr(self.settings, "theme", "") or th.DEFAULT_THEME
        res = th.export_theme(target)
        if res.get("ok"):
            self.log(f"已导出主题 {target} → {res.get('path')}")
        return res

    def import_theme_dialog(self) -> dict:
        """弹出系统文件对话框选一份主题 JSON 导入。"""
        from . import theme as th
        path = ""
        try:
            import tkinter as tk
            from tkinter import filedialog
            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            path = filedialog.askopenfilename(
                title="选择主题文件（JSON）",
                filetypes=[("主题文件", "*.json"), ("所有文件", "*.*")],
                initialdir=str(th.themes_dir()),
            )
            root.destroy()
        except Exception as exc:
            return {"ok": False, "error": f"无法打开文件对话框：{exc}"}
        if not path:
            return {"ok": True, "cancelled": True}
        res = th.import_theme(path)
        if res.get("ok"):
            self.settings.theme = res["theme"]["id"]
            self.settings.save()
            self.log(f"已导入主题：{res['theme']['name']}（来自 {path}）")
        return res

    def export_builtin_themes(self) -> dict:
        """把内置主题导出为可编辑文件，便于玩家照着改。"""
        from . import theme as th
        n = th.reset_builtin_files()
        return {"ok": True, "count": n, "dir": str(th.themes_dir())}

    # ------------------------------------------------------------------
    # 本地自动补丁（不联网）
    # ------------------------------------------------------------------
    def generate_auto_patches(self, kinds: list[str] | None = None) -> dict:
        """用内置规则在本地生成补丁草稿（**不调用任何 AI**）。

        这一步只生成草稿，不写盘；确认后再走 :meth:`apply_auto_patches`。
        """
        from .patch.local import generate_local_patches, summarize
        if self.report is None:
            return {"ok": False,
                    "error": "还没有检测结果。请先点「开始检测」。"}
        drafts = generate_local_patches(self.report, self.library,
                                       self.indexes, kinds=kinds)
        self._auto_drafts = drafts
        # 同时登记进统一账本，这样"待保存补丁"里也能看到它们
        self._ledger_add_local(drafts)
        s = summarize(drafts)
        self.log(f"本地生成 {s['total']} 份补丁草稿"
                 f"（{'、'.join(f'{k} {v}' for k, v in s['by_rule'].items())}）")
        return {"ok": True, "summary": s,
                "drafts": [d.to_dict() for d in drafts]}

    def apply_auto_patches(self, indexes_wanted: list[int] | None = None
                           ) -> dict:
        """把选中的草稿写成一个补丁模组。

        ``indexes_wanted`` 是要写入的草稿下标；留空表示全部。
        """
        from .ai.patchgen import PatchFile
        from .patch.service import save_patch
        drafts = getattr(self, "_auto_drafts", None) or []
        if not drafts:
            return {"ok": False, "error": "没有可写入的草稿，请先生成。"}
        want = (set(indexes_wanted) if indexes_wanted is not None
                else set(range(len(drafts))))
        picked = [d for i, d in enumerate(drafts) if i in want]
        if not picked:
            return {"ok": False, "error": "没有选中任何补丁。"}

        files = [PatchFile(rel=d.rel_path, text=d.body) for d in picked]
        detail = "\n".join(
            f"- [{d.rule}] {d.title}（{d.lines} 行，置信度 {d.confidence}）\n"
            f"  理由：{d.reason}" for d in picked)
        res = save_patch(
            self.settings.paths,
            title=f"自动补丁 {len(picked)} 项",
            files=files,
            description=("由「熵光矩阵」的本地规则自动生成，"
                         "生成过程未联网、未使用 AI。\n"
                         "每条补丁的依据与影响面见下；应用前请自行复核。"),
            source="auto",
            extra_readme=detail,
        )
        if res.get("ok"):
            self.log(f"已写入自动补丁模组：{res.get('name')}")
            self._auto_drafts = []
            try:
                self.library = load_library(self.settings.paths)
            except Exception:
                pass
        return res

    def guess_patch_scope(self) -> dict:
        """按问题类型统计能生成多少补丁，供界面显示可选项。"""
        from .patch.local import RULES, RULE_LABEL
        if self.report is None:
            return {"ok": False, "error": "还没有检测结果"}
        from collections import Counter
        c = Counter(f.kind.upper() for f in self.report.findings)
        out = []
        for kind, fn in RULES.items():
            n = c.get(kind, 0)
            if not n:
                continue
            out.append({"kind": kind, "count": n,
                        "label": RULE_LABEL.get(
                            getattr(fn, "__name__", "").replace("rule_", ""),
                            kind)})
        return {"ok": True, "kinds": out}

    # ------------------------------------------------------------------
    # 一键翻译
    # ------------------------------------------------------------------
    def translation_plan(self, target: str = "zh", limit: int = 0,
                         mod_keys: list[str] | None = None) -> dict:
        """统计要翻译多少条（不发送任何内容，纯本地统计）。

        带上语言清单，这样界面第一次调用就拿到下拉框选项，
        不必再多跑一次完整扫描。
        """
        from .translate import collect_entries, language_choices
        if self.library is None:
            return {"ok": False, "error": "请先加载模组清单"}
        plan = collect_entries(self.library, target, limit=limit,
                               mod_keys=mod_keys)
        d = plan.to_dict()
        d["ok"] = True
        d["languages"] = language_choices()
        d["current_language"] = getattr(self.settings, "language", "zh")
        return d

    def start_translate_all(self, target: str = "zh", limit: int = 0,
                            mod_keys: list[str] | None = None) -> bool:
        """后台任务：一键翻译全部（或指定）模组。"""
        from .translate import (collect_entries, translate_entries,
                                write_translation_files)

        def job() -> dict:
            self._task_progress("统计待翻译文本", 0.02)
            plan = collect_entries(
                self.library, target, limit=limit, mod_keys=mod_keys,
                progress=lambda m, p: self._task_progress(m, p * 0.4),
                should_stop=lambda: self.cancelled)
            if not plan.entries:
                return {"ok": False,
                        "error": "没有找到需要翻译的文本"
                                 "（可能都已经有译文了）"}
            self._task_progress(
                f"翻译 {len(plan.entries)} 条（共 {plan.chars} 字符）", 0.42)
            res = translate_entries(
                plan.entries, target,
                progress=lambda m, p: self._task_progress(m, 0.42 + p * 0.5),
                should_stop=lambda: self.cancelled)
            if not res.get("ok"):
                return res
            self._task_progress("写入本地化文件", 0.94)
            out_root = self._translation_out_root()
            written = write_translation_files(
                self.library, res["translated"], target, out_root=out_root)
            res["written"] = written
            res["mods_written"] = sum(1 for w in written if w.get("ok"))
            res["target"] = target
            res["total_planned"] = len(plan.entries)
            return res

        def done(res: Any, err: str) -> None:
            self.emit("translate_done", {"result": res, "error": err})
        return self.run_async("一键翻译", job, done)

    def _translation_out_root(self) -> Path:
        """译文写到哪里。

        默认做成一个**独立的汉化补丁模组**，而不是塞进每个原模组目录：
        原模组会被 Steam 更新覆盖，写进去的译文会丢；而且改别人的模组
        目录不礼貌。做成独立模组也方便一键卸载。
        """
        from .patch.service import PATCH_PREFIX, patch_root
        d = patch_root(self.settings.paths) / f"{PATCH_PREFIX}translation"
        d.mkdir(parents=True, exist_ok=True)
        # 让游戏认得它：写描述符
        try:
            from .paths import write_text
            write_text(d / "descriptor.mod",
                       'name="SMD 机器翻译补丁"\nversion="1.0"\n'
                       'supported_version="v4.*"\ntags={\n\t"Translation"\n}\n')
            mf = patch_root(self.settings.paths) / \
                f"{PATCH_PREFIX}translation.mod"
            write_text(mf, 'name="SMD 机器翻译补丁"\nversion="1.0"\n'
                           'supported_version="v4.*"\n'
                           f'path="{str(d).replace(chr(92), "/")}"\n'
                           'tags={\n\t"Translation"\n}\n')
        except OSError:
            pass
        return d

    def verify_translation(self, target: str = "zh") -> dict:
        """翻译后自检：统计有多少键已经有了目标语言译文。"""
        from .translate import LANGUAGES, collect_translated_keys
        if self.library is None:
            return {"ok": False, "error": "请先加载模组清单"}
        suffix = LANGUAGES.get(target, LANGUAGES["zh"])[0]
        keys = collect_translated_keys(self.library, target)
        return {"ok": True, "suffix": suffix, "keys": len(keys),
                "label": LANGUAGES.get(target, ("", target, ""))[1]}

    # ------------------------------------------------------------------
    # 界面语言
    # ------------------------------------------------------------------
    def set_language(self, lang: str) -> dict:
        """切换软件界面语言。"""
        self.settings.language = (lang or "zh").strip().lower()
        self.settings.save()
        return {"ok": True, "language": self.settings.language}

    # ------------------------------------------------------------------
    # 杂项
    # ------------------------------------------------------------------
    def open_path(self, path: str) -> dict:
        """在资源管理器里打开某个文件所在位置或目录（走系统 ``explorer``）。

        路径不存在或系统调用失败时返回 ``{"ok": False, "message": ...}``。
        """
        ok, msg = open_in_explorer(path)
        return {"ok": ok, "message": msg}

    def open_url(self, url: str) -> dict:
        """用系统默认程序打开一个网址（帮助链接、创意工坊页面等）。

        失败时返回 ``{"ok": False, "message": ...}``。
        """
        ok, msg = open_url(url)
        return {"ok": ok, "message": msg}

    def open_mod_workshop(self, key: str) -> dict:
        """打开某个模组的 Steam 创意工坊详情页。

        本地模组没有工坊 id，此时返回提示而不是打开一个空页面。
        """
        if self.library is None:
            return {"ok": False, "error": "请先加载模组清单"}
        m = self.library.get(key)
        if m is None:
            return {"ok": False, "error": "找不到该模组"}
        url = m.workshop_url()
        if not url:
            return {"ok": False,
                    "error": f"「{m.name}」是本地模组，没有创意工坊页面"}
        ok, msg = open_url(url)
        return {"ok": ok, "url": url, "message": msg}

    def set_show_ghost_mods(self, show: bool) -> dict:
        """切换是否在模组列表里显示已退订的残留条目。"""
        self.settings.show_ghost_mods = bool(show)
        self.settings.save()
        self.log("模组列表将" + ("显示" if show else "隐藏") + "已退订的残留条目")
        return {"ok": True, "show_ghost_mods": bool(show),
                "mods": self.mods_list(), "switches": self.switch_state()}

    def ghost_report(self) -> dict:
        """列出所有「已退订残留」，供界面提示与清理。

        残留指的是：Steam 已经退订、内容目录也没了，但
        Documents 里那份 ``.mod`` 描述符还留着（Steam 退订时不会删它）。
        它不占多少空间，但会一直出现在启动器列表和游戏里，看着像坏了。
        """
        if self.library is None:
            return {"ok": False, "error": "请先加载模组清单"}
        out = []
        for m in self.library.all():
            if not m.is_ghost():
                continue
            files = []
            if m.mod_file and is_file(m.mod_file):
                files.append(m.mod_file)
            out.append({
                "key": m.key, "name": m.name,
                "remote_file_id": m.remote_file_id,
                "files": files,
                "has_files": bool(files),
            })
        return {"ok": True, "items": out,
                "total": len(out),
                "with_files": sum(1 for x in out if x["has_files"])}

    def clean_ghosts(self) -> dict:
        """清理已退订的残留：把它们的 ``.mod`` 描述符移到备份目录。

        **为什么是"移动"而不是"删除"**：万一判断错了（例如 Steam 正处在
        订阅状态同步中），文件还在备份里，随时能拿回来。清理用户磁盘上的
        东西，可逆比干净更重要。

        只动 ``.mod`` 描述符，**不碰** ``workshop/content/`` 下的任何内容，
        也不动本地模组。
        """
        from datetime import datetime as _dt

        from .apply import backup_root
        rep = self.ghost_report()
        if not rep.get("ok"):
            return rep
        items = [x for x in rep["items"] if x["has_files"]]
        if not items:
            return {"ok": True, "moved": 0, "items": [],
                    "notes": ["没有需要清理的残留文件"]}

        dst = backup_root() / f"{_dt.now():%Y%m%d_%H%M%S}_ghost_mods"
        dst.mkdir(parents=True, exist_ok=True)
        moved: list[str] = []
        failed: list[str] = []
        for it in items:
            for f in it["files"]:
                src = Path(f)
                try:
                    shutil.move(long_path(src), long_path(dst / src.name))
                    moved.append(str(src))
                except OSError as exc:
                    failed.append(f"{src.name}: {exc}")
        if moved:
            try:
                self.library = load_library(self.settings.paths)
            except Exception:
                pass
            self.log(f"已清理 {len(moved)} 个已退订残留（备份在 {dst}）")
        notes = []
        if moved:
            notes.append(f"备份目录：{dst}")
            notes.append("若想恢复，把里面的 .mod 文件拷回「文档\\Paradox "
                         "Interactive\\Stellaris\\mod\\」即可")
        if failed:
            notes.append("失败：" + "；".join(failed[:3]))
        return {"ok": True, "moved": len(moved), "failed": len(failed),
                "backup_dir": str(dst), "items": moved,
                "mods": self.mods_list(), "switches": self.switch_state(),
                "notes": notes}

    def export_json(self, what: str = "report", out_path: str = "") -> dict:
        """导出检测结果 / 方案为 JSON。"""
        data: Any
        if what == "report":
            data = self.report.to_dict() if self.report else {}
        elif what == "solution":
            data = self.solution.to_dict() if self.solution else {}
        elif what == "mods":
            data = self.mods_list()
        else:
            data = self.status()
        if not out_path:
            d = report_output_dir(self.settings.paths)
            out_path = str(d / f"导出_{what}_"
                           f"{datetime.now():%Y%m%d_%H%M%S}.json")
        try:
            write_text(out_path, json.dumps(
                {"app": APP_NAME, "version": __version__,
                 "exported_at": datetime.now().isoformat(),
                 "what": what, "data": data},
                ensure_ascii=False, indent=2), bom=False)
        except (OSError, TypeError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}
        self.log(f"已导出 {what} → {out_path}")
        return {"ok": True, "path": out_path}


def _mod_concepts(idx: Any) -> list[str]:
    """给模组打几个「它改了什么」的标签。"""
    if idx is None:
        return []
    cats = idx.category_counts()
    label = {
        "scripted_trigger": "触发器", "scripted_effect": "脚本效果",
        "scripted_variable": "变量", "technology": "科技",
        "building": "建筑", "pop_job": "职业", "trait": "特质",
        "component_template": "舰船部件", "section_template": "舰船区段",
        "megastructure": "巨构", "event": "事件", "UI 界面定义": "界面",
        "localisation": "文本", "本地化文本": "文本", "static_modifier": "修正量",
        "ship_size": "舰船", "government": "政体", "civic": "国策",
        "edict": "法令", "policy": "政策", "relic": "遗珍",
        "resolution": "议案", "district": "区划", "deposit": "资源点",
    }
    out: list[str] = []
    for k, v in sorted(cats.items(), key=lambda kv: -kv[1]):
        for key, name in label.items():
            if key in k and name not in out:
                out.append(name)
                break
        if len(out) >= 6:
            break
    if idx.loc_keys and "文本" not in out:
        out.append("文本")
    if not out and idx.file_count:
        out.append("资源文件")
    return out


# ---------------------------------------------------------------------------
# 补丁文件名
# ---------------------------------------------------------------------------

# 本地规则生成的补丁会在原名前加这些前缀（见 patch/local.py 的 AUTO_PREFIX）。
# 判断"这个补丁改的是哪个原文件"时要逐个剥掉。
_PATCH_FILE_MARKS = (
    "zzz_smd_auto_merge_", "zzz_smd_auto_", "zzz_smd_", "zzz_", "smd_",
)


def _extract_commands(text: str) -> list[str]:
    """从 AI 的回答里抠出**看起来像控制台命令**的行。

    用途：把 AI 给的建议直接变成"可加入待送队列"的条目。
    刻意保守——只认那些一眼就是命令的行：

    * 去掉 Markdown 代码块的围栏、行首的编号/项目符号
    * 丢掉以中文开头的行（那是解释，不是命令）
    * 丢掉明显是句子的行（太长、含句号结尾）

    宁可少抠几条让用户手动补，也不要把解释文字塞进待送队列——
    那些送进游戏只会报"未知命令"。
    """
    out: list[str] = []
    for raw in (text or "").splitlines():
        s = raw.strip()
        if not s or s.startswith("```") or s.startswith("#"):
            continue
        # 去掉行首编号与项目符号
        s = re.sub(r"^\s*(?:[-*•]|\d+[.)、])\s*", "", s).strip()
        # 去掉行尾的 `# 注释`
        s = re.sub(r"\s*#\s*[^#]*$", "", s).strip()
        if not s or len(s) > 120:
            continue
        # 以中文/标点开头 → 是解释
        if re.match(r"^[\u4e00-\u9fff，。；：！？、]", s):
            continue
        # 以句号结尾、或含中文句读 → 是句子
        if s.endswith("。") or re.search(r"[，。；：！？]", s):
            continue
        # 首个词必须是 ASCII 标识符（命令名都是英文）
        if not re.match(r"^[a-z_][a-z0-9_]*\b", s):
            continue
        if s not in out:
            out.append(s)
    return out[:40]


def _slug_theme_id(name: str) -> str:
    """把主题名转成安全的 id。

    主题 id 会变成文件名（``<id>.json``），所以必须剥掉路径分隔符与
    盘符之类的东西——否则 ``../../x`` 这样的名字能写到主题目录外面去。
    AI 给的名字不可信，这一步不能省。
    """
    s = re.sub(r"[^0-9A-Za-z_\u4e00-\u9fff\-]+", "_", (name or "").strip())
    s = s.strip("_")[:40] or "ai_theme"
    # 纯中文 id 也能用，但加个前缀便于和内置主题区分
    return f"ai_{s}" if not s.startswith("ai_") else s


def strip_patch_marks(name: str) -> str:
    """剥掉补丁文件名上的前缀，还原成被改的那个文件的名字。

    ``zzz_SMD_auto_merge_00_biogenesis_utilities.txt``
    → ``00_biogenesis_utilities.txt``

    为什么要还原：冲突报告里的目标是**原文件名**，而补丁写出来的是
    带前缀的新文件。不还原就永远匹配不上，用户会看到
    "明明打了补丁，问题还挂在列表里"。
    """
    s = (name or "").replace("\\", "/").lower()
    changed = True
    while changed:
        changed = False
        for mk in _PATCH_FILE_MARKS:
            if s.startswith(mk) and len(s) > len(mk):
                s = s[len(mk):]
                changed = True
    return s
