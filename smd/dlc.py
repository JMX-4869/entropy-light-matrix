"""DLC 系统 —— 把扩展功能从主程序里分出去。

**为什么要 DLC**

主程序是 MIT。要做的 AI 代打那一块底下压着 GPL-3.0 的代码（OGAS 的命令管线）。
把两堆代码放进同一个二进制，整个项目就得改协议；分成独立进程加载，边界就干净了。

顺带还解决三件事：

* 版本节奏 —— DLC 跟着游戏版本走，主程序按自己的节奏发
* 装不装都一样 —— 不装 DLC 就是原来的排查工具，一行代码都不多
* 可扩展 —— 以后别的功能也走这套，不用改主程序

**目录约定**

::

    %LOCALAPPDATA%\\StellarisModDoctor\\dlc\\
        singularity-protocol\\
            manifest.json
            engine\\...
            runtime\\...

启动器只认 ``manifest.json``，不认里面的实现。

**manifest 规范**::

    {
      "schema": "smd.dlc.v1",
      "id": "singularity-protocol",
      "name": "Singularity Protocol",
      "name_zh": "奇点协议",
      "version": "0.1.0",
      "description_zh": "把你的帝国交给一个系统去跑",
      "requires": { "app": ">=1.0.7", "game": ">=4.4,<4.6" },
      "entry": "runtime/ai-runtime.exe",
      "engine": "engine/ogas-engine.exe",
      "license": "GPL-3.0",
      "elevation": "required",
      "capabilities": ["build", "research", "fleet"]
    }
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import __version__

# manifest 的 schema 标识。以后不兼容的改动换 v2
SCHEMA = "smd.dlc.v1"


def dlc_root() -> Path:
    """DLC 根目录（和用户数据同层，卸载主程序默认保留）。"""
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    p = Path(base) / "StellarisModDoctor" / "dlc"
    p.mkdir(parents=True, exist_ok=True)
    return p


# ---------------------------------------------------------------- 版本比较


_VER = re.compile(r"(\d+)\.(\d+)\.(\d+)")


def _ver_tuple(s: str) -> tuple[int, int, int]:
    """把一个版本串压成三元组。解析不出来就当 0.0.0。

    不用 packaging 库：主程序要打包成单文件 exe，能少一个依赖就少一个，
    而且我们只用得到 ``x.y.z`` 这一种形式。
    """
    m = _VER.search(str(s))
    if not m:
        return (0, 0, 0)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def check_constraint(current: str, expr: str) -> bool:
    """检查 ``current`` 是否满足 ``expr``。

    ``expr`` 支持逗号分隔的多个条件，全部满足才算过::

        ">=1.0.7"          当前版本不低于 1.0.7
        ">=4.4,<4.6"       区间
        ""                 空 = 不限制
    """
    expr = (expr or "").strip()
    if not expr:
        return True
    cur = _ver_tuple(current)
    for part in expr.split(","):
        part = part.strip()
        if not part:
            continue
        m = re.match(r"(>=|<=|>|<|==|=)?\s*(.+)$", part)
        if not m:
            continue
        op = m.group(1) or "=="
        want = _ver_tuple(m.group(2))
        if op == ">=" and not (cur >= want):
            return False
        if op == "<=" and not (cur <= want):
            return False
        if op == ">" and not (cur > want):
            return False
        if op == "<" and not (cur < want):
            return False
        if op in ("==", "=") and not (cur == want):
            return False
    return True


# ---------------------------------------------------------------- 数据


@dataclass
class DLC:
    """一个已安装的 DLC。

    ``ok`` 和 ``reason`` 是**算出来的**，不是 manifest 里写的 ——
    启动器每次扫描都重算一遍，所以 DLC 升级后不用改主程序。
    """

    id: str
    name: str
    name_zh: str
    version: str
    description_zh: str = ""
    requires: dict = field(default_factory=dict)
    entry: str = ""
    engine: str = ""
    license: str = ""
    elevation: str = "none"
    capabilities: list = field(default_factory=list)
    path: Path | None = None

    # 运行时状态（不来自 manifest）
    ok: bool = True
    reason: str = ""
    pid: int = 0

    def to_dict(self) -> dict:
        """给前端的状态快照。"""
        return {
            "id": self.id,
            "name": self.name,
            "name_zh": self.name_zh,
            "version": self.version,
            "description_zh": self.description_zh,
            "license": self.license,
            "elevation": self.elevation,
            "capabilities": list(self.capabilities),
            "path": str(self.path or ""),
            "ok": self.ok,
            "reason": self.reason,
            "running": self.is_running(),
            "pid": self.pid,
        }

    def entry_path(self) -> Path | None:
        """主入口 exe 的绝对路径。"""
        if not self.path or not self.entry:
            return None
        p = (self.path / self.entry).resolve()
        # 不允许跳出 DLC 自己的目录 —— manifest 是外部文件，不能让它指哪打哪
        try:
            p.relative_to(self.path.resolve())
        except ValueError:
            return None
        return p

    def is_running(self) -> bool:
        """进程还活着吗。

        用 ``os.kill(pid, 0)`` 在 Windows 上不可靠（会真发信号），
        所以走 tasklist。失败就当没在跑 —— 宁可显示"未运行"让人重开，
        也不要显示"运行中"却点不动。
        """
        if not self.pid:
            return False
        try:
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {self.pid}", "/NH", "/FO", "CSV"],
                capture_output=True, text=True, timeout=10,
                creationflags=0x08000000,
            ).stdout or ""
        except (OSError, subprocess.SubprocessError):
            return False
        return str(self.pid) in out


# ---------------------------------------------------------------- 扫描


def _parse_manifest(f: Path) -> DLC | None:
    """读一个 manifest.json。格式不对就返回 None（跳过这个 DLC）。"""
    try:
        raw = json.loads(f.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    if raw.get("schema") != SCHEMA:
        return None
    did = str(raw.get("id") or "").strip()
    if not did:
        return None
    return DLC(
        id=did,
        name=str(raw.get("name") or did),
        name_zh=str(raw.get("name_zh") or raw.get("name") or did),
        version=str(raw.get("version") or "0.0.0"),
        description_zh=str(raw.get("description_zh") or ""),
        requires=dict(raw.get("requires") or {}),
        entry=str(raw.get("entry") or ""),
        engine=str(raw.get("engine") or ""),
        license=str(raw.get("license") or ""),
        elevation=str(raw.get("elevation") or "none"),
        capabilities=list(raw.get("capabilities") or []),
        path=f.parent,
    )


def discover(game_version: str = "") -> list[DLC]:
    """扫描 DLC 目录，算出每个的可用性。

    ``game_version`` 从存档或游戏本体读出来；空字符串表示跳过游戏版本检查
    （读不到版本时不该把 DLC 判死）。
    """
    out: list[DLC] = []
    root = dlc_root()
    for d in sorted(root.iterdir() if root.is_dir() else []):
        if not d.is_dir():
            continue
        mf = d / "manifest.json"
        if not mf.is_file():
            continue
        dlc = _parse_manifest(mf)
        if dlc is None:
            continue

        # 版本与文件检查 —— 每次扫描重算，不缓存
        problems: list[str] = []
        want_app = str((dlc.requires or {}).get("app") or "")
        if want_app and not check_constraint(__version__, want_app):
            problems.append(f"需要主程序 {want_app}，当前 {__version__}")
        want_game = str((dlc.requires or {}).get("game") or "")
        if want_game and game_version and not check_constraint(
                game_version, want_game):
            problems.append(f"需要游戏 {want_game}，当前 {game_version}")
        ep = dlc.entry_path()
        if ep is None:
            problems.append("manifest 里的入口路径不合法")
        elif not ep.is_file():
            problems.append(f"入口文件缺失：{dlc.entry}")
        dlc.ok = not problems
        dlc.reason = "；".join(problems)
        out.append(dlc)
    return out


def find(dlc_id: str, game_version: str = "") -> DLC | None:
    """按 id 找一个 DLC。"""
    for d in discover(game_version):
        if d.id == dlc_id:
            return d
    return None


# ---------------------------------------------------------------- 调度


def launch(dlc: DLC, args: list[str] | None = None,
           env_extra: dict[str, str] | None = None) -> dict[str, Any]:
    """起 DLC 进程。

    ``elevation == "required"`` 时用 ``ShellExecute`` 走 UAC —— 普通
    ``CreateProcess`` 拿不到管理员权限，pydivert 会直接失败。

    返回 ``{ok, pid, message}``，不抛异常：调用方是界面，扔异常过去就是
    "点了没反应"。
    """
    if not dlc.ok:
        return {"ok": False, "pid": 0, "message": dlc.reason or "DLC 不可用"}
    ep = dlc.entry_path()
    if ep is None or not ep.is_file():
        return {"ok": False, "pid": 0, "message": "入口文件不存在"}
    if dlc.is_running():
        return {"ok": True, "pid": dlc.pid, "message": "已经在运行"}

    cmd = [str(ep), *(args or [])]
    env = dict(os.environ)
    env.update(env_extra or {})
    # 把主程序知道的事实传过去，DLC 不用自己再猜一遍
    env["SMD_APP_VERSION"] = __version__
    env["SMD_DLC_DIR"] = str(dlc.path)
    env["SMD_DATA_DIR"] = str(Path(os.environ.get("LOCALAPPDATA")
                                   or os.path.expanduser("~"))
                              / "StellarisModDoctor")

    try:
        if dlc.elevation == "required" and sys.platform.startswith("win"):
            # ShellExecute "runas" 会弹 UAC。它不返回 PID ——
            # 所以起完之后靠 tasklist 按 exe 名找回来。
            import ctypes
            r = ctypes.windll.shell32.ShellExecuteW(
                None, "runas", str(ep), " ".join(f'"{a}"' for a in (args or [])),
                str(ep.parent), 1)
            if r <= 32:
                return {"ok": False, "pid": 0,
                        "message": "提权被拒绝或启动失败（UAC）"}
            time.sleep(1.5)
            pid = _find_pid_by_name(ep.name)
            dlc.pid = pid
            return {"ok": bool(pid), "pid": pid,
                    "message": "已提权启动" if pid else "已提权，未能确认进程"}

        proc = subprocess.Popen(cmd, cwd=str(ep.parent), env=env,
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                                creationflags=0x08000000)
        dlc.pid = proc.pid
        return {"ok": True, "pid": proc.pid, "message": "已启动"}
    except OSError as exc:
        return {"ok": False, "pid": 0, "message": f"启动失败：{exc}"}


def _find_pid_by_name(exe_name: str) -> int:
    """按 exe 名找 PID。ShellExecute 不给 PID，只能反查。"""
    try:
        out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {exe_name}",
                              "/NH", "/FO", "CSV"],
                             capture_output=True, text=True, timeout=10,
                             creationflags=0x08000000).stdout or ""
    except (OSError, subprocess.SubprocessError):
        return 0
    m = re.search(r'"[^"]+","(\d+)"', out)
    return int(m.group(1)) if m else 0


def stop(dlc: DLC) -> dict[str, Any]:
    """停 DLC 进程。"""
    if not dlc.is_running():
        dlc.pid = 0
        return {"ok": True, "message": "本来就没在运行"}
    try:
        subprocess.run(["taskkill", "/PID", str(dlc.pid), "/T", "/F"],
                       capture_output=True, timeout=15,
                       creationflags=0x08000000)
        dlc.pid = 0
        return {"ok": True, "message": "已停止"}
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "message": f"停止失败：{exc}"}
