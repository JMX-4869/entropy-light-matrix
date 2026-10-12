"""补齐重建映射表后仍未翻译的少量键。

为什么会出现"没翻译的键"：`build_i18n_map.py` 每次重建都会重新分配
键名（`patch.x0057` 这种是按页面内顺序编的），而翻译结果是按**键名**
存的。所以扫描口径一变（比如换成 htmlscan 扫描器、多抓到几个文本节点），
键名就会错位，少量键找不到对应翻译。

这里做两件事：
1. 对已知的遗漏键补上英文（人工给出，术语与其它批次一致）
2. 把 JS 那批的「中文原文 → 英文」也合进主表

补齐后 `write_i18n_table.py` 就能正常工作。
"""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                              errors="replace")

MAP = ROOT / "tools" / "_i18n_map.json"
TRANS = ROOT / "tools" / "_i18n_trans.json"

# 重建映射表后键位变化导致的遗漏。键名 → 英文。
# 术语与 b1–b4 批次保持一致（"Actions" 是表格列头的通用说法；
# "Pick a category to start browsing." 承接上一键的 Category browsing）。
FILL = {
    "操作": "Actions",
    "选一个分类开始浏览。": "Pick a category to start browsing.",
}


def main() -> int:
    keys = json.loads(MAP.read_text(encoding="utf-8"))["keys"]
    trans = json.loads(TRANS.read_text(encoding="utf-8")) if TRANS.is_file() else {}

    # 1. 按**文本**补齐（键名会漂，文本不会）
    by_text = {}
    for src in ("js1", "js2"):
        p = ROOT / "tools" / f"_i18n_trans_{src}.json"
        if p.is_file():
            by_text.update(json.loads(p.read_text(encoding="utf-8")))

    filled = 0
    for k, v in keys.items():
        zh = v["zh"]
        if (trans.get(k) or "").strip():
            continue
        if zh in FILL:
            trans[k] = FILL[zh]
            filled += 1
        elif zh in by_text:
            trans[k] = by_text[zh]
            filled += 1
    print(f"  按文本补齐 {filled} 个键")

    # 2. 把 JS 中文字面量也作为键并入（源码里是 t('中文')，不带前缀）
    js_n = 0
    for zh, en in by_text.items():
        if zh and (en or "").strip() and not (trans.get(zh) or "").strip():
            trans[zh] = en
            js_n += 1
    print(f"  并入 JS 中文字面量键 {js_n} 个")

    missing = [k for k in keys if not (trans.get(k) or "").strip()]
    print(f"  仍未翻译: {len(missing)} 个")
    for k in missing[:8]:
        print(f"      {k} = {keys[k]['zh']!r}")

    TRANS.write_text(json.dumps(trans, ensure_ascii=False, indent=1),
                     encoding="utf-8")
    print(f"  已写入 {TRANS}（共 {len(trans)} 条）")
    return 0 if not missing else 1


if __name__ == "__main__":
    sys.exit(main())
