"""AI 补丁生成与校验层的测试。

重点验证「防幻觉」这一类检查是否真的能拦住坏补丁：

* 语法错误（括号不配对）→ 必须 fatal
* 幻觉引用（编造的对象名）→ 必须 warn
* 越权重定义（顺手覆盖无关对象）→ 必须 warn
* 路径越界 → 必须 warn
* 正常补丁 → 必须通过

另外验证解析器能从各种格式的模型输出里正确抽出文件。

用法::  python tools/ai_patch_test.py
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from smd.ai.patchgen import (PatchFile, parse_ai_response,  # noqa: E402
                             verify_patch)
from smd.patch import get_templates, validate_text  # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"  [{'OK' if ok else '!!'}] {name}" + (f" — {detail}" if detail else ""))


def levels(issues: list) -> dict[str, int]:
    from collections import Counter
    return dict(Counter(i["level"] for i in issues))


def main() -> int:
    print("=" * 64)
    print("AI 补丁生成 / 校验层测试")
    print("=" * 64)

    # ---------- 1. 解析模型输出 ----------
    print("\n--- 1. 解析模型输出 ---")
    resp = """### 说明
这两个模组都想改同一个触发器。我用 OR 把双方条件合并。

### 补丁文件
```path: common/scripted_triggers/zzz_smd_patch.txt
my_trigger = {
\tOR = {
\t\thas_technology = "tech_x"
\t\tnum_pops > 25
\t}
}
```

### 风险评估
- 置信度：高
- 影响面：只影响 my_trigger
- 回退方式：删除本模组
"""
    files, expl, risk = parse_ai_response(resp)
    check("解析出 1 个补丁文件", len(files) == 1,
          files[0].rel if files else "无")
    check("解析出说明", "OR" in expl or "合并" in expl, expl[:40])
    check("解析出风险评估", "置信度" in risk, risk[:30])
    check("识别置信度=高", "高" in risk)

    # 无 path: 前缀的围栏
    resp2 = """### 补丁文件
```common/scripted_variables/patch.txt
@val = 50
```
"""
    f2, _, _ = parse_ai_response(resp2)
    check("兼容无 path: 前缀的围栏",
          len(f2) == 1 and f2[0].rel.startswith("common/"),
          f2[0].rel if f2 else "无")

    # 缺少路径时按内容猜
    resp3 = """### 补丁文件
```
my_trigger = { always = yes }
```
"""
    f3, _, _ = parse_ai_response(resp3)
    check("缺路径时按内容推断",
          len(f3) == 1 and "scripted_triggers" in f3[0].rel,
          f3[0].rel if f3 else "无")

    # 模型拒绝合并（只给文字）
    resp4 = """### 说明
这两个模组的设计互斥，不建议自动合并。建议保留 A。

