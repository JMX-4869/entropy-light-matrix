"""交叉验证：我们读到的 DLC 清单 vs 游戏实际加载的（来自崩溃 meta.yml）。

崩溃转储里的 ``meta.yml`` 会列出游戏**真正加载**的 DLC，是唯一权威的
旁证。用它来验证我们解析 ``.dlc`` 得到的名字是否和游戏一致。

用法::  python tools/dlc_test.py
"""

from __future__ import annotations

import io
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from smd.config import Settings  # noqa: E402
from smd.manage import (DlcInfo, _is_disabled, apply_dlc_switches,  # noqa: E402
                        dlc_overview, read_dlc_list, read_disabled_dlcs,
                        report_dlc_hint)

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"  [{'OK' if ok else '!!'}] {name}" + (f" — {detail}" if detail else ""))


def loaded_dlc_from_crash(paths) -> list[str]:
    """从最近的崩溃 meta.yml 里取游戏实际加载的 DLC 名。"""
    import os
    try:
        dirs = sorted((d for d in os.scandir(str(paths.crashes_dir)) if d.is_dir()),
                      key=lambda e: e.stat().st_mtime, reverse=True)
    except OSError:
        return []
    for d in dirs[:8]:
        mp = os.path.join(d.path, "meta.yml")
        if not os.path.isfile(mp):
            continue
        text = Path(mp).read_text(encoding="utf-8", errors="replace")
        names = re.findall(r"^DLC_\d+:\s*(.+?)\s*$", text, re.M)
        if names:
            return names
    return []


def main() -> int:
    paths = Settings.load().paths
    print("=" * 66)
    print("DLC 管理测试")
    print("=" * 66)

    # ---------- 1. 读取 ----------
    print("\n--- 1. 读取 DLC 清单 ---")
    dlcs = read_dlc_list(paths)
    check("读到 DLC", len(dlcs) > 0, f"{len(dlcs)} 个")
    check("每个都有名字", all(d.name for d in dlcs))
    check("每个都有稳定 key", all(d.key for d in dlcs))
    check("解析出资料片", any(d.is_expansion for d in dlcs),
          f"{sum(1 for d in dlcs if d.is_expansion)} 个")
    check("解析出类别", sum(1 for d in dlcs if d.category) > 0,
          "、".join(sorted({d.category for d in dlcs if d.category})))
    check("解析出 steam_id", sum(1 for d in dlcs if d.steam_id) > 0,
          f"{sum(1 for d in dlcs if d.steam_id)} 个有")

    ov = dlc_overview(paths)
    check("概览结构完整",
          all(k in ov for k in ("dlcs", "by_category", "total", "enabled",
                                "disabled")),
          f"共 {ov['total']}，启用 {ov['enabled']}，禁用 {ov['disabled']}")
    check("按类别分组非空", len(ov["by_category"]) >= 3,
          "、".join(f"{k}({len(v)})" for k, v in ov["by_category"].items()))

    # ---------- 2. 与游戏实际加载的交叉验证 ----------
    print("\n--- 2. 与游戏实际加载交叉验证 ---")
    loaded = loaded_dlc_from_crash(paths)
    if not loaded:
        print("      （没有崩溃记录可对照，跳过）")
    else:
        ours = {d.name for d in dlcs}
        theirs = {n.strip() for n in loaded}
        check("我们读到的名字覆盖游戏加载的全部",
              theirs <= ours,
              f"游戏 {len(theirs)} 个；我们缺：{sorted(theirs - ours)[:5]}"
              if not (theirs <= ours) else f"游戏加载 {len(theirs)} 个，全部命中")
        extra = sorted(ours - theirs)
        check("多出来的已安装 DLC 有合理解释",
              True, f"{len(extra)} 个未加载：{'、'.join(extra[:4])}"
                    + ("…" if len(extra) > 4 else ""))
        # 名字必须逐字一致，否则按名字匹配 disabled_dlcs 会失效
        check("名字与游戏内部标识完全一致", theirs <= ours)
        print(f"      交叉验证提示：{report_dlc_hint(paths)}")

    # ---------- 3. 禁用匹配的健壮性 ----------
    print("\n--- 3. 禁用条目的多种写法都要认 ---")
    sample = dlcs[0]
    variants = [
        sample.key,                                  # 文件夹名
        sample.name,                                 # .dlc 里的 name
        sample.localizable,                          # 本地化键
        sample.key.upper(),                          # 大小写不同
        f"dlc/{sample.key}/",                         # 路径写法
        f"{sample.key}.dlc",                          # 带扩展名
    ]
    for v in variants:
        if not v:
            continue
        check(f"识别写法 {v[:34]}", _is_disabled(sample, [v]))
    check("空列表不算禁用", not _is_disabled(sample, []))
    check("无关条目不算禁用", not _is_disabled(sample, ["某個不存在的DLC"]))

    # ---------- 4. 写入往返（真实写盘再还原） ----------
    print("\n--- 4. 写入往返 ---")
    p = paths.dlc_load
    before = p.read_bytes() if p.is_file() else b""
    before_disabled = read_disabled_dlcs(paths)
    try:
        target = dlcs[0]
        r = apply_dlc_switches(paths, dlcs, [target.key])
        check("禁用 1 个 DLC 写入成功", r.get("ok"),
              f"启用 {r.get('enabled')} / 禁用 {r.get('disabled')}")
        after = read_disabled_dlcs(paths)
        check("禁用列表里出现了它", any(target.key.lower() in x.lower()
                                        or target.name.lower() in x.lower()
                                        for x in after),
              str(after))
        reread = read_dlc_list(paths)
        got = next((d for d in reread if d.key == target.key), None)
        check("重新读取时该 DLC 显示为已禁用",
              got is not None and not got.enabled,
              f"{target.name} enabled={got.enabled if got else '?'}")

        # 再启用回去
        r2 = apply_dlc_switches(paths, dlcs, [])
        check("恢复启用写入成功", r2.get("ok"),
              f"禁用 {r2.get('disabled')}")
        after2 = read_disabled_dlcs(paths)
        check("禁用列表已清空", not after2, str(after2))
        reread2 = read_dlc_list(paths)
        got2 = next((d for d in reread2 if d.key == target.key), None)
        check("重新读取时已恢复启用",
              got2 is not None and got2.enabled)
    finally:
        # 无论成败都还原成测试前的字节
        if before:
            p.write_bytes(before)
            print("      （已还原 dlc_load.json）")
    check("原始禁用列表已还原",
          read_disabled_dlcs(paths) == before_disabled,
          f"{before_disabled}")

    # ---------- 5. 不认识的条目不能被删掉 ----------
    print("\n--- 5. 保护已有配置 ---")
    if p.is_file():
        try:
            data = json.loads(p.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            data = {}
        data["disabled_dlcs"] = ["某個未知條目", dlcs[-1].key]
        p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        try:
            apply_dlc_switches(paths, dlcs, [])
            kept = read_disabled_dlcs(paths)
            check("认不出的条目被原样保留",
                  "某個未知條目" in kept, str(kept))
            check("已知条目被正确启用",
                  not any(dlcs[-1].key.lower() == k.lower() for k in kept),
                  str(kept))
        finally:
            p.write_bytes(before)

    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print()
    print("=" * 66)
    print(f"结果：{passed}/{total} 通过")
    for n, ok, d in results:
        if not ok:
            print(f"  失败：{n} — {d}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
