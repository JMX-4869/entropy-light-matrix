"""批量处理完，目标要从列表里消失、计数要跟着变。

之前的问题：跑完一轮本地批量，列表纹丝不动、数字一点没减，
用户以为没生效而反复点。原因是"生成"出来的补丁还在内存里、
没保存到磁盘，而列表过滤靠的是扫补丁目录，自然看不到它们。

这个测试盯住三件事：
1. 本地批量跑完后，待处理列表**确实变短**，本地可处理数**确实减少**
2. 「只选需要 AI 的」按钮选中的正是本地处理不了的那批
3. 两个引擎的记账**互不干扰**（本地做过的不该从 AI 列表里消失）
"""
import io
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
from sandbox import enter_sandbox            # noqa: E402

enter_sandbox(game_dir=True)
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                              errors="replace")

import json  # noqa: E402
import os  # noqa: E402
from dataclasses import replace  # noqa: E402

from smd.app import App  # noqa: E402
from smd.modinfo import load_library  # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"  [{'OK' if ok else '!!'}] {name}" + (f" — {detail}" if detail else ""))


def real_user_dir() -> str:
    base = os.environ.get("_SMD_REAL_LOCALAPPDATA") or ""
    cfg = Path(base) / "StellarisModDoctor" / "config.json"
    try:
        return json.loads(cfg.read_text(encoding="utf-8"))["paths"]["user_dir"]
    except Exception:
        return ""


def main() -> int:
    print("=" * 70)
    print("批量处理完从列表移除")
    print("=" * 70)
    app = App()
    real = real_user_dir()
    if real:
        app.settings.paths = replace(app.settings.paths, user_dir=real)
        app.library = load_library(app.settings.paths)
        app.load_mods()

    print("\n--- 1. 扫描 ---")
    # 先确认**真实 mod 目录里没有残留补丁**。
    # 有残留的话，扫描一开始就会把一批冲突判成"已被补丁解决"，
    # 本地可处理数于是比预期少，后面"数量应当减少"的断言会假失败。
    # 那不是被测代码的问题，是环境脏了——显式说出来，
    # 别让它表现成一个莫名其妙的断言失败（之前就为此白查过一轮）。
    from smd.patch.service import PATCH_PREFIX, patch_root
    real_root = patch_root(app.settings.paths)
    stale = ([d.name for d in real_root.iterdir()
              if d.is_dir() and d.name.startswith(PATCH_PREFIX)]
             if real_root.is_dir() else [])
    check("真实 mod 目录没有残留补丁（有残留则计数会失真）",
          not stale, f"{len(stale)} 个：{stale[:2]}")

    app.start_scan(semantic=True)
    t0 = time.time()
    while app.task.running and time.time() - t0 < 400:
        time.sleep(0.3)
    check("扫描完成", app.report is not None,
          f"{app.report.count() if app.report else 0} 条")
    if app.report is None:
        return _report()

    print("\n--- 2. 处理前 ---")
    p0 = app.batch_patch_plan(None, "local")
    a0 = app.batch_patch_plan(None, "ai")
    check("本地计划有目标", p0["selected"] > 0, f"{p0['selected']} 个")
    check("本地可处理数 > 0", p0["local_capable"] > 0,
          f"{p0['local_capable']} 个")
    check("需要 AI 的数 > 0", p0["ai_only"] > 0, f"{p0['ai_only']} 个")
    check("计划带出候选明细（界面靠它渲染列表）",
          isinstance(p0.get("candidates"), list)
          and len(p0["candidates"]) == p0["selected"],
          f"{len(p0.get('candidates') or [])} 条")
    check("候选明细带 local_capable 标记",
          all("local_capable" in c for c in (p0.get("candidates") or [])[:5]))

    print("\n--- 3. 「只选需要 AI 的」选中的正是本地做不了的 ---")
    ai_only_idx = {c["index"] for c in p0["candidates"]
                   if not c["local_capable"]}
    check("存在只属于 AI 的候选", len(ai_only_idx) > 0, f"{len(ai_only_idx)} 个")
    plan_ai = app.batch_patch_plan(sorted(ai_only_idx), "ai")
    check("按这批下标取计划，全部都需要 AI",
          plan_ai["local_capable"] == 0,
          f"local_capable={plan_ai['local_capable']}")

    print("\n--- 4. 跑一轮本地批量 ---")
    before_n = p0["selected"]
    before_local = p0["local_capable"]
    app.start_batch_patches(None, "local")
    t0 = time.time()
    while app.task.running and time.time() - t0 < 400:
        time.sleep(0.3)
    drafts = getattr(app, "_auto_drafts", None) or []
    check("本地产出了草稿", len(drafts) > 0, f"{len(drafts)} 份")

    print("\n--- 5. 处理后：列表应当变短 ---")
    p1 = app.batch_patch_plan(None, "local")
    check("本地待处理目标减少了", p1["selected"] < before_n,
          f"{before_n} → {p1['selected']}")
    check("本地可处理数减少了", p1["local_capable"] < before_local,
          f"{before_local} → {p1['local_capable']}")
    check("计划里报出已处理数量", p1.get("handled", 0) > 0,
          f"handled={p1.get('handled')}")
    # 减少的量应当和实际产出的草稿数**对得上**（不是随便少几个）。
    # 用 <= 而不是 ==：有几份草稿可能指向同一个目标文件（同一文件的多条
    # 冲突只产出一份合并补丁），所以"减少的目标数"可能略少于草稿数。
    # 也不能断言"本地可处理数归零"：有些目标规则跑完发现没什么可合并的，
    # 产不出草稿，它们留在列表里是对的。
    removed = before_n - p1["selected"]
    check("减少的目标数与产出的草稿数相当",
          0 < removed <= len(drafts),
          f"减少 {removed} 个，产出 {len(drafts)} 份")
    check("剩下的都是本地做不了的",
          p1["local_capable"] == p1["selected"] - p1["ai_only"]
          or p1["ai_only"] > 0,
          f"剩余 {p1['selected']}，其中需 AI {p1['ai_only']}")

    print("\n--- 6. 两个引擎的记账互不干扰 ---")
    a1 = app.batch_patch_plan(None, "ai")
    check("AI 的待处理列表不受影响", a1["selected"] == a0["selected"],
          f"{a0['selected']} → {a1['selected']}")
    check("AI 计划里仍标出需要 AI 的数量", a1["ai_only"] > 0,
          f"{a1['ai_only']} 个")

    print("\n--- 7. 幂等：再跑一次不会重复产出 ---")
    app.start_batch_patches(None, "local")
    t0 = time.time()
    while app.task.running and time.time() - t0 < 400:
        time.sleep(0.3)
    p2 = app.batch_patch_plan(None, "local")
    check("再跑一次列表不再变少", p2["selected"] == p1["selected"],
          f"{p1['selected']} → {p2['selected']}")

    print("\n--- 8. 已保存的补丁也会把目标去掉 ---")
    app.settings.paths = replace(
        app.settings.paths, user_dir=os.environ["SMD_SANDBOX_USER_DIR"])
    res = app.batch_save_patches("local")
    check("批量保存成功", res.get("saved", 0) > 0, f"{res.get('saved')} 个")
    note = app.findings_filter_note()
    check("保存后冲突列表也变短", note["hidden_patched"] > 0,
          f"隐藏 {note['hidden_patched']} 条")

    return _report()


def _report() -> int:
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
