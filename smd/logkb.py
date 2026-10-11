"""日志知识库：把群星的英文报错翻译成中文并加上注释。

群星的 ``error.log`` 动辄几万行，99% 的内容对普通玩家毫无意义，
真正致命的那几行往往藏在几十万行噪音里。本模块做三件事：

1. **解析** —— 把 ``[时间][源文件:行号]: 消息`` 结构化
2. **翻译** —— 用知识库把英文报错翻成中文，并解释「这意味着什么」
3. **判定** —— 区分「可忽略的噪音」与「真正会导致崩溃的错误」

知识库条目来自对真实 error.log 的统计（本项目在 8.8 MB / 59602 行、
13420 种不同消息的真实日志上做过聚类），覆盖出现频率最高的那些模式。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Iterator

from .paths import read_text

# 严重度
SEV_FATAL = "fatal"       # 直接导致崩溃
SEV_ERROR = "error"       # 功能损坏
SEV_WARN = "warn"         # 潜在问题
SEV_NOISE = "noise"       # 无害噪音，可忽略

SEV_LABEL = {
    SEV_FATAL: "致命", SEV_ERROR: "错误", SEV_WARN: "警告", SEV_NOISE: "噪音",
}
SEV_RANK = {SEV_FATAL: 0, SEV_ERROR: 1, SEV_WARN: 2, SEV_NOISE: 3}


@dataclass
class KBEntry:
    """一条知识库规则。"""

    pattern: "re.Pattern[str]"
    severity: str
    title_zh: str          # 中文标题
    meaning_zh: str        # 这句话在说什么
    cause_zh: str          # 为什么会这样
    fix_zh: str            # 怎么办
    category: str = "通用"
    id: str = ""
    # 聚合键：``rule`` 按规则聚合；``file`` 表示还要按消息里的文件名
    # 再细分（例如「事件表损坏」有 47 个文件各报几十条，混在一起没法看）
    group_by: str = "rule"


@dataclass
class LogLine:
    """解析后的一行日志。"""

    raw: str
    time: str = ""
    source: str = ""
    line_no: str = ""
    message: str = ""
    entry: KBEntry | None = None
    severity: str = SEV_NOISE
    params: dict[str, str] = field(default_factory=dict)

    @property
    def severity_label(self) -> str:
        """严重度的中文名。"""
        return SEV_LABEL.get(self.severity, self.severity)

    @property
    def group_key(self) -> str:
        """用于聚合的分组键。"""
        if self.entry is None:
            return "raw"
        if self.entry.group_by == "file" and self.params.get("file"):
            # 文件名可能很长，去掉路径只留 basename 更易读
            f = self.params["file"].strip().replace("\\", "/")
            return f"{self.entry.id}::{f}"
        return self.entry.id

    def to_dict(self) -> dict:
        """序列化，含知识库注解内容。"""
        return {
            "time": self.time, "source": self.source, "src_line": self.line_no,
            "message": self.message, "severity": self.severity,
            "severity_label": self.severity_label,
            "title_zh": self.entry.title_zh if self.entry else "",
            "meaning_zh": self.entry.meaning_zh if self.entry else "",
            "cause_zh": self.entry.cause_zh if self.entry else "",
            "fix_zh": self.entry.fix_zh if self.entry else "",
            "category": self.entry.category if self.entry else "未分类",
            "raw": self.raw,
        }


def _e(id_: str, pattern: str, severity: str, title: str, meaning: str,
       cause: str, fix: str, category: str = "通用",
       group_by: str = "rule") -> KBEntry:
    return KBEntry(
        pattern=re.compile(pattern), severity=severity, title_zh=title,
        meaning_zh=meaning, cause_zh=cause, fix_zh=fix, category=category,
        id=id_, group_by=group_by,
    )


# ---------------------------------------------------------------------------
# 知识库
# ---------------------------------------------------------------------------

KB: list[KBEntry] = [
    # ===== 文件 / 模组加载 =====
    _e("file.mod_missing",
       r"Could not find files for mod:\s*(?P<path>.+)$",
       SEV_ERROR,
       "模组文件缺失",
       "游戏在启动时试图加载一个模组，但它的文件夹不存在。",
       "通常有三种原因：① 你在 playset 里勾选了这个模组，但它在 Steam 上已取消订阅或被卸载；"
       "② 模组作者删除了该工坊项目（下架）；③ 磁盘/杀毒软件误删了文件。",
       "打开启动器的模组列表，把报错中这个路径对应的模组取消勾选，或重新在创意工坊订阅。"
       "若反复出现，用本软件的「修复」功能清理幽灵条目。",
       "模组加载"),

    _e("file.supported_version",
       r"Invalid supported_version in\s+file:\s*(?P<file>.+?)\s+line:\s*(?P<line>\d+)",
       SEV_WARN,
       "supported_version 字段格式不合法",
       "模组的描述符里 `supported_version` 写得不规范，启动器无法解析它支持的版本。",
       "常见写法错误：写成 `4.4.6` 而不是 `v4.4.*`；用了中文引号；"
       "或者游戏大版本更新后旧格式失效。这本身不致命，但会让启动器把模组标成「不兼容」。",
       "一般可以忽略。如果启动器把它显示为不兼容，请更新该模组，"
       "或手动把 .mod 文件里的 supported_version 改成 `v4.*` 这种宽泛写法。",
       "模组加载"),

    _e("file.store_backend",
       r"Could not find item in store backend",
       SEV_NOISE,
       "商店后端查不到该项目",
       "启动器向 Paradox/Steam 商店查询某个模组的元数据时没查到。",
       "离线模式、或模组已从工坊下架时会这样。不影响游戏加载。",
       "可忽略。",
       "模组加载"),

    # ===== 数据库键引用（最高频噪音） =====
    _e("db.key_ref_missing",
       r"Failed to read key reference\s+(?P<key>\S+)\s+from database\s+file:\s*(?P<file>.+)$",
       SEV_WARN,
       "数据库键引用失败",
       "某个脚本引用了 `%(key)s`，但游戏数据库里根本没有这个东西。",
       "多半是模组引用了另一个模组的对象，但那个模组没启用/没安装；"
       "或者模组版本过旧，引用的对象在当前游戏版本里已被移除。"
       "极少情况下是原版游戏自身的历史遗留问题。",
       "若只是零星几条（<50 条）可忽略。成百上千条时，"
       "通常意味着 `%(file)s` 所属的模组缺少前置，请检查依赖。",
       "脚本引用"),

    _e("db.key_ref_deferred",
       r"Failed to deferred read key reference\s+(?P<key>\S+)\s+from database\s+file:\s*(?P<file>.+)$",
       SEV_WARN,
       "延迟数据库键引用失败",
       "同上，但发生在「延迟加载」阶段——游戏先记下这个引用，稍后再解析，结果还是没找到 `%(key)s`。",
       "同上。延迟引用失败通常集中在 `common/scripted_triggers/` 里，"
       "因为触发器会在运行期反复求值。",
       "参考上一条。重点检查 `%(file)s`。",
       "脚本引用"),

    _e("db.object_dup",
       r"(?P<key>\S+)\s+is already defined",
       SEV_ERROR,
       "对象被重复定义",
       "`%(key)s` 被定义了两次以上，后一个定义会覆盖前一个。",
       "这是**典型的模组冲突**：两个模组定义了同名的游戏对象。"
       "先加载的模组里所有依赖该对象的功能会静默失效——"
       "表现就是「某个模组装了但感觉没生效」。",
       "用本软件的「冲突检测」找到是哪些模组定义了 `%(key)s`，"
       "然后调整加载顺序（把你想保留的排后面）或禁用其中一个。",
       "模组冲突"),

    # ===== 作用域 / 触发器 =====
    _e("script.wrong_scope",
       r"Wrong scope for trigger\s+'(?P<trigger>\S+)'\s+at\s+(?P<file>.+?)(?:\s+line:\s*(?P<line>\d+))?$",
       SEV_ERROR,
       "触发器作用域错误",
       "`%(trigger)s` 被用在了错误的对象上（比如对一个「星球」用了只能对「国家」用的触发器）。",
       "模组脚本写错了，或模组是为另一个游戏版本写的——"
       "旧版本里这个作用域合法，新版本改了。多见于版本落后的模组。",
       "检查 `%(file)s` 所属模组的版本声明。若它声明支持的版本落后游戏 2 个以上大版本，"
       "基本可以确定是它的问题。这条报错集中在同一个文件时，"
       "说明是该模组的系统性写法问题，不是偶发。",
       "脚本逻辑",
       group_by="file"),

    _e("script.trigger_missing",
       r"Unknown trigger:\s*(?P<trigger>\S+)",
       SEV_ERROR,
       "未知的触发器",
       "脚本里用了 `%(trigger)s`，但游戏不认识这个触发器。",
       "① 模组用了另一个模组定义的 scripted_trigger，但那个模组没装/没启用；"
       "② 模组针对的游戏版本与当前不符，触发器已被改名或删除。",
       "先确认前置模组是否齐全；若不是依赖问题，说明该模组需要更新。",
       "脚本逻辑"),

    _e("script.effect_missing",
       r"Unknown effect:\s*(?P<effect>\S+)",
       SEV_ERROR,
       "未知的效果指令",
       "脚本里用了 `%(effect)s`，但游戏不认识这个效果。",
       "同上：前置缺失或模组版本过旧。",
       "同上。",
       "脚本逻辑"),

    _e("script.custom_desc",
       r"script_value/weight field: lacking a custom description at\s+(?P<file>.+)$",
       SEV_WARN,
       "AI 权重缺少自定义描述",
       "`%(file)s` 里的权重字段没有提供 `custom_tooltip`。",
       "这是模组作者的疏漏，会让 AI 决策缺少说明文本。**不影响游戏运行。**",
       "可忽略。属于模组作者可以改进的地方。",
       "脚本逻辑"),

    _e("script.malformed_token",
       r"Error:\s*\"?Malformed token:\s*(?P<token>[^,\"]+),?\s*near line:\s*(?P<line>\d+)",
       SEV_ERROR,
       "语法错误：非法记号",
       "解析 `%(token)s` 时失败（大致在报错行附近）。",
       "模组文件里的括号不配对、少了等号、或者混入了非法字符。"
       "常见于手工编辑过的模组或从其它语言版本复制粘贴时出错。",
       "若该文件属于某个模组，请联系作者修复或换用其它版本。"
       "如果是汉化模组改坏的，试试禁用该汉化。",
       "语法错误"),

    _e("script.localisation_quote",
       r"Expected quotation mark \(\"\) at end of line\s+(?P<line>\d+)\s+in\s+(?P<file>.+)$",
       SEV_WARN,
       "本地化文件引号未闭合",
       "`%(file)s` 第 `%(line)s` 行的引号没有配对。",
       "汉化模组编辑 yml 时最常见的问题：译文里包含了英文引号 `\"` 而没有转义。"
       "这一行的文本会显示异常或干脆不显示。",
       "在对应 yml 文件的那一行把引号改成中文引号「」或转义为 `\\\"`。"
       "如果嫌麻烦，禁用该汉化模组即可消除。",
       "本地化"),

    _e("script.localisation_colon",
       r"Expected colon\(:\) at line\s+(?P<line>\d+)\s+and column\s+(?P<col>\d+)\s+in\s+(?P<file>.+)$",
       SEV_WARN,
       "本地化行缺少冒号",
       "`%(file)s` 第 `%(line)s` 行的格式不对，缺少 `:` 分隔。",
       "yml 本地化的格式必须是 ``键:0 \"文本\"``。这行要么少了冒号，"
       "要么是上一行的引号没闭合导致解析错位。**通常与上一条同时出现。**",
       "通常修好引号问题这条就会一起消失。",
       "本地化"),

    _e("script.localisation_break",
       r"Illegal break character \(utf32=(?P<code>\d+)\) at line\s+(?P<line>\d+)\s+and column\s+(?P<col>\d+)\s+in\s+(?P<file>.+)$",
       SEV_WARN,
       "本地化文本含非法换行字符",
       "`%(file)s` 第 `%(line)s` 行有非法控制字符（编码 %(code)s）。",
       "译文里混入了换行符或制表符。群星的 yml 值必须是单行。",
       "删除该行译文中的换行。`\\n` 才是合法的换行写法。",
       "本地化"),

    _e("script.localisation_keyname",
       r"Invalid key name at line\s+(?P<line>\d+)\s+and column\s+(?P<col>\d+)\s+in\s+(?P<file>.+)$",
       SEV_WARN,
       "本地化键名非法",
       "`%(file)s` 第 `%(line)s` 行的键名不符合规则。",
       "键名只能包含字母、数字、下划线、点。出现空格或中文就会报这个。",
       "修正键名，或忽略（该条文本不会生效）。",
       "本地化"),

    # ===== 图形 / 资源 =====
    _e("gfx.texture",
       r"(?P<what>Failed to (?:load|find) (?:texture|sprite|image))\s*:?\s*(?P<name>\S*)",
       SEV_WARN,
       "贴图/精灵图加载失败",
       "游戏找不到 `%(name)s` 这个图形资源。",
       "模组引用了不存在的贴图文件，或者 gfx 定义文件与实际的 dds/png 文件不匹配。"
       "界面会出现粉红方块或空白图标。",
       "把界面相关的模组（UI Overhaul、图标包等）排到加载顺序后段，"
       "或禁用引起问题的那个模组。",
       "图形资源"),

    _e("gfx.font",
       r"(?P<what>Failed to (?:load|find) font)\s*:?\s*(?P<name>\S*)",
       SEV_WARN,
       "字体加载失败",
       "找不到字体 `%(name)s`。",
       "汉化模组替换了字体但文件缺失，或字体文件名大小写不匹配（Linux 移植模组常见）。",
       "重新安装该汉化模组。中文字体缺失会导致界面显示方块。",
       "图形资源"),

    _e("sound.missing",
       r"(?P<what>Failed to (?:load|find) sound)\s*:?\s*(?P<name>\S*)",
       SEV_NOISE,
       "音效加载失败",
       "找不到音效 `%(name)s`。",
       "模组删掉了音频文件但没更新定义。只影响声音。",
       "可忽略。",
       "音频"),

    # ===== 事件 / 脚本运行期 =====
    _e("event.not_found",
       r"(?P<what>Event\s+(?P<event>\S+)\s+(?:not found|does not exist))",
       SEV_ERROR,
       "事件不存在",
       "脚本试图触发事件 `%(event)s`，但该事件没有定义。",
       "事件所属模组未启用、被覆盖、或版本不匹配改了事件 id。",
       "检查 `%(event)s` 前缀对应的模组是否已启用。",
       "事件系统"),

    _e("event.option_invalid",
       r"(?:Invalid|Unknown) (?:event )?option\s+(?P<opt>\S+)",
       SEV_ERROR,
       "事件选项无效",
       "事件里的选项 `%(opt)s` 不存在。",
       "通常是一个模组的事件被另一个模组覆盖了，导致选项对不上。",
       "这是模组冲突的表现，用冲突检测定位相关模组。",
       "事件系统"),

    # ===== 舰船 / 部件 =====
    _e("ship.section_missing",
       r"(?:Could not find|Unknown|Invalid)\s+(?:section|section_template)\s*:?\s*(?P<name>\S+)",
       SEV_ERROR,
       "舰船区段缺失",
       "舰船设计引用了不存在的区段模板 `%(name)s`。",
       "NSC/巨构/ACOT 这类大改舰船的模组互相覆盖了区段定义。"
       "表现为：某些舰船无法设计、设计界面空白、或者舰队莫名其妙消失。",
       "这是重型冲突。建议把舰船类模组（NSC3、ACOT、巨构）梳理清楚加载顺序，"
       "并确保安装了它们之间的兼容补丁。",
       "舰船部件"),

    _e("ship.component_missing",
       r"(?:Could not find|Unknown|Invalid)\s+(?:component|component_template)\s*:?\s*(?P<name>\S+)",
       SEV_ERROR,
       "舰船部件缺失",
       "引用了不存在的部件模板 `%(name)s`。",
       "同区段问题。部件被覆盖或所属模组未启用。",
       "同区段问题的处理方式。",
       "舰船部件"),

    _e("ship.size_missing",
       r"(?:Could not find|Unknown|Invalid)\s+(?:ship_size|ship size)\s*:?\s*(?P<name>\S+)",
       SEV_ERROR,
       "舰船类型缺失",
       "引用了不存在的舰船类型 `%(name)s`。",
       "舰船类模组冲突。",
       "同上。",
       "舰船部件"),

    # ===== 存档 / 序列化 =====
    _e("save.version",
       r"(?:Save game|savegame).*(?:version|incompatible)",
       SEV_ERROR,
       "存档版本不兼容",
       "读档失败：存档是用另一个游戏版本或另一套模组保存的。",
       "游戏本体更新后，或启用/禁用了影响存档结构的模组，旧存档就读不了。",
       "回到原来的模组组合再读档，或开新档。"
       "**这就是为什么改动模组前要先备份存档。**",
       "存档"),

    _e("save.corrupt",
       r"(?:Failed to (?:load|read|open) save ?game|save ?game is corrupt|"
       r"corrupt save|Failed to deserialize)",
       SEV_FATAL,
       "存档损坏",
       "存档文件读取失败。",
       "存档过程中崩溃、磁盘写入不完整，或模组在存档里留下了无法解析的数据。",
       "尝试读取 `save games` 目录里的 `_backup` 子目录（游戏会自动保留上一份）。",
       "存档"),

    # ===== 事件表损坏（模组脚本真实错误）=====
    _e("event.corrupt_table",
       r"Corrupt Event Table Entry\s*-\s*(?P<token>\S+)\s+in\s+(?P<file>.+?)line:\s*(?P<line>\d+)",
       SEV_ERROR,
       "事件定义表损坏",
       "游戏解析 `%(file)s` 时在第 %(line)s 行遇到了无法理解的记号 `%(token)s`。",
       "这几乎总是**模组脚本的语法错误**：事件的 `{ }` 括号不配对、"
       "多了或少了一个 `=`、或者从其它文件复制粘贴时带进了残缺片段。\n"
       "一旦事件表损坏，该文件里**所有**事件都无法注册——"
       "表现就是「这个模组的事件从来不会触发」。",
       "这是必须修的模组 bug。请检查 `%(file)s` 第 %(line)s 行附近的括号配对；"
       "若是汉化模组改坏的，禁用该汉化即可恢复。"
       "多条同类报错通常来自同一个文件，按文件归并后逐个修。",
       "事件系统",
       group_by="file"),

    _e("event.unknown_scope",
       r"(?:Unknown|Invalid) (?:scope|event target|target)\s*:?\s*(?P<name>\S+)",
       SEV_ERROR,
       "未知的作用域 / 事件目标",
       "脚本引用了不存在的作用域或事件目标 `%(name)s`。",
       "模组用了另一个模组的对象但那个模组没启用，或模组版本与游戏不符。",
       "检查前置依赖；若依赖齐全，说明该模组需要更新。",
       "事件系统"),

    # ===== 内存 / 性能 =====
    _e("mem.allocation",
       r"(?:Failed to allocate|out of memory|bad_alloc|OutOfMemory)",
       SEV_FATAL,
       "内存分配失败",
       "游戏申请内存被系统拒绝。",
       "**这是群星加模组后崩溃的第一大原因。** 32 位地址空间限制、"
       "巨型星系 + 大量模组 + 后期舰队，很容易吃满内存。"
       "你的机器若有 32 GB 内存，真正卡住的一般是单个进程的内存上限或显存。",
       "① 把星系规模从 huge 降到 large/medium；"
       "② 减少同时启用的重型模组；"
       "③ 降低贴图质量设置；"
       "④ 关闭 `shadowSize=2048` 与 8x 抗锯齿；"
       "⑤ 使用「Better Performance」类优化模组。",
       "性能与内存"),

    _e("mem.stack",
       r"(?:stack overflow|StackOverflow)",
       SEV_FATAL,
       "栈溢出",
       "脚本无限递归。",
       "模组的 scripted_effect 里出现了自引用，或者 on_action 形成了无限循环。"
       "这类问题一定会在同一时间点稳定复现。",
       "定位崩溃时间点前后刚触发的模组功能。若稳定复现，"
       "逐个禁用最近新增的模组来二分定位。",
       "性能与内存"),

    _e("perf.slow",
       r"(?:script profiling|took too long|Slow)",
       SEV_NOISE,
       "脚本执行缓慢",
       "某段脚本耗时较长。",
       "模组写了低效的触发器（例如在大循环里做全星系的遍历）。",
       "如果游戏后期明显卡顿，可尝试禁用 `! Better Performance & Utilities` 之外的重型脚本模组。",
       "性能与内存"),
]


# 补丁式补充：一些只按前缀/片段匹配的规则
KB_SUBSTRING: list[tuple[str, str, str, str, str, str, str]] = [
    # (子串, 严重度, 标题, 含义, 成因, 处理, 类别)
    ("is already defined", SEV_ERROR, "对象被重复定义（冲突）",
     "同名游戏对象被两个模组定义。", "模组冲突。", "用冲突检测调整加载顺序。", "模组冲突"),
    ("duplicate", SEV_WARN, "发现重复条目",
     "同一内容出现多次。", "多个模组提供了相同定义。", "检查加载顺序。", "模组冲突"),
    ("has no effect", SEV_WARN, "指令无效",
     "某条脚本指令在当前上下文没有作用。", "模组脚本写法问题或版本不匹配。", "参考所属模组版本。", "脚本逻辑"),
    ("not found", SEV_WARN, "引用的对象未找到",
     "脚本引用了一个不存在的对象。", "前置缺失或模组过期。", "检查依赖与版本。", "脚本引用"),
    ("icon", SEV_NOISE, "图标问题",
     "界面图标相关。", "图形资源缺失。", "一般可忽略。", "图形资源"),
    ("african", SEV_NOISE, "（无关噪音）", "", "", "可忽略。", "通用"),
]

# 已识别的可忽略噪音源文件名（原版游戏自身的历史遗留报错）
NOISE_SOURCES = {
    "strategic_resources_gui_group.cpp",   # 原版 UI 资源组历史问题
    "persistent.cpp",
    "pdxassetutil.cpp",
}


def lookup(message: str, source: str = "") -> KBEntry | None:
    """在知识库中查找匹配条目。"""
    for e in KB:
        if e.pattern.search(message):
            return e
    low = message.lower()
    for sub, sev, title, mean, cause, fix, cat in KB_SUBSTRING:
        if sub in low:
            return KBEntry(
                pattern=re.compile(re.escape(sub)), severity=sev,
                title_zh=title, meaning_zh=mean, cause_zh=cause, fix_zh=fix,
                category=cat, id=f"sub:{sub}",
            )
    return None


# ---------------------------------------------------------------------------
# 日志解析
# ---------------------------------------------------------------------------

# [04:30:42][dlc.cpp:1840]: 消息
LINE_RE = re.compile(
    r"^\[(?P<time>\d{1,2}:\d{2}:\d{2})\]\[(?P<src>[^\]:]+):(?P<line>\d+)\]:\s?(?P<msg>.*)$"
)
# 没有源文件的行：[04:30:42]: 消息
LINE_RE2 = re.compile(r"^\[(?P<time>\d{1,2}:\d{2}:\d{2})\]:\s?(?P<msg>.*)$")
# 完全没有时间戳
LINE_RE3 = re.compile(r"^(?P<msg>.+)$")


def parse_line(raw: str) -> LogLine:
    """解析单行日志。"""
    m = LINE_RE.match(raw)
    if m:
        msg = m.group("msg")
        ll = LogLine(raw=raw, time=m.group("time"), source=m.group("src"),
                     line_no=m.group("line"), message=msg)
    else:
        m2 = LINE_RE2.match(raw)
        if m2:
            ll = LogLine(raw=raw, time=m2.group("time"), message=m2.group("msg"))
        else:
            ll = LogLine(raw=raw, message=raw)
    return annotate(ll)


def annotate(ll: LogLine) -> LogLine:
    """给日志行补上知识库注解。"""
    e = lookup(ll.message, ll.source)
    ll.entry = e
    if e is not None:
        ll.severity = e.severity
        # 提取正则命名组作为参数，用于填充 %(name)s
        try:
            m = e.pattern.search(ll.message)
            if m:
                ll.params = {k: v for k, v in m.groupdict().items() if v}
        except re.error:
            ll.params = {}
    else:
        ll.severity = _guess_severity(ll.message, ll.source)
    return ll


def _guess_severity(msg: str, source: str) -> str:
    low = msg.lower()
    if any(k in low for k in ("error", "failed", "invalid", "unknown",
                              "missing", "cannot", "could not", "unable")):
        return SEV_WARN
    if source and source.split(".")[0] in {s.split(".")[0] for s in NOISE_SOURCES}:
        return SEV_NOISE
    return SEV_NOISE


def _fill(template: str, params: dict[str, str]) -> str:
    """安全填充 ``%(name)s``。"""
    if not template or not params:
        return template
    out = template
    for k, v in params.items():
        out = out.replace(f"%({k})s", str(v))
    # 清掉没填上的占位符
    out = re.sub(r"%\(\w+\)s", "（未知）", out)
    return out


def rendered(ll: LogLine) -> dict[str, str]:
    """返回填充好参数的中文注解。"""
    if ll.entry is None:
        return {}
    p = ll.params
    return {
        "title": ll.entry.title_zh,
        "meaning": _fill(ll.entry.meaning_zh, p),
        "cause": _fill(ll.entry.cause_zh, p),
        "fix": _fill(ll.entry.fix_zh, p),
        "category": ll.entry.category,
        "rule": ll.entry.id,
    }


# ---------------------------------------------------------------------------
# 日志文件分析
# ---------------------------------------------------------------------------

@dataclass
class LogAnalysis:
    """一份日志文件的分析结果。"""

    path: str = ""
    total_lines: int = 0
    parsed_lines: int = 0
    size_bytes: int = 0
    # 按知识库规则聚合
    groups: dict[str, dict[str, Any]] = field(default_factory=dict)
    severity_counts: dict[str, int] = field(default_factory=dict)
    top_files: list[tuple[str, int]] = field(default_factory=list)
    top_mods: list[tuple[str, int]] = field(default_factory=list)
    crash_tail: list[LogLine] = field(default_factory=list)
    unparsed_samples: list[str] = field(default_factory=list)

    def sorted_groups(self) -> list[dict[str, Any]]:
        """按「严重度 → 出现次数」排序的错误分组。"""
        rows = list(self.groups.values())
        rows.sort(key=lambda g: (SEV_RANK.get(g["severity"], 9), -g["count"]))
        return rows

    def actionable(self) -> list[dict[str, Any]]:
        """只保留需要处理的（致命 + 错误）分组。"""
        return [g for g in self.sorted_groups()
                if g["severity"] in (SEV_FATAL, SEV_ERROR)]

    def to_dict(self) -> dict:
        """序列化为界面与报告用的字典。"""
        return {
            "path": self.path, "total_lines": self.total_lines,
            "parsed_lines": self.parsed_lines, "size_bytes": self.size_bytes,
            "severity_counts": self.severity_counts,
            "groups": self.sorted_groups(),
            "top_files": self.top_files,
            "top_mods": self.top_mods,
            "unparsed_samples": self.unparsed_samples[:20],
        }


# 从消息/文件名里认出模组名
_MOD_HINT_RE = re.compile(r"([A-Za-z0-9_\-]{3,})/([A-Za-z0-9_\-./]+\.(?:txt|yml|gui|gfx))")


def analyze_log(path, *, max_lines: int = 400_000,
                progress: Callable[[str, float], None] | None = None,
                should_stop: Callable[[], bool] | None = None,
                sample_per_group: int = 3) -> LogAnalysis:
    """分析一个日志文件，按知识库规则聚合。"""
    import os
    from .paths import long_path

    an = LogAnalysis(path=str(path))
    try:
        an.size_bytes = os.path.getsize(long_path(path))
    except OSError:
        return an

    groups: dict[str, dict[str, Any]] = {}
    sev_counts: dict[str, int] = {k: 0 for k in SEV_LABEL}
    file_counts: dict[str, int] = {}
    mod_counts: dict[str, int] = {}
    tail: list[LogLine] = []
    unparsed: list[str] = []

    n = 0
    with open(long_path(path), "r", encoding="utf-8", errors="replace") as f:
        for raw in f:
            n += 1
            if n > max_lines:
                break
            if should_stop and n % 20000 == 0 and should_stop():
                break
            raw = raw.rstrip("\n").rstrip("\r")
            if not raw.strip():
                continue
            ll = parse_line(raw)
            an.parsed_lines += 1
            sev_counts[ll.severity] = sev_counts.get(ll.severity, 0) + 1
            if ll.source:
                file_counts[ll.source] = file_counts.get(ll.source, 0) + 1

            # 模组名启发
            for m in _MOD_HINT_RE.finditer(ll.message):
                mod_counts[m.group(1)] = mod_counts.get(m.group(1), 0) + 1
            for m in re.finditer(r"mod/([A-Za-z0-9_\-]+)\.mod", ll.message):
                mod_counts[m.group(1)] = mod_counts.get(m.group(1), 0) + 1

            key = ll.group_key
            g = groups.get(key)
            if g is None:
                g = {
                    "rule": key,
                    "severity": ll.severity,
                    "severity_label": SEV_LABEL.get(ll.severity, ll.severity),
                    "count": 0,
                    "category": ll.entry.category if ll.entry else "未分类",
                    "title": ll.entry.title_zh if ll.entry else "未识别消息",
                    "meaning": "", "cause": "", "fix": "",
                    "samples": [],
                    "sources": {},
                }
                if ll.entry:
                    r = rendered(ll)
                    g["meaning"], g["cause"], g["fix"] = (
                        r["meaning"], r["cause"], r["fix"])
                    # 按文件细分时，把文件名写进标题，一眼看出是哪个文件坏了
                    if ll.entry.group_by == "file" and ll.params.get("file"):
                        g["title"] = f"{r['title']}：{ll.params['file'].strip()}"
                else:
                    g["meaning"] = "本条消息不在知识库中。"
                    g["fix"] = "如反复出现且伴随异常，请把原文反馈给开发者扩充知识库。"
                groups[key] = g
            g["count"] += 1
            if ll.source:
                g["sources"][ll.source] = g["sources"].get(ll.source, 0) + 1
            if len(g["samples"]) < sample_per_group:
                d = ll.to_dict()
                if ll.entry:
                    r = rendered(ll)
                    d.update({"title_zh": r["title"], "meaning_zh": r["meaning"],
                              "cause_zh": r["cause"], "fix_zh": r["fix"]})
                g["samples"].append(d)

            # 保留尾巴（崩溃前最后的行为最有用）
            tail.append(ll)
            if len(tail) > 400:
                tail.pop(0)

            if not ll.entry and len(unparsed) < 40 and len(ll.message) < 200:
                if ll.severity != SEV_NOISE and _looks_interesting(ll.message):
                    unparsed.append(ll.message)

            if progress and n % 50000 == 0:
                progress(f"解析日志 {n} 行", min(0.95, n / max(1, an.total_lines or n)))

    an.total_lines = n
    an.groups = groups
    an.severity_counts = sev_counts
    an.top_files = sorted(file_counts.items(), key=lambda kv: -kv[1])[:25]
    an.top_mods = sorted(mod_counts.items(), key=lambda kv: -kv[1])[:25]
    an.crash_tail = tail[-60:]
    an.unparsed_samples = unparsed
    return an


def _normalize_msg(msg: str) -> str:
    """把消息里的数字/引号抹平，用于聚类。"""
    s = re.sub(r"\d+", "#", msg)
    s = re.sub(r"[A-Za-z0-9_\-]{20,}", "<id>", s)
    return s[:120]


def _looks_interesting(msg: str) -> bool:
    low = msg.lower()
    return any(k in low for k in ("error", "fail", "invalid", "unknown",
                                  "conflict", "duplicat", "crash", "assert"))


def iter_log_files(paths) -> Iterator[tuple[str, str]]:
    """列出所有可分析的日志文件 ``(显示名, 路径)``。"""
    import os

    logs = paths.logs_dir
    if not os.path.isdir(str(logs)):
        return
    order = ["error.log", "game.log", "system.log", "setup.log", "debug.log",
             "time.log", "info.log", "memory.log", "ai.log", "profiler.log",
             "pdxsdk.log", "script_profiling.log", "event_data.log"]
    seen = set()
    for name in order:
        p = os.path.join(str(logs), name)
        if os.path.isfile(p):
            seen.add(name)
            yield name, p
    try:
        for e in sorted(os.scandir(str(logs)), key=lambda x: x.name):
            if e.is_file() and e.name not in seen and e.name.endswith(".log"):
                yield e.name, e.path
    except OSError:
        return
