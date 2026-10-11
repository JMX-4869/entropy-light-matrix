"""游戏控制台（指令台）。

为什么这么做
------------
群星的控制台**只能**在游戏里按 ``~`` 打开、手敲命令，官方没有提供任何
外部接口。查过游戏目录后确认，唯一持久的、双向的通道是：

    <游戏本体目录>/console_history.txt

游戏把每一行当作"输入过的命令"，打开控制台按 ``↑`` 就能翻出来。
所以：

* **读它** → 知道玩家最近在游戏里敲了什么
* **写它** → 把用户在软件里挑好的命令送进游戏，进游戏按 ↑ 即可取用

这是唯一不需要改游戏、不需要装 mod、也不会被反作弊/校验挡住的方案。
软件里把命令按用途整理好、参数填好，用户进游戏只要按 ↑ 再回车。

**不做的事**：不往游戏进程里注入按键、不伪造控制台输入。
那类做法依赖未公开的内存结构，版本一更新就崩，还可能被判定为作弊。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .paths import is_dir, is_file, read_text, write_text


@dataclass
class ConsoleCommand:
    """一条控制台命令。"""

    cmd: str                     # 命令本体（不含参数），例如 "damage"
    label: str                   # 中文名，界面上给人看的
    desc: str = ""               # 一句话说明
    syntax: str = ""             # 完整写法示例，例如 "damage 1000000"
    args: list[str] = field(default_factory=list)   # 常用参数预设
    category: str = "其他"
    needs_selection: bool = False   # 是否必须先在地图上选中目标
    risky: bool = False          # 是否会显著改变存档状态
    example: str = ""            # 实际可用的完整例子

    def to_dict(self) -> dict:
        """序列化，供界面渲染命令卡片。"""
        return {
            "cmd": self.cmd, "label": self.label, "desc": self.desc,
            "syntax": self.syntax or self.cmd, "args": self.args,
            "category": self.category,
            "needs_selection": self.needs_selection, "risky": self.risky,
            "example": self.example or self.syntax or self.cmd,
        }


# ---------------------------------------------------------------------------
# 命令库
# ---------------------------------------------------------------------------
# 只收录**确认可用**的常见命令，每条都写清前提条件。
# 宁可少列，也不要列一堆用不了的名字让用户白试。
CATEGORIES = ("资源与科技", "舰队与星球", "探索与地图", "调试与信息",
              "事件与外交", "整活与测试")

LIBRARY: tuple[ConsoleCommand, ...] = (
    # ---- 资源与科技 ----
    ConsoleCommand(
        "energy", "加能量币", "给当前帝国加指定数量的能量币",
        "energy <数量>", ["10000", "1000000"], "资源与科技",
        example="energy 100000"),
    ConsoleCommand(
        "minerals", "加矿物", "给当前帝国加矿物",
        "minerals <数量>", ["10000", "1000000"], "资源与科技",
        example="minerals 100000"),
    ConsoleCommand(
        "alloys", "加合金", "给当前帝国加合金",
        "alloys <数量>", ["10000", "1000000"], "资源与科技",
        example="alloys 100000"),
    ConsoleCommand(
        "unity", "加凝聚力", "给当前帝国加凝聚力",
        "unity <数量>", ["10000", "1000000"], "资源与科技",
        example="unity 100000"),
    ConsoleCommand(
        "influence", "加影响力", "给当前帝国加影响力",
        "influence <数量>", ["1000", "10000"], "资源与科技",
        example="influence 5000"),
    ConsoleCommand(
        "research_all_technologies", "解锁全部科技",
        "研究完所有科技（可带层数参数，默认当前可研究的全部）",
        "research_all_technologies", [], "资源与科技", risky=True,
        example="research_all_technologies"),
    ConsoleCommand(
        "finish_research", "立即完成研究",
        "当前正在研究的项目立刻完成",
        "finish_research", [], "资源与科技",
        example="finish_research"),
    ConsoleCommand(
        "finish_special_projects", "立即完成特殊项目",
        "所有特殊项目立刻完成",
        "finish_special_projects", [], "资源与科技",
        example="finish_special_projects"),

    # ---- 舰队与星球 ----
    ConsoleCommand(
        "instant_build", "瞬间建造",
        "开关型：建筑、舰队、巨构瞬间完成。再输一次关闭",
        "instant_build", [], "舰队与星球", risky=True,
        example="instant_build"),
    ConsoleCommand(
        "instant_move", "瞬间移动",
        "开关型：舰队移动不耗时。再输一次关闭",
        "instant_move", [], "舰队与星球",
        example="instant_move"),
    ConsoleCommand(
        "own", "占领选中目标",
        "**必须先用鼠标选中目标**：星球 / 舰队 / 星系，归你所有",
        "own", [], "舰队与星球", needs_selection=True, risky=True,
        example="own"),
    ConsoleCommand(
        "damage", "对选中目标造成伤害",
        "**必须先选中目标**。数值给很大即秒杀",
        "damage <数量>", ["1000000", "10000000000"], "舰队与星球",
        needs_selection=True, example="damage 1000000"),
    ConsoleCommand(
        "kill_country", "灭掉选中帝国",
        "**必须先选中一个帝国**（在地图上点它的领土）",
        "kill_country", [], "舰队与星球", needs_selection=True, risky=True,
        example="kill_country"),
    ConsoleCommand(
        "planet_resource", "给选中星球加资源",
        "**必须选中星球**。资源代号见游戏内提示，例如 sr_dark_nanites",
        "planet_resource <资源代号> <数量>", ["energy 10000"],
        "舰队与星球", needs_selection=True,
        example="planet_resource sr_dark_nanites 1000"),
    ConsoleCommand(
        "add_relic", "获得遗珍",
        "获得指定遗珍（代号用 `relic` 相关 ID）",
        "add_relic <遗珍ID>", [], "舰队与星球",
        example="add_relic r_galatron"),
    ConsoleCommand(
        "max_unity", "凝聚力相关上限", "与凝聚力相关的调试命令",
        "max_unity", [], "舰队与星球", example="max_unity"),

    # ---- 探索与地图 ----
    ConsoleCommand(
        "survey", "瞬间勘探",
        "**必须先选中舰队**：该舰队瞬间勘探完所在星系",
        "survey", [], "探索与地图", needs_selection=True,
        example="survey"),
    ConsoleCommand(
        "rr", "随机选一个星球",
        "**两步命令的第一步**：随机选中一个星球，接着输入 own 即可占领",
        "rr", [], "探索与地图",
        example="rr"),
    ConsoleCommand(
        "observe", "观察者模式",
        "开关型：可以查看全图、看 AI 都在干什么",
        "observe", [], "探索与地图", risky=True,
        example="observe"),
    ConsoleCommand(
        "smooth", "平滑地图视野", "切换地图视野的平滑效果",
        "smooth", [], "探索与地图", example="smooth"),
    ConsoleCommand(
        "intel", "查看情报", "查看当前情报状态",
        "intel", [], "探索与地图", example="intel"),
    ConsoleCommand(
        "survey_all", "勘探全图",
        "一键勘探所有星系（部分版本可能不支持）",
        "survey_all", [], "探索与地图", risky=True,
        example="survey_all"),

    # ---- 调试与信息 ----
    ConsoleCommand(
        "debugtooltip", "调试提示",
        "开关型：鼠标悬停时显示对象 ID 与内部字段。**找 ID 最常用**",
        "debugtooltip", [], "调试与信息",
        example="debugtooltip"),
    ConsoleCommand(
        "help", "查看命令帮助",
        "在游戏里列出所有可用命令",
        "help", [], "调试与信息", example="help"),
    ConsoleCommand(
        "clear", "清屏", "清空控制台输出",
        "clear", [], "调试与信息", example="clear"),
    ConsoleCommand(
        "version", "查看版本", "显示游戏版本号",
        "version", [], "调试与信息", example="version"),
    ConsoleCommand(
        "3dstats", "性能统计", "开关型：显示帧率等渲染统计",
        "3dstats", [], "调试与信息", example="3dstats"),
    ConsoleCommand(
        "nomen", "去掉菜单", "开关型：隐藏界面（截图用）",
        "nomen", [], "调试与信息", example="nomen"),
    ConsoleCommand(
        "tweakergui", "调试面板",
        "开关型：打开 Tweaker 面板（部分版本）",
        "tweakergui", [], "调试与信息", example="tweakergui"),

    # ---- 事件与外交 ----
    ConsoleCommand(
        "event", "触发事件",
        "触发指定事件。事件 ID 用 debugtooltip 找",
        "event <事件ID>", [], "事件与外交",
        example="event distar.1"),
    ConsoleCommand(
        "add_trait", "给选中目标加特质",
        "**必须先选中**（领袖 / 种族 / 星球）",
        "add_trait <特质ID>", [], "事件与外交", needs_selection=True,
        example="add_trait leader_trait_psionic"),
    ConsoleCommand(
        "remove_trait", "移除特质",
        "**必须先选中目标**",
        "remove_trait <特质ID>", [], "事件与外交",
        needs_selection=True,
        example="remove_trait leader_trait_psionic"),
    ConsoleCommand(
        "add_opinion", "调整好感度",
        "**必须先选中一个帝国**，调整它对你的好感",
        "add_opinion <数值>", ["100", "-100"], "事件与外交",
        needs_selection=True, example="add_opinion 100"),

    # ---- 整活与测试 ----
    ConsoleCommand(
        "ai", "交给 AI 托管", "开关型：当前帝国交给 AI 自动打",
        "ai", [], "整活与测试", risky=True, example="ai"),
    ConsoleCommand(
        "play", "切换控制国家",
        "**必须先选中一个帝国**：改为控制它",
        "play", [], "整活与测试", needs_selection=True, risky=True,
        example="play"),
    ConsoleCommand(
        "crash", "故意崩溃", "让游戏崩溃（测试崩溃报告用）",
        "crash", [], "整活与测试", risky=True, example="crash"),
    ConsoleCommand(
        "game_over", "游戏结束", "立即结束当前游戏",
        "game_over", [], "整活与测试", risky=True, example="game_over"),
)


def library() -> list[dict]:
    """整个命令库（序列化后供界面使用）。"""
    return [c.to_dict() for c in LIBRARY]


def find(cmd: str) -> ConsoleCommand | None:
    """按命令名查一条命令。"""
    low = (cmd or "").strip().lower()
    for c in LIBRARY:
        if c.cmd.lower() == low:
            return c
    return None


# ---------------------------------------------------------------------------
# 读写游戏的控制台历史
# ---------------------------------------------------------------------------

def read_history(paths) -> dict:
    """读游戏的控制台历史。

    返回 ``{ok, path, lines, exists, mtime}``。
    文件不存在不算错误——玩家可能从没开过控制台。
    """
    p = paths.console_history
    if not p or not str(p):
        return {"ok": False, "error": "未找到游戏目录，无法定位控制台历史"}
    try:
        if not is_file(p):
            return {"ok": True, "path": str(p), "lines": [], "exists": False,
                    "count": 0}
        text = read_text(p)
        lines = [ln.rstrip("\r\n") for ln in text.splitlines()]
        st = p.stat()
        return {"ok": True, "path": str(p), "lines": lines,
                "exists": True, "count": len(lines), "mtime": st.st_mtime}
    except OSError as exc:
        return {"ok": False, "error": f"读取控制台历史失败：{exc}"}


def write_history(paths, lines: list[str], *, backup: bool = True) -> dict:
    """覆盖写控制台历史。

    ``backup`` 为真时先备份原文件（游戏自己的历史别弄丢了）。
    **纯 UTF-8 无 BOM**：游戏读这个文件不认 BOM。
    """
    p = paths.console_history
    if not p or not str(p):
        return {"ok": False, "error": "未找到游戏目录，无法写入控制台历史"}
    parent = Path(p).parent
    if not is_dir(parent):
        return {"ok": False, "error": f"游戏目录不存在：{parent}"}

    backup_path = ""
    try:
        if backup and is_file(p):
            from datetime import datetime
            b = parent / (f"console_history.backup_"
                          f"{datetime.now():%Y%m%d_%H%M%S}.txt")
            b.write_bytes(Path(p).read_bytes())
            backup_path = str(b)
        # 去重且保序：同一命令重复进历史没有意义，还会把 ↑ 翻找变长
        seen: set[str] = set()
        clean: list[str] = []
        for ln in lines:
            s = str(ln).strip()
            if not s or s in seen:
                continue
            seen.add(s)
            clean.append(s)
        write_text(p, "\n".join(clean) + ("\n" if clean else ""), bom=False)
        return {"ok": True, "path": str(p), "count": len(clean),
                "backup": backup_path}
    except OSError as exc:
        return {"ok": False, "error": f"写入控制台历史失败：{exc}"}


def push_commands(paths, commands: list[str], *, keep_existing: bool = True,
                  limit: int = 0) -> dict:
    """把命令送进游戏控制台历史。

    写进去之后，**进游戏按 ``~`` 打开控制台、再按 ``↑``** 就能翻到它们，
    回车即执行。

    ``keep_existing`` 为真时保留玩家原有的历史（新的排在前面）。

    ``limit`` **默认 0 = 不截断**。这一点很重要：真实历史有 516 行、
    其中 414 条是**不重复**的，早先默认砍到 200 行会静默丢掉 100 多条
    玩家自己的命令（实测踩过）。截断只在调用方明确要求时才做。

    去重是无害的：玩家反复敲同一条命令是噪音，收掉它反而让 ↑ 更好翻。
    """
    cleaned = [str(c).strip() for c in (commands or []) if str(c).strip()]
    if not cleaned:
        return {"ok": False, "error": "没有可写入的命令"}

    old = read_history(paths)
    if not old.get("ok"):
        return old
    existing = list(old.get("lines") or []) if keep_existing else []
    # 新命令放最前（↑ 先翻到最近的），旧历史接在后面
    merged = cleaned + [x for x in existing if x not in set(cleaned)]
    dropped = 0
    if limit and len(merged) > limit:
        dropped = len(merged) - limit
        merged = merged[:limit]
    res = write_history(paths, merged)
    if res.get("ok"):
        res["pushed"] = len(cleaned)
        res["kept"] = len(existing)
        res["dropped"] = dropped
        if dropped:
            res["warning"] = (f"为了让历史短一些，丢弃了 {dropped} 条较旧的记录。"
                              f"不想要这个效果就把 limit 设成 0。")
    return res
