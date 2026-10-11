"""一键翻译功能测试（不需要真实 API Key）。

覆盖：

* 语言清单完整（10 种，后缀正确）
* 能从真实模组里收集出待翻译条目，且**已有的译文不重复收集**
* 分批逻辑（条数 / 字符数两个上限都要生效）
* 模型回复的**严格校验**：漏条目、凭空造键、带 markdown 围栏、
  非 JSON、键名被改写 —— 全部要被正确处理
* 落盘格式：BOM + ``l_xxx:`` 头 + ``key:0 "译文"``，文件名前缀可覆盖原文
* 未开启隐私开关时必须拒绝联网

用法::  python tools\\translate_test.py
"""

from __future__ import annotations

import io
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# --- 测试沙箱：必须在 import smd 之前生效，否则配置会被测试写脏 ---
sys.path.insert(0, str(Path(__file__).resolve().parent))
from sandbox import enter_sandbox          # noqa: E402
enter_sandbox()

from smd.app import App  # noqa: E402
from smd.scanner import _parse_localisation  # noqa: E402
from smd.translate import (LANGUAGES, LocEntry, batch_entries,  # noqa: E402
                           build_translate_messages, collect_entries,
                           language_choices, parse_translation_reply,
                           write_translation_files)

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"  [{'OK' if ok else '!!'}] {name}" + (f" — {detail}" if detail else ""))


