"""补丁模组服务：把 AI 生成或玩家手写的补丁变成真正的群星模组。

统一处理三件事：

1. **落盘为模组** —— 在 ``mod/`` 下创建 ``zzz_SMD_<名字>/``，写入补丁文件、
   ``descriptor.mod``、``README``，并在 ``mod/`` 放一份 ``.mod`` 描述符
2. **登记到 playset** —— 让补丁排在加载顺序最后（否则覆盖不了别人）
3. **管理既有补丁** —— 列出、读取、更新、删除

补丁模组统一用 ``zzz_SMD_`` 前缀，方便一键识别与清除。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from .. import APP_NAME, PATCH_PREFIX, UNIFIED_PATCH_NAME
from ..ai.patchgen import (ALLOWED_DIRS, PatchFile, PatchIssue,
                           verify_patch, verify_patch_text)
from ..config import Paths
from ..paths import (exists, human_size, is_dir, is_file, long_path, read_text,
                     write_text)
from ..paradox import flatten_definitions, parse_string

PATCH_PREFIX = "zzz_SMD_"

# 玩家手写补丁的模板
TEMPLATES: dict[str, dict[str, str]] = {
    "trigger_merge": {
        "name": "合并两个同名触发器（OR 组合）",
        "desc": "两个模组都想改同一个 scripted_trigger 时，"
                "用 OR 把双方条件都保留下来。",
        "rel": "common/scripted_triggers/zzz_smd_patch.txt",
        "text": """# 合并冲突触发器：让双方的判断条件同时生效
# 注意：群星是「整体覆盖」语义，必须写出完整的定义
my_conflicting_trigger = {
\tOR = {
\t\t# ── 模组 A 原本的条件 ──
\t\thas_technology = "tech_from_mod_a"

\t\t# ── 模组 B 原本的条件 ──
\t\tAND = {
\t\t\tis_country = yes
\t\t\tnum_pops > 25
\t\t}
\t}
}
""",
    },
    "variable_take_max": {
        "name": "数值取双方最大值",
        "desc": "两个模组定义了同名脚本变量但数值不同时，取较大者"
                "（通常能让两边的功能都不被削弱）。",
        "rel": "common/scripted_variables/zzz_smd_patch.txt",
        "text": """# 同名脚本变量取双方最大值
@conflicting_value = 50
""",
    },
    "variable_take_min": {
        "name": "数值取双方最小值",
        "desc": "与上一条相反，取较小者（偏向保守/平衡）。",
        "rel": "common/scripted_variables/zzz_smd_patch.txt",
        "text": """# 同名脚本变量取双方最小值
@conflicting_value = 10
""",
    },
    "ship_component": {
        "name": "合并舰船部件定义",
        "desc": "两个大型模组（NSC/ACOT/巨构）覆盖了同一个部件模板时，"
                "把双方差异字段合并进一份定义。",
        "rel": "common/component_templates/zzz_smd_patch.txt",
        "text": """# 合并舰船部件定义
# 逐个字段核对：只有确认不冲突的字段才能这样合并
my_component = {
\tkey = "my_component"
\tsize = small
\tentity = "my_component_entity"
\t# ── 来自模组 A ──
\tmodifier = {
\t\t ship_weapon_damage_mult = 0.10
\t}
\t# ── 来自模组 B ──
\tpower = 15
\thull = 200
}
""",
    },
    "static_modifier": {
        "name": "合并静态修正量",
        "desc": "两个模组各自往同名 static_modifier 里加了修正，"
                "合并成一份完整定义。",
        "rel": "common/static_modifiers/zzz_smd_patch.txt",
        "text": """# 合并静态修正量
my_static_modifier = {
\t# ── 模组 A 的修正 ──
\tcountry_resource_energy_mult = 0.10

\t# ── 模组 B 的修正 ──
\tcountry_resource_minerals_mult = 0.10
}
""",
    },
    "on_action_merge": {
        "name": "合并 on_action 事件钩子",
        "desc": "两个模组都往同一个 on_action 里挂事件时，"
                "保留双方的事件调用（顺序会影响执行先后）。",
        "rel": "common/on_actions/zzz_smd_patch.txt",
        "text": """# 合并 on_action 钩子：双方的事件都会触发
