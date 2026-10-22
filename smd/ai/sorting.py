"""AI 辅助的模组定位分析。

## 为什么不是纯规则

``modrole.py`` 的规则版能处理 90% 的模组（96 个里只剩 5 个低置信），
但那 5 个恰恰是最需要判断的：名字看不出类型、目录结构平庸、tags 只写 Gameplay。

**规则读不懂「这个 mod 在干什么」，AI 可以。**

## 喂给 AI 什么

不是把文件全塞进去（那会爆上下文），而是**指纹**：

* 顶层目录与各目录文件数 —— 看形状就知道是内容包还是补丁
* 关键定义计数 —— 改了多少建筑/科技/事件
* 依赖与 tags —— 作者自己的声明
* 工坊描述（抓到的话）—— 作者写的排序要求

一份指纹约 200~400 token，96 个模组一次批量约 30K token，
**一次调用搞定，不是 96 次**。

## 防瞎猜

规则版的教训（见 ``sortreq.py`` 顶部的真实案例）是：**模型会从功能列表里
读出并不存在的排序要求**。所以这里要求：

1. 每条结论必须带 ``evidence``（引原文或引指纹里的具体字段）
2. 允许输出 ``unknown`` —— 看不出来就说看不出来，比猜一个强
3. 置信度分开算：AI 自评 + 有没有证据，两者取低

调用方把 AI 结果和规则结果比对，不一致的标出来让用户定。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

# 这些目录里有"作者意图"最密集的信息
_KEY_DIRS = (
    "common/buildings", "common/technologies", "common/events",
    "common/scripted_triggers", "common/scripted_effects",
    "common/pop_jobs", "common/ship_sizes", "common/section_templates",
    "common/component_templates", "common/edicts", "common/decisions",
    "common/traits", "common/governments", "common/policies",
    "events", "interface", "localisation",
)

# 指纹只统计这些目录下的文件数，不做全文扫描（要快）
_COUNT_DIRS = ("common", "events", "gfx", "interface", "localisation",
               "sound", "fonts", "prescripted_countries", "map")


@dataclass
class Fingerprint:
    """一个模组的「形状」。"""

    name: str
    key: str = ""
    tags: list[str] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)
    version: str = ""
    supported_version: str = ""
    top_dirs: dict[str, int] = field(default_factory=dict)   # 目录 -> 文件数（估）
    key_files: list[str] = field(default_factory=list)       # 命中 _KEY_DIRS 的相对路径
    size_mb: float = 0.0
    description: str = ""                                    # 工坊描述（可能为空）

    def to_prompt(self) -> str:
        """压成紧凑文本。**不用 JSON** —— 中文名在 JSON 里反斜杠太多，
        模型读起来反而更费 token。"""
        parts = [f"### {self.name}"]
        if self.tags:
            parts.append(f"tags: {', '.join(self.tags)}")
        if self.dependencies:
            parts.append(f"依赖: {', '.join(self.dependencies[:6])}")
        if self.version or self.supported_version:
            parts.append(f"版本: {self.version} / 支持 {self.supported_version}")
        dirs = ", ".join(f"{k}({v})" for k, v in
                         sorted(self.top_dirs.items(), key=lambda x: -x[1])[:8])
        if dirs:
            parts.append(f"目录: {dirs}")
        if self.key_files:
            parts.append(f"关键文件: {', '.join(self.key_files[:10])}")
        if self.size_mb:
            parts.append(f"体量: {self.size_mb:.1f} MB")
        if self.description:
            # 描述里的排序小节最有价值，整段带上；其余截断
            parts.append(f"工坊描述: {self.description[:1200]}")
        return "\n".join(parts)


def _count_files(root: Path, sub: str, limit: int = 4000) -> int:
    """数一个子目录下的文件（有上限，工坊 mod 可能有几万个）。"""
    d = root / sub
    if not d.is_dir():
        return 0
    n = 0
    try:
        for _dirpath, _dirnames, filenames in os.walk(d):
            n += len(filenames)
            if n >= limit:
                return n
    except OSError:
        pass
    return n


def fingerprint(mod, *, description: str = "") -> Fingerprint:
    """从 Mod 对象造指纹。"""
    root = Path(getattr(mod, "path", "") or "")
    fp = Fingerprint(
        name=getattr(mod, "name", "") or "",
        key=getattr(mod, "key", "") or "",
        tags=[str(t) for t in (getattr(mod, "tags", []) or [])],
        dependencies=[str(d) for d in (getattr(mod, "dependencies", []) or [])],
        version=str(getattr(mod, "version", "") or ""),
        supported_version=str(getattr(mod, "supported_version", "") or ""),
        description=description,
    )
    if not root.is_dir():
        return fp

    total = 0
    for sub in _COUNT_DIRS:
        c = _count_files(root, sub)
        if c:
            fp.top_dirs[sub] = c
            total += c
    # 体量：用 top_dirs 的文件数粗估，避免 os.walk 整个 2GB 的 ACOT
    fp.size_mb = round(total * 8 / 1024, 1)   # 平均每个文件按 8KB 估

    for rel in _KEY_DIRS:
        d = root / rel
        if d.is_dir():
            try:
                n = sum(1 for _ in d.iterdir())
            except OSError:
                continue
            if n:
                fp.key_files.append(f"{rel}/ ({n})")
    return fp


SYSTEM = """你是《群星》(Stellaris) 模组加载顺序专家。

