"""代码质量审计：统计注释/文档覆盖情况，并找出遗留问题。

检查项
------
* 每个模块是否有 docstring
* 每个 class / 函数（含公开方法）是否有 docstring
* 是否存在遗留的调试代码（print / TODO / FIXME / breakpoint / pdb）
* 是否有超长函数（难以维护的信号）

用法::  python tools/code_audit.py            # 摘要
        python tools/code_audit.py --list     # 列出每条缺失
"""

from __future__ import annotations

import ast
import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

SKIP_DIRS = {".venv", "build", "dist", "__pycache__", ".git", "tools"}
DEBUG_MARKS = ("TODO", "FIXME", "XXX", "HACK", "breakpoint()", "pdb.set_trace")
LONG_FUNC = 90          # 超过这么多行算偏长


def iter_sources() -> list[Path]:
    out: list[Path] = []
    for p in sorted(ROOT.rglob("*.py")):
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        out.append(p)
    return out


def audit(path: Path) -> dict:
    src = path.read_text(encoding="utf-8")
    rel = str(path.relative_to(ROOT)).replace("\\", "/")
    info = {
        "path": rel, "lines": src.count("\n") + 1,
        "module_doc": False, "missing": [], "debug": [], "long": [],
        "classes": 0, "functions": 0,
    }
    try:
        tree = ast.parse(src, rel)
    except SyntaxError as exc:
        info["missing"].append(f"<语法错误> {exc}")
        return info

    if ast.get_docstring(tree):
        info["module_doc"] = True
    else:
        info["missing"].append("模块缺少 docstring")

    def note(fn, prefix: str = "") -> None:
        """记录一个函数/方法的文档与长度情况。"""
        name = f"{prefix}{fn.name}()"
        info["functions"] += 1
        if not ast.get_docstring(fn) and not fn.name.startswith("_"):
            info["missing"].append(f"{name} 缺 docstring")
        if fn.end_lineno and fn.end_lineno - fn.lineno > LONG_FUNC:
            info["long"].append(
                f"{name} {fn.end_lineno - fn.lineno} 行")

    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            info["classes"] += 1
            if not ast.get_docstring(node):
                info["missing"].append(f"class {node.name} 缺 docstring")
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    note(item, prefix=f"{node.name}.")
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            note(node, prefix="def ")

    for i, line in enumerate(src.splitlines(), 1):
        stripped = line.strip()
        # 忽略注释里的说明性中文（只找真正的调试残留）
        if any(m in line for m in DEBUG_MARKS):
            info["debug"].append(f"{i}: {stripped[:70]}")
        if stripped.startswith("print(") and "verify_patch" not in rel:
            # ai/ 与 patch/ 里不应有 print 调试
            if rel.startswith(("smd/ai/", "smd/patch/")):
                info["debug"].append(f"{i}: print 调试残留")
    return info


def main() -> int:
    show = "--list" in sys.argv
    rows = [audit(p) for p in iter_sources()]

    total_lines = sum(r["lines"] for r in rows)
    total_cls = sum(r["classes"] for r in rows)
    total_fn = sum(r["functions"] for r in rows)
    missing_all = [(r["path"], m) for r in rows for m in r["missing"]]
    debug_all = [(r["path"], d) for r in rows for d in r["debug"]]
    long_all = [(r["path"], x) for r in rows for x in r["long"]]
    no_mod = [r["path"] for r in rows if not r["module_doc"]]

    print("=" * 66)
    print("代码审计")
    print("=" * 66)
    print(f"源文件        : {len(rows)}")
    print(f"总行数        : {total_lines}")
    print(f"类 / 函数     : {total_cls} / {total_fn}")
    print(f"缺模块 docstring: {len(no_mod)}")
    print(f"缺 docstring  : {len(missing_all)}")
    print(f"调试残留      : {len(debug_all)}")
    print(f"偏长函数(>{LONG_FUNC}行): {len(long_all)}")

    print("\n--- 逐文件 ---")
    print(f"{'文件':<34}{'行数':>6}{'类':>4}{'函数':>5}{'缺文档':>7}{'调试':>5}")
    for r in sorted(rows, key=lambda x: -x["lines"]):
        print(f"{r['path']:<34}{r['lines']:>6}{r['classes']:>4}"
              f"{r['functions']:>5}{len(r['missing']):>7}{len(r['debug']):>5}")

    if show:
        if missing_all:
            print("\n--- 缺失的 docstring ---")
            for p, m in missing_all:
                print(f"  {p}: {m}")
        if debug_all:
            print("\n--- 调试残留 ---")
            for p, d in debug_all:
                print(f"  {p}: {d}")
        if long_all:
            print("\n--- 偏长函数 ---")
            for p, x in long_all:
                print(f"  {p}: {x}")

    print()
    print("=" * 66)
    ok = not missing_all and not debug_all
    print("结论：" + ("文档完整、无调试残留" if ok else
                    f"有 {len(missing_all)} 处缺文档、"
                    f"{len(debug_all)} 处调试残留待处理"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
