"""删掉 i18n.js 里"HTML 已不再引用、且中文值仍是键名"的孤立条目。

来源：修 `data-i18n` 嵌套时把外层标注挪到了内部 span 上，
旧键（`ai.x0008` 等）就没人引用了。它们的中文值还是键名本身
（rebuild 时给 zh 表写的就是键名），留着既无意义，
又让"中文表里有键名当值"的检查一直报警。
"""
import io
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                              errors="replace")

ROOT = Path(__file__).resolve().parent.parent
P = ROOT / "smd" / "web" / "i18n.js"
HTML = ROOT / "smd" / "web" / "index.html"

html = HTML.read_text(encoding="utf-8")
src = P.read_text(encoding="utf-8")

# 定位 zh 表
m = re.search(r"\bzh\s*:\s*\{", src)
a = m.end() - 1
depth = 0
b = len(src)
for i in range(a, len(src)):
    if src[i] == "{":
        depth += 1
    elif src[i] == "}":
        depth -= 1
        if depth == 0:
            b = i
            break
body = src[a:b]

# 匹配 `'key': 'value',`（value 里可能有转义）
PAIR = re.compile(r"^\s*'([\w.]+)':\s*'(.*)',?\s*$", re.M)
orphans = []
for line in body.split("\n"):
    mm = PAIR.match(line)
    if not mm:
        continue
    k, v = mm.group(1), mm.group(2)
    if k != v or not re.search(r"\.x\d{4}$", k):
        continue
    if f'data-i18n="{k}"' in html:
        continue
    orphans.append(k)
print(f"  zh 表里的孤立条目 {len(orphans)} 个")
for k in orphans[:6]:
    print(f"    {k}")

# 逐行删除（两张表都删）
removed = 0
kept = []
for line in src.split("\n"):
    mm = PAIR.match(line)
    if mm and mm.group(1) in orphans:
        removed += 1
        continue
    kept.append(line)
src = "\n".join(kept)
print(f"  删除 {removed} 行")

with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                 encoding="utf-8", newline="\n") as fh:
    fh.write(src)
    tmp = fh.name
r = subprocess.run(["node", "--check", tmp], capture_output=True, text=True,
                   encoding="utf-8", errors="replace")
Path(tmp).unlink(missing_ok=True)
if r.returncode != 0:
    print(f"  ！语法不合法，已放弃\n    {(r.stderr or '')[:250]}")
    sys.exit(1)
P.write_text(src, encoding="utf-8", newline="\n")
print("  已写入")
