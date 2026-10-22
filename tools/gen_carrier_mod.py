"""生成「奇点协议」的载体 mod。

**为什么需要它**

OGAS 的机制是：拦截游戏自然发出的建造命令，把里面的建筑 ID 换掉。
能换多长的 ID，取决于载体建筑 ID 有多长 —— 包里那个字符串槽位是定长的。

原版最长的建筑是 48 字节（``building_forever_cruise_passenger_consumer_goods``），
但它有 ``potential = { owner? = { has_origin = origin_forever_cruise } }``，
普通国家建不了，点不出命令 —— 用不了。

所以自己造：几个**任何国家都能建、零成本、一天建成、无产出**的空壳建筑，
ID 做成 60 / 80 / 120 字节。点它 → 游戏发命令 → OGAS 换成真正的目标建筑。

**覆盖能力**（实测的模组 ID 长度分布）：

    上限 48 字节 → 334/339 建筑、158/179 区划
    上限 60 字节 → 338/339 建筑、177/179 区划
    上限 80 字节 → 全部

**这不是作弊建筑** —— 它只存在于载体飞地上，产出为零，成本为零，
唯一作用是"替游戏发一条合法的建造命令"。
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

OUT = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

ROOT = Path(r"E:\yiuxi\OGAS\smd_carrier_mod")
MOD_NAME = "SMD Carrier Relay"
PREFIX = "building_smd_relay"

# 要造哪几个长度档。60 兜住 98.5%，80 兜住全部，120 留给以后更长的模组。
LENGTHS = (60, 80, 120)


def make_id(total_bytes: int) -> str:
    """造一个**恰好** ``total_bytes`` 字节的建筑 ID。

    Clausewitz 的标识符只允许 ``[a-z0-9_]``，全小写，所以直接按字节数补。
    短了补 ``z``，长了就报错 —— 补出来的长度不对，整个机制就白搭。
    """
    head = f"{PREFIX}_{total_bytes}_"
    pad = total_bytes - len(head)
    if pad < 0:
        raise ValueError(f"{total_bytes} 字节装不下前缀 {head}")
    ident = head + "z" * pad
    assert len(ident.encode()) == total_bytes, (
        f"ID 长度不对：{len(ident.encode())} != {total_bytes}")
    return ident


def buildings_txt() -> str:
    """建筑定义。

    几个关键字段的作用：

    ``can_build = yes``      允许建造 —— 否则按钮是灰的，点不出命令
    ``potential = always``   任何国家、任何星球都能建 —— 不挑起源
    ``cost = 0``             免费，避免 AI 规划时被经济约束挡住
    ``base_buildtime = 1``   一天建成，下一回合就能再点
    ``upkeep = 0``           不拖经济
    ``category``             决定它出现在哪个建造页签
    ``building_sets``        必须挂一个已有的集合，否则 UI 不显示
    """
    lines = [
        "# 「奇点协议」载体建筑 —— 由 tools/gen_carrier_mod.py 生成，不要手改。",
        "#",
        "# 这些建筑是**指令中继**：点它会触发一次正常的建造命令，",
        "# 外部执行器拦截那条命令，把建筑 ID 换成真正的目标，再放回游戏。",
        "# 所以它本身不产出任何东西，成本和时间都压到最低。",
        "",
    ]
    for n in LENGTHS:
        ident = make_id(n)
        lines += [
            f"# {n} 字节载体：能替换 ID 长度不超过 {n} 字节的建筑",
            f"{ident} = {{",
            "\tcan_build = yes",
            "\tbase_buildtime = 1",
            "\tcategory = planet_buildings",
            "",
            "\tbuilding_sets = {",
            "\t\tresearch",
            "\t}",
            "",
            "\tpotential = {",
            "\t\talways = yes",
            "\t}",
            "",
            "\tallow = {",
            "\t\talways = yes",
            "\t}",
            "",
            "\tdestroy_trigger = {",
            "\t\talways = yes",
            "\t}",
            "",
            "\tresources = {",
            "\t\tcategory = planet_buildings",
            "\t\tcost = {",
            "\t\t\tminerals = 0",
            "\t\t}",
            "\t\tproduces = {",
            "\t\t}",
            "\t\tupkeep = {",
            "\t\t\tenergy = 0",
            "\t\t}",
            "\t}",
            "",
            "\tprereq = {",
            "\t}",
            "",
            "\tai_weight = {",
            "\t\tweight = 0",
            "\t}",
            "}",
            "",
        ]
    return "\n".join(lines)


def loc_yml(lang: str) -> str:
    """本地化。中文名要让玩家一眼看出这是干什么的。"""
    head = "l_simp_chinese" if lang == "simp_chinese" else "l_english"
    lines = [f"{head}:", " # 「奇点协议」载体建筑"]
    for n in LENGTHS:
        ident = make_id(n)
        if lang == "simp_chinese":
            name = f"指令中继 · {n} 字节"
            desc = "点击它会让游戏发出一条建造命令，外部执行器会把它改写成真正的目标。"
        else:
            name = f"Command Relay · {n}B"
            desc = ("Clicking this emits a real build command; the external "
                    "executor rewrites it into the actual target.")
        lines.append(f' {ident}:0 "{name}"')
        lines.append(f' {ident}_desc:0 "{desc}"')
    return "\n".join(lines) + "\n"


def descriptor() -> str:
    return (
        'version="0.1.0"\n'
        'tags={\n'
        '    "Gameplay"\n'
        '    "Utilities"\n'
        '}\n'
        f'name="{MOD_NAME}"\n'
        'supported_version="4.4.*"\n'
    )


def main() -> int:
    ROOT.mkdir(parents=True, exist_ok=True)
    (ROOT / "common" / "buildings").mkdir(parents=True, exist_ok=True)
    for lang in ("simp_chinese", "english"):
        (ROOT / "localisation" / lang).mkdir(parents=True, exist_ok=True)

    (ROOT / "descriptor.mod").write_text(descriptor(), encoding="utf-8",
                                         newline="\n")
    (ROOT / "common" / "buildings" / "smd_carriers.txt").write_text(
        buildings_txt(), encoding="utf-8", newline="\n")
    (ROOT / "localisation" / "simp_chinese"
     / "smd_carriers_l_simp_chinese.yml").write_text(
        loc_yml("simp_chinese"), encoding="utf-8", newline="\n")
    (ROOT / "localisation" / "english"
     / "smd_carriers_l_english.yml").write_text(
        loc_yml("english"), encoding="utf-8", newline="\n")

    print(f"  已生成 {ROOT}", file=OUT)
    print("", file=OUT)
    print("  载体 ID（字节数必须精确）:", file=OUT)
    for n in LENGTHS:
        i = make_id(n)
        print(f"    {len(i.encode()):>3} 字节  {i[:46]}…", file=OUT)
    print("", file=OUT)
    print("  文件:", file=OUT)
    for f in sorted(ROOT.rglob("*")):
        if f.is_file():
            print(f"    {f.relative_to(ROOT)}  ({f.stat().st_size} B)",
                  file=OUT)
    OUT.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
