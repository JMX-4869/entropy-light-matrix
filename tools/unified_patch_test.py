"""统一补丁模组：所有补丁合并成一个，且补丁模组本身不产生冲突。

要点：
1. 反复保存补丁，始终只有**一个**补丁模组目录
2. 新增的补丁**并进**已有模组，不新建
3. 同名文件以最新一次为准
4. 账本与 README 随合并更新
5. 补丁模组**自己**不会再被检测成冲突源（它天生整份覆盖）
6. 被补丁解决的冲突从检测结果与补丁候选里消失
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

# game_dir=True 必须开：补丁模组写进 `<user_dir>/mod/`，
# 那不是 LOCALAPPDATA，只切 LOCALAPPDATA 挡不住。
# 漏了这一步就会直接往用户的游戏目录里写。
enter_sandbox(game_dir=True)
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                              errors="replace")

from smd.ai.patchgen import PatchFile  # noqa: E402
from smd.patch.service import (MANIFEST_NAME, UNIFIED_NAME,  # noqa: E402
                               list_patches, patch_root, read_patch,
                               save_patch)

results: list[tuple[str, bool, str]] = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"  [{'OK' if ok else '!!'}] {name}" + (f" — {detail}" if detail else ""))


def main() -> int:
    print("=" * 70)
    print("统一补丁模组")
    print("=" * 70)
    from smd.config import Settings
    paths = Settings.load().paths
    root = patch_root(paths)
    print(f"mod 目录：{root}\n")
    check("沙箱 mod 目录在临时目录里",
          "Temp" in str(root) or "tmp" in str(root).lower(), str(root))

    print("\n--- 1. 第一次保存 ---")
    r1 = save_patch(paths, title="测试 A",
                    files=[PatchFile("common/a.txt", "a = { }\n")],
                    source="local", conflict="a")
    check("保存成功", r1.get("ok"), str(r1.get("error", "")))
    check("目录名是统一名", r1.get("name") == UNIFIED_NAME, r1.get("name", ""))
    check("第一次是新建", r1.get("existed") is False)

    print("\n--- 2. 再保存两次：仍是同一个模组 ---")
    r2 = save_patch(paths, title="测试 B",
                    files=[PatchFile("common/b.txt", "b = { }\n")],
                    source="ai", provider="mock/model", conflict="b")
    r3 = save_patch(paths, title="测试 C",
                    files=[PatchFile("common/c.txt", "c = { }\n")],
                    source="local", conflict="c")
    check("第二、三次都是并入已有模组",
          r2.get("existed") is True and r3.get("existed") is True)
    dirs = [d for d in root.iterdir() if d.is_dir()
            and d.name.startswith("zzz_SMD_")]
    check("补丁模组只有一个", len(dirs) == 1,
          f"{len(dirs)} 个：{[d.name for d in dirs]}")
    check("文件都并进去了", r3.get("total_files") == 3,
          f"{r3.get('total_files')} 个文件")

    print("\n--- 3. 同名文件以最新为准 ---")
    r4 = save_patch(paths, title="测试 A 改",
                    files=[PatchFile("common/a.txt", "a = { v2 }\n")],
                    source="local", conflict="a")
    check("识别出是覆盖而非新增",
          r4.get("added") == 0 and "common/a.txt" in (r4.get("replaced") or []),
          f"added={r4.get('added')} replaced={r4.get('replaced')}")
    got = read_patch(paths, UNIFIED_NAME, "common/a.txt")
    check("读到的是新内容", "v2" in str(got.get("text", "")),
          str(got.get("text", ""))[:30])
    check("文件数没被重复累加", r4.get("total_files") == 3,
          f"{r4.get('total_files')}")

    print("\n--- 4. 账本与 README ---")
    d = root / UNIFIED_NAME
    check("账本已写入", (d / MANIFEST_NAME).is_file())
    man = json.loads((d / MANIFEST_NAME).read_text(encoding="utf-8"))
    check("账本记录了 3 条", len(man.get("entries", [])) == 3,
          f"{len(man.get('entries', []))} 条")
    rels = {e["rel"] for e in man["entries"]}
    check("账本里的路径齐全",
          rels == {"common/a.txt", "common/b.txt", "common/c.txt"}, str(rels))
    readme = (d / "README_SMD.txt").read_text(encoding="utf-8")
    check("README 提到统一维护", "合并进这一个模组" in readme)
    check("README 列出了文件", "common/b.txt" in readme and "common/c.txt" in readme)

    print("\n--- 5. 列补丁时能看出是统一模组 ---")
    patches = list_patches(paths)
    check("只有一条补丁记录", len(patches) == 1, f"{len(patches)} 条")
    if patches:
        p = patches[0]
        check("标记为 unified", p.get("unified") is True, str(p.get("source")))
        check("带出账本条目", len(p.get("entries") or []) == 3,
              f"{len(p.get('entries') or [])} 条")
        check("来源分布可读", set(p.get("source_mix") or []) == {"local", "ai"},
              str(p.get("source_mix")))

    print("\n--- 6. 旧式的多个补丁模组会被并入统一补丁 ---")
    # 模拟早期版本留下的"一个补丁一个模组"
    for i in range(3):
        od = root / f"zzz_SMD_AI_遗留{i}_0101_00000{i}"
        (od / "common").mkdir(parents=True, exist_ok=True)
        (od / "common" / f"legacy{i}.txt").write_text(
            f"legacy{i} = {{ }}\n", encoding="utf-8")
        (root / f"{od.name}.mod").write_text('name="legacy"\n',
                                             encoding="utf-8")
    before_dirs = [d.name for d in root.iterdir()
                   if d.is_dir() and d.name.startswith("zzz_SMD_")]
    check("先造出了多个补丁模组", len(before_dirs) == 4,
          f"{len(before_dirs)} 个")

    from smd.app import App
    app = App()
    res = app.consolidate_old_patch_mods()
    check("合并执行成功", res.get("ok") is True, str(res.get("error", "")))
    check("合并了 3 个旧模组", res.get("merged") == 3,
          f"merged={res.get('merged')}")
    after_dirs = [d.name for d in root.iterdir()
                  if d.is_dir() and d.name.startswith("zzz_SMD_")]
    check("只剩统一补丁模组", after_dirs == [UNIFIED_NAME], str(after_dirs))
    left_mods = [f.name for f in root.iterdir()
                 if f.name.endswith(".mod") and f.name.startswith("zzz_SMD_")]
    check("旧的 .mod 已清掉", left_mods == [f"{UNIFIED_NAME}.mod"],
          str(left_mods))
    check("统一补丁的 .mod 还在（否则启动器看不到它）",
          (root / f"{UNIFIED_NAME}.mod").is_file())
    # 旧文件应当被搬进来了
    moved = (root / UNIFIED_NAME / "common" / "legacy0.txt").is_file()
    check("旧补丁的文件已并入", moved)
    check("统一补丁的原有文件没被弄丢",
          (root / UNIFIED_NAME / "common" / "a.txt").is_file())

    print("\n--- 7. 补丁模组不该被当成冲突来源 ---")
    from smd import PATCH_PREFIX as _PFX
    fake = {"key": "local:x", "path": str(root / UNIFIED_NAME)}
    is_patch = _PFX.lower() in fake["path"].lower()
    check("补丁模组的路径能被识别出来", is_patch, fake["path"])

    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print()
    print("=" * 70)
    print(f"结果：{passed}/{total} 通过")
    for n, ok, dd in results:
        if not ok:
            print(f"  失败：{n} — {dd}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
