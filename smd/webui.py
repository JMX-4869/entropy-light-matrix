"""WebView 桥：把 :class:`smd.app.App` 暴露给前端 JavaScript。

设计约定
--------
* 所有方法都返回 **JSON 字符串**，避免 pywebview 在复杂对象上的序列化差异
* 方法名用 camelCase（pywebview 的 JS 代理按名字直接查找）
* 耗时操作统一走 ``App.run_async``，前端轮询 ``poll()`` 拿进度与事件
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from typing import Any

from . import APP_NAME, APP_NAME_EN, __version__
from .app import App


# 序列化锁：``json.dumps`` 的 C 实现在遍历 dict 时会释放 GIL。
# 若另一个线程正在改那个 dict（轮询线程读 status() 的同时，
# 用户点了开关、代码改了内部状态），就会抛
# ``RuntimeError: dictionary changed size during iteration``。
# 这个错误发生在 C 层，和业务逻辑毫无关系，光看消息完全无法定位——
# 界面上只会出现一句莫名其妙的失败提示。所以：
#   ① 序列化串行化，避免和状态改动重叠；
#   ② 万一还是撞上，退化成可读错误而不是崩掉整个调用。
_DUMP_LOCK = threading.Lock()


def _dumps(obj: Any) -> str:
    """把对象序列化成 JSON 字符串（线程安全且容错）。"""
    for attempt in range(3):
        try:
            with _DUMP_LOCK:
                return json.dumps(obj, ensure_ascii=False, default=str)
        except RuntimeError as exc:
            if "changed size" in str(exc) and attempt < 2:
                time.sleep(0.02)      # 让改动先落地，重试拿一致快照
                continue
            return json.dumps({"error": f"序列化时对象被并发修改：{exc}"},
                              ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            return json.dumps({"error": f"序列化失败：{exc}"}, ensure_ascii=False)
    return json.dumps({"error": "序列化重试耗尽"}, ensure_ascii=False)


def _guard(fn):
    """把桥接方法的异常转成 ``{error, traceback}``。

    界面只显示 ``error`` 的短消息，但完整栈会一起返回并写入日志——
    否则遇到只在特定时序下出现的错误（例如并发修改），
    界面上只看到一句 ``dictionary changed size during iteration``，
    根本不知道是哪一行，排查成本极高。
    """
    import functools

    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        try:
            return fn(self, *args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - 桥接层必须兜住一切
            import traceback
            tb = traceback.format_exc()
            try:
                from .paths import write_text
                from .config import app_data_dir
                d = app_data_dir() / "logs"
                d.mkdir(parents=True, exist_ok=True)
                with open(d / "bridge_errors.log", "a", encoding="utf-8") as f:
                    f.write(f"\n===== {fn.__name__} =====\n{tb}\n")
            except Exception:
                pass
            return _dumps({"error": f"{type(exc).__name__}: {exc}",
                           "traceback": tb[-2000:]})
    return wrapper


class WebBridge:
    """JS ↔ Python 桥。"""

    def __init__(self, app: App) -> None:
        self.app = app
        self._queue: list[dict] = []
        self._qlock = threading.Lock()
        self._seq = 0
        app._push = self._push_internal

    # -- 内部推送 ---------------------------------------------------
    def _push_internal(self, channel: str, payload: dict) -> None:
        with self._qlock:
            self._seq += 1
            self._queue.append({"id": self._seq, "channel": channel,
                                "payload": payload})
            if len(self._queue) > 800:
                del self._queue[:200]

    # -- 前端轮询 ---------------------------------------------------
    @_guard
    def poll(self, since: str = "0") -> str:
        """取出断点之后的所有推送消息。"""
        try:
            last = int(since)
        except (TypeError, ValueError):
            last = 0
        with self._qlock:
            msgs = [m for m in self._queue if m["id"] > last]
        return _dumps({"messages": msgs,
                       "cursor": self._seq})

    # -- 基础 -------------------------------------------------------
    @_guard
    def getStatus(self) -> str:
        """返回应用整体状态的 JSON：路径与路径问题、设置、模组库摘要、检测结论、
        方案、当前任务进度、监控信息与运行时长。"""
        return _dumps(self.app.status())

    @_guard
    def getSettings(self) -> str:
        """返回当前全部配置项的 JSON（路径、关机与提醒、监控阈值、排序偏好等）。"""
        return _dumps(self.app.get_settings())

    @_guard
    def saveSettings(self, patch_json: str = "{}") -> str:
        """按 ``patch_json``（JSON 对象字符串，解析失败按空对象处理）局部更新配置并
        落盘，返回保存后的完整配置。"""
        try:
            patch = json.loads(patch_json) if isinstance(patch_json, str) else patch_json
        except ValueError:
            patch = {}
        return _dumps(self.app.save_settings(patch or {}))

    @_guard
    def autodetectPaths(self) -> str:
        """重新自动探测游戏目录与用户数据目录并保存，返回本次修复的项、最新路径与
        仍然存在的问题。"""
        return _dumps(self.app.autodetect_paths())

    @_guard
    def getVersion(self) -> str:
        """返回应用名称（中英文）、版本号、Python 版本与界面偏好。

        界面偏好（语言、是否显示已退订的空条目）也一并返回，
        这样前端在首次渲染前就能把设置定下来，不会闪一下默认值。
        """
        st = self.app.settings
        # 顺手把历史遗留的"一个补丁一个模组"并入统一补丁（后台跑，不挡启动）。
        # 早期版本每保存一次就新建一个模组，用几次就攒出几十个。
        if not getattr(self, "_consolidated", False):
            self._consolidated = True
            threading.Thread(target=self._consolidate_quietly,
                             name="smd-consolidate", daemon=True).start()
        return _dumps({"name": APP_NAME, "name_en": APP_NAME_EN,
                       "version": __version__,
                       "python": sys.version.split()[0],
                       "language": getattr(st, "language", "zh") or "zh",
                       "show_ghost_mods": bool(
                           getattr(st, "show_ghost_mods", False)),
                       "theme": getattr(st, "theme", "") or ""})

    def _consolidate_quietly(self) -> None:
        """后台合并旧补丁模组；失败不能影响界面。"""
        try:
            res = self.app.consolidate_old_patch_mods()
            if res.get("merged"):
                self.app.emit("patches_consolidated", res)
        except Exception as exc:  # noqa: BLE001 - 启动路径不能被它拖垮
            self.app.log(f"合并旧补丁模组失败：{exc}", "warn")

    # -- 模组 -------------------------------------------------------
    @_guard
    def loadMods(self) -> str:
        """（同步）重新读取模组清单，返回模组摘要、模组列表与启动器 playset 信息。"""
        return _dumps(self.app.load_mods())

    @_guard
    def getMods(self) -> str:
        """返回模组列表与库摘要的 JSON；尚未加载过模组库时会先加载一次。"""
        if self.app.library is None:
            self.app.load_mods()
        return _dumps({"mods": self.app.mods_list(),
                       "summary": self.app.library.summary()
                       if self.app.library else {}})

    @_guard
    def getModDetail(self, key: str) -> str:
        """返回 ``key`` 指定模组的详情：条目信息、改动的概念分布、样例定义与牵涉
        到它的检测问题。"""
        return _dumps(self.app.mod_detail(key))

    # -- 检测 -------------------------------------------------------
    @_guard
    def startScan(self) -> str:
        """启动「扫描 → 检测」后台任务，返回是否启动成功及失败原因；进度与结果由
        ``poll()`` 推送。"""
        ok = self.app.start_scan()
        return _dumps({"started": ok,
                       "reason": "" if ok else "已有任务在运行或路径无效"})

    @_guard
    def getFindings(self, severity: str = "all", kind: str = "all",
                    limit: str = "500", show_patched: str = "false") -> str:
        """按 ``severity``（``all`` 表示不限）、``kind`` 与 ``limit`` 过滤检测问题，
        返回问题列表、类型清单、各级数量、健康分、统计与游戏版本。

        ``show_patched='true'`` 时把"已被补丁解决"的条目也一并列出（默认不列）。
        """
        try:
            lim = int(limit)
        except (TypeError, ValueError):
            lim = 500
        show = str(show_patched).lower() in ("true", "1", "yes")
        return _dumps({
            "findings": self.app.findings(severity, kind, lim,
                                          show_patched=show),
            "patched_note": self.app.findings_filter_note(),
            "kinds": self.app.finding_kinds(),
            "counts": {
                s: (self.app.report.count(s) if self.app.report else 0)
                for s in ("critical", "high", "medium", "low", "info")
            },
            "health": (self.app.report.health_score()
                       if self.app.report else 0),
            "stats": (self.app.report.stats
                      if self.app.report else {}),
            "game_version": (list(self.app.report.game_version)
                             if self.app.report else [0, 0, 0]),
        })

    # -- 方案 -------------------------------------------------------
    @_guard
    def computeSolution(self) -> str:
        """立即求解最优加载顺序方案（顺序调整、兼容补丁与禁用建议），返回方案详情；
        尚未扫描时返回错误信息。"""
        return _dumps(self.app.compute_solution())

    @_guard
    def getSolution(self) -> str:
        """返回已算出方案的 JSON；尚未计算时返回 ``error`` 提示。"""
        if self.app.solution is None:
            return _dumps({"error": "尚未计算方案"})
        return _dumps(self.app.solution.to_dict())

    # ---- 模组开关：必须由用户决定，程序不擅自关闭 ------------------
    @_guard
    def decideDisable(self, key: str, approved: str = "false") -> str:
        """记录用户对某个「建议禁用」模组的决定（``approved`` 为真表示同意关闭）；
        这是改变模组启用状态的唯一入口，返回最新方案与待决/已同意数量。"""
        return _dumps(self.app.decide_disable(
            key, str(approved).lower() in ("true", "1", "yes")))

    @_guard
    def decideAllDisable(self, approved: str = "false") -> str:
        """一次性确认全部「建议禁用」项（``approved`` 为真表示全部关闭，否则全部
        保留），返回处理数量与最新方案。"""
        return _dumps(self.app.decide_all_disable(
            str(approved).lower() in ("true", "1", "yes")))

    @_guard
    def resetDecisions(self) -> str:
        """把所有模组开关决策退回「未表态」，返回重置数量与最新方案。"""
        return _dumps(self.app.reset_decisions())

    @_guard
    def getPendingDecisions(self) -> str:
        """返回仍在等用户拍板的禁用建议，以及待决、已同意、已保留与建议总数。"""
        return _dumps(self.app.pending_decisions())

    @_guard
    def applySolution(self, force: str = "false") -> str:
        """启动应用当前方案的后台任务（``force`` 为真时跳过安全检查强制执行），
        返回是否启动成功；结果与进度由 ``poll()`` 推送。"""
        ok = self.app.start_apply(force=str(force).lower() in ("true", "1", "yes"))
        return _dumps({"started": ok})

    @_guard
    def oneClickOptimize(self, apply: str = "true") -> str:
        """一键执行「扫描 → 检测 → 求解 → 应用」全流程（``apply`` 为假时只求解不
        写入），返回是否启动成功；不会自动关闭任何模组。"""
        ok = self.app.one_click_optimize(
            apply=str(apply).lower() in ("true", "1", "yes"))
        return _dumps({"started": ok})

    @_guard
    def cancelTask(self) -> str:
        """请求取消当前后台任务（协作式取消，任务自行检查取消标志），返回固定成功
        标记。"""
        self.app.cancel()
        return _dumps({"ok": True})

    # -- 备份 / 回滚 ------------------------------------------------
    @_guard
    def listBackups(self) -> str:
        """返回已有备份列表的 JSON（含路径、时间与大小）。"""
        return _dumps({"backups": self.app.list_backups()})

    @_guard
    def rollback(self, backup_dir: str = "") -> str:
        """**破坏性操作**：用备份覆盖当前用户数据目录与 mod 配置；``backup_dir`` 为
        空时取最近一次备份。返回恢复结果。"""
        return _dumps(self.app.rollback(backup_dir))

    @_guard
    def makeBackup(self) -> str:
        """立即创建一份手动备份并落盘，返回备份目录路径。"""
        return _dumps(self.app.make_backup())

    @_guard
    def removePatches(self) -> str:
        """**破坏性操作**：删除本程序自动生成的补丁模组，返回被移除的补丁列表。"""
        return _dumps(self.app.remove_patches())

    # -- 监控 -------------------------------------------------------
    @_guard
    def startMonitor(self) -> str:
        """启动后台运行时监控（已在运行则直接复用），返回监控状态。"""
        return _dumps(self.app.start_monitor())

    @_guard
    def stopMonitor(self) -> str:
        """停止后台运行时监控，返回停止后的监控状态。"""
        return _dumps(self.app.stop_monitor())

    @_guard
    def getMonitorStatus(self) -> str:
        """返回监控状态的 JSON（是否运行、游戏进程、内存峰值、错误计数等）。"""
        return _dumps(self.app.monitor_status())

    @_guard
    def getMonitorEvents(self, limit: str = "200") -> str:
        """返回最近 ``limit`` 条监控事件的 JSON（默认 200，非法输入按 200 处理）。"""
        try:
            lim = int(limit)
        except (TypeError, ValueError):
            lim = 200
        return _dumps({"events": self.app.monitor_events(lim)})

    @_guard
    def launchGame(self, use_steam: str = "true", monitor: str = "true") -> str:
        """启动游戏：``use_steam`` 决定走 Steam 还是直接启动，``monitor`` 为真时先
        开启监控；返回是否成功与提示信息。"""
        return _dumps(self.app.launch(
            use_steam=str(use_steam).lower() in ("true", "1", "yes"),
            monitor=str(monitor).lower() in ("true", "1", "yes")))

    # -- 日志与报告 -------------------------------------------------
    @_guard
    def getLogFiles(self) -> str:
        """返回游戏日志文件列表的 JSON（名称、路径、大小与修改时间）。"""
        return _dumps({"files": self.app.log_files()})

    @_guard
    def analyzeLog(self, name: str = "error.log") -> str:
        """启动对 ``name`` 指定日志的异步分析，返回是否启动成功；结果由 ``poll()``
        推送。"""
        ok = self.app.start_log_analysis(name)
        return _dumps({"started": ok})

    @_guard
    def getCrashDirs(self) -> str:
        """返回崩溃日志目录列表的 JSON（含时间、文件数与摘要信息）。"""
        return _dumps({"crashes": self.app.crash_dirs()})

    @_guard
    def generateReport(self, crash_dir: str = "") -> str:
        """启动崩溃报告生成任务（``crash_dir`` 为空时自动挑最新崩溃目录），返回是否
        启动成功；结果由 ``poll()`` 推送。"""
        ok = self.app.start_generate_report(crash_dir)
        return _dumps({"started": ok})

    @_guard
    def getReports(self) -> str:
        """返回已生成报告的文件列表（按修改时间倒序，含类型、大小与时间）。"""
        return _dumps({"reports": self.app.reports_list()})

    @_guard
    def readReport(self, path: str) -> str:
        """读取 ``path`` 指定报告的文本内容，返回文件名、正文与字符数；文件不存在时
        返回错误。"""
        return _dumps(self.app.read_report(path))

    @_guard
    def openPath(self, path: str) -> str:
        """用系统文件管理器打开 ``path``（目录或文件），返回是否成功与提示信息。"""
        return _dumps(self.app.open_path(path))

    @_guard
    def openUrl(self, url: str) -> str:
        """用系统默认浏览器打开 ``url``，返回是否成功与提示信息。"""
        return _dumps(self.app.open_url(url))

    @_guard
    def exportJson(self, what: str = "report") -> str:
        """把 ``what``（``report`` / ``solution`` / ``mods``，其它值导出整体状态）写成
        JSON 文件到报告目录，返回写入路径。"""
        return _dumps(self.app.export_json(what))

    # -- 关机 / 提醒 ------------------------------------------------
    @_guard
    def finish(self, shutdown: str = "", reminder: str = "",
               summary: str = "") -> str:
        """会话收尾：生成结束小结、按 ``reminder`` 注册开机提醒、按 ``shutdown`` 安排
        关机；**可能真的关机**，返回执行结果与消息列表。"""
        kw: dict[str, Any] = {}
        if shutdown != "":
            kw["do_shutdown"] = str(shutdown).lower() in ("true", "1", "yes")
        if reminder != "":
            kw["do_reminder"] = str(reminder).lower() in ("true", "1", "yes")
        if summary:
            kw["summary"] = summary
        return _dumps(self.app.finish(**kw))

    @_guard
    def cancelShutdown(self) -> str:
        """取消已安排的定时关机，返回是否成功与提示信息。"""
        return _dumps(self.app.cancel_shutdown())

    @_guard
    def scheduleShutdown(self, delay: str = "120") -> str:
        """**危险操作**：安排 ``delay`` 秒后关机（默认 120，非法输入按 120 处理），
        返回是否已安排、剩余时间与延迟。"""
        try:
            d = int(delay)
        except (TypeError, ValueError):
            d = 120
        return _dumps(self.app.shutdown_now(d))

    @_guard
    def getPendingReminder(self) -> str:
        """返回尚未确认的开机提醒内容；没有待办提醒时返回 ``null``。"""
        return _dumps({"reminder": self.app.pending_reminder()})

    @_guard
    def dismissReminder(self) -> str:
        """清除待确认的开机提醒（同时写盘），返回固定成功标记。"""
        return _dumps(self.app.dismiss_reminder())

    @_guard
    def registerReminder(self, on: str = "true") -> str:
        """按 ``on`` 注册或取消「开机提醒」计划任务，返回是否成功与提示信息。"""
        return _dumps(self.app.register_reminder(
            str(on).lower() in ("true", "1", "yes")))

    # -- AI 接入 ----------------------------------------------------
    @_guard
    def getAiConfig(self) -> str:
        """返回 AI 配置的 JSON；出于隐私考虑 ``api_key`` 一律置空，只给出
        ``api_key_set`` 与掩码串，绝不下发明文密钥。"""
        return _dumps(self.app.ai_config())

    @_guard
    def saveAiConfig(self, patch_json: str = "{}") -> str:
        """按 ``patch_json`` 更新 AI 配置并落盘；``api_key`` 为空串表示不修改，传
        ``__CLEAR__`` 表示清空。返回掩码后的最新配置。"""
        try:
            patch = json.loads(patch_json) if isinstance(patch_json, str) \
                else (patch_json or {})
        except ValueError:
            patch = {}
        return _dumps(self.app.save_ai_config(patch or {}))

    @_guard
    def aiTest(self, override_json: str = "{}") -> str:
        """启动 AI 连通性测试后台任务（只发固定问候语、不含模组内容）。

        ``override_json`` 传界面上**尚未保存**的设置（尤其是刚输入的密钥），
        这样用户填完就能直接测，不需要先保存。结果经 ``ai_test_done``
        事件推送。
        """
        try:
            override = json.loads(override_json) if override_json else {}
        except ValueError:
            override = {}
        ok = self.app.run_async(
            "测试 AI 连接", lambda: self.app.ai_test(override),
            lambda r, e: self.app.emit("ai_test_done",
                                       {"result": r, "error": e}))
        return _dumps({"started": ok})

    @_guard
    def aiListModels(self, override_json: str = "{}") -> str:
        """拉取服务商的可用模型列表。

        ``override_json`` 同上，用于让未保存的密钥/地址立即生效。
        返回里带 ``source``：``"api"`` 是真的问到了，``"fallback"``
        表示请求失败、给的是内置预设（界面必须如实说明）。
        """
        try:
            override = json.loads(override_json) if override_json else {}
        except ValueError:
            override = {}
        return _dumps(self.app.ai_list_models(override))

    @_guard
    def getPatchCandidates(self, limit: str = "300") -> str:
        """返回可交给 AI 生成补丁的冲突候选（仅 S1/F1/L1 且严重度较高），``limit``
        为最大条数（默认 300，非法输入按 300 处理）。"""
        try:
            lim = int(limit)
        except (TypeError, ValueError):
            lim = 300
        return _dumps({"candidates": self.app.patch_candidates(lim)})

    @_guard
    def aiPatchPreview(self, index: str = "0", hint: str = "",
                       goal: str = "merge") -> str:
        """生成将要发给 AI 的提示词预览：``index`` 为候选序号，``hint`` 为附加要求，
        ``goal`` 为处理目标。返回上下文、提示词与 token 估算，便于用户先确认。"""
        try:
            i = int(index)
        except (TypeError, ValueError):
            i = 0
        return _dumps(self.app.ai_patch_preview(i, user_hint=hint, goal=goal))

    @_guard
    def aiGeneratePatch(self, index: str = "0", hint: str = "",
                        goal: str = "merge") -> str:
        """启动 AI 补丁生成任务（参数含义同预览）。

        **前置校验放在这里同步做**，而不是丢给后台任务：

        * 没开「允许发送模组内容」→ 立刻回 ``need_consent``，什么都不启动
        * 没配好供应商/模型 → 立刻回 ``need_config``

        早先这里直接 ``run_async``，同意与否要等后台任务跑完才知道，
        界面拿到的是 ``started: true``——用户以为开始了，
        过一会儿又收到一条"失败"。而且文档里写着会返回 ``need_consent``，
        实际从来没返回过：**文档与行为不一致**，
        连带把测试也带偏了（它只断言 ``started != True``，
        在同意开着的时候是假通过）。

        返回是否启动成功，结果经 ``ai_patch_done`` 推送。
        """
        if not self.app.settings.ai.allow_send_mod_content:
            return _dumps({
                "started": False, "need_consent": True,
                "error": "「允许发送模组内容」未打开。"
                         "AI 生成会把模组内容发送给服务商，"
                         "请先到 AI 接入页显式开启。",
            })
        if not self.app.settings.ai.configured:
            return _dumps({
                "started": False, "need_config": True,
                "error": "AI 尚未配置完成（缺少 API Key 或模型名）",
            })
        try:
            i = int(index)
        except (TypeError, ValueError):
            i = 0

        def job() -> dict:
            self.app._task_progress("正在请求 AI 生成补丁", 0.1)
            return self.app.ai_generate_patch(i, user_hint=hint, goal=goal,
                                              progress=self.app._task_progress)

        def done(res, err) -> None:
            self.app.emit("ai_patch_done",
                          {"result": res, "error": err, "index": i})
        ok = self.app.run_async("AI 生成补丁", job, done)
        return _dumps({"started": ok})

    @_guard
    def aiSavePatch(self, target: str = "", title: str = "",
                    description: str = "") -> str:
        """把刚生成的 AI 补丁（按 ``target`` 关联）保存为真正的补丁模组，``title`` /
        ``description`` 可覆盖标题与说明；存在严重校验问题时拒绝保存。"""
        return _dumps(self.app.ai_save_patch(target, title=title,
                                             description=description))

    # -- 手写补丁 ---------------------------------------------------
    @_guard
    def getPatchTemplates(self) -> str:
        """返回手写补丁可用的模板列表的 JSON。"""
        return _dumps({"templates": self.app.patch_templates()})

    @_guard
    def validatePatch(self, text: str = "", rel: str = "",
                      target: str = "") -> str:
        """校验 ``text`` 补丁文本（``rel`` 为落地路径，``target`` 为要解决的冲突目标），
        返回是否通过及问题列表，不做任何写盘。"""
        return _dumps(self.app.patch_validate(text, rel=rel,
                                              conflict_target=target))

    @_guard
    def saveManualPatch(self, title: str = "", rel: str = "", text: str = "",
                        description: str = "") -> str:
        """保存手写补丁为补丁模组：写入前强制校验，不通过则拒绝并返回问题列表；成功
        时返回新补丁的名称与路径。"""
        return _dumps(self.app.patch_save_manual(
            title=title, rel=rel, text=text, description=description))

    @_guard
    def getModFile(self, mod_key: str = "", rel: str = "",
                   around: str = "") -> str:
        """读取 ``mod_key`` 模组中 ``rel`` 文件的内容供手写补丁时对照，``around`` 指定
        聚焦的代码块/行附近；返回正文与截断信息。"""
        return _dumps(self.app.patch_file_source(mod_key, rel, around))

    # -- 补丁管理 ---------------------------------------------------
    @_guard
    def getPatches(self) -> str:
        """返回已有补丁模组列表的 JSON。"""
        return _dumps({"patches": self.app.patch_list()})

    @_guard
    def readPatch(self, name: str = "", rel: str = "") -> str:
        """读取补丁模组 ``name`` 中 ``rel`` 文件的内容（``rel`` 为空时返回该补丁的文件
        清单），返回正文或错误信息。"""
        return _dumps(self.app.patch_read(name, rel))

    @_guard
    def updatePatch(self, name: str = "", rel: str = "",
                    text: str = "") -> str:
        """用 ``text`` 覆盖补丁模组 ``name`` 中 ``rel`` 文件的内容，返回写入结果。"""
        return _dumps(self.app.patch_update(name, rel, text))

    @_guard
    def deletePatch(self, name: str = "") -> str:
        """**破坏性操作**：删除 ``name`` 指定的补丁模组，返回删除结果。"""
        return _dumps(self.app.patch_delete(name))

    # -- 手动排序 ---------------------------------------------------
    @_guard
    def getOrderView(self) -> str:
        """返回「排序」页视图：当前手动顺序、自动排序建议、两者的差异与被排除的模组。"""
        return _dumps(self.app.get_order_view())

    @_guard
    def moveMod(self, key: str = "", delta: str = "0",
                to_index: str = "") -> str:
        """把 ``key`` 模组在加载顺序里上/下移动；``delta`` 为相对偏移量，``to_index``
        非空时直接指定目标位置（优先于偏移量）。返回移动结果与最新顺序视图。"""
        try:
            d = int(delta)
        except (TypeError, ValueError):
            d = 0
        idx = None
        if str(to_index).strip() != "":
            try:
                idx = int(to_index)
            except (TypeError, ValueError):
                idx = None
        r = self.app.reorder_move(key, d, to_index=idx)
        r["view"] = self.app.get_order_view()
        return _dumps(r)

    @_guard
    def resetOrder(self) -> str:
        """丢弃手动排序，回到自动排序结果（没有方案时回到磁盘顺序），返回重置后的顺序
        与最新视图。"""
        r = self.app.reorder_reset()
        r["view"] = self.app.get_order_view()
        return _dumps(r)

    @_guard
    def applyOrder(self, order_json: str = "") -> str:
        """**会写入群星配置**：把 ``order_json``（JSON 字符串数组，为空时用当前手动顺序）
        写成实际加载顺序，立刻影响游戏（不改模组启用状态）。返回是否启动任务。"""
        order = None
        if order_json:
            try:
                order = json.loads(order_json)
            except ValueError:
                order = None
        ok = self.app.start_reorder_apply(order)
        return _dumps({"started": ok})

    # -- 模组启用开关 -----------------------------------------------
    @_guard
    def scanAchievements(self) -> str:
        """扫描哪些模组会影响成就（后台任务，结果经 achievement_done 推送）。"""
        return _dumps({"started": self.app.achievement_scan()})

    @_guard
    def getAchievementRisks(self) -> str:
        """会影响成就的模组清单（含命中项数与依据）。"""
        return _dumps({"ok": True,
                       "rows": self.app.achievement_risk_rows()})

    @_guard
    def stageAchievementRisks(self, keys_json: str = "[]") -> str:
        """把选中的「影响成就」模组**暂存**为关闭（不写盘）。

        ``keys_json`` 为空表示处理全部当前启用的命中项。
        """
        try:
            keys = json.loads(keys_json) if keys_json else []
        except ValueError:
            keys = []
        if not isinstance(keys, list):
            keys = []
        return _dumps(self.app.stage_achievement_risks(
            [str(k) for k in keys] or None))

    @_guard
    def startChecksumScan(self) -> str:
        """Start read-only multiplayer checksum triage."""
        return _dumps({"started": self.app.start_checksum_scan()})

    @_guard
    def stageChecksumRisks(self) -> str:
        """Stage suspect mods for disabling; existing preview/apply commits it."""
        return _dumps(self.app.stage_checksum_risks())

    @_guard
    def getSwitchState(self) -> str:
        """返回每个模组的开关暂存状态（滑块用）与待写入的变化统计。"""
        return _dumps(self.app.switch_state())

    @_guard
    def setModSwitch(self, key: str = "", enabled: str = "true") -> str:
        """暂存单个模组的开关；只进暂存区，点「应用」才写盘。"""
        return _dumps(self.app.set_mod_switch(
            key, str(enabled).lower() in ("true", "1", "yes")))

    @_guard
    def setModSwitches(self, keys_json: str = "[]",
                       enabled: str = "true") -> str:
        """批量暂存模组开关（用于「只对筛选结果操作」）。"""
        try:
            keys = json.loads(keys_json) if keys_json else []
        except ValueError:
            keys = []
        return _dumps(self.app.set_mod_switches(
            list(keys or []), str(enabled).lower() in ("true", "1", "yes")))

    @_guard
    def switchBulk(self, mode: str = "reset") -> str:
        """整体批量：``enable_all`` / ``disable_all`` / ``invert`` / ``reset``。"""
        return _dumps(self.app.switch_bulk(mode))

    @_guard
    def previewModSwitches(self) -> str:
        """预览即将写入的模组开关变化（写盘前确认用）。"""
        return _dumps(self.app.preview_mod_switches())

    @_guard
    def applyModSwitches(self, sync_launcher: str = "true") -> str:
        """把暂存的模组开关写入群星（``dlc_load.json`` + 同步启动器）。"""
        ok = self.app.start_apply_mod_switches(
            sync_launcher=str(sync_launcher).lower() in ("true", "1", "yes"))
        return _dumps({"started": ok})

    def openModWorkshop(self, key: str = "") -> str:
        """打开某个模组的 Steam 创意工坊详情页。"""
        return _dumps(self.app.open_mod_workshop(key))

    def setShowGhostMods(self, show: str = "false") -> str:
        """切换模组列表是否显示已退订的残留条目。"""
        return _dumps(self.app.set_show_ghost_mods(
            str(show).lower() in ("true", "1", "yes")))

    @_guard
    def getGhostReport(self) -> str:
        """列出所有已退订残留（含仍留在磁盘上的 .mod 描述符）。"""
        return _dumps(self.app.ghost_report())

    @_guard
    def cleanGhosts(self) -> str:
        """把已退订残留的 .mod 描述符移到备份目录（可恢复）。"""
        return _dumps(self.app.clean_ghosts())

    # -- DLC 管理 ---------------------------------------------------
    @_guard
    def getDlcState(self) -> str:
        """返回全部 DLC、按类别分组、以及暂存的开关改动。"""
        return _dumps(self.app.dlc_state())

    @_guard
    def setDlcSwitch(self, key: str = "", enabled: str = "true") -> str:
        """暂存单个 DLC 的开关；只进暂存区，点「应用」才写盘。"""
        return _dumps(self.app.set_dlc_switch(
            key, str(enabled).lower() in ("true", "1", "yes")))

    @_guard
    def setDlcSwitches(self, keys_json: str = "[]",
                       enabled: str = "true") -> str:
        """批量暂存 DLC 开关（按类别操作用）。"""
        try:
            keys = json.loads(keys_json) if keys_json else []
        except ValueError:
            keys = []
        return _dumps(self.app.set_dlc_switches(
            list(keys or []), str(enabled).lower() in ("true", "1", "yes")))

    @_guard
    def dlcBulk(self, mode: str = "reset") -> str:
        """批量：``enable_all`` / ``disable_all`` / ``reset`` /
        ``enable:<类别>`` / ``disable:<类别>``。"""
        return _dumps(self.app.dlc_bulk(mode))

    @_guard
    def applyDlcSwitches(self) -> str:
        """把 DLC 开关写入 ``dlc_load.json``。"""
        return _dumps({"started": self.app.start_apply_dlc()})

    @_guard
    def openDlcFolder(self) -> str:
        """在资源管理器里打开游戏的 ``dlc`` 目录。"""
        return _dumps(self.app.open_dlc_folder())

    # -- 本地自动补丁（不联网） -------------------------------------
    def guessPatchScope(self) -> str:
        """列出哪些问题类型能本地生成补丁、各有多少条。"""
        return _dumps(self.app.guess_patch_scope())

    def generateAutoPatches(self, kinds_json: str = "[]") -> str:
        """用内置规则在本地生成补丁草稿（不调用任何 AI）。"""
        try:
            kinds = json.loads(kinds_json) if kinds_json else []
        except ValueError:
            kinds = []
        return _dumps(self.app.generate_auto_patches(list(kinds or []) or None))

    def applyAutoPatches(self, indexes_json: str = "[]") -> str:
        """把选中的自动补丁草稿写成一个补丁模组。"""
        try:
            idx = json.loads(indexes_json) if indexes_json else []
        except ValueError:
            idx = []
        return _dumps(self.app.apply_auto_patches(
            [int(i) for i in (idx or [])] or None))

    # -- 一键翻译 ---------------------------------------------------
    def getTranslatePlan(self, target: str = "zh", limit: str = "0") -> str:
        """统计要翻译多少条（纯本地统计，不发送任何内容）。"""
        try:
            lim = int(limit)
        except ValueError:
            lim = 0
        return _dumps(self.app.translation_plan(target, limit=lim))

    def startTranslateAll(self, target: str = "zh",
                          limit: str = "0") -> str:
        """一键翻译全部模组到指定语言（会把文本发送给 AI 服务商）。"""
        try:
            lim = int(limit)
        except ValueError:
            lim = 0
        return _dumps({"started": self.app.start_translate_all(
            target, limit=lim)})

    def verifyTranslation(self, target: str = "zh") -> str:
        """翻译后自检：统计目标语言已有多少条译文。"""
        return _dumps(self.app.verify_translation(target))

    # -- 界面语言 ---------------------------------------------------
    def setLanguage(self, lang: str = "zh") -> str:
        """切换软件界面语言（``zh`` / ``en``）。"""
        return _dumps(self.app.set_language(lang))

    # -- 批量生成补丁 -----------------------------------------------
    @_guard
    def getBatchPlan(self, indices_json: str = "[]",
                     engine: str = "local") -> str:
        """批量生成前的预览：选中多少目标、本地/AI 各能产出多少。"""
        try:
            idx = json.loads(indices_json) if indices_json else []
        except ValueError:
            idx = []
        return _dumps(self.app.batch_patch_plan(
            [int(i) for i in (idx or [])] or None, engine))

    @_guard
    def startBatchPatches(self, indices_json: str = "[]",
                          engine: str = "local", goal: str = "merge",
                          hint: str = "") -> str:
        """启动批量生成补丁（``engine`` 为 ``local`` 或 ``ai``）。

        AI 模式会联网发送模组内容，需要先开启隐私开关。
        """
        try:
            idx = json.loads(indices_json) if indices_json else []
        except ValueError:
            idx = []
        ok = self.app.start_batch_patches(
            [int(i) for i in (idx or [])] or None, engine, goal, hint)
        return _dumps({"started": ok})

    # -- 翻译工作表（用户自己翻）------------------------------------
    @_guard
    def getTranslationSheet(self, target: str = "zh",
                            limit: str = "200") -> str:
        """取出待翻译条目，让用户自己填译文。"""
        try:
            lim = int(limit)
        except ValueError:
            lim = 200
        return _dumps(self.app.translation_sheet(target, limit=lim))

    @_guard
    def saveManualTranslation(self, target: str = "zh",
                              entries_json: str = "{}") -> str:
        """保存用户手写的译文（空译文会被丢弃）。"""
        try:
            entries = json.loads(entries_json) if entries_json else {}
        except ValueError:
            entries = {}
        return _dumps(self.app.save_manual_translation(
            target, entries if isinstance(entries, dict) else {}))

    @_guard
    def cancelTask(self) -> str:
        """停止当前后台任务（界面「停止」按钮）。

        只置位取消标志、不强杀线程：耗时步骤会在自己的循环里主动退出，
        正在进行的写盘不会被中断。返回当前任务快照供界面立即刷新。
        """
        return _dumps(self.app.cancel_task())

    @_guard
    def getTask(self) -> str:
        """当前任务快照（界面重连后补画进度条用）。"""
        return _dumps(self.app.task.to_dict())

    # -- 批量保存补丁 -----------------------------------------------
    @_guard
    def getBatchSavePlan(self, engine: str = "ai") -> str:
        """批量保存前的预览：多少待保存、哪些会被拒绝及原因。"""
        return _dumps(self.app.batch_save_plan(engine))

    @_guard
    def batchSavePatches(self, engine: str = "ai") -> str:
        """把已生成的补丁一次性全部保存成模组。"""
        return _dumps(self.app.batch_save_patches(engine))

    @_guard
    def getFindingsFilterNote(self) -> str:
        """有多少冲突因「已被补丁解决」而没显示出来。"""
        return _dumps(self.app.findings_filter_note())

    # -- 时之虫档案（群星知识库）------------------------------------
    @_guard
    def getWikiHome(self) -> str:
        """档案首页：元信息、分类目录、可送进游戏的命令。"""
        return _dumps(self.app.wiki_home())

    @_guard
    def wikiSearch(self, query: str = "", limit: str = "200") -> str:
        """在档案里全文检索（多关键词用空格分隔，全部命中才算）。"""
        try:
            lim = int(limit)
        except (TypeError, ValueError):
            lim = 200
        return _dumps(self.app.wiki_search(query, lim))

    @_guard
    def wikiSection(self, key: str = "", offset: str = "0",
                    limit: str = "300") -> str:
        """按分类取行（分页，避免一次下发几千行卡住界面）。"""
        try:
            off = int(offset)
        except (TypeError, ValueError):
            off = 0
        try:
            lim = int(limit)
        except (TypeError, ValueError):
            lim = 300
        return _dumps(self.app.wiki_section(key, off, lim))

    @_guard
    def aiMakeTheme(self, prompt: str = "", name: str = "") -> str:
        """用一句提示词让 AI 生成并保存一套界面主题。"""
        return _dumps(self.app.ai_make_theme(prompt, name=name))

    @_guard
    def wikiAsk(self, question: str = "") -> str:
        """向档案提问（先本地检索，再把相关条目连同问题发给 AI）。"""
        return _dumps(self.app.wiki_ask(question))

    @_guard
    def wikiWriteCommands(self, want: str = "") -> str:
        """让 AI 按需求写控制台命令（只输出命令，可直接送进游戏）。"""
        return _dumps(self.app.wiki_write_commands(want))

    # -- 控制台（指令台）--------------------------------------------
    @_guard
    def getConsole(self) -> str:
        """命令库 + 游戏控制台历史状态。"""
        return _dumps(self.app.console_library())

    @_guard
    def pushConsole(self, commands_json: str = "[]",
                    keep_existing: str = "true") -> str:
        """把选中的命令写进游戏控制台历史。

        进游戏按 ``~`` 打开控制台、按 ``↑`` 翻到、回车执行。
        """
        try:
            cmds = json.loads(commands_json) if commands_json else []
        except ValueError:
            cmds = []
        if not isinstance(cmds, list):
            cmds = []
        keep = str(keep_existing).lower() in ("true", "1", "yes")
        return _dumps(self.app.console_push([str(c) for c in cmds], keep))

    @_guard
    def clearConsole(self) -> str:
        """清空游戏控制台历史（先备份原文件）。"""
        return _dumps(self.app.console_clear())

    # -- AI 接管 ----------------------------------------------------
    @_guard
    def startTakeover(self, allow_disable: str = "false",
                      allow_reorder: str = "true",
                      apply: str = "false") -> str:
        """启动 AI 接管：深度解析 → 出方案（可选直接执行）。

        ``allow_disable`` / ``allow_reorder`` 是**用户开局的选择**，
        由界面上的勾选框传入；两个都关掉时只诊断、不改任何东西。
        """
        ok = self.app.start_ai_takeover(
            allow_disable=str(allow_disable).lower() in ("true", "1", "yes"),
            allow_reorder=str(allow_reorder).lower() in ("true", "1", "yes"),
            apply=str(apply).lower() in ("true", "1", "yes"),
        )
        return _dumps({"started": ok})

    @_guard
    def getTakeoverPlan(self) -> str:
        """取当前接管方案（供界面审阅后确认执行）。"""
        return _dumps(self.app.ai_takeover_plan())

    @_guard
    def applyTakeover(self) -> str:
        """按已生成的方案执行接管。"""
        ok = self.app.ai_takeover_apply()
        return _dumps({"started": ok,
                       "error": "" if ok else "还没有生成接管方案"})

    # -- AI 分析（崩溃 / 日志 / 加载顺序）----------------------------
    @_guard
    def aiAnalyzeCrash(self, crash_dir: str = "") -> str:
        """让 AI 分析一次崩溃，结论进补丁工坊。"""
        ok = self.app.ai_analyze_crash(crash_dir)
        return _dumps({"started": ok})

    @_guard
    def aiAnalyzeLog(self, name: str = "error.log") -> str:
        """让 AI 分析一个日志文件。"""
        ok = self.app.ai_analyze_log(name)
        return _dumps({"started": ok})

    @_guard
    def aiAnalyzeOrder(self) -> str:
        """让 AI 分析加载顺序并给建议。"""
        ok = self.app.ai_analyze_order()
        return _dumps({"started": ok})

    @_guard
    def getAiIssues(self) -> str:
        """取 AI 分析出来的问题（按来源分组，已解决的已剔除）。"""
        return _dumps({"ok": True,
                       "groups": self.app.ai_issue_groups_visible(),
                       "all": self.app.ai_issue_groups()})

    @_guard
    def aiPatchIssues(self, keys_json: str = "[]") -> str:
        """对选中的 AI 问题生成补丁。"""
        try:
            keys = json.loads(keys_json) if keys_json else []
        except ValueError:
            keys = []
        if not isinstance(keys, list):
            keys = []
        ok = self.app.ai_patch_group([str(k) for k in keys])
        return _dumps({"started": ok,
                       "error": "" if ok else "没有可生成补丁的选中项"})

    @_guard
    def resolveAiIssues(self, keys_json: str = "[]") -> str:
        """把选中的 AI 问题标记为已解决（之后不再显示）。"""
        try:
            keys = json.loads(keys_json) if keys_json else []
        except ValueError:
            keys = []
        return _dumps(self.app.ai_resolve_issues(
            [str(k) for k in (keys if isinstance(keys, list) else [])]))

    # -- 界面主题 ---------------------------------------------------
    @_guard
    def getThemes(self) -> str:
        """返回全部主题、当前主题与令牌清单（供选择器与编辑器使用）。"""
        return _dumps(self.app.themes_payload())

    @_guard
    def saveTheme(self, theme_json: str = "{}") -> str:
        """保存一份自制主题；校验不通过会被拒绝并返回问题清单。"""
        try:
            theme = json.loads(theme_json) if isinstance(theme_json, str) \
                else (theme_json or {})
        except ValueError:
            return _dumps({"ok": False, "error": "主题数据不是合法 JSON"})
        return _dumps(self.app.save_theme(theme or {}))

    @_guard
    def deleteTheme(self, tid: str = "") -> str:
        """删除一份自制主题；内置主题会被拒绝。"""
        return _dumps(self.app.delete_theme(tid))

    @_guard
    def exportTheme(self, tid: str = "") -> str:
        """把主题导出成 JSON 文件，便于分享。"""
        return _dumps(self.app.export_theme(tid))

    @_guard
    def pickThemeFile(self) -> str:
        """弹出文件选择框并导入主题；用户取消时返回 cancelled。"""
        return _dumps(self.app.import_theme_dialog())

    @_guard
    def exportBuiltinThemes(self) -> str:
        """把内置主题各导出一份到主题目录，方便玩家照着改。"""
        return _dumps(self.app.export_builtin_themes())

    # -- 窗口控制（自绘标题栏用）-------------------------------------
    @_guard
    def minimize(self) -> str:
        """最小化主窗口，返回固定成功标记。"""
        self._window_call("minimize")
        return _dumps({"ok": True})

    @_guard
    def toggleMaximize(self) -> str:
        """在最大化与还原之间切换，并返回切换后的状态。

        注意用的是 ``maximize`` / ``restore`` 而不是 ``toggle_fullscreen``：
        全屏会连标题栏一起吃掉，用户就再也点不到最小化/还原了，
        和标题栏上的"最大化"语义完全不符。

        "当前是不是最大化"优先问 pywebview 的 ``window.state``，
        但**部分后端（Windows/EdgeChromium 实测）返回的是空字典**，
        取不到 .state 时改用窗口尺寸是否铺满屏幕来判断，
        再退一步就用本方法自己记录的开关。三层兜底，
        保证按钮图标和实际状态不会各说各话。
        """
        w = self._window
        if w is None:
            return _dumps({"ok": False, "error": "窗口不可用"})
        maximized = self._is_maximized()
        try:
            if maximized:
                w.restore()
            else:
                w.maximize()
        except Exception as exc:
            return _dumps({"ok": False, "error": str(exc)})
        self._maximized = not maximized
        return _dumps({"ok": True, "maximized": self._maximized})

    @_guard
    def closeWindow(self) -> str:
        """**关闭主窗口并结束程序**，返回固定成功标记。"""
        self._window_call("destroy")
        return _dumps({"ok": True})

    def _is_maximized(self) -> bool:
        """尽力判断窗口是否处于最大化；判断不了就用自己记录的开关。"""
        w = self._window
        if w is None:
            return self._maximized
        # 1) 问后端要状态
        try:
            st = getattr(w, "state", None)
            if isinstance(st, str) and st:
                return st.strip().lower() in ("maximized", "fullscreen")
        except Exception:
            pass
        # 2) 看窗口尺寸是否铺满屏幕工作区
        try:
            import ctypes

            sw = ctypes.windll.user32.GetSystemMetrics(0)
            sh = ctypes.windll.user32.GetSystemMetrics(1)
            if w.width and w.height and sw and sh:
                return int(w.width) >= sw - 20 and int(w.height) >= sh - 60
        except Exception:
            pass
        # 3) 用自己记录的开关
        return self._maximized

    _window = None
    _maximized = False

    def _window_call(self, method: str) -> None:
        w = self._window
        if w is None:
            return
        try:
            getattr(w, method)()
        except Exception:
            pass
