"""补齐界面里最后这些散落的界面文字翻译。

来源：模组列表的状态标签（汉化/修复/前置/缺文件/有问题/启用/未启用/
已改/不可用）、列表空态、以及各处提示。这些原先硬编码在
innerHTML 模板里，已包上 t()，这里给出对应英文。

术语与既有批次一致（translation mod / fix / dependency / missing files…）。
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

FILL = {
    # ---- 模组列表状态标签（每个模组都会显示，影响面最大）----
    "汉化": "Translation",
    "修复": "Fix",
    "前置": "Dependency",
    "缺文件": "Missing files",
    "有问题": "Problem",
    "启用": "Enabled",
    "未启用": "Disabled",
    "已改": "Changed",
    "不可用": "Unusable",
    "点击关闭": "Click to disable",
    "点击启用": "Click to enable",
    "磁盘上没有内容，无法启用": "No content on disk; cannot be enabled",
    "没有匹配的模组": "No matching mods",
    # ---- 其它零散提示 ----
    "秒": " s",
    "未找到": "not found",
    "已隐藏": "Hidden",
    "清理残留（可恢复）": "Clean up leftovers (recoverable)",
    "查看残留": "View leftovers",
}


def main() -> int:
    trans = json.loads(TRANS.read_text(encoding="utf-8")) if TRANS.is_file() else {}
    added = skipped = 0
    for zh, en in FILL.items():
        if zh in trans:
            skipped += 1
            continue
        trans[zh] = en
        added += 1
    print(f"  新增 {added} 个，已存在跳过 {skipped} 个")
    TRANS.write_text(json.dumps(trans, ensure_ascii=False, indent=1),
                     encoding="utf-8")
    print(f"  已写入 {TRANS}（共 {len(trans)} 条）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
