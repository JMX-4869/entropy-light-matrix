"""给「动态设置文本」元素的静态默认值包上 `t()`。

背景：这些元素去掉了 `data-i18n`（否则 applyI18n 会把运行时内容
重置回静态值）。代价是它们的静态默认值会原样显示 —— 在 JS 覆盖它之前，
英文用户会看到中文。

修法：把默认值包成 `t('…')`。脚本在 i18n.js 之后解析，此时 `t` 已可用，
所以 `<div id="dCrit">${t('尚未检测')}</div>` 这类占位会按当前语言出现。

用 t() 包的是**默认/占位文本**，不是运行时内容 —— 后者仍由
render* 函数用 t() 设置，两者不冲突。
"""
from __future__ import annotations

import io
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                              errors="replace")

INDEX = ROOT / "smd" / "web" / "index.html"
TRANS = ROOT / "tools" / "_i18n_trans.json"
Q = "'"

# 元素 id → 默认文本（与 check_dynamic_text.py 报出的一致）
FIX = {
    "dCrit": "尚未检测",
    "wikiHit": "输入关键词",
    "ftState": "就绪",
    "mkTitle": "提示",
}

# 这几个默认值的英文（术语与其它批次一致）
EN = {
    "尚未检测": "Not scanned yet",
    "输入关键词": "Enter a keyword",
    "就绪": "Ready",
    "提示": "Notice",
}


def main() -> int:
    html = INDEX.read_text(encoding="utf-8")
    fixed = 0
    for eid, default in FIX.items():
        # 该元素的静态文本紧跟在开标签之后
        pat = re.compile(
            r'(<[^>]*\bid="' + re.escape(eid) + r'"[^>]*>)(\s*)'
            + re.escape(default))
        new = (r'\1' + r'\2' +
               "${t(" + Q + default + Q + ")}")
        html, n = pat.subn(new, html, count=1)
        if n:
            fixed += n
            print(f"  {eid}: 默认值已包 t()")
        else:
            print(f"  {eid}: 未命中（默认文本可能已变）")
    print(f"  共修 {fixed} 处")

    # 补翻译
    d = json.loads(TRANS.read_text(encoding="utf-8")) if TRANS.is_file() else {}
    added = 0
    for zh, en in EN.items():
        if zh not in d:
            d[zh] = en
            added += 1
    TRANS.write_text(json.dumps(d, ensure_ascii=False, indent=1),
                     encoding="utf-8")
    print(f"  翻译补充 {added} 条")

    INDEX.write_text(html, encoding="utf-8", newline="\n")
    print(f"  已写入 {INDEX}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
