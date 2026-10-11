"""冲突检测引擎。

检测七类问题，每一类都给出「证据 + 成因 + 建议处理」：

======  ==========================================================
编号     类型
======  ==========================================================
F1      同一文件被多个模组提供（文件级覆盖冲突）
S1      同名脚本对象被多个模组重定义且内容不同（语义级冲突）
S2      同名脚本对象被重定义但内容相同（安全的重复，可忽略）
L1      本地化键冲突（汉化模组互相打架/覆盖）
D1      前置依赖缺失（模组声明依赖但未启用/未安装）
V1      模组声明支持的游戏版本与当前版本不符
G1      幽灵模组 / 文件缺失（已启用但文件不存在）
======  ==========================================================

严重度 ``severity``: ``critical`` > ``high`` > ``medium`` > ``low`` > ``info``
"""

from __future__ import annotations

import itertools
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Iterator

from . import PATCH_PREFIX
from .modinfo import Mod, ModLibrary
from .paths import human_size, norm
from .scanner import Definition, IndexSet, ModIndex, category_for

SEV_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
SEV_LABEL = {
    "critical": "致命", "high": "严重", "medium": "中等",
    "low": "轻微", "info": "提示",
}
# 检测类型 -> 中文名（界面筛选与统计共用）
KIND_LABEL = {
    "F1": "文件覆盖冲突", "F1S": "被大量覆盖的模组",
    "S1": "脚本对象重定义冲突", "S2": "重复定义（无害）",
    "L1": "本地化/汉化键冲突", "D1": "依赖关系问题",
    "V1": "版本兼容问题", "G1": "文件完整性问题",
    "E1": "模组自身语法错误", "XX": "检查异常",
}

# 这些文件天然被大量模组改写，覆盖它们是「设计如此」而非冲突
BENIGN_OVERWRITE = {
    "descriptor.mod", "thumbnail.png", "readme.txt", "readme.md",
    "changelog.txt", "license.txt", "steam_workshop.txt",
}

# 一旦被覆盖就很可能导致另一个模组失效的关键文件模式
CRITICAL_FILE_PATTERNS = (
    re.compile(r"^common/scripted_variables/"),
    re.compile(r"^common/scripted_triggers/"),
    re.compile(r"^common/scripted_effects/"),
    re.compile(r"^common/on_action"),
    re.compile(r"^common/global_ship_designs/"),
    re.compile(r"^common/component_templates/"),
    re.compile(r"^common/section_templates/"),
    re.compile(r"^common/ship_sizes/"),
    re.compile(r"^common/technology/"),
    re.compile(r"^common/pop_jobs/"),
    re.compile(r"^common/buildings/"),
    re.compile(r"^common/governments/"),
    re.compile(r"^common/traits/"),
    re.compile(r"^common/megastructures/"),
    re.compile(r"^common/static_modifiers/"),
    re.compile(r"^events/"),
    re.compile(r"^common/defines/"),
    re.compile(r"^common/inline_scripts/"),
)

# 覆盖它一般是「故意打补丁」，不算事故
PATCH_FRIENDLY_SUFFIXES = (
    "/zz_", "zz_", "_patch.", "_override.", "_compat.",
)


@dataclass
class Finding:
    """一条检测结论。"""

    kind: str                  # F1 / S1 / L1 / D1 / V1 / G1 ...
    severity: str              # critical/high/medium/low/info
    title: str                 # 一句话结论
    detail: str = ""           # 成因解释
    advice: str = ""           # 建议处理
    mods: list[str] = field(default_factory=list)     # 相关模组 key
    mod_names: list[str] = field(default_factory=list)
    target: str = ""           # 冲突对象（文件名 / 定义名 / 本地化键）
    category: str = ""         # 游戏系统类别
    evidence: dict[str, Any] = field(default_factory=dict)
    # 自动修复动作（由 solver 消费）
    autofix: str = ""          # "" | "reorder" | "disable" | "keep_translation"
    fix_hint: dict[str, Any] = field(default_factory=dict)
    # 判定「谁赢」
    winner: str = ""           # 当前加载顺序下实际生效的模组 key
    loser: list[str] = field(default_factory=list)

    @property
    def severity_rank(self) -> int:
        """严重度排序权重（越小越严重）。"""
        return SEV_ORDER.get(self.severity, 9)

    def to_dict(self) -> dict:
        """序列化，并附带严重度中文名。"""
        d = dict(self.__dict__)
        d["severity_label"] = SEV_LABEL.get(self.severity, self.severity)
        return d


