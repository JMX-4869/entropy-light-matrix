"""探测：能不能从 240 MB 的 gamestate 里准确取出国家状态。

**为什么不是先写完整解析器**：gamestate 是 2.5 亿字符的 Clausewitz 文本，
先从里面捞出"我这个国家现在什么情况"这一层，验证可行性再扩。

要确认的三件事：
1. 能不能定位到**玩家自己的国家**（不是随便哪个 AI 国家）
2. 能不能读到**资源收支**（月入/月出 —— "不赤字"要靠它）
3. 能不能读到**星球与建筑**（自动规划建筑要靠它）

读法：流式扫描。2.5 亿字符全读进 Python str 也要 500 MB 左右，
再用 splitlines 会翻倍 —— 所以按块读、按行处理。
"""
from __future__ import annotations

import io
import json
import re
import sys
import time
import zipfile
from pathlib import Path

out = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

SAVE_DIR = Path.home() / "Documents" / "Paradox Interactive" / "Stellaris" / "save games"


def newest_save() -> Path | None:
    """最近改动的存档。"""
    files = list(SAVE_DIR.glob("*/*.sav"))
    if not files:
        return None
    return max(files, key=lambda p: p.stat().st_mtime)


def scan(path: Path) -> dict:
    """扫一遍 gamestate，捞出玩家国家与资源。

    只做**一次线性扫描**，边读边找关键块 —— 不建完整语法树。
    240 MB 建树会吃掉几个 G，而我们要的信息其实很局部。
    """
    info: dict = {"file": path.name, "size_mb": path.stat().st_size / 1048576}
    t0 = time.time()

    with zipfile.ZipFile(path) as z:
        name = next((n for n in z.namelist() if "gamestate" in n), None)
        if not name:
            return {"error": "包里没有 gamestate"}
        with z.open(name) as f:
            text = io.TextIOWrapper(f, encoding="utf-8-sig", errors="replace")
            # 扫头部：版本、日期、玩家国家 id
            head_lines = []
            player_tag = ""
            player_id = ""
            for _ in range(4000):
                line = text.readline()
                if not line:
                    break
                head_lines.append(line)
                if "player=" in line:
                    m = re.search(r"player=\s*\{?\s*(\d+)?", line)
                    if m and m.group(1):
                        player_id = m.group(1)
                if player_tag == "" and re.match(r"\s*name=", line):
                    pass
            head = "".join(head_lines)

            # 玩家国家：Clausewitz 存档里通常有 `player=` 指向 country id
            info["head_sample"] = head[:400]
            info["player_id_from_head"] = player_id

            # 找 `countries={` 块的开头位置（用 readline 数行号）
            # 简化：全量扫一遍找几个标志
            text.seek(0)
            # zipfile 的流不支持 seek，重新打开
        # ── 第二遍：线性找关键行
        with z.open(name) as f:
            text = io.TextIOWrapper(f, encoding="utf-8-sig", errors="replace")
            counters = {"countries": 0, "planets": 0, "fleet": 0,
                        "budget": 0, "research": 0}
            samples: dict[str, list[str]] = {k: [] for k in counters}
            line_no = 0
            cur_country = ""
            for line in text:
                line_no += 1
                s = line.strip()
                if s.startswith("countries={"):
                    counters["countries"] = line_no
                elif s.startswith("planets={"):
                    counters["planets"] = line_no
                elif s.startswith("fleet={"):
                    counters["fleet"] = line_no
                # 玩家国家标记
                if "player=yes" in s or s == "player=yes":
                    info.setdefault("player_marks", []).append(line_no)
                # 取几段样
                low = s.lower()
                for key in ("budget", "research", "country_resource",
                            "monthly_income", "max_resources"):
                    if key in low and len(samples["budget"]) < 6:
                        if s not in samples["budget"]:
                            samples["budget"].append(s[:120])
            info["line_count"] = line_no
            info["block_starts"] = counters
            info["samples"] = samples

    info["elapsed_s"] = round(time.time() - t0, 1)
    return info


def main() -> int:
    save = newest_save()
    if not save:
        print(f"  ！没找到存档：{SAVE_DIR}", file=out)
        return 1
    print(f"  存档: {save.parent.name}/{save.name}", file=out)
    info = scan(save)
    print("", file=out)
    for k, v in info.items():
        if k == "head_sample":
            print("  ── 头部 ──", file=out)
            for line in str(v).splitlines()[:8]:
                print("   " + line[:92], file=out)
        elif k == "samples":
            print("  ── 资源相关行的样本 ──", file=out)
            for s in v.get("budget", [])[:6]:
                print("   " + s, file=out)
        elif isinstance(v, list):
            print(f"  {k}: {v[:8]}", file=out)
        elif isinstance(v, dict):
            print(f"  {k}: {json.dumps(v, ensure_ascii=False)[:200]}", file=out)
        else:
            print(f"  {k}: {v}", file=out)
    out.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
