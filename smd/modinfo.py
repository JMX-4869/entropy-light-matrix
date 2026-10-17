"""模组发现与元数据归一化。

群星的模组信息散落在三个地方，而且经常互不一致：

1. ``mod/ugc_<id>.mod``      —— 群星自己读的描述符（游戏内实际生效）
2. ``launcher-v2.sqlite``    —— Paradox 启动器的数据库（DLC/playset 真源）
3. ``workshop/content/281990/<id>/`` —— 工坊下载的实际文件

任何一个都可能是「幽灵模组」的来源（存在描述符但文件没了，或反之）。
本模块负责把三者合并成一份可信的模组清单。
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .config import Paths
from .paradox import Node, parse_string
from .paths import (exists, human_size, is_dir, is_file, long_path, norm, norm_key,
                    read_text)

# 群星支持的游戏版本号形如 v4.4.*、v4.*、4.4.6、3.40.1
_VER_RE = re.compile(r"v?(\d+)(?:\.(\d+))?(?:\.(\d+))?")
_UGC_RE = re.compile(r"ugc_(\d+)", re.I)

# 判断一个模组「类别」用的标签
TAG_TRANSLATION = {"translation", "localization"}
TAG_FIX = {"fixes", "bug fixes", "fix"}
TAG_UI = {"ui", "graphics", "interface"}
TAG_PATCH = {"patch", "compatibility", "compat"}


@dataclass
class Mod:
    """单个模组的归一化描述。"""

    key: str = ""                     # 稳定主键：ugc id 或 local:<名字>
    name: str = ""                    # 显示名（描述符里的 name）
    dir_name: str = ""                # 启动器数据库里的 displayName
    mod_file: str = ""                # mod/ugc_xxx.mod 的绝对路径
    game_registry_id: str = ""        # dlc_load.json 里引用的 "mod/ugc_xxx.mod"
    path: str = ""                    # 内容根目录
    remote_file_id: str = ""          # 工坊 id
    version: str = ""
    supported_version: str = ""
    tags: list[str] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)
    source: str = "local"             # steam | local | paradox
    status: str = ""                  # ready_to_play | ...
    picture: str = ""
    # 来源标记（哪些来源观察到它）
    from_mod_file: bool = False
    from_sqlite: bool = False
    from_workshop: bool = False
    enabled_in_playset: bool = False
    playset_position: int = -1
    # 派生
    problems: list[str] = field(default_factory=list)

    # 内部：工坊根目录列表，由 load_library 注入，供 content_dir() 反查
    _workshop_dirs: list[str] = field(default_factory=list, repr=False)

    # ---- 便捷属性 ------------------------------------------------
    @property
    def is_translation(self) -> bool:
        """是否属于本地化/汉化模组（按标签与名称关键词判断）。"""
        tl = {t.lower() for t in self.tags}
        if tl & TAG_TRANSLATION:
            return True
        n = self.name.lower()
        return any(k in n for k in ("translation", "汉化", "漢化", "chinese patch",
                                    "chn patch", "中文", "简中", "當地語系", "localization"))

    @property
    def is_fix(self) -> bool:
        """是否属于修复/补丁类模组。"""
        tl = {t.lower() for t in self.tags}
        if tl & TAG_FIX:
            return True
        n = self.name.lower()
        return any(k in n for k in ("bug fix", "bugfix", "hotfix", "patch", "修补",
                                    "修复", "修正"))

    @property
    def is_library_or_framework(self) -> bool:
        """是否是前置框架或基础库（应当最先加载）。"""
        n = self.name.lower()
        return any(k in n for k in ("framework", " library", "core framework",
                                    "universal resource patch", "shared",
                                    "framework", "基础库", "前置"))

    @property
    def is_ui(self) -> bool:
        """是否属于界面类模组。"""
        tl = {t.lower() for t in self.tags}
        if tl & TAG_UI:
            return True
        n = self.name.lower()
        return any(k in n for k in ("ui ", " ui", "interface", "outliner", "topbar",
                                    "tooltip", "tooltips"))

    @property
    def is_gigastructural(self) -> bool:
        """是否属于巨构/NSC/ACOT 这类大型机制模组。"""
        n = self.name.lower()
        return "gigastructural" in n or "giga" in n or "nsc" in n or "acot" in n

    @property
    def size_hint(self) -> int:
        """粗略体量：用支持版本/标签猜不出，留给扫描器填真实值。"""
        return getattr(self, "_size", 0)

    @property
    def supported_major(self) -> tuple[int, int, int]:
        """把 supported_version 解析成 (major, minor, patch)，缺失补 0。"""
        m = _VER_RE.search(self.supported_version or "")
        if not m:
            return (0, 0, 0)
        return (int(m.group(1)), int(m.group(2) or 0), int(m.group(3) or 0))

    @property
    def has_wildcard_patch(self) -> bool:
        """``supported_version`` 是否用了 ``*`` 通配小版本。"""
        return "*" in (self.supported_version or "")

    def accepts_game_version(self, major: int, minor: int, patch: int,
                             allow_minor_mismatch: bool = False) -> bool:
        """判断本模组是否声明兼容给定游戏版本。"""
        sm, sn, sp = self.supported_major
        if sm == 0:
            return True  # 没写版本号，不判它过期
        if sm != major:
            return False
        if sn != minor:
            return allow_minor_mismatch
        if self.has_wildcard_patch:
            return True
        if sp == 0:
            return True
        return sp == patch

    def resolve_content_path(self) -> str:
        """返回内容目录；若描述符里的 path 失效则返回空串。"""
        if not self.path:
            return ""
        if is_dir(self.path):
            return self.path
        return self._fix_path_case()

    def _fix_path_case(self) -> str:
        """修正路径**大小写**后重新确认目录是否存在。

        为什么需要：Windows 路径不区分大小写，但 ``os.path.isdir`` 是
        按字节比较的（Python 会直接调系统 API，多数情况下系统会做
        case-insensitive 匹配，但盘符/UNC 前缀不一致时会失配）。

        真实案例：``PSE Chinese patch`` 的 ``.mod`` 里写的是
        ``path="E:/steam/steamapps/workshop/content/281990/2565465427"``，
        而机器上真实的工坊目录是 ``e:\\steam\\...``（小写盘符）。
        这个模组其实**能正常加载**，却因为大小写被判成"磁盘上没有内容"，
        界面上开关被灰掉、提示用户去重新订阅——完全是误报。

        修法：按路径各级目录逐个做不区分大小写的匹配，拼回真实路径。
        """
        try:
            p = Path(self.path)
            parts = p.parts
            if not parts:
                return ""
            # 从盘符（或根）开始逐级往下找
            cur = Path(parts[0])
            if not cur.exists():
                # 盘符大小写不对：在可用盘符里找同名的一个
                drive = parts[0].rstrip("\\/").rstrip(":")
                found = None
                for letter in "abcdefghijklmnopqrstuvwxyz":
                    cand = Path(f"{letter}:\\")
                    try:
                        if cand.exists() and letter.lower() == drive.lower():
                            found = cand
                            break
                    except OSError:
                        continue
                if found is None:
                    return ""
                cur = found
            for part in parts[1:]:
                nxt = cur / part
                if nxt.exists():
                    cur = nxt
                    continue
                # 找不到就按不区分大小写扫一遍当前层
                hit = None
                try:
                    target = part.lower()
                    for child in cur.iterdir():
                        if child.name.lower() == target:
                            hit = child
                            break
                except OSError:
                    return ""
                if hit is None:
                    return ""
                cur = hit
            return str(cur) if cur.is_dir() else ""
        except (OSError, ValueError):
            return ""

    def content_dir(self) -> str:
        """尽量找出这个模组的内容目录（找不到返回空串）。

        比 :meth:`resolve_content_path` 多两条退路：

        * ``path`` 指向的位置不在（工坊内容被清掉了），
          但 ``remote_file_id`` 对应的工坊目录还在
        * 本地模组没写 ``path``，但内容就躺在 ``mod/<目录名>`` 下

        只用来判断「磁盘上到底有没有东西」和给用户指路，
        **不参与加载决策**——那是 ``enabled_in_playset`` 的事。
        """
        p = self.resolve_content_path()
        if p:
            return p
        # 退路 1：按工坊 id 到各工坊目录里找
        if self.remote_file_id:
            roots = list(getattr(self, "_workshop_dirs", None) or [])
            if self.path:
                # 从已记录的路径反推工坊根目录，兼容没传 _workshop_dirs 的情况
                parts = Path(self.path).parts
                for i in range(len(parts), 2, -1):
                    cand_root = Path(*parts[:i])
                    if cand_root.name == "281990" or "workshop" in str(
                            cand_root).lower():
                        roots.append(str(cand_root))
                        break
            for w in roots:
                cand = Path(w) / str(self.remote_file_id)
                if is_dir(cand):
                    return str(cand)
        # 退路 2：path 写了但目录不在，也如实返回，便于提示用户
        if self.path:
            return self.path
        return ""

    def is_loadable(self) -> bool:
        """磁盘上到底有没有可加载的内容。

        为 ``False`` 表示：**把它勾上也没用**——游戏读不到任何文件。
        这种模组要么是已退订但启动器数据库还留着记录，
        要么是工坊内容被 Steam 清掉了。

        判定只看「有没有东西可加载」，**不看元数据告警**：
        版本不一致、缺前置之类的模组照常可以启用。
        """
        if self.resolve_content_path():
            return True
        cpath = self.content_dir()
        return bool(cpath and is_dir(cpath))

    def workshop_url(self) -> str:
        """Steam 创意工坊详情页地址；没有工坊 id 时返回空串。"""
        return (f"https://steamcommunity.com/sharedfiles/filedetails/"
                f"?id={self.remote_file_id}") if self.remote_file_id else ""

    def is_ghost(self) -> bool:
        """是不是「已退订的残留」——不该再出现在模组列表里。

        判定分两条路径：

        **A. 纯数据库幽灵**
        只在 Paradox 启动器数据库里有，磁盘上什么都没有。

        **B. 退订残留（``.mod`` 描述符还在，但 Steam 已经不认了）**
        退订时 Steam 会删掉 ``workshop/content/<id>/``，
        **但不会删 Documents 里那份 ``.mod`` 描述符**——
        于是磁盘上"还有东西"，看起来像正常模组，其实内容早已不存在。
        这种残留光看文件系统分不清和"暂时缺文件"的区别，
        必须用 Steam 的 ACF 订阅清单来判：id 不在清单里 = 已退订。

        任一命中即为 ghost。已订阅但内容暂缺（正在下载）的**不算**。
        """
        # A：什么都没有
        if not (self.mod_file and is_file(self.mod_file)) \
                and not self.from_workshop:
            cpath = self.content_dir()
            if not (cpath and is_dir(cpath)) \
                    and not (self.path and is_dir(self.path)):
                return True

        # B：描述符还在，但内容不在且 Steam 已不认这个订阅
        #    只对"指向工坊"的模组生效，本地模组不适用
        if self.remote_file_id and not self.path_links_to_workshop():
            return False
        if self.remote_file_id:
            cpath = self.content_dir()
            if cpath and is_dir(cpath):
                return False                     # 内容还在，正常
            sub = getattr(self, "_steam_subscribed", None)
            if sub is False:
                return True                      # Steam 明确说没订阅
        return False

    def path_links_to_workshop(self) -> bool:
        """描述符里的 path 是否指向工坊目录（而不是本地 mod 目录）。"""
        p = (self.path or "").replace("\\", "/").lower()
        return "workshop/content/" in p or "/workshop/" in p

    def to_dict(self) -> dict:
        """序列化为界面用的字典。"""
        d = {k: v for k, v in self.__dict__.items() if not k.startswith("_")}
        d["workshop_url"] = self.workshop_url()
        d["is_ghost"] = self.is_ghost()
        return d


# ---------------------------------------------------------------------------
# .mod 描述符解析
# ---------------------------------------------------------------------------

def parse_mod_file(path: str | Path) -> dict[str, Any]:
    """解析一个 ``.mod`` 描述符，返回原始字段字典。"""
    text = read_text(path)
    root = parse_string(text)
    out: dict[str, Any] = {}
    for key, node in root.items():
        if key.startswith("__"):
            continue
        if node.is_block:
            # tags / dependencies 这类块：收集成字符串列表
            vals: list[str] = []
            for _k, child in node.items():
                if not child.is_block:
                    vals.append(str(child.value))
            out[key] = vals
        else:
            out[key] = node.value
    return out


def _key_for(raw: dict[str, Any], mod_file: Path | None) -> tuple[str, str]:
    """决定模组主键与工坊 id。"""
    rfid = str(raw.get("remote_file_id") or "").strip()
    if rfid:
        return f"ugc:{rfid}", rfid
    if mod_file is not None:
        m = _UGC_RE.search(mod_file.stem)
        if m:
            return f"ugc:{m.group(1)}", m.group(1)
        return f"local:{mod_file.stem}", ""
    return f"local:{raw.get('name', 'unknown')}", ""


def scan_mod_descriptors(paths: Paths) -> dict[str, Mod]:
    """扫描所有 ``.mod`` 描述符（用户 mod 目录 + 工坊内嵌描述符）。"""
    found: dict[str, Mod] = {}
    roots: list[Path] = []
    if paths.user_dir:
        roots.append(Path(paths.user_dir) / "mod")
    for w in paths.workshop_dirs:
        roots.append(Path(w))

    for root in roots:
        if not is_dir(root):
            continue
        try:
            entries = sorted(Path(long_path(root)).glob("*.mod"))
        except OSError:
            continue
        for mf in entries:
            try:
                raw = parse_mod_file(mf)
            except Exception:
                continue
            key, rfid = _key_for(raw, mf)
            content = str(raw.get("path") or "").replace("\\", "/")
            mod = Mod(
                key=key,
                name=str(raw.get("name") or mf.stem),
                mod_file=str(mf),
                game_registry_id=f"mod/{mf.name}",
                path=content,
                remote_file_id=rfid,
                version=str(raw.get("version") or ""),
                supported_version=str(raw.get("supported_version") or ""),
                tags=[str(t) for t in (raw.get("tags") or [])],
                dependencies=[str(t) for t in (raw.get("dependencies") or [])],
                picture=str(raw.get("picture") or ""),
                source="steam" if rfid else "local",
                from_mod_file=True,
            )
            # 内容目录不存在时，退而求其次用工坊目录
            if not mod.resolve_content_path() and rfid:
                for w in paths.workshop_dirs:
                    cand = Path(w) / rfid
                    if is_dir(cand):
                        mod.path = str(cand)
                        mod.from_workshop = True
                        break
            prev = found.get(key)
            if prev is None:
                found[key] = mod
            else:
                # 用户 mod 目录优先（游戏实际读的就是它）
                if "mod" in norm(mod.mod_file) and "workshop" not in norm(mod.mod_file):
                    found[key] = mod
    return found


def scan_workshop(paths: Paths, known: dict[str, Mod]) -> list[Mod]:
    """扫描工坊目录，找出「有文件但没描述符」的幽灵模组。"""
    extra: list[Mod] = []
    for w in paths.workshop_dirs:
        if not is_dir(w):
            continue
        try:
            subs = [d for d in Path(long_path(w)).iterdir() if d.is_dir()]
        except OSError:
            continue
        for d in subs:
            if not d.name.isdigit():
                continue
            key = f"ugc:{d.name}"
            if key in known:
                known[key].from_workshop = True
                if not known[key].resolve_content_path():
                    known[key].path = str(d)
                continue
            # 工坊目录里可能自己带 descriptor.mod
            inner = d / "descriptor.mod"
            name = d.name
            sup = ""
            tags: list[str] = []
            if is_file(inner):
                try:
                    raw = parse_mod_file(inner)
                    name = str(raw.get("name") or name)
                    sup = str(raw.get("supported_version") or "")
                    tags = [str(t) for t in (raw.get("tags") or [])]
                except Exception:
                    pass
            m = Mod(
                key=key, name=name, dir_name=name, path=str(d),
                remote_file_id=d.name, source="steam",
                supported_version=sup, tags=tags,
                from_workshop=True,
            )
            m.problems.append("工坊有文件但缺少 .mod 描述符（游戏不会加载它）")
            extra.append(m)
            known[key] = m
    return extra


# ---------------------------------------------------------------------------
# launcher-v2.sqlite
# ---------------------------------------------------------------------------

def scan_launcher_db(paths: Paths, known: dict[str, Mod]) -> tuple[list[Mod], dict]:
    """读取 Paradox 启动器数据库：补全名称、状态、playset 启用情况。"""
    extra: list[Mod] = []
    info: dict[str, Any] = {"available": False, "playset": "", "mods": {}}
    db = paths.launcher_db
    if not is_file(db):
        return extra, info
    try:
        con = sqlite3.connect(f"file:{long_path(db)}?mode=ro", uri=True)
    except sqlite3.Error:
        try:
            con = sqlite3.connect(long_path(db))
        except sqlite3.Error:
            return extra, info
    try:
        con.row_factory = sqlite3.Row
        cur = con.cursor()
        info["available"] = True

        # 启用中的 playset
        try:
            cur.execute("SELECT id,name,isActive,loadOrder FROM playsets "
                        "WHERE isActive=1 LIMIT 1")
            row = cur.fetchone()
            if row:
                info["playset"] = row["name"] or ""
                info["playset_id"] = row["id"]
        except sqlite3.Error:
            pass

        # playset 内的启用状态与顺序。
        #
        # **必须按当前活动播放集过滤**。`playsets_mods` 每行是
        # (playsetId, modId, enabled, position) —— 用户有多个播放集时，
        # 同一个 modId 会出现多行。不过滤的话后面覆盖前面，
        # 结果就是"所有播放集的启用状态混在一起、几乎全是启用"，
        # 这正是用户抱怨的「一扫描就把我订阅的 mod 全部一起弄了」。
        #
        # 只用活动播放集那一份：玩家在启动器里选的是哪个，
        # 游戏实际加载的就是哪个，工具应该跟着它走。
        enabled: dict[str, tuple[bool, int, Any]] = {}
        pid = info.get("playset_id")
        if pid:
            try:
                cur.execute(
                    "SELECT pm.modId, pm.enabled, pm.position, "
                    "       m.steamId, m.gameRegistryId "
                    "FROM playsets_mods pm "
                    "LEFT JOIN mods m ON m.id = pm.modId "
                    "WHERE pm.playsetId = ?", (pid,))
                for r in cur.fetchall():
                    enabled[str(r["modId"])] = (
                        bool(r["enabled"]),
                        int(r["position"] or 0),
                        r["steamId"],
                    )
            except sqlite3.Error:
                pass
        else:
            # 读不到活动播放集 id（老版本启动器 / 表结构不同）：
            # 退回不带过滤的旧行为，但**只取每个 mod 的第一行**，
            # 避免同一 mod 被多个播放集反复覆盖。
            try:
                cur.execute(
                    "SELECT pm.modId, pm.enabled, pm.position, "
                    "       m.steamId, m.gameRegistryId "
                    "FROM playsets_mods pm "
                    "LEFT JOIN mods m ON m.id = pm.modId "
                    "GROUP BY pm.modId")
                for r in cur.fetchall():
                    enabled[str(r["modId"])] = (
                        bool(r["enabled"]),
                        int(r["position"] or 0),
                        r["steamId"],
                    )
            except sqlite3.Error:
                pass

        try:
            cur.execute(
                "SELECT id,steamId,gameRegistryId,displayName,name,version,"
                "requiredVersion,tags,status,source,dirPath,repositoryPath,"
                "remoteSteamId,size FROM mods"
            )
            rows = cur.fetchall()
        except sqlite3.Error:
            rows = []

        for r in rows:
            steam_id = str(r["steamId"] or r["remoteSteamId"] or "").strip()
            reg = str(r["gameRegistryId"] or "").strip()
            key = f"ugc:{steam_id}" if steam_id else (f"reg:{norm(reg)}" if reg else "")
            if not key:
                continue
            disp = str(r["displayName"] or r["name"] or "").strip()
            tags_raw = r["tags"]
            try:
                tags = json.loads(tags_raw) if tags_raw else []
            except (ValueError, TypeError):
                tags = []
            en, pos = False, -1
            if r["id"] in enabled:
                en, pos, _sid = enabled[r["id"]]
            info["mods"][key] = {
                "display_name": disp, "enabled": en, "position": pos,
                "status": str(r["status"] or ""),
                "required_version": str(r["requiredVersion"] or ""),
                "dir": str(r["dirPath"] or ""),
                "size": int(r["size"] or 0),
            }
            mod = known.get(key)
            if mod is None:
                # 本地模组：用 gameRegistryId 反查
                if reg:
                    for k2, m2 in known.items():
                        if norm(m2.game_registry_id) == norm(reg):
                            mod = m2
                            break
                if mod is None and reg:
                    mod = Mod(
                        key=key, name=disp or reg, dir_name=disp,
                        game_registry_id=reg, source=str(r["source"] or "local"),
                        version=str(r["version"] or ""),
                        supported_version=str(r["requiredVersion"] or ""),
                        tags=[str(t) for t in tags],
                        path=str(r["dirPath"] or ""),
                        from_sqlite=True,
                    )
                    mod.problems.append("启动器数据库有记录，但找不到 .mod 描述符文件")
                    known[key] = mod
                    extra.append(mod)
            if mod is not None:
                mod.from_sqlite = True
                if disp and not mod.dir_name:
                    mod.dir_name = disp
                if not mod.name or mod.name.startswith("ugc_"):
                    mod.name = disp or mod.name
                if not mod.tags and tags:
                    mod.tags = [str(t) for t in tags]
                if not mod.version and r["version"]:
                    mod.version = str(r["version"])
                if not mod.supported_version and r["requiredVersion"]:
                    mod.supported_version = str(r["requiredVersion"])
                if not mod.status and r["status"]:
                    mod.status = str(r["status"])
                if not mod.path and r["dirPath"]:
                    mod.path = str(r["dirPath"])
                mod.enabled_in_playset = en
                mod.playset_position = pos
                try:
                    mod._size = int(r["size"] or 0)
                except Exception:
                    pass
    finally:
        try:
            con.close()
        except Exception:
            pass
    return extra, info


def read_dlc_load(paths: Paths) -> dict[str, Any]:
    """读取 dlc_load.json —— 游戏运行时真正读取的启用模组清单。"""
    p = paths.dlc_load
    if not is_file(p):
        return {"disabled_dlcs": [], "enabled_mods": []}
    try:
        d = json.loads(read_text(p))
        if not isinstance(d, dict):
            return {"disabled_dlcs": [], "enabled_mods": []}
        d.setdefault("disabled_dlcs", [])
        d.setdefault("enabled_mods", [])
        return d
    except (OSError, ValueError):
        return {"disabled_dlcs": [], "enabled_mods": []}


@dataclass
class ModLibrary:
    """一次完整的模组清点结果。"""

    mods: dict[str, Mod] = field(default_factory=dict)
    launcher_info: dict = field(default_factory=dict)
    dlc_load: dict = field(default_factory=dict)
    paths: Paths | None = None

    def all(self) -> list[Mod]:
        """全部模组，按名称排序。"""
        return sorted(self.mods.values(), key=lambda m: m.name.lower())

    def enabled(self) -> list[Mod]:
        """在当前 playset 中已启用的模组。"""
        return [m for m in self.all() if m.enabled_in_playset]

    def by_registry(self) -> dict[str, Mod]:
        """``gameRegistryId（归一化） -> 模组`` 反查表。"""
        return {norm(m.game_registry_id): m for m in self.mods.values()
                if m.game_registry_id}

    def get(self, key: str) -> Mod | None:
        """按主键取模组；不存在返回 ``None``。"""
        return self.mods.get(key)

    def load_order(self) -> list[Mod]:
        """按 playset 位置排序的已启用模组。"""
        en = [m for m in self.enabled() if m.playset_position >= 0]
        en.sort(key=lambda m: m.playset_position)
        return en

    def summary(self) -> dict[str, int]:
        """统计。

        ``missing_files`` 只统计**已启用**却找不到文件的模组——那才是会让游戏
        报 ``Could not find files for mod`` 的真问题。
        未启用但记录过期的（Steam 已取消订阅、数据库残留）另算
        ``stale_records``，它们不影响游戏。
        """
        s = {"total": len(self.mods), "enabled": 0, "missing_files": 0,
             "stale_records": 0, "ghost": 0, "stale": 0, "translation": 0}
        for m in self.mods.values():
            if m.enabled_in_playset:
                s["enabled"] += 1
            ok = bool(m.resolve_content_path())
            if not ok:
                if m.enabled_in_playset:
                    s["missing_files"] += 1      # 真问题
                else:
                    s["stale_records"] += 1      # 只是过期记录
            if any("工坊有文件但缺少" in p for p in m.problems):
                s["ghost"] += 1
            if m.is_translation:
                s["translation"] += 1
        return s


def read_steam_subscriptions(paths: Paths) -> set[str]:
    """从 Steam 的 ``appworkshop_<appid>.acf`` 读出**当前仍订阅**的工坊 id。

    为什么需要它：退订之后 Steam 会删掉 ``workshop/content/<id>/``，
    也会把它从 ACF 的 ``WorkshopItemsInstalled`` 里移除——
    但 **Documents 里那份 ``.mod`` 描述符不会被删**。

    这种「描述符还在、内容没了、Steam 也不认了」的残留，
    光看文件系统是分不清「已退订」和「Steam 正在下载 / 暂时缺文件」的。
    ACF 正是那个权威判据。

    读不到（Steam 未安装、ACF 缺失）时返回空集合，调用方需自行降级处理。
    """
    out: set[str] = set()
    try:
        roots: list[Path] = []
        for w in (paths.workshop_dirs or []):
            # ``workshop_dirs`` 一般指向 ``.../workshop/content/<appid>``，
            # 而 ACF 在 ``.../workshop/`` 下——层级随 Steam 版本会变
            # （曾经以为只有两层，结果一直读不到）。所以逐级往上找，
            # 谁先含 appworkshop_*.acf 就用谁。
            cur = Path(w)
            for _ in range(4):
                if cur.is_dir() and any(cur.glob("appworkshop_*.acf")):
                    roots.append(cur)
                    break
                if cur.parent == cur:
                    break
                cur = cur.parent
        for root in dict.fromkeys(roots):        # 去重且保持顺序
            for acf in root.glob("appworkshop_*.acf"):
                try:
                    text = read_text(acf)
                except OSError:
                    continue
                m = re.search(r'"WorkshopItemsInstalled"\s*\{', text)
                if not m:
                    continue
                # 只收**直接子条目**（缩进两层的 "id" {），
                # 不能把嵌套的 TimeUpdated / Manifest / size 之类算进来。
                #
                # 判据用「缩进恰好是两个制表符」：ACF 是 Valve 用固定
                # 缩进生成的，直接子条目就是这一层。用花括号配平去判
                # 容易因为解析细节多收（实测收到过 309 条，实际只有 75）。
                body_start = m.end()
                nxt = re.search(r'\n\t\}', text[body_start:])
                body = text[body_start:body_start + (nxt.start() if nxt
                                                     else len(text))]
                out |= set(re.findall(r'\n\t\t"(\d{5,})"\s*\{', body))
    except (OSError, ValueError):
        return set()
    return out


def _is_subscribed(m: Mod, subs: set[str]) -> bool:
    """这个模组的工坊 id 是否仍在 Steam 订阅清单里。"""
    if not m.remote_file_id:
        return False
    return str(m.remote_file_id) in subs


def load_library(paths: Paths, *, deep_workshop_scan: bool = True) -> ModLibrary:
    """完整清点：描述符 + 工坊 + 启动器数据库 + dlc_load.json。"""
    mods = scan_mod_descriptors(paths)
    if deep_workshop_scan:
        scan_workshop(paths, mods)
    extra, info = scan_launcher_db(paths, mods)
    lib = ModLibrary(mods=mods, launcher_info=info, dlc_load=read_dlc_load(paths),
                     paths=paths)

    # 给每个模组挂上工坊目录，供 content_dir() 反查内容位置
    ws_dirs = list(paths.workshop_dirs or [])
    for m in mods.values():
        m._workshop_dirs = ws_dirs

    # 挂上 Steam 订阅清单：判断"已退订的残留"要靠它
    subs = read_steam_subscriptions(paths)
    lib.steam_subscriptions = subs
    for m in mods.values():
        m._steam_subscribed = _is_subscribed(m, subs) if subs else None

    # 用 dlc_load.json 交叉校验：它是游戏真正读的清单
    reg = lib.by_registry()
    enabled_regs = {norm(x) for x in lib.dlc_load.get("enabled_mods", [])}
    for m in mods.values():
        if m.game_registry_id and norm(m.game_registry_id) in enabled_regs:
            m.enabled_in_playset = True
    for r in enabled_regs:
        if r not in reg:
            # dlc_load 引用了不存在的模组 —— 典型「幽灵模组」导致启动异常
            pass

    _post_validate(lib)
    return lib


def _post_validate(lib: ModLibrary) -> None:
    """给每个模组补上问题标记。"""
    paths = lib.paths
    workshop = {norm(w) for w in (paths.workshop_dirs if paths else [])}
    for m in lib.mods.values():
        cpath = m.resolve_content_path()
        if m.enabled_in_playset and not cpath:
            m.problems.append("已启用但内容目录不存在（会导致游戏启动时报 "
                              "Could not find files for mod）")
        # 订阅了但没在 playset 里
        if not m.enabled_in_playset and m.from_mod_file and m.remote_file_id:
            pass
        # 描述符位于 mod 目录但内容指向工坊 —— 正常
        # 检查是否有 descriptor.mod 与 .mod 不一致
        if cpath:
            inner = Path(cpath) / "descriptor.mod"
            if is_file(inner):
                try:
                    raw = parse_mod_file(inner)
                    inner_sup = str(raw.get("supported_version") or "")
                    if inner_sup and m.supported_version and \
                            inner_sup != m.supported_version:
                        m.problems.append(
                            f"描述符版本不一致：mod/ 为 {m.supported_version}，"
                            f"descriptor.mod 为 {inner_sup}"
                        )
                except Exception:
                    pass
        # 依赖缺失
        for dep in m.dependencies:
            dep_norm = norm(dep)
            hit = any(norm(x.game_registry_id) == dep_norm or
                      norm(x.remote_file_id) == dep_norm or
                      norm(x.name) == dep_norm
                      for x in lib.mods.values())
            if not hit:
                m.problems.append(f"缺少前置依赖：{dep}")
        # 内容根本不在磁盘上：勾上也没用，必须明确告诉用户
        if not m.is_loadable():
            m.problems.append(
                "内容不在磁盘上——勾选也不会加载。"
                "多半是已退订（启动器数据库还留着记录），"
                "或工坊文件被 Steam 清理过。"
                "重新订阅后即可启用。")
