"""补齐任务名与最后一批界面文字的英文。

任务名来自 Python（`run_async("扫描与冲突检测", …)`），
JS 侧现在会过一层 `t()`，所以只要表里有对应英文就能跟随语言。

术语与前几批一致：scan / conflict detection / Best Plan / load order /
AI Takeover / collision …
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
    # ---- Python 侧的任务名（进度条与任务栏显示）----
    "扫描与冲突检测": "Scanning & conflict detection",
    "扫描成就影响": "Scanning achievement impact",
    "联机校验排查": "Checksum check",
    "计算最优方案": "Computing best plan",
    "应用最优方案": "Applying best plan",
    "应用手动排序": "Applying manual order",
    "一键最优处理": "One-Click Optimize",
    "一键翻译": "One-Click Translation",
    "AI 接管": "AI Takeover",
    "执行 AI 接管": "Running AI Takeover",
    "AI 分析崩溃原因": "AI crash analysis",
    "AI 分析日志": "AI log analysis",
    "AI 分析加载顺序": "AI load-order analysis",
    "AI 生成补丁": "AI patch generation",
    "批量生成补丁": "Batch patch generation",
    "生成崩溃报告": "Generating crash report",
    "写入模组开关": "Writing mod switches",
    "写入 DLC 开关": "Writing DLC switches",
    "任务": "Task",
    # ---- 最后几处界面文字 ----
    "模组 (.mod) 目录": "Mod (.mod) directory",
    "秒": " s",
}


def main() -> int:
    d = json.loads(TRANS.read_text(encoding="utf-8")) if TRANS.is_file() else {}
    added = 0
    for k, v in FILL.items():
        if k not in d:
            d[k] = v
            added += 1
    print(f"  新增 {added} 条（已存在 {len(FILL) - added} 条跳过）")
    TRANS.write_text(json.dumps(d, ensure_ascii=False, indent=1),
                     encoding="utf-8")
    print(f"  总计 {len(d)} 条")
    return 0


if __name__ == "__main__":
    sys.exit(main())
