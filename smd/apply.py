"""方案落地：把求解结果真正写回群星。

写入目标
--------
1. ``launcher-v2.sqlite`` 的 ``playsets_mods``（位置 / 启用位）
2. ``dlc_load.json`` 的 ``enabled_mods``（游戏实际读取的清单）
3. ``mod/<补丁名>/`` + ``mod/<补丁名>.mod``（生成的兼容补丁模组）

安全策略
--------
* 任何写入前都先备份到 ``%LOCALAPPDATA%/StellarisModDoctor/backups/<时间戳>/``
* 检测到 Paradox 启动器正在运行则拒绝写入（它会锁库并覆盖我们的改动）
* 全部写入走「临时文件 + 原子替换」，避免半途失败把库写坏
* 每个动作都返回结构化结果，界面可逐条展示
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable

from .config import Paths, app_data_dir
from .modinfo import Mod, ModLibrary, parse_mod_file
from .paths import (exists, is_dir, is_file, long_path, norm, read_text,
                    write_text)
from .solver import PatchProposal, Solution, TIER_TRANSLATION

PATCH_MOD_PREFIX = "zzz_SMD_"

# 会锁住 launcher-v2.sqlite / dlc_load.json 的进程名（全部小写）。
# 注意必须写得**具体**：早先版本用 "launcher.exe" 做子串匹配，
# 结果把 SakuraLauncher.exe 这类无关程序也当成 Paradox 启动器，
# 导致写入被无谓地拦住。
BLOCKING_PROCESSES = {
    "paradox launcher.exe",
    "bootstrapper.exe",          # Paradox 启动器的引导进程
    "stellaris.exe",
    "dowser.exe",                # 旧版 Paradox 启动器
}
# 名称里含这些片段也视为 Paradox 启动器
BLOCKING_SUBSTRINGS = ("paradox launcher", "paradoxlauncher", "pdxlauncher")


def launcher_running() -> list[str]:
    """检测 Paradox 启动器 / 群星是否在运行（会锁库）。"""
    found: list[str] = []
    try:
        import subprocess

        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        out = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=15,
            startupinfo=si, creationflags=0x08000000,
        ).stdout
    except Exception:
        return found
    for line in out.splitlines():
        parts = [p.strip('" ') for p in line.split('","')]
        if not parts:
            continue
        name = parts[0].strip('"')
        low = name.lower()
        if low in BLOCKING_PROCESSES:
            tag = "（游戏本体）" if low == "stellaris.exe" else "（Paradox 启动器）"
            found.append(name + tag)
        elif any(sub in low for sub in BLOCKING_SUBSTRINGS):
            found.append(name + "（Paradox 启动器）")
    # 去重保序
    seen: set[str] = set()
    uniq: list[str] = []
    for f in found:
        if f not in seen:
            seen.add(f)
            uniq.append(f)
    return uniq

@dataclass
class Action:
    """一个已执行/待执行的动作。"""

    kind: str
    description: str
    ok: bool = True
    detail: str = ""
    undo: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        """序列化单个动作。"""
        return self.__dict__


@dataclass
class ApplyResult:
    """一次「应用方案」的完整结果（动作清单 + 备份目录 + 错误）。"""
    actions: list[Action] = field(default_factory=list)
    backup_dir: str = ""
    ok: bool = True
    errors: list[str] = field(default_factory=list)

    def add(self, kind: str, description: str, ok: bool = True,
            detail: str = "", **undo: Any) -> Action:
        """追加一个动作；``ok=False`` 会把整体标记为失败。"""
        a = Action(kind=kind, description=description, ok=ok, detail=detail,
                   undo=undo)
        self.actions.append(a)
        if not ok:
            self.ok = False
            self.errors.append(f"{description}: {detail}")
        return a

    def to_dict(self) -> dict:
        """序列化，供界面逐条展示。"""
        return {
            "ok": self.ok,
            "backup_dir": self.backup_dir,
            "errors": self.errors,
            "actions": [a.to_dict() for a in self.actions],
            "count": len(self.actions),
        }


def backup_root() -> Path:
    """备份根目录（``%LOCALAPPDATA%/StellarisModDoctor/backups``）。"""
    p = app_data_dir() / "backups"
    p.mkdir(parents=True, exist_ok=True)
    return p


def make_backup(paths: Paths, label: str = "manual") -> str:
    """备份关键文件，返回备份目录。"""
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dst = backup_root() / f"{stamp}_{label}"
    dst.mkdir(parents=True, exist_ok=True)
    for src in (paths.launcher_db, paths.dlc_load, paths.settings_txt):
        try:
            if is_file(src):
                shutil.copy2(long_path(src), long_path(dst / Path(src).name))
        except OSError:
            continue
    return str(dst)


def list_backups() -> list[dict]:
    """列出所有备份（新→旧），含内含文件名。"""
    out: list[dict] = []
    root = backup_root()
    try:
        for d in sorted(root.iterdir(), reverse=True):
            if d.is_dir():
                files = list(d.iterdir())
                out.append({
                    "path": str(d), "name": d.name,
                    "files": [f.name for f in files],
                    "created": d.stat().st_mtime,
                })
    except OSError:
        pass
    return out


# ---------------------------------------------------------------------------
# playset 写入
# ---------------------------------------------------------------------------

def _connect(db: Path, readonly: bool = False) -> sqlite3.Connection:
    if readonly:
        return sqlite3.connect(f"file:{long_path(db)}?mode=ro", uri=True)
    con = sqlite3.connect(long_path(db), timeout=20)
    con.execute("PRAGMA busy_timeout=20000")
    return con


def read_playset_state(paths: Paths, playset_id: str = "") -> dict[str, Any]:
    """读取 playset 的原始状态（用于预览与回滚）。

    ``playset_id`` 留空 = 启动器里活动的那个；给了具体 id 就读它。
    **必须支持指定**：用户在工具里选了播放集 B 做分析，
    写回时若还写活动播放集 A，就成了"看的是 B、改的是 A" ——
    那是会毁配置的错位。
    """
    out: dict[str, Any] = {"playset_id": None, "playset_name": "",
                           "entries": [], "available": False}
    if not is_file(paths.launcher_db):
        return out
    try:
        con = _connect(paths.launcher_db, readonly=True)
    except sqlite3.Error:
        return out
    try:
        con.row_factory = sqlite3.Row
        cur = con.cursor()
        row = None
        if playset_id:
            cur.execute("SELECT id,name FROM playsets WHERE id=? LIMIT 1",
                        (playset_id,))
            row = cur.fetchone()
        if row is None:
            cur.execute("SELECT id,name FROM playsets WHERE isActive=1 LIMIT 1")
            row = cur.fetchone()
        if not row:
            cur.execute("SELECT id,name FROM playsets ORDER BY updatedOn DESC LIMIT 1")
            row = cur.fetchone()
        if not row:
            return out
        out["playset_id"] = row["id"]
        out["playset_name"] = row["name"]
        out["available"] = True
        cur.execute(
            "SELECT pm.modId, pm.enabled, pm.position, m.displayName, m.name, "
            "m.steamId, m.gameRegistryId "
            "FROM playsets_mods pm LEFT JOIN mods m ON m.id=pm.modId "
            "WHERE pm.playsetId=? ORDER BY pm.position",
            (row["id"],),
        )
        for r in cur.fetchall():
            out["entries"].append({
                "modId": r["modId"], "enabled": bool(r["enabled"]),
                "position": r["position"],
                "displayName": r["displayName"] or r["name"] or "",
                "steamId": r["steamId"] or "",
                "gameRegistryId": r["gameRegistryId"] or "",
            })
    except sqlite3.Error:
        pass
    finally:
        con.close()
    return out


def write_playset(paths: Paths, order: list[str], disabled: list[str],
                  library: ModLibrary, patches: list[PatchProposal],
                  result: ApplyResult,
                  progress: Callable[[str, float], None] | None = None,
                  playset_id: str = "") -> None:
    """把加载顺序与启用状态写回 launcher-v2.sqlite。

    ``playset_id`` 必须与**分析时用的**是同一个 —— 否则会出现
    "看的是 B、改的是 A"的错位，直接毁掉另一套配置。
    """
    if not is_file(paths.launcher_db):
        result.add("db", "写回 playset", False, "找不到 launcher-v2.sqlite")
        return

    state = read_playset_state(paths, playset_id=playset_id)
    pid = state.get("playset_id")
    if pid is None:
        result.add("db", "写回 playset", False, "数据库中没有可用的 playset")
        return

    # modId 反查表
    key_to_modid: dict[str, str] = {}
    reg_to_modid: dict[str, str] = {}
    name_to_modid: dict[str, str] = {}
    try:
        con = _connect(paths.launcher_db, readonly=True)
        con.row_factory = sqlite3.Row
        cur = con.cursor()
        cur.execute("SELECT id,steamId,gameRegistryId,displayName,name FROM mods")
        for r in cur.fetchall():
            sid = str(r["steamId"] or "").strip()
            if sid:
                key_to_modid[f"ugc:{sid}"] = r["id"]
            reg = str(r["gameRegistryId"] or "")
            if reg:
                reg_to_modid[norm(reg)] = r["id"]
            nm = str(r["displayName"] or r["name"] or "")
            if nm:
                name_to_modid[norm(nm)] = r["id"]
        con.close()
    except sqlite3.Error as exc:
        result.add("db", "读取 mod 表", False, str(exc))
        return

    def resolve(key: str) -> str | None:
        if key in key_to_modid:
            return key_to_modid[key]
        m = library.get(key)
        if m is None:
            return None
        if m.game_registry_id and norm(m.game_registry_id) in reg_to_modid:
            return reg_to_modid[norm(m.game_registry_id)]
        if norm(m.name) in name_to_modid:
            return name_to_modid[norm(m.name)]
        return None

    try:
        con = _connect(paths.launcher_db)
        cur = con.cursor()
        cur.execute("BEGIN IMMEDIATE")

        ordered_ids: list[str] = []
        missing: list[str] = []
        for key in order:
            mid = resolve(key)
            if mid is None:
                missing.append(key)
                continue
            if mid not in ordered_ids:
                ordered_ids.append(mid)

        # 1) 写启用位与位置
        cur.execute("DELETE FROM playsets_mods WHERE playsetId=?", (pid,))
        for i, mid in enumerate(ordered_ids):
            cur.execute(
                "INSERT INTO playsets_mods(playsetId,modId,enabled,position) "
                "VALUES(?,?,?,?)",
                (pid, mid, 1, i),
            )
        result.add("db", f"写入 {len(ordered_ids)} 个模组的加载顺序",
                   detail=f"playset「{state.get('playset_name')}」", )
        if missing:
            result.add("db", f"{len(missing)} 个模组无法在数据库中找到",
                       False, "、".join(missing[:8]))

        # 2) 补丁模组登记
        for p in patches:
            if not p.files:
                continue
            reg = patch_registry_id(p)
            row = cur.execute(
                "SELECT id FROM mods WHERE gameRegistryId=?", (reg,)
            ).fetchone()
            if row:
                mid = row[0]
            else:
                mid = str(uuid.uuid4())
                cur.execute(
                    "INSERT INTO mods(id,pdxId,steamId,gameRegistryId,name,"
                    "displayName,version,requiredVersion,tags,status,source,"
                    "dirPath,isNew,size) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (mid, None, None, reg, p.name, p.name, "1.0",
                     "v4.*", json.dumps(["Fixes"], ensure_ascii=False),
                     "ready_to_play", "local",
                     str(patch_dir(paths, p)), 1, 0),
                )
            if mid not in ordered_ids:
                ordered_ids.append(mid)
                cur.execute(
                    "INSERT INTO playsets_mods(playsetId,modId,enabled,position) "
                    "VALUES(?,?,?,?)", (pid, mid, 1, len(ordered_ids) - 1))
            result.add("db", f"登记兼容补丁「{p.name}」")

        cur.execute("COMMIT")
        con.close()
    except sqlite3.Error as exc:
        try:
            con.rollback()
            con.close()
        except Exception:
            pass
        result.add("db", "写回 playset 失败", False, str(exc))
        return
    if progress:
        progress("写回 playset", 0.6)


def patch_registry_id(p: PatchProposal) -> str:
    """补丁模组在启动器数据库里的 ``gameRegistryId``。"""
    return f"mod/{PATCH_MOD_PREFIX}{_slug_of(p.name)}.mod"


def patch_dir(paths: Paths, p: PatchProposal) -> Path:
    """补丁模组的落地目录。"""
    return Path(paths.user_dir) / "mod" / f"{PATCH_MOD_PREFIX}{_slug_of(p.name)}"


def _slug_of(s: str) -> str:
    import re
    out = re.sub(r"[^0-9A-Za-z_]+", "_", s).strip("_")
    return (out or "patch")[:40]


# ---------------------------------------------------------------------------
# dlc_load.json 写入
# ---------------------------------------------------------------------------

def write_dlc_load(paths: Paths, library: ModLibrary, order: list[str],
                   disabled: list[str], patches: list[PatchProposal],
                   result: ApplyResult,
                   clean_stale: bool = True) -> None:
    """重建 dlc_load.json —— 游戏运行时真正读取的启用清单。"""
    data = dict(library.dlc_load or {})
    data.setdefault("disabled_dlcs", [])

    existing = [str(x) for x in data.get("enabled_mods", [])]
    existing_norm = {norm(x) for x in existing}

    # 删除已备份/禁用的
    disabled_regs = set()
    for key in disabled:
        m = library.get(key)
        if m and m.game_registry_id:
            disabled_regs.add(norm(m.game_registry_id))
    for key in disabled:
        if key.startswith("ugc:"):
            disabled_regs.add(f"mod/ugc_{key.split(':', 1)[1]}.mod")

    keep: list[str] = []
    known_regs = {norm(m.game_registry_id): m for m in library.mods.values()
                  if m.game_registry_id}

    # 按求解出的顺序输出
    for key in order:
        m = library.get(key)
        if m is None or not m.game_registry_id:
            continue
        if norm(m.game_registry_id) in disabled_regs:
            continue
        keep.append(m.game_registry_id)

    # 补丁模组
    for p in patches:
        if p.files:
            keep.append(patch_registry_id(p))

    if clean_stale:
        # 保留 existing 中「我们不知道但确实存在」的条目，剔除真正幽灵的
        for ref in existing:
            if norm(ref) in {norm(x) for x in keep}:
                continue
            if norm(ref) in known_regs:
                # 已知模组但被我们排除了 —— 尊重求解结果
                continue
            # 幽灵条目：不在库中且文件不存在
            fname = ref.replace("\\", "/").split("/")[-1]
            p = Path(paths.user_dir) / "mod" / fname
            if is_file(p):
                keep.append(ref)          # 文件在，保留
            else:
                result.add("dlc_load", f"清理幽灵条目 {ref}",
                           detail="mod 目录下已无对应描述符")

    # 去重保序
    seen: set[str] = set()
    final: list[str] = []
    for r in keep:
        k = norm(r)
        if k in seen:
            continue
        seen.add(k)
        final.append(r)

    data["enabled_mods"] = final
    try:
        # bom=False：dlc_load.json 是 JSON，游戏与启动器都按标准 JSON 读它。
        # 原文件本来就没有 BOM，我们不该给它加上——BOM 会让严格的
        # JSON 解析器报错，属于"我们改坏了别人的文件"。
        write_text(paths.dlc_load, json.dumps(data, ensure_ascii=False,
                                              separators=(",", ":")),
                   bom=False)
        result.add("dlc_load", f"更新 dlc_load.json（{len(final)} 个启用模组）",
                   detail=f"原 {len(existing)} 条 → 新 {len(final)} 条")
    except OSError as exc:
        result.add("dlc_load", "写入 dlc_load.json 失败", False, str(exc))


# ---------------------------------------------------------------------------
# 兼容补丁文件生成
# ---------------------------------------------------------------------------

def patch_mod_content(paths: Paths, p: PatchProposal) -> dict[str, str]:
    """生成补丁模组的完整文件集（含 descriptor.mod）。"""
    slug = f"{PATCH_MOD_PREFIX}{_slug_of(p.name)}"
    files: dict[str, str] = {}
    for rel, body in p.files.items():
        files[rel] = body
    files["descriptor.mod"] = (
        f'name="{p.name}"\n'
        f'version="1.0"\n'
        f'supported_version="v4.*"\n'
        f'tags={{\n\t"Fixes"\n}}\n'
    )
    files["README_smd.txt"] = (
        f"本模组由「熵光矩阵」自动生成。\n"
        f"生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        f"用途：{p.description}\n\n"
        f"涉及模组：\n" + "\n".join(f"  - {k}" for k in p.mod_keys) +
        f"\n\n如需撤销，请在启动器中禁用本模组，或直接删除目录：\n"
        f"  {patch_dir(paths, p)}\n"
    )
    _ = slug
    return files


def create_patch_mods(paths: Paths, patches: list[PatchProposal],
                      result: ApplyResult) -> list[str]:
    """在 ``mod/`` 下创建补丁模组目录与描述符，返回创建的名称列表。"""
    made: list[str] = []
    for p in patches:
        if not p.files:
            continue
        d = patch_dir(paths, p)
        try:
            d.mkdir(parents=True, exist_ok=True)
            files = patch_mod_content(paths, p)
            for rel, body in files.items():
                target = d / rel.replace("/", os.sep)
                target.parent.mkdir(parents=True, exist_ok=True)
                write_text(target, body)
            # 描述符也要放到 mod/ 目录下（游戏/启动器从这里读）
            modfile = Path(paths.user_dir) / "mod" / f"{d.name}.mod"
            body = (
                f'name="{p.name}"\n'
                f'version="1.0"\n'
                f'supported_version="v4.*"\n'
                f'path="{str(d).replace(chr(92), "/")}"\n'
                f'tags={{\n\t"Fixes"\n}}\n'
            )
            write_text(modfile, body)
            made.append(p.name)
            result.add("patch", f"生成兼容补丁：{p.name}",
                       detail=f"{len(p.files)} 个文件 → {d}")
        except OSError as exc:
            result.add("patch", f"生成补丁失败：{p.name}", False, str(exc))
    return made


def remove_patch_mods(paths: Paths, result: ApplyResult | None = None
                      ) -> list[str]:
    """删除所有由本软件生成的补丁模组。"""
    removed: list[str] = []
    mod_dir = Path(paths.user_dir) / "mod"
    if not is_dir(mod_dir):
        return removed
    try:
        entries = list(mod_dir.iterdir())
    except OSError:
        return removed
    for d in entries:
        if not d.name.startswith(PATCH_MOD_PREFIX):
            continue
        try:
            if d.is_dir():
                shutil.rmtree(long_path(d), ignore_errors=True)
            else:
                d.unlink()
            removed.append(d.name)
        except OSError:
            continue
    if result:
        for n in removed:
            result.add("patch", f"移除补丁模组 {n}")
    return removed


# ---------------------------------------------------------------------------
# 顶层：应用方案
# ---------------------------------------------------------------------------

def apply_solution(paths: Paths, library: ModLibrary, solution: Solution, *,
                   write_db: bool = True, write_json: bool = True,
                   create_patches: bool = True, clean_dlc_load: bool = True,
                   force: bool = False,
                   progress: Callable[[str, float], None] | None = None,
                   playset_id: str = ""
                   ) -> ApplyResult:
    """把方案落地到群星。

    **重要**：只会关闭 ``solution.approved_disable()`` 里的模组——
    也就是用户逐项确认过的那些。求解器给的其它建议一律不动，
    它们会保留在 playset 中继续启用。
    """
    res = ApplyResult()

    # 只关闭用户确认过的模组
    approved = solution.approved_disable()
    recommended = solution.recommended_disable()
    skipped = [k for k in recommended if k not in approved]
    if recommended:
        res.add(
            "decision",
            f"禁用决策：确认关闭 {len(approved)} 个，保留启用 {len(skipped)} 个",
            detail=("、".join(_name_of(library, k) for k in approved)
                    if approved else "用户未确认关闭任何模组"),
        )

    if not force:
        running = launcher_running()
        if running:
            res.add("guard", "拒绝写入：检测到相关进程正在运行", False,
                    "、".join(running) +
                    "。请先关闭 Paradox 启动器与游戏，否则改动会被覆盖。")
            return res

    if progress:
        progress("备份原文件", 0.05)
    try:
        res.backup_dir = make_backup(paths, "apply")
        res.add("backup", f"已备份原始文件", detail=res.backup_dir)
    except OSError as exc:
        res.add("backup", "备份失败", False, str(exc))

    if create_patches and solution.patches:
        if progress:
            progress("生成兼容补丁", 0.2)
        create_patch_mods(paths, solution.patches, res)

    # 新顺序中剔除用户确认关闭的模组；未确认的保留
    order = [k for k in solution.order if k not in set(approved)]
    for k in skipped:
        if k not in order:
            order.append(k)          # 用户选择保留 → 依然启用

    if write_db:
        if progress:
            progress("写回 playset", 0.5)
        write_playset(paths, order, approved, library,
                      solution.patches, res, progress,
                      playset_id=playset_id)

    if write_json:
        if progress:
            progress("更新 dlc_load.json", 0.8)
        write_dlc_load(paths, library, order, approved,
                       solution.patches, res, clean_stale=clean_dlc_load)

    if progress:
        progress("完成", 1.0)
    return res


def _name_of(library: ModLibrary, key: str) -> str:
    m = library.get(key)
    return m.name if m else key


def rollback(backup_dir: str, paths: Paths, result: ApplyResult | None = None
             ) -> ApplyResult:
    """从备份目录恢复。"""
    res = result or ApplyResult()
    src = Path(backup_dir)
    if not is_dir(src):
        res.add("rollback", "备份目录不存在", False, str(src))
        return res
    pairs = [
        (src / "launcher-v2.sqlite", paths.launcher_db),
        (src / "dlc_load.json", paths.dlc_load),
        (src / "settings.txt", paths.settings_txt),
    ]
    for s, d in pairs:
        if not is_file(s):
            continue
        try:
            if is_file(d):
                shutil.copy2(long_path(d), long_path(str(d) + ".before_rollback"))
            shutil.copy2(long_path(s), long_path(d))
            res.add("rollback", f"已恢复 {Path(d).name}")
        except OSError as exc:
            res.add("rollback", f"恢复 {Path(d).name} 失败", False, str(exc))
    return res
