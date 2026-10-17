"""界面主题引擎。

设计目标
--------
1. **令牌驱动** —— 界面所有颜色/圆角/描边都由一组命名令牌决定，
   CSS 里不写死任何颜色。这样换主题只需换一份令牌。
2. **玩家可制作、可导入** —— 主题就是一份 JSON；界面上可以直接改、
   导出分享、导入别人的。导入时严格校验，坏文件不会把界面搞崩。
3. **内置几套成品** —— 其中「群星」主题刻意还原游戏的视觉语言：
   深空蓝底、青金描边、切角嵌板、细密金属纹理。

令牌命名
--------
``bg`` ``panel`` ``line`` ``fg`` ``acc`` 等基础色，
外加 ``radius`` ``panel_clip`` ``glow`` ``font`` 等形态令牌。

主题文件结构::

    {
      "id": "my_theme",
      "name": "我的主题",
      "base": "dark",              # 可选的继承基座
      "tokens": { "bg": "#0a0e1a", ... },
      "flags":  { "clip": true, "texture": "grid" }
    }
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from .config import app_data_dir
from .paths import is_dir, is_file, long_path, read_text, write_text

THEMES_DIR_NAME = "themes"

# 颜色令牌允许的格式：
#   #rgb / #rrggbb / #rrggbbaa  —— 主要形式
#   rgba(r,g,b,a) / rgb(r,g,b)  —— 阴影与遮罩需要透明度，必须放行
#   transparent                 —— 显式无色
_COLOR_RE = re.compile(
    r"^(?:"
    r"#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})"
    r"|rgba?\(\s*\d{1,3}\s*,\s*\d{1,3}\s*,\s*\d{1,3}"
    r"(?:\s*,\s*(?:0|1|0?\.\d+|\d{1,3}%))?\s*\)"
    r"|transparent"
    r")$"
)
# 允许的尺寸/数值令牌格式
_SIZE_RE = re.compile(r"^-?\d+(?:\.\d+)?(?:px|rem|em|%|vh|vw)?$")

# ---------------------------------------------------------------------------
# 令牌定义（顺序即界面上的展示顺序）
# ---------------------------------------------------------------------------

# 颜色类令牌：值必须是颜色
COLOR_TOKENS: dict[str, str] = {
    # 背景层次（由深到浅）
    "bg": "最底层背景",
    "bg2": "次层背景",
    "panel": "面板背景",
    "panel2": "面板次级背景",
    "panel3": "面板高亮背景",
    # 线条
    "line": "主分割线",
    "line2": "次分割线（更亮）",
    "lineglow": "描边高光",
    # 文字
    "fg": "主文字",
    "dim": "次要文字",
    "dim2": "最弱文字",
    # 强调色
    "acc": "主强调色",
    "acc2": "次强调色",
    "acc3": "第三强调色",
    "acctext": "强调色上的文字",
    # 语义色
    "crit": "致命/危险",
    "high": "严重/警告",
    "med": "中等/注意",
    "low": "轻微/信息",
    "info": "中性提示",
    "ok": "成功/正常",
    # 语义色上的文字（保证对比度）
    "oncrit": "致命色上的文字",
    "onhigh": "严重色上的文字",
    "onmed": "中色上的文字",
    "onlow": "轻微色上的文字",
    "onok": "成功色上的文字",
    # 装饰
    "accentline": "标题下划线",
    "shadow": "投影颜色",
    "overlay": "遮罩层颜色",
}

# 形态类令牌：值可以是 px 数值、布尔或枚举
SHAPE_TOKENS: dict[str, dict[str, Any]] = {
    "radius": {"desc": "面板圆角（px）", "type": "size", "default": "11px"},
    "radius_sm": {"desc": "小控件圆角（px）", "type": "size", "default": "8px"},
    "border_w": {"desc": "描边粗细（px）", "type": "size", "default": "1px"},
    "clip": {"desc": "切角嵌板（群星风格）", "type": "bool", "default": False},
    "clip_size": {"desc": "切角大小（px）", "type": "size", "default": "10px"},
    "texture": {"desc": "背景纹理", "type": "enum",
                "values": ["none", "stars", "grid", "carbon", "hex"],
                "default": "stars"},
    "glow": {"desc": "描边发光强度（0-1）", "type": "number", "default": 0.0},
    "uppercase_title": {"desc": "标题转大写", "type": "bool", "default": False},
    "font": {"desc": "字体族", "type": "text",
             "default": '-apple-system,BlinkMacSystemFont,"Segoe UI",'
                        '"Microsoft YaHei UI","Microsoft YaHei",sans-serif'},
    "font_title": {"desc": "标题字体族", "type": "text", "default": ""},
    "font_mono": {"desc": "等宽字体族", "type": "text",
                  "default": 'ui-monospace,Consolas,"Cascadia Mono",monospace'},
    "texture_op": {"desc": "背景纹理强度（0-1）", "type": "number",
                   "default": 0.5},
    "sidebar_w": {"desc": "侧栏宽度（px）", "type": "size", "default": "216px"},
    "density": {"desc": "行高密度", "type": "enum",
                "values": ["compact", "normal", "relaxed"],
                "default": "normal"},
    # 浏览器自绘部件（右键菜单、滚动条、选择框的系统部件）按哪种明暗渲染。
    # 不给它正确值的话，深色主题下这些部件会按浅色画，看起来就是一块块白框。
    "scheme": {"desc": "浏览器部件明暗", "type": "enum",
               "values": ["dark", "light"], "default": "dark"},
}

ALL_TOKEN_KEYS = tuple(COLOR_TOKENS) + tuple(SHAPE_TOKENS)


# ---------------------------------------------------------------------------
# 内置主题
# ---------------------------------------------------------------------------

def _t(**kw: Any) -> dict[str, Any]:
    return dict(kw)


BUILTIN: dict[str, dict[str, Any]] = {
    # ---------------------------------------------------------------
    # 群星：还原游戏的视觉语言
    # ---------------------------------------------------------------
    "stellaris": {
        "name": "群星（默认）",
        "desc": "还原《群星》本体界面：深空蓝底、青金描边、切角嵌板、"
                "细密星图纹理。与游戏本体视觉统一。",
        "author": "内置",
        "tokens": _t(
            bg="#05080f", bg2="#080d18", panel="#0b1220",
            panel2="#0f1828", panel3="#152238",
            line="#1e2f4a", line2="#2c4468", lineglow="#4a7fb5",
            fg="#d8e6f5", dim="#8fa6c4", dim2="#5d7a99",
            acc="#4a9eda", acc2="#5fd3c0", acc3="#c9a961",
            acctext="#04121e",
            crit="#e0505f", high="#e08a3c", med="#d9b64a",
            low="#4a9eda", info="#7d90ad", ok="#4fc79a",
            oncrit="#1a0508", onhigh="#1a0f04", onmed="#1a1405",
            onlow="#04121e", onok="#04140e",
            accentline="#c9a961", shadow="rgba(0,0,0,.7)",
            overlay="rgba(2,5,10,.82)",
        ),
        "flags": _t(clip=True, clip_size="11px", radius="3px",
                    radius_sm="2px", border_w="1px", glow=0.5,
                    texture="grid", uppercase_title=False,
                    sidebar_w="222px", density="normal",
                    font='"Segoe UI","Microsoft YaHei UI","Microsoft YaHei",'
                         'sans-serif',
                    font_title='"Palatino Linotype","Book Antiqua",'
                               '"Microsoft YaHei UI",serif'),
    },
    # ---------------------------------------------------------------
    # 深空（原本的暗色，作为基座）
    # ---------------------------------------------------------------
    "dark": {
        "name": "深空（原版暗色）",
        "desc": "本工具最初的配色：低饱和深色，长时间盯屏幕不累。",
        "author": "内置",
        "tokens": _t(
            bg="#080b12", bg2="#0d1220", panel="#111827",
            panel2="#161f33", panel3="#1c2740",
            line="#243049", line2="#2f3d5c", lineglow="#3d4d70",
            fg="#e8eefc", dim="#8b9ab8", dim2="#66748f",
            acc="#7c6cff", acc2="#4dd4ac", acc3="#4da6ff",
            acctext="#ffffff",
            crit="#ff5d6c", high="#ff9f43", med="#ffd93d",
            low="#4da6ff", info="#8b9ab8", ok="#4dd4ac",
            oncrit="#ffffff", onhigh="#1a1005", onmed="#1a1605",
            onlow="#04141f", onok="#04140f",
            accentline="#7c6cff", shadow="rgba(0,0,0,.55)",
            overlay="rgba(4,7,13,.76)",
        ),
        "flags": _t(clip=False, radius="11px", radius_sm="8px",
                    border_w="1px", glow=0.0, texture="stars",
                    sidebar_w="216px", density="normal"),
    },
    # ---------------------------------------------------------------
    # 星海黎明：浅色
    # ---------------------------------------------------------------
    "daylight": {
        "name": "星海黎明（浅色）",
        "desc": "明亮配色，适合白天或投影环境。",
        "author": "内置",
        "tokens": _t(
            bg="#eef2f8", bg2="#e4eaf4", panel="#ffffff",
            panel2="#f5f8fd", panel3="#e8eef8",
            line="#ccd8e8", line2="#b3c3da", lineglow="#7aabdd",
            fg="#16233a", dim="#5d7292", dim2="#8a9db8",
            acc="#2f6fb5", acc2="#12897a", acc3="#a8761a",
            acctext="#ffffff",
            crit="#c62f42", high="#c26a12", med="#a8860f",
            low="#2f6fb5", info="#68789a", ok="#1b8a63",
            oncrit="#ffffff", onhigh="#ffffff", onmed="#ffffff",
            onlow="#ffffff", onok="#ffffff",
            accentline="#2f6fb5", shadow="rgba(30,50,80,.16)",
            overlay="rgba(240,244,250,.82)",
        ),
        "flags": _t(clip=False, radius="10px", radius_sm="7px",
                    border_w="1px", glow=0.0, texture="none",
                    sidebar_w="216px", density="normal", scheme="light"),
    },
    # ---------------------------------------------------------------
    # 高对比：可读性优先
    # ---------------------------------------------------------------
    "highcontrast": {
        "name": "高对比（无障碍）",
        "desc": "纯黑底 + 纯白字 + 高饱和语义色，色弱或小屏友好。",
        "author": "内置",
        "tokens": _t(
            bg="#000000", bg2="#0a0a0a", panel="#111111",
            panel2="#1a1a1a", panel3="#262626",
            line="#4d4d4d", line2="#7a7a7a", lineglow="#ffffff",
            fg="#ffffff", dim="#d0d0d0", dim2="#a0a0a0",
            acc="#4db8ff", acc2="#4dffb8", acc3="#ffd24d",
            acctext="#000000",
            crit="#ff4d4d", high="#ff9d33", med="#ffee33",
            low="#4db8ff", info="#c0c0c0", ok="#33e08a",
            oncrit="#000000", onhigh="#000000", onmed="#000000",
            onlow="#000000", onok="#000000",
            accentline="#ffd24d", shadow="rgba(0,0,0,.9)",
            overlay="rgba(0,0,0,.9)",
        ),
        "flags": _t(clip=False, radius="4px", radius_sm="3px",
                    border_w="2px", glow=0.0, texture="none",
                    sidebar_w="220px", density="relaxed"),
    },
    # ---------------------------------------------------------------
    # 虚空紫：赛博
    # ---------------------------------------------------------------
    "void": {
        "name": "虚空紫（赛博）",
        "desc": "紫青霓虹、发光描边、碳纤维纹理。",
        "author": "内置",
        "tokens": _t(
            bg="#07060f", bg2="#0c0a1a", panel="#120e24",
            panel2="#1a1433", panel3="#241c47",
            line="#2e2456", line2="#453a7a", lineglow="#8b6cff",
            fg="#ece7ff", dim="#a396d4", dim2="#6f65a0",
            acc="#a06cff", acc2="#3ce8d8", acc3="#ff6cc4",
            acctext="#0b0518",
            crit="#ff4d7d", high="#ff9d3c", med="#ffe23c",
            low="#3cc8ff", info="#a396d4", ok="#3ce8c0",
            oncrit="#1a0410", onhigh="#1a0f04", onmed="#1a1704",
            onlow="#04141a", onok="#04140f",
            accentline="#a06cff", shadow="rgba(0,0,0,.75)",
            overlay="rgba(5,3,12,.85)",
        ),
        "flags": _t(clip=True, clip_size="9px", radius="6px",
                    radius_sm="4px", border_w="1px", glow=0.9,
                    texture="carbon", sidebar_w="218px",
                    density="normal"),
    },
}

DEFAULT_THEME = "stellaris"


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------

@dataclass
class ThemeIssue:
    """主题校验发现的问题。"""

    level: str      # error | warn
    message: str
    field: str = ""

    def to_dict(self) -> dict:
        """序列化，供界面展示校验问题。"""
        return {"level": self.level, "message": self.message, "field": self.field}


def _valid_color(v: Any) -> bool:
    return isinstance(v, str) and bool(_COLOR_RE.match(v.strip()))


def _valid_size(v: Any) -> bool:
    if isinstance(v, (int, float)):
        return True
    return isinstance(v, str) and bool(_SIZE_RE.match(v.strip()))


# ---------------------------------------------------------------------------
# 对比度（WCAG 2.1）
# ---------------------------------------------------------------------------

def _srgb_to_linear(c: float) -> float:
    """sRGB 分量 → 线性光。对比度必须在线性空间算，直接拿 0~255 算会偏。"""
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def rel_luminance(color: str) -> float:
    """相对亮度，0（黑）~1（白）。"""
    h = color.strip().lstrip("#")
    if len(h) != 6:
        raise ValueError(f"不是 #rrggbb：{color!r}")
    r, g, b = (int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
    return (0.2126 * _srgb_to_linear(r)
            + 0.7152 * _srgb_to_linear(g)
            + 0.0722 * _srgb_to_linear(b))


def contrast_ratio(fg: str, bg: str) -> float:
    """两色的对比度，1.0（相同）~21.0（黑白）。"""
    a, b = rel_luminance(fg), rel_luminance(bg)
    hi, lo = (a, b) if a >= b else (b, a)
    return (hi + 0.05) / (lo + 0.05)


def _hex_channels(color: str) -> tuple[int, int, int]:
    h = color.strip().lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def _mix(a: str, b: str, t: float) -> str:
    """把 a 往 b 混 t（0~1），返回 #rrggbb。"""
    ca, cb = _hex_channels(a), _hex_channels(b)
    mixed = [round(x + (y - x) * t) for x, y in zip(ca, cb)]
    return "#" + "".join(f"{max(0, min(255, v)):02x}" for v in mixed)


