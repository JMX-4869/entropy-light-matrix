"""检查界面用到的载荷体积，确认桥接序列化不会成为瓶颈。

用法::  python tools/payload_check.py
"""

from __future__ import annotations

import io
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from smd.app import App  # noqa: E402


def size(obj) -> int:
    return len(json.dumps(obj, ensure_ascii=False))


def main() -> int:
    app = App()
    print("加载模组…")
    lm = app.load_mods()
    print(f"  loadMods 载荷        : {size(lm):,} 字节")
    print(f"  模组数               : {len(lm['mods'])}")
    print(f"  单个模组条目         : {size(lm['mods'][0]):,} 字节")

    print("扫描…")
    t0 = time.time()
    app.start_scan()
    while app.task.running:
        time.sleep(0.2)
    print(f"  完成，用时 {time.time() - t0:.1f}s")

    st = app.status()
    cf = st.get("conflicts") or {}
    print(f"  status() 载荷        : {size(st):,} 字节")
    print(f"  其中 conflicts       : {size(cf):,} 字节")
    finds = cf.get("findings") or []
    print(f"  findings 条数        : {len(finds)}")
    print(f"  kinds 条数           : {len(cf.get('kinds') or [])}")
    if finds:
        sizes = sorted((size(f) for f in finds), reverse=True)
        print(f"  最大/中位 finding    : {sizes[0]:,} / {sizes[len(sizes)//2]:,} 字节")

    f = app.get_findings_payload = app.findings("all", "all", 500)
    print(f"  getFindings 载荷     : {size(f):,} 字节 ({len(f)} 条)")

    print("求解…")
    t0 = time.time()
    sol = app.compute_solution()
    print(f"  完成，用时 {time.time() - t0:.1f}s")
    if "error" in sol:
        print("  错误：", sol["error"])
        return 1
    print(f"  solution 载荷        : {size(sol):,} 字节")
    print(f"  plans/order/patches  : {len(sol['plans'])} / "
          f"{len(sol['order'])} / {len(sol['patches'])}")
    print(f"  禁用/启用/调整       : {len(sol['disabled'])} / "
          f"{len(sol['to_enable'])} / {sol['change_count']}")
    print(f"  健康分               : {sol['score_before']} -> {sol['score_after']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