@dataclass
class ConflictReport:
    """一次冲突检测的完整结果（问题列表 + 统计 + 健康分）。"""
    findings: list[Finding] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)
    game_version: tuple[int, int, int] = (0, 0, 0)
    scan_seconds: float = 0.0

    def by_severity(self) -> dict[str, list[Finding]]:
        """按严重度分组。"""
        out: dict[str, list[Finding]] = defaultdict(list)
        for f in self.findings:
            out[f.severity].append(f)
        return dict(out)

    def count(self, severity: str | None = None) -> int:
        """统计问题数；``severity`` 为空时返回总数。"""
        if severity is None:
            return len(self.findings)
        return sum(1 for f in self.findings if f.severity == severity)

    def critical_and_high(self) -> list[Finding]:
        """只取致命与严重的问题。"""
        return [f for f in self.findings if f.severity in ("critical", "high")]

    def health_score(self) -> int:
        """0-100 的健康分。

        校准原则：分数要能**区分**「有点乱但能玩」和「根本进不去游戏」。
        实测一台装有 76 个模组（含巨构/NSC/ACOT 全家桶）的机器，
        冲突数百处但游戏可正常运行——把这种状态打成 0 分毫无信息量。

        模型：**归一化加权坏度**
          1. 每一类问题先算「坏度」``1 - decay**条数`` ∈ [0,1]，
             条数再多也收敛到 1，不会无限累加。
          2. 按类型重要度加权平均（权重和固定为 1），得到总坏度。
          3. ``健康分 = 100 × (1 - 总坏度)``。

        这样分数对「问题种类数」敏感、对「同类条数」不敏感，
        且天然落在 0-100 区间内。
        """
        # 条数 -> 坏度的衰减率（越小越快饱和）
        rate = {"critical": 0.75, "high": 0.93, "medium": 0.978,
                "low": 0.994, "info": 1.0}
        # 类型权重：S1（静默破坏功能）最重，越往下越轻
        #
        # 注意 F1（文件覆盖）权重不宜过高：模组覆盖同一个游戏文件是
        # 群星模组生态的**常态**，很多还是设计如此（补丁覆盖本体）。
        # V1（版本声明）更只是参考信息，权重压到最低。
        weight = {
            "S1": 1.00, "S1S": 0.75,
            "F1": 0.55, "F1S": 0.40,
            "G1": 0.70, "D1": 0.90, "L1": 0.45,
            "V1": 0.12, "E1": 0.70, "S2": 0.06,
            "XX": 0.60,
        }
        default_w = 0.40
        floor_w = 0.12          # 任何出现过的类型至少占这个权重

        per_kind: dict[str, list[Finding]] = defaultdict(list)
        for f in self.findings:
            per_kind[f.kind].append(f)

        total_w = 0.0
        bad = 0.0
        for kind, fs in per_kind.items():
            worst = min(fs, key=lambda f: SEV_ORDER.get(f.severity, 9)).severity
            r = rate.get(worst, 0.95)
            b = 1.0 - (r ** len(fs))          # ∈ [0,1)，饱和
            w = max(weight.get(kind, default_w), floor_w)
            total_w += w
            bad += w * b
        if total_w <= 0:
            return 100
        badness = bad / total_w
        return max(0, min(100, int(round(100.0 * (1.0 - badness)))))

    def kinds(self) -> list[dict]:
        """按类型聚合的统计（供界面做筛选芯片）。"""
        from collections import Counter
        c = Counter(f.kind for f in self.findings)
        return [{"kind": k, "count": n, "label": KIND_LABEL.get(k, k)}
                for k, n in sorted(c.items(), key=lambda kv: -kv[1])]

    def to_dict(self) -> dict:
        """序列化为界面与导出用的字典。"""
        return {
            "findings": [f.to_dict() for f in self.findings],
            "stats": self.stats,
            "kinds": self.kinds(),
            "game_version": list(self.game_version),
            "scan_seconds": self.scan_seconds,
            "health": self.health_score(),
            "counts": {
                sev: self.count(sev) for sev in
                ("critical", "high", "medium", "low", "info")
            },
        }


