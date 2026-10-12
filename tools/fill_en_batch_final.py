"""给最后一批 JS 界面文字的英文翻译。

来源：`tools/_need_en.json`（`wrap_static_js_text.py` 包了 t() 但表里没英文的）。

其中若干条是**拼接片段**（如 `将把当前` + `${n}` + `个模组标记为…`），
英文语序不同，所以给它们带上必要的首尾空格，让拼出来的句子通顺。
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
    # ---- 表格 / 卡片标题 ----
    "AI 分析结论": "AI findings",
    "兼容补丁": "Compatibility patch",
    "最终加载顺序": "Final load order",
    "求解说明": "Solver notes",
    "需要处理的错误": "Errors to fix",
    "高频但无害的噪音": "Frequent but harmless noise",
    "报错最多的源文件": "Source files with most errors",
    "可能的原因": "Possible causes",
    "崩溃现场": "Crash context",
    "建议的修复步骤": "Suggested fixes",
    "详细信息": "Details",
    "建议处理": "Suggested action",
    "相关模组": "Related mods",
    "崩溃现场".replace("现场", "上下文"): "Crash context",
    "等待事件": "Waiting for events",
    "这句话在说什么": "What this means",
    "为什么会这样": "Why this happens",
    "怎么办": "What to do",
    "原始日志": "Raw log",
    "处理": "Action",
    "调用栈": "Call stack",
    "问题": "Issue",
    "改动内容": "Changes",
    "状态": "Status",
    "文件": "File",
    "模组": "Mod",
    "路径": "Path",
    "允许": "Allow",
    "禁止": "Block",
    # ---- 确认对话框 ----
    "你即将确认关闭": "You are about to confirm disabling",
    "即将写入的改动：": "Changes to be written:",
    "即将写入": "About to write",
    "即将执行：": "About to run:",
    "会把所有模组标记为加载。": " will mark all mods as loaded.",
    "关闭全部 DLC 会显著改变游戏内容，且": "Disabling all DLC changes game content significantly, and",
    "你打开了「允许关闭模组」。这": "You enabled “Allow disabling mods”. This",
    # ---- 拼接片段（带空格）----
    "将把当前": "Will disable ",
    "将删除": "Will delete ",
    "这会把": "This will ",
    "将把选中的": "Will target the selected ",
    "会把": "Will ",
    "将对": "Will ",
    "将把": "Will ",
    "将启用": "Enable ",
    "将关闭": "Disable ",
    "这些目标的模组内容会": "The mod content of these targets will be ",
    "已挪": "Moved ",
    "已载入模板：": "Template loaded: ",
    "你打开了「允许关闭模组」。这": "You enabled “Allow disabling mods”. This",
    "其中 ": " of which ",
    "原因：": "Cause: ",
    "建议：": "Suggestion: ",
    "相关模组：": "Related mods: ",
    "定位：": "Target: ",
    "来源：": "Source: ",
    "AI 判断：": "AI verdict: ",
    "发现 ": "Found ",
    "你有 ": "You have ",
    "相关冲突（": "Related conflicts (",
    "目标：": "Target: ",
    "补丁模组名前缀为": "The patch mod name prefix is ",
    "✓ 从 ": "✓ From ",
    "✗ 没能从服务商拉到模型列表": "✗ Could not fetch the model list from the provider",
    # ---- 说明文字 ----
    "这份报告是 HTML 格式，点下方按钮用浏览器打开可以看完整排版。":
        "This report is HTML; open it in a browser with the button below "
        "to see the full layout.",
    "本地规则引擎不联网、不花钱。":
        "The local rule engine works offline and costs nothing.",
    "生成完还要": "After generating you still need to ",
    "没有建议关闭的模组": "No mods are recommended for disabling",
    "AI 正在思考…（先检索档案，再作答）":
        "AI is thinking… (searching the archive first)",
    "AI 正在写命令…": "AI is writing commands…",
    "加载顺序语义：": "Load order semantics: ",
    "需要先在「AI 接入」页完成配置。":
        "Configure it on the “AI Setup” page first.",
    "（无脚本文件；本地化冲突会直接给译文对照）":
        "(no script files; localisation conflicts are shown side by side)",
    "勾选左侧滑块选择要加载的模组。":
        "Tick the switches on the left to choose which mods load.",
    "补丁无法解决": "Cannot be fixed by a patch",
    "会自动创建为独立模组": "will be created as a separate mod automatically",
    "不用管它们": "You can ignore them",
    "影响存档兼容性": "Affects save compatibility",
    "发送给 AI 服务商": "Sent to the AI provider",
    "保存成模组": "Save as mod",
    "环境存在问题（部分功能不可用）":
        "Environment has problems (some features unavailable)",
    "不会立刻关掉任何东西": "will not disable anything immediately",
    "模组开关完全由你决定。": "Mod switches are entirely your decision.",
    "「允许把模组内容发送给 AI」尚未开启。":
        "“Allow sending mod content to AI” is not enabled yet.",
    "这就是将要发送的全部内容": "This is everything that will be sent",
    "✓ 校验通过": "✓ Validation passed",
    "✓ 通过校验": "✓ Passed validation",
    "✓ 连接成功": "✓ Connected",
    "✗ 连接失败": "✗ Connection failed",
    "数字越大越晚加载": "Higher numbers load later",
    # ---- 带变量的片段 ----
    "预设：$": "Preset: ",
    "将关闭：$": "Disable: ",
    "将启用：$": "Enable: ",
    "交叉验证：$": "Cross-check: ",
    "已索引 $": "Indexed ",
}


def main() -> int:
    d = json.loads(TRANS.read_text(encoding="utf-8")) if TRANS.is_file() else {}
    added = 0
    for k, v in FILL.items():
        if k not in d:
            d[k] = v
            added += 1
    TRANS.write_text(json.dumps(d, ensure_ascii=False, indent=1),
                     encoding="utf-8")
    print(f"  新增 {added} 条，总计 {len(d)} 条")

    need = json.loads((ROOT / "tools" / "_need_en.json").read_text(
        encoding="utf-8")) if (ROOT / "tools" / "_need_en.json").is_file() else []
    # 扫描器有时会在末尾多带一个 `$`（正则边界残留），
    # 比对时按"剥掉末尾 $ 后的文本"再查一次，避免误报缺失。
    miss = []
    for s in need:
        if s in d:
            continue
        if s.endswith("$") and s[:-1].strip() in d:
            continue
        miss.append(s)
    print(f"  _need_en 里仍缺 {len(miss)} 条")
    for s in miss[:12]:
        print(f"    {s!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
