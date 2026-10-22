"""最优方案求解器。

目标：在当前模组集合下，算出一个「能让最多模组正常协同工作」的加载顺序与
启用集合，并生成可直接落地的修复动作。

群星加载顺序语义（关键）
------------------------
``loadOrder`` / playset 位置数字**越大越晚加载**。
后加载的模组会**整体覆盖**先加载模组的同名文件，
因此「谁排后面谁说了算」。

排序策略（分层 + 约束求解）
---------------------------
1. **硬约束**：依赖必须在前；被 A 覆盖了文件的模组之间保持原有相对顺序
   （除非检测到倒置会损害功能更关键的模组）。
2. **分层权重**：基础库 → 大型机制 → 内容 → UI → 兼容补丁 → 汉化。
3. **稳定排序**：同层内尽量保持用户原有顺序，改动最小化。

生成兼容补丁
------------
对于「两个模组定义同名对象、内容不同」的语义冲突，本模块可以把两边的定义
**合并**成一份覆盖补丁。群星对同名具名对象采取覆盖语义而非合并语义，因此
无条件合并可能改变游戏平衡；补丁只对**可安全合并**的类别（scripted_variables、
component_templates 的附加字段等）做合并，其余仅做「保留哪一份」的显式声明。
"""

from __future__ import annotations

import math
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Iterator

from .conflict import ConflictReport, Finding
from .modinfo import Mod, ModLibrary
from .paradox import Node, canonical_text, parse_string
from .paths import human_size, norm, sha1_text
from .scanner import Definition, IndexSet, category_for
from . import sortreq

# 分层权重：数字越小越先加载（越容易被覆盖）
TIER_BASE = 10          # 基础库/前置框架/兼容性底座
TIER_OVERHAUL = 20      # 大型机制重做
TIER_CONTENT = 30       # 常规内容
TIER_UI = 40            # 界面
TIER_PATCH = 50         # 兼容补丁 / Bug 修复
TIER_MINOR = 60         # 小型调整、数值
TIER_TRANSLATION = 70   # 汉化（最晚，压过一切文本）
TIER_OVERRIDE = 80      # 最终覆盖层

TIER_LABEL = {
    TIER_BASE: "前置框架 / 基础库",
    TIER_OVERHAUL: "大型机制重做",
    TIER_CONTENT: "常规内容",
    TIER_UI: "界面 UI",
    TIER_PATCH: "兼容补丁 / Bug 修复",
    TIER_MINOR: "小型调整",
    TIER_TRANSLATION: "本地化 / 汉化",
    TIER_OVERRIDE: "最终覆盖层",
}

# 明确的前置框架特征
_BASE_PATTERNS = (
    "universal resource patch", "urp", "core framework", "framework",
    "better performance", "performance & utilities", "shared library",
    "common library", "前置", "基础库", "兼容性框架",
)
_OVERRIDE_PATTERNS = ("override", "z_override", "覆盖层", "_final")
_PATCH_PATTERNS = ("patch", "compat", "compatibility", "fix", "hotfix",
                   "修补", "修复", "兼容")


@dataclass
class ModPlan:
    """单个模组在方案里的处置。

    ``action == "disable"`` 时，它只是**建议**，并且带一个三态决策：

    * ``decision == "pending"`` —— 用户还没表态（默认）
    * ``decision == "keep"``    —— 用户明确选择保留启用
    * ``decision == "close"``   —— 用户明确选择关闭

    ``approved`` 是 ``decision == "close"`` 的便捷别名，
    只有它为真时该模组才会在应用方案时被真正关闭。

    设计原则：**任何模组都不会被自动关闭。**
    求解器只负责说清「为什么建议关」，关不关由用户决定。
    """

    key: str
    name: str
    action: str = "keep"       # keep | enable | disable | reorder
    old_position: int = -1
    new_position: int = -1
    tier: int = TIER_CONTENT
    tier_label: str = ""
    reason: str = ""
    warnings: list[str] = field(default_factory=list)
    # 是否属于「确定性硬故障」（文件丢失 / 缺前置）。仅用于排序与提示，
    # 不代表可以自动关闭。
    hard_failure: bool = False
    # 用户决策：pending（默认，未表态）| keep | close
    decision: str = "pending"

    @property
    def needs_decision(self) -> bool:
        """是否还没表态。"""
        return self.action == "disable" and self.decision == "pending"

    @property
    def approved(self) -> bool:
        """是否已确认关闭。"""
        return self.action == "disable" and self.decision == "close"


