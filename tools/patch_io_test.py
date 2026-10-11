"""补丁落盘测试：保存 / 读取 / 更新 / 删除，全程真实写盘再清理。

会真的在 ``mod/`` 下创建 ``zzz_SMD_`` 开头的目录，测试结束会删掉自己
创建的每一个。绝不碰其它模组。

用法::  python tools/patch_io_test.py
"""

from __future__ import annotations

import io
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from smd.ai.patchgen import PatchFile  # noqa: E402
from smd.config import Settings  # noqa: E402
from smd.patch import (PATCH_PREFIX, delete_patch, list_patches,  # noqa: E402
                       read_patch, save_patch, update_patch_file)

results: list[tuple[str, bool, str]] = []
created: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"  [{'OK' if ok else '!!'}] {name}" + (f" — {detail}" if detail else ""))


def main() -> int:
    paths = Settings.load().paths
    print("=" * 64)
    print(f"补丁落盘测试　mod 目录：{paths.mod_dir}")
    print("=" * 64)

    before = {p["name"] for p in list_patches(paths)}
    print(f"测试前已有补丁：{len(before)} 个")

    # ---------- 1. 保存一个 AI 风格补丁 ----------
    print("\n--- 1. 保存补丁模组 ---")
    files = [
        PatchFile("common/scripted_triggers/zzz_smd_test.txt",
                  "# 测试补丁\nzzz_smd_test_trigger = {\n\talways = yes\n}\n"),
        PatchFile("localisation/simp_chinese/zzz_smd_test_l_simp_chinese.yml",
                  'l_simp_chinese:\n zzz_smd_test_key:0 "测试文本"\n'),
    ]
    res = save_patch(paths, title="IO 测试补丁", files=files,
                     description="自动化测试用，结束后会被删除。",
                     source="ai", conflict="zzz_test_trigger",
                     provider="mock/model")
    check("保存成功", res.get("ok"), res.get("name", res.get("error", "")))
    if not res.get("ok"):
        return 1
    name = res["name"]
    created.append(name)

    d = Path(res["path"])
    check("补丁目录已创建", d.is_dir(), str(d))
    check("目录名带统一前缀", name.startswith(PATCH_PREFIX), name)
    check("描述符已写入", (d / "descriptor.mod").is_file())
    check("mod/ 下的 .mod 已写入", Path(res["mod_file"]).is_file(),
          Path(res["mod_file"]).name)
    check("README 已生成", (d / "README_SMD.txt").is_file())
    for rel in res["files"]:
        check(f"补丁文件已落盘：{rel.split('/')[-1]}",
              (d / rel.replace("/", os.sep)).is_file())

    # 描述符内容可被解析。
    # 名字现在固定是统一补丁的标题，不再等于传入的 title——
    # 因为所有补丁都合并进同一个模组，模组名必须稳定。
    from smd.modinfo import parse_mod_file
    from smd.patch.service import UNIFIED_NAME, UNIFIED_TITLE
    raw = parse_mod_file(res["mod_file"])
    check("描述符可被解析", raw.get("name") == UNIFIED_TITLE,
          str(raw.get("name")))
    check("目录名是统一补丁名", name == UNIFIED_NAME, name)
    check("描述符 path 指向补丁目录",
          str(raw.get("path", "")).replace("\\", "/").endswith(name),
          str(raw.get("path", ""))[-28:])

    # ---------- 2. 出现在列表里 ----------
    print("\n--- 2. 列表与读取 ---")
    lst = list_patches(paths)
    me = next((p for p in lst if p["name"] == name), None)
    check("出现在补丁列表中", me is not None, f"共 {len(lst)} 个")
    if me:
        check("列表统计文件数正确", len(me["files"]) == 2,
              f"{len(me['files'])} 个")
        # 统一补丁的来源标记是 unified（里面可能混着 AI/本地/手写）
        check("标记为统一补丁", me["source"] == "unified", me["source"])
        check("记录了大小", me["bytes"] > 0, me["size_human"])
        check("账本里有本次这条",
              any(e.get("rel", "").endswith("zzz_smd_test.txt")
                  for e in (me.get("entries") or [])),
              f"{len(me.get('entries') or [])} 条")

    r = read_patch(paths, name, "common/scripted_triggers/zzz_smd_test.txt")
    check("读取补丁文件", r.get("ok") and "always = yes" in r.get("text", ""),
          f"{len(r.get('text', ''))} 字符")

    # 路径越界必须被拒绝
    esc = read_patch(paths, name, "../../../config.json")
    check("拒绝路径越界读取", "error" in esc, str(esc.get("error"))[:40])

    # ---------- 3. 更新（含校验） ----------
    print("\n--- 3. 更新补丁 ---")
    good = "zzz_smd_test_trigger = {\n\talways = no\n}\n"
    u = update_patch_file(paths, name,
                          "common/scripted_triggers/zzz_smd_test.txt", good)
    check("更新合法内容成功", u.get("ok"), str(u.get("error", ""))[:50])
    r2 = read_patch(paths, name, "common/scripted_triggers/zzz_smd_test.txt")
    check("更新后内容已生效", "always = no" in r2.get("text", ""))

    bad = "zzz_smd_test_trigger = {\n"
    u2 = update_patch_file(paths, name,
                           "common/scripted_triggers/zzz_smd_test.txt", bad)
    check("拒绝写入语法错误的内容", not u2.get("ok"),
          str(u2.get("error", ""))[:40])
    r3 = read_patch(paths, name, "common/scripted_triggers/zzz_smd_test.txt")
    check("被拒后原内容未被破坏", "always = no" in r3.get("text", ""))

    # ---------- 4. 删除 ----------
    print("\n--- 4. 删除补丁 ---")
    refuse = delete_patch(paths, "SomeRealMod")
    check("拒绝删除非自动补丁", not refuse.get("ok"),
          str(refuse.get("error", ""))[:44])

    dl = delete_patch(paths, name)
    check("删除成功", dl.get("ok"), f"移除 {len(dl.get('removed', []))} 项")
    check("补丁目录已消失", not d.exists())
    check("mod/ 下的 .mod 已消失", not Path(res["mod_file"]).exists())
    if name in created:
        created.remove(name)

    after = {p["name"] for p in list_patches(paths)}
    check("列表已不含该补丁", name not in after, f"剩余 {len(after)} 个")
    check("没有影响其它补丁", after == before - {name} or after <= before,
          f"测试前 {len(before)} → 测试后 {len(after)}")

    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print()
    print("=" * 64)
    print(f"结果：{passed}/{total} 通过")
    for n, ok, dd in results:
        if not ok:
            print(f"  失败：{n} — {dd}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    try:
        code = main()
    finally:
        # 无论如何都不留下测试残留
        paths = Settings.load().paths
        for n in created:
            try:
                shutil.rmtree(paths.mod_dir / n, ignore_errors=True)
                (paths.mod_dir / f"{n}.mod").unlink(missing_ok=True)
                print(f"[清理] 已移除测试补丁 {n}")
            except OSError:
                pass
    sys.exit(code)
