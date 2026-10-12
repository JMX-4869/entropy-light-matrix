"""修 `renderTask(t)` 的参数名遮蔽翻译函数 `t`。

问题：函数签名是 `function renderTask(t)`，参数名叫 `t`，
把全局的翻译函数 `t` **遮蔽**了。于是函数体里所有想调用翻译的地方
实际都在访问 task 对象：

    $('tpMsg').textContent = t.message || t('处理中…');
    //                       ↑ task.message   ↑ 这里在把 task 当函数调用

`t('处理中…')` 抛 `TypeError: t is not a function`，被包在 try 里
或者被后续逻辑吞掉，表现为「任务栏提示不更新」——
`task_test.py` 的「点击后立刻给出反馈」就是这么红的：
点停止后 `tpMsg` 始终停在静态默认值「准备中…」。

修法：参数改名为 `task`，函数体里所有 `t.xxx` 改成 `task.xxx`，
原本想调翻译的地方自然恢复成调用全局 `t()`。

同时给函数体内的界面文字补上 `t()` —— 它们原先都是硬编码中文。
"""
from __future__ import annotations

import io
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                              errors="replace")

INDEX = ROOT / "smd" / "web" / "index.html"
TRANS = ROOT / "tools" / "_i18n_trans.json"
Q = "'"

# renderTask 内出现的界面文字 → 英文
EN = {
    "任务失败": "Task failed",
    "已停止": "Stopped",
    "任务": "Task",
    "处理中…": "Working…",
    "这个任务不能中途停止（会写坏文件），请等它跑完":
        "This task cannot be stopped midway (it writes files); let it finish",
    "正在停止…": "Stopping…",
    "停止当前任务": "Stop the current task",
    "停止": "Stop",
    "失败": "Failed",
    "完成": "Done",
}


def main() -> int:
    html = INDEX.read_text(encoding="utf-8")

    # 取出整个 renderTask 函数体
    m = re.search(r"function renderTask\(t\)\{", html)
    if not m:
        print("  ！找不到 function renderTask(t){")
        return 1
    depth = 1
    i = m.end()
    while i < len(html) and depth:
        if html[i] == "{":
            depth += 1
        elif html[i] == "}":
            depth -= 1
        i += 1
    body = html[m.end():i]

    orig = body
    # 1. 参数改名：函数体内所有 `t.属性` → `task.属性`
    body = re.sub(r"\bt\.(running|message|error|cancelled|progress|name|"
                  r"elapsed|indeterminate|cancellable|stage)\b",
                  r"task.\1", body)
    # 2. `t.name ? t(t.name) : ''` 里的 `t(` 也要改成 task(
    body = body.replace("task.name ? t(task.name)", "task.name ? t(task.name)")
    # 3. 函数签名
    html = (html[:m.start()] + "function renderTask(task){" + body
            + html[i:])

    # 4. 给函数体内的界面文字包 t()
    for zh in sorted(EN, key=len, reverse=True):
        for pat in (f"'{zh}'", f'"{zh}"'):
            if pat in body:
                html = html.replace(
                    "function renderTask(task){" + body,
                    "function renderTask(task){"
                    + body.replace(pat, f"t({pat})"), 1)
                body = body.replace(pat, f"t({pat})")
    print(f"  参数改名 + 界面文字包 t() 完成（原 {len(orig)} 字符 → {len(body)}）")

    # 5. 校验
    parts = re.findall(r"<script\b[^>]*>(.*?)</script>", html, re.S)
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                     encoding="utf-8", newline="\n") as fh:
        fh.write("\n;\n".join(parts))
        tmp = fh.name
    r = subprocess.run(["node", "--check", tmp], capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    Path(tmp).unlink(missing_ok=True)
    if r.returncode != 0:
        print(f"  ！语法不合法，已放弃\n    {(r.stderr or '')[:300]}")
        return 1
    print("  脚本语法 OK")

    # 6. 补翻译
    d = json.loads(TRANS.read_text(encoding="utf-8")) if TRANS.is_file() else {}
    added = sum(1 for k, v in EN.items() if k not in d)
    d.update({k: v for k, v in EN.items() if k not in d})
    TRANS.write_text(json.dumps(d, ensure_ascii=False, indent=1),
                     encoding="utf-8")
    print(f"  翻译补充 {added} 条")

    INDEX.write_text(html, encoding="utf-8", newline="\n")
    print(f"  已写入 {INDEX}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
