"""档案 AI（问答 / 代写命令）与 AI 生成外观的链路测试。

用假 AI：不消耗 token，只验证链路、解析与隐私闸门。
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

from smd.ai import analyze as A  # noqa: E402
from smd.app import App, _extract_commands  # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"  [{'OK' if ok else '!!'}] {name}" + (f" — {detail}" if detail else ""))


_ASK_REPLY = """你可以用这几条命令：

1. research_all_technologies
2. finish_research
- unity 100000

注意先选中舰队再勘探。
"""

_THEME_TOKENS = {
    "bg": "#0b0a12", "bg2": "#120f1c", "panel": "#171326",
    "panel2": "#1d1830", "panel3": "#251e3d",
    "fg": "#ece7ff", "dim": "#a79ec9", "dim2": "#6f6790",
    "line": "#2a2340", "line2": "#3a3157",
    "acc": "#c026d3", "acc2": "#22d3ee", "acctext": "#0b0a12",
    "ok": "#34d399", "warn": "#fbbf24", "high": "#fb923c",
    "crit": "#f87171", "info": "#60a5fa", "scheme": "dark",
}


def main() -> int:
    print("=" * 70)
    print("档案 AI / AI 生成外观")
    print("=" * 70)
    app = App()

    print("\n--- 1. 命令抽取（保守，不把解释当命令）---")
    cmds = _extract_commands(_ASK_REPLY)
    check("抽出 3 条命令", len(cmds) == 3, str(cmds))
    check("编号项被识别", "research_all_technologies" in cmds)
    check("项目符号项被识别", "unity 100000" in cmds)
    check("中文解释没被当成命令",
          all(not c.startswith(("你", "注意")) for c in cmds))
    check("去掉行尾 # 注释",
          _extract_commands("damage 100 # 需先选中") == ["damage 100"],
          str(_extract_commands("damage 100 # 需先选中")))
    check("代码块围栏被跳过",
          "```" not in " ".join(_extract_commands("```\nown\n```")))

    print("\n--- 2. 隐私闸门（未授权时明确拒绝）---")
    app.settings.ai.allow_send_mod_content = False
    r = app.wiki_ask("怎么快速解锁科技")
    check("问答被闸门拦住",
          r.get("ok") is False and r.get("kind") == "consent",
          f"ok={r.get('ok')} kind={r.get('kind')}")
    r2 = app.wiki_write_commands("给我资源")
    check("代写命令被闸门拦住",
          r2.get("ok") is False and r2.get("kind") == "consent",
          f"kind={r2.get('kind')}")
    r3 = app.ai_make_theme("赛博朋克")
    check("生成外观被闸门拦住",
          r3.get("ok") is False and r3.get("kind") == "consent",
          f"kind={r3.get('kind')}")

    # 放行（只影响沙箱内的假 AI）
    app.settings.ai.allow_send_mod_content = True

    print("\n--- 3. 档案问答（打桩 AI）---")
    captured: dict = {}

    def fake_ask(settings, system, user, *, temperature=0.2,
                 should_stop=None):
        captured["system"] = system
        captured["user"] = user
        captured["temp"] = temperature
        return _ASK_REPLY

    A.ask = fake_ask
    r4 = app.wiki_ask("怎么快速解锁全部科技")
    check("问答成功", r4.get("ok") is True, str(r4.get("error", "")))
    check("回答原文带回", "research_all_technologies" in str(r4.get("answer")))
    check("从回答里认出了命令", len(r4.get("commands") or []) >= 2,
          str(r4.get("commands")))
    check("提示词里带了档案检索结果",
          "档案里检索到" in captured.get("user", "")
          or "没有检索到" in captured.get("user", ""),
          str(captured.get("user", ""))[:60])
    check("检索确实命中了条目", (r4.get("total_hits") or 0) > 0,
          f"{r4.get('total_hits')} 条")

    print("\n--- 4. 让 AI 写命令 ---")
    r5 = app.wiki_write_commands("我想要一大堆资源")
    check("生成成功", r5.get("ok") is True, str(r5.get("error", "")))
    check("拿到 3 条可直接用的命令", len(r5.get("commands") or []) == 3,
          str(r5.get("commands")))
    check("提示词要求只输出命令",
          "只输出命令" in captured.get("system", ""),
          captured.get("system", "")[:50])
    check("低温（命令要稳定）", captured.get("temp", 1) <= 0.3,
          str(captured.get("temp")))

    print("\n--- 5. AI 生成外观 ---")
    def fake_theme(settings, prompt, *, base=None, should_stop=None):
        return {"ok": True, "name": "赛博霓虹", "tokens": dict(_THEME_TOKENS),
                "fallback": []}

    A.make_theme = fake_theme
    r6 = app.ai_make_theme("赛博朋克霓虹，紫青撞色")
    check("主题已生成并保存", r6.get("ok") is True, str(r6.get("error", "")))
    if r6.get("ok"):
        th = r6["theme"]
        check("主题名沿用 AI 给的", th["name"] == "赛博霓虹", th["name"])
        check("id 被规范化（防路径穿越）",
              th["id"].startswith("ai_") and "/" not in th["id"], th["id"])
        check("颜色按 AI 给的写入", th["tokens"]["acc"] == "#c026d3",
              th["tokens"]["acc"])
        check("形态令牌沿用当前主题（不只换颜色）",
              bool(th.get("flags")), str(list((th.get("flags") or {}))[:3]))
        check("当前主题已切换到新主题",
              app.settings.theme == th["id"], app.settings.theme)
        # 能被主题系统重新读出来
        from smd import theme as T
        got = T.get_theme(th["id"])
        check("主题可被重新加载", got.get("name") == "赛博霓虹",
              str(got.get("name")))

    print("\n--- 6. 恶意主题名不会写到目录外 ---")
    r7 = app.ai_make_theme("x", name="../../evil")
    if r7.get("ok"):
        check("路径穿越被消解", "/" not in r7["theme"]["id"]
              and ".." not in r7["theme"]["id"], r7["theme"]["id"])
    else:
        check("路径穿越被拒绝", True, str(r7.get("error"))[:40])

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
