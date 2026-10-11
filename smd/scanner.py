"""模组内容索引：文件级覆盖 + 语义级定义索引。

两阶段设计（性能考虑：76 个模组、数 GB 文件）：

**第一阶段 · 文件级**（只读目录元数据，秒级完成）
    列出每个模组提供的所有文件，按「模组内相对路径」求交集。
    交集非空 = 两个模组提供同一个游戏文件 = **覆盖冲突**。
    群星规则：加载顺序靠后的模组整体覆盖靠前的同名文件。

**第二阶段 · 语义级**（只解析文本脚本，跳过贴图/音频）
    对 ``common/`` ``events/`` ``interface/`` ``localisation/`` 等目录下的脚本文件
    真正解析语法树，提取「定义了哪些具名对象」。
    两个模组定义同名对象 = **重定义冲突**（此时代码内容不同则必须由用户选择保留谁）。

语义索引记录的是「名字 -> 定义来源」，这是判断兼容性的关键：
例如两个模组都定义 ``scripted_trigger = { my_check = ... }``，
后加载者的定义会静默生效，先加载者的逻辑被吃掉 —— 这正是游戏里
「某模组的功能莫名失效」的根因。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Iterator

from .modinfo import Mod
from .paradox import Node, canonical_text, flatten_definitions, parse_string
from .paths import (decode_text, human_size, is_dir, is_file, long_path, norm,
                    rel_posix, sha1_text)

# 需要做语义解析的目录（其它目录（gfx/sound/music）只做文件级比较）
SEMANTIC_ROOTS = (
    "common", "events", "interface", "localisation", "localization",
    "decisions", "gfx/interface", "sound", "prescripted_countries",
    "map", "flags", "fonts", "dlc", "tutorial", "species_names",
)

# 这些「定义」不是全局游戏对象，不能拿来做跨模组重定义比较：
#
# ``@name = 123``   与 ``$name$`` 是 Clausewitz 的**文件局部**宏/变量，
#                   只在同一个文件内有效。不同模组各自定义 @cost0
#                   完全正常，报成冲突就是误报。
# ``key = 值``      在 component_templates 之类文件里，顶层还可以出现
#                   直接的数值赋值（``size = 20``），同样不是具名对象。
_LOCAL_MACRO_PREFIX = ("@", "$", "_")
# 顶层出现这些名字时属于「结构性字段」而非游戏对象
_STRUCTURAL_KEYS = {
    "name", "version", "tags", "path", "remote_file_id", "supported_version",
    "picture", "size", "type", "value", "key", "hidden", "trigger", "effect",
    "weight", "modifier", "potential", "allow", "color", "icon", "desc",
    "localization", "prerequisites", "possible", "ai_weight", "factor",
}


def is_comparable_definition(name: str) -> bool:
    """判断一个顶层名字是否值得做跨模组重定义比较。"""
    if not name:
        return False
    if name.startswith(_LOCAL_MACRO_PREFIX):
        return False
    if name.startswith("__"):
        return False
    if name.lower() in _STRUCTURAL_KEYS:
        return False
    # 单个字符、纯符号的名字没有意义
    if len(name) < 2:
        return False
    return True

# 纯二进制/媒体扩展名：只比较是否覆盖，不解析
BINARY_EXT = {
    ".dds", ".png", ".tga", ".jpg", ".jpeg", ".bmp", ".gif", ".webp",
    ".ogg", ".wav", ".mp3", ".wem", ".bnk", ".bank", ".fsb",
    ".ttf", ".otf", ".fnt", ".bmfc",
    ".dmp", ".zip", ".7z", ".rar", ".exe", ".dll",
}

# 需要语义解析的文本扩展名
TEXT_EXT = {".txt", ".yml", ".yaml", ".gui", ".gfx", ".asset", ".csv", ".info"}

# 群星里「具名对象」所在的脚本目录 -> 类别名（用于生成人话说明）
CATEGORY_BY_DIR: dict[str, str] = {    "common/scripted_triggers": "scripted_trigger 脚本触发器",
    "common/scripted_effects": "scripted_effect 脚本效果",
    "common/scripted_variables": "scripted_variable 脚本变量",
    "common/scripted_loc": "scripted_loc 动态本地化",
    "common/event_chains": "event_chain 事件链",
    "common/technology": "technology 科技",
    "common/buildings": "building 建筑",
    "common/districts": "district 区划",
    "common/governments": "government 政体",
    "common/governments/civics": "civic 国策",
    "common/governments/authorities": "authority 权威",
    "common/traits": "trait 特质",
    "common/species_classes": "species_class 物种分类",
    "common/component_templates": "component_template 舰船部件",
    "common/section_templates": "section_template 舰船区段",
    "common/ship_sizes": "ship_size 舰船类型",
    "common/starbase_types": "starbase_type 星港类型",
    "common/megastructures": "megastructure 巨构",
    "common/planet_classes": "planet_class 星球类型",
    "common/star_classes": "star_class 恒星类型",
    "common/static_modifiers": "static_modifier 静态修正",
    "common/opinion_modifiers": "opinion_modifier 观点修正",
    "common/edicts": "edict 法令",
    "common/policies": "policy 政策",
    "common/agendas": "agenda 议程",
    "common/ambient_objects": "ambient_object 环境物体",
    "common/anomalies": "anomaly 异常现象",
    "common/archaeological_site_types": "archaeological_site 考古遗址",
    "common/armies": "army 陆军",
    "common/ascension_perks": "ascension_perk 飞升天赋",
    "common/attitudes": "attitude 态度",
    "common/bombardment_stances": "bombardment_stance 轰炸姿态",
    "common/bypass": "bypass 通道",
    "common/casus_belli": "casus_belli 宣战理由",
    "common/colony_types": "colony_type 殖民地类型",
    "common/country_types": "country_type 国家类型",
    "common/deposits": "deposit 资源点",
    "common/diplomatic_actions": "diplomatic_action 外交行动",
    "common/ethics": "ethic 思潮",
    "common/fallen_empires": "fallen_empire 堕落帝国",
    "common/global_ship_designs": "global_ship_design 全局舰船设计",
    "common/governments/councils": "council 议会",
    "common/name_lists": "name_list 名称列表",
    "common/notification_modifiers": "notification_modifier 通知修正",
    "common/observables": "observable 观测",
    "common/on_actions": "on_action 事件钩子",
    "common/personalities": "personality AI 性格",
    "common/pop_jobs": "pop_job 人口职业",
    "common/precursor_civilizations": "precursor 先驱文明",
    "common/relics": "relic 遗珍",
    "common/resolution_categories": "resolution_category 议案分类",
    "common/resolutions": "resolution 议案",
    "common/solar_system_initializers": "solar_system_initializer 星系初始化",
    "common/special_projects": "special_project 特殊项目",
    "common/scripted_triggers/": "scripted_trigger",
    "events": "event 事件",
    "interface": "UI 界面定义",
    "localisation": "本地化文本",
    "localisation_synced": "同步本地化文本",
    "prescripted_countries": "预设国家",
    "map/galaxy": "银河生成",
    "map/setup_scenarios": "开局场景",
}


def category_for(rel: str) -> str:
    """把模组内的相对路径映射成「游戏系统类别」中文名。

        例如 ``common/technology/foo.txt`` → ``technology 科技``。
        用于界面上显示「这个模组改了什么」，以及冲突分类。
        """
    r = rel.lower()
    best = ""
    best_len = -1
    for prefix, cat in CATEGORY_BY_DIR.items():
        if r.startswith(prefix) and len(prefix) > best_len:
            best, best_len = cat, len(prefix)
    if best:
        return best
    # 兜底：取前两级目录
    parts = r.split("/")
    return "/".join(parts[:2]) if len(parts) > 1 else r


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass
class FileInfo:
    """模组内一个文件的元信息（路径、大小、行数、是否需要解析）。"""
    rel: str                    # 相对模组根的路径（小写）
    size: int = 0
    lines: int = 0
    is_text: bool = False
    owner: str = ""             # 模组 key
    content_hash: str = ""      # 仅文本文件、且需要比较时计算


@dataclass
class Definition:
    """一个具名对象的定义。"""

    name: str
    rel: str
    category: str
    body_hash: str
    line: int = 0
    owner: str = ""


@dataclass
class ModIndex:
    """单个模组的索引结果。"""

    key: str
    name: str
    root: str
    files: dict[str, FileInfo] = field(default_factory=dict)
    definitions: dict[str, list[Definition]] = field(default_factory=dict)
    # 本地化键：localisation 文件里的 key
    loc_keys: dict[str, str] = field(default_factory=dict)   # key -> rel
    total_bytes: int = 0
    file_count: int = 0
    text_file_count: int = 0
    errors: list[str] = field(default_factory=list)
    parsed_files: int = 0

    def category_counts(self) -> dict[str, int]:
        """按类别统计本模组的具名对象数量。"""
        out: dict[str, int] = {}
        for d in self.definitions:
            out[d] = out.get(d, 0) + len(self.definitions[d])
        return out


def _is_text_file(p: Path) -> bool:
    ext = p.suffix.lower()
    if ext in BINARY_EXT:
        return False
    if ext in TEXT_EXT:
        return True
    # 无扩展名的常见脚本
    return ext == ""


_LOC_ENTRY_RE = re.compile(r'^\s*([A-Za-z0-9_.\-]+)\s*:\s*(?:\d+\s*)?"(.*)"\s*$')


def _parse_localisation(text: str) -> dict[str, str]:
    """解析 yml 本地化文件，返回 ``key -> 文本``。"""
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _LOC_ENTRY_RE.match(line)
        if m:
            key = m.group(1)
            # 键名必须以字母开头且长度 >= 2，否则多半是把普通行误认成键
            if len(key) < 2 or not key[0].isalpha():
                continue
            # 过滤掉 yml 里的元数据行（l_english: 之类）
            if key.lower() in ("l_english", "l_simp_chinese", "l_chinese",
                               "l_french", "l_german", "l_spanish",
                               "l_russian", "l_polish", "l_braz_por",
                               "l_japanese", "l_korean"):
                continue
            out[key] = m.group(2)
    return out


class Scanner:
    """模组内容扫描器。"""

    def __init__(self, *, max_text_bytes: int = 24 * 1024 * 1024,
                 progress: Callable[[str, float], None] | None = None,
                 should_stop: Callable[[], bool] | None = None) -> None:
        self.max_text_bytes = max_text_bytes
        self.progress = progress
        self.should_stop = should_stop
        self._cache: dict[str, ModIndex] = {}

    # -- 对外接口 ---------------------------------------------------
    def scan(self, mods: Iterable[Mod], *, semantic: bool = True) -> dict[str, ModIndex]:
        """批量扫描多个模组，返回 ``{模组key: ModIndex}``。

        ``semantic=False`` 时只做文件级索引（快但不检测脚本重定义）。
        """
        mods = list(mods)
        out: dict[str, ModIndex] = {}
        for i, m in enumerate(mods):
            if self.should_stop and self.should_stop():
                break
            if self.progress:
                self.progress(f"扫描 {m.name}", (i + 1) / max(1, len(mods)))
            idx = self.scan_mod(m, semantic=semantic)
            if idx is not None:
                out[m.key] = idx
        return out

    def scan_mod(self, mod: Mod, *, semantic: bool = True) -> ModIndex | None:
        """扫描单个模组。

        返回 ``None`` 表示该模组的内容目录不存在。
        同一目录会被缓存复用（多个描述符指向同一工坊目录的情况）。
        """
        root = mod.resolve_content_path()
        if not root:
            return None
        cache_key = norm(root)
        cached = self._cache.get(cache_key)
        if cached is not None and semantic:
            # 复用已扫描的结果，但保留新的 key/name
            cached.key = mod.key
            cached.name = mod.name
            return cached
        idx = ModIndex(key=mod.key, name=mod.name, root=root)
        self._walk(idx, root, semantic=semantic)
        if semantic:
            self._cache[cache_key] = idx
        return idx

    # -- 内部实现 ---------------------------------------------------
    def _walk(self, idx: ModIndex, root: str, *, semantic: bool) -> None:
        base = Path(long_path(root))
        stack: list[tuple[Path, str]] = [(base, "")]
        seen = 0
        while stack:
            if self.should_stop and self.should_stop():
                return
            d, prefix = stack.pop()
            try:
                it = os.scandir(long_path(d))
            except (OSError, PermissionError) as exc:
                idx.errors.append(f"无法读取 {d}: {exc}")
                continue
            with it:
                for entry in it:
                    seen += 1
                    if seen % 4096 == 0 and self.should_stop and self.should_stop():
                        return
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            sub = f"{prefix}{entry.name}/"
                            # 跳过明显的非游戏内容，避免无谓 IO
                            if entry.name.lower() in (
                                ".git", ".svn", "__pycache__", ".vs", "node_modules",
                            ):
                                continue
                            stack.append((Path(entry.path), sub))
                            continue
                        if not entry.is_file(follow_symlinks=False):
                            continue
                    except OSError:
                        continue
                    rel = (prefix + entry.name).lower()
                    try:
                        st = entry.stat(follow_symlinks=False)
                        size = st.st_size
                    except OSError:
                        size = 0
                    p = Path(entry.path)
                    is_text = _is_text_file(p)
                    fi = FileInfo(rel=rel, size=size, is_text=is_text,
                                  owner=idx.key)
                    idx.files[rel] = fi
                    idx.file_count += 1
                    idx.total_bytes += size
                    if is_text:
                        idx.text_file_count += 1
                        if semantic and size <= self.max_text_bytes and \
                                self._in_semantic_scope(rel):
                            self._index_text(idx, p, rel, fi)
        return

    @staticmethod
    def _in_semantic_scope(rel: str) -> bool:
        return any(rel.startswith(r) for r in SEMANTIC_ROOTS)

    def _index_text(self, idx: ModIndex, p: Path, rel: str, fi: FileInfo) -> None:
        try:
            raw = p.read_bytes()
        except OSError as exc:
            idx.errors.append(f"读取失败 {rel}: {exc}")
            return
        text = decode_text(raw)
        idx.parsed_files += 1
        fi.lines = text.count("\n") + 1

        # 本地化文件特殊处理：键是本地化 key
        if rel.startswith(("localisation/", "localization/", "localisation_synced/")) \
                or rel.endswith(".yml"):
            try:
                for k, v in _parse_localisation(text).items():
                    idx.loc_keys[k] = rel
            except Exception as exc:
                idx.errors.append(f"本地化解析失败 {rel}: {exc}")
            return

        fi.content_hash = sha1_text(text)
        try:
            root_node = parse_string(text)
        except Exception as exc:
            idx.errors.append(f"脚本解析失败 {rel}: {exc}")
            return
        cat = category_for(rel)
        defs = flatten_definitions(root_node)
        for name, node in defs.items():
            if not is_comparable_definition(name):
                continue
            d = Definition(
                name=name, rel=rel, category=cat,
                body_hash=sha1_text(canonical_text(node)),
                line=node.line, owner=idx.key,
            )
            idx.definitions.setdefault(name, []).append(d)


# ---------------------------------------------------------------------------
# 索引集合
# ---------------------------------------------------------------------------

@dataclass
class IndexSet:
    """多个模组的索引集合，提供反查能力。"""

    indexes: dict[str, ModIndex] = field(default_factory=dict)
    names: dict[str, str] = field(default_factory=dict)

    def add(self, idx: ModIndex) -> None:
        """加入一个模组的索引。"""
        self.indexes[idx.key] = idx
        self.names[idx.key] = idx.name

    def total_bytes(self) -> int:
        """所有已索引模组的体量合计（字节）。"""
        return sum(i.total_bytes for i in self.indexes.values())

    def file_owners(self) -> dict[str, list[str]]:
        """``相对路径 -> [拥有它的模组 key]``。"""
        out: dict[str, list[str]] = {}
        for key, idx in self.indexes.items():
            for rel in idx.files:
                out.setdefault(rel, []).append(key)
        return out

    def definition_owners(self) -> dict[str, list[Definition]]:
        """``定义名 -> [所有定义]``。"""
        out: dict[str, list[Definition]] = {}
        for idx in self.indexes.values():
            for name, defs in idx.definitions.items():
                out.setdefault(name, []).extend(defs)
        return out

    def loc_key_owners(self) -> dict[str, list[str]]:
        """``本地化键 -> [定义它的模组]``，用于汉化冲突检测。"""
        out: dict[str, list[str]] = {}
        for key, idx in self.indexes.items():
            for lk in idx.loc_keys:
                out.setdefault(lk, []).append(key)
        return out

    def size_of(self, key: str) -> int:
        """取某个模组的索引体量；未索引时返回 0。"""
        i = self.indexes.get(key)
        return i.total_bytes if i else 0