def ensure_contrast(fg: str, bg: str, target: float,
                    *, steps: int = 24) -> tuple[str, bool]:
    """把 ``fg`` 往"更亮"或"更暗"推，直到与 ``bg`` 的对比度达标。

    往哪个方向推由背景决定：背景暗就提亮前景，背景亮就压暗前景 ——
    这样不会出现"为了让文字更明显，把深色主题的文字也变黑"。

    返回 ``(调整后的颜色, 是否改动过)``。
    推不到目标就返回能到的最好结果（``steps`` 上限），**不报错**：
    宁可给出一个尽力而为的主题，也不要让用户对着"生成失败"重试。
    """
    if not _valid_color(fg) or not _valid_color(bg):
        return fg, False
    if contrast_ratio(fg, bg) >= target:
        return fg, False
    # 背景偏暗 → 前景提亮；背景偏亮 → 前景压暗
    toward = "#ffffff" if rel_luminance(bg) < 0.5 else "#000000"
    best, best_ratio = fg, contrast_ratio(fg, bg)
    for i in range(1, steps + 1):
        cand = _mix(fg, toward, i / steps)
        r = contrast_ratio(cand, bg)
        if r > best_ratio:
            best, best_ratio = cand, r
        if r >= target:
            return cand, True
    return best, best != fg


