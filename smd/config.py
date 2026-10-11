"""配置与路径探测。

群星的目录结构（Windows）::

    用户数据  %USERPROFILE%\\Documents\\Paradox Interactive\\Stellaris\\
              ├── mod\\*.mod          本地/工坊模组描述符
              ├── launcher-v2.sqlite Paradox 启动器数据库（playset 真源）
              ├── dlc_load.json       游戏实际读取的启用模组列表
              ├── logs\\               error.log / game.log / ...
              ├── crashes\\            崩溃转储目录（每次崩溃一个子目录）
              ├── settings.txt        游戏设置
              └── save games\\
    游戏本体  <steam>\\steamapps\\common\\Stellaris\\
    工坊内容  <steam>\\steamapps\\workshop\\content\\281990\\<id>\\
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from . import APP_ID, APP_NAME, __version__
from .paths import exists, is_dir, is_file, long_path, read_text, write_text

DOCUMENTS = Path(os.path.expanduser("~")) / "Documents"
STELLARIS_USER_DIR_NAME = "Paradox Interactive/Stellaris"
STELLARIS_APPID = "281990"
STELLARIS_EXE = "stellaris.exe"


def _candidate_user_dirs() -> list[Path]:
    """穷举用户数据目录可能的位置（含 OneDrive 重定向）。"""
    out: list[Path] = []
    home = Path(os.path.expanduser("~"))
    for base in (
        home / "Documents",
        home / "OneDrive" / "Documents",
        home / "OneDrive" / "文件" / "Documents",
        home / "OneDrive - Personal" / "Documents",
        home / "文档",
    ):
        out.append(base / STELLARIS_USER_DIR_NAME)
    for env in ("USERPROFILE", "HOMEDRIVE", "OneDrive"):
        v = os.environ.get(env)
        if v and env not in ("HOMEDRIVE",):
            out.append(Path(v) / "Documents" / STELLARIS_USER_DIR_NAME)
    # 去重
    seen: set[str] = set()
    uniq: list[Path] = []
    for p in out:
        k = str(p).lower()
        if k not in seen:
            seen.add(k)
            uniq.append(p)
    return uniq


def _steam_roots() -> list[Path]:
    """定位所有 Steam 库根目录（含额外库盘符）。"""
    roots: list[Path] = []
    # 1) 注册表
    if sys.platform.startswith("win"):
        try:
            import winreg  # type: ignore

            for hive, sub in (
                (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam"),
                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam"),
                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam"),
            ):
                try:
                    with winreg.OpenKey(hive, sub) as k:
                        for name in ("SteamPath", "InstallPath"):
                            try:
                                val, _ = winreg.QueryValueEx(k, name)
                            except OSError:
                                continue
                            if val:
                                roots.append(Path(str(val).replace("/", "\\")))
                except OSError:
                    continue
        except Exception:
            pass
    # 2) 常见安装位置
    for drive in "CDEFGHIJKLMNOPQRSTUVWXYZ":
        for rel in ("steam", "Steam", "SteamLibrary", "Games/Steam",
                    "Program Files (x86)/Steam", "Program Files/Steam"):
            roots.append(Path(f"{drive}:/{rel}"))
    # 去重且存在
    seen: set[str] = set()
    out: list[Path] = []
    for r in roots:
        k = str(r).lower()
        if k in seen:
            continue
        seen.add(k)
        if is_dir(r):
            out.append(r)
    return out


def _parse_library_folders(vdf: str) -> list[str]:
    """从 libraryfolders.vdf 里抠出所有库路径。"""
    out: list[str] = []
    for m in re.finditer(r'"path"\s*"([^"]+)"', vdf):
        out.append(m.group(1).replace("\\\\", "\\"))
    return out


def find_game_install() -> Path | None:
    """找到群星游戏本体目录。"""
    for root in _steam_roots():
        cand = root / "steamapps" / "common" / "Stellaris"
        if is_file(cand / STELLARIS_EXE) or is_dir(cand / "common"):
            return cand
        # 额外库
        lf = root / "steamapps" / "libraryfolders.vdf"
        if is_file(lf):
            try:
                vdf = read_text(lf)
            except OSError:
                vdf = ""
            for lib in _parse_library_folders(vdf):
                c2 = Path(lib) / "steamapps" / "common" / "Stellaris"
                if is_file(c2 / STELLARIS_EXE) or is_dir(c2 / "common"):
                    return c2
    return None


def find_workshop_dirs() -> list[Path]:
    """找到所有 Steam 创意工坊 281990 内容目录。"""
    out: list[Path] = []
    candidates: list[Path] = []
    for root in _steam_roots():
        candidates.append(root / "steamapps" / "workshop" / "content" / STELLARIS_APPID)
        lf = root / "steamapps" / "libraryfolders.vdf"
        if is_file(lf):
            try:
                vdf = read_text(lf)
            except OSError:
                vdf = ""
            for lib in _parse_library_folders(vdf):
                candidates.append(
                    Path(lib) / "steamapps" / "workshop" / "content" / STELLARIS_APPID
                )
    seen: set[str] = set()
    for c in candidates:
        k = str(c).lower()
        if k in seen:
            continue
        seen.add(k)
        if is_dir(c):
            out.append(c)
    return out


def find_user_dir() -> Path | None:
    """找到群星用户数据目录。"""
    for c in _candidate_user_dirs():
        if is_dir(c) and (is_dir(c / "logs") or is_file(c / "settings.txt")
                          or is_dir(c / "mod")):
            return c
    for c in _candidate_user_dirs():
        if is_dir(c):
            return c
    return None


def app_data_dir() -> Path:
    """本软件的数据目录。"""
    if sys.platform.startswith("win"):
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    p = Path(base) / APP_ID
    p.mkdir(parents=True, exist_ok=True)
    return p


@dataclass
class Paths:
    """一组解析后的关键路径。"""

    user_dir: str = ""
    game_dir: str = ""
    workshop_dirs: list[str] = field(default_factory=list)

    # ---- 派生路径 ------------------------------------------------
    @property
    def mod_dir(self) -> Path:
        """模组描述符目录（``用户数据/mod``）。"""
        return Path(self.user_dir) / "mod"

    @property
    def logs_dir(self) -> Path:
        """游戏日志目录。"""
        return Path(self.user_dir) / "logs"

    @property
    def crashes_dir(self) -> Path:
        """崩溃转储目录。"""
        return Path(self.user_dir) / "crashes"

    @property
    def dumps_dir(self) -> Path:
        """内存转储目录。"""
        return Path(self.user_dir) / "dumps"

    @property
    def launcher_db(self) -> Path:
        """Paradox 启动器数据库（playset 的真源）。"""
        return Path(self.user_dir) / "launcher-v2.sqlite"

    @property
    def dlc_load(self) -> Path:
        """``dlc_load.json`` —— 游戏运行时实际读取的启用清单。"""
        return Path(self.user_dir) / "dlc_load.json"

    @property
    def settings_txt(self) -> Path:
        """游戏设置文件。"""
        return Path(self.user_dir) / "settings.txt"

    @property
    def console_history(self) -> Path:
        """游戏的控制台历史文件。

        **位置在游戏本体目录，不在用户数据目录**（实测确认）：
        ``<game_dir>/console_history.txt``，纯 UTF-8 无 BOM，一行一条命令。

        这是通往游戏内控制台的**唯一可用通道**：
        游戏把它当作"按 ↑ 能翻出来的历史"，所以往里写什么，
        进游戏开控制台按 ↑ 就能翻到什么——不用改游戏、不用装 mod。
        """
        if not self.game_dir:
            return Path("")
        return Path(self.game_dir) / "console_history.txt"

    @property
    def exe(self) -> Path | None:
        """游戏可执行文件；找不到时返回 ``None``。"""
        if not self.game_dir:
            return None
        p = Path(self.game_dir) / STELLARIS_EXE
        return p if is_file(p) else None

    @property
    def workshop_dir(self) -> Path | None:
        """第一个存在的创意工坊内容目录。"""
        for d in self.workshop_dirs:
            if is_dir(d):
                return Path(d)
        return None

    def validate(self) -> list[str]:
        """返回问题列表（空表示一切正常）。"""
        problems: list[str] = []
        if not self.user_dir or not is_dir(self.user_dir):
            problems.append("未找到群星用户数据目录（Documents/Paradox Interactive/Stellaris）")
        elif not is_dir(self.mod_dir):
            problems.append(f"缺少 mod 目录：{self.mod_dir}")
        if not self.game_dir or not is_dir(self.game_dir):
            problems.append("未找到群星游戏本体目录")
        elif self.exe is None:
            problems.append(f"游戏目录下找不到 {STELLARIS_EXE}：{self.game_dir}")
        if not self.workshop_dir:
            problems.append("未找到 Steam 创意工坊内容目录（可能没有订阅任何模组）")
        return problems

    def to_dict(self) -> dict:
        """序列化为可写进配置文件的字典。"""
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Paths":
        """从配置字典还原。"""
        return cls(
            user_dir=d.get("user_dir", ""),
            game_dir=d.get("game_dir", ""),
            workshop_dirs=list(d.get("workshop_dirs") or []),
        )


@dataclass
class AiSettings:
    """AI 接入配置。

    隐私默认值很重要：``allow_send_mod_content`` 默认 **False**，
    意味着即使填了密钥，程序也不会擅自把模组内容发出去。
    """

    provider: str = "deepseek"
    api_key: str = ""
    base_url: str = ""              # 留空则用供应商默认地址
    model: str = ""
    temperature: float = 0.2
    timeout: int = 120
    max_tokens: int = 8192
    verify_ssl: bool = True
    allow_send_mod_content: bool = False   # 必须显式打开才会联网发送
    auto_verify: bool = True               # 生成后自动做语法/引用校验
    stream: bool = True                    # 流式输出
    last_test_ok: bool = False
    last_test_at: str = ""

    def to_dict(self) -> dict:
        """序列化 AI 配置（含密钥，仅用于写入本地配置文件）。"""
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "AiSettings":
        """从配置字典还原 AI 设置。"""
        s = cls()
        for k, v in (d or {}).items():
            if hasattr(s, k) and v is not None:
                setattr(s, k, v)
        return s

    @property
    def configured(self) -> bool:
        """是否已具备调用条件。"""
        from .ai.client import PROVIDERS
        spec = PROVIDERS.get(self.provider)
        if spec is None:
            return False
        if spec.needs_key and not self.api_key.strip():
            return False
        return bool(self.model or spec.default_model or spec.protocol == "openai")


@dataclass
class Settings:
    """用户配置。"""

    # 路径
    paths: Paths = field(default_factory=Paths)
    # 自动关机
    shutdown_enabled: bool = False
    shutdown_delay_seconds: int = 120
    shutdown_after_report: bool = True
    # 下次开机提醒
    boot_reminder_enabled: bool = True
    # 监控
    monitor_poll_seconds: float = 2.0
    monitor_memory_warn_mb: int = 7000
    monitor_workshop_stale_check: bool = True
    # 排序策略
    auto_apply_optimal_order: bool = True
    keep_translation_on_top: bool = True
    # AI
    ai: AiSettings = field(default_factory=AiSettings)
    # 界面
    language: str = "zh"
    theme: str = "dark"
    # 模组列表是否显示「幽灵记录」（已退订但启动器数据库还留着的空条目）
    show_ghost_mods: bool = False
    # 首次运行标记
    first_run_done: bool = False
    # 统计
    last_scan_epoch: float = 0.0

    @property
    def config_file(self) -> Path:
        """配置文件路径（``%LOCALAPPDATA%/StellarisModDoctor/config.json``）。"""
        return app_data_dir() / "config.json"

    def save_scan_epoch(self) -> None:
        """只更新磁盘上的扫描时间戳，**不动其它任何字段**。

        为什么要单独一个方法：``save()`` 会把内存里整份设置写回磁盘。
        而像 :meth:`App.start_ai_takeover` 这种地方只是想把"上次扫描时间"
        记下来，却因此把内存里**所有**改动一起持久化了——
        包括隐私开关 ``allow_send_mod_content``。

        这造成过一个很隐蔽的问题：测试在内存里打开隐私开关去跑打桩 AI，
        接管流程顺手存了一次时间戳，结果把开关**真的写进了用户的配置**，
        下次启动就是开着的。用户没同意过，软件却记住了。

        隐私开关这种字段只能由用户在界面上显式改动后落盘
        （见 ``webui.aiSet``），不能因为别的流程恰好存了一次配置就跟着变。
        """
        if not is_file(self.config_file):
            return
        try:
            raw = read_text(self.config_file, encoding="utf-8-sig")
            data = json.loads(raw)
        except (OSError, ValueError):
            # 读不到或坏了就整体存一次，至少别把时间戳丢了
            self.save()
            return
        data["last_scan_epoch"] = self.last_scan_epoch
        data["version"] = __version__
        text = json.dumps(data, ensure_ascii=False, indent=2)
        tmp = self.config_file.with_suffix(".json.tmp")
        try:
            write_text(tmp, text, bom=False)
            os.replace(long_path(tmp), long_path(self.config_file))
        except OSError:
            self.save()

    def save(self) -> None:
        """写入配置文件。

        写入前先把上一份**留成 .bak**：这个文件里有用户的 AI 供应商、
        模型名、路径等设置，一旦被默认值覆盖就得全部重填。
        实际发生过几次「跑完测试后模型名变回默认」，
        有 .bak 至少能立刻找回。

        文件里含 API Key，因此保存后会尝试把权限收紧到仅本人可读（``0600``）。
        """
        data = {
            "version": __version__,
            "paths": self.paths.to_dict(),
            "shutdown_enabled": self.shutdown_enabled,
            "shutdown_delay_seconds": self.shutdown_delay_seconds,
            "shutdown_after_report": self.shutdown_after_report,
            "boot_reminder_enabled": self.boot_reminder_enabled,
            "monitor_poll_seconds": self.monitor_poll_seconds,
            "monitor_memory_warn_mb": self.monitor_memory_warn_mb,
            "monitor_workshop_stale_check": self.monitor_workshop_stale_check,
            "auto_apply_optimal_order": self.auto_apply_optimal_order,
            "keep_translation_on_top": self.keep_translation_on_top,
            "ai": self.ai.to_dict(),
            "language": self.language,
            "theme": self.theme,
            "show_ghost_mods": self.show_ghost_mods,
            "first_run_done": self.first_run_done,
            "last_scan_epoch": self.last_scan_epoch,
        }
        # 覆盖前留一份上一版：配置里有 AI 供应商/模型/路径，
        # 被默认值盖掉要全部重填（真实发生过）。
        try:
            if is_file(self.config_file):
                shutil.copy2(long_path(self.config_file),
                             long_path(self.config_file.with_suffix(".json.bak")))
        except OSError:
            pass

        # 绊线：磁盘上的 AI 配置**和这次要写的不一样**时记一笔。
        #
        # 早先的判据是"旧的配过 AI 且新的正好等于内置默认值"，
        # 这个条件太窄：实测发生过 ollama/nemotron-3-nano → deepseek/
        # deepseek-chat 的覆盖，新值**不是**内置默认，于是绊线一声不吭，
        # 白排查了一轮。改判"AI 段是否真的变了"——
        # 只比较有意义的那几个字段，并附带调用栈。
        #
        # 只记日志、不阻止写入：用户确实可能去 AI 接入页改供应商。
        try:
            if is_file(self.config_file):
                old = self._read_config(self.config_file)
                old_ai = (old or {}).get("ai") or {}
                new_ai = data.get("ai") or {}
                keys = ("provider", "model", "base_url")
                changed = [(k, old_ai.get(k), new_ai.get(k)) for k in keys
                           if (old_ai.get(k) or "") != (new_ai.get(k) or "")]
                # 密钥从有到无也是丢配置，单独算一条
                if old_ai.get("api_key") and not new_ai.get("api_key"):
                    changed.append(("api_key", "(已设置)", "(被清空)"))
                if changed:
                    import traceback
                    pretty = "；".join(f"{k}: {a!r} → {b!r}"
                                      for k, a, b in changed)
                    warn = (f"AI 配置发生变更：{pretty}\n"
                            + "".join(traceback.format_stack()[-8:-1]))
                    self._save_warning = warn
                    try:
                        import time as _time
                        logp = (app_data_dir() / "logs"
                                / "config_overwrite.log")
                        logp.parent.mkdir(parents=True, exist_ok=True)
                        stamp = _time.strftime("%Y-%m-%d %H:%M:%S")
                        with open(logp, "a", encoding="utf-8") as fh:
                            fh.write(f"\n=== {stamp} ===\n{warn}\n")
                    except Exception:
                        pass
        except Exception:
            pass
        # 配置文件用**不带 BOM** 的 UTF-8 写。
        # 它是给人看、也给人手动改的（里面有 API Key、路径），
        # BOM 会让不少 JSON 解析器直接报
        # "Unexpected UTF-8 BOM"；游戏脚本那边才需要 BOM，别搞混。
        #
        # **先写临时文件再改名**（原子替换）：直接覆盖的话，写一半崩了/
        # 被中断就会留下一个截断的 JSON，下次启动解析失败 →
        # 静默退回默认值 → 用户的 AI 供应商和模型名全部丢失。
        # 改名在同一分区上是原子的，要么是旧的完整文件，要么是新的完整文件。
        text = json.dumps(data, ensure_ascii=False, indent=2)
        tmp = self.config_file.with_suffix(".json.tmp")
        try:
            write_text(tmp, text, bom=False)
            os.replace(long_path(tmp), long_path(self.config_file))
        except OSError:
            # 退路：改名失败就退回直接写，至少别把这次改动丢掉
            try:
                write_text(self.config_file, text, bom=False)
            except OSError:
                return
        # 里面有 API Key，尽量收紧权限。
        #
        # **诚实说明**：`os.chmod` 在 Windows 上只能切换只读属性，
        # 改不了 ACL。实测这个文件的 ACL 仍然是继承来的
        # 「自己 / SYSTEM / Administrators 完全控制」——
        # 也就是说这一句在 Windows 上基本是空操作，密钥靠的是
        # 它在 %LOCALAPPDATA%（别的普通用户读不到）而不是靠这里。
        #
        # 留着它是为了在真正支持 POSIX 权限的平台上（开发/CI 跑 Linux 时）
        # 确实生效；不要把它当成 Windows 上的安全保证。
        try:
            os.chmod(self.config_file, 0o600)
        except OSError:
            pass

    @classmethod
    def load(cls) -> "Settings":
        """读取配置；文件不存在或损坏时退回默认值。

        加载完成后会自动补全缺失的路径（不覆盖用户已手动指定的）。

        **损坏时会尝试从 ``.json.bak`` 恢复**：这个文件里存着 AI 供应商、
        模型名、路径等，静默退回默认值等于让用户重填一遍。
        备份是每次保存前留的，比"全默认"更接近用户真实想要的配置。
        """
        s = cls()
        cf = s.config_file
        d = cls._read_config(cf)
        if not isinstance(d, dict):
            # 主文件读不出来：试备份。但**只接受"确实有内容"的备份**——
            # 如果备份本身也只是一份默认配置，拿它来恢复毫无意义，
            # 反而会掩盖问题（默认值会让人以为"我的设置怎么没了"）。
            bak = cf.with_suffix(".json.bak")
            d2 = cls._read_config(bak)
            if isinstance(d2, dict) and cls._looks_configured(d2):
                d = d2
                try:
                    shutil.copy2(long_path(bak), long_path(cf))
                except OSError:
                    pass
        if isinstance(d, dict):
            d = cls._self_heal_ai(d, cf)
            s.paths = Paths.from_dict(d.get("paths") or {})
            s.ai = AiSettings.from_dict(d.get("ai") or {})
            for key in (
                "shutdown_enabled", "shutdown_delay_seconds",
                "shutdown_after_report", "boot_reminder_enabled",
                "monitor_poll_seconds", "monitor_memory_warn_mb",
                "monitor_workshop_stale_check", "auto_apply_optimal_order",
                "keep_translation_on_top", "language", "theme",
                "show_ghost_mods",
                "first_run_done", "last_scan_epoch",
            ):
                if key in d and d[key] is not None:
                    setattr(s, key, d[key])
        s.autodetect(missing_only=True)
        return s

    @classmethod
    def _self_heal_ai(cls, d: dict, cf: Path) -> dict:
        """AI 段莫名变成"出厂默认"时，从备份里把 AI 段捞回来。

        这个项目里 AI 配置被重置过多次（供应商从 ollama 变回 deepseek、
        模型名丢失），一直没定位到是谁写的。与其继续找，
        不如让**下一次读配置时自动修复**——用户不必再重填一遍。

        只在两个条件同时成立时才动手，避免误判：

        1. 当前配置里的 AI 段**完全是出厂默认**（供应商/模型/密钥/地址
           都是默认值）
        2. 备份里的 AI 段**确实配过**（换过供应商，或填过密钥/模型）

        主文件与备份都不可信时什么都不做。
        修复后会把结果写回主文件，省得每次启动都捞一遍。
        """
        try:
            ai = d.get("ai") or {}
            base = AiSettings().to_dict()
            if any(ai.get(k) != v for k, v in base.items()):
                return d                      # 已经配过，不用管
            bak = cls._read_config(cf.with_suffix(".json.bak"))
            if not isinstance(bak, dict):
                return d
            bai = bak.get("ai") or {}
            if all(bai.get(k) == v for k, v in base.items()):
                return d                      # 备份也是默认，捞不到东西
            # 备份的 AI 段确实配过 → 把 AI 段接回来
            d = dict(d)
            d["ai"] = bai
            try:
                text = json.dumps(d, ensure_ascii=False, indent=2)
                tmp = cf.with_suffix(".json.tmp")
                write_text(tmp, text, bom=False)
                os.replace(long_path(tmp), long_path(cf))
            except OSError:
                pass
            return d
        except Exception:
            return d

    @staticmethod
    def _looks_configured(d: dict) -> bool:
        """这份配置（``to_dict()`` 那种结构）里有没有"用户真的设置过"的痕迹。

        用来判断备份值不值得拿来恢复。

        做法是**和真正的默认值逐字段比对**，而不是猜"某个字段非空就算配过"——
        默认配置本身就带着 provider=deepseek、model=deepseek-chat，
        "非空即算"会把一份纯默认值误判成用户配置，
        恢复过来等于什么也没救回来（实测踩过）。

        只要有任何一项偏离默认，就认为用户动过设置。
        """
        try:
            base = AiSettings().to_dict()
        except Exception:
            base = {}
        ai = d.get("ai")
        if isinstance(ai, dict):
            for k, v in ai.items():
                # api_key 非空一定是用户填的
                if k == "api_key" and v:
                    return True
                if k not in base:
                    if v:
                        return True
                    continue
                if v != base[k]:
                    return True
        paths = d.get("paths")
        if isinstance(paths, dict):
            # 路径只要有一项非空就算（默认全是空串）
            if any(v for k, v in paths.items() if k != "version"):
                return True
        if d.get("language") not in (None, "", "zh"):
            return True
        if d.get("theme") not in (None, "", "stellaris"):
            return True
        return False

    @staticmethod
    def _read_config(path: Path) -> "Any | None":
        """读一个配置文件；不存在、读不了、不是合法 JSON 都返回 None。"""
        try:
            if not is_file(path):
                return None
            return json.loads(read_text(path))
        except (OSError, ValueError):
            return None

    def autodetect(self, missing_only: bool = True) -> list[str]:
        """自动探测路径。返回本次实际补上的字段名。"""
        fixed: list[str] = []
        if not missing_only or not self.paths.user_dir or not is_dir(self.paths.user_dir):
            u = find_user_dir()
            if u and str(u) != self.paths.user_dir:
                self.paths.user_dir = str(u)
                fixed.append("user_dir")
        if not missing_only or not self.paths.game_dir or not is_dir(self.paths.game_dir):
            g = find_game_install()
            if g and str(g) != self.paths.game_dir:
                self.paths.game_dir = str(g)
                fixed.append("game_dir")
        w = find_workshop_dirs()
        cur = {str(x).lower() for x in self.paths.workshop_dirs}
        new = [str(x) for x in w if str(x).lower() not in cur]
        if new:
            self.paths.workshop_dirs = list(self.paths.workshop_dirs) + new
            fixed.append("workshop_dirs")
        return fixed


def default_settings() -> Settings:
    """构造一份已完成路径自动探测的默认配置。"""
    s = Settings()
    s.autodetect(missing_only=False)
    return s
