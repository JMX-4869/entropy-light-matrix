"""从「4.2.4 星系热症天灾宝典」xlsx 抽取「时之虫档案」数据。

产出 ``smd/data/wiki.json``（随程序分发）：

    {
      "meta":     {source, version, updated, sections, entries},
      "sections": [ {key,title,desc,columns,rows,cat_col?}, ... ]
    }

各页结构差异很大（表头行号 2~6 不等、大量合并单元格、页首还有说明行），
所以**按页手工描述抽取规则**，不硬套一套通用逻辑——
通用逻辑在这种人工维护的表上一定会出错。
"""
import io
import json
import os
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                              errors="replace")
import openpyxl

ROOT = Path(__file__).resolve().parent.parent

# 源表格路径。默认找仓库根目录下的 xlsx；也可以用环境变量指定，
# 免得把某台机器上的绝对路径写进代码里。
SRC = Path(os.environ.get("SMD_WIKI_XLSX")
           or next(iter(sorted(ROOT.glob("*.xlsx"))), ROOT / "宝典.xlsx"))
# 输出直接放进包内数据目录，路径由脚本位置推导，不写死
OUT = ROOT / "smd" / "data" / "wiki.json"


def cell(v) -> str:
    """单元格转字符串：合并内部空白、去掉换行（表格里显示不需要换行）。"""
    if v is None:
        return ""
    s = str(v).replace("\r\n", "\n").replace("\r", "\n")
    return " ".join(" ".join(p.split()) for p in s.split("\n")).strip()


def grid(ws, max_col=None):
    """整页读成二维字符串数组。"""
    return [[cell(c) for c in row]
            for row in ws.iter_rows(max_col=max_col, values_only=True)]


def take(row, cols):
    """按列号（0-based）取子集。"""
    return [row[i] if i < len(row) else "" for i in cols]


def header_row(g, token, limit=14):
    """找含指定文字的行号（0-based）。"""
    for i, row in enumerate(g[:limit]):
        if any(token in c for c in row):
            return i
    return -1


def rows_from(g, start, cols, *, carry=0):
    """从 start 行起按列抽取；``carry`` 指定一个"向下填充"的列（分类列）。"""
    out, cur = [], ""
    for row in g[start:]:
        if carry is not None and carry >= 0 and carry < len(row):
            if row[carry]:
                cur = row[carry]
        vals = take(row, cols)
        if not any(vals):
            continue
        if carry is not None and carry >= 0 and carry < len(vals):
            vals[carry] = cur
        out.append(vals)
    return out


def dedupe(rows):
    """整行完全重复的去掉（人工表里很常见）。"""
    seen, out = set(), []
    for r in rows:
        k = tuple(r)
        if k in seen:
            continue
        seen.add(k)
        out.append(r)
    return out


def section(wb, key, title, sheet, *, cols, start_token=None,
            start_offset=1, desc="", carry=None, max_col=24):
    g = grid(wb[sheet], max_col=max_col)
    if start_token:
        h = header_row(g, start_token)
        if h < 0:
            print(f"  ! {sheet}: 找不到表头「{start_token}」")
            h = 0
        start = h + start_offset
    else:
        start = start_offset
    rows = dedupe(rows_from(g, start, cols, carry=carry))
    return {"key": key, "title": title, "desc": desc,
            "columns": [f"c{i}" for i in cols], "rows": rows}


