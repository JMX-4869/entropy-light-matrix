"""模组启用开关 与 DLC 管理。

两个功能共享同一套「暂存 → 预览 → 写入」流程：

* **模组开关** —— 在模组列表里用滑块勾选要加载的模组。
  改动先进暂存区，点「应用」才真正写盘，避免手滑就把几十个模组关掉。
* **DLC 管理** —— 读取游戏 ``dlc/`` 下的 ``.dlc`` 描述符，列出全部 DLC
  与所属类别，可以单独开关。

写入目标
--------
``dlc_load.json`` —— **这是游戏真正读取的清单**（``enabled_mods``
与 ``disabled_dlcs``）。可选同步 Paradox 启动器的 ``launcher-v2.sqlite``，
这样下次开启动器时界面里勾选状态是对的；启动器在跑的时候会跳过，
因为它会把我们的改动覆盖掉。
"""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .config import Paths
from .paths import (decode_text, is_dir, is_file, long_path, norm, read_text,
                    write_text)
from .paradox import parse_string

DLC_LOAD = "dlc_load.json"


# ---------------------------------------------------------------------------
# 模组开关
# ---------------------------------------------------------------------------

@dataclass
class SwitchPlan:
    """模组开关的暂存计划。"""

    # mod key -> 期望的启用状态
    desired: dict[str, bool] = field(default_factory=dict)

    def set(self, key: str, enabled: bool) -> None:
        """暂存某个模组的期望开关状态。"""
        self.desired[key] = bool(enabled)

    def get(self, key: str, current: bool) -> bool:
        """取某个模组的期望状态（没改过就返回当前状态）。"""
        return self.desired.get(key, current)

    def clear(self) -> None:
        """丢弃全部暂存改动。"""
        self.desired.clear()

    def changes(self, library: Any) -> list[dict]:
        """相对当前状态的实际变化。

        **只统计磁盘上有内容（可加载）的模组**——这是唯一权威过滤点。
        磁盘上没内容的模组写了也白写，游戏读不到任何文件，
        让它出现在「待写入」里只会让计数和用户看到的不一致
        （例如可加载 76 个，提示却写 77 处改动）。

        调用方（界面、批量操作）可能各自做过一次可加载判断，
        但文件系统状态会变（工坊更新、Steam 清理），
        多次判断可能不一致；所以这里再收一次口，保证计数自洽。
        """
        out: list[dict] = []
        for key, want in self.desired.items():
            m = library.get(key) if library is not None else None
            if m is None:
                continue
            if bool(m.enabled_in_playset) == bool(want):
                continue
            if want and not m.is_loadable():
                continue          # 想启用但磁盘上没东西：做不到，不算变化
            out.append({
                "key": key, "name": m.name, "enable": bool(want),
                "registry": m.game_registry_id,
                "remote_file_id": m.remote_file_id,
                "path_ok": bool(m.resolve_content_path()),
            })
        return out

    def summary(self, library: Any) -> dict:
        """汇总待写入的变化（供界面提示条与写盘前确认）。"""
        ch = self.changes(library)
        on = [c for c in ch if c["enable"]]
        off = [c for c in ch if not c["enable"]]
        return {
            "pending": len(ch),
            "to_enable": len(on),
            "to_disable": len(off),
            "enable_names": [c["name"] for c in on][:20],
            "disable_names": [c["name"] for c in off][:20],
            "missing_files": [c["name"] for c in ch if not c["path_ok"]],
        }


def registry_ref(mod: Any) -> str:
    """算出这个模组在 ``dlc_load.json`` 里的条目。

    工坊模组用 ``mod/ugc_<id>.mod``；本地模组用描述符的实际文件名。
    """
    reg = (mod.game_registry_id or "").replace("\\", "/")
    if reg:
        return reg
    if mod.remote_file_id:
        return f"mod/ugc_{mod.remote_file_id}.mod"
    if mod.mod_file:
        return f"mod/{Path(mod.mod_file).name}"
    return ""


