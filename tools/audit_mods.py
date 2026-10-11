"""弄清「有问题的模组」里，哪些是真的没法启用，哪些是我们误挡了。

三种情况必须分开：

1. **磁盘上真的没有内容** —— 工坊目录和 .mod 都不存在。
   这种情况启用也没用，游戏读不到；应该明确告诉用户而不是给个能点的开关。
2. **内容在，只是元数据有问题**（版本不一致等）—— 应该能正常启用。
3. **内容在，但 .mod 描述符被误判** —— 属于我们的 bug，必须修。

用法::  python tools\\audit_mods.py
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from smd.app import App  # noqa: E402
from smd.config import Settings  # noqa: E402


def main() -> int:
    paths = Settings.load().paths
    app = App()
    app.load_mods()
    mods = app.library.all()

    mod_dir = Path(paths.user_dir) / "mod" if paths.user_dir else None
    ws_dirs = [Path(w) for w in (paths.workshop_dirs or [])]

    print("=" * 84)
    print("模组状态审计")
    print("=" * 84)
    print(f"mod 目录   : {mod_dir}")
    print(f"工坊目录   : {[str(w) for w in ws_dirs]}")
    print()

    really_gone, metadata_only, misjudged, fine = [], [], [], []

    for m in mods:
        cpath = m.resolve_content_path()
        # 磁盘上到底有没有内容？
        on_disk = bool(cpath and Path(cpath).is_dir())
        if not on_disk and m.remote_file_id:
            cand = [w / str(m.remote_file_id) for w in ws_dirs]
            on_disk = any(c.is_dir() for c in cand)
        has_mod_file = bool(m.mod_file and Path(m.mod_file).is_file())
        has_problems = bool(m.problems)

        if not on_disk and not has_mod_file:
            really_gone.append(m)
        elif on_disk and not has_mod_file:
            misjudged.append(m)          # 内容在，但我们说没描述符
        elif has_problems:
            metadata_only.append(m)
        else:
            fine.append(m)

    def show(title: str, items: list, note: str) -> None:
        print(f"--- {title}：{len(items)} 个 ---")
        if note:
            print(f"    {note}")
        for m in items[:40]:
            print(f"    · {m.name[:52]:54} enabled={m.enabled_in_playset}"
                  f"  {('; '.join(m.problems))[:60] if m.problems else ''}")
        if len(items) > 40:
            print(f"    … 还有 {len(items)-40} 个")
        print()

    show("① 磁盘上真的没有内容（启用也没用）", really_gone,
         "工坊目录与 .mod 描述符都不存在 —— 多半是已退订/被清理，"
         "但启动器数据库还留着记录")
    show("② 内容在、但缺 .mod 描述符（我们的问题，要修）", misjudged, "")
    show("③ 内容在、只是元数据有告警（应可正常启用）", metadata_only, "")
    show("④ 完全正常", fine, "")

    print("=" * 84)
    print("结论")
    print("=" * 84)
    print(f"  真正无法启用的（磁盘无内容）: {len(really_gone)}")
    print(f"  被我们误判、其实能启用的    : {len(misjudged)}")
    print(f"  有告警但能启用的            : {len(metadata_only)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