def game_version_from_meta(paths) -> tuple[int, int, int]:
    """从最近的崩溃 meta.yml / game.log 里读出游戏版本。"""
    import glob
    import os

    from .paths import is_file, read_text

    # 1) game.log: "Game Version: Pegasus v4.4.6"
    gl = paths.logs_dir / "game.log"
    if is_file(gl):
        try:
            m = re.search(r"Game Version:\s*\S*\s*v?(\d+)\.(\d+)\.(\d+)", read_text(gl))
            if m:
                return (int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except OSError:
            pass
    # 2) 最近的崩溃 meta.yml: "AppVersion: 4.4.6"
    try:
        cdirs = sorted(
            (d for d in os.scandir(str(paths.crashes_dir)) if d.is_dir()),
            key=lambda e: e.stat().st_mtime, reverse=True,
        )
    except OSError:
        cdirs = []
    for d in cdirs[:5]:
        mp = os.path.join(d.path, "meta.yml")
        if is_file(mp):
            try:
                m = re.search(r"^AppVersion:\s*(\d+)\.(\d+)\.(\d+)",
                              read_text(mp), re.M)
                if m:
                    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except OSError:
                continue
    # 3) 游戏目录里的 version 文件
    if paths.game_dir:
        for cand in ("launcher-settings.json", "version.txt"):
            p = os.path.join(paths.game_dir, cand)
            if is_file(p):
                try:
                    t = read_text(p)
                    m = re.search(r"(\d+)\.(\d+)\.(\d+)", t)
                    if m:
                        return (int(m.group(1)), int(m.group(2)), int(m.group(3)))
                except OSError:
                    continue
    return (0, 0, 0)


def _is_critical_file(rel: str) -> bool:
    return any(p.match(rel) for p in CRITICAL_FILE_PATTERNS)


def _is_benign(rel: str) -> bool:
    base = rel.rsplit("/", 1)[-1]
    if base in BENIGN_OVERWRITE:
        return True
    if rel.startswith(("gfx/", "sound/", "music/", "fonts/")):
        return True
    return False


def _looks_like_patch(rel: str, mod: Mod | None) -> bool:
    r = rel.lower()
    if any(r.endswith(s) or r.rsplit("/", 1)[-1].startswith(s)
           for s in PATCH_FRIENDLY_SUFFIXES):
        return True
    if mod is not None and mod.is_fix:
        return True
    return False


class ConflictDetector:
    """把 ``IndexSet`` + ``ModLibrary`` 变成一份 ``ConflictReport``。"""

    def __init__(self, library: ModLibrary, indexes: IndexSet, *,
                 game_version: tuple[int, int, int] = (0, 0, 0),
                 enabled_only: bool = True,
                 max_examples: int = 200,
                 scan_started: float = 0.0,
                 progress: Callable[[str, float], None] | None = None,
                 should_stop: Callable[[], bool] | None = None) -> None:
        self.lib = library
        self.idx = indexes
        self.game_version = game_version
        self.enabled_only = enabled_only
        self.max_examples = max_examples
        self.progress = progress
        self.should_stop = should_stop
        # 索引阶段的耗时（由调用方传入），让界面显示的「扫描耗时」
        # 反映端到端时间而不是仅检测阶段
        self.scan_started = scan_started
        self._mods: dict[str, Mod] = {m.key: m for m in library.mods.values()}

    # -- 工具 -------------------------------------------------------
    def _stopped(self) -> bool:
        return bool(self.should_stop and self.should_stop())

    def _name(self, key: str) -> str:
        m = self._mods.get(key)
        if m:
            return m.name or m.dir_name or key
        i = self.idx.indexes.get(key)
        return i.name if i else key

    def _mod(self, key: str) -> Mod | None:
        return self._mods.get(key)

    def _is_our_patch(self, key: str) -> bool:
        """这个模组是不是「熵光矩阵」自己生成的补丁模组。

        为什么要单独认出来：补丁模组**天生**就是"排在最后、整份覆盖"，
        它存在的意义就是盖住前面的模组。把它当成普通模组参与检测，
        会得到一堆没意义的结论——

        * "文件覆盖冲突"：它覆盖别人正是设计意图
        * "被大量覆盖的模组"：它自己会被统计成一个"受害者"
        * "重复定义"：它钉住的那些定义全都会被报一遍

        这些噪音会把真正需要用户处理的问题淹掉。
        所以补丁模组**只作为"解药"参与统计，不作为"病灶"参与检测**。
        """
        if not key:
            return False
        m = self._mod(key)
        if m is None:
            return False
        return PATCH_PREFIX.lower() in str(getattr(m, "path", "") or "").lower()

    def _order_index(self) -> dict[str, int]:
        """当前 playset 顺序：数字越小越先加载（越先被覆盖）。"""
        out: dict[str, int] = {}
        ordered = self.lib.load_order()
        for i, m in enumerate(ordered):
            out[m.key] = i
        # 未在 playset 里的排最后
        n = len(ordered)
        for key in self.idx.indexes:
            out.setdefault(key, n + 1)
        return out

    def _active_keys(self) -> set[str]:
        if not self.enabled_only:
            keys = set(self.idx.indexes)
        else:
            en = {m.key for m in self.lib.mods.values() if m.enabled_in_playset}
            # 索引里有但库里标记未启用的，仍可能被 dlc_load 启用
            keys = {k for k in self.idx.indexes
                    if k in en or k not in self._mods}
        # 把自己生成的补丁模组摘出去（见 _is_our_patch 的说明）
        return {k for k in keys if not self._is_our_patch(k)}

    def _has_patch_relation(self, a: str, b: str) -> bool:
        """判断两个模组是不是「主体 ↔ 它的汉化/补丁」这种配套关系。

        配套关系下的键覆盖是**设计如此**，不该按冲突处理。
        判据：名字短的那个是名字长的那个的子串（例如
        ``Ancient Cache of Technologies`` 与 ``ACOT_CN4.2`` 不算，
        但 ``Gigastructural Engineering & More`` 与
        ``Gigastructural Engineering & More [CN]`` 算）。
        """
        ma, mb = self._mod(a), self._mod(b)
        if not (ma and mb):
            return False
        na, nb = ma.name.lower().strip(), mb.name.lower().strip()
        if not na or not nb:
            return False
        core, ext = (na, nb) if len(na) <= len(nb) else (nb, na)
        # 去掉方括号/圆括号里的语言标记再比
        import re as _re
        core_clean = _re.sub(r"\s*[\(\[（【].*?[\)\]）】]\s*", " ", core).strip()
        return len(core_clean) >= 8 and core_clean in ext

    # -- 主流程 -----------------------------------------------------
    def run(self) -> ConflictReport:
        """执行全部检查项，返回 :class:`ConflictReport`。

        单个检查项抛异常不会中断整体流程，而是记录成一条 info 级问题——
        检测工具本身不应该因为一条边界情况就整个失败。
        """
        import time

        t0 = self.scan_started or time.time()
        findings: list[Finding] = []
        active = self._active_keys()
        order = self._order_index()

        steps = [
            ("检查文件级覆盖", self._check_file_overwrites),
            ("检查脚本对象重定义", self._check_semantic_redefinitions),
            ("检查本地化键冲突", self._check_localisation),
            ("检查前置依赖", self._check_dependencies),
            ("检查游戏版本兼容性", self._check_versions),
            ("检查幽灵模组与缺失文件", self._check_ghosts),
            ("检查模组自身语法错误", self._check_mod_errors),
        ]
        for i, (label, fn) in enumerate(steps):
            if self._stopped():
                break
            if self.progress:
                self.progress(label, (i + 1) / len(steps))
            try:
                findings.extend(fn(active, order))
            except Exception as exc:  # 单个检查项失败不应中断整体
                findings.append(Finding(
                    kind="XX", severity="info",
                    title=f"检查「{label}」时出错",
                    detail=repr(exc), advice="请把该信息反馈给开发者。",
                ))

        findings.sort(key=lambda f: (f.severity_rank, f.kind, f.title))
        rep = ConflictReport(
            findings=findings,
            game_version=self.game_version,
            scan_seconds=time.time() - t0,
        )
        rep.stats = self._build_stats(findings, active)
        return rep

    # -- F1 文件级覆盖 ---------------------------------------------
    def _check_file_overwrites(self, active: set[str], order: dict[str, int]
                               ) -> Iterator[Finding]:
        owners: dict[str, list[str]] = defaultdict(list)
        for key in active:
            idx = self.idx.indexes.get(key)
            if not idx:
                continue
            for rel in idx.files:
                owners[rel].append(key)

        # 统计每个模组被覆盖的次数，用于汇总
        covered_count: dict[str, int] = defaultdict(int)
        emitted = 0
        multi: list[tuple[str, list[str]]] = [
            (rel, ks) for rel, ks in owners.items() if len(ks) > 1
        ]
        # 关键文件优先
        multi.sort(key=lambda kv: (not _is_critical_file(kv[0]), kv[0]))

        for rel, keys in multi:
            if self._stopped():
                return
            if _is_benign(rel):
                continue
            keys_sorted = sorted(keys, key=lambda k: order.get(k, 9999))
            winner = keys_sorted[-1]
            losers = keys_sorted[:-1]
            for k in losers:
                covered_count[k] += 1

            # 内容是否真的不同？
            hashes = set()
            sizes = set()
            for k in keys:
                idx = self.idx.indexes.get(k)
                fi = idx.files.get(rel) if idx else None
                if fi is None:
                    continue
                if fi.content_hash:
                    hashes.add(fi.content_hash)
                sizes.add(fi.size)
            identical = len(hashes) == 1 and len(hashes) > 0
            size_diff = len(sizes) > 1

            crit = _is_critical_file(rel)
            if identical:
                continue  # 内容完全一致，覆盖无害

            # 只有贴图/音效大小不同 → 中低
            if not crit and not rel.startswith(("common/", "events/", "interface/")):
                sev = "medium"
            elif crit:
                sev = "high"
            else:
                sev = "medium"
            is_patchy = any(_looks_like_patch(rel, self._mod(k)) for k in keys)
            if is_patchy and sev == "high":
                sev = "medium"

            if emitted >= self.max_examples:
                continue
            emitted += 1

            cat = category_for(rel)
            names = [self._name(k) for k in keys_sorted]
            det = (
                f"{len(keys)} 个模组提供同一个文件：{rel}\n"
                f"加载顺序（先 → 后）：{' → '.join(names)}\n"
                f"实际生效：{self._name(winner)}（最后加载者整体覆盖之前的版本）"
            )
            if size_diff:
                det += "\n文件大小不同，说明内容确实存在差异。"
            yield Finding(
                kind="F1",
                severity=sev,
                title=f"文件覆盖：{rel}",
                detail=det,
                advice=(
                    f"「{self._name(winner)}」会吃掉其它 {len(losers)} 个模组对 "
                    f"该文件的改动。若被覆盖的模组功能异常，请调整加载顺序，"
                    f"或改用兼容补丁（patch）模组。"
                ),
                mods=keys_sorted, mod_names=names,
                target=rel, category=cat,
                evidence={"winner": winner, "losers": losers,
                          "identical": identical, "sizes": sorted(sizes)},
                autofix="reorder", fix_hint={"target": rel, "winner": winner},
                winner=winner, loser=losers,
            )

        # 汇总：被覆盖最惨的模组
        worst = sorted(covered_count.items(), key=lambda kv: -kv[1])[:6]
        for key, n in worst:
            if n < 5 or self._stopped():
                continue
            m = self._mod(key)
            yield Finding(
                kind="F1S",
                severity="high" if n > 40 else "medium",
                title=f"「{self._name(key)}」有 {n} 个文件被其它模组覆盖",
                detail=(f"该模组提供的 {n} 个文件会被排在其后的模组覆盖。"
                        f"覆盖数量如此之高，通常意味着它和其它大型模组"
                        f"（巨构/NSC/ACOT 类）存在结构性不兼容。"),
                advice="建议把它排到加载顺序靠后的位置，或寻找对应的兼容补丁。",
                mods=[key], mod_names=[self._name(key)],
                category="加载顺序", autofix="reorder",
                fix_hint={"mod": key, "reason": "被大量覆盖"},
            )

    # -- S1/S2 语义级重定义 ----------------------------------------
    def _check_semantic_redefinitions(self, active: set[str], order: dict[str, int]
                                      ) -> Iterator[Finding]:
        by_name: dict[str, list[Definition]] = defaultdict(list)
        for key in active:
            idx = self.idx.indexes.get(key)
            if not idx:
                continue
            for name, defs in idx.definitions.items():
                by_name[name].extend(defs)

        # 汇总：模组间重定义计数
        redef_count: dict[str, int] = defaultdict(int)
        emitted = 0
        items = [(n, ds) for n, ds in by_name.items() if len({d.owner for d in ds}) > 1]
        # 按「危害」排序：scripted_variables / triggers / effects 优先
        def rank(item: tuple[str, list[Definition]]) -> tuple:
            cat = item[1][0].category if item[1] else ""
            hot = 0 if any(k in cat for k in (
                "scripted_variable", "scripted_trigger", "scripted_effect",
                "on_action", "component_template", "section_template",
                "technology", "pop_job", "building",
            )) else 1
            return (hot, item[0])

        items.sort(key=rank)
        for name, defs in items:
            if self._stopped():
                return
            # 一个模组可能在多个文件里定义同一个名字；展示时只保留
            # 每个模组的第一处，避免 "2 个模组" 却列出 5 行定义。
            per_owner: dict[str, Definition] = {}
            for d in sorted(defs, key=lambda x: order.get(x.owner, 9999)):
                per_owner.setdefault(d.owner, d)
            owners = sorted(per_owner, key=lambda k: order.get(k, 9999))
            if len(owners) < 2:
                continue
            shown = [per_owner[k] for k in owners]
            bodies = {d.body_hash for d in defs}
            same = len(bodies) == 1
            cat = defs[0].category
            winner = owners[-1]
            losers = owners[:-1]

            if same:
                if emitted < self.max_examples:
                    emitted += 1
                    yield Finding(
                        kind="S2", severity="low",
                        title=f"重复定义（内容相同）：{name}",
                        detail=(f"{len(owners)} 个模组定义了完全相同的 {cat}「{name}」，"
                                f"内容一致，因此不会产生实际影响。"),
                        advice="无需处理；仅在关注加载顺序时参考。",
                        mods=owners, mod_names=[self._name(k) for k in owners],
                        target=name, category=cat,
                        evidence={"count": len(owners), "identical": True},
                        winner=winner, loser=losers,
                    )
                continue

            for k in losers:
                redef_count[k] += 1

            if emitted >= self.max_examples:
                continue
            emitted += 1

            # 严重度按「后果有多隐蔽」来定，而不是按类别名字：
            #
            # * scripted_trigger / scripted_effect / scripted_variable /
            #   on_action —— 被覆盖后**没有任何提示**，另一个模组的功能
            #   直接静默消失（科技不出现、事件不触发、修正量算错）。
            #   这是最恶劣的一类，定 critical。
            # * 其余内容型对象（部件/区段/科技/建筑…）被覆盖后
            #   通常会在 error.log 里留痕，或表现为内容缺失，
            #   可察觉也易恢复，定 high。
            if any(k in cat for k in (
                "scripted_variable", "scripted_trigger", "scripted_effect",
                "on_action", "脚本变量", "脚本触发器", "脚本效果",
            )):
                sev = "critical"
            elif any(k in cat for k in (
                "component_template", "section_template", "ship_size",
                "global_ship_design", "舰船",
            )):
                sev = "high"
            else:
                sev = "medium"
            where = "\n".join(
                f"  · {self._name(d.owner)} → {d.rel}（第 {d.line} 行）"
                for d in shown
            )
            yield Finding(
                kind="S1", severity=sev,
                title=f"重定义冲突：{cat}「{name}」",
                detail=(
                    f"{len(owners)} 个模组各自定义了同名的 {cat}，且内容不同。\n"
                    f"定义位置：\n{where}\n"
                    f"群星规则：只有最后加载的定义生效，"
                    f"「{self._name(winner)}」的版本会完全替换其它版本。"
                ),
                advice=(
                    f"这类冲突会让「{self._name(losers[0])}」的相关功能静默失效"
                    f"（科技不出现、事件不触发、修正量算错等）。\n"
                    f"处理方式：① 把要保留的模组排到更后面；"
                    f"② 若两者都想要，需要安装专门的兼容补丁；"
                    f"③ 二选一禁用其中一个。"
                ),
                mods=owners, mod_names=[self._name(k) for k in owners],
                target=name, category=cat,
                evidence={
                    "definitions": [
                        {"owner": d.owner, "name": self._name(d.owner),
                         "rel": d.rel, "line": d.line}
                        for d in shown
                    ],
                },
                autofix="reorder",
                fix_hint={"target": name, "winner": winner, "category": cat},
                winner=winner, loser=losers,
            )

        # 汇总：重定义最多的模组
        for key, n in sorted(redef_count.items(), key=lambda kv: -kv[1])[:5]:
            if n < 10 or self._stopped():
                continue
            yield Finding(
                kind="S1S",
                severity="high" if n > 60 else "medium",
                title=f"「{self._name(key)}」有 {n} 个定义被其它模组重定义",
                detail=(f"该模组声明的 {n} 个游戏对象（触发器/变量/部件等）"
                        f"会被其它模组的同名定义替换。"),
                advice=("说明它正处于激烈竞争的生态位（通常是大型机制模组）。"
                        "请把它排在加载顺序后段以保住它的逻辑。"),
                mods=[key], mod_names=[self._name(key)],
                category="加载顺序", autofix="reorder",
                fix_hint={"mod": key, "reason": "大量重定义"},
            )

    # -- L1 本地化键 -----------------------------------------------
    def _check_localisation(self, active: set[str], order: dict[str, int]
                            ) -> Iterator[Finding]:
        owners: dict[str, list[str]] = defaultdict(list)
        for key in active:
            idx = self.idx.indexes.get(key)
            if not idx:
                continue
            for lk in idx.loc_keys:
                owners[lk].append(key)

        collisions: dict[tuple[str, ...], list[str]] = defaultdict(list)
        # 只看「不同模组之间」的键碰撞
        per_mod_pair: dict[tuple[str, str], int] = defaultdict(int)
        for lk, ks in owners.items():
            uniq = sorted(set(ks))
            if len(uniq) < 2:
                continue
            for a, b in itertools.combinations(uniq, 2):
                per_mod_pair[(a, b)] += 1
            collisions[tuple(uniq)].append(lk)

        # 汉化模组之间的键冲突最值得报
        emitted = 0
        rows = sorted(per_mod_pair.items(), key=lambda kv: -kv[1])
        for (a, b), n in rows:
            if self._stopped():
                return
            if n < 3:
                continue
            ma, mb = self._mod(a), self._mod(b)
            both_tr = bool(ma and mb and ma.is_translation and mb.is_translation)
            if n < 20 and not both_tr:
                continue
            if emitted >= 60:
                break
            emitted += 1
            first, second = sorted((a, b), key=lambda k: order.get(k, 9999))
            # 严重度判定：只有在「两者都翻译同一批内容」时才可能真的打架。
            # 若一方是另一方的补丁/延伸（键集合大小悬殊），只是正常覆盖，
            # 不算严重问题——所以绝不自动禁用，只分级提示。
            na = len(self.idx.indexes[a].loc_keys) if a in self.idx.indexes else 0
            nb = len(self.idx.indexes[b].loc_keys) if b in self.idx.indexes else 0
            hi = max(na, nb)
            ratio = (n / hi) if hi else 1.0          # 碰撞占大者的比例
            related = self._has_patch_relation(a, b)
            if related:
                sev = "low"
            elif both_tr and ratio > 0.5 and n > 200:
                sev = "critical"
            elif both_tr and n > 50:
                sev = "high"
            elif both_tr:
                sev = "medium"
            else:
                sev = "low"
            sample = next((v for k, v in collisions.items()
                           if set(k) == {a, b}), [])
            yield Finding(
                kind="L1", severity=sev,
                title=f"本地化键冲突：{self._name(a)} ↔ {self._name(b)}（{n} 处）",
                detail=(
                    f"这两个模组都改写了 {n} 个相同的本地化键。\n"
                    f"后加载者「{self._name(second)}」的文本会覆盖"
                    f"「{self._name(first)}」。\n"
                    f"示例键：{', '.join(sample[:6])}"
                    + ("…" if len(sample) > 6 else "")
                    + ("\n\n注意：两者都是汉化模组，"
                       "同时启用会出现「一半中文一半另一种译文」的混乱。"
                       if both_tr else "")
                ),
                advice=(
                    "把希望生效的汉化模组排到后面。"
                    "若两个汉化覆盖的是不同模组，通常可以共存；"
                    "若覆盖同一批键，建议只保留一个。"
                ),
                mods=[first, second],
                mod_names=[self._name(first), self._name(second)],
                target=f"{self._name(a)} ↔ {self._name(b)}",
                category="本地化 / 汉化",
                evidence={"count": n, "sample_keys": sample[:20]},
                autofix="keep_translation", fix_hint={"keep": second},
                winner=second, loser=[first],
            )

    # -- D1 依赖 ---------------------------------------------------
    def _check_dependencies(self, active: set[str], order: dict[str, int]
                            ) -> Iterator[Finding]:
        by_reg = {norm(m.game_registry_id): m for m in self.lib.mods.values()
                  if m.game_registry_id}
        by_name = {norm(m.name): m for m in self.lib.mods.values() if m.name}
        by_id = {norm(m.remote_file_id): m for m in self.lib.mods.values()
                 if m.remote_file_id}
        enabled_keys = {m.key for m in self.lib.mods.values() if m.enabled_in_playset}
        installed_keys = set(self.lib.mods.keys())

        for m in self.lib.mods.values():
            if m.key not in active and self.enabled_only:
                continue
            for dep in m.dependencies:
                dn = norm(dep)
                found: Mod | None = None
                for table in (by_reg, by_name, by_id):
                    if dn in table:
                        found = table[dn]
                        break
                if found is None:
                    yield Finding(
                        kind="D1", severity="high",
                        title=f"缺少前置模组：{m.name} 需要「{dep}」",
                        detail=(f"「{m.name}」在其描述符中声明依赖 {dep}，"
                                f"但当前系统中找不到该模组。"),
                        advice="请在创意工坊订阅该前置模组，否则它加载时脚本会大量报错。",
                        mods=[m.key], mod_names=[m.name],
                        target=dep, category="依赖关系",
                        evidence={"dependency": dep},
                    )
                elif found.key not in enabled_keys:
                    yield Finding(
                        kind="D1", severity="critical",
                        title=f"前置模组未启用：{m.name} 需要「{found.name}」",
                        detail=(f"「{m.name}」依赖「{found.name}」，"
                                f"但后者未在当前 playset 中启用。"),
                        advice=f"请在 playset 中勾选「{found.name}」。",
                        mods=[m.key, found.key],
                        mod_names=[m.name, found.name],
                        target=dep, category="依赖关系",
                        evidence={"dependency": dep, "found": found.key},
                        autofix="disable",
                        fix_hint={"enable": found.key},
                    )
                else:
                    # 启用了，但顺序不对？
                    oi, of = order.get(m.key, 0), order.get(found.key, 0)
                    if oi < of:
                        yield Finding(
                            kind="D1", severity="medium",
                            title=f"依赖顺序颠倒：{m.name} 排在依赖项之前",
                            detail=(f"「{m.name}」（位置 {oi}）在加载顺序上早于"
                                    f"其依赖「{found.name}」（位置 {of}）。"),
                            advice="把前置模组排到依赖它的模组之前。",
                            mods=[m.key, found.key],
                            mod_names=[m.name, found.name],
                            target=dep, category="依赖关系",
                            autofix="reorder",
                            fix_hint={"before": found.key, "after": m.key},
                        )
        _ = installed_keys

    # -- V1 版本兼容 -----------------------------------------------
    #
    # 重要设计取舍：``supported_version`` 只是模组作者的**声明**，
    # 不是可靠性证据。实测中大量正常工作的模组声明 ``v4.0`` 却在
    # 4.4 上跑得很好（作者懒得改，或写了宽泛的小版本）。
    # 因此版本不符一律**不自动禁用**，只做提示；严重度也压到
    # low/medium，避免淹没真正的冲突。
    def _check_versions(self, active: set[str], order: dict[str, int]
                        ) -> Iterator[Finding]:
        gm, gn, gp = self.game_version
        if gm == 0:
            return
        for key in sorted(active):
            m = self._mod(key)
            if m is None:
                continue
            sm, sn, sp = m.supported_major
            if sm == 0:
                yield Finding(
                    kind="V1", severity="info",
                    title=f"未声明支持版本：{m.name}",
                    detail="该模组的描述符里没有 supported_version 字段，"
                           "启动器无法判断它是否适配当前游戏版本。",
                    advice="如果不报错就无需理会；频繁报错则考虑更新该模组。",
                    mods=[key], mod_names=[m.name],
                    target=m.supported_version or "(空)", category="版本兼容",
                )
                continue
            if sm == gm and sn == gn:
                continue
            if sm == gm and sn < gn:
                gap = gn - sn
                yield Finding(
                    kind="V1", severity="low" if gap <= 2 else "medium",
                    title=(f"模组声明的版本较旧：{m.name}"
                           f"（声明 v{sm}.{sn}，游戏为 v{gm}.{gn}.{gp}）"),
                    detail=(f"该模组声明支持的版本是 {m.supported_version}，"
                            f"当前游戏是 v{gm}.{gn}.{gp}。\n"
                            f"注意：这只是作者的声明，**不代表模组一定有问题**——"
                            f"很多模组写的是宽泛的小版本号，实际完全可用。"),
                    advice=("仅在游戏里出现异常（科技不出现、事件不触发、"
                            "界面错乱）时才需要考虑更新它。"
                            "若运行正常，请忽略本条。"),
                    mods=[key], mod_names=[m.name],
                    target=m.supported_version, category="版本兼容",
                    evidence={"mod": m.supported_version,
                              "game": f"v{gm}.{gn}.{gp}", "gap": gap},
                )
            elif sm == gm and sn > gn:
                yield Finding(
                    kind="V1", severity="medium",
                    title=(f"模组要求的版本更新：{m.name}"
                           f"（声明 v{sm}.{sn}，游戏为 v{gm}.{gn}.{gp}）"),
                    detail="该模组针对比当前游戏更新的版本编写。",
                    advice="建议更新游戏本体，否则模组的新功能可能报错。",
                    mods=[key], mod_names=[m.name],
                    target=m.supported_version, category="版本兼容",
                )
            else:
                yield Finding(
                    kind="V1", severity="high",
                    title=(f"模组跨大版本：{m.name}"
                           f"（声明 v{sm}.{sn}，游戏为 v{gm}.{gn}.{gp}）"),
                    detail=(f"该模组面向 v{sm}.{sn}，与当前 v{gm}.{gn}.{gp} "
                            f"不在同一个大版本。"),
                    advice=("跨大版本时群星的脚本接口常有破坏性改动，"
                            "该模组很可能引发 error.log 刷屏。"
                            "若游戏内确实异常，请禁用或寻找更新版。"),
                    mods=[key], mod_names=[m.name],
                    target=m.supported_version, category="版本兼容",
                    evidence={"mod": m.supported_version,
                              "game": f"v{gm}.{gn}.{gp}"},
                )

    # -- G1 幽灵模组 -----------------------------------------------
    def _check_ghosts(self, active: set[str], order: dict[str, int]
                      ) -> Iterator[Finding]:
        for m in self.lib.mods.values():
            if "工坊有文件但缺少" in " ".join(m.problems):
                yield Finding(
                    kind="G1", severity="medium" if not m.enabled_in_playset else "high",
                    title=f"工坊文件缺少描述符：{m.name}",
                    detail=(f"工坊目录 {m.path} 存在，但没有对应的 mod/ugc_*.mod "
                            f"描述符文件，因此游戏不会加载它。"),
                    advice="在 Paradox 启动器里取消再重新订阅，让它重建描述符；"
                           "或手动创建描述符。",
                    mods=[m.key], mod_names=[m.name],
                    target=m.path, category="文件完整性",
                )
            if m.enabled_in_playset and not m.resolve_content_path():
                yield Finding(
                    kind="G1", severity="critical",
                    title=f"已启用但文件丢失：{m.name}",
                    detail=(f"该模组在当前 playset 中处于启用状态，"
                            f"但它指向的目录不存在：{m.path or '(空)'}\n"
                            f"这会让游戏在启动时报 "
                            f"「Could not find files for mod」并可能导致加载失败。"),
                    advice="重新订阅该模组，或把它从 playset 中取消勾选。",
                    mods=[m.key], mod_names=[m.name],
                    target=m.path, category="文件完整性",
                    autofix="disable", fix_hint={"mod": m.key, "missing": True},
                )
            elif not m.resolve_content_path() and m.from_sqlite:
                # 未启用但记录过期：Steam 已取消订阅，数据库里还留着条目。
                # 不影响游戏，只提示可以清理。
                yield Finding(
                    kind="G1", severity="info",
                    title=f"过期的模组记录：{m.name}",
                    detail=(f"启动器数据库里还有这个模组，但内容目录已不存在"
                            f"（{m.path or '(空)'}）。\n"
                            f"通常是因为已经在 Steam 上取消订阅。"
                            f"因为它未启用，所以不会影响游戏运行。"),
                    advice="可以在 Paradox 启动器里删除这条记录，或忽略。",
                    mods=[m.key], mod_names=[m.name],
                    target=m.path, category="文件完整性",
                )

        # dlc_load.json 引用了不存在于 mod 目录的条目
        reg = {norm(m.game_registry_id) for m in self.lib.mods.values()
               if m.game_registry_id}
        for ref in self.lib.dlc_load.get("enabled_mods", []):
            if norm(ref) not in reg:
                yield Finding(
                    kind="G1", severity="high",
                    title=f"dlc_load.json 引用了不存在的模组：{ref}",
                    detail=("游戏的启用列表里还有这个条目，但 mod 目录下已找不到"
                            "对应描述符。这类「幽灵条目」会让启动器认为模组数量"
                            "与实际不符，偶发启动黑屏。"),
                    advice="运行「修复」会自动清理该条目。",
                    target=ref, category="文件完整性",
                    autofix="clean_dlc_load", fix_hint={"ref": ref},
                )

    # -- 模组自身语法错误 ------------------------------------------
    def _check_mod_errors(self, active: set[str], order: dict[str, int]
                          ) -> Iterator[Finding]:
        rows = []
        for key in active:
            idx = self.idx.indexes.get(key)
            if idx and idx.errors:
                rows.append((key, idx.errors))
        rows.sort(key=lambda kv: -len(kv[1]))
        for key, errs in rows[:25]:
            if self._stopped():
                return
            yield Finding(
                kind="E1",
                severity="high" if len(errs) > 20 else "medium",
                title=f"模组自身存在语法/读取问题：{self._name(key)}"
                      f"（{len(errs)} 处）",
                detail="\n".join(f"  · {e}" for e in errs[:15])
                       + ("\n  …" if len(errs) > 15 else ""),
                advice="这些是解析模组文件时发现的错误，通常也会出现在游戏 "
                       "error.log 中。请联系模组作者或寻找修复版。",
                mods=[key], mod_names=[self._name(key)],
                category="文件完整性",
                evidence={"errors": errs[:50]},
            )

    # -- 统计 ------------------------------------------------------
    def _build_stats(self, findings: list[Finding], active: set[str]) -> dict:
        kinds: dict[str, int] = defaultdict(int)
        for f in findings:
            kinds[f.kind] += 1
        files = sum(i.file_count for k, i in self.idx.indexes.items() if k in active)
        defs = sum(len(i.definitions) for k, i in self.idx.indexes.items()
                   if k in active)
        byts = sum(i.total_bytes for k, i in self.idx.indexes.items() if k in active)
        return {
            "kinds": dict(kinds),
            "active_mods": len(active),
            "indexed_files": files,
            "indexed_definitions": defs,
            "indexed_bytes": byts,
            "indexed_bytes_human": human_size(byts),
            "game_version": "v%d.%d.%d" % self.game_version
                            if self.game_version[0] else "未知",
        }
