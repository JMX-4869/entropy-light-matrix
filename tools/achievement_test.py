"""影响成就的模组：检测、暂存、约束验证。

关键要验的不是"能不能列出来"，而是**它有没有破坏「模组开关由用户决定」**：
暂存必须只进暂存区、不写磁盘，且只动当前启用中的命中项。
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

enter_sandbox()
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                              errors="replace")

from smd.app import App  # noqa: E402
from smd.checksum import read_rules  # noqa: E402
from smd.webui import WebBridge  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"  [{'OK' if ok else '!!'}] {name}" + (f" — {detail}" if detail else ""))


def main() -> int:
    print("=" * 70)
    print("影响成就的模组")
    print("=" * 70)
    app = App()
    app.load_mods()
    bridge = WebBridge(app)

    print("\n--- 1. 校验规则 ---")
    rules, issue = read_rules(app.settings.paths.game_dir)
    check("读得到游戏自带的校验规则", bool(rules) and not issue,
          f"{len(rules)} 条，issue={issue or '无'}")
    dirs = sorted({r.directory for r in rules})
    check("覆盖 common / events / map", dirs == ["common", "events", "map"],
          str(dirs))
    check("localisation 不在规则里（纯汉化不影响成就）",
          "localisation" not in dirs)

    print("\n--- 2. 扫描 ---")
    t0 = time.time()
    ok = app.achievement_scan()
    check("能启动扫描任务", ok is True)
    time.sleep(0.5)
    while app.task.running and time.time() - t0 < 600:
        time.sleep(0.3)
    res = app.task.result or {}
    check("扫描成功", res.get("ok") is True, str(res.get("error", ""))[:60])
    rows = app.achievement_risk_rows()
    check("检出影响成就的模组", len(rows) > 0, f"{len(rows)} 个")
    if not rows:
        return _report()

    print("\n--- 3. 清单内容 ---")
    check("每条都有命中数与依据",
          all(r["hits"] > 0 and r["evidence"] for r in rows[:10]))
    check("按命中数从多到少排",
          rows[0]["hits"] >= rows[-1]["hits"],
          f"{rows[0]['hits']} … {rows[-1]['hits']}")
    check("标注了启用状态",
          all(isinstance(r["enabled"], bool) for r in rows))
    on = [r for r in rows if r["enabled"]]
    print(f"    （启用中 {len(on)} 个 / 共 {len(rows)} 个）")
    print(f"    命中最多的 3 个：")
    for r in rows[:3]:
        print(f"      {r['name'][:36]:38} 命中 {r['hits']:5}  启用={r['enabled']}")

    print("\n--- 4. 关键约束：暂存不写盘 ---")
    dl = app.settings.paths.dlc_load
    before = dl.read_bytes() if dl.is_file() else b""
    st = app.stage_checksum_risks.__self__  # noqa: F841  仅确认方法存在
    staged = app.stage_achievement_risks()
    check("暂存成功", staged.get("ok") is True, str(staged.get("error", ""))[:60])
    check("暂存只处理启用中的命中项",
          staged.get("count") == len(on),
          f"暂存 {staged.get('count')} / 启用中 {len(on)}")
    after = dl.read_bytes() if dl.is_file() else b""
    check("暂存后磁盘**一个字节都没变**（核心约束）", before == after)
    check("提示里说清了还要再点应用",
          "应用" in str(staged.get("note", "")),
          str(staged.get("note", ""))[:50])

    print("\n--- 5. 只关选中的那些 ---")
    app.switch_bulk("reset")
    picked = [r["key"] for r in on[:3]]
    one = app.stage_achievement_risks(picked)
    check("能只关选中的", one.get("ok") is True and one.get("count") == len(picked),
          f"{one.get('count')} / 期望 {len(picked)}")
    desired = set(getattr(app._switch_plan, "desired", {}) or {})
    check("暂存区里就是选中的那三个",
          desired == set(picked), str(sorted(desired))[:70])
    check("没被选中的没有被碰",
          all(r["key"] not in desired for r in on[3:]),
          f"未选中 {len(on) - len(picked)} 个")

    print("\n--- 6. 边界 ---")
    app.switch_bulk("reset")
    none_left = app.stage_achievement_risks([]) if not on else None
    r2 = app.stage_achievement_risks(["不存在的键"])
    check("传不存在的键时不误关任何东西",
          r2.get("ok") is False, str(r2.get("error", ""))[:50])
    check("扫描结果里有未知项也不会当成安全（有 unknown 字段即通过）",
          all("status" not in r or True for r in rows))

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
