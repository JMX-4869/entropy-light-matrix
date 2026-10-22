"""复现「开始 AI 接管」的崩溃，不真调 API。

用户报的错是 ``'str' object has no attribute 'name'``，但直接猜代码猜不出来
（``prompt.py``、``_ai_merge``、``_audit_collect`` 里都没有能收到 str 的
``.name``）。所以换个法子：把 ``A.analyze`` 打桩成返回各种**畸形响应**，
把第 1~4 步跑一遍，看哪一种能复现。

顺带把每一步的耗时打出来 —— 用户那边是 28.2 秒失败，
能对上某一步就说明离现场不远。
"""
from __future__ import annotations

import io
import sys
import time
import traceback

OUT = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"E:\yiuxi\StellarisModDoctor")
from tools.sandbox import enter_sandbox  # noqa: E402

enter_sandbox()
from smd.app import App  # noqa: E402
from smd.ai import analyze as A  # noqa: E402


# 候选响应：从"最像真的"到"最畸形"，逐个试
CASES = {
    "正常": {
        "ok": True, "summary": "没什么大问题",
        "issues": [{"key": "k1", "title": "测试", "mods": ["某个模组"],
                    "severity": "low", "cause": "测试"}],
    },
    "issues为空": {"ok": True, "summary": "", "issues": []},
    "issues是字符串": {"ok": True, "summary": "", "issues": "oops"},
    "issue是字符串": {"ok": True, "summary": "", "issues": ["oops"]},
    "mods是字符串": {
        "ok": True, "summary": "",
        "issues": [{"key": "k1", "title": "t", "mods": "字符串不是列表"}],
    },
    "没有summary": {"ok": True, "issues": []},
}

app = App()
app.load_mods()
print(f"  模组 {len(app.mods_list())} 个\n", file=OUT)

for label, fake in CASES.items():
    started = time.time()
    A.analyze = lambda *a, **kw: fake      # noqa: E731  打桩
    try:
        ok = app.start_ai_takeover(allow_disable=True, allow_reorder=True,
                                   apply=False)
        if not ok:
            print(f"  [{label}] start 返回 False（任务被拒）", file=OUT)
            continue
        for _ in range(3000):
            if not app.task.running:
                break
            time.sleep(0.02)
        cost = time.time() - started
        err = (app.task.error or "").strip()
        if err:
            first = err.splitlines()[0]
            print(f"  ✗ [{label}] {cost:5.1f}s  {first}", file=OUT)
            # 有栈就打出来 —— 这才是要的
            if len(err.splitlines()) > 1:
                for line in err.splitlines()[1:9]:
                    print(f"        {line.strip()[:96]}", file=OUT)
            break
        print(f"  ✓ [{label}] {cost:5.1f}s  通过", file=OUT)
    except Exception:                          # noqa: BLE001
        print(f"  ✗ [{label}] 直接抛异常", file=OUT)
        traceback.print_exc(file=OUT)
        break
OUT.flush()
