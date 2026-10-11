"""配置文件的健壮性测试：原子写入 + 损坏恢复。

配置文件里存着 AI 供应商、模型名、API Key、路径、主题。
它一旦变成默认值，用户要全部重填——这个项目里**真实发生过好几次**，
所以专门测这几条保障：

1. 保存是原子的：先写 .tmp 再改名，不留残留、不产生半截 JSON
2. 每次保存前留 .bak
3. 主文件损坏时能从 .bak 恢复
4. 但 .bak 若只是一份默认值，**不假装恢复**（否则等于什么都没救）
"""
import io
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 先把 LOCALAPPDATA 指到临时目录，别碰用户真实配置
_TMP = tempfile.mkdtemp(prefix="smd_cfg_")
import os  # noqa: E402

os.environ["LOCALAPPDATA"] = _TMP
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                              errors="replace")

from smd.config import AiSettings, Settings  # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"  [{'OK' if ok else '!!'}] {name}" + (f" — {detail}" if detail else ""))


def main() -> int:
    print("=" * 70)
    print("配置健壮性测试（沙箱，不动真实配置）")
    print("=" * 70)
    cf = Settings().config_file
    print(f"沙箱配置路径: {cf}\n")
    check("配置写在沙箱里", str(cf).startswith(_TMP), str(cf))

    print("\n--- 1. 原子写入 ---")
    s = Settings.load()
    s.ai.provider = "ollama"
    s.ai.model = "nemotron-3-nano:latest"
    s.save()
    check("主文件已写出", cf.is_file())
    check("没有残留 .tmp", not cf.with_suffix(".json.tmp").exists(),
          str(cf.with_suffix(".json.tmp")))
    d = json.loads(cf.read_text(encoding="utf-8"))
    check("写入内容正确", d["ai"]["model"] == "nemotron-3-nano:latest",
          d["ai"]["model"])

    print("\n--- 2. 保存会留 .bak ---")
    s2 = Settings.load()
    s2.ai.model = "second-value"
    s2.save()
    bak = cf.with_suffix(".json.bak")
    check(".bak 已生成", bak.is_file())
    bd = json.loads(bak.read_text(encoding="utf-8"))
    check(".bak 存的是上一版（不是刚写的新值）",
          bd["ai"]["model"] == "nemotron-3-nano:latest", bd["ai"]["model"])

    print("\n--- 3. 主文件损坏 → 从 .bak 恢复 ---")
    cf.write_text('{"ai": {"provider": "oll', encoding="utf-8")   # 模拟写一半崩了
    r = Settings.load()
    check("恢复出了备份里的值", r.ai.model == "nemotron-3-nano:latest",
          f"{r.ai.provider}/{r.ai.model}")
    check("主文件被修回合法 JSON", Settings._read_config(cf) is not None)

    print("\n--- 4. 纯默认值的 .bak 不该被当成「用户配置」 ---")
    pure = {"ai": AiSettings().to_dict(), "paths": {}, "language": "zh",
            "theme": "stellaris"}
    check("默认值判定为未配置", Settings._looks_configured(pure) is False)
    configured = {"ai": {**AiSettings().to_dict(), "provider": "ollama",
                         "model": "x"}, "paths": {}}
    check("改过 provider 判定为已配置",
          Settings._looks_configured(configured) is True)
    onlykey = {"ai": {**AiSettings().to_dict(), "api_key": "sk-abc"}}
    check("只填了 API Key 也算已配置",
          Settings._looks_configured(onlykey) is True)

    # 真的放一份默认值进 .bak，主文件弄坏，应当退回默认而不是"假恢复"
    bak.write_text(json.dumps(pure, ensure_ascii=False), encoding="utf-8")
    cf.write_text("坏的", encoding="utf-8")
    r3 = Settings.load()
    check("没有拿默认备份假装恢复",
          r3.ai.model == AiSettings().model,
          f"{r3.ai.provider}/{r3.ai.model!r}")

    print("\n--- 5. 主题与语言也能存住 ---")
    s5 = Settings.load()
    s5.theme = "daylight"
    s5.language = "en"
    s5.save()
    r5 = Settings.load()
    check("主题已保存", r5.theme == "daylight", r5.theme)
    check("语言已保存", r5.language == "en", r5.language)

    print("\n--- 5b. AI 段被重置时能自愈 ---")
    # 造一份"配过的"配置，并让 .bak 也是配过的
    sA = Settings.load()
    sA.ai.provider = "ollama"
    sA.ai.model = "my-real-model:latest"
    sA.ai.timeout = 300
    sA.save()
    Settings.load().save()          # 让 .bak 也变成配过的版本
    dA = json.loads(cf.read_text(encoding="utf-8"))
    check("主文件已配过",
          dA["ai"]["provider"] == "ollama"
          and dA["ai"]["model"] == "my-real-model:latest",
          f"{dA['ai']['provider']}/{dA['ai']['model']}")

    # 把主文件的 AI 段砸回出厂默认（模拟被某个流程重置）
    dA["ai"] = AiSettings().to_dict()
    cf.write_text(json.dumps(dA, ensure_ascii=False, indent=2),
                  encoding="utf-8")
    rA = Settings.load()
    check("读配置时自动从备份捞回 AI 段",
          rA.ai.provider == "ollama"
          and rA.ai.model == "my-real-model:latest",
          f"{rA.ai.provider}/{rA.ai.model}")
    fixedA = json.loads(cf.read_text(encoding="utf-8"))
    check("主文件也被修好了",
          fixedA["ai"]["model"] == "my-real-model:latest",
          fixedA["ai"]["model"])

    # 备份也是默认时不该瞎恢复
    sB = Settings.load()
    sB.save()
    dA["ai"] = AiSettings().to_dict()
    cf.write_text(json.dumps(dA, ensure_ascii=False, indent=2),
                  encoding="utf-8")
    cf.with_suffix(".json.bak").write_text(
        json.dumps({"ai": AiSettings().to_dict()}, ensure_ascii=False),
        encoding="utf-8")
    rB = Settings.load()
    check("备份也是默认时不假装自愈",
          rB.ai.model == AiSettings().model,
          f"{rB.ai.provider}/{rB.ai.model!r}")

    print("\n--- 6. 单实例保护 ---")
    from smd.ui import _acquire_single_instance
    h1, first1 = _acquire_single_instance()
    check("第一个实例拿到所有权", first1 is True, f"handle={bool(h1)}")
    # 同一进程再取一次：命名互斥体已存在，应当拿到"已被占用"
    h2, first2 = _acquire_single_instance()
    check("第二次取用判定为已在运行", first2 is False, f"handle={bool(h2)}")
    # 释放后应当又能拿到
    try:
        import ctypes
        if h1:
            ctypes.windll.kernel32.ReleaseMutex(h1)
            ctypes.windll.kernel32.CloseHandle(h1)
        if h2:
            ctypes.windll.kernel32.CloseHandle(h2)
    except Exception:
        pass
    h3, first3 = _acquire_single_instance()
    check("释放后可以重新获取", first3 is True, f"handle={bool(h3)}")

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