@dataclass
class Solution:
    """完整的最优方案。

    ``disabled`` 保留向后兼容（等于建议禁用的全部），
    真正落地时用的永远是 :meth:`approved_disable`。
    """

    plans: list[ModPlan] = field(default_factory=list)
    order: list[str] = field(default_factory=list)        # 新加载顺序（mod key）
    disabled: list[str] = field(default_factory=list)     # = 建议禁用的全部
    to_enable: list[str] = field(default_factory=list)
    patches: list["PatchProposal"] = field(default_factory=list)
    clean_dlc_load: bool = False
    notes: list[str] = field(default_factory=list)
    score_before: int = 0
    score_after: int = 0
    conflicts_resolved: int = 0

    # ---- 用户决策 -------------------------------------------------
    def disable_plans(self) -> list[ModPlan]:
        """所有「建议禁用」的条目。"""
        return [p for p in self.plans if p.action == "disable"]

    def approved_disable(self) -> list[str]:
        """用户已确认关闭的模组 key —— 只有这些会被真正关掉。"""
        return [p.key for p in self.plans
                if p.action == "disable" and p.approved]

    def recommended_disable(self) -> list[str]:
        """求解器建议关闭的全部模组 key（仅建议）。"""
        return [p.key for p in self.plans if p.action == "disable"]

    def pending_decisions(self) -> list[ModPlan]:
        """还没表态的建议。"""
        return [p for p in self.plans
                if p.action == "disable" and p.decision == "pending"]

    def kept(self) -> list[ModPlan]:
        """用户明确选择保留的建议。"""
        return [p for p in self.plans
                if p.action == "disable" and p.decision == "keep"]

    def decide(self, key: str, approved: bool) -> bool:
        """记录用户对某个建议的决定。可反复修改。"""
        for p in self.plans:
            if p.action == "disable" and p.key == key:
                p.decision = "close" if approved else "keep"
                return True
        return False

    def decide_all(self, approved: bool) -> int:
        """一键全选/全不选。返回受影响的条目数。"""
        n = 0
        for p in self.plans:
            if p.action == "disable":
                p.decision = "close" if approved else "keep"
                n += 1
        return n

    def reset_decisions(self) -> int:
        """把所有决策退回「未表态」。"""
        n = 0
        for p in self.plans:
            if p.action == "disable":
                p.decision = "pending"
                n += 1
        return n

    def change_count(self) -> int:
        """需要改动的条目数（不含未确认的建议禁用）。"""
        return sum(1 for p in self.plans
                   if p.action != "keep" and not (
                       p.action == "disable" and not p.approved))



    def to_dict(self) -> dict:
        """序列化为界面用的字典。"""
        return {
            "plans": [p.__dict__ for p in self.plans],
            "order": self.order,
            "disabled": self.disabled,
            "to_enable": self.to_enable,
            "patches": [p.to_dict() for p in self.patches],
            "clean_dlc_load": self.clean_dlc_load,
            "notes": self.notes,
            "score_before": self.score_before,
            "score_after": self.score_after,
            "conflicts_resolved": self.conflicts_resolved,
            "change_count": self.change_count(),
        }


@dataclass
class PatchProposal:
    """一份建议生成的兼容补丁。"""

    name: str
    mod_keys: list[str]
    category: str
    target: str
    files: dict[str, str] = field(default_factory=dict)   # 相对路径 -> 内容
    description: str = ""
    mode: str = "declare"     # declare | merge
    confidence: str = "medium"

    def to_dict(self) -> dict:
        """序列化，并附带文件数与字节数。"""
        d = dict(self.__dict__)
        d["file_count"] = len(self.files)
        d["bytes"] = sum(len(v.encode("utf-8")) for v in self.files.values())
        return d


# ---------------------------------------------------------------------------
# 分层判定
# ---------------------------------------------------------------------------

