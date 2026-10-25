"""前端静态检查 —— 专查"点了没反应 / 页面空白"这一类。

## 为什么是这几条

前面踩过的坑，成因完全不同，但表现一模一样：**点了没反应、页面一片空白**。

  · `env.openGuide()` —— env 是手写对象，后端有方法不等于前端就有
  · `$('sevChips')`   —— 那个 id 在静态 HTML 里，但整块容器被 innerHTML
                          换掉之后就没了，拿到的永远是 null
  · `S.findings`      —— 变量名写错一个字母，undefined.length 抛 TypeError，
                          整个 refresh() 从中间断掉
  · `querySelector('.x[aria-label=t("中文")] svg')`
                      —— 把 t(...) 的源码拼进了选择器

这四条的共同点：**语法合法、跑起来才炸，而且炸得很难看**。

## 检查项

① `api('x')` 调的，webui.py 里得有
② `env.x` 用的，env 对象里得有
③ `$('x')` / getElementById 引用的 id，HTML 或 JS 模板串里得真有
④ `querySelector` 参数里不能有裸的 `t(` / `$(` 源码
⑤ HTML 里不能有重复 id
⑥ `<script>` 之外不能写 `${t(...)}`

## 不查什么

不查业务逻辑 —— 那要跑起来。也**不追求条数**
（第一版 6 类跑出 720 条、99% 误报，看一眼就关了）。
每条输出都要能说清"哪个名字、为什么不行"。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HTML = ROOT / "smd" / "web" / "index.html"
WEBUI = ROOT / "smd" / "webui.py"


def strip_comments(s: str) -> str:
    s = re.sub(r"/\*.*?\*/", "", s, flags=re.S)
    s = re.sub(r"(?m)^\s*//.*$", "", s)
    return s


def main() -> int:
    raw = HTML.read_text(encoding="utf-8")
    scripts = "\n;\n".join(re.findall(r"<script>(.*?)</script>", raw, re.S))
    js = strip_comments(scripts)
    problems: list[str] = []

    # ---- ① api('x') vs 后端 ------------------------------------------
    called = set(re.findall(r"""api\(\s*['"]([a-zA-Z_]\w*)['"]""", js))
    if WEBUI.is_file():
        py = WEBUI.read_text(encoding="utf-8")
        backend = set(re.findall(r"def\s+([a-zA-Z_]\w*)\s*\(", py))
        for m in sorted(called - backend):
            problems.append(f"api('{m}') —— webui.py 里没有这个方法")
    print(f"  ① api 方法 {len(called)} 个")

    # ---- ② env.x 用的，env 里得有 ------------------------------------
    env_methods: set[str] = set()
    m = re.search(r"const env\s*=\s*\{(.*?)\n\};", js, re.S)
    if not m:
        m = re.search(r"\benv\s*=\s*\{(.*?)\n\};", js, re.S)
    if m:
        env_methods = set(re.findall(r"(?m)^\s{2}(?:async\s+)?([a-zA-Z_$][\w$]*)\s*\(",
                                     m.group(1)))
    # 查 js（已剥注释）而不是 raw —— 注释里出现 "env.xxx" 是举例，不是引用
    used = set(re.findall(r"\benv\.([a-zA-Z_$][\w$]*)", js))
    for u in sorted(used - env_methods):
        problems.append(f"env.{u} —— env 里没有这个方法（手写对象，必须自己定义）")
    print(f"  ② env 方法 {len(env_methods)} 个，被引用 {len(used)} 个")

    # ---- ③ $('x') 引用的 id 得存在 -----------------------------------
    ids_static = set(re.findall(r'\bid="([^"\']+)"', raw))
    ids_js = set(re.findall(r"""\bid=\\?["']([A-Za-z][\w-]*)\\?["']""", scripts))
    known = ids_static | ids_js
    refs = set(re.findall(r"""\$\(\s*['"]([A-Za-z][\w-]*)['"]\s*\)""", js))
    refs |= set(re.findall(r"""getElementById\(\s*['"]([A-Za-z][\w-]*)['"]""", js))
    # 这些是 JS 里 createElement 之后赋的 id，静态分析看不到
    DYNAMIC_IDS = {"fatalBar", "toastBox", "tipBox"}
    missing = sorted(refs - known - DYNAMIC_IDS)
    for mm in missing:
        problems.append(f"$('{mm}') —— id 不存在（静态 HTML 和 JS 模板串里都没有）")
    print(f"  ③ 引用 id {len(refs)} 个，缺失 {len(missing)} 个")

    # ---- ④ querySelector 里不能有裸 t( / $( ---------------------------
    bad_sel = 0
    for mm in re.finditer(r"""querySelector(?:All)?\(\s*([^\n]{0,160})""", js):
        frag = mm.group(1)
        if re.search(r"(?<![\w.$])t\(", frag) or re.search(r"(?<![\w.$])\$\(", frag):
            ln = js[:mm.start()].count("\n") + 1
            problems.append(f"L~{ln} querySelector 参数里有裸 t( / $( —— "
                            f"源码会进选择器：{frag.strip()[:70]}")
            bad_sel += 1
    print(f"  ④ querySelector 可疑 {bad_sel} 处")

    # ---- ⑤ 重复 id --------------------------------------------------
    seen: dict[str, int] = {}
    for i in re.findall(r'\bid="([^"\']+)"', raw):
        seen[i] = seen.get(i, 0) + 1
    dups = [k for k, v in seen.items() if v > 1]
    for d in dups:
        problems.append(f"重复 id: {d}（getElementById 只拿第一个）")
    print(f"  ⑤ id {len(seen)} 个，重复 {len(dups)} 个")

    # ---- ⑥ <script> 外的 ${t(...)} -----------------------------------
    outside = re.sub(r"<script>.*?</script>", "", raw, flags=re.S)
    bad_t = 0
    for ln, line in enumerate(outside.splitlines(), 1):
        if "${t(" in line:
            problems.append(f"L{ln} <script> 之外写了 ${{t(...)}}，不会被求值")
            bad_t += 1
    print(f"  ⑥ script 外模板串 {bad_t} 处")

    # ---- 输出 --------------------------------------------------------
    if not problems:
        print("\n  ✓ 六项全过")
        return 0
    print(f"\n  发现 {len(problems)} 个问题：")
    for p in problems:
        print(f"    · {p}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
