"""统一补丁账本测试。

账本存在的意义是**去掉重复**：原本本地草稿、AI 按冲突生成、
AI 按分析结论生成三处各存一份，保存时按来源走不同分支。
这里验证它们现在是同一种东西。
"""
import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
from sandbox import enter_sandbox            # noqa: E402

enter_sandbox()
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                              errors="replace")

from smd.ai.patchgen import AiPatchOutcome, PatchFile, PatchIssue  # noqa: E402
from smd.app import App, strip_patch_marks  # noqa: E402
from smd.patch.ledger import PatchEntry, PatchLedger  # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"  [{'OK' if ok else '!!'}] {name}" + (f" — {detail}" if detail else ""))


def main() -> int:
    print("=" * 70)
    print("统一补丁账本")
    print("=" * 70)

    print("\n--- 1. 账本本身 ---")
    led = PatchLedger()
    led.add(PatchEntry(engine="local", target="a.txt", title="A",
                       files=[PatchFile("a.txt", "x")]))
    led.add(PatchEntry(engine="ai", target="B", title="B",
                       files=[PatchFile("b.txt", "y")]))
    check("登记两条", len(led) == 2, str(len(led)))
    check("按来源过滤", len(led.pending("local")) == 1
          and len(led.pending("ai")) == 1)
    check("取得到", led.get("ai", "B") is not None)

    led.mark_saved("local", "a.txt")
    check("保存后不再列为待保存",
          all(e.target != "a.txt" for e in led.pending()),
          str([e.target for e in led.pending()]))
    check("但底层记录还在（可复查）", len(led.all_entries()) == 2)

    print("\n--- 2. 重新生成会覆盖，且**不会**沿用已保存标记 ---")
    led.add(PatchEntry(engine="local", target="a.txt", title="A2",
                       files=[PatchFile("a.txt", "z")]))
    check("覆盖后标题是新的", led.get("local", "a.txt").title == "A2",
          led.get("local", "a.txt").title)
    check("覆盖后重新变成待保存（关键：新生成的是待保存的东西）",
          led.get("local", "a.txt").saved is False)
    check("覆盖不新增条目（同 key 只占一格）", len(led) == 2, str(len(led)))

    print("\n--- 3. 阻塞的条目标记为不可保存 ---")
    led.add(PatchEntry(engine="ai", target="C", title="C", files=[],
                       block="模型只给了文字建议"))
    check("没文件的不可保存", led.get("ai", "C").savable is False)
    check("并且原因带出来",
          "文字建议" in led.get("ai", "C").to_dict()["reason"])

    print("\n--- 4. 去重：路径带补丁前缀也能认出来 ---")
    led2 = PatchLedger()
    led2.add(PatchEntry(
        engine="local",
        target="common/x/zzz_SMD_auto_merge_orig.txt", title="orig",
        aliases={"common/x/zzz_SMD_auto_merge_orig.txt",
                 strip_patch_marks("zzz_SMD_auto_merge_orig.txt")}))
    check("原文件名能匹配上带前缀的记账",
          led2.is_handled("local", target="common/x/orig.txt") is True)
    check("对象名走标题也能匹配",
          led2.is_handled("local", target="", title="orig") is True)
    check("无关目标不会误判",
          led2.is_handled("local", target="common/y/other.txt") is False)
    check("按来源隔离", led2.is_handled("ai", target="common/x/orig.txt")
          is False)

    print("\n--- 5. 应用层：三种来源都进同一个账本 ---")
    app = App()
    led3 = app.patch_ledger()
    check("账本初始为空", len(led3) == 0)

    class D:
        rel_path = "common/t/zzz_SMD_auto_merge_f.txt"
        title = "f 的合并"
        body = "# body"
        reason = "规则：合并"
        rule = "merge"

    n = app._ledger_add_local([D()])
    check("本地草稿登记成功", n == 1 and len(led3) == 1, f"{n} 条")

    oc = AiPatchOutcome(
        ok=True, files=[PatchFile("common/t/g.txt", "# g")],
        explanation="说明", risk="风险", provider="ollama", model="m")
    app._ledger_add_outcome("g.txt", oc)
    check("AI 按冲突出登记成功", len(led3) == 2, str(len(led3)))

    oc2 = AiPatchOutcome(
        ok=True, files=[PatchFile("events/h.txt", "# h")],
        explanation="来自崩溃分析", provider="ollama", model="m")
    app._ledger_add_outcome("h.txt", oc2)
    check("AI 按分析结论登记成功（同一条链路）", len(led3) == 3,
          str(len(led3)))

    check("全部出现在待保存列表里",
          len(app._pending_patch_items("")) == 3,
          str(len(app._pending_patch_items(""))))
    plan = app.batch_save_plan("ai")
    check("批量保存计划能看到 AI 的两条", plan["total"] == 2,
          f"{plan['total']} 条")
    check("两条都可保存", plan["savable"] == 2, str(plan["savable"]))

    print("\n--- 6. 校验不通过的不会被保存 ---")
    bad = AiPatchOutcome(
        ok=True, files=[PatchFile("common/t/bad.txt", "x")],
        issues=[PatchIssue("error", "括号不配对", "bad.txt")],
        provider="ollama", model="m")
    app._ledger_add_outcome("bad.txt", bad)
    plan2 = app.batch_save_plan("ai")
    check("不可保存的被单独统计", plan2["rejected"] == 1,
          f"可保存 {plan2['savable']} / 拒绝 {plan2['rejected']}")
    check("拒绝原因说得清",
          any("括号不配对" in r for r in plan2["reject_reasons"]),
          str(plan2["reject_reasons"])[:70])

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