def apply_mod_switches(paths: Paths, library: Any, plan: SwitchPlan, *,
                       sync_launcher: bool = True,
                       launcher_running: bool = False) -> dict:
    """把模组开关写入群星。

    **两个地方都要写，缺一不可：**

    * ``launcher-v2.sqlite`` 的 ``playsets_mods.enabled``
      —— **权威来源**。Paradox 启动器每次启动都会按它重建
      ``dlc_load.json``。只改 JSON 的话，下次开启动器就被覆盖回去。
    * ``dlc_load.json`` 的 ``enabled_mods``
      —— 游戏实际读取的清单，启动器不在时也生效。

    早期版本只写了 JSON，结果「关闭模组」在重新读取时仍显示为启用
    （因为库里的状态没变），测试抓到了这个不一致。

    启动器正在运行时会跳过写库（它会锁文件并在退出时覆盖），
    并在返回的 ``notes`` 里说明。
    """
    changes = plan.changes(library)
    if not changes:
        return {"ok": True, "changed": 0, "added": [], "removed": [],
                "db_synced": False, "notes": ["没有需要写入的变化"]}

    notes: list[str] = []
    db_synced = False
    db_skipped_reason = ""

    # ---- 1. 先写权威来源：启动器数据库 ----
    if sync_launcher:
        if launcher_running:
            db_skipped_reason = "Paradox 启动器正在运行"
        else:
            db_synced, msg = _sync_launcher_db(paths, library, changes)
            if not db_synced:
                db_skipped_reason = msg
    else:
        db_skipped_reason = "已按设置跳过同步启动器数据库"
    if db_skipped_reason:
        notes.append(f"未写入启动器数据库（{db_skipped_reason}）。"
                     f"下次打开 Paradox 启动器时，"
                     f"它可能按自己的记录重建模组清单。")

    # ---- 2. 再写游戏实际读取的清单 ----
    p = paths.dlc_load
    data: dict[str, Any] = {"disabled_dlcs": [], "enabled_mods": []}
    if is_file(p):
        try:
            loaded = json.loads(read_text(p))
            if isinstance(loaded, dict):
                data.update(loaded)
        except (OSError, ValueError):
            pass
    data.setdefault("disabled_dlcs", [])
    current = [str(x) for x in data.get("enabled_mods", [])]
    current_norm = {norm(x) for x in current}

    added: list[str] = []
    removed: list[str] = []
    for c in changes:
        m = library.get(c["key"]) if library is not None else None
        ref = registry_ref(m) if m is not None else ""
        if not ref:
            continue
        if c["enable"]:
            if norm(ref) not in current_norm:
                current.append(ref)
                current_norm.add(norm(ref))
                added.append(c["name"])
        else:
            if norm(ref) in current_norm:
                current = [x for x in current if norm(x) != norm(ref)]
                current_norm.discard(norm(ref))
                removed.append(c["name"])

    data["enabled_mods"] = current
    try:
        # bom=False：这是标准 JSON，游戏与启动器都按标准 JSON 解析。
        # 原文件没有 BOM，加上会让严格的解析器报错。
        write_text(p, json.dumps(data, ensure_ascii=False,
                                 separators=(",", ":")), bom=False)
    except OSError as exc:
        return {"ok": False, "error": f"写入 {DLC_LOAD} 失败：{exc}",
                "db_synced": db_synced, "notes": notes}

    notes.insert(0, f"{DLC_LOAD}：启用 {len(current)} 个模组")
    if db_synced:
        notes.append("启动器数据库已同步，下次打开启动器勾选状态也是对的。")

    return {"ok": True, "changed": len(changes), "added": added,
            "removed": removed, "db_synced": db_synced,
            "enabled_total": len(current), "notes": notes}


def _sync_launcher_db(paths: Paths, library: Any, changes: list[dict]
                      ) -> tuple[bool, str]:
    """把开关状态同步到 launcher-v2.sqlite 的 playsets_mods。

    启动器的勾选状态存在这里。不同步的话，下次打开启动器看到的还是旧状态，
    用户一保存就把我们的改动冲掉了。
    """
    db = paths.launcher_db
    if not is_file(db):
        return False, "未找到启动器数据库，已跳过同步"
    try:
        con = sqlite3.connect(long_path(db), timeout=20)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA busy_timeout=20000")
        cur = con.cursor()
        cur.execute("SELECT id FROM playsets WHERE isActive=1 LIMIT 1")
        row = cur.fetchone()
        if not row:
            con.close()
            return False, "数据库里没有启用的 playset，已跳过同步"
        pid = row["id"]

        key_to_id: dict[str, str] = {}
        cur.execute("SELECT id,steamId,gameRegistryId,displayName,name FROM mods")
        for r in cur.fetchall():
            sid = str(r["steamId"] or "").strip()
            if sid:
                key_to_id[f"ugc:{sid}"] = r["id"]
            reg = norm(str(r["gameRegistryId"] or ""))
            if reg:
                key_to_id[f"reg:{reg}"] = r["id"]
            nm = norm(str(r["displayName"] or r["name"] or ""))
            if nm:
                key_to_id[f"name:{nm}"] = r["id"]

        updated = 0
        for c in changes:
            m = library.get(c["key"]) if library is not None else None
            if m is None:
                continue
            mid = key_to_id.get(c["key"])
            if mid is None and m.game_registry_id:
                mid = key_to_id.get(f"reg:{norm(m.game_registry_id)}")
            if mid is None and m.name:
                mid = key_to_id.get(f"name:{norm(m.name)}")
            if mid is None:
                continue
            cur.execute(
                "UPDATE playsets_mods SET enabled=? WHERE playsetId=? AND modId=?",
                (1 if c["enable"] else 0, pid, mid))
            updated += cur.rowcount
        con.commit()
        con.close()
        return True, f"启动器数据库已同步（{updated} 条）"
    except sqlite3.Error as exc:
        return False, f"同步启动器数据库失败：{exc}"