# 界面里真正会"叠在一起看"的前景/背景配对。
# 只校验这些 —— 不是所有组合都会同时出现，全查会误伤。
CONTRAST_RULES: tuple[tuple[str, str, float, str], ...] = (
    ("fg", "bg", 4.5, "正文"),
    ("fg", "panel", 4.5, "正文在面板上"),
    ("dim", "bg", 3.0, "次要文字"),
    ("dim2", "bg", 3.0, "更弱的提示"),
    ("dim", "panel", 3.0, "次要文字在面板上"),
    ("acctext", "acc", 4.5, "按钮上的文字"),
    ("acc", "bg", 3.0, "强调色"),
    ("ok", "bg", 3.0, "成功色"),
    ("warn", "bg", 3.0, "警告色"),
    ("high", "bg", 3.0, "高优先色"),
    ("crit", "bg", 3.0, "严重色"),
    ("info", "bg", 3.0, "信息色"),
)


def check_theme_contrast(tokens: dict[str, Any]) -> list[tuple[str, str,
                                                              float, float]]:
    """检查主题里所有关键配对的对比度。

    返回 ``[(前景键, 背景键, 实际, 要求), …]``，只列**不达标**的。
    """
    bad: list[tuple[str, str, float, float]] = []
    for fg_k, bg_k, need, _label in CONTRAST_RULES:
        fg, bg = tokens.get(fg_k), tokens.get(bg_k)
        if not _valid_color(fg) or not _valid_color(bg):
            continue
        try:
            got = contrast_ratio(str(fg), str(bg))
        except ValueError:
            continue
        if got < need:
            bad.append((fg_k, bg_k, got, need))
    return bad