def main() -> int:
    print("=" * 70)
    print("一键翻译测试")
    print("=" * 70)

    # ---------- 1. 语言 ----------
    print("\n--- 1. 语言清单 ---")
    ch = language_choices()
    check("提供 10 种目标语言", len(ch) == 10, f"{len(ch)} 种")
    check("每种都有 Paradox 后缀",
          all(c["suffix"].startswith("l_") for c in ch),
          "、".join(c["suffix"] for c in ch[:4]) + "…")
    check("中文用简体后缀",
          LANGUAGES["zh"][0] == "l_simp_chinese", LANGUAGES["zh"][0])
    check("覆盖游戏原生语言",
          {"zh", "en", "fr", "de", "es", "ru", "pl", "pt", "ja", "ko"}
          == set(LANGUAGES), f"{len(LANGUAGES)} 种")

    # ---------- 2. 收集 ----------
    print("\n--- 2. 从真实模组收集 ---")
    app = App()
    app.load_mods()
    if app.library is None:
        check("加载模组库", False, "无模组库")
        return 1
    t0 = time.time()
    plan = collect_entries(app.library, "zh", limit=300)
    check("收集到待翻译条目", len(plan.entries) > 0,
          f"{len(plan.entries)} 条 / {plan.chars} 字符"
          f"（{time.time() - t0:.1f}s）")
    check("统计了模组数", plan.mods_total > 0,
          f"共 {plan.mods_total} 个模组，其中 {plan.mods_with_text} 个有英文文本")
    check("条目带模组归属",
          all(e.mod_key and e.mod_name for e in plan.entries))
    check("条目文本非空", all(e.text.strip() for e in plan.entries))
    check("已跳过已有中文译文的键", plan.skipped_existing > 0,
          f"跳过 {plan.skipped_existing} 条")
    check("计划序列化正常", "sample" in plan.to_dict(),
          f"target={plan.to_dict()['target_label']}")

    # 已存在译文的键不能重复收集
    zh_keys = set()
    for m in app.library.all():
        cdir = m.content_dir()
        if not cdir:
            continue
        loc = Path(cdir) / "localisation"
        if loc.is_dir():
            for f in loc.rglob("*_l_simp_chinese.yml"):
                try:
                    zh_keys |= set(_parse_localisation(
                        f.read_text(encoding="utf-8-sig", errors="replace")).keys())
                except OSError:
                    pass
    got_keys = {e.key for e in plan.entries}
    overlap = got_keys & zh_keys
    check("不与已有中文译文重叠", not overlap,
          f"重叠 {len(overlap)} 条" + (f"：{sorted(overlap)[:2]}" if overlap else ""))

    # ---------- 3. 分批 ----------
    print("\n--- 3. 分批 ---")
    fake = [LocEntry(key=f"k{i}", text="x" * 50, mod_key="m", mod_name="M")
            for i in range(95)]
    bs = batch_entries(fake, batch_size=40, batch_chars=100000)
    check("按条数切批", [len(b) for b in bs] == [40, 40, 15],
          str([len(b) for b in bs]))
    bs2 = batch_entries(fake, batch_size=1000, batch_chars=200)
    check("按字符数切批也生效", all(sum(len(e.text) for e in b) <= 250
                                   for b in bs2),
          f"{len(bs2)} 批，每批 ≤ {max(sum(len(e.text) for e in b) for b in bs2)} 字符")
    check("空输入返回空", batch_entries([]) == [])
    check("单条也成批", len(batch_entries(fake[:1])) == 1)

    # ---------- 4. 提示词 ----------
    print("\n--- 4. 提示词 ---")
    msgs = build_translate_messages(fake[:2], "zh")
    check("包含 system 与 user", len(msgs) == 2 and
          msgs[0]["role"] == "system" and msgs[1]["role"] == "user")
    check("system 指定了目标语言", "简体中文" in msgs[0]["content"])
    check("system 要求键名一致", "键必须与输入完全一致" in msgs[0]["content"])
    check("user 是 JSON", msgs[1]["content"].strip().startswith("{"))
    msgs_ja = build_translate_messages(fake[:1], "ja")
    check("切换语言会换提示词", "日本語" in msgs_ja[0]["content"])

    # ---------- 5. 回复校验（模型常犯的错）----------
    print("\n--- 5. 回复校验 ---")
    batch = [LocEntry(key="a.1", text="One", mod_key="m", mod_name="M"),
             LocEntry(key="a.2", text="Two", mod_key="m", mod_name="M"),
             LocEntry(key="a.3", text="Three", mod_key="m", mod_name="M")]

    got, probs = parse_translation_reply(
        '{"a.1":"一","a.2":"二","a.3":"三"}', batch)
    check("完整正确回复全部通过",
          len(got) == 3 and not probs, f"{len(got)} 条，问题 {probs}")

    got, probs = parse_translation_reply('{"a.1":"一"}', batch)
    check("漏条目被报告", len(got) == 1 and any("未返回" in p for p in probs),
          str(probs))

    got, probs = parse_translation_reply(
        '{"a.1":"一","a.2":"二","a.3":"三","bogus":"x"}', batch)
    check("凭空造键被丢弃并报告",
          "bogus" not in got and any("键名对不上" in p for p in probs),
          str(probs))

    got, probs = parse_translation_reply(
        "```json\n{\"a.1\":\"一\",\"a.2\":\"二\",\"a.3\":\"三\"}\n```", batch)
    check("markdown 围栏能剥掉", len(got) == 3, f"{len(got)} 条")

    got, probs = parse_translation_reply(
        "好的，这是翻译：\n{\"a.1\":\"一\",\"a.2\":\"二\",\"a.3\":\"三\"}\n希望有帮助",
        batch)
    check("能容忍前后废话", len(got) == 3, f"{len(got)} 条")

    got, probs = parse_translation_reply("抱歉，我不能翻译这个。", batch)
    check("非 JSON 被拒绝", not got and probs, str(probs))

    got, probs = parse_translation_reply('{"a.1":"","a.2":"二","a.3":"三"}', batch)
    check("空译文被丢弃", "a.1" not in got, f"通过 {len(got)} 条")

    got, probs = parse_translation_reply('["a.1"]', batch)
    check("数组被拒绝", not got, str(probs))

    got, probs = parse_translation_reply('{"A.1":"一","a.2":"二","a.3":"三"}', batch)
    check("大小写不同的键算对不上", "A.1" not in got, f"通过 {len(got)} 条")

    # ---------- 6. 落盘格式 ----------
    print("\n--- 6. 落盘格式 ---")
    with tempfile.TemporaryDirectory() as td:
        tr = {"m1": {"key.one": "第一条", "key.two": 'He said "hi"'}}
        # 造一个假 library
        class _M:
            def __init__(self, k, n):
                self.key, self.name = k, n

        class _Lib:
            def get(self, k):
                return _M(k, "测试模组") if k == "m1" else None

        written = write_translation_files(_Lib(), tr, "zh", out_root=Path(td))
        check("写出了文件", len(written) == 1 and written[0]["ok"],
              str(written[0].get("rel", written[0].get("error"))))
        p = Path(written[0]["path"])
        check("文件名带覆盖前缀", p.name.startswith("zzz_SMD_tr_"), p.name)
        check("文件名带语言后缀", p.name.endswith("_l_simp_chinese.yml"), p.name)
        raw = p.read_bytes()
        check("写入 BOM（群星要求）", raw.startswith(b"\xef\xbb\xbf"))
        text = raw.decode("utf-8-sig")
        check("有语言头", text.splitlines()[0].strip() == "l_simp_chinese:",
              text.splitlines()[0])
        check("条目格式正确", ' key.one:0 "第一条"' in text)
        check("引号被转义", '\\"hi\\"' in text, text.splitlines()[2].strip()[:50])
        # 解析回来必须和写入一致
        parsed = _parse_localisation(text)
        check("能被自家解析器读回",
              parsed.get("key.one") == "第一条" and
              len(parsed) == 2, str(parsed)[:70])

        w2 = write_translation_files(_Lib(), {"m1": {"k": "x"}}, "ja",
                                     out_root=Path(td))
        check("换语言生成不同后缀",
              Path(w2[0]["path"]).name.endswith("_l_japanese.yml"),
              Path(w2[0]["path"]).name)
        check("空译文不写文件",
              write_translation_files(_Lib(), {"m1": {}}, "zh",
                                      out_root=Path(td)) == [])

    # ---------- 7. 隐私开关 ----------
    print("\n--- 7. 隐私开关 ---")
    from smd.config import Settings
    st = Settings.load()
    print(f"  （进来时 allow_send_mod_content = "
          f"{st.ai.allow_send_mod_content}）")
    try:
        st.ai.allow_send_mod_content = False
        # translate_entries 内部会重新 load 配置，所以直接测它的守卫逻辑
        from smd import translate as T
        import smd.config as C
        orig_load = C.Settings.load
        C.Settings.load = classmethod(lambda cls: st)  # type: ignore
        try:
            r = T.translate_entries(fake[:2], "zh")
            check("未开启隐私开关时拒绝翻译",
                  r.get("ok") is False and "允许发送模组内容" in (r.get("error") or ""),
                  (r.get("error") or "")[:60])
        finally:
            C.Settings.load = orig_load  # type: ignore
    finally:
        # 统一收敛到 False（隐私开关的正确默认值），而不是"还原成进来时的样子"。
        # 万一哪个用例把它泄漏成 True，"原样还原"会把这个错误值继续传下去，
        # 后面的 bridge_test 断言"隐私默认关闭"就会时对时错。
        st.ai.allow_send_mod_content = False
        try:
            st.save()
        except Exception:
            pass

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
