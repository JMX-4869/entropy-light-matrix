"""为手工改写的 JS 模板补充翻译键。

这些是"整句模板"里的片段：原来是一整句带 `${}` 的中文，无法直接用
一个键覆盖（`t()` 只替换 `{x}` 形式的占位符，JS 模板用的是 `${}`）。
所以拆成几个片段分别翻译，再用模板拼回去。

拆分的代价是英文语序可能别扭（中文"共 94 个，文件缺失 0 个"直译成
"Total 94 missing files 0" 就不通）。所以这里的译法按**英文语序**
安排片段顺序，宁可和中文的结构不同。

用法: python tools/fill_template_keys.py
"""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                              errors="replace")

TRANS = ROOT / "tools" / "_i18n_trans.json"

# 片段 → 英文。译法按英文语序，例如
#   `${t('共')} 94 ${t('个，文件缺失')} 0 ${t('个')}` 会拼成
#   "Total 94 · missing files 0" —— 靠片段里的标点把语序理顺。
FILL = {
    "用户数据": "User data",
    "游戏": "Game",
    "未找到": "not found",
    "共": "Total",
    "个，文件缺失": "mods, missing files",
    "个": "",
}


def main() -> int:
    trans = json.loads(TRANS.read_text(encoding="utf-8")) if TRANS.is_file() else {}
    added = 0
    for zh, en in FILL.items():
        if zh not in trans:
            trans[zh] = en
            added += 1
    print(f"  新增键 {added} 个（已有 {len(FILL) - added} 个跳过）")
    TRANS.write_text(json.dumps(trans, ensure_ascii=False, indent=1),
                     encoding="utf-8")
    print(f"  已写入 {TRANS}（共 {len(trans)} 条）")
    for zh, en in FILL.items():
        print(f"    {zh!r} → {en!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