def auto_fix_contrast(tokens: dict[str, Any]
                      ) -> tuple[dict[str, Any], list[str]]:
    """把不达标的配对自动修到达标，返回 ``(新令牌, 改了哪些键)``。

    修的是**前景**而不是背景：背景一动，整片区域的观感就变了；
    前景微调（提亮/压暗）对整体配色性格的破坏小得多。

    顺序有讲究 —— 先修 ``fg`` 这类主文字，再修 ``dim`` 这类次级文字。
    因为 ``dim`` 的调整可能依赖 ``fg`` 已经定好的明暗方向，
    反过来先调 ``dim`` 会出现"两套文字亮度撞在一起"。

    **要迭代**：一轮修完之后，某个**背景**键（如 ``acc``）可能也被改过，
    于是"前景 vs 背景"的配对关系就变了 —— 刚才修好的 ``acctext`` 又不达标。
    实测踩过：``acctext`` 先修、``acc`` 后修，结果 ``acctext/acc`` 从 1.09
    修到 1.81 就卡住了，还是不能看。
    所以这里跑到收敛为止（上限 4 轮，防止两个键互相追尾死循环）。
    """
    out = dict(tokens)
    changed: list[str] = []
    # 按"重要性"排：主文字先修
    order = ["fg", "acctext", "dim", "dim2", "acc", "ok", "warn", "high",
             "crit", "info"]
    rules = sorted(CONTRAST_RULES, key=lambda r: order.index(r[0])
                   if r[0] in order else 99)
    for _round in range(4):
        moved = False
        for fg_k, bg_k, need, _label in rules:
            fg, bg = out.get(fg_k), out.get(bg_k)
            if not isinstance(fg, str) or not isinstance(bg, str):
                continue
            fixed, did = ensure_contrast(fg, bg, need)
            if did:
                out[fg_k] = fixed
                moved = True
                if fg_k not in changed:
                    changed.append(fg_k)
        if not moved:
            break
    return out, changed