# ---------------------------------------------------------------------------
# DLC 管理
# ---------------------------------------------------------------------------

# 类别 -> 中文说明
CATEGORY_LABEL = {
    "expansion": "大型资料片",
    "content_pack": "内容包",
    "species_pack": "种族包",
    "story_pack": "故事包",
    "music_pack": "音乐包",
    "portrait_pack": "肖像包",
    "utilities": "工具",
    "": "其它",
}


@dataclass
class DlcInfo:
    """一个 DLC。"""

    key: str = ""                 # 文件夹名（稳定主键）
    name: str = ""                # .dlc 里的 name（游戏内部标识）
    localizable: str = ""         # 本地化键
    archive: str = ""
    steam_id: str = ""
    category: str = ""
    affects_checksum: bool = False
    affects_compat: bool = False  # 关掉它会不会影响模组兼容性
    thumbnail: str = ""
    enabled: bool = True
    is_expansion: bool = False

    @property
    def category_label(self) -> str:
        """类别的中文显示名。"""
        return CATEGORY_LABEL.get(self.category, self.category or "其它")

    def match_keys(self) -> list[str]:
        """可能出现在 ``disabled_dlcs`` 里的各种写法。

        游戏内部到底用哪个字段没有公开文档，所以三种都认：
        文件夹名、``.dlc`` 里的 ``name``、以及本地化键。
        匹配时统一小写比较，避免大小写差异造成误判。
        """
        out = [self.key, self.name, self.localizable]
        return [x for x in (s.strip() for s in out) if x]

    def to_dict(self) -> dict:
        """序列化，供界面渲染。"""
        return {
            "key": self.key, "name": self.name, "localizable": self.localizable,
            "steam_id": self.steam_id, "category": self.category,
            "category_label": self.category_label,
            "affects_checksum": self.affects_checksum,
            "affects_compat": self.affects_compat,
            "thumbnail": self.thumbnail, "enabled": self.enabled,
            "is_expansion": self.is_expansion,
        }


def parse_dlc_file(path: Path) -> dict[str, Any]:
    """解析一个 ``.dlc`` 描述符。"""
    out: dict[str, Any] = {}
    try:
        text = decode_text(path.read_bytes())
    except OSError:
        return out
    root = parse_string(text)
    for key, node in root.items():
        if key.startswith("__") or node.is_block:
            continue
        out[key] = node.value
    return out


def read_dlc_list(paths: Paths) -> list[DlcInfo]:
    """扫描游戏目录下的 ``dlc/*/*.dlc``，得到完整 DLC 清单。"""
    out: list[DlcInfo] = []
    game = paths.game_dir
    if not game:
        return out
    dlc_root = Path(game) / "dlc"
    if not is_dir(dlc_root):
        return out

    disabled = read_disabled_dlcs(paths)
    try:
        entries = sorted(dlc_root.iterdir())
    except OSError:
        return out

    for d in entries:
        if not d.is_dir():
            continue
        files = sorted(d.glob("*.dlc"))
        if not files:
            continue
        raw = parse_dlc_file(files[0])
        if not raw:
            continue
        info = DlcInfo(
            key=d.name,
            name=str(raw.get("name") or d.name),
            localizable=str(raw.get("localizable_name") or ""),
            archive=str(raw.get("archive") or ""),
            steam_id=str(raw.get("steam_id") or ""),
            category=str(raw.get("category") or ""),
            affects_checksum=str(raw.get("affects_checksum") or "no").lower()
            in ("yes", "true", "1"),
            affects_compat=str(raw.get("affects_compatability")
                               or raw.get("affects_compatibility")
                               or "no").lower() in ("yes", "true", "1"),
            thumbnail=str(raw.get("thumbnail") or ""),
            enabled=True,
            is_expansion=str(raw.get("category") or "") == "expansion",
        )
        info.enabled = not _is_disabled(info, disabled)
        out.append(info)

    # 资料片排前面，其余按名称排
    out.sort(key=lambda x: (not x.is_expansion, x.category, x.name))
    return out


def read_disabled_dlcs(paths: Paths) -> list[str]:
    """读 ``dlc_load.json`` 里的 ``disabled_dlcs`` 原始列表。"""
    p = paths.dlc_load
    if not is_file(p):
        return []
    try:
        d = json.loads(read_text(p))
    except (OSError, ValueError):
        return []
    if not isinstance(d, dict):
        return []
    val = d.get("disabled_dlcs") or []
    if isinstance(val, list):
        return [str(x) for x in val]
    return []


