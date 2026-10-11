"""验证「模组开关必须由用户决定」这条设计约束。

核心断言：

1. 求解器产生禁用建议时，``approved`` 必须全部为 ``False``
2. 未确认时 ``approved_disable()`` 为空，``apply`` 不会关闭任何模组
3. 用户点了「关闭」后，只有被点过的那个进入 ``approved_disable()``
4. 一键优化不会自动确认任何建议
5. 写入后 playset 里被关掉的模组数量 == 用户确认的数量

用法::

    python tools/consent_test.py            # 只验证 1~4（不改动真实配置）
    python tools/consent_test.py --write    # 追加验证 5（写一次再回滚）
"""

from __future__ import annotations

import io
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from smd.app import App  # noqa: E402
from smd.apply import (apply_solution, launcher_running, read_playset_state,  # noqa: E402
                       rollback)

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"  [{'OK' if ok else '!!'}] {name}" + (f" — {detail}" if detail else ""))


def main() -> int:
    do_write = "--write" in sys.argv
    print("=" * 64)
    print("「模组开关由用户决定」约束验证")
    print("=" * 64)

    app = App()
    app.load_mods()
    print("跑检测与求解…")
    app.one_click_optimize(apply=False)
    while app.task.running:
        time.sleep(0.2)
    if app.task.error:
        print("任务失败：", app.task.error[:400])
        return 2

    sol = app.solution
    assert sol is not None

    # --- 1. 建议默认全部未确认 -----------------------------------
    dis = sol.disable_plans()
    print(f"\n求解器给出 {len(dis)} 条禁用建议")
    for p in dis:
        print(f"    · {p.name}　[{p.reason[:46]}]　"
              f"needs_decision={p.needs_decision} approved={p.approved}")
    check("建议存在时全部标记为待决策",
          all(p.needs_decision for p in dis) if dis else True,
          f"{len(dis)} 条")
    check("建议默认全部未确认（approved 全为 False）",
          all(not p.approved for p in dis) if dis else True)
    check("未确认时 approved_disable() 为空",
          sol.approved_disable() == [],
          f"{sol.approved_disable()}")
    check("建议列表已记录（供界面展示）",
          len(sol.recommended_disable()) == len(dis),
          f"{len(sol.recommended_disable())} 条")

    # --- 2. 一键优化不应自动确认 ---------------------------------
    check("一键优化未自动确认任何建议", sol.approved_disable() == [])

    # --- 3. 逐项决策 ---------------------------------------------
    if dis:
        target = dis[0]
        r = app.decide_disable(target.key, True)
        check("点「关闭」后该模组进入已确认列表",
              target.key in app.solution.approved_disable(),
              target.name)
        check("其它建议仍未确认",
              all(not p.approved for p in app.solution.disable_plans()
                  if p.key != target.key),
              f"其余 {len(dis)-1} 条")

        # 撤销
        app.decide_disable(target.key, False)
        check("点「保留」后撤销生效",
              target.key not in app.solution.approved_disable(),
              target.name)
        check("点「保留」后进入 kept 列表而非 pending",
              any(p.key == target.key for p in app.solution.kept()),
              f"kept={len(app.solution.kept())} "
              f"pending={len(app.solution.pending_decisions())}")

        # 重置后回到未表态
        app.reset_decisions()
        check("重置选择后回到「待你决定」",
              len(app.solution.pending_decisions()) == len(dis)
              and not app.solution.kept()
              and not app.solution.approved_disable(),
              f"待决策 {len(app.solution.pending_decisions())} 条")
    else:
        print("   （本机没有禁用建议，跳过逐项决策测试）")

    # --- 4. 未确认时应用不关闭任何模组 ---------------------------
    print("\n--- 验证「未确认 = 不关闭」---")
    state0 = read_playset_state(app.settings.paths)
    enabled0 = [e["modId"] for e in state0["entries"] if e["enabled"]]
    print(f"    当前启用 {len(enabled0)} 个模组")

    if do_write:
        running = launcher_running()
        check("没有进程占用配置文件", not running,
              "、".join(running) if running else "干净")
        if running:
            print("   Paradox 启动器/游戏在运行，跳过写入验证。")
            return 1

        # 4a) 未确认 → 一个都不该少
        res = apply_solution(app.settings.paths, app.library, app.solution)
        check("应用成功（未确认状态）", res.ok, f"{len(res.actions)} 个动作")
        st1 = read_playset_state(app.settings.paths)
        enabled1 = [e["modId"] for e in st1["entries"] if e["enabled"]]
        lost = set(enabled0) - set(enabled1)
        check("未确认时没有模组被关闭", not lost,
              f"启用量 {len(enabled0)} → {len(enabled1)}，"
              f"被关 {len(lost)} 个")

        # 4b) 只确认一个 → 只少一个
        if dis:
            app.decide_disable(dis[0].key, True)
            approved = app.solution.approved_disable()
            res2 = apply_solution(app.settings.paths, app.library, app.solution)
            check("应用成功（确认 1 个）", res2.ok, f"{len(res2.actions)} 个动作")
            st2 = read_playset_state(app.settings.paths)
            enabled2 = [e["modId"] for e in st2["entries"] if e["enabled"]]
            lost2 = set(enabled0) - set(enabled2)
            check("只关闭了用户确认的那一个",
                  len(lost2) == len(approved),
                  f"确认 {len(approved)} 个，实际关闭 {len(lost2)} 个")

        # 回滚
        rb = rollback(res.backup_dir, app.settings.paths)
        st3 = read_playset_state(app.settings.paths)
        enabled3 = [e["modId"] for e in st3["entries"] if e["enabled"]]
        check("已回滚到原始状态", set(enabled3) == set(enabled0),
              f"恢复 {len(enabled3)} 个启用模组")
        _ = rb
    else:
        check("未确认时应用不关闭任何模组（逻辑校验）",
              app.solution.approved_disable() == [],
              "approved_disable() 为空 → apply 只写顺序，不动启用位")
        print("   （加 --write 做真实写入+回滚验证）")

    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print()
    print("=" * 64)
    print(f"结果：{passed}/{total} 通过")
    for n, ok, d in results:
        if not ok:
            print(f"  失败：{n} — {d}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