def main() -> int:
    if not SRC.is_file():
        print(f"找不到源表格：{SRC}")
        print("把 xlsx 放到项目根目录，或用环境变量指定：")
        print('  set SMD_WIKI_XLSX=D:\\path\\to\\宝典.xlsx')
        return 1
    wb = openpyxl.load_workbook(SRC, read_only=True, data_only=True)
    S: list[dict] = []

    def add(key, title, desc, columns, rows):
        S.append({"key": key, "title": title, "desc": desc,
                  "columns": columns, "rows": rows})

    # ---- 扩展代码 ----
    g = grid(wb["扩展代码"], max_col=8)
    h = header_row(g, "类别")
    rows, cur = [], ""
    for row in g[h + 1:]:
        if row[0]:
            cur = row[0]
        eff, code, note = row[1], row[2], row[5]
        if not (eff or code):
            continue
        rows.append([cur, eff, code, note])
    add("console_ext", "扩展代码",
        "进阶控制台命令。备注里写了使用方法，注意看清前提条件。",
        ["类别", "效果", "代码", "备注"], dedupe(rows))

    # ---- 组合代码（run 脚本）：人工排版，横向三组，需逐格扫 ----
    g = grid(wb["组合代码"], max_col=13)
    rows = []
    for row in g:
        note = ""
        for c in row:
            if not c:
                continue
            if c.startswith("备注"):
                note = c.replace("备注：", "").strip()
            elif c.startswith("run "):
                rows.append([note, c])
                note = ""
    add("console_run", "组合代码（run 脚本）",
        "把大段命令存成 txt 放进游戏根目录，控制台里用 run 调用。",
        ["说明", "命令"], dedupe(rows))

    # ---- 起源 ----
    g = grid(wb["起源"], max_col=6)
    h = header_row(g, "起源名称")
    add("origin", "起源", "开局起源。输入对应代码即可更换。",
        ["状态", "起源名称", "控制台代码"],
        dedupe([[r[0], r[3], r[5]] for r in g[h + 1:] if r[3] or r[5]]))

    # ---- 国策 ----
    g = grid(wb["国策"], max_col=9)
    h = header_row(g, "添加代码")
    add("civic", "国策",
        "国家国策。超过上限也能生效，但未满足前置条件的不会真正起作用。",
        ["类型", "名称", "添加代码", "移除代码", "key"],
        dedupe([[r[0], r[3], r[4], r[6], r[8]] for r in g[h + 1:]
                if r[3] or r[4] or r[8]]))

    # ---- 传统 ----
    g = grid(wb["传统"], max_col=10)
    rows = []
    for r in g[3:]:
        vals = [c for c in r if c]
        if vals:
            rows.append([vals[0], " | ".join(vals[1:])])
    add("tradition", "传统",
        "传统树与代码。代码生成的传统可超过 7 传统上限，但不显示实际效果。",
        ["定位", "内容"], dedupe(rows))

    # ---- 飞升 ----
    g = grid(wb["飞升"], max_col=6)
    h = header_row(g, "名称")
    add("ascension", "飞升天赋", "飞升天赋。添加/移除都用一整套 effect 代码。",
        ["序号", "名称", "自适应代码", "key", "备注"],
        dedupe([[r[0], r[2], r[3], r[4], r[5]] for r in g[h + 1:]
                if r[2] or r[3] or r[4]]))

    # ---- 物种特质 ----
    g = grid(wb["物种特质"], max_col=12)
    h = header_row(g, "特质名称")
    add("trait_species", "物种特质",
        "人口特质。注意「人口ID」——先选中人口，或直接填 ID。",
        ["类别", "特质名称", "自适应代码", "效果", "特质代码"],
        dedupe([[r[0], r[3], r[5], r[7], r[8]] for r in g[h + 1:]
                if r[3] or r[8]]))

    # ---- 科技 ----
    g = grid(wb["科技"], max_col=14)
    h = header_row(g, "名称")
    add("tech", "科技", "科技代码。用 research_technology 加单个科技。",
        ["类别", "名称", "代码", "等级", "key", "属性"],
        dedupe([[r[0], r[3], r[5], r[6], r[7], r[8]] for r in g[h + 1:]
                if r[3] or r[5]]))

    # ---- 遗珍 ----
    g = grid(wb["遗珍"], max_col=9)
    h = header_row(g, "名称")
    add("relic", "遗珍", "遗珍。用 add_relic 获得，effect remove_relic 移除。",
        ["序号", "名称", "代码", "被动效果", "激活花费", "激活效果", "获取来源"],
        dedupe([[r[0], r[2], r[4], r[5], r[6], r[7], r[8]] for r in g[h + 1:]
                if r[2] or r[4]]))

    # ---- 异常 ----
    g = grid(wb["异常"], max_col=6)
    h = header_row(g, "等级")
    add("anomaly", "异常点", "异常点。用 add_anomaly 添加。",
        ["序号", "名称", "等级", "出现地点", "添加代码", "备注"],
        dedupe([[r[0], r[1], r[2], r[3], r[4], r[5]] for r in g[h + 1:]
                if r[1] or r[4]]))

    # ---- 事件 ----
    g = grid(wb["事件"], max_col=6)
    h = header_row(g, "名称")
    add("event", "事件", "事件。用 event 加事件 ID 触发。",
        ["类别", "名称", "代码"],
        dedupe([[r[0], r[2], r[3]] for r in g[h + 1:] if r[2] or r[3]]))

    # ---- 成就 ----
    g = grid(wb["成就"], max_col=6)
    h = header_row(g, "完成条件")
    add("achievement", "成就", "成就与快速完成办法。",
        ["序号", "名称", "完成条件", "快速完成办法"],
        dedupe([[r[0], r[1], r[3], r[4]] for r in g[h + 1:]
                if r[1] or r[3]]))

    # ---- 巨构 ----
    g = grid(wb["巨构"], max_col=10)
    add("megastructure", "巨构",
        "巨构相关代码。多数需要先选中恒星或星球。",
        ["内容"], dedupe([[c] for r in g[3:] for c in r if c]))

    # ---- 行星矿藏 ----
    g = grid(wb["行星矿藏"], max_col=11)
    h = header_row(g, "名称")
    rows, cur = [], ""
    for r in g[h + 1:]:
        if r[0] and not r[1] and not r[2]:
            cur = r[0]          # 分类标题行
            continue
        if not (r[2] or r[4] or r[9]):
            continue
        rows.append([cur, r[0], r[2], r[3], r[4], r[9], r[10]])
    add("deposit", "行星矿藏", "星球矿藏（deposit）。用 effect add_deposit 添加。",
        ["类别", "序号", "名称", "效果", "自适应代码", "key", "备注"],
        dedupe(rows))

    # ---- 全局修正 ----
    g = grid(wb["全局修正"], max_col=12)
    h = header_row(g, "自适应代码")
    rows, cur = [], ""
    for r in g[h + 1:]:
        if r[0] and not r[1] and not r[2]:
            cur = r[0]
            continue
        if not (r[1] or r[2]):
            continue
        rows.append([cur, r[0], r[1], r[2], r[6], r[7], r[8]])
    add("modifier", "全局修正",
        "全局/国家/星球修正。生效位置随选中的物体而定。",
        ["类别", "序号", "名称", "自适应代码", "修正效果", "修正描述", "key"],
        dedupe(rows))

    # ---- 舰船组件 ----
    g = grid(wb["舰船组件"], max_col=20)
    h = header_row(g, "代码")
    add("component", "舰船组件",
        "舰船组件数据。代码即组件 key，可用于组件相关命令。",
        [f"c{i}" for i in range(18)],
        dedupe([[c for c in r[:18]] for r in g[h + 1:] if r[1] or r[2]]))

    meta = {
        "source": SRC.name,
        "version": "4.2.4",
        "updated": "2026-01-10",
        "sections": len(S),
        "entries": sum(len(x["rows"]) for x in S),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"meta": meta, "sections": S},
                              ensure_ascii=False, separators=(",", ":")),
                   encoding="utf-8")

    print("已抽取：")
    for x in S:
        print(f"  {x['title']:22} {len(x['rows']):5} 行")
    print(f"\n合计 {meta['entries']} 条")
    print(f"输出 {OUT}  {OUT.stat().st_size / 1024:.0f} KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
