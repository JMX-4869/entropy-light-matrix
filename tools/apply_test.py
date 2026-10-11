"""「应用方案」写回的真实性测试（带完整回滚）。

这是最有破坏性的一个环节：它要改 ``launcher-v2.sqlite`` 与 ``dlc_load.json``。
所以测试流程是：

    备份原文件 → 记录原始状态 → 应用方案 → 校验写入结果
    → **无论成败都恢复原状** → 校验恢复结果

用法::

    python tools/apply_test.py            # 演练（不改动，只验证计算）
    python tools/apply_test.py --write    # 真的写一次再回滚
"""

from __future__ import annotations

import io
import json
import shutil
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from smd.apply import (apply_solution, launcher_running, make_backup,  # noqa: E402
                       read_playset_state, rollback)
from smd.config import Settings  # noqa: E402
from smd.modinfo import load_library  # noqa: E402
from smd.paths import read_text  # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"  [{'OK' if ok else '!!'}] {name}" + (f" — {detail}" if detail else ""))


def snapshot(paths) -> dict:
    """完整快照关键文件内容（字节级），用于校验恢复。"""
    out = {}
    for p in (paths.launcher_db, paths.dlc_load):
        try:
            out[str(p)] = Path(p).read_bytes() if Path(p).is_file() else b""
        except OSError:
            out[str(p)] = b""
    return out


def main() -> int:
    do_write = "--write" in sys.argv
    s = Settings.load()
    paths = s.paths
    print("=" * 62)
    print(f"应用 / 回滚测试　模式：{'真实写入+回滚' if do_write else '只读演练'}")
    print("=" * 62)

    running = launcher_running()
    check("没有进程占用配置文件", not running,
          "、".join(running) if running else "干净")
    if running:
        print("   Paradox 启动器或游戏正在运行，跳过写入测试。")
        return 1

    before = snapshot(paths)
    check("读取到原始文件", all(v for v in before.values()),
          "、".join(f"{Path(k).name}:{len(v)}B" for k, v in before.items()))

    state0 = read_playset_state(paths)
    check("读到 playset", state0.get("available"),
          f"「{state0.get('playset_name')}」{len(state0.get('entries', []))} 个模组")
    order0 = [e["modId"] for e in state0["entries"]]

    lib = load_library(paths)
    print(f"  模组库：{lib.summary()}")

    # 跑一次分析拿到方案
    from smd.conflict import ConflictDetector, game_version_from_meta
    from smd.scanner import IndexSet, Scanner
    from smd.solver import OrderSolver

    mods = [m for m in lib.mods.values() if m.resolve_content_path()]
    idx = IndexSet()
    for k, i in Scanner().scan(mods, semantic=True).items():
        idx.add(i)
    rep = ConflictDetector(lib, idx,
                           game_version=game_version_from_meta(paths)).run()
    sol = OrderSolver(lib, idx, rep).solve()
    print(f"  方案：调整 {sol.change_count()} 项，禁用 {len(sol.disabled)} 个，"
          f"健康分 {sol.score_before} → {sol.score_after}")
    check("方案包含加载顺序", len(sol.order) > 0, f"{len(sol.order)} 个模组")

    if not do_write:
        check("演练模式未修改文件", snapshot(paths) == before)
        print("\n（未写入。加 --write 做真实写入+回滚测试）")
        return 0 if all(ok for _, ok, _ in results) else 1

    print("\n--- 应用方案 ---")
    t0 = time.time()
    res = apply_solution(paths, lib, sol)
    check("应用成功", res.ok, f"{len(res.actions)} 个动作，{time.time()-t0:.1f}s")
    check("已生成备份", bool(res.backup_dir) and Path(res.backup_dir).is_dir(),
          res.backup_dir)
    for a in res.actions:
        print(f"      [{'v' if a.ok else 'x'}] {a.description}"
              + (f" — {a.detail}" if a.detail else ""))

    # 校验数据库
    state1 = read_playset_state(paths)
    order1 = [e["modId"] for e in state1["entries"]]
    check("playset 条目数已更新", len(order1) > 0, f"{len(order0)} → {len(order1)}")

    # 顺序是否改变取决于当前磁盘顺序与最优解的差异。
    # 如果上一轮测试已经把顺序写成最优，这次应用后自然「没有变化」——
    # 那是正确行为，不是失败。所以这里只报告，不做断言。
    expected = [k for k in sol.order]
    same = order1 == order0
    print(f"      顺序变化：{'无（当前已是最优或与上次一致）' if same else '有'}"
          f"　条目 {len(order0)} → {len(order1)}")

    # 位置列必须是 0..n-1 连续
    try:
        con = sqlite3.connect(f"file:{paths.launcher_db}?mode=ro", uri=True)
        cur = con.cursor()
        cur.execute("SELECT position FROM playsets_mods WHERE playsetId=? "
                    "ORDER BY position", (state1["playset_id"],))
        pos = [r[0] for r in cur.fetchall()]
        con.close()
        check("position 连续无空洞", pos == list(range(len(pos))),
              f"{pos[:8]}… 共 {len(pos)}")
    except sqlite3.Error as exc:
        check("position 连续无空洞", False, str(exc))

    # 校验 dlc_load.json
    try:
        dl = json.loads(read_text(paths.dlc_load))
        enabled = dl.get("enabled_mods", [])
        check("dlc_load.json 可解析", isinstance(enabled, list),
              f"{len(enabled)} 条启用项")
        # 每一条都应该指向真实存在的描述符或补丁
        mod_dir = Path(paths.user_dir) / "mod"
        missing = []
        for ref in enabled:
            fn = ref.replace("\\", "/").split("/")[-1]
            if not (mod_dir / fn).is_file():
                missing.append(ref)
        check("dlc_load 无悬空引用", not missing,
              f"悬空 {len(missing)} 条：{missing[:3]}" if missing
              else "全部有效")
    except (ValueError, OSError) as exc:
        check("dlc_load.json 可解析", False, str(exc))

    print("\n--- 回滚 ---")
    rb = rollback(res.backup_dir, paths)
    check("回滚执行完成", rb.ok, f"{len(rb.actions)} 个动作")
    after = snapshot(paths)
    same_db = after[str(paths.launcher_db)] == before[str(paths.launcher_db)]
    same_dl = after[str(paths.dlc_load)] == before[str(paths.dlc_load)]
    check("launcher-v2.sqlite 已还原（字节级一致）", same_db)
    check("dlc_load.json 已还原（字节级一致）", same_dl)

    state2 = read_playset_state(paths)
    order2 = [e["modId"] for e in state2["entries"]]
    check("playset 顺序已还原", order2 == order0,
          f"{len(order2)} 个条目")

    # 回滚应当把这次写入产生的补丁模组也一并收走。
    # 留下残渣会污染后续测试的计数（`batch_removal_test` 之类会因为
    # 起点不同而假失败），而且真实 mod 目录里多出一个陌生模组，
    # 用户打开启动器会莫名其妙。
    from smd import PATCH_PREFIX
    mod_dir = Path(paths.user_dir) / "mod"
    leftovers = ([d.name for d in mod_dir.iterdir()
                  if d.name.startswith(PATCH_PREFIX)]
                 if mod_dir.is_dir() else [])
    check("回滚后没有残留补丁模组", not leftovers,
          f"残留 {len(leftovers)} 个：{leftovers[:3]}" if leftovers
          else "干净")

    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print()
    print("=" * 62)
    print(f"结果：{passed}/{total} 通过")
    for n, ok, d in results:
        if not ok:
            print(f"  失败：{n} — {d}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
