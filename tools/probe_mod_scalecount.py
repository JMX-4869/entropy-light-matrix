"""量一下用户装的模组里有多少「需要适配的内容」。

「全模组适配」听起来是句口号，实际工作量取决于三件事：
1. 有多少个建筑、区划、分区特化（OGAS 的 content pack 现在只支持这三类）
2. 有多少新建筑是模组加的（原版 17 个之外的）
3. 有哪些新资源、新科技前置（决定 role 和 required_technology 能不能自动填）

这里的数字直接决定是「跑一遍自动生成」还是「得人工过几千条」。

扫法：直接找各模组的 `common/buildings/`、`common/districts/` 等目录，
用正则抓 `xxx = {` 形式的顶层定义 —— 和 Stellaris 加载脚本的方式一致。
"""
from __future__ import annotations

import io
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

out = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

sys.path.insert(0, r"E:\yiuxi\StellarisModDoctor")
from tools.sandbox import enter_sandbox  # noqa: E402

enter_sandbox()
from smd.app import App  # noqa: E402

# Stellaris 里这几目录决定 OGAS 能不能操作
TARGETS = {
    "buildings": "common/buildings",
    "districts": "common/districts",
    "zones": "common/planet_zones",       # 区域特化
    "technology": "common/technology",
    "resources": "common/strategic_resources",
}

# 顶层定义：缩进 0 或 tab 的 `id = {`
DEF = re.compile(r"^[ \t]*([a-z][a-z0-9_]*)\s*=\s*\{\s*$", re.M)


def count_file(path: Path) -> int:
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return 0
    return len(DEF.findall(text))


def main() -> int:
    app = App()
    app.load_mods()
    mods = [m for m in app.mods_list() if m.get("enabled")]
    print(f"  启用的模组：{len(mods)} 个", file=out)

    per_kind: dict[str, Counter] = defaultdict(Counter)
    per_mod: dict[str, int] = Counter()
    missing = 0

    for m in mods:
        # 路径在 mods_list 里，**不在 mod_detail 里** —— 后者返回的是
        # concepts / mod / related_findings / sample_definitions 四块。
        # 一开始写错，量出来全是 0，差点以为模组里没有建筑定义。
        root = Path(m.get("path") or "")
        if not root.is_dir():
            # 工坊模组：从 workshop 目录反查
            wid = m.get("remote_file_id")
            if wid:
                for w in (r"e:\steam\steamapps\workshop\content\281990",):
                    cand = Path(w) / str(wid)
                    if cand.is_dir():
                        root = cand
                        break
        if not root.is_dir():
            missing += 1
            continue
        for kind, sub in TARGETS.items():
            d = root / sub
            if not d.is_dir():
                continue
            n = 0
            for f in d.glob("*.txt"):
                n += count_file(f)
            if n:
                per_kind[kind][m["key"]] = n
                per_mod[m["name"]] += n

    print(f"\n  {'类型':14} {'模组数':>6} {'定义总数':>9}", file=out)
    print("  " + "-" * 34, file=out)
    for kind in TARGETS:
        c = per_kind.get(kind, Counter())
        print(f"  {kind:14} {len(c):>6} {sum(c.values()):>9}", file=out)

    print(f"\n  贡献最多的模组：", file=out)
    for name, n in per_mod.most_common(8):
        print(f"    {n:>5}  {name[:52]}", file=out)
    if missing:
        print(f"\n  （{missing} 个模组没定位到目录，可能已退订或路径异常）", file=out)
    out.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