def validate_theme(raw: Any) -> tuple[dict[str, Any] | None, list[ThemeIssue]]:
    """校验并规范化一份主题定义。

    返回 ``(规范化后的主题 或 None, 问题列表)``。
    只接受已知令牌；未知键会被忽略并记一条 warn——
    这样别人手写的主题即使多了几个键也能用，但不会往 CSS 里注入任意内容。
    """
    issues: list[ThemeIssue] = []
    if not isinstance(raw, dict):
        return None, [ThemeIssue("error", "主题文件必须是一个 JSON 对象")]

    tokens_in = raw.get("tokens")
    flags_in = raw.get("flags")
    if tokens_in is None and flags_in is None:
        # 允许直接把令牌写在顶层（更宽松的手写格式）
        tokens_in = {k: v for k, v in raw.items() if k in COLOR_TOKENS}
        flags_in = {k: v for k, v in raw.items() if k in SHAPE_TOKENS}
        if not tokens_in and not flags_in:
            return None, [ThemeIssue(
                "error", "没有找到任何已知的令牌（tokens / flags 都为空）")]

    tokens: dict[str, str] = {}
    flags: dict[str, Any] = {}

    for k, v in (tokens_in or {}).items():
        if k not in COLOR_TOKENS:
            issues.append(ThemeIssue("warn", f"未知的颜色令牌，已忽略：{k}", k))
            continue
        if not _valid_color(v):
            issues.append(ThemeIssue(
                "error", f"「{k}」不是合法颜色（需要 #rgb/#rrggbb/#rrggbbaa）："
                         f"{v!r}", k))
            continue
        tokens[k] = str(v).strip()

    for k, v in (flags_in or {}).items():
        if k not in SHAPE_TOKENS:
            issues.append(ThemeIssue("warn", f"未知的形态令牌，已忽略：{k}", k))
            continue
        spec = SHAPE_TOKENS[k]
        t = spec["type"]
        if t == "bool":
            flags[k] = bool(v)
        elif t == "size":
            if not _valid_size(v):
                issues.append(ThemeIssue("error", f"「{k}」不是合法尺寸：{v!r}", k))
                continue
            flags[k] = (f"{v}px" if isinstance(v, (int, float)) else str(v))
        elif t == "number":
            try:
                flags[k] = max(0.0, min(1.0, float(v)))
            except (TypeError, ValueError):
                issues.append(ThemeIssue("error", f"「{k}」不是数字：{v!r}", k))
        elif t == "enum":
            if v not in spec["values"]:
                issues.append(ThemeIssue(
                    "error", f"「{k}」只能是 {'/'.join(spec['values'])}，"
                             f"收到 {v!r}", k))
                continue
            flags[k] = v
        elif t == "text":
            flags[k] = str(v)[:300]

    # 缺失的令牌用基座或默认主题补齐，保证界面不会出现「无颜色」的洞
    base_id = str(raw.get("base") or "").strip()
    base = None
    if base_id:
        base = get_builtin(base_id) or load_user_theme(base_id)
        if base is None:
            issues.append(ThemeIssue("warn", f"基座主题不存在：{base_id}"))
    if base is None:
        base = BUILTIN[DEFAULT_THEME]

    merged_tokens = dict(base["tokens"])
    merged_tokens.update(tokens)
    merged_flags = dict(base["flags"])
    merged_flags.update(flags)

    missing = [k for k in COLOR_TOKENS if k not in merged_tokens]
    if missing:
        issues.append(ThemeIssue("warn",
                                 f"这些颜色令牌没有值，已用默认值补齐："
                                 f"{'、'.join(missing[:8])}"))
        for k in missing:
            merged_tokens[k] = BUILTIN[DEFAULT_THEME]["tokens"][k]

    tid = str(raw.get("id") or "").strip()
    if not tid or not re.match(r"^[A-Za-z0-9_\u4e00-\u9fff-]{1,48}$", tid):
        tid = "user_" + datetime.now().strftime("%Y%m%d_%H%M%S")
        issues.append(ThemeIssue("warn", f"主题 id 不合法或缺失，已自动改为 {tid}"))

    theme = {
        "id": tid,
        "name": str(raw.get("name") or tid)[:60],
        "desc": str(raw.get("desc") or "")[:300],
        "author": str(raw.get("author") or "")[:60],
        "base": base_id if base_id and base_id in {**BUILTIN} else "",
        "tokens": merged_tokens,
        "flags": merged_flags,
        "builtin": False,
    }
    has_error = any(i.level == "error" for i in issues)
    return (None if has_error else theme), issues


