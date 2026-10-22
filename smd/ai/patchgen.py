"""AI 补丁生成 + 校验（防幻觉）。

为什么需要这一层
----------------
大模型写 Clausewitz 脚本时会犯三类致命错误：

1. **语法错误** —— 括号不配对、少等号。群星解析到就废掉整个文件。
2. **幻觉引用** —— 编造一个不存在的 ``scripted_trigger`` 或修正量名。
   游戏不报错，但那行判断永远为假，玩家会以为补丁「装了没用」。
3. **越权重定义** —— 顺手把没冲突的对象一起覆盖了，
   静默废掉第三个模组的功能。

所以本模块的流程是：**生成 → 解析 → 校验 → 只有通过才交给用户**。
校验失败的补丁仍然会展示（附上问题清单），但会被明确标记为「不建议使用」。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from ..paradox import Node, flatten_definitions, parse_string
from ..paths import human_size

__all__ = ["PatchFile", "PatchIssue", "AiPatchOutcome", "parse_ai_response",
           "verify_patch", "verify_patch_text", "generate_patch_with_ai",
           "ALLOWED_DIRS"]

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

# 补丁文件允许落地的目录（游戏会读取，且不会污染核心数据）
ALLOWED_DIRS = (
    "common/scripted_triggers/",
    "common/scripted_effects/",
    "common/scripted_variables/",
    "common/static_modifiers/",
    "common/component_templates/",
    "common/section_templates/",
    "common/technology/",
    "common/buildings/",
    "common/pop_jobs/",
    "common/traits/",
    "common/megastructures/",
    "common/edicts/",
    "common/policies/",
    "common/governments/",
    "common/opinion_modifiers/",
    "common/on_actions/",
    "events/",
    "interface/",
    "localisation/",
    "common/inline_scripts/",
)

# 从 Clausewitz 脚本里提取「像对象名」的词，用于引用校验
_IDENT_RE = re.compile(r"\b([a-z_][a-z0-9_]{3,})\b")

# 这些词是脚本关键字/内建触发器，不属于「需要定义」的对象
BUILTIN_WORDS = frozenset("""
yes no none all any this root from prev owner controller capital
and or not nor if else while switch case default break continue
trigger effect hidden custom_tooltip desc name value weight factor
modifier potential allow possible random list every ordered limit
count exists has_ set_ add_ remove_ multiply_ divide_ round_ floor_
is_ can_ will_ should_ get_ clear_ save_ load_ country planet fleet
ship pop leader system starbase megastructure sector faction war
galaxy empire species army job building district technology
always never first last random_pick random_list base days months
years hours mtth ai_chance ai_weight show_ ok_to_ abort_ abort
""".split())

# 群星内建触发器的常见前缀（有这些前缀的词不要求被定义）
BUILTIN_PREFIXES = (
    "has_", "is_", "can_", "count_", "num_", "any_", "every_", "random_",
    "set_", "add_", "remove_", "multiply_", "divide_", "clear_", "save_",
    "load_", "get_", "check_", "owns_", "uses_", "allows_", "enable",
    "disable", "create_", "destroy_", "spawn_", "kill_", "give_", "take_",
    "change_", "mod_", "pop_", "planet_", "country_", "fleet_", "ship_",
    "leader_", "species_", "tech_", "building_", "district_", "job_",
    "event_", "on_", "situation_", "archaeology_", "first_contact_",
    "cosmic_storm_", "espionage_", "intel_", "war_", "peace_", "diplo_",
    "opinion_", "federation_", "galcom_", "resolution_", "council_",
    "relic_", "tradition_", "ascension_", "edict_", "policy_", "civic_",
    "government_", "authority_", "trait_", "megastructure_", "starbase_",
    "bypass_", "hyperlane_", "star_", "deposit_", "anomaly_", "army_",
    "combat_", "strike_craft_", "component_", "slot_", "utility_",
    "weapon_", "ship_size_", "section_", "growth_", "pop_growth_",
    "stability", "amenities", "housing", "crime", "unity", "influence",
    "energy", "minerals", "food", "alloys", "consumer_goods", "physics",
    "society", "engineering", "research", "trade", "logistics",
    "military_power", "economy_power", "technology_power", "fleet_power",
    "intel_level", "attitude", "trust", "favors", "infamy",
)


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass
class PatchFile:
    """补丁里的一个文件。"""

    rel: str
    text: str

    @property
    def lines(self) -> int:
        """该补丁文件的行数。"""
        return self.text.count("\n") + 1

    @property
    def bytes(self) -> int:
        """该补丁文件的 UTF-8 字节数。"""
        return len(self.text.encode("utf-8"))

    def to_dict(self) -> dict:
        """序列化，供界面展示与保存。"""
        return {"rel": self.rel, "text": self.text, "lines": self.lines,
                "bytes": self.bytes, "size_human": human_size(self.bytes)}


@dataclass
class PatchIssue:
    """校验发现的一个问题。"""

    level: str          # fatal | error | warn | info
    message: str
    where: str = ""     # 文件名 / 对象名
    hint: str = ""

    @property
    def is_blocking(self) -> bool:
        """该问题是否阻塞保存（fatal / error 级）。"""
        return self.level in ("fatal", "error")

    def to_dict(self) -> dict:
        """序列化，供界面展示。"""
        return {"level": self.level, "message": self.message,
                "where": self.where, "hint": self.hint,
                "blocking": self.is_blocking}


@dataclass
class AiPatchOutcome:
    """AI 生成补丁的完整结果。"""

    ok: bool = False
    files: list[PatchFile] = field(default_factory=list)
    explanation: str = ""            # 模型给的说明
    risk: str = ""                   # 风险评估
    raw: str = ""                    # 模型原始输出
    issues: list[PatchIssue] = field(default_factory=list)
    error: str = ""
    provider: str = ""
    model: str = ""
    elapsed: float = 0.0
    tokens_estimate: int = 0
    # 校验结论
    verified: bool = False
    confidence: str = "unknown"      # high | medium | low | unknown
    context: dict = field(default_factory=dict)
    # 这次实际跑了几轮（1 = 一次就过；>1 = 靠反馈修正过的）
    rounds_used: int = 1

    @property
    def blocking(self) -> list[PatchIssue]:
        """所有阻塞级问题。"""
        return [i for i in self.issues if i.is_blocking]

    def summary(self) -> str:
        """一句话结论，用于界面与日志。"""
        if self.error:
            return f"生成失败：{self.error}"
        n = len(self.files)
        b = len(self.blocking)
        if b:
            return f"生成了 {n} 个文件，但有 {b} 个严重问题，不建议直接使用"
        if n:
            return f"生成了 {n} 个文件，校验通过"
        return "模型没有返回可用的补丁文件"

    def to_dict(self) -> dict:
        """序列化完整结果，供界面展示。"""
        return {
            "ok": self.ok, "summary": self.summary(),
            "files": [f.to_dict() for f in self.files],
            "explanation": self.explanation, "risk": self.risk,
            "raw": self.raw,
            "issues": [i.to_dict() for i in self.issues],
            "error": self.error, "provider": self.provider,
            "model": self.model, "elapsed": round(self.elapsed, 2),
            "tokens_estimate": self.tokens_estimate,
            "verified": self.verified, "confidence": self.confidence,
            "context": self.context,
        }


# ---------------------------------------------------------------------------
# 解析模型输出
# ---------------------------------------------------------------------------

# ```path: xxx/yyy.txt  或  ```xxx/yyy.txt
#
# 两种写法的区别在围栏后**有没有空格**：
#   ` ```path: a/b.txt `  —— 紧跟 `path:`，没有空格
#   ` ``` a/b.txt `       —— 有一个空格再接路径
# 而「只有内容没有路径」的写法是 ` ``` ` 后直接换行。
# 必须把这三者区分开，否则空代码块会被当成一个没有文件名的补丁收下。
# ```path: xxx/yyy.txt  或  ```xxx/yyy.txt  或  ``` xxx/yyy.txt
#
# 需要区分的三种写法：
#   ` ```path: a/b.txt `  显式关键字
#   ` ```a/b.txt `        直接贴路径（GitHub 风格，无空格）
#   ` ``` `               后直接换行 —— 没有路径，交给 _guess_path 推断
#
# 因此：先尝试 `path:` 前缀，再尝试「紧跟的非空白串」，
# 后者只有在通过 _normalize_path（含 / 且带扩展名）时才认。
_FENCE_RE = re.compile(
    r"```[ \t]*path[ \t]*[:：][ \t]*([^\n`]*)\n(.*?)```"
    r"|"
    r"```[ \t]*([^\s`]+)\n(.*?)```",
    re.S | re.I,
)
# ``` 后直接换行（无路径）——交给 _guess_path 推断落点
_FENCE_NOPATH_RE = re.compile(r"```[ \t]*\n(.*?)```", re.S)


def parse_ai_response(text: str) -> tuple[list[PatchFile], str, str]:
    """把模型输出拆成 ``(文件列表, 说明, 风险评估)``。"""
    files: list[PatchFile] = []
    seen: list[tuple[int, int]] = []

    for m in _FENCE_RE.finditer(text):
        if m.group(1) is not None:
            header, body = m.group(1), m.group(2)
        else:
            header, body = m.group(3), m.group(4)
        body = body or ""
        seen.append((m.start(), m.end()))
        # 第二分支捕到的可能只是语言标记（```txt），
        # _normalize_path 会因为缺少 "/" 而拒绝它，此时退回按内容推断
        rel = _normalize_path(header or "") or _guess_path(body)
        if rel and body.strip():
            files.append(PatchFile(rel=rel, text=body.rstrip() + "\n"))

    for m in _FENCE_NOPATH_RE.finditer(text):
        if any(a <= m.start() < b for a, b in seen):
            continue
        body = m.group(1)
        if not body.strip():
            continue
        rel = _guess_path(body)
        if rel:
            files.append(PatchFile(rel=rel, text=body.rstrip() + "\n"))

    # 说明 = 「### 补丁文件」之前的部分
    split = re.split(r"###\s*补丁文件", text, maxsplit=1)
    head = split[0]
    explanation = re.sub(r"^###\s*说明\s*", "", head.strip(),
                         flags=re.M).strip()

    risk = ""
    m = re.search(r"###\s*风险评估(.*)$", text, re.S)
    if m:
        risk = m.group(1).strip()

    if not files:
        # 模型可能拒绝合并，只给了文字建议
        explanation = explanation or text.strip()
    return files, explanation, risk


def _normalize_path(header: str) -> str:
    """把模型给的文件名规范化成 ``a/b/c.txt``。"""
    if not header:
        return ""
    h = header.strip().replace("\\", "/")
    h = re.sub(r"^[./]+", "", h)
    # 去掉可能的引号与说明文字
    h = h.split()[0].strip('"\'`') if h.split() else ""
    if not h or "." not in h.rsplit("/", 1)[-1]:
        return ""
    if "/" not in h:
        return ""          # 只要目录结构，不接受裸文件名
    return h


def _guess_path(body: str) -> str:
    """从内容猜一个合适的落地路径。

    模型有时只给内容不给路径。这里按「出现了哪种具名对象」来推断，
    推不出来就返回空（宁可不收，也不放到错误目录）。
    """
    head = body[:6000]
    if re.search(r"^\s*l_[a-z_]+:", head, re.M):
        return "localisation/simp_chinese/zzz_smd_patch_l_simp_chinese.yml"
    # 按「顶层具名对象」的常见类别判断
    for name, rel in (
        ("scripted_trigger", "common/scripted_triggers/zzz_smd_patch.txt"),
        ("scripted_effect", "common/scripted_effects/zzz_smd_patch.txt"),
        ("static_modifier", "common/static_modifiers/zzz_smd_patch.txt"),
        ("component_template", "common/component_templates/zzz_smd_patch.txt"),
        ("section_template", "common/section_templates/zzz_smd_patch.txt"),
        ("ship_size", "common/ship_sizes/zzz_smd_patch.txt"),
        ("megastructure", "common/megastructures/zzz_smd_patch.txt"),
        ("building", "common/buildings/zzz_smd_patch.txt"),
        ("pop_job", "common/pop_jobs/zzz_smd_patch.txt"),
        ("technology", "common/technology/zzz_smd_patch.txt"),
        ("on_action", "common/on_actions/zzz_smd_patch.txt"),
        ("edict", "common/edicts/zzz_smd_patch.txt"),
    ):
        if re.search(rf"^\s*{name}\s*=", head, re.M):
            return rel
    # 事件文件
    if re.search(r"^\s*(country_event|planet_event|event|ship_event|"
                 r"pop_event|fleet_event|system_event)\s*=", head, re.M):
        return "events/zzz_smd_patch.txt"
    # 界面
    if re.search(r"^\s*(types|containerWindowType|buttonType|"
                 r"windowType)\s*=", head, re.M):
        return "interface/zzz_smd_patch.gui"
    # 兜底：只要看起来像 `名字 = { ... }` 就当触发器（最常见的冲突类型）
    if re.search(r"^\s*[a-z_][a-z0-9_]*\s*=\s*\{", body, re.M):
        return "common/scripted_triggers/zzz_smd_patch.txt"
    return ""


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------

def _collect_known_symbols(indexes: Any, extra: Iterable[str] = ()) -> set[str]:
    """收集已知的具名对象，作为引用校验的白名单。"""
    known: set[str] = set(extra)
    if indexes is None:
        return known
    for idx in getattr(indexes, "indexes", {}).values():
        known.update(idx.definitions.keys())
        known.update(idx.loc_keys.keys())
    return known


def _is_probably_builtin(word: str) -> bool:
    """判断一个词是否更像游戏内建关键字（不必被定义）。"""
    if word in BUILTIN_WORDS:
        return True
    if word.startswith(BUILTIN_PREFIXES):
        return True
    if "_" not in word:
        # 单个单词且很短：更像数值键名而非自定义对象
        return len(word) < 8
    return False


def verify_patch(files: list[PatchFile], *, indexes: Any = None,
                 conflict_target: str = "",
                 conflict_mods: Iterable[str] = (),
                 original_definitions: set[str] | None = None
                 ) -> tuple[bool, list[PatchIssue]]:
    """校验补丁文件。返回 ``(是否通过, 问题列表)``。"""
    issues: list[PatchIssue] = []
    if not files:
        issues.append(PatchIssue("error", "没有解析出任何补丁文件",
                                 hint="模型可能只是给了文字建议，"
                                      "或输出格式不符合要求。"))
        return False, issues

    known = _collect_known_symbols(indexes)
    orig_defs = set(original_definitions or ())

    for f in files:
        # --- 1. 路径合法性 ---
        rel = f.rel.replace("\\", "/").lstrip("/")
        if not any(rel.startswith(d) for d in ALLOWED_DIRS):
            issues.append(PatchIssue(
                "warn", f"文件路径不在常规补丁目录内：{rel}",
                where=rel,
                hint=f"建议放到：{ALLOWED_DIRS[0]} 这类目录下，"
                     f"否则游戏可能不会读取。"))

        # --- 2. 语法解析 ---
        is_loc = rel.endswith(".yml") or rel.startswith("localisation")
        if is_loc:
            _verify_localisation(f, issues)
            continue

        root = parse_string(f.text)
        perr = root.get_all("__parse_errors__")
        if perr:
            for e in perr[:5]:
                issues.append(PatchIssue(
                    "fatal", f"语法错误：{e.value}", where=rel,
                    hint="群星解析失败会导致整个文件失效。"
                         "通常是括号不配对或少了等号。"))
            continue

        defs = flatten_definitions(root)
        if not defs:
            issues.append(PatchIssue(
                "error", "该文件没有定义任何具名对象（空补丁）", where=rel,
                hint="空文件不会产生任何效果。"
                     "如果模型只给了文字建议，请改用「保留哪一个」的处理方式。"))
            continue

        # --- 3. 越权重定义检查 ---
        if orig_defs:
            extra = [n for n in defs if n not in orig_defs
                     and not n.startswith("@")]
            if extra:
                issues.append(PatchIssue(
                    "warn",
                    f"补丁额外定义了 {len(extra)} 个冲突之外的对象："
                    + "、".join(extra[:6]) + ("…" if len(extra) > 6 else ""),
                    where=rel,
                    hint="这些对象会覆盖同名的其它模组定义。"
                         "如果不是有意为之，建议删掉。"))

        if conflict_target and conflict_target not in defs \
                and "@" not in conflict_target:
            # 只有当冲突对象看起来是可定义的具名对象时才提示
            looks_like_def = bool(re.match(r"^[a-z_][a-z0-9_]*$",
                                           conflict_target or ""))
            if looks_like_def and "/" not in conflict_target:
                issues.append(PatchIssue(
                    "warn",
                    f"补丁里没有重新定义冲突对象「{conflict_target}」",
                    where=rel,
                    hint="群星是整体覆盖语义：要修正它必须写出它的完整新定义。"
                         "如果补丁改的是别的东西，可以忽略这条。"))

        # --- 4. 引用幻觉检查 ---
        _verify_references(f, defs, known, issues)

    blocking = [i for i in issues if i.is_blocking]
    return (not blocking), issues


def _verify_localisation(f: PatchFile, issues: list[PatchIssue]) -> None:
    """校验 yml 本地化文件。"""
    bad = 0
    for i, line in enumerate(f.text.splitlines(), 1):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if not re.match(r'^[A-Za-z0-9_.\-]+\s*:\s*\d*\s*".*"\s*$', s):
            bad += 1
            if bad <= 3:
                issues.append(PatchIssue(
                    "warn", f"第 {i} 行本地化格式不合法：{s[:60]}",
                    where=f.rel,
                    hint='正确格式：`键:0 "文本"`，且文本必须在一行内。'))
    if bad:
        issues.append(PatchIssue(
            "info", f"共 {bad} 行本地化格式可疑", where=f.rel))


def _verify_references(f: PatchFile, defs: dict[str, Node], known: set[str],
                       issues: list[PatchIssue]) -> None:
    """检查补丁里引用的自定义对象是否真实存在。

    只对「看起来像自定义对象」的词报警，避免把内建触发器当成幽灵。
    没有索引信息时（``known`` 为空）直接跳过，避免误报。
    """
    if not known:
        return
    text = f.text
    # 补丁自己定义的名字也算已知
    local_known = set(defs.keys()) | known

    suspicious: dict[str, int] = {}
    for m in _IDENT_RE.finditer(text):
        word = m.group(1)
        if word in local_known or word in defs:
            continue
        if _is_probably_builtin(word):
            continue
        # 只关注像「自定义脚本对象」的词：含下划线、长度够、非纯数值键
        if "_" not in word or len(word) < 6:
            continue
        suspicious[word] = suspicious.get(word, 0) + 1

    if suspicious:
        top = sorted(suspicious.items(), key=lambda kv: -kv[1])[:8]
        issues.append(PatchIssue(
            "warn",
            "补丁引用了索引中不存在的名字，可能是模型编造的："
            + "、".join(f"{w}({n}次)" for w, n in top),
            where=f.rel,
            hint="请对照原文确认这些名字是否真的存在。"
                 "编造的引用不会让游戏报错，但那行判断永远为假，"
                 "表现为「补丁装了没效果」。"
                 "若这些是原版游戏自带的对象，可以忽略本条。"))


def verify_patch_text(text: str, *, indexes: Any = None,
                      conflict_target: str = "",
                      original_definitions: set[str] | None = None,
                      rel: str = "", expect_files: bool = False
                      ) -> tuple[bool, list[PatchIssue], list[PatchFile]]:
    """对一段补丁文本做校验。

    支持两种输入：

    * **带围栏的模型输出** —— 走 :func:`parse_ai_response` 拆分。
      若围栏里没有内容，直接判为不通过（不能因为解析不出就把整段
      含围栏的文本当成一个裸文件塞进去）。
    * **玩家手写的裸脚本** —— 传 ``rel`` 指定落点，或让本函数按内容推断。

    这两种模式必须区分：前者是「模型给的完整回答」，后者是「编辑器里
    的一段代码」。混在一起会导致空补丁被误判为合法。
    """
    fenced = "```" in text
    if fenced:
        files, _expl, _risk = parse_ai_response(text)
        if not files:
            return False, [PatchIssue(
                "error", "没有解析出任何有效补丁文件",
                hint="代码块为空，或没有写文件路径。"
                     "请确认补丁内容非空，且用 ```path: xxx/yyy.txt 标明落点。")], []
    else:
        if not text.strip():
            return False, [PatchIssue("error", "补丁内容为空")], []
        target = rel or _guess_path(text)
        if not target:
            return False, [PatchIssue(
                "error", "无法判断这份补丁应该放在哪个目录",
                hint="请在编辑器里选择补丁类型（触发器/变量/部件/本地化…），"
                     "或在开头写一行注释说明。")], []
        files = [PatchFile(rel=target, text=text)]

    ok, issues = verify_patch(files, indexes=indexes,
                              conflict_target=conflict_target,
                              original_definitions=original_definitions)
    return ok, issues, files


# ---------------------------------------------------------------------------
# 顶层：生成
# ---------------------------------------------------------------------------

def generate_patch_with_ai(ctx: Any, *, cfg: dict, goal: str = "merge",
                           user_hint: str = "", indexes: Any = None,
                           on_delta: Callable[[str], None] | None = None,
                           should_stop: Callable[[], bool] | None = None,
                           max_rounds: int = 3,
                           learn_dir: Any = None,
                           on_round: Callable[[int, str], None] | None = None
                           ) -> AiPatchOutcome:
    """调用 AI 生成补丁，**失败会带着错误信息重试**。

    ``max_rounds`` —— 最多试几轮。1 = 旧行为（一次就结束）。
    ``learn_dir``  —— 记错迭代的数据目录；给了就会读历史教训注入提示词、
                      并把这次的阻塞问题记下来。``None`` 表示不学习。
    ``on_round``   —— 每轮开始前回调 ``(轮次, 说明)``，供界面显示进度。

    为什么要循环：旧流程校验没过就直接返回，那个错误**从来没告诉过 AI** ——
    于是它每次都犯同一个错，用户看到「AI 生成补丁」反复产出同样的坏结果。
    """
    import time

    from .client import AIError, build_client
    from .prompt import build_patch_prompt, estimate_tokens

    out = AiPatchOutcome(context=ctx.to_dict() if hasattr(ctx, "to_dict") else {})
    t0 = time.time()

    try:
        client = build_client(cfg)
    except AIError as exc:
        out.error = str(exc)
        return out

    out.provider = client.spec.name
    out.model = client.model

    try:
        messages = build_patch_prompt(ctx, user_hint=user_hint, goal=goal)
    except Exception as exc:
        out.error = f"构建提示词失败：{exc}"
        return out

    # 把历史高频错误注入系统提示词（记错迭代的「用」这一侧）
    lessons: dict = {}
    if learn_dir is not None:
        try:
            from . import lessons as _L
            lessons = _L.load(learn_dir)
            extra = _L.lesson_prompt(lessons)
            if extra and messages and messages[0].get("role") == "system":
                messages[0]["content"] = messages[0]["content"] + "\n" + extra
                out.context["lessons_injected"] = len(lessons)
        except Exception:
            pass

    out.tokens_estimate = estimate_tokens(messages)
    rounds = max(1, int(max_rounds))
    last_issues: list = []

    for rnd in range(rounds):
        if should_stop and should_stop():
            out.error = "已取消"
            return out
        if on_round:
            try:
                on_round(rnd + 1, "重新生成（带着上一版的错误）" if rnd else "生成中")
            except Exception:
                pass

        try:
            raw = client.chat(messages, stream=bool(on_delta) and rnd == 0,
                              on_delta=on_delta)
        except AIError as exc:
            out.error = str(exc)
            out.elapsed = time.time() - t0
            return out
        except Exception as exc:
            out.error = f"调用 AI 时发生未预期错误：{exc!r}"
            out.elapsed = time.time() - t0
            return out

        if should_stop and should_stop():
            out.error = "已取消"
            return out

        files, explanation, risk = parse_ai_response(raw)
        out.raw = raw
        out.files = files
        out.explanation = explanation
        out.risk = risk

        # 收集冲突原本涉及的定义名，用于「越权」检查
        orig: set[str] = set()
        for d in (getattr(ctx, "definitions", None) or []):
            if d.get("name"):
                orig.add(str(d["name"]))
        if getattr(ctx, "target", ""):
            orig.add(str(ctx.target))

        ok, issues = verify_patch(
            files, indexes=indexes,
            conflict_target=getattr(ctx, "target", ""),
            original_definitions=orig,
        )
        out.issues = issues
        out.verified = ok and bool(files)
        out.ok = out.verified
        out.rounds_used = rnd + 1

        if out.verified:
            break

        last_issues = issues
        # 记错：把这次的阻塞问题归类存起来（记错迭代的「学」这一侧）
        if learn_dir is not None:
            try:
                from . import lessons as _L
                _L.record(issues, learn_dir)
            except Exception:
                pass

        if rnd == rounds - 1:
            break          # 最后一轮，不再喂反馈

        # **把错误喂回去**。这里没有反馈就没有第二轮的意义 ——
        # 模型看不到自己错在哪，只会换个姿势再错一遍。
        try:
            from . import lessons as _L
            fb = _L.feedback_prompt(issues)
        except Exception:
            fb = ""
        if not fb:
            break          # 没有阻塞问题可反馈（多半是解析失败），再试也没用
        messages = messages + [
            {"role": "assistant", "content": raw},
            {"role": "user", "content": fb},
        ]

    out.elapsed = time.time() - t0
    if last_issues and not out.verified:
        out.context["last_issue_count"] = len(last_issues)

    # 置信度：从模型输出里读，读不到就按校验结果推断
    m = re.search(r"置信度[：:]\s*(高|中|低)", out.risk or out.explanation or "")
    if m:
        out.confidence = {"高": "high", "中": "medium", "低": "low"}[m.group(1)]
    elif out.verified:
        out.confidence = "medium"
    else:
        out.confidence = "low"

    return out
