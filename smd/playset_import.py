"""「导入播放集 JSON」的存取与解析。

## 为什么需要这个

自动探测只认两种来源：官方启动器的 sqlite，和 Irony Mod Manager 的
``Database_*.json``（而且只在 ``%APPDATA%`` / ``%LOCALAPPDATA%`` 下面
一层通配里找）。

真实情况比这杂：

  · Irony 装在非默认位置 —— 探测不到
  · 用的是别的第三方启动器（Paradox Launcher Manager、自制的），
    格式各不相同
  · 别人发来一个播放集 JSON，想直接用

这几种都落到「手动给他一个文件路径」。所以这里做两件事：

  1. **认格式**：Irony 的数据库、单个播放集、以及几种常见变体
  2. **存下来**：导入的播放集落到
     ``%LOCALAPPDATA%/StellarisModDoctor/imported_playsets.json``，
     下次 ``discover()`` 一并列出来，重启不丢

## 支持的输入

**A. Irony ModManager 的 Database_*.json**（整个文件）
```
[{"Name": "ModCollection", "Value": [
   {"Name": "铁人", "Game": "Stellaris", "ModIds": [{"SteamId": "123"}, …]}
]}, …]
```

**B. 单个播放集**（最省事的手写格式）
```
{"Name": "我的播放集", "ModIds": ["123", "456"]}
```
或
```
{"name": "我的播放集", "mods": [123, 456]}
```

**C. Paradox 官方启动器导出**（如果哪天有）
```
{"name": "...", "mods": [{"steamId": "123"}, …]}
```

三种都归一成同一份内部结构，再转成 ``Playset``。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


def _app_data() -> Path:
    import os
    return (Path(os.environ.get("LOCALAPPDATA") or Path.home())
            / "StellarisModDoctor")


def store_path() -> Path:
    """导入的播放集存在哪。"""
    return _app_data() / "imported_playsets.json"


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------

def _steam_ids_of(item: dict[str, Any]) -> list[str]:
    """从一个播放集 dict 里抠出有序的工坊 id 列表。

    兼容 ``ModIds`` / ``Mods`` / ``mods`` 三种字段名，
    以及元素是 ``{"SteamId": …}`` / ``123`` / ``"123"`` 三种形态。
    """
    # 注意最后那个 steam_ordered —— 自己存盘的格式就是这个，
    # 少认它会让 save_imported 之后 load_imported 读到空（实测踩过）。
    seq = (item.get("ModIds") or item.get("Mods") or item.get("mods")
           or item.get("SteamIds") or item.get("steam_ordered") or [])
    if not isinstance(seq, list):
        return []
    out: list[str] = []
    for x in seq:
        sid = ""
        if isinstance(x, dict):
            sid = str(x.get("SteamId") or x.get("steamId")
                      or x.get("steam_id") or "").strip()
        elif isinstance(x, (int, str)):
            sid = str(x).strip()
        if sid and sid not in ("0", "None"):
            out.append(sid)
    return out


def _name_of(item: dict[str, Any], fallback: str) -> str:
    for k in ("Name", "name", "Title", "title"):
        v = item.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return fallback


def _looks_like_playset(d: dict[str, Any]) -> bool:
    """判据放宽一点：有名字 + 有模组序列，就算一个播放集。"""
    has_ids = any(k in d for k in ("ModIds", "Mods", "mods", "SteamIds"))
    has_name = any(k in d for k in ("Name", "name", "Title", "title"))
    return has_ids and has_name


def parse(raw: Any, fallback_prefix: str = "导入") -> list[dict[str, Any]]:
    """把任意形状的 JSON 归一成播放集列表。

    返回每项：``{"name", "steam_ordered": [...], "active": bool}``。
    解析不出东西就返回空列表 —— 调用方据此报错，别静默成功。
    """
    found: list[dict[str, Any]] = []

    def push(item: dict[str, Any], default_name: str) -> None:
        # Irony 的数据库里混着欧陆、钢四、群星…… 只收群星
        game = str(item.get("Game") or item.get("game") or "")
        if game and game.lower() != "stellaris":
            return
        ids = _steam_ids_of(item)
        if not ids:
            return
        found.append({
            "name": _name_of(item, default_name),
            "steam_ordered": ids,
            "active": bool(item.get("IsSelected") or item.get("isActive")
                           or item.get("active")),
        })

    def walk(node: Any, depth: int = 0) -> None:
        if depth > 6:
            return
        if isinstance(node, list):
            for i, ent in enumerate(node):
                if isinstance(ent, dict):
                    # Irony 的容器：{"Name": "ModCollection", "Value": [...]}
                    if isinstance(ent.get("Value"), list):
                        walk(ent["Value"], depth + 1)
                    elif _looks_like_playset(ent):
                        push(ent, f"{fallback_prefix} {i + 1}")
                elif isinstance(ent, list):
                    walk(ent, depth + 1)
        elif isinstance(node, dict):
            if isinstance(node.get("Value"), list):
                walk(node["Value"], depth + 1)
                return
            # 有些导出是 {"Playsets": [...]} / {"collections": [...]}
            for k in ("Playsets", "playsets", "Collections", "collections",
                      "Items", "items"):
                if isinstance(node.get(k), list):
                    walk(node[k], depth + 1)
                    return
            if _looks_like_playset(node):
                push(node, fallback_prefix)

    walk(raw)
    return found


# ---------------------------------------------------------------------------
# 存取
# ---------------------------------------------------------------------------

def load_imported() -> list[dict[str, Any]]:
    """读出之前导入过的播放集（坏文件当没导入）。"""
    p = store_path()
    if not p.is_file():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    out: list[dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        ids = _steam_ids_of(item)
        if not ids:
            continue
        out.append({
            "id": str(item.get("id") or f"import-{int(time.time())}"),
            "name": _name_of(item, "导入的播放集"),
            "steam_ordered": ids,
            "imported_at": item.get("imported_at") or "",
            "origin": item.get("origin") or "",
        })
    return out


def save_imported(items: list[dict[str, Any]]) -> Path:
    """整份重写。``items`` 是 :func:`load_imported` 那种结构。"""
    p = store_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(items, ensure_ascii=False, indent=2),
                 encoding="utf-8")
    return p


def import_file(path: str | Path) -> dict[str, Any]:
    """导入一个 JSON 文件。

    返回 ``{"ok": bool, "added": n, "names": [...], "error": str}``。
    ``added`` 是**新增**的数量（重名的跳过，避免导入两次出现两条）。
    """
    src = Path(str(path))
    if not src.is_file():
        return {"ok": False, "added": 0, "names": [],
                "error": f"文件不存在：{src}"}
    try:
        raw = json.loads(src.read_text(encoding="utf-8-sig"))
    except ValueError as exc:
        return {"ok": False, "added": 0, "names": [],
                "error": f"不是合法的 JSON：{exc}"}
    except OSError as exc:
        return {"ok": False, "added": 0, "names": [],
                "error": f"读不了这个文件：{exc}"}

    parsed = parse(raw, fallback_prefix=src.stem)
    if not parsed:
        return {"ok": False, "added": 0, "names": [],
                "error": "这个文件里没找到播放集。支持 Irony ModManager 的 "
                         "Database_*.json，或 {\"Name\": ..., \"ModIds\": [...]} "
                         "这种单条格式。"}

    existing = load_imported()
    seen = {(e["name"], tuple(e["steam_ordered"])) for e in existing}
    added: list[dict[str, Any]] = []
    for i, ps in enumerate(parsed):
        key = (ps["name"], tuple(ps["steam_ordered"]))
        if key in seen:
            continue
        seen.add(key)
        added.append({
            "id": f"import-{int(time.time() * 1000)}-{i}",
            "name": ps["name"],
            "steam_ordered": ps["steam_ordered"],
            "imported_at": time.strftime("%Y-%m-%d %H:%M"),
            "origin": str(src),
        })

    if not added:
        return {"ok": True, "added": 0,
                "names": [ps["name"] for ps in parsed],
                "error": "", "note": "这些播放集之前已经导入过了"}

    save_imported(existing + added)
    return {"ok": True, "added": len(added),
            "names": [a["name"] for a in added], "error": ""}


def remove_imported(pid: str) -> dict[str, Any]:
    """删掉一条导入的播放集。"""
    items = load_imported()
    keep = [x for x in items if x["id"] != pid]
    if len(keep) == len(items):
        return {"ok": False, "error": "没有这条播放集"}
    save_imported(keep)
    return {"ok": True, "removed": len(items) - len(keep)}