# ---------------------------------------------------------------------------
# 存取
# ---------------------------------------------------------------------------

def themes_dir() -> Path:
    """用户主题目录（``%LOCALAPPDATA%/StellarisModDoctor/themes``）。"""
    d = app_data_dir() / THEMES_DIR_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_builtin(tid: str) -> dict[str, Any] | None:
    """取一份内置主题的副本；``tid`` 不存在时返回 ``None``。"""
    b = BUILTIN.get(tid)
    if b is None:
        return None
    return {
        "id": tid,
        "name": b["name"],
        "desc": b.get("desc", ""),
        "author": b.get("author", "内置"),
        "base": "",
        "tokens": dict(b["tokens"]),
        "flags": dict(b["flags"]),
        "builtin": True,
    }


def list_themes() -> list[dict[str, Any]]:
    """列出全部主题（内置在前，用户主题在后）。"""
    out = [get_builtin(k) for k in BUILTIN]
    d = themes_dir()
    try:
        for f in sorted(d.glob("*.json")):
            try:
                raw = json.loads(read_text(f))
            except (OSError, ValueError):
                continue
            theme, _issues = validate_theme(raw)
            if theme is None:
                continue
            theme["file"] = str(f)
            out.append(theme)
    except OSError:
        pass
    return [t for t in out if t]


