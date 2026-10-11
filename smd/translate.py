"""一键翻译：把模组的本地化文本翻成目标语言。

工作方式
--------
1. **收集**：扫描每个模组 ``localisation/`` 下的 ``*_l_<语言>.yml``，
   解析出 ``键 -> 文本``。只收目标语言**还没有**的键
   （已经有译文的就不重复翻，省 token 也避免覆盖别人的好翻译）。
2. **分批**：按条数与字符数切批。一批太大模型会漏条目；
   太小则请求数暴涨。默认每批 40 条 / 4000 字符。
3. **请求**：走已配置的 AI 供应商（``smd.ai.client``），
   在**现有 AI 接入页**里配好就能用，不需要额外设置。
4. **校验**：模型返回的键必须**原样存在**于请求里——键名对不上的
   一律丢弃。模型很爱"顺手修一下"键名或补充不存在的条目，
   放进去会污染本地化文件。
5. **落盘**：写成 ``localisation/zzz_SMD_tr_<语言>.yml``，
   前缀保证加载顺序排最后，因此**覆盖**原模组的英文原文。

隐私
----
只有在「AI 接入」页打开 ``allow_send_mod_content`` 之后才会发送文本。
模组的本地化文本会离开你的电脑——这一点在界面上会明确提示。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from .paths import is_dir, is_file, read_text
from .scanner import _parse_localisation

# 支持的目标语言：键 -> (Paradox 语言后缀, 中文显示名, 给模型的英文名)
LANGUAGES: dict[str, tuple[str, str, str]] = {
    "zh": ("l_simp_chinese", "简体中文", "Simplified Chinese"),
    "en": ("l_english", "English", "English"),
    "fr": ("l_french", "Français", "French"),
    "de": ("l_german", "Deutsch", "German"),
    "es": ("l_spanish", "Español", "Spanish"),
    "ru": ("l_russian", "Русский", "Russian"),
    "pl": ("l_polish", "Polski", "Polish"),
    "pt": ("l_braz_por", "Português (Brasil)", "Brazilian Portuguese"),
    "ja": ("l_japanese", "日本語", "Japanese"),
    "ko": ("l_korean", "한국어", "Korean"),
}

# 源语言后缀：优先取这些，其余语言的内容不当作"原文"
SOURCE_SUFFIXES = ("l_english",)

# 翻译补丁前缀（沿用全局前缀，保证加载顺序排最后）
TR_PREFIX = "zzz_SMD_tr_"


def language_choices() -> list[dict]:
    """给界面用的语言清单。"""
    return [{"key": k, "label": v[1], "suffix": v[0], "english": v[2]}
            for k, v in LANGUAGES.items()]


@dataclass
class LocEntry:
    """一条待翻译文本。"""

    key: str
    text: str
    mod_key: str
    mod_name: str
    source_rel: str = ""


@dataclass
class TranslatePlan:
    """一次翻译任务的计划。"""

    target: str = "zh"
    entries: list[LocEntry] = field(default_factory=list)
    mods_total: int = 0
    mods_with_text: int = 0
    skipped_existing: int = 0
    chars: int = 0

    @property
    def suffix(self) -> str:
        """Paradox 语言后缀（例如 ``l_simp_chinese``）。"""
        return LANGUAGES.get(self.target, LANGUAGES["zh"])[0]

    def to_dict(self) -> dict:
        """序列化，供界面展示统计与预览。"""
        return {
            "target": self.target,
            "target_label": LANGUAGES.get(self.target, ("", self.target, ""))[1],
            "suffix": self.suffix,
            "entries": len(self.entries),
            "chars": self.chars,
            "mods_total": self.mods_total,
            "mods_with_text": self.mods_with_text,
            "skipped_existing": self.skipped_existing,
            "sample": [
                {"key": e.key, "text": e.text[:70], "mod": e.mod_name}
                for e in self.entries[:8]
            ],
        }


# ---------------------------------------------------------------------------
# 收集
# ---------------------------------------------------------------------------

def _loc_files(mod_dir: Path, suffix: str) -> list[Path]:
    """列出某个模组里指定语言的本地化文件。"""
    root = mod_dir / "localisation"
    if not is_dir(root):
        # 少数模组用 localisation 以外的目录（如 localization）
        for alt in ("localization", "Localisation"):
            if is_dir(mod_dir / alt):
                root = mod_dir / alt
                break
        else:
            return []
    out: list[Path] = []
    try:
        for p in root.rglob("*.yml"):
            n = p.name.lower()
            if n.endswith(f"_{suffix}.yml") or n.endswith(f"_{suffix}.yaml"):
                out.append(p)
    except OSError:
        return []
    return out


def _existing_keys(mod_dir: Path, target_suffix: str) -> set[str]:
    """该模组自己的目标语言键。"""
    keys: set[str] = set()
    for p in _loc_files(mod_dir, target_suffix):
        try:
            keys |= set(_parse_localisation(read_text(p)).keys())
        except OSError:
            continue
    return keys


_KEY_CACHE: dict[tuple[str, int], set[str]] = {}


def collect_translated_keys(library: Any, target: str, *,
                            use_cache: bool = True,
                            progress: Callable[[str, float], None] | None = None,
                            should_stop: Callable[[], bool] | None = None
                            ) -> set[str]:
    """全库范围内，目标语言**已经有译文**的全部键。

    为什么必须跨模组统计：汉化是以独立模组的形式存在的。
    ACOT 的英文键由「ACOT临时汉化」提供中文，如果只看单个模组的目录，
    就会以为这些键没翻译过，白白再翻一遍——既浪费钱，
    又可能用机器翻译覆盖掉人工翻译的好译文。

    这个扫描要读遍所有模组的本地化文件（实测约 3-4 秒），
    而界面每次切到翻译页都会调一次，所以结果按「模组数」缓存：
    模组集合没变就复用，变了一次性失效。
    """
    suffix = LANGUAGES.get(target, LANGUAGES["zh"])[0]
    keys: set[str] = set()
    if library is None:
        return keys
    mods = [m for m in library.all() if not m.is_ghost()]
    ck = (suffix, len(mods))
    if use_cache and ck in _KEY_CACHE:
        return set(_KEY_CACHE[ck])
    for i, m in enumerate(mods):
        if should_stop and should_stop():
            break
        if progress and i % 10 == 0:
            progress(f"统计已有译文：{m.name}",
                     (i + 1) / max(1, len(mods)) * 0.4)
        cdir = m.content_dir()
        if not cdir or not is_dir(cdir):
            continue
        keys |= _existing_keys(Path(cdir), suffix)
    if use_cache and not (should_stop and should_stop()):
        _KEY_CACHE[ck] = set(keys)
    return keys


def collect_entries(library: Any, target: str = "zh", *,
                    mod_keys: Iterable[str] | None = None,
                    limit: int = 0,
                    skip_keys: set[str] | None = None,
                    progress: Callable[[str, float], None] | None = None,
                    should_stop: Callable[[], bool] | None = None
                    ) -> TranslatePlan:
    """收集需要翻译的条目。

    ``limit`` > 0 时限制总条数（用于试翻一小批看效果）。
    ``skip_keys`` 是**全库范围**内已经有译文的键（见
    :func:`collect_translated_keys`），这些一律不重复翻译。
    传 ``None`` 时会自动统计一次。
    """
    suffix = LANGUAGES.get(target, LANGUAGES["zh"])[0]
    plan = TranslatePlan(target=target)
    if library is None:
        return plan

    if skip_keys is None:
        skip_keys = collect_translated_keys(
            library, target, progress=progress, should_stop=should_stop)

    allow = set(mod_keys) if mod_keys else None
    mods = [m for m in library.all() if not m.is_ghost()]
    if allow:
        mods = [m for m in mods if m.key in allow]
    plan.mods_total = len(mods)

    seen_keys: set[str] = set()
    for i, m in enumerate(mods):
        if should_stop and should_stop():
            break
        if progress:
            progress(f"收集文本：{m.name}",
                     0.4 + (i + 1) / max(1, len(mods)) * 0.6)
        cdir = m.content_dir()
        if not cdir or not is_dir(cdir):
            continue
        root = Path(cdir)
        have = _existing_keys(root, suffix) | skip_keys
        got = False
        # 原文来自英文文件；没有英文的模组跳过（可能是原生中文模组）
        for src in _loc_files(root, "l_english"):
            try:
                text = read_text(src)
            except OSError:
                continue
            for k, v in _parse_localisation(text).items():
                if k in have or k in seen_keys:
                    plan.skipped_existing += 1
                    continue
                if not v or not v.strip():
                    continue
                seen_keys.add(k)
                plan.entries.append(LocEntry(
                    key=k, text=v.strip(), mod_key=m.key, mod_name=m.name,
                    source_rel=str(src.relative_to(root)).replace("\\", "/")))
                plan.chars += len(v)
                got = True
                if limit and len(plan.entries) >= limit:
                    plan.mods_with_text = len(
                        {e.mod_key for e in plan.entries})
                    return plan
        if got:
            plan.mods_with_text += 1
    return plan


# ---------------------------------------------------------------------------
# 分批与提示词
# ---------------------------------------------------------------------------

def batch_entries(entries: list[LocEntry], *, batch_size: int = 40,
                  batch_chars: int = 4000) -> list[list[LocEntry]]:
    """按条数与字符数切批。

    模型对「一次翻太多条」很敏感——中间漏几条它不会说，
    所以宁可多切几批。
    """
    out: list[list[LocEntry]] = []
    cur: list[LocEntry] = []
    n = 0
    for e in entries:
        if cur and (len(cur) >= batch_size or n + len(e.text) > batch_chars):
            out.append(cur)
            cur, n = [], 0
        cur.append(e)
        n += len(e.text)
    if cur:
        out.append(cur)
    return out


def build_translate_messages(batch: list[LocEntry], target: str) -> list[Any]:
    """构造一批的对话消息。"""
    lang = LANGUAGES.get(target, LANGUAGES["zh"])
    payload = {e.key: e.text for e in batch}
    system = (
        "你是《群星》(Stellaris) 模组本地化译者。\n"
        "你会收到一个 JSON 对象，键是本地化键名，值是英文原文。\n"
        f"请把每个值翻译成{lang[1]}（{lang[2]}）。\n\n"
        "严格遵守：\n"
        "1. 只输出一个 JSON 对象，**键必须与输入完全一致**，"
        "一个不多一个不少。\n"
        "2. 不要输出任何解释、注释或 markdown 代码块标记。\n"
        "3. 保留原文里的方括号变量（如 [Root.GetName]、$VAR$、£icon£）"
        "与转义符，位置可以调整以符合语序。\n"
        "4. 保留 \\n 等转义序列。\n"
        "5. 术语用《群星》官方中文译名（例如 Empire=帝国、"
        "Fleet=舰队、Ascension Perk=飞升天赋、Starbase=恒星基地）。\n"
        "6. 文本很短时不要加句号。\n"
    )
    return [
        {"role": "system", "content": system},
        {"role": "user",
         "content": json.dumps(payload, ensure_ascii=False, indent=1)},
    ]


_CODE_FENCE_RE = re.compile(r"^\s*```[a-zA-Z]*\s*|\s*```\s*$")


def parse_translation_reply(reply: str, batch: list[LocEntry]
                            ) -> tuple[dict[str, str], list[str]]:
    """从模型回复里取出译文，并做**严格校验**。

    返回 ``(通过校验的 键->译文, 问题列表)``。

    校验点（模型经常在这里出错）：

    * 必须能解析成 JSON 对象
    * 键必须**原样**出现在请求里 —— 模型会自作主张改键名、
      拆键、合并键，这些一律丢弃
    * 值必须是非空字符串
    """
    problems: list[str] = []
    text = (reply or "").strip()
    text = _CODE_FENCE_RE.sub("", text)
    # 有些模型会前后加一句话，截取最外层大括号
    i, j = text.find("{"), text.rfind("}")
    if i < 0 or j <= i:
        return {}, ["回复里找不到 JSON 对象"]
    try:
        data = json.loads(text[i:j + 1])
    except ValueError as exc:
        return {}, [f"JSON 解析失败：{exc}"]
    if not isinstance(data, dict):
        return {}, ["回复不是 JSON 对象"]

    want = {e.key for e in batch}
    out: dict[str, str] = {}
    for k, v in data.items():
        if k not in want:
            continue                     # 模型凭空造的键，丢弃
        if not isinstance(v, str) or not v.strip():
            continue
        out[k] = v.strip()
    missing = want - set(out)
    if missing:
        problems.append(f"{len(missing)} 条未返回译文（例如 "
                        f"{sorted(missing)[0]}）")
    extra = set(data) - want
    if extra:
        problems.append(f"{len(extra)} 条键名对不上，已丢弃（例如 "
                        f"{sorted(extra)[0]}）")
    return out, problems


# ---------------------------------------------------------------------------
# 执行
# ---------------------------------------------------------------------------

def write_translation_files(library: Any, translated: dict[str, dict[str, str]],
                            target: str, *, out_root: Path | None = None
                            ) -> list[dict]:
    """把译文写成补丁模组的本地化文件。

    ``translated`` 结构：``{mod_key: {loc_key: 译文}}``。

    每个模组生成一个 ``localisation/zzz_SMD_tr_<语言>.yml``，
    文件名前缀保证加载顺序排在最后，从而覆盖原模组的英文原文。
    """
    suffix = LANGUAGES.get(target, LANGUAGES["zh"])[0]
    written: list[dict] = []
    if out_root is None:
        return written
    for mod_key, entries in translated.items():
        if not entries:
            continue
        m = library.get(mod_key) if library is not None else None
        name = m.name if m else mod_key
        # 文件名里不能有非法字符
        safe = re.sub(r"[^\w.\-]+", "_", name)[:48] or "mod"
        rel = f"localisation/{TR_PREFIX}{safe}_{suffix}.yml"
        path = Path(out_root) / rel
        lines = [f"{suffix}:", ""]
        for k in sorted(entries):
            v = entries[k].replace('"', '\\"').replace("\n", "\\n")
            lines.append(f' {k}:0 "{v}"')
        body = "\n".join(lines) + "\n"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("\ufeff" + body, encoding="utf-8", newline="\n")
        except OSError as exc:
            written.append({"mod": name, "ok": False, "error": str(exc)})
            continue
        written.append({"mod": name, "mod_key": mod_key, "ok": True,
                        "rel": rel, "path": str(path), "count": len(entries)})
    return written


def translate_entries(entries: list[LocEntry], target: str, *,
                      batch_size: int = 40, batch_chars: int = 4000,
                      progress: Callable[[str, float], None] | None = None,
                      should_stop: Callable[[], bool] | None = None,
                      on_batch: Callable[[dict[str, str], int, int], None] | None = None
                      ) -> dict:
    """调用 AI 把条目翻完。

    返回 ``{ok, translated, problems, batches, seconds}``，
    其中 ``translated`` 是 ``{mod_key: {key: 译文}}``。
    """
    from .ai.client import build_client
    from .config import Settings

    cfg = Settings.load().ai
    if not getattr(cfg, "allow_send_mod_content", False):
        return {"ok": False,
                "error": "未开启「允许发送模组内容」。请到 AI 接入页打开该开关——"
                         "翻译需要把模组文本发送给 AI 服务商。"}
    client = build_client(cfg.to_dict() if hasattr(cfg, "to_dict") else cfg)
    if client is None:
        return {"ok": False, "error": "AI 客户端未配置（请先填 API Key）"}

    batches = batch_entries(entries, batch_size=batch_size,
                            batch_chars=batch_chars)
    translated: dict[str, dict[str, str]] = {}
    problems: list[str] = []
    t0 = time.time()
    by_key = {}
    for e in entries:
        by_key.setdefault(e.mod_key, {})[e.key] = e

    for i, batch in enumerate(batches):
        if should_stop and should_stop():
            problems.append("用户中止")
            break
        if progress:
            progress(f"翻译第 {i + 1}/{len(batches)} 批（{len(batch)} 条）",
                     (i + 1) / max(1, len(batches)))
        msgs = build_translate_messages(batch, target)
        try:
            reply = client.chat(msgs)
        except Exception as exc:
            problems.append(f"第 {i + 1} 批请求失败：{exc}")
            continue
        got, probs = parse_translation_reply(reply, batch)
        problems.extend(f"第 {i + 1} 批：{p}" for p in probs)
        for k, v in got.items():
            src = None
            for e in batch:
                if e.key == k:
                    src = e
                    break
            if src is None:
                continue
            translated.setdefault(src.mod_key, {})[k] = v
        if on_batch:
            on_batch(got, i + 1, len(batches))

    n = sum(len(v) for v in translated.values())
    return {"ok": n > 0, "translated": translated, "count": n,
            "problems": problems[:40], "batches": len(batches),
            "seconds": round(time.time() - t0, 1)}