on_game_start = {
\t# ── 模组 A 挂的事件 ──
\tevents = {
\t\tmod_a_startup.1
\t}
\t# ── 模组 B 挂的事件 ──
\trandom_events = {
\t\tchance = {
\t\t\tbase = 100
\t\t}
\t\tevents = {
\t\t\tmod_b_startup.1
\t\t}
\t}
}
""",
    },
    "loc_override": {
        "name": "修正本地化文本（汉化冲突）",
        "desc": "两个汉化模组覆盖同一个文本键时，用它固定最终显示的文字。",
        "rel": "localisation/simp_chinese/zzz_smd_patch_l_simp_chinese.yml",
        "text": """l_simp_chinese:
 # 固定冲突文本键的最终显示内容
 conflicting_key:0 "这里填写你希望的最终文本"
 another_key:0 "另一条文本"
""",
    },
    "disable_effect": {
        "name": "清空某个定义（变相禁用）",
        "desc": "把冲突对象定义成空块，让它不产生任何效果。"
                "适合「两个模组都别生效」的情况。",
        "rel": "common/scripted_triggers/zzz_smd_patch.txt",
        "text": """# 把冲突定义清空：任何判断都返回假
# 用在「两个模组的功能都不想要」的场合
my_conflicting_trigger = {
\talways = no
}
""",
    },
}


@dataclass
class PatchRecord:
    """一个已存在的补丁模组。"""

    name: str = ""              # 目录名（含前缀）
    title: str = ""             # 描述符里的显示名
    path: str = ""
    files: list[dict] = field(default_factory=list)
    created: float = 0.0
    bytes: int = 0
    registered: bool = False    # 是否已在启动器数据库里
    enabled: bool = False       # 是否已在 playset 中启用
    source: str = ""            # ai | manual | unified | unknown
    unified: bool = False       # 是不是那个"统一补丁模组"
    entries: list = field(default_factory=list)   # 统一模组的账本记录
    source_mix: list = field(default_factory=list)  # 账本里出现过的来源

    @property
    def size_human(self) -> str:
        """体量的人类可读形式。"""
        return human_size(self.bytes)

    def to_dict(self) -> dict:
        """序列化，供界面展示。"""
        return {
            "name": self.name, "title": self.title, "path": self.path,
            "files": self.files, "created": self.created,
            "created_str": (datetime.fromtimestamp(self.created)
                            .strftime("%Y-%m-%d %H:%M") if self.created else ""),
            "bytes": self.bytes, "size_human": self.size_human,
            "registered": self.registered, "enabled": self.enabled,
            "source": self.source,
            "unified": self.unified,
            "entries": self.entries,
            "source_mix": self.source_mix,
        }


# ---------------------------------------------------------------------------
# 路径工具
# ---------------------------------------------------------------------------

def patch_root(paths: Paths) -> Path:
    """补丁模组的父目录（即群星的 ``mod`` 目录）。"""
    return Path(paths.user_dir) / "mod"


def _slug(name: str, limit: int = 40) -> str:
    s = re.sub(r"[^0-9A-Za-z_\u4e00-\u9fff]+", "_", name or "").strip("_")
    return (s or "patch")[:limit]


def make_patch_name(title: str, source: str = "ai") -> str:
    """生成补丁模组的目录名。"""
    stamp = datetime.now().strftime("%m%d_%H%M%S")
    tag = "AI" if source == "ai" else "手写"
    return f"{PATCH_PREFIX}{tag}_{_slug(title, 28)}_{stamp}"


# ---------------------------------------------------------------------------
# 统一补丁模组
# ---------------------------------------------------------------------------
# 所有补丁都写进**同一个**模组，而不是一次生成一个。
#
# 为什么改：批量生成一次能出几十上百份补丁，一份一个模组的话，
# 启动器列表会被刷屏、加载顺序里排一长串、想整体关掉还要一个个点。
# 合并成一个之后，用户只需要管一个开关。
#
# 代价与应对：改了哪个文件、针对哪个冲突，靠 MANIFEST.json 记账，
# README 也按账本重新生成，不会因为合并就丢失可追溯性。
#
# 常量定义在 ``smd/__init__.py``：``modinfo`` 也要用它们把补丁模组
# 排除在冲突检测之外，放在这里会造成循环导入。
UNIFIED_NAME = UNIFIED_PATCH_NAME
UNIFIED_TITLE = f"{APP_NAME} · 兼容补丁"
MANIFEST_NAME = "SMD_MANIFEST.json"


def unified_patch_dir(paths: Paths) -> Path:
    """统一补丁模组的目录。"""
    return patch_root(paths) / UNIFIED_NAME


def is_unified_patch(name: str) -> bool:
    """这个目录名是不是统一补丁模组。"""
    return name == UNIFIED_NAME


def _read_manifest(d: Path) -> dict:
    """读补丁账本；没有或损坏就返回空账本。"""
    p = d / MANIFEST_NAME
    try:
        if is_file(p):
            data = json.loads(read_text(p))
            if isinstance(data, dict):
                data.setdefault("entries", [])
                return data
    except (OSError, ValueError):
        pass
    return {"entries": []}


def _write_manifest(d: Path, man: dict) -> None:
    """写补丁账本（不带 BOM：它是给人看也给人改的 JSON）。"""
    try:
        write_text(d / MANIFEST_NAME,
                   json.dumps(man, ensure_ascii=False, indent=2), bom=False)
    except OSError:
        pass


def _write_descriptors(paths: Paths, d: Path) -> None:
    """写好补丁模组的描述符：目录里的 ``descriptor.mod`` 与 mod/ 下的 ``.mod``。

    两个都要有：``descriptor.mod`` 是模组自己声明身份用的，
    ``.mod`` 是启动器**发现**这个模组用的。少写 ``.mod`` 的话
    模组在启动器里根本不会出现——文件都在，就是加载不到。
    """
    write_text(d / "descriptor.mod", (
        f'name="{UNIFIED_TITLE}"\n'
        f'version="1.0"\n'
        f'supported_version="v4.*"\n'
        f'tags={{\n\t"Fixes"\n}}\n'
    ))
    write_text(patch_root(paths) / f"{UNIFIED_NAME}.mod", (
        f'name="{UNIFIED_TITLE}"\n'
        f'version="1.0"\n'
        f'supported_version="v4.*"\n'
        f'path="{str(d).replace(chr(92), "/")}"\n'
        f'tags={{\n\t"Fixes"\n}}\n'
    ))


def save_patch(paths: Paths, *, title: str, files: list[PatchFile],
               description: str = "", source: str = "manual",
               conflict: str = "", provider: str = "",
               extra_readme: str = "") -> dict:
    """把补丁写进**统一补丁模组**，返回 ``{ok, name, path, added, ...}``。

    已存在就合并进去：
    * 新文件加进去，**同名文件覆盖**（新补丁针对同一路径时以新的为准）
    * 账本 MANIFEST.json 追加一条记录
    * README 按账本重新生成，读起来还是完整的清单
    """
    if not files:
        return {"ok": False, "error": "没有可写入的补丁文件"}

    if not paths.user_dir or not is_dir(paths.user_dir):
        return {"ok": False, "error": "未找到群星用户数据目录，无法创建补丁模组"}

    name = UNIFIED_NAME
    d = unified_patch_dir(paths)
    existed = is_dir(d)
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return {"ok": False, "error": f"创建目录失败：{exc}"}

    man = _read_manifest(d)
    written: list[str] = []
    replaced: list[str] = []
    try:
        for f in files:
            rel = f.rel.replace("\\", "/").lstrip("/")
            target = d / rel.replace("/", os.sep)
            target.parent.mkdir(parents=True, exist_ok=True)
            if is_file(target):
                replaced.append(rel)
            write_text(target, f.text)
            written.append(rel)

        _write_descriptors(paths, d)

        # 记账：同一个 rel 以最新一次为准，避免账本里堆重复路径
        entries = [e for e in man.get("entries", [])
                   if isinstance(e, dict) and e.get("rel") not in written]
        now = f"{datetime.now():%Y-%m-%d %H:%M:%S}"
        for rel in written:
            entries.append({
                "rel": rel, "title": title, "source": source,
                "conflict": conflict, "provider": provider,
                "at": now,
            })
        man["entries"] = entries
        man["updated"] = now
        man["title"] = UNIFIED_TITLE
        _write_manifest(d, man)

        readme = _build_readme(UNIFIED_TITLE, description, entries, source,
                               conflict, provider, extra_readme)
        write_text(d / "README_SMD.txt", readme)
    except OSError as exc:
        return {"ok": False, "error": f"写入补丁失败：{exc}"}

    return {
        "ok": True, "name": name, "title": UNIFIED_TITLE, "path": str(d),
        "mod_file": str(patch_root(paths) / f"{name}.mod"),
        "files": written, "source": source,
        "added": len(written) - len(replaced),
        "replaced": replaced,
        "existed": existed,
        "total_files": len(man.get("entries", [])),
    }


def _build_readme(title: str, description: str, entries: list[dict],
                  source: str, conflict: str, provider: str,
                  extra: str) -> str:
    """按账本重新生成 README。

    ``entries`` 是 MANIFEST 里的记录（``rel/title/source/conflict/at``），
    不是纯文件名——合并成一个模组后，README 要能说清
    "哪个文件是为哪个冲突加的、什么时候加的"，否则可追溯性就丢了。
    """
    src = {"ai": f"由 AI 生成（{provider or '未知模型'}）",
           "manual": "由玩家手写",
           "local": "由本地规则引擎生成"}.get(source, source or "未知来源")
    lines = [
        "=" * 58,
        f"  {title}",
        "=" * 58,
        "",
        f"最近一次来源：{src}",
        f"最近更新时间：{datetime.now():%Y-%m-%d %H:%M:%S}",
        f"补丁文件总数：{len(entries)}",
        "",
        "本模组由「熵光矩阵」统一维护：**后续生成的补丁都会合并进这一个模组**，",
        "不会每生成一次就多出一个模组。",
        "",
    ]
    if conflict:
        lines += [f"最近针对的冲突：{conflict}", ""]
    if description:
        lines += ["【最近一次说明】", description, ""]
    if extra:
        lines += ["【附加信息】", extra, ""]
    lines += ["【包含的补丁文件】"]
    for e in entries:
        if not isinstance(e, dict):
            lines.append(f"  - {e}")
            continue
        rel = e.get("rel", "")
        t = e.get("title") or ""
        at = e.get("at") or ""
        s = {"ai": "AI", "local": "本地", "manual": "手写"}.get(
            e.get("source", ""), e.get("source", ""))
        tail = "　".join(x for x in (s, at) if x)
        lines.append(f"  - {rel}")
        if t or tail:
            lines.append(f"      针对：{t}" + (f"　（{tail}）" if tail else ""))
    lines += [
        "",
        "【如何撤销】",
        "  1. 在 Paradox 启动器里取消勾选本模组（推荐，可随时再打开）",
        "  2. 或删除整个补丁目录与同名 .mod 文件",
        "  3. 想只撤销某一条：删掉上面对应的文件即可",
        "",
        "【注意】",
        "  本模组由「熵光矩阵」生成，请自行确认其效果符合预期。",
        "  若游戏出现异常，可先禁用本模组再排查。",
        "",
    ]
    return "\n".join(lines)


def list_patches(paths: Paths, *, library: Any = None) -> list[dict]:
    """列出所有 ``zzz_SMD_`` 补丁模组。"""
    out: list[dict] = []
    root = patch_root(paths)
    if not is_dir(root):
        return out

    enabled_keys: set[str] = set()
    if library is not None:
        for m in library.mods.values():
            if m.enabled_in_playset and m.remote_file_id == "" and \
                    PATCH_PREFIX.lower() in (m.path or "").lower():
                enabled_keys.add(m.key)

    try:
        entries = sorted(root.iterdir())
    except OSError:
        return out

    for e in entries:
        if not e.name.startswith(PATCH_PREFIX):
            continue
        if not e.is_dir():
            continue
        rec = PatchRecord(name=e.name, path=str(e))
        try:
            st = e.stat()
            rec.created = st.st_mtime
        except OSError:
            pass
        # 描述符里的显示名
        desc = e / "descriptor.mod"
        if is_file(desc):
            try:
                raw = parse_string(read_text(desc))
                rec.title = str(raw.scalar("name") or e.name)
            except Exception:
                rec.title = e.name
        else:
            rec.title = e.name
        # 文件清单
        total = 0
        for dirpath, _dirs, fnames in os.walk(long_path(e)):
            for fn in fnames:
                full = os.path.join(dirpath, fn)
                try:
                    size = os.path.getsize(full)
                except OSError:
                    size = 0
                total += size
                rel = os.path.relpath(full, str(e)).replace("\\", "/")
                if rel in ("descriptor.mod", MANIFEST_NAME) \
                        or rel.startswith("README"):
                    continue
                rec.files.append({"rel": rel, "bytes": size,
                                  "size_human": human_size(size)})
        rec.bytes = total
        # 来源：统一补丁模组看账本，其余按目录名推断
        if is_unified_patch(e.name):
            man = _read_manifest(e)
            rec.source = "unified"
            rec.entries = man.get("entries", [])
            rec.unified = True
            # 账本里记录的来源分布，界面上能看出"这个模组里 AI 和本地各占多少"
            srcs = [x.get("source") for x in rec.entries
                    if isinstance(x, dict)]
            if srcs:
                rec.source_mix = sorted({s for s in srcs if s})
        elif "AI_" in e.name:
            rec.source = "ai"
        elif "手写" in e.name:
            rec.source = "manual"
        # 是否已登记/启用
        rec.registered = is_file(patch_root(paths) / f"{e.name}.mod")
        if library is not None:
            for m in library.mods.values():
                if PATCH_PREFIX.lower() in (m.path or "").lower() and \
                        Path(m.path).name == e.name:
                    rec.enabled = m.enabled_in_playset
                    rec.registered = True
                    break
        out.append(rec.to_dict())
    out.sort(key=lambda r: -r.get("created", 0))
    return out


def read_patch(paths: Paths, name: str, rel: str = "") -> dict:
    """读取补丁模组的一个文件内容。"""
    root = patch_root(paths) / name
    if not is_dir(root):
        return {"error": "补丁不存在"}
    if not rel:
        # 返回第一个脚本文件
        recs = [p for p in list_patches(paths) if p["name"] == name]
        if recs and recs[0]["files"]:
            rel = recs[0]["files"][0]["rel"]
        else:
            return {"error": "该补丁没有可读文件"}
    target = (root / rel.replace("/", os.sep)).resolve()
    try:
        target.relative_to(root.resolve())
    except ValueError:
        return {"error": "路径越界，已拒绝"}
    if not is_file(target):
        return {"error": f"文件不存在：{rel}"}
    try:
        return {"ok": True, "name": name, "rel": rel,
                "text": read_text(target)}
    except OSError as exc:
        return {"error": str(exc)}


def update_patch_file(paths: Paths, name: str, rel: str, text: str) -> dict:
    """覆盖补丁里的某个文件（供编辑器保存）。"""
    root = patch_root(paths) / name
    if not is_dir(root):
        return {"ok": False, "error": "补丁不存在"}
    target = (root / rel.replace("/", os.sep)).resolve()
    try:
        target.relative_to(root.resolve())
    except ValueError:
        return {"ok": False, "error": "路径越界，已拒绝"}
    # 先校验再写，避免把坏补丁存进去
    ok, issues, _files = verify_patch_text(text, rel=rel)
    if not ok:
        blocking = [i for i in issues if i.is_blocking]
        return {"ok": False, "error": "校验未通过，未保存",
                "issues": [i.to_dict() for i in blocking]}
    try:
        write_text(target, text)
    except OSError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "issues": [i.to_dict() for i in issues]}


def delete_patch(paths: Paths, name: str) -> dict:
    """删除一个补丁模组（目录 + 描述符）。"""
    if not name.startswith(PATCH_PREFIX):
        return {"ok": False,
                "error": f"出于安全考虑，只允许删除 {PATCH_PREFIX} 开头的"
                         f"自动生成补丁"}
    root = patch_root(paths) / name
    removed: list[str] = []
    if is_dir(root):
        try:
            shutil.rmtree(long_path(root), ignore_errors=True)
            removed.append(str(root))
        except OSError as exc:
            return {"ok": False, "error": f"删除目录失败：{exc}"}
    modfile = patch_root(paths) / f"{name}.mod"
    if is_file(modfile):
        try:
            modfile.unlink()
            removed.append(str(modfile))
        except OSError:
            pass
    return {"ok": True, "removed": removed}


def validate_text(text: str, *, indexes: Any = None,
                  conflict_target: str = "", rel: str = "",
                  expect_files: bool = False) -> dict:
    """校验一段补丁文本。

    * ``rel`` 非空 —— 当作玩家手写的裸脚本，校验后会落到 ``rel``
    * ``rel`` 为空 —— 当作（可能带围栏的）完整补丁文本
    """
    ok, issues, files = verify_patch_text(
        text, indexes=indexes, conflict_target=conflict_target,
        rel=rel, expect_files=expect_files)
    return {
        "ok": ok,
        "issues": [i.to_dict() for i in issues],
        "files": [f.to_dict() for f in files],
        "blocking": len([i for i in issues if i.is_blocking]),
    }


def get_templates() -> list[dict]:
    """返回手写补丁的模板列表。"""
    return [
        {"key": k, "name": v["name"], "desc": v["desc"], "rel": v["rel"],
         "text": v["text"]}
        for k, v in TEMPLATES.items()
    ]