def load_user_theme(tid: str) -> dict[str, Any] | None:
    """读取一份用户主题；文件不存在或校验失败时返回 ``None``。"""
    p = themes_dir() / f"{tid}.json"
    if not is_file(p):
        return None
    try:
        raw = json.loads(read_text(p))
    except (OSError, ValueError):
        return None
    theme, _ = validate_theme(raw)
    if theme:
        theme["file"] = str(p)
    return theme


def get_theme(tid: str) -> dict[str, Any]:
    """取主题；找不到时回落到默认主题。"""
    return (get_builtin(tid) or load_user_theme(tid)
            or get_builtin(DEFAULT_THEME))


def _write_json(path: Path, payload: dict) -> None:
    """写 JSON 文件，**不带 BOM**。

    主题文件是要拿去分享、用别的编辑器改的。BOM 会让不少 JSON 解析器
    直接报错（Python 的 ``json.loads`` 会抛 ``Unexpected UTF-8 BOM``）。
    游戏脚本那边相反——群星喜欢 BOM——所以这里不复用路径模块的
    :func:`smd.paths.write_text`。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                    encoding="utf-8", newline="\n")


def save_user_theme(theme: dict[str, Any]) -> dict[str, Any]:
    """保存（或覆盖）一份用户主题。返回保存结果。"""
    norm, issues = validate_theme(theme)
    if norm is None:
        return {"ok": False,
                "error": "主题校验未通过",
                "issues": [i.to_dict() for i in issues]}
    if norm["id"] in BUILTIN:
        return {"ok": False, "error": f"「{norm['id']}」是内置主题的 id，"
                                      f"请换一个名字"}
    p = themes_dir() / f"{norm['id']}.json"
    payload = {
        "id": norm["id"], "name": norm["name"], "desc": norm["desc"],
        "author": norm["author"], "base": norm.get("base", ""),
        "tokens": norm["tokens"], "flags": norm["flags"],
    }
    try:
        _write_json(p, payload)
    except OSError as exc:
        return {"ok": False, "error": f"写入失败：{exc}"}
    norm["file"] = str(p)
    return {"ok": True, "theme": norm,
            "issues": [i.to_dict() for i in issues]}


def delete_user_theme(tid: str) -> dict[str, Any]:
    """删除一份用户主题；内置主题会被拒绝。"""
    if tid in BUILTIN:
        return {"ok": False, "error": "内置主题不能删除"}
    p = themes_dir() / f"{tid}.json"
    if not is_file(p):
        return {"ok": False, "error": "主题不存在"}
    try:
        p.unlink()
    except OSError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True}


def export_theme(tid: str, out_path: str = "") -> dict[str, Any]:
    """把主题导出成 JSON 文件（便于分享）。"""
    theme = get_builtin(tid) or load_user_theme(tid)
    if theme is None:
        return {"ok": False, "error": "主题不存在"}
    if not out_path:
        d = app_data_dir() / "exports"
        d.mkdir(parents=True, exist_ok=True)
        out_path = str(d / f"theme_{tid}.json")
    payload = {
        "id": theme["id"], "name": theme["name"],
        "desc": theme.get("desc", ""), "author": theme.get("author", ""),
        "base": theme.get("base", ""),
        "tokens": theme["tokens"], "flags": theme["flags"],
    }
    try:
        _write_json(Path(out_path), payload)
    except OSError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "path": out_path}


def import_theme(path: str, *, rename: str = "") -> dict[str, Any]:
    """从 JSON 文件导入主题。"""
    p = Path(path)
    if not is_file(p):
        return {"ok": False, "error": f"文件不存在：{path}"}
    try:
        raw = json.loads(read_text(p))
    except (OSError, ValueError) as exc:
        return {"ok": False, "error": f"不是合法的 JSON：{exc}"}
    if isinstance(raw, list):
        return {"ok": False, "error": "看起来不是主题文件（顶层是数组）"}
    norm, issues = validate_theme(raw)
    if norm is None:
        return {"ok": False, "error": "主题校验未通过",
                "issues": [i.to_dict() for i in issues]}
    if rename:
        norm["name"] = rename[:60]
    # id 撞车就加后缀，避免覆盖用户已有的主题
    base_id = norm["id"]
    n = 1
    while norm["id"] in BUILTIN or \
            is_file(themes_dir() / f"{norm['id']}.json"):
        n += 1
        norm["id"] = f"{base_id}_{n}"
    res = save_user_theme(norm)
    if res.get("ok"):
        res["issues"] = [i.to_dict() for i in issues]
        res["renamed"] = norm["id"] != base_id
    return res


def reset_builtin_files() -> int:
    """把内置主题写一份到用户主题目录，方便玩家照着改。

    返回写出的文件数。已存在的不覆盖。
    """
    d = themes_dir()
    n = 0
    for tid in BUILTIN:
        p = d / f"_内置_{tid}.json"
        if is_file(p):
            continue
        theme = get_builtin(tid)
        if not theme:
            continue
        theme["id"] = f"copy_of_{tid}"
        theme["name"] = f"{theme['name']}（副本）"
        theme["author"] = "由内置主题导出"
        try:
            _write_json(p, {k: theme[k] for k in
                            ("id", "name", "desc", "author", "base",
                             "tokens", "flags")})
            n += 1
        except OSError:
            continue
    return n


def snapshot_dir() -> Path:
    """主题快照目录（保留给「导入前自动备份」用）。"""
    d = app_data_dir() / "theme_snapshots"
    d.mkdir(parents=True, exist_ok=True)
    return d


def theme_for_ui(tid: str) -> dict[str, Any]:
    """给界面用的主题数据（含令牌说明，便于做编辑器）。"""
    theme = get_theme(tid)
    return {
        "theme": theme,
        "color_tokens": [{"key": k, "desc": v}
                         for k, v in COLOR_TOKENS.items()],
        "shape_tokens": [{"key": k, **v} for k, v in SHAPE_TOKENS.items()],
        "builtin_ids": list(BUILTIN.keys()),
        "default": DEFAULT_THEME,
    }
