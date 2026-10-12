"""从零重建 i18n.js 的键值表：先删掉所有 `page.xNNNN` 与本项目新增的键，
再按汇总好的翻译一次写入。

为什么不能继续追加：`write_i18n_from_all.py` 的"缺哪些键就加哪些"逻辑
在反复运行后会重复插入 —— 特别是键名本身是中文的那些
（`'汉化': 'Translation'`），因为它们同时也出现在 HTML 的
`data-i18n` 反查里，两轮下来表里就出现同名两条，测试报「en 多」。

这里的做法是**幂等重建**：
1. 从 git 取一份干净的 i18n.js 作为骨架（保留原有 186 个键与全部注释）
2. 删掉骨架里所有 `^[a-z]+\.x\d{4}$` 形式的键（那些是本项目生成的）
3. 把汇总好的翻译**整体**写进去，每个键只写一条

反复运行结果一致，不会累积。
"""
from __future__ import annotations

import io
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                              errors="replace")

I18N = ROOT / "smd" / "web" / "i18n.js"
GIT = "C:/Program Files/Git/cmd/git.exe"

Q = "'"
BT = chr(96)

# 要清理掉的键形式：本项目生成的占位键
GENERATED = re.compile(r"^\s*'[a-z]+\.x\d{4}':\s*.*$", re.M)


def js_str(s: str) -> str:
    """写成单引号 JS 字面量（撇号转义）。"""
    o = s.replace("\\", "\\\\").replace(Q, "\\" + Q)
    o = o.replace("\n", "\\n").replace("\r", "")
    return Q + o + Q


def skeleton() -> str:
    """从 git 取干净的 i18n.js。"""
    r = subprocess.run([GIT, "show", "HEAD:smd/web/i18n.js"],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", cwd=str(ROOT))
    if r.returncode != 0 or not r.stdout.strip():
        print("  ！取不到 git 里的 i18n.js，改用工作区文件")
        return I18N.read_text(encoding="utf-8")
    return r.stdout


def find_table(src: str, lang: str) -> tuple[int, int]:
    m = re.search(rf"\b{lang}\s*:\s*\{{", src)
    if not m:
        raise SystemExit(f"  找不到 {lang}: {{")
    start = m.end() - 1
    depth = 0
    for i in range(start, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return start, i
    raise SystemExit(f"  {lang} 表括号不配对")


def collect() -> dict[str, str]:
    """汇总所有翻译来源 → {键: 英文}。

    键可能是 `page.xNNNN`（HTML 标注用）也可能是中文原文（JS 里 `t('…')`）。
    """
    out: dict[str, str] = {}

    def load(name: str) -> dict:
        p = ROOT / "tools" / name
        if not p.is_file():
            return {}
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    # HTML 文本：`page.xNNNN` → 英文，中文要从各批译文的键反推。
    # 直接用合并表（它已经是 key→英文）
    merged = load("_i18n_trans.json")
    for k, v in merged.items():
        if isinstance(v, str) and v.strip():
            out[k] = v
    # 中文原文键（`t('汉化')` 走这条）
    for n in ("js1", "js2"):
        for k, v in load(f"_i18n_trans_{n}.json").items():
            if isinstance(v, str) and v.strip():
                out[k] = v

    # JS 那批在合并表里是 `js.中文`，也要把不带前缀的中文加进去
    for k, v in list(out.items()):
        if k.startswith("js.") and isinstance(v, str) and v.strip():
            out.setdefault(k[3:], v)
    return out


def main() -> int:
    src = skeleton()
    print(f"  骨架: {len(src.splitlines())} 行")
    # 1. 删掉生成键（两张表里都删）
    before = len(GENERATED.findall(src))
    src = GENERATED.sub("", src)
    print(f"  清理生成键 {before} 条")

    # 2. 收集翻译，并把 HTML 标注里的中文→键 也覆盖到
    zh_en = collect()
    html = (ROOT / "smd" / "web" / "index.html").read_text(encoding="utf-8")
    for m in re.finditer(r'data-i18n="([\w.]+)"[^>]*>([^<]{1,400})<', html):
        key, text = m.group(1), m.group(2).strip()
        if not re.search(r"[\u4e00-\u9fff]", text):
            continue
        en = zh_en.get(key)
        if en:
            zh_en.setdefault(text, en)
            zh_en[key] = en
    zh_en.update({
        "nav.ai": "AI Setup", "nav.patch": "Patch Workshop",
        "nav.console": "The Worm's Archive",
        "nav.monitor": "Runtime Monitor",
    })
    zh_en = {k: v for k, v in zh_en.items() if k and v is not None}
    print(f"  待写入键 {len(zh_en)} 条")

    # 导航名的统一**不在这里做**。
    #
    # 曾经在这里加过一段"把 nav.* 替换成术语表"的 sub —— 但它没有范围
    # 限制，会连 en 表里带撇号的双引号值一起改坏
    # （`'nav.console': "The Worm's Archive"` → `'时之虫档案's Archive'`）。
    # 现在交给 `_nav_names.py`：那个脚本按 zh/en 两张表分别定位、分别设值，
    # 并且写盘前跑语法校验。

    # 3. 一次写两张表
    for lang in ("zh", "en"):
        start, end = find_table(src, lang)
        body = src[start:end]
        have = set(re.findall(r"['\"]([\w.\u4e00-\u9fff ]+)['\"]\s*:", body))
        add = {k: v for k, v in zh_en.items() if k not in have}
        if not add:
            print(f"  {lang}: 无新增")
            continue
        lines = "\n".join(
            f"      {js_str(k)}: {js_str(k if lang == 'zh' else v)},"
            for k, v in sorted(add.items()))
        head = src[:end].rstrip()
        if not head.endswith(","):
            head += ","
        src = head + "\n" + lines + "\n    " + src[end:]
        print(f"  {lang}: 插入 {len(add)} 条")

    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                     encoding="utf-8", newline="\n") as fh:
        fh.write(src)
        tmp = fh.name
    r = subprocess.run(["node", "--check", tmp], capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    Path(tmp).unlink(missing_ok=True)
    if r.returncode != 0:
        print(f"  ！语法不合法，已放弃写入\n    {(r.stderr or '')[:300]}")
        return 1
    I18N.write_text(src, encoding="utf-8", newline="\n")
    print(f"  已写入 {I18N}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