def classify_tier(mod: Mod) -> tuple[int, str]:
    """给模组定层，返回 ``(tier, 判定理由)``。"""
    n = mod.name.lower()
    tags = {t.lower() for t in mod.tags}

    if mod.is_translation:
        return TIER_TRANSLATION, "识别为本地化/汉化模组"
    if any(p in n for p in _OVERRIDE_PATTERNS):
        return TIER_OVERRIDE, "名称含 override，属于最终覆盖层"
    if any(p in n for p in _BASE_PATTERNS) or "framework" in tags:
        return TIER_BASE, "识别为前置框架/基础库"
    if any(p in n for p in _PATCH_PATTERNS) or mod.is_fix:
        return TIER_PATCH, "识别为兼容补丁/Bug 修复"
    if mod.is_ui:
        return TIER_UI, "识别为界面 UI 模组"

    # 体量大的视为机制重做（用标签 + 名称启发）
    heavy_tags = {"megastructures", "overhaul", "total conversion", "gameplay"}
    if tags & {"megastructures", "overhaul", "total conversion"}:
        return TIER_OVERHAUL, "标签表明是大型机制模组"
    if mod.is_gigastructural:
        return TIER_OVERHAUL, "识别为大型机制模组（巨构/NSC/ACOT 系）"
    if tags & heavy_tags:
        return TIER_CONTENT, "常规内容模组"
    if len(mod.tags) <= 1 and mod.size_hint and mod.size_hint < 2 * 1024 * 1024:
        return TIER_MINOR, "体量很小的微调模组"
    return TIER_CONTENT, "常规内容模组"


# ---------------------------------------------------------------------------
# 求解器
# ---------------------------------------------------------------------------