def _is_disabled(info: DlcInfo, disabled: list[str]) -> bool:
    """判断某个 DLC 是否在禁用列表里（多种写法都认）。"""
    keys = {k.lower() for k in info.match_keys()}
    for raw in disabled:
        s = str(raw).strip().lower()
        if not s:
            continue
        if s in keys:
            return True
        # 也可能写成 dlc/dlc001_xxx/ 这种路径
        base = s.replace("\\", "/").rstrip("/").split("/")[-1]
        if base in keys:
            return True
        # 或者带上 .dlc 扩展名
        if base.endswith(".dlc") and base[:-4] in keys:
            return True
    return False


def apply_dlc_switches(paths: Paths, dlcs: list[DlcInfo],
                       disabled_keys: Iterable[str]) -> dict:
    """把 DLC 开关写入 ``dlc_load.json``。

    ``disabled_keys`` 是**被禁用**的 DLC 的 ``key`` 集合。
    写入时优先用文件夹名，同时保留原来的写法（如果它已经在列表里），
    避免因为换写法而导致游戏认不出来。
    """
    p = paths.dlc_load
    data: dict[str, Any] = {"disabled_dlcs": [], "enabled_mods": []}
    if is_file(p):
        try:
            loaded = json.loads(read_text(p))
            if isinstance(loaded, dict):
                data.update(loaded)
        except (OSError, ValueError):
            pass
    data.setdefault("enabled_mods", [])

    want_off = {str(k) for k in disabled_keys}
    existing = [str(x) for x in (data.get("disabled_dlcs") or [])]

    out_list: list[str] = []
    # 先保留原有条目里仍然要禁用的（连同它原来的写法）
    for raw in existing:
        hit = next((d for d in dlcs if _is_disabled(d, [raw])), None)
        if hit is not None and hit.key in want_off:
            out_list.append(raw)
            want_off.discard(hit.key)
        elif hit is None:
            # 认不出来的条目：原样保留，不要擅自删掉别人的配置
            out_list.append(raw)
    # 新增的
    for d in dlcs:
        if d.key in want_off:
            out_list.append(d.key)
            want_off.discard(d.key)

    data["disabled_dlcs"] = out_list
    try:
        write_text(p, json.dumps(data, ensure_ascii=False,
                                 separators=(",", ":")), bom=False)
    except OSError as exc:
        return {"ok": False, "error": f"写入失败：{exc}"}

    on = [d.name for d in dlcs if d.key not in set(disabled_keys)]
    off = [d.name for d in dlcs if d.key in set(disabled_keys)]
    return {"ok": True, "enabled": len(on), "disabled": len(off),
            "enabled_names": on, "disabled_names": off,
            "written": out_list}


def dlc_overview(paths: Paths) -> dict:
    """给界面用的 DLC 概览。"""
    dlcs = read_dlc_list(paths)
    by_cat: dict[str, list[dict]] = {}
    for d in dlcs:
        by_cat.setdefault(d.category_label, []).append(d.to_dict())
    return {
        "dlcs": [d.to_dict() for d in dlcs],
        "by_category": by_cat,
        "total": len(dlcs),
        "enabled": sum(1 for d in dlcs if d.enabled),
        "disabled": sum(1 for d in dlcs if not d.enabled),
        "expansions": sum(1 for d in dlcs if d.is_expansion),
        "game_dir": paths.game_dir,
        "dlc_dir": str(Path(paths.game_dir) / "dlc") if paths.game_dir else "",
        "raw_disabled": read_disabled_dlcs(paths),
    }


def report_dlc_hint(paths: Paths) -> str:
    """从最近的崩溃 meta.yml 里读游戏实际加载的 DLC 数量。

    用来交叉验证「我们以为的启用状态」和「游戏实际加载的」是否一致。
    """
    import os

    try:
        dirs = sorted(
            (d for d in os.scandir(str(paths.crashes_dir)) if d.is_dir()),
            key=lambda e: e.stat().st_mtime, reverse=True)
    except OSError:
        return ""
    for d in dirs[:5]:
        mp = os.path.join(d.path, "meta.yml")
        if not is_file(mp):
            continue
        try:
            text = read_text(mp)
        except OSError:
            continue
        n = len(re.findall(r"^DLC_\d+:", text, re.M))
        ver = re.search(r"^AppVersion:\s*(\S+)", text, re.M)
        if n:
            return (f"最近一次崩溃记录里游戏加载了 {n} 个 DLC"
                    + (f"（游戏版本 {ver.group(1)}）" if ver else ""))
    return ""