你会收到一批模组的「指纹」：名字、tags、依赖、顶层目录与文件数、关键定义文件、工坊描述。
你的任务是判断**每个模组该站在加载顺序的哪一层**，以及它有没有**明确的排序要求**。

# 分层

- base      前置框架/基础库（被大量模组依赖、提供共享定义；不需要别的 mod 就能工作）
- overhaul  大型机制重做（改模型/经济/科技树，体量大）
- content   常规内容（加建筑/事件/科技，规模一般）
- ui        界面（改 interface/，或有 UI 依赖）
- patch     兼容补丁/Bug 修复（修正别人的东西）
- minor     小型调整（数值微调、语音包、音乐包，顺序无所谓）
- translation 汉化（只含 localisation/）
- unknown   看不出来

# 排序要求

只认**作者明确写下的**：
- 「必须在 X 后面 / 之前」「load after / before X」「place below X」
- 作者说「不需要排序」「顺序无所谓」→ 明确记为 none

**不要从功能介绍里脑补排序关系。** 「支持 XXX 模组」「兼容 XXX」不是排序要求。
「设置面板支持 [XXX]」更不是 —— 那只是说 panel 能控制它。

# 输出

严格 JSON，不要 markdown 代码块：

{
  "mods": [
    {
      "name": "模组名（照抄输入，一字不改）",
      "tier": "content",
      "confidence": 0.8,
      "evidence": "依据的指纹字段或原文，一句话",
      "sort_after": ["另一个模组的名字"],
      "sort_before": [],
      "note": ""
    }
  ],
  "unknown": ["判断不了的模组名"]
}

