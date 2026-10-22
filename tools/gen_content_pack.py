"""从用户装的全部模组生成 OGAS 的 content pack。

**为什么需要这个**

OGAS 的模组兼容是「声明式 content pack」，但官方只交付了 ``vanilla_4_4``
（17 个建筑 / 4 个区划）。实测用户这套配置有 **330 个建筑 + 171 个区划**，
原版包覆盖不到 5%。

**能做自动化的部分**（这里做的）

* 定义 ID（从 ``common/buildings/*.txt`` 等文件里抓顶层定义）
* 中文名（从模组的 ``localisation`` 里查，退回到 ID）
* 角色 role（从 ``produces`` / ``job`` 字段推断）
* 科技前置（从 ``potential`` 里的 ``has_technology`` 抓）
* 兼容分区前缀（从 ``allowed_zones`` 或按 role 推）
* 模组依赖清单（写进 ``required_mods``）

**做不了自动化的部分**

``carriers`` 里的 UI 坐标 —— 那是屏幕校准出来的。所以这里**不生成 carriers**，
只生成定义表；载体的校准留给 OGAS 的向导（5 个，几分钟）。

**增量**

带 ``--state`` 时把每个模组的文件指纹记下来；下次只有变化的模组会重扫。
新订阅的模组自动进增量，不需要改代码。
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import sys
from pathlib import Path

OUT = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

sys.path.insert(0, r"E:\yiuxi\StellarisModDoctor")
from tools.sandbox import enter_sandbox  # noqa: E402

enter_sandbox()
from smd.app import App  # noqa: E402

# 顶层定义：行首**不能有缩进**。
# 用 [ \t] 而不是 \s —— \s 会吃掉换行，导致 $ 永远匹配不上（踩过）。
TOP_DEF = re.compile(r"^([a-z][a-z0-9_]*)[ \t]*=[ \t]*\{[ \t]*$", re.M)
TECH = re.compile(r"has_technology[ \t]*=[ \t]*([a-z0-9_]+)")
PRODUCES = re.compile(r"produces[ \t]*=[ \t]*\{([^}]{0,400})\}", re.S)
JOB = re.compile(r"job[ \t]*=[ \t]*([a-z0-9_]+)")
# 建筑的 ``category``（manufacturing / research / military …）比 job/produces
# 可靠得多 —— 它是**原作者声明的用途**，而 job/produces 在模组里经常嵌在
# resources 块里（实测我的 produces 正则抓到的是 cost 那一层，导致 233 个
# 建筑被判成 other）。取第一个出现的位置：顶层 category 总在 resources 之前。
CATEGORY = re.compile(r"^[ \t]+category[ \t]*=[ \t]*([a-z0-9_]+)[ \t]*$", re.M)
ALLOW_MULTI = re.compile(r"allow_multiple[ \t]*=[ \t]*yes")
LOC_LINE = re.compile(r'^\s*([a-z0-9_.]+):\d*\s+"([^"]*)"', re.M)

# Stellaris 的建筑 category → role
CATEGORY_ROLE = {
    "manufacturing": "industry",
    "industrial": "industry",
    "research": "research",
    "military": "military",
    "army": "military",
    "trade": "trade",
    "amenities": "amenities",
    "housing": "housing",
    "government": "government",
    "unity": "unity",
    "pop_assembly": "pop_growth",
    "resource": "resource",
    "generator": "energy",
    "mining": "minerals",
    "farming": "food",
    "refinery": "strategic",
}

# 资源 → 角色。用于在没有 job/produces 时兜底推断
RESOURCE_ROLE = {
    "energy": "energy", "minerals": "minerals", "food": "food",
    "alloys": "alloys", "consumer_goods": "consumer_goods",
    "unity": "unity", "influence": "unity", "research": "research",
    "physics_research": "research", "society_research": "research",
    "engineering_research": "research", "volatile_motes": "strategic",
    "exotic_gases": "strategic", "rare_crystals": "strategic",
    "sr_living_metal": "strategic", "sr_zro": "strategic",
    "sr_dark_matter": "strategic", "amenities": "amenities",
    "housing": "housing", "trade": "trade",
}

KINDS = {
    "buildings": ("common/buildings", "building_buildings_v1"),
    "districts": ("common/districts", "district_buildings_v1"),
}

# 区划 ID 里的命名线索 → role。顺序重要：更具体的放前面。
DISTRICT_HINT = (
    ("_generator", "energy"), ("_energy", "energy"),
    ("_mining", "minerals"), ("_mineral", "minerals"),
    ("_farming", "food"), ("_agriculture", "food"), ("_food", "food"),
    ("_research", "research"), ("_science", "research"),
    ("_unity", "unity"), ("_bureaucratic", "unity"),
    ("_forge", "alloys"), ("_foundry", "alloys"), ("_industrial", "industry"),
    ("_factory", "consumer_goods"),
    ("_fortress", "military"), ("_military", "military"), ("_barracks", "military"),
    ("_trade", "trade"), ("_commercial", "trade"),
    ("_city", "housing"), ("_housing", "housing"), ("_urban", "housing"),
)


# 本地化目录搜索顺序：中文优先，退化到英文。
# **不能只看自己模组** —— 建筑定义在一个模组、中文名在汉化模组里，
# 各查各的会得到满屏空 label（实测 330 条建筑全部无名）。
LOC_DIRS = (
    "localisation/simp_chinese",
    "localisation/simp_chinese/replace",
    "localisation_synced/simp_chinese",
    "localisation/english",
)


def loc_index(root: Path, into: dict[str, str] | None = None) -> dict[str, str]:
    """把一个模组的中文名收进（或新建）一张索引。"""
    out = into if into is not None else {}
    for sub in LOC_DIRS:
        d = root / sub
        if not d.is_dir():
            continue
        for f in d.glob("*.yml"):
            try:
                txt = f.read_text(encoding="utf-8-sig", errors="replace")
            except OSError:
                continue
            for key, value in LOC_LINE.findall(txt):
                out.setdefault(key, value)
    return out


def parse_one(kind: str, text: str, loc: dict[str, str]) -> dict[str, dict]:
    """把一个文件里的顶层定义拆出来。"""
    found: dict[str, dict] = {}
    # 按顶层定义切块：每个定义从它的行开始，到下一个顶层定义为止
    marks = [(m.start(), m.group(1)) for m in TOP_DEF.finditer(text)]
    for i, (pos, ident) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else len(text)
        body = text[pos:end]
        entry: dict = {"label_zh": loc.get(ident, ""), "enabled": True}

        job = JOB.search(body)
        produces = PRODUCES.search(body)
        role = ""
        # 1) 先看 category —— 原作者声明的用途，最可信
        cat = CATEGORY.search(body)
        if cat:
            role = CATEGORY_ROLE.get(cat.group(1), "")
        # 2) 退到 job 名里的资源线索
        if not role and job:
            for token, r in RESOURCE_ROLE.items():
                if token in job.group(1):
                    role = r
                    break
            if not role:
                role = "jobs"
        # 3) 再退到 produces 里的资源线索（可能是嵌套的 cost 层，尽力而为）
        if not role and produces:
            for token, r in RESOURCE_ROLE.items():
                if token in produces.group(1):
                    role = r
                    break
        entry["role"] = role or ("housing" if kind == "districts" else "other")

        # 4) 区划常常什么标记都没有，只剩 ID 名带线索。
        # district_generator / district_mining / xxx_city 这类命名是社区惯例，
        # 比一律兜底成 housing 准得多（之前 171 个区划里 139 个被判成住宅）。
        if entry["role"] in ("other", "housing") and kind == "districts":
            for token, r in DISTRICT_HINT:
                if token in ident:
                    entry["role"] = r
                    break

        tech = TECH.search(body)
        entry["required_technology"] = tech.group(1) if tech else None

        if kind == "buildings":
            entry["compatible_zone_prefixes"] = []
            entry["adds_jobs"] = bool(job)
            entry["allow_multiple"] = bool(ALLOW_MULTI.search(body))
        else:
            entry["adds_jobs"] = bool(job)
            entry["planner_enabled"] = True
        entry["source"] = "mod"
        found[ident] = entry
    return found


def dir_fingerprint(d: Path) -> str:
    """目录内容指纹：文件名 + 大小 + 修改时间。用于增量。"""
    h = hashlib.sha1()
    if not d.is_dir():
        return ""
    for f in sorted(d.glob("*.txt")):
        try:
            st = f.stat()
        except OSError:
            continue
        h.update(f"{f.name}:{st.st_size}:{int(st.st_mtime)}".encode())
    return h.hexdigest()


def main() -> int:
    state_path = Path(__file__).with_name("_content_pack_state.json")
    state: dict[str, str] = {}
    if state_path.is_file():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except ValueError:
            state = {}

    app = App()
    app.load_mods()
    mods = [m for m in app.mods_list() if m.get("enabled")]
    print(f"  启用的模组：{len(mods)}", file=OUT)

    packs: dict[str, dict[str, dict]] = {k: {} for k in KINDS}
    used_mods: list[str] = []
    rescanned = 0
    reused = 0

    # 先把**所有**模组的本地化收进一张全局索引 —— 名字常常来自汉化模组，
    # 和定义所在的模组不是同一个。
    print("  建立全局本地化索引…", file=OUT)
    global_loc: dict[str, str] = {}
    for m in mods:
        r = Path(m.get("path") or "")
        if r.is_dir():
            loc_index(r, global_loc)
    print(f"    收录 {len(global_loc)} 条", file=OUT)

    for m in mods:
        root = Path(m.get("path") or "")
        if not root.is_dir():
            continue
        key = m["key"]
        # 增量只用来**统计**，不再用来跳过解析。
        #
        # 之前的写法是「指纹没变就 continue」，结果第二次运行全部命中缓存，
        # packs 里一条都没有，却照样写盘 —— 把上一次的结果覆盖成空的
        # （实测：第二次跑出 0 条建筑 0 条区划）。模组全量扫一遍也就几十秒，
        # 换来的是「每次输出都完整」，这个交易划算。
        for kind, (sub, _cap) in KINDS.items():
            d = root / sub
            if d.is_dir():
                fp = dir_fingerprint(d)
                if state.get(f"{key}|{kind}") == fp:
                    reused += 1
                else:
                    rescanned += 1
                    state[f"{key}|{kind}"] = fp
        loc = global_loc
        for kind, (sub, _cap) in KINDS.items():
            d = root / sub
            if not d.is_dir():
                continue
            for f in d.glob("*.txt"):
                try:
                    txt = f.read_text(encoding="utf-8-sig", errors="replace")
                except OSError:
                    continue
                packs[kind].update(parse_one(kind, txt, loc))
        used_mods.append(m.get("name") or key)

    print(f"  解析 {len(used_mods)} 个模组"
          f"（其中 {rescanned} 个有变化、{reused} 个与上次相同）", file=OUT)
    print("", file=OUT)
    for kind, table in packs.items():
        print(f"  {kind:12} {len(table):>5} 条", file=OUT)
        if table:
            sample = list(table.items())[:3]
            for ident, e in sample:
                print(f"      {ident:38} role={e.get('role', ''):14}"
                      f" label={e.get('label_zh', '')[:12]}", file=OUT)

    # ── 写盘：合并进 vanilla_4_4
    #
    # **不能另建目录。** ``src/iag/core/paths.py:vanilla_content_pack_root()``
    # 硬编码 ``content_packs/vanilla_4_4``，环境变量只认 ``IAG_PROJECT_ROOT``，
    # 改不了 pack 名 —— 另建 ``user_mods/`` 它根本不会读（实测确认）。
    #
    # 合并规则：**原版条目优先**。它们带 ``verified_transport``（真实执行验证
    # 过的），模组条目是按脚本推断出来的，没有那层证据，不能覆盖原版。
    out_dir = Path(r"E:\yiuxi\OGAS\content_packs\vanilla_4_4")
    caps_path = out_dir / "mappings" / "capabilities.json"
    backup = out_dir / "mappings" / "capabilities.vanilla.json"
    if caps_path.is_file() and not backup.is_file():
        backup.write_bytes(caps_path.read_bytes())
        print(f"\n  原版已备份 → {backup.name}", file=OUT)

    base_caps: dict = {}
    if backup.is_file():
        try:
            base_caps = json.loads(backup.read_text(encoding="utf-8"))
        except ValueError:
            base_caps = {}

    KINDS_ALL = ("carriers", "carrier", "districts", "buildings",
                 "building_replacement_policy", "building_upgrades", "zones")
    merged: dict = {}
    added = skipped = 0
    for kind in KINDS_ALL:
        orig = base_caps.get(kind) or {}
        if isinstance(orig, list):
            # building_upgrades 在原版里是**列表**（12 条升级链），不是字典。
            # 列表形式没法按键合并，原样保留 —— 模组生成的只有 buildings /
            # districts 两块，用不到这里。
            merged[kind] = orig
            continue
        both = dict(orig)
        for k, v in (packs.get(kind) or {}).items():
            if k in orig:
                skipped += 1        # 原版已有的，保留原版
                continue
            both[k] = v
            added += 1
        if both:
            merged[kind] = both
    merged["_meta"] = {
        "generated_by": "熵光矩阵 content pack generator",
        "mod_count": len(used_mods),
        "added_from_mods": added,
        "kept_vanilla": sum(len(base_caps.get(k) or {}) for k in KINDS_ALL),
        "note": ("原版条目原样保留（带 verified_transport）；模组条目由扫描"
                 "生成，role 按 job/produces 推断；carriers 的 UI 坐标仍需在"
                 "OGAS 向导里校准"),
    }
    caps_path.parent.mkdir(parents=True, exist_ok=True)
    caps_path.write_text(json.dumps(merged, ensure_ascii=False, indent=2),
                         encoding="utf-8")
    print(f"\n  合并：新增 {added} 条，保留原版（同名跳过 {skipped} 条）",
          file=OUT)
    print(f"  写入 {caps_path}", file=OUT)

    toml = [
        'schema_version = "iag.content_pack_manifest.v1"',
        'pack_id = "stellaris_vanilla_4_4"',
        'version = "0.2.0"',
        'display_name_zh = "Stellaris 4.4 原版 + 用户模组"',
        'description_zh = ("原版 4.4 定义 + 熵光矩阵扫描的当前模组定义。'
                          '模组部分由脚本生成，role 为推断值。")',
        "",
        'supported_game_versions = [">=4.4,<4.6"]',
        "required_mods = " + json.dumps(sorted(used_mods), ensure_ascii=False),
        "verified_mod_versions = []",
        "required_platform_capabilities = [",
        '    "economy_construction_v1",',
        '    "stellaris_definition_reader_v1",',
        "]",
        "",
        "[resources]",
        'capabilities = "mappings/capabilities.json"',
    ]
    (out_dir / "pack.toml").write_text("\n".join(toml) + "\n",
                                       encoding="utf-8")
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=0),
                          encoding="utf-8")
    print(f"\n  已写入 {out_dir}", file=OUT)
    OUT.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