### 风险评估
- 置信度：低
"""
    f4, e4, _ = parse_ai_response(resp4)
    check("识别「拒绝合并」情形",
          len(f4) == 0 and "互斥" in e4, e4[:40])

    # ---------- 2. 语法校验 ----------
    print("\n--- 2. 语法校验（必须拦住）---")
    good = '''```path: common/scripted_triggers/zzz_smd_patch.txt
my_trigger = {
\tOR = {
\t\thas_technology = "tech_x"
\t\tnum_pops > 25
\t}
}
```'''
    r = validate_text(good)
    check("正常补丁通过校验", r["ok"], f"阻塞问题 {r['blocking']} 个")

    unbalanced = '''```path: common/scripted_triggers/zzz_smd_patch.txt
my_trigger = {
\tOR = {
\t\thas_technology = "tech_x"
```'''
    r = validate_text(unbalanced)
    check("括号不配对 → 不通过", not r["ok"], str(levels(r["issues"])))
    check("括号不配对 → 报 fatal",
          any(i["level"] == "fatal" for i in r["issues"]))

    empty = '''```path: common/scripted_triggers/zzz_smd_patch.txt

```'''
    r = validate_text(empty)
    check("空补丁 → 不通过", not r["ok"], str(levels(r["issues"])))

    # 玩家手写模式：裸脚本 + 指定落点
    manual = """my_trigger = {
\talways = yes
}"""
    r = validate_text(manual, rel="common/scripted_triggers/zzz_smd_manual.txt")
    check("手写裸脚本可校验通过", r["ok"],
          f"阻塞 {r['blocking']}，落点 {r['files'][0]['rel'] if r['files'] else '?'}")

    r = validate_text("my_trigger = {", rel="common/scripted_triggers/x.txt")
    check("手写裸脚本语法错误被拦住", not r["ok"], str(levels(r["issues"])))

    r = validate_text("", rel="common/scripted_triggers/x.txt")
    check("手写空内容被拦住", not r["ok"], str(levels(r["issues"])))

    badloc = '''```path: localisation/simp_chinese/x_l_simp_chinese.yml
l_simp_chinese:
 this line has no colon or quotes
 ok_key:0 "正常文本"
```'''
    r = validate_text(badloc)
    check("本地化格式错误被发现",
          any("本地化" in i["message"] for i in r["issues"]),
          str(levels(r["issues"])))

    # ---------- 3. 幻觉引用 ----------
    print("\n--- 3. 幻觉引用检查 ---")
    # 伪造一个索引，里面只有 my_real_trigger
    class _Idx:
        class _I:
            definitions = {"my_real_trigger": []}
            loc_keys = {}
        indexes = {"m1": _I()}

    hallu = '''```path: common/scripted_triggers/zzz_smd_patch.txt
my_trigger = {
\tmy_real_trigger = yes
\ttotally_made_up_trigger = yes
\tanother_fictional_check = 1
}
```'''
    ok, issues = verify_patch(
        [PatchFile("common/scripted_triggers/z.txt",
                   hallu.split("\n", 1)[1].rsplit("```", 1)[0])],
        indexes=_Idx())
    msgs = " ".join(i.message for i in issues)
    check("发现编造的对象名", "编造" in msgs or "不存在" in msgs, msgs[:90])
    check("未把真实对象误报",
          "my_real_trigger" not in msgs, "my_real_trigger 未被误报")

    # ---------- 4. 越权重定义 ----------
    print("\n--- 4. 越权重定义检查 ---")
    over = '''```path: common/scripted_triggers/zzz_smd_patch.txt
my_trigger = { always = yes }
unrelated_other_trigger = { always = no }
```'''
    ok, issues = verify_patch(
        [PatchFile("common/scripted_triggers/z.txt",
                   over.split("\n", 1)[1].rsplit("```", 1)[0])],
        conflict_target="my_trigger",
        original_definitions={"my_trigger"})
    msgs = " ".join(i.message for i in issues)
    check("发现越权重定义", "冲突之外" in msgs, msgs[:90])

    # ---------- 5. 路径越界 ----------
    print("\n--- 5. 路径检查 ---")
    ok, issues = verify_patch(
        [PatchFile("../../evil.txt", "x = 1")],
        conflict_target="")
    msgs = " ".join(i.message for i in issues)
    check("发现可疑落地路径", "路径" in msgs, msgs[:90])

    # ---------- 6. 模板 ----------
    print("\n--- 6. 手写补丁模板 ---")
    tpls = get_templates()
    check("模板数量 >= 6", len(tpls) >= 6, f"{len(tpls)} 个")
    allok = True
    for t in tpls:
        rr = validate_text(f"```path: {t['rel']}\n{t['text']}\n```")
        if not rr["ok"] and "清空" not in t["name"]:
            # trigger_merge 等模板含占位符，允许 warn 但不该 fatal
            if any(i["level"] == "fatal" for i in rr["issues"]):
                allok = False
                print(f"      模板「{t['name']}」有 fatal："
                      f"{[i['message'] for i in rr['issues'] if i['level']=='fatal']}")
    check("所有模板无语法致命错误", allok)

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