规则：
- name 必须和输入**完全一致**，不要翻译不要缩写
- sort_after/before 里只能填**这批指纹里出现过的模组名**，不要杜撰
- 没有依据就把 tier 写成 unknown，confidence 给 0.3 以下
- evidence 必须引用看得见的东西（"工坊描述写着必须在UI Overhaul后面" / "tags 只有 Gameplay，目录只有 common"）
"""


def build_prompt(fingerprints: list[Fingerprint]) -> str:
    """把一批指纹拼成一次请求的 payload。"""
    return "\n\n".join(fp.to_prompt() for fp in fingerprints)


def parse_result(data: object, names: list[str]) -> dict:
    """清洗 AI 返回。**名称对不上就直接丢** —— 宁可少几条，不要张冠李戴。"""
    if not isinstance(data, dict):
        return {"mods": [], "unknown": [], "dropped": 0, "ok": False}

    valid = {n for n in names}
    out = []
    dropped = 0
    for it in (data.get("mods") or []):
        if not isinstance(it, dict):
            dropped += 1
            continue
        name = str(it.get("name") or "").strip()
        if name not in valid:
            dropped += 1
            continue
        tier = str(it.get("tier") or "unknown").lower()
        if tier not in ("base", "overhaul", "content", "ui", "patch",
                        "minor", "translation", "unknown"):
            tier = "unknown"
        try:
            conf = float(it.get("confidence") or 0)
        except (TypeError, ValueError):
            conf = 0.0
        evidence = str(it.get("evidence") or "").strip()
        # 没有证据的结论打折 —— 这是防瞎猜的最后一道
        if not evidence:
            conf = min(conf, 0.3)

        def _names(v) -> list[str]:
            if not isinstance(v, list):
                return []
            # 只留真实存在的模组名，杜撰的直接丢
            return [str(x).strip() for x in v if str(x).strip() in valid]

        out.append({
            "name": name,
            "tier": tier,
            "confidence": round(max(0.0, min(1.0, conf)), 2),
            "evidence": evidence,
            "sort_after": _names(it.get("sort_after")),
            "sort_before": _names(it.get("sort_before")),
            "note": str(it.get("note") or "")[:200],
        })

    return {"mods": out, "unknown": data.get("unknown") or [],
            "dropped": dropped, "ok": True}


# --------------------------------------------------------------------------
# 缓存：同一批指纹不必重跑
# --------------------------------------------------------------------------

def cache_path(data_dir: Path) -> Path:
    return Path(data_dir) / "ai_sort_cache.json"


def load_cache(data_dir: Path) -> dict:
    p = cache_path(data_dir)
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_cache(data_dir: Path, cache: dict) -> None:
    p = cache_path(data_dir)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(cache, ensure_ascii=False, indent=1),
                     encoding="utf-8")
    except OSError:
        pass


def fingerprint_key(fp: Fingerprint) -> str:
    """指纹的稳定摘要 —— 指纹变了（mod 更新）就重算。"""
    import hashlib
    blob = json.dumps({
        "n": fp.name, "t": fp.tags, "d": fp.dependencies,
        "v": fp.supported_version, "dirs": fp.top_dirs, "k": fp.key_files,
        "desc": bool(fp.description),
    }, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------
# 编排：分批 → 调用 → 合并
# --------------------------------------------------------------------------

# 一批几个。96 个全塞一次约 9000 token，理论可行，但：
# ① 模型对长列表的注意力会衰减（末尾的模组容易被糊弄过去）
# ② 输出也要几百 token，太长容易截断成不是合法 JSON
# 30 个一批，3~4 次调用，稳。
BATCH_SIZE = 30


def analyze_batch(settings, fingerprints: list[Fingerprint], *,
                  should_stop=None, log=None,
                  cache_dir: Path | None = None) -> dict:
    """把一批模组交给 AI 定层 + 提排序要求。

    返回::

        {
          "mods": [...],            # 合并后的结论
          "unknown": [...],
          "dropped": 12,            # 名称对不上被丢掉的条数
          "batches": 3,
          "cached": 2,              # 直接命中缓存的批次数
          "error": "",              # 全部失败时的原因
        }

    **缓存按指纹摘要**：mod 更新过（指纹变了）自动重算，没变就不重复花钱。
    """
    from .analyze import ask_json  # 延迟导入，避免循环

    cache: dict = {}
    cache_file = None
    if cache_dir is not None:
        cache = load_cache(cache_dir)
        cache_file = cache_path(cache_dir)

    names = [fp.name for fp in fingerprints]
    merged: list[dict] = []
    unknown: list[str] = []
    dropped = 0
    batches = 0
    cached_hit = 0
    errors: list[str] = []

    for i in range(0, len(fingerprints), BATCH_SIZE):
        if should_stop and should_stop():
            break
        chunk = fingerprints[i:i + BATCH_SIZE]
        batches += 1

        # 这一批的合并摘要：任一指纹变了就整批重算
        key = hashlib_batch_key(chunk)
        hit = cache.get(key)
        if hit and isinstance(hit, dict):
            cached_hit += 1
            res = hit
        else:
            if log:
                log(f"AI 分析第 {batches} 批（{len(chunk)} 个模组）…")
            payload = build_prompt(chunk)
            try:
                data = ask_json(settings, SYSTEM, payload,
                                temperature=0.1, should_stop=should_stop)
            except Exception as exc:          # 网络/额度/解析都可能
                errors.append(f"第 {batches} 批：{exc}")
                continue
            res = parse_result(data, names)
            if not res.get("ok"):
                errors.append(f"第 {batches} 批返回的不是对象")
                continue
            if cache_dir is not None:
                cache[key] = res

        merged.extend(res.get("mods") or [])
        unknown.extend(res.get("unknown") or [])
        dropped += int(res.get("dropped") or 0)

    if cache_dir is not None and cache:
        save_cache(cache_dir, cache)

    return {
        "mods": merged,
        "unknown": unknown,
        "dropped": dropped,
        "batches": batches,
        "cached": cached_hit,
        "error": "；".join(errors[:3]),
    }


def hashlib_batch_key(chunk: list[Fingerprint]) -> str:
    """一批指纹的合并键。"""
    import hashlib
    acc = hashlib.sha1()
    for fp in chunk:
        acc.update(fingerprint_key(fp).encode("ascii"))
    return acc.hexdigest()[:20]


# --------------------------------------------------------------------------
# 和规则版对比
# --------------------------------------------------------------------------

_TIER_TO_NAME = {
    "base": "前置框架 / 基础库", "overhaul": "大型机制重做",
    "content": "常规内容", "ui": "界面 UI", "patch": "兼容补丁 / Bug 修复",
    "minor": "小型调整", "translation": "本地化 / 汉化", "unknown": "看不出来",
}


def compare(ai_results: list[dict], rule_tiers: dict[str, int]) -> list[dict]:
    """AI 结论 vs 规则结论，挑出不一致的让用户看。

    ``rule_tiers`` 是 ``{mod名: tier数字}``。
    **不一致不等于 AI 错** —— 这正是要人看一眼的地方。
    """
    from ..modrole import TIER_LABEL, TIER_BASE, TIER_OVERHAUL, TIER_CONTENT, \
        TIER_UI, TIER_PATCH, TIER_MINOR, TIER_TRANSLATION

    name_to_tier = {
        "base": TIER_BASE, "overhaul": TIER_OVERHAUL, "content": TIER_CONTENT,
        "ui": TIER_UI, "patch": TIER_PATCH, "minor": TIER_MINOR,
        "translation": TIER_TRANSLATION,
    }
    out = []
    for it in ai_results:
        name = it.get("name", "")
        ai_tier = it.get("tier", "unknown")
        if ai_tier == "unknown" or name not in rule_tiers:
            continue
        rt = rule_tiers[name]
        at = name_to_tier.get(ai_tier, TIER_CONTENT)
        if at != rt:
            out.append({
                "name": name,
                "rule": TIER_LABEL.get(rt, str(rt)),
                "ai": TIER_LABEL.get(at, ai_tier),
                "ai_confidence": it.get("confidence", 0),
                "evidence": it.get("evidence", ""),
            })
    return out
