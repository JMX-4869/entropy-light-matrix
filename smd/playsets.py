"""播放集发现：把各家启动器的播放集汇总成一份，并标出"同一套"。

## 为什么需要这一层

《群星》的播放集不止存在一个地方：

* **官方启动器**（Paradox Launcher v2）—— ``launcher-v2.sqlite`` 的
  ``playsets`` / ``playsets_mods`` 两张表
* **Irony Mod Manager**（第三方）—— ``%APPDATA%\\<X>\\IronyModManager\\
  Database_<ver>.json``，一个 JSON 数组，其中 ``Name=ModCollection``
  那一项的 ``Value`` 是播放集列表

而这两处**对同一套配置的叫法和数量都可能不同**。实测本机：

    官方库：「IronyModManager」 74 条，74 启用
    Irony ：名字就叫「1」       76 条，IsSelected=True

只看一边，用户会以为有两个播放集；只看官方库，会以为播放集叫
"IronyModManager"（其实是 Irony 同步过去时起的名字）。
所以这一层的职责是：**都列出来，并算出谁和谁是同一套。**

## 用什么断定"同一套"

用**工坊 id 集合的重合度**，不用名字 —— 名字会变（用户能重命名、
不同工具起不同名字），id 不会。判据：交集占较小集合的比例 ≥ 0.8。
（不要求完全相等：Irony 可能多几个它自己生成的补丁 mod，
官方库可能少几个还没同步过去的。）
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import Paths
from .paths import is_file, long_path

# 来源标识与显示名
SRC_PARADOX = "paradox"
SRC_IRONY = "irony"

SOURCE_LABELS = {
    SRC_PARADOX: "官方启动器",
    SRC_IRONY: "Irony Mod Manager",
}


@dataclass
class Playset:
    """一个播放集（来自某个具体来源）。"""

    source: str                       # paradox / irony
    id: str                           # 该来源内部的 id
    name: str                         # 显示名
    mods: int = 0                     # 条目数
    enabled: int = 0                  # 启用数
    active: bool = False              # 在该来源内是否活动
    steam_ids: set[str] = field(default_factory=set)
    # 有序的工坊 id 列表。**顺序有意义** —— Irony 的 ModIds 列表
    # 就是它的加载顺序；官方库那边用 position 表达，这里只在 Irony 侧填。
    steam_ordered: list[str] = field(default_factory=list)
    # 与其它来源里"同一套"的项：[(source, name), …]
    same_as: list[tuple[str, str]] = field(default_factory=list)
    note: str = ""                    # 补充说明（如"未同步到官方库"）

    @property
    def label(self) -> str:
        """来源的显示名（"官方启动器" / "Irony Mod Manager"）。"""
        return SOURCE_LABELS.get(self.source, self.source)

    @property
    def key(self) -> str:
        """全局唯一键：来源 + 内部 id。界面用它做选择。"""
        return f"{self.source}:{self.id}"

    def to_dict(self) -> dict[str, Any]:
        """序列化成界面/JSON 能直接用的形状。

        刻意**不**输出 `steam_ids` 集合本身（那是内部用来算对应的），
        但输出有序的 `steam_ids_ordered` —— `load_library` 要按它
        还原 Irony 播放集的加载顺序。
        """
        return {
            "key": self.key,
            "source": self.source,
            "source_label": self.label,
            "id": self.id,
            "name": self.name,
            "mods": self.mods,
            "enabled": self.enabled,
            "active": self.active,
            "steam_ids_ordered": list(self.steam_ordered),
            "same_as": [{"source": s, "source_label": SOURCE_LABELS.get(s, s),
                         "name": n} for s, n in self.same_as],
            "note": self.note,
        }


# ---------------------------------------------------------------------------
# 官方启动器
# ---------------------------------------------------------------------------

def _paradox_playsets(paths: Paths) -> list[Playset]:
    """从 launcher-v2.sqlite 读播放集。"""
    out: list[Playset] = []
    db = paths.launcher_db
    if not is_file(db):
        return out
    try:
        con = sqlite3.connect(f"file:{long_path(db)}?mode=ro", uri=True)
    except sqlite3.Error:
        try:
            con = sqlite3.connect(long_path(db))
        except sqlite3.Error:
            return out
    try:
        con.row_factory = sqlite3.Row
        cur = con.cursor()
        try:
            cur.execute("SELECT id,name,isActive FROM playsets")
            rows = cur.fetchall()
        except sqlite3.Error:
            return out
        for r in rows:
            pid = str(r["id"] or "")
            if not pid:
                continue
            ps = Playset(source=SRC_PARADOX, id=pid,
                         name=str(r["name"] or pid),
                         active=bool(r["isActive"]))
            # 条目数 + 启用的 steamId 集合
            try:
                cur.execute(
                    "SELECT pm.enabled, m.steamId FROM playsets_mods pm "
                    "LEFT JOIN mods m ON m.id = pm.modId "
                    "WHERE pm.playsetId = ?", (pid,))
                for row in cur.fetchall():
                    ps.mods += 1
                    if row["enabled"]:
                        ps.enabled += 1
                    sid = str(row["steamId"] or "").strip()
                    if sid:
                        ps.steam_ids.add(sid)
            except sqlite3.Error:
                pass
            out.append(ps)
    finally:
        try:
            con.close()
        except Exception:                     # noqa: BLE001
            pass
    return out


# ---------------------------------------------------------------------------
# Irony Mod Manager
# ---------------------------------------------------------------------------

def _irony_db_files() -> list[Path]:
    """找 Irony 的数据库文件。

    路径形如 ``%APPDATA%\\<X>\\IronyModManager\\Database_1.27.json``。
    中间那层 ``<X>`` 在不同机器上不一样（实测是 ``Mario``），所以
    **不能写死**，扫一层通配。可能的根目录两个都试。
    """
    import os

    roots: list[Path] = []
    for env in ("APPDATA", "LOCALAPPDATA"):
        base = os.environ.get(env)
        if base:
            roots.append(Path(base))
    found: list[Path] = []
    for base in roots:
        # 一层通配：%APPDATA%/*/IronyModManager/Database_*.json
        try:
            for mid in base.iterdir():
                if not mid.is_dir():
                    continue
                d = mid / "IronyModManager"
                if not d.is_dir():
                    continue
                found.extend(sorted(d.glob("Database_*.json")))
        except OSError:
            continue
        # 也试试直接放在根下（不同版本布局）
        d2 = base / "IronyModManager"
        if d2.is_dir():
            found.extend(sorted(d2.glob("Database_*.json")))
    # 同一来源可能有多个版本文件，按修改时间取最新的一份
    if not found:
        return []
    found.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return found


def _irony_playsets() -> list[Playset]:
    """从 Irony Mod Manager 的数据库 JSON 里读播放集。

    Irony 存的是一个数组，每项形如::

        {"Type": "...", "Name": "ModCollection", "Value": [ …播放集… ]}

    播放集里有 ``Name`` / ``Game`` / ``IsSelected`` / ``ModIds``（每项
    ``{ParadoxId, SteamId}``）/ ``ModNames`` / ``ModPaths``。

    只取 ``Game == "Stellaris"`` 的 —— Irony 也管别的 P 社游戏。
    """
    out: list[Playset] = []
    for db in _irony_db_files():
        try:
            raw = json.loads(db.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        if not isinstance(raw, list):
            continue
        coll: list[Any] = []
        for item in raw:
            if isinstance(item, dict) and item.get("Name") == "ModCollection":
                coll = item.get("Value") or []
                break
        if not isinstance(coll, list):
            continue
        for i, c in enumerate(coll):
            if not isinstance(c, dict):
                continue
            game = str(c.get("Game") or "")
            if game and game.lower() != "stellaris":
                continue
            ids = c.get("ModIds") or []
            steam: set[str] = set()
            for x in ids:
                if isinstance(x, dict):
                    sid = str(x.get("SteamId") or "").strip()
                    if sid and sid != "0":
                        steam.add(sid)
                elif isinstance(x, str) and x.isdigit():
                    steam.add(x)
            ps = Playset(
                source=SRC_IRONY,
                # Irony 的播放集没有稳定 id（实测 Id=None），用序号
                id=f"{db.stem}#{i}",
                name=str(c.get("Name") or f"播放集 {i + 1}"),
                mods=len(ids) or len(c.get("ModNames") or []),
                enabled=len(ids),      # Irony 的集合里等于"参与排序的"
                active=bool(c.get("IsSelected")),
                steam_ids=steam,
                steam_ordered=[
                    str(x.get("SteamId") or "").strip()
                    for x in ids if isinstance(x, dict)
                    and str(x.get("SteamId") or "").strip() not in ("", "0")
                ],
            )
            if not ps.mods:
                ps.mods = len(c.get("ModNames") or [])
            out.append(ps)
        if out:
            break                  # 用最新那份数据库就够了
    return out


# ---------------------------------------------------------------------------
# 对应关系
# ---------------------------------------------------------------------------

def _match(playlists: list[Playset], *, threshold: float = 0.8) -> None:
    """算出哪些播放集是"同一套"，写回各自的 ``same_as``。

    用 **工坊 id 集合的重合度**，不用名字：名字会变，id 不会。
    判据是交集占**较小集合**的比例 ≥ threshold，并且交集不为空。
    用较小集合做分母，是为了让"一边多几个 mod"仍能认成同一套。
    """
    for i, a in enumerate(playlists):
        for b in playlists[i + 1:]:
            if a.source == b.source:
                continue
            if not a.steam_ids or not b.steam_ids:
                continue
            inter = a.steam_ids & b.steam_ids
            if not inter:
                continue
            smaller = min(len(a.steam_ids), len(b.steam_ids))
            if smaller and len(inter) / smaller >= threshold:
                a.same_as.append((b.source, b.name))
                b.same_as.append((a.source, a.name))


def discover(paths: Paths) -> list[dict[str, Any]]:
    """返回所有来源的播放集（已标注对应关系），按"活动优先、来源、名字"排。

    每项都是可直接给界面的 dict（见 :meth:`Playset.to_dict`）。
    """
    items: list[Playset] = []
    items.extend(_paradox_playsets(paths))
    items.extend(_irony_playsets())
    _match(items)

    # 给"只在第三方里存在"的补一句说明 —— 否则用户会问
    # "为什么官方启动器里没有这个"
    for p in items:
        if p.source == SRC_IRONY and not any(s == SRC_PARADOX for s, _ in p.same_as):
            p.note = "还没同步到官方启动器"

    items.sort(key=lambda p: (not p.active, p.source, p.name.lower()))
    return [p.to_dict() for p in items]


def find(paths: Paths, key: str) -> dict[str, Any] | None:
    """按全局键（``source:id``）找一项。"""
    for item in discover(paths):
        if item["key"] == key:
            return item
    return None
