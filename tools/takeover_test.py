"""AI 接管链路测试（用假 AI，不消耗 token）。

验证：
1. 不开「允许关闭模组」时，方案里**一个模组都不会被建议关闭**
2. 开了之后，只有「本地求解器 + AI 同时判定」的模组才会进列表
3. 只诊断模式（两个开关都关）不改任何文件
4. 执行接管会真的写回，并给出「能否启动」的结论
5. 顺序变更数算得对
"""
import io
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
from sandbox import enter_sandbox            # noqa: E402

_SANDBOX = enter_sandbox(game_dir=True)
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                              errors="replace")

import os  # noqa: E402

from smd.ai import analyze as A  # noqa: E402
from smd.app import App  # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"  [{'OK' if ok else '!!'}] {name}" + (f" — {detail}" if detail else ""))


_REAL: dict = {}


def fake_analyze(app, *, kind, payload, should_stop=None, source_ref=""):
    """假 AI：回一条针对"本地求解器建议关闭的第一个模组"的结论。"""
    _REAL["payload_len"] = len(payload)
    _REAL["kind"] = kind
    # 从载荷里抠出一个本地建议关闭的模组名，好验证交集逻辑
    name = _REAL.get("first_rec_name") or ""
    issues = []
    if name:
        issues.append({
            "key": f"takeover:{name}", "title": f"{name} 与其他模组冲突",
            "severity": "high", "cause": "文件被覆盖且顺序靠前",
            "fix": "关闭或后置", "mods": [name], "target": name,
            "ai_patchable": False, "source": kind, "source_ref": source_ref,
        })
    # 再塞一条不可能命中的，确认交集过滤有效
    issues.append({
        "key": "takeover:nobody", "title": "某个不存在的模组",
        "severity": "low", "cause": "x", "fix": "y",
        "mods": ["__不存在的模组__"], "target": "__不存在的模组__",
        "ai_patchable": False, "source": kind, "source_ref": source_ref,
    })
    return {"ok": True, "summary": "测试用摘要", "issues": issues,
            "parsed": True}


def wait(app, timeout=600):
    t0 = time.time()
    while app.task.running and time.time() - t0 < timeout:
        time.sleep(0.3)
    return app.task.result, app.task.error


def main() -> int:
    print("=" * 70)
    print("AI 接管链路（假 AI）")
    print("=" * 70)
    A.analyze = fake_analyze          # 打桩

    from smd.modinfo import load_library
    from dataclasses import replace
    base = os.environ.get("_SMD_REAL_LOCALAPPDATA") or ""
    cfg = Path(base) / "StellarisModDoctor" / "config.json"
    try:
        real_user = json.loads(cfg.read_text(encoding="utf-8"))["paths"]["user_dir"]
    except Exception:
        real_user = ""

    # ---------------- 只诊断：两个开关都关 ----------------
    print("\n--- 1. 只诊断模式（不改顺序、不关模组）---")
    app = App()
    if real_user:
        app.settings.paths = replace(app.settings.paths, user_dir=real_user)
        app.library = load_library(app.settings.paths)
        app.load_mods()
    app.settings.ai.allow_send_mod_content = True   # 让假 AI 不被闸门拦

    repo = app.settings.paths.launcher_db
    before = repo.read_bytes() if repo.is_file() else b""
    app.start_ai_takeover(allow_disable=False, allow_reorder=False,
                          apply=False)
    res, err = wait(app)
    check("任务无异常", not err, str(err)[:80])
    check("拿到方案", isinstance(res, dict) and res.get("ok") is True,
          str(res)[:60] if res else "无")
    if isinstance(res, dict) and res.get("ok"):
        check("方案里没有建议关闭的模组", not res.get("disable"),
              f"{len(res.get('disable') or [])} 个")
        check("方案里顺序调整为 0", res.get("reorder_count") == 0,
              str(res.get("reorder_count")))
        check("深度解析确实索引了文件",
              (res.get("indexed_files") or 0) > 1000,
              f"{res.get('indexed_files')} 个文件")
        check("带上「能否启动」结论",
              isinstance(res.get("report"), dict)
              and "launch_ok" in res["report"])
        check("假 AI 收到了完整载荷", _REAL.get("payload_len", 0) > 500,
              f"{_REAL.get('payload_len')} 字符")
    after = repo.read_bytes() if repo.is_file() else b""
    check("只诊断模式没有写盘", before == after)

    # ---------------- 允许关闭：只取交集 ----------------
    print("\n--- 2. 允许关闭：只列'本地+AI 同时判定'的模组 ---")
    plans = app.solution.recommended_disable() if app.solution else []
    if plans:
        _REAL["first_rec_name"] = plans[0].name
        print(f"  （本地求解器建议关闭：{plans[0].name}）")
    app2 = App()
    if real_user:
        app2.settings.paths = replace(app2.settings.paths, user_dir=real_user)
        app2.library = load_library(app2.settings.paths)
        app2.load_mods()
    app2.settings.ai.allow_send_mod_content = True
    if real_user:
        app2.settings.paths = replace(app2.settings.paths, user_dir=real_user)
        app2.library = load_library(app2.settings.paths)
        app2.load_mods()
    app2.start_ai_takeover(allow_disable=True, allow_reorder=True, apply=False)
    res2, err2 = wait(app2)
    check("任务无异常", not err2, str(err2)[:80])
    if isinstance(res2, dict) and res2.get("ok"):
        dis = res2.get("disable") or []
        if plans:
            check("交集判定生效（只列双方都同意的）",
                  all(d["name"] != "__不存在的模组__" for d in dis),
                  f"{len(dis)} 个：{[d['name'][:18] for d in dis[:3]]}")
            check("本地建议的那个被列出来了",
                  any(d["name"] == plans[0].name for d in dis),
                  f"{len(dis)} 个")
        else:
            check("本地无建议时列表为空", not dis, f"{len(dis)} 个")
        check("每条都带 AI 理由与本地理由",
              all(d.get("reason") is not None
                  and d.get("local_reason") is not None for d in dis))

        # ---------------- 执行接管 ----------------
        print("\n--- 3. 执行接管（写回 + 启动判据）---")
        app2.settings.paths = replace(
            app2.settings.paths, user_dir=os.environ["SMD_SANDBOX_USER_DIR"])
        ok = app2.ai_takeover_apply()
        check("能启动执行任务", ok is True)
        res3, err3 = wait(app2)
        check("执行无异常", not err3, str(err3)[:80])
        if isinstance(res3, dict):
            rep = res3.get("report") or {}
            check("给出了能否启动的结论", "launch_ok" in rep, str(rep)[:60])
            check("执行结果里有启用模组数",
                  any("启用模组" in x for x in (rep.get("lines") or [])),
                  str(rep.get("lines"))[:80])
            check("写回的启用模组 > 0",
                  any("启用模组 0 个" not in x for x in (rep.get("lines") or []))
                  or rep.get("launch_ok") is True,
                  str(rep.get("lines"))[:80])
    else:
        check("拿到方案", False, str(res2)[:80])

    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print()
    print("=" * 70)
    print(f"结果：{passed}/{total} 通过")
    for n, ok, d in results:
        if not ok:
            print(f"  失败：{n} — {d}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
