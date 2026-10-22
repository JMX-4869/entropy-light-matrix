"""探明 gamestate 的结构，为状态层定位关键块。

149 MB 的 Clausewitz 文本，不能整块读进内存（Python 字符串会是
三倍以上），所以全部走流式扫描：逐行读，只在需要时缓存当前块的头部。

要回答的四个问题：

1. 玩家国家是哪个（``player`` 块里的 ``country``）
2. 国家块叫什么、怎么索引（``countries``? ``country=``?）
3. 星球块怎么组织，每颗星的建筑/区划存在哪
4. 资源收支从哪个块读（``budget`` / ``economy``）
"""
from __future__ import annotations

import io
import sys
import zipfile
from pathlib import Path

OUT = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

SAVE = Path(r"C:\Users\asus\Documents\Paradox Interactive\Stellaris"
            r"\save games\19_-1712208651\jjj.sav")

# 想定位的顶层块名
WANT = ("player", "planets", "planet", "countries", "country", "budget",
        "economy", "galactic_object", "fleets", "ships", "pop", "meta")


def main() -> int:
    with zipfile.ZipFile(SAVE) as z:
        raw = z.open("gamestate")
        # 逐行流式，不做任何整体 decode
        text = io.TextIOWrapper(raw, encoding="utf-8", errors="replace")

        print(f"  文件: {SAVE.name}", file=OUT)
        hits: dict[str, list[int]] = {}
        total = 0
        depth0 = 0          # 当前还没闭合的顶层块数
        depth = 0
        ctx: list[str] = []  # 最近几行，用于定位 player 的内容

        for lineno, line in enumerate(text, 1):
            total = lineno
            s = line.strip()
            if not s:
                continue
            # 顶层块：行首无缩进且形如 `xxx = {`
            if line[:1] not in (" ", "\t") and "=" in s and s.endswith("{"):
                name = s.split("=")[0].strip()
                if name in WANT and len(hits.get(name, [])) < 3:
                    hits.setdefault(name, []).append(lineno)
                depth0 += 1
            depth0 += line.count("{") - line.count("}")
            # player 块内部内容抓一小段
            if 100 <= lineno <= 140:
                ctx.append(f"{lineno}: {s[:88]}")

        print(f"  总行数: {total:,}", file=OUT)
        print("", file=OUT)
        print("  关键块首次出现位置:", file=OUT)
        for k in WANT:
            v = hits.get(k)
            if v:
                print(f"    {k:18} 行 {', '.join(str(x) for x in v)}", file=OUT)
            else:
                print(f"    {k:18} 没找到", file=OUT)
        print("", file=OUT)
        print("  player 附近内容（行 100-140）:", file=OUT)
        for line in ctx[:20]:
            print("    " + line, file=OUT)
    OUT.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