class OrderSolver:
    """求解最优加载顺序。"""

    def __init__(self, library: ModLibrary, indexes: IndexSet,
                 report: ConflictReport, *,
                 keep_translation_on_top: bool = True,
                 aggressive: bool = True,
                 max_disable: int = 12,
                 descriptions: dict[str, str] | None = None) -> None:
        self.lib = library
        self.idx = indexes
        self.report = report
        self.keep_translation_on_top = keep_translation_on_top
        self.aggressive = aggressive
        self.max_disable = max_disable
        # {模组名: 工坊描述全文} —— 有就用作者的排序要求做硬约束
        self.descriptions = descriptions or {}
        self._mods = {m.key: m for m in library.mods.values()}

    # -- 主入口 -----------------------------------------------------
    def solve(self) -> Solution:
        """求解最优加载顺序与处置方案。

        流程：生成禁用建议 → 构建约束 → 稳定拓扑排序 → 生成补丁建议
        → 估算健康分。返回的 :class:`Solution` 里所有禁用建议都处于
        「未表态」状态，**不会自动关闭任何模组**。
        """
        sol = Solution()
        sol.score_before = self.report.health_score()

        enabled = [m for m in self.lib.mods.values() if m.enabled_in_playset]
        current_order = [m.key for m in self.lib.load_order()]
        # 补齐：已启用但位置无效的也纳入
        for m in enabled:
            if m.key not in current_order:
                current_order.append(m.key)

        tiers: dict[str, int] = {}
        reasons: dict[str, str] = {}
        for m in enabled:
            t, why = classify_tier(m)
            tiers[m.key] = t
            reasons[m.key] = why

        # ---- 1. 生成禁用建议（等待用户决策，不自动关闭）-----------
        to_disable = self._decide_disable(enabled, tiers)
        hard = set(getattr(self, "_hard", ()))

        # ---- 2. 构建约束 -----------------------------------------
        #    注意：排序只按「实际会启用」的模组算。建议禁用的模组
        #    仍参与排序，这样用户临时决定保留它们时顺序也是合理的。
        active = list(current_order)
        constraints, cnotes = self._build_constraints(active, tiers)
        sol.notes.extend(cnotes)

        # ---- 3. 排序 ---------------------------------------------
        old_pos = {k: i for i, k in enumerate(current_order)}
        new_order = self._stable_toposort(active, tiers, constraints, old_pos)

        # ---- 4. 生成方案条目 -------------------------------------
        for i, key in enumerate(new_order):
            m = self._mods.get(key)
            if m is None:
                continue
            op = old_pos.get(key, -1)
            if key in to_disable:
                # 建议禁用：不参与顺序，等用户决定
                sol.plans.append(ModPlan(
                    key=key, name=m.name, action="disable",
                    old_position=op, new_position=i,
                    tier=tiers.get(key, TIER_CONTENT),
                    tier_label=TIER_LABEL.get(tiers.get(key, TIER_CONTENT), ""),
                    reason=to_disable[key],
                    hard_failure=key in hard,
                    decision="pending",
                ))
                continue
            if op < 0:
                action = "enable"
                reason = "模组已被启用，纳入新顺序"
            elif op != i:
                action = "reorder"
                reason = self._reorder_reason(key, tiers, constraints)
            else:
                action = "keep"
                reason = reasons.get(key, "")
            sol.plans.append(ModPlan(
                key=key, name=m.name, action=action,
                old_position=op, new_position=i,
                tier=tiers.get(key, TIER_CONTENT),
                tier_label=TIER_LABEL.get(tiers.get(key, TIER_CONTENT), ""),
                reason=reason,
            ))

        # 启用的前置依赖（只补「已安装但没启用」的，不涉及关闭任何模组）
        sol.to_enable = self._missing_prereqs(to_disable)
        for key in sol.to_enable:
            m = self._mods.get(key)
            if m is None:
                continue
            t, why = classify_tier(m)
            sol.plans.append(ModPlan(
                key=key, name=m.name, action="enable", new_position=-1,
                tier=t, tier_label=TIER_LABEL.get(t, ""),
                reason="被其它已启用模组声明为前置依赖，自动启用",
            ))
            if key not in new_order:
                new_order.insert(0, key)
            tiers.setdefault(key, t)

        # 建议禁用的模组不进入新顺序
        disabled_set = set(to_disable)
        sol.order = [k for k in new_order if k not in disabled_set]
        sol.disabled = list(to_disable.keys())
        # 默认全部保留原状：未经用户确认，一个都不关（decision 默认 pending）

        # ---- 5. 兼容补丁 -----------------------------------------
        sol.patches = self._propose_patches(
            [k for k in active if k not in disabled_set], tiers)
        sol.clean_dlc_load = any(
            f.kind == "G1" and f.autofix == "clean_dlc_load"
            for f in self.report.findings
        )

        # ---- 6. 估算 ---------------------------------------------
        sol.conflicts_resolved = self._estimate_resolved(disabled_set,
                                                         sol.patches)
        sol.score_after = self._estimate_score(sol)
        if sol.pending_decisions():
            sol.notes.append(
                f"有 {len(sol.pending_decisions())} 个模组存在硬故障"
                f"（文件丢失或缺少前置），已列出建议等待你确认。"
                f"在你确认之前，它们不会被关闭。"
            )
        return sol

    # -- 禁用建议 ---------------------------------------------------
    def _decide_disable(self, enabled: list[Mod], tiers: dict[str, int]
                        ) -> dict[str, str]:
        """**建议**需要用户考虑的模组（绝不自动关闭）。

        本方法只产出「建议 + 理由」，返回的字典会被写进
        :class:`ModPlan` 且 ``needs_decision=True``、``approved=False``。
        真正落到 playset 的禁用列表来自 ``Solution.approved_disable()``，
        即用户明确勾选过的那些。

        会被建议的两类情况（都是**确定性硬故障**，不是猜测）：

        * 已启用但文件目录不存在 → 游戏启动会报
          ``Could not find files for mod``
        * 缺少描述符里声明的前置依赖 → 加载后必然大量脚本报错

        注意用词：这里叫「建议」，因为这些是**用户的模组**。
        即使证据确凿，也要让用户看清理由后自己决定。
        """
        out: dict[str, str] = {}
        self._hard: set[str] = set()

        # 1) 已启用但文件丢失
        for m in enabled:
            if not m.resolve_content_path():
                out[m.key] = ("已启用但文件目录不存在"
                              "（会让游戏启动时报 Could not find files for mod）")
                self._hard.add(m.key)
        # 2) 缺少必需前置
        for f in self.report.findings:
            if f.kind == "D1" and f.severity in ("critical", "high") and f.mods:
                k = f.mods[0]
                if k not in out:
                    out[k] = f"缺少前置依赖「{f.target}」，加载后会大量报错"
                    self._hard.add(k)

        # 列表长度不设上限：全部交由用户判断，不做任何自动取舍
        return out

    def _has_patch_relation(self, a: str, b: str) -> bool:
        ma, mb = self._mods.get(a), self._mods.get(b)
        if not (ma and mb):
            return False
        na, nb = ma.name.lower(), mb.name.lower()
        # 例如 "X Chinese Translation" 与 "X" —— 是配套关系而非冲突
        core = min(na, nb, key=len)
        return len(core) > 6 and core in max(na, nb)

    def _missing_prereqs(self, maybe_disabled: dict[str, str]) -> list[str]:
        """找出「已启用模组的必需前置」中尚未启用但**已安装**的。

        这类前置直接启用是安全的：它对应用户已经订阅了它，
        而且依赖它的模组本来就要用。不涉及关闭任何东西。

        对于「等待用户决定是否关闭」的模组，不替它补前置——
        否则用户选择关掉它时，我们反而多启用了一个模组。
        """
        need: list[str] = []
        by_reg = {norm(m.game_registry_id): m for m in self.lib.mods.values()}
        by_name = {norm(m.name): m for m in self.lib.mods.values()}
        by_id = {norm(m.remote_file_id): m for m in self.lib.mods.values()}
        enabled_keys = {m.key for m in self.lib.mods.values()
                        if m.enabled_in_playset}
        for m in self.lib.mods.values():
            if not m.enabled_in_playset or m.key in maybe_disabled:
                continue
            for dep in m.dependencies:
                d = norm(dep)
                found = by_reg.get(d) or by_name.get(d) or by_id.get(d)
                if found and found.key not in enabled_keys \
                        and found.key not in need:
                    if found.resolve_content_path():
                        need.append(found.key)
        return need

    # -- 约束构建 ---------------------------------------------------
    def _build_constraints(self, active: list[str], tiers: dict[str, int]
                           ) -> tuple[list[tuple[str, str, str]], list[str]]:
        """构建 ``(before, after, 理由)`` 硬约束列表。"""
        cons: list[tuple[str, str, str]] = []
        notes: list[str] = []
        aset = set(active)

        # 1) 依赖关系
        by_reg = {norm(m.game_registry_id): m.key for m in self.lib.mods.values()
                  if m.game_registry_id}
        by_name = {norm(m.name): m.key for m in self.lib.mods.values() if m.name}
        by_id = {norm(m.remote_file_id): m.key for m in self.lib.mods.values()
                 if m.remote_file_id}
        for key in active:
            m = self._mods.get(key)
            if m is None:
                continue
            for dep in m.dependencies:
                d = norm(dep)
                dk = by_reg.get(d) or by_name.get(d) or by_id.get(d)
                if dk and dk in aset and dk != key:
                    cons.append((dk, key, "前置依赖必须先加载"))

        # 2) 同文件覆盖：不要让「提供该文件的主力模组」被无关小模组覆盖
        #    这里不是硬性禁止，而是把「后加载才合理」的关系固定下来。
        #    做法：找出每个文件的所有提供者，把 tier 大的放后面。
        owners: dict[str, list[str]] = defaultdict(list)
        for k in active:
            i = self.idx.indexes.get(k)
            if not i:
                continue
            for rel in i.files:
                owners[rel].append(k)
        inconsistent = 0
        for rel, ks in owners.items():
            if len(ks) < 2:
                continue
            if rel.startswith(("gfx/", "sound/", "music/", "fonts/")):
                continue
            ks2 = sorted(set(ks))
            for a, b in zip(ks2, ks2[1:]):
                ta, tb = tiers.get(a, TIER_CONTENT), tiers.get(b, TIER_CONTENT)
                if ta < tb:
                    cons.append((a, b, f"分层：{TIER_LABEL.get(ta,'')} 先于 "
                                       f"{TIER_LABEL.get(tb,'')}"))
                elif ta > tb:
                    cons.append((b, a, f"分层：{TIER_LABEL.get(tb,'')} 先于 "
                                       f"{TIER_LABEL.get(ta,'')}"))
        if inconsistent:
            notes.append(f"发现 {inconsistent} 处分层与依赖冲突，已按依赖优先处理。")

        # 3) 汉化总是最后
        if self.keep_translation_on_top:
            tr = [k for k in active if tiers.get(k) == TIER_TRANSLATION]
            nontr = [k for k in active if tiers.get(k) != TIER_TRANSLATION]
            if tr and len(tr) <= 40:
                for t in tr:
                    for n in nontr[:80]:
                        cons.append((n, t, "汉化模组应最后加载以覆盖文本"))

        # 4) **作者自己写下的排序要求**
        #
        #    这是最硬的依据 —— 作者比他自己的模组更清楚该怎么加载。
        #    但它们的描述只写在工坊页面上（descriptor.mod 里没有），
        #    所以文本由调用方通过 descriptions 传进来（抓取或缓存的）。
        #
        #    真实案例：``! Core Framework of Kasako`` 名字里带 framework，
        #    旧版按名字把它排到第 0 位；而作者的工坊描述写着
        #    「必须在【UI Overhaul Dynamic】后面」—— 用户实报「放最上面没绷住」。
        if self.descriptions:
            names = [self._mods[k].name for k in active if k in self._mods]
            for key in active:
                m = self._mods.get(key)
                if m is None:
                    continue
                text = self.descriptions.get(m.name) or ""
                if not text:
                    continue
                try:
                    parsed = sortreq.parse(text, own_name=m.name)
                except Exception:
                    continue
                for c in parsed.constraints:
                    hits = sortreq.match_targets(c, names)
                    for target_name in hits:
                        tkey = next((k for k in active
                                     if k in self._mods
                                     and self._mods[k].name == target_name), "")
                        if not tkey or tkey == key or tkey not in aset:
                            continue
                        why = f"作者要求：{c.evidence[:60]}"
                        # after = 我在它后面 → 它必须先加载
                        if c.kind == "after":
                            cons.append((tkey, key, why))
                        else:
                            cons.append((key, tkey, why))

        # 去重
        seen = set()
        uniq: list[tuple[str, str, str]] = []
        for c in cons:
            if c[0] == c[1]:
                continue
            k = (c[0], c[1])
            if k in seen:
                continue
            seen.add(k)
            uniq.append(c)
        return uniq, notes

    # -- 拓扑排序（稳定）-------------------------------------------
    def _stable_toposort(self, nodes: list[str], tiers: dict[str, int],
                         constraints: list[tuple[str, str, str]],
                         old_pos: dict[str, int]) -> list[str]:
        """Kahn 算法 + 分层优先队列；同层内保持原顺序（改动最小化）。"""
        node_set = set(nodes)
        adj: dict[str, list[str]] = defaultdict(list)
        indeg: dict[str, int] = {n: 0 for n in nodes}
        for before, after, _why in constraints:
            if before not in node_set or after not in node_set:
                continue
            adj[before].append(after)
            indeg[after] = indeg.get(after, 0) + 1

        import heapq

        def key(n: str) -> tuple[int, int, str]:
            return (tiers.get(n, TIER_CONTENT), old_pos.get(n, 10 ** 6), n)

        heap = [key(n) for n in nodes if indeg.get(n, 0) == 0]
        heapq.heapify(heap)
        out: list[str] = []
        # 从堆元素还原 key 需要映射
        keymap: dict[tuple, str] = {}
        for n in nodes:
            keymap.setdefault(key(n), n)

        remaining = {n: indeg.get(n, 0) for n in nodes}
        while heap:
            k = heapq.heappop(heap)
            n = keymap.pop(k, None)
            if n is None or n in out:
                continue
            out.append(n)
            for nxt in adj.get(n, ()):
                remaining[nxt] -= 1
                if remaining[nxt] == 0 and nxt not in out:
                    nk = key(nxt)
                    if nk not in keymap:
                        keymap[nk] = nxt
                        heapq.heappush(heap, nk)

        # 处理环：剩余节点按分层 + 原顺序追加
        if len(out) < len(nodes):
            left = [n for n in nodes if n not in set(out)]
            left.sort(key=key)
            out.extend(left)

        # 去重保序
        seen = set()
        final = []
        for n in out:
            if n not in seen:
                seen.add(n)
                final.append(n)
        return final

    def _reorder_reason(self, key: str, tiers: dict[str, int],
                        constraints: list[tuple[str, str, str]]) -> str:
        rel = [c for c in constraints if c[0] == key or c[1] == key]
        if rel:
            _b, _a, why = rel[0]
            return why
        t = tiers.get(key, TIER_CONTENT)
        return f"按分层归位：{TIER_LABEL.get(t, '')}"

    # -- 兼容补丁 ---------------------------------------------------
    def _propose_patches(self, active: list[str], tiers: dict[str, int]
                         ) -> list[PatchProposal]:
        """针对语义冲突生成可合并的覆盖补丁。"""
        out: list[PatchProposal] = []
        if not self.aggressive:
            return out

        # 收集 F1（同文件不同内容）里「一方明显是补丁，另一方是主体」的组合
        seen_targets: set[str] = set()
        for f in self.report.findings:
            if len(out) >= 12:
                break
            if f.kind == "S1" and f.severity in ("critical", "high"):
                merged = self._try_merge_definitions(f)
                if merged is not None:
                    out.append(merged)
                    seen_targets.add(f.target)
            elif f.kind == "F1" and f.severity == "high":
                prop = self._try_patch_file_overwrite(f, tiers)
                if prop is not None:
                    out.append(prop)
                    seen_targets.add(f.target)
        return out

    def _try_merge_definitions(self, f: Finding) -> PatchProposal | None:
        """尝试把同名定义合并。仅对可安全合并的类别生效。"""
        cat = f.category or ""
        if "scripted_variable" not in cat and "static_modifier" not in cat:
            return None
        defs = f.evidence.get("definitions") or []
        if len(defs) < 2:
            return None
        # 读回每一份定义的原始文本
        bodies: list[tuple[str, str, str]] = []   # (owner, rel, text)
        for d in defs[:4]:
            idx = self.idx.indexes.get(d["owner"])
            if not idx:
                continue
            rel = d["rel"]
            path = None
            import os
            from .paths import long_path
            full = os.path.join(long_path(idx.root), rel.replace("/", os.sep))
            if os.path.isfile(full):
                path = full
            if not path:
                continue
            try:
                with open(path, "r", encoding="utf-8-sig", errors="replace") as fh:
                    bodies.append((d["owner"], rel, fh.read()))
            except OSError:
                continue
        if len(bodies) < 2:
            return None

        # 逐份解析，取出目标定义的子键并合并
        merged_fields: dict[str, list[str]] = {}
        conflicts: list[str] = []
        for owner, rel, text in bodies:
            root = parse_string(text)
            node = None
            for k, lst in root.items():
                if k == f.target and lst:
                    node = lst[0]
                    break
            if node is None or not node.is_block:
                continue
            for k, child in node.items():
                txt = _render(child)
                lst = merged_fields.setdefault(k, [])
                if txt not in lst:
                    lst.append(txt)
        if not merged_fields:
            return None

        # 检测真正互斥的字段（同名不同值 → 只能二选一）
        for k, vals in merged_fields.items():
            if len(vals) > 1:
                conflicts.append(k)
        if conflicts:
            # 有互斥字段时不做自动合并，降级为「声明式」补丁
            return None

        lines = [f"{f.target} = {{"]
        for k, vals in merged_fields.items():
            for v in vals:
                lines.append(f"\t{k} = {v}")
        lines.append("}")
        body = "\n".join(lines) + "\n"

        names = [self._safe_name(d["owner"]) for d in defs[:4]]
        fname = f"zz_smd_merge_{_slug(f.target)}.txt"
        owners = [d["owner"] for d in defs[:4]]
        return PatchProposal(
            name=f"合并补丁：{f.target}",
            mod_keys=owners,
            category=f.category,
            target=f.target,
            files={_patch_rel_path(f.category, fname): body},
            description=(
                f"把 {len(bodies)} 个模组对 `{f.target}` 的定义合并为一份。\n"
                f"参与合并：{'、'.join(names)}\n"
                f"合并字段：{', '.join(merged_fields.keys())}\n"
                f"（这些字段没有互相矛盾，可以安全叠加。）"
            ),
            mode="merge",
            confidence="high",
        )

    def _try_patch_file_overwrite(self, f: Finding, tiers: dict[str, int]
                                  ) -> PatchProposal | None:
        """文件级冲突：若一方是补丁类模组，生成说明与占位覆盖文件。"""
        rel = f.target
        if not rel.startswith(("common/", "events/")):
            return None
        if not rel.endswith((".txt", ".gui", ".gfx")):
            return None
        winner = f.evidence.get("winner", "")
        losers = f.evidence.get("losers") or []
        if not winner or not losers:
            return None
        # 只处理「补丁类模组被非补丁模组覆盖」这种明确情况
        wm = self._mods.get(winner)
        patchy = [k for k in losers if (self._mods.get(k) and self._mods[k].is_fix)]
        if not (wm and wm.is_fix) and not patchy:
            return None
        if wm and wm.is_fix:
            return PatchProposal(
                name=f"顺序修正建议：{rel}",
                mod_keys=[winner] + list(losers),
                category=category_for(rel),
                target=rel,
                files={},
                description=(
                    f"该文件被 {len(losers)+1} 个模组覆盖，其中"
                    f"「{wm.name}」是修复/补丁类模组，应当最后加载才能生效。\n"
                    f"本方案已通过调整加载顺序解决，无需额外补丁文件。"
                ),
                mode="declare",
                confidence="medium",
            )
        return None

    def _safe_name(self, key: str) -> str:
        m = self._mods.get(key)
        return m.name if m else key

    # -- 评分估算 ---------------------------------------------------
    def _estimate_resolved(self, to_disable: dict[str, str],
                           patches: list[PatchProposal]) -> int:
        n = 0
        for f in self.report.findings:
            if f.severity not in ("critical", "high"):
                continue
            if any(k in to_disable for k in f.mods):
                n += 1
            elif any(p.target == f.target for p in patches):
                n += 1
        return n

    def _estimate_score(self, sol: Solution) -> int:
        base = sol.score_before
        # 排序 + 禁用 + 补丁带来的收益
        gain = 0
        gain += min(20, len(sol.patches) * 3)
        gain += min(35, len(sol.disabled) * 3)
        # 顺序理顺本身也有收益
        reordered = sum(1 for p in sol.plans if p.action == "reorder")
        gain += min(15, reordered // 3)
        return min(100, base + gain)


# ---------------------------------------------------------------------------
# 渲染辅助
# ---------------------------------------------------------------------------

def _render(node: Node) -> str:
    """把节点渲染回脚本片段。"""
    if not node.is_block:
        v = node.value
        if isinstance(v, bool):
            return "yes" if v else "no"
        if isinstance(v, str) and (" " in v or v == ""):
            return f'"{v}"'
        return str(v)
    parts = ["{"]
    for k, child in node.items():
        parts.append(f"{k} = {_render(child)}")
    parts.append("}")
    return " ".join(parts)


_SLUG_RE = re.compile(r"[^a-zA-Z0-9_]+")


def _slug(s: str) -> str:
    return _SLUG_RE.sub("_", s)[:48].strip("_") or "patch"


# 语义类别 -> 补丁文件应放置的相对目录
_CATEGORY_DIR = {
    "scripted_trigger": "common/scripted_triggers",
    "scripted_effect": "common/scripted_effects",
    "scripted_variable": "common/scripted_variables",
    "static_modifier": "common/static_modifiers",
    "technology": "common/technology",
    "building": "common/buildings",
    "pop_job": "common/pop_jobs",
    "trait": "common/traits",
    "component_template": "common/component_templates",
    "section_template": "common/section_templates",
    "megastructure": "common/megastructures",
    "edict": "common/edicts",
    "policy": "common/policies",
    "event": "events",
}


def _patch_rel_path(category: str, filename: str) -> str:
    for k, d in _CATEGORY_DIR.items():
        if k in (category or ""):
            return f"{d}/{filename}"
    return f"common/scripted_variables/{filename}"
