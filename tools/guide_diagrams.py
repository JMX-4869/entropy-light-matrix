# -*- coding: utf-8 -*-
"""给 guide.html 插入示意图（内联 SVG）。

为什么用内联 SVG 而不是 PNG：
  · 打包只要带 guide.html 一个文件，不用管图片目录
  · 深色主题下不会出现"白底方块"，颜色跟着 CSS 变量走
  · 文字可选、可缩放，指南窗口拉大也不糊

风格跟软件本体一致：深底 #141922 / 描边 #242c38 / 主色 #4a9eff。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

GUIDE = Path(__file__).resolve().parent.parent / "smd" / "web" / "guide.html"

# ---- 通用 ---------------------------------------------------------------
DEFS = """<defs>
    <marker id="ar" viewBox="0 0 10 10" refX="9" refY="5"
      markerWidth="6" markerHeight="6" orient="auto-start-reverse">
      <path d="M0 0 L10 5 L0 10 z" fill="#4a9eff"/>
    </marker>
    <linearGradient id="gAcc" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#4a9eff" stop-opacity=".28"/>
      <stop offset="1" stop-color="#4a9eff" stop-opacity=".06"/>
    </linearGradient>
  </defs>"""

CSS = """<style>
  .dgm{margin:18px 0 10px;border:1px solid var(--line);border-radius:8px;
    background:var(--bg2);padding:18px 16px 14px}
  .dgm svg{display:block;width:100%;height:auto}
  .dgm figcaption{margin-top:10px;font-size:12px;color:var(--dim);
    text-align:center;letter-spacing:.02em}
  .bx{fill:var(--bg3);stroke:var(--line);stroke-width:1;rx:6}
  .bx.p{stroke:var(--pri);fill:url(#gAcc)}
  .bx.ok{stroke:var(--ok);fill:rgba(61,220,151,.1)}
  .bx.warn{stroke:var(--warn);fill:rgba(255,182,72,.1)}
  .bx.err{stroke:var(--err);fill:rgba(255,107,107,.1)}
  .t{fill:var(--fg);font:12px/1 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
  .t.b{font-weight:600}
  .t.s{fill:var(--mut);font-size:10.5px}
  .t.d{fill:var(--dim);font-size:9.5px}
  .ln{stroke:#3a4757;stroke-width:1.4;fill:none}
  .ln.a{stroke:#4a9eff;marker-end:url(#ar)}
  .dot{fill:var(--pri)}
</style>"""

# ---- ① 整体架构 ---------------------------------------------------------
HERO = """<figure class="dgm">
  <svg viewBox="0 0 700 168" role="img" aria-label="整体架构">
    """ + DEFS + """
    <rect class="bx" x="8" y="52" width="104" height="58"/>
    <text class="t b" x="60" y="76" text-anchor="middle">你的模组</text>
    <text class="t s" x="60" y="93" text-anchor="middle">97 个 · 20 GB</text>

    <line class="ln a" x1="116" y1="81" x2="152" y2="81"/>

    <rect class="bx p" x="156" y="52" width="104" height="58"/>
    <text class="t b" x="208" y="76" text-anchor="middle">扫描</text>
    <text class="t s" x="208" y="93" text-anchor="middle">17 秒</text>

    <line class="ln a" x1="264" y1="81" x2="300" y2="81"/>

    <rect class="bx warn" x="304" y="52" width="104" height="58"/>
    <text class="t b" x="356" y="76" text-anchor="middle">冲突清单</text>
    <text class="t s" x="356" y="93" text-anchor="middle">510 条 · 按严重度</text>

    <line class="ln a" x1="412" y1="70" x2="452" y2="34"/>
    <line class="ln a" x1="412" y1="92" x2="452" y2="128"/>

    <rect class="bx ok" x="456" y="10" width="112" height="46"/>
    <text class="t b" x="512" y="30" text-anchor="middle">排加载顺序</text>
    <text class="t s" x="512" y="46" text-anchor="middle">七层 + 作者要求</text>

    <rect class="bx ok" x="456" y="108" width="112" height="46"/>
    <text class="t b" x="512" y="128" text-anchor="middle">打补丁</text>
    <text class="t s" x="512" y="144" text-anchor="middle">本地 / AI</text>

    <rect class="bx" x="592" y="52" width="100" height="58"/>
    <text class="t b" x="642" y="76" text-anchor="middle">能进游戏</text>
    <text class="t s" x="642" y="93" text-anchor="middle">或者知道为什么不能</text>
  </svg>
  <figcaption>整个软件就干这三件事：查、排、补</figcaption>
</figure>"""

# ---- ② 三分钟上手 -------------------------------------------------------
QUICK = """<figure class="dgm">
  <svg viewBox="0 0 700 96" role="img" aria-label="上手五步">
    """ + DEFS + """
    <line class="ln" x1="40" y1="30" x2="660" y2="30" stroke-dasharray="3 4"/>
    <g>
      <circle class="dot" cx="70" cy="30" r="11"/><text class="t b" x="70" y="34" text-anchor="middle" fill="#04121f">1</text>
      <text class="t s" x="70" y="58" text-anchor="middle">看路径</text>
      <text class="t d" x="70" y="72" text-anchor="middle">自动探测</text>
    </g>
    <g>
      <circle class="dot" cx="210" cy="30" r="11"/><text class="t b" x="210" y="34" text-anchor="middle" fill="#04121f">2</text>
      <text class="t s" x="210" y="58" text-anchor="middle">开始检测</text>
      <text class="t d" x="210" y="72" text-anchor="middle">约 20 秒</text>
    </g>
    <g>
      <circle class="dot" cx="350" cy="30" r="11"/><text class="t b" x="350" y="34" text-anchor="middle" fill="#04121f">3</text>
      <text class="t s" x="350" y="58" text-anchor="middle">算最优方案</text>
      <text class="t d" x="350" y="72" text-anchor="middle">手动点</text>
    </g>
    <g>
      <circle class="dot" cx="490" cy="30" r="11"/><text class="t b" x="490" y="34" text-anchor="middle" fill="#04121f">4</text>
      <text class="t s" x="490" y="58" text-anchor="middle">应用顺序</text>
      <text class="t d" x="490" y="72" text-anchor="middle">写启动器</text>
    </g>
    <g>
      <circle cx="630" cy="30" r="11" fill="none" stroke="#4a9eff" stroke-dasharray="2 3"/>
      <text class="t b" x="630" y="34" text-anchor="middle" fill="#4a9eff">5</text>
      <text class="t s" x="630" y="58" text-anchor="middle">补丁（可选）</text>
      <text class="t d" x="630" y="72" text-anchor="middle">先本地后 AI</text>
    </g>
  </svg>
  <figcaption>第一次打开按这个顺序走，后面就看你想调什么</figcaption>
</figure>"""

# ---- ③ 路径：目录结构 ---------------------------------------------------
PATHS = """<figure class="dgm">
  <svg viewBox="0 0 700 210" role="img" aria-label="三个路径">
    """ + DEFS + """
    <rect class="bx p" x="10" y="8" width="330" height="88" rx="6"/>
    <text class="t b" x="26" y="30">用户数据</text>
    <text class="t d" x="26" y="46">Documents/Paradox Interactive/Stellaris</text>
    <line class="ln" x1="26" y1="56" x2="26" y2="74"/>
    <line class="ln" x1="26" y1="62" x2="42" y2="62"/><text class="t s" x="48" y="66">mod/ ← 本地模组</text>
    <line class="ln" x1="26" y1="74" x2="42" y2="74"/><text class="t s" x="48" y="78">save games/ ← 存档</text>
    <line class="ln" x1="26" y1="86" x2="42" y2="86"/><text class="t s" x="48" y="90">logs/ · crashes/</text>

    <rect class="bx" x="360" y="8" width="330" height="52" rx="6"/>
    <text class="t b" x="376" y="30">游戏目录</text>
    <text class="t d" x="376" y="48">steamapps/common/Stellaris</text>
    <text class="t s" x="376" y="62" opacity="0">.</text>

    <rect class="bx" x="360" y="74" width="330" height="52" rx="6"/>
    <text class="t b" x="376" y="96">模组路径（工坊）</text>
    <text class="t d" x="376" y="112">steamapps/workshop/content/281990</text>

    <rect class="bx ok" x="10" y="112" width="330" height="86" rx="6"/>
    <text class="t b" x="26" y="134">学习版 / 便携版</text>
    <text class="t s" x="26" y="152">五项都能单独指定，留空就用上面对应的默认</text>
    <text class="t d" x="26" y="170">存档 · 启动器数据库 · 日志 · 崩溃 · 额外模组目录</text>
    <text class="t d" x="26" y="188">探测时还会往上下各一层递归找，放游戏目录里也认得出</text>
  </svg>
  <figcaption>三个必填在左边和右上，右下那一块只在整合版才需要动</figcaption>
</figure>"""

# ---- ④ 冲突类型 ---------------------------------------------------------
CONFLICT = """<figure class="dgm">
  <svg viewBox="0 0 700 132" role="img" aria-label="冲突类型">
    <rect class="bx err" x="6" y="8" width="220" height="56" rx="6"/>
    <text class="t b" x="20" y="30">脚本对象重定义</text>
    <text class="t s" x="20" y="47">同名建筑 / 科技 / 特质</text>
    <text class="t d" x="20" y="60" opacity="0">.</text>

    <rect class="bx err" x="240" y="8" width="220" height="56" rx="6"/>
    <text class="t b" x="254" y="30">文件覆盖</text>
    <text class="t s" x="254" y="47">同名文件被整个盖掉</text>

    <rect class="bx warn" x="474" y="8" width="220" height="56" rx="6"/>
    <text class="t b" x="488" y="30">本地化键冲突</text>
    <text class="t s" x="488" y="47">同一个键两种译文</text>

    <rect class="bx warn" x="6" y="72" width="220" height="56" rx="6"/>
    <text class="t b" x="20" y="94">版本不符</text>
    <text class="t s" x="20" y="111">supported_version 对不上</text>

    <rect class="bx warn" x="240" y="72" width="220" height="56" rx="6"/>
    <text class="t b" x="254" y="94">依赖缺失</text>
    <text class="t s" x="254" y="111">声明要的模组没装</text>

    <rect class="bx" x="474" y="72" width="220" height="56" rx="6"/>
    <text class="t b" x="488" y="94">文件完整性</text>
    <text class="t s" x="488" y="111">描述符写的东西不存在</text>
  </svg>
  <figcaption>红框是真会让游戏出问题的，黄框先看数量再决定要不要管</figcaption>
</figure>"""

# ---- ⑤ 七层排序 ---------------------------------------------------------
LAYERS = """<figure class="dgm">
  <svg viewBox="0 0 700 224" role="img" aria-label="七层加载顺序">
    """ + DEFS + """
    <text class="t d" x="8" y="14">先加载</text>
    <text class="t d" x="8" y="216">后加载</text>
    <line class="ln" x1="52" y1="16" x2="52" y2="206" stroke-dasharray="2 4"/>

    <rect class="bx p" x="62" y="8" width="180" height="26" rx="4"/>
    <text class="t s" x="74" y="26">① 前置框架 / 基础库</text>
    <text class="t d" x="250" y="26">别人依赖它</text>

    <rect class="bx p" x="62" y="40" width="240" height="26" rx="4"/>
    <text class="t s" x="74" y="58">② 大型机制重做</text>
    <text class="t d" x="310" y="58">改动最多</text>

    <rect class="bx" x="62" y="72" width="200" height="26" rx="4"/>
    <text class="t s" x="74" y="90">③ 常规内容</text>
    <text class="t d" x="270" y="90">加东西不改机制</text>

    <rect class="bx" x="62" y="104" width="220" height="26" rx="4"/>
    <text class="t s" x="74" y="122">④ 界面 UI</text>
    <text class="t d" x="290" y="122">盖住上面改的界面</text>

    <rect class="bx warn" x="62" y="136" width="260" height="26" rx="4"/>
    <text class="t s" x="74" y="154">⑤ 兼容补丁 / Bug 修复</text>
    <text class="t d" x="330" y="154">专修别人的冲突</text>

    <rect class="bx" x="62" y="168" width="160" height="26" rx="4"/>
    <text class="t s" x="74" y="186">⑥ 小型调整</text>

    <rect class="bx ok" x="62" y="200" width="196" height="20" rx="4"/>
    <text class="t s" x="74" y="215">⑦ 汉化 / Override</text>
    <text class="t d" x="266" y="215">必须最后</text>

    <line class="ln a" x1="348" y1="210" x2="348" y2="30" stroke-dasharray="3 4" opacity=".5"/>
    <text class="t d" x="356" y="120" opacity=".7">起点递增</text>
    <text class="t d" x="356" y="134" opacity=".7">= 排对了</text>
  </svg>
  <figcaption>区间会重叠（跨层约束造成的），判断标准是每层起点在变大，不是区间不交</figcaption>
</figure>"""

# ---- ⑥ 补丁五种方式 -----------------------------------------------------
PATCH = """<figure class="dgm">
  <svg viewBox="0 0 700 118" role="img" aria-label="补丁五种方式">
    <rect class="bx p" x="6" y="8" width="132" height="42" rx="6"/>
    <text class="t b" x="18" y="27">AI 生成</text>
    <text class="t d" x="18" y="42">复杂 · 花钱</text>

    <rect class="bx ok" x="146" y="8" width="132" height="42" rx="6"/>
    <text class="t b" x="158" y="27">本地自动</text>
    <text class="t d" x="158" y="42">机械活 · 免费</text>

    <rect class="bx" x="286" y="8" width="132" height="42" rx="6"/>
    <text class="t b" x="298" y="27">自己写</text>
    <text class="t d" x="298" y="42">你要会脚本</text>

    <rect class="bx p" x="426" y="8" width="132" height="42" rx="6"/>
    <text class="t b" x="438" y="27">批量生成</text>
    <text class="t d" x="438" y="42">几十条一起</text>

    <rect class="bx warn" x="566" y="8" width="128" height="42" rx="6"/>
    <text class="t b" x="578" y="27">一键翻译</text>
    <text class="t d" x="578" y="42">补中文 · 花钱</text>

    <path class="ln a" d="M72 56 L72 76 L350 76 L350 88"/>
    <text class="t s" x="360" y="80">生成 ≠ 生效</text>
    <text class="t d" x="360" y="96">还要点「保存成补丁模组」，才会写成真的模组文件</text>
  </svg>
  <figcaption>推荐路线：先用本地跑一遍（免费），剩下的再考虑 AI</figcaption>
</figure>"""

# ---- ⑦ AI 请求与 token 预估 ---------------------------------------------
TOKEN = """<figure class="dgm">
  <svg viewBox="0 0 700 176" role="img" aria-label="token 预估与自校准">
    """ + DEFS + """
    <rect class="bx" x="8" y="14" width="120" height="46" rx="6"/>
    <text class="t s" x="20" y="34">选中的目标</text>
    <text class="t d" x="20" y="50">114 条</text>

    <line class="ln a" x1="132" y1="37" x2="168" y2="37"/>

    <rect class="bx p" x="172" y="14" width="150" height="46" rx="6"/>
    <text class="t s" x="184" y="34">抽 5 条真建 prompt</text>
    <text class="t d" x="184" y="50">只读文件 · 不联网 · 不花钱</text>

    <line class="ln a" x1="326" y1="37" x2="362" y2="37"/>

    <rect class="bx warn" x="366" y="14" width="140" height="46" rx="6"/>
    <text class="t s" x="378" y="34">取平均 × 条数</text>
    <text class="t d" x="378" y="50">≈ 140 万 token</text>

    <line class="ln a" x1="510" y1="37" x2="546" y2="37"/>

    <rect class="bx" x="550" y="14" width="142" height="46" rx="6"/>
    <text class="t s" x="562" y="34">给你看 + 粗算 ¥</text>
    <text class="t d" x="562" y="50">跑之前心里有数</text>

    <line class="ln a" x1="370" y1="64" x2="370" y2="96"/>

    <rect class="bx ok" x="270" y="100" width="200" height="46" rx="6"/>
    <text class="t s" x="282" y="120">真跑完 → 实际用量</text>
    <text class="t d" x="282" y="136">服务商返回的 usage，不是估的</text>

    <line class="ln a" x1="270" y1="123" x2="120" y2="123"/>
    <path class="ln a" d="M120 123 L120 100 L60 100 L60 64"/>

    <rect class="bx" x="8" y="72" width="112" height="44" rx="6"/>
    <text class="t s" x="20" y="91">校准系数</text>
    <text class="t d" x="20" y="106">实际 ÷ 估算，滚动修正</text>

    <text class="t d" x="520" y="120">跑得越多越贴实际</text>
    <text class="t d" x="520" y="136">系数存在 %LOCALAPPDATA%</text>
  </svg>
  <figcaption>预估 → 实跑 → 拿实际修正系数 → 下次更准，这个环是闭的</figcaption>
</figure>"""

# ---- ⑧ 隐私边界 ---------------------------------------------------------
PRIVACY = """<figure class="dgm">
  <svg viewBox="0 0 700 118" role="img" aria-label="隐私边界">
    <rect class="bx ok" x="8" y="8" width="200" height="100" rx="6"/>
    <text class="t b" x="22" y="30">会发出去</text>
    <text class="t s" x="22" y="50">· 冲突涉及的文件</text>
    <text class="t s" x="22" y="68">· 冲突描述、模组名</text>
    <text class="t s" x="22" y="86">· 预览里能看全</text>

    <rect class="bx err" x="226" y="8" width="200" height="100" rx="6"/>
    <text class="t b" x="240" y="30">绝不发</text>
    <text class="t s" x="240" y="50">· 你的存档</text>
    <text class="t s" x="240" y="68">· 账号信息</text>
    <text class="t s" x="240" y="86">· 其它模组的文件</text>

    <rect class="bx warn" x="444" y="8" width="248" height="100" rx="6"/>
    <text class="t b" x="458" y="30">总闸默认是关的</text>
    <text class="t s" x="458" y="50">「允许发送模组内容」不开，</text>
    <text class="t s" x="458" y="68">AI 功能会直接拒绝，</text>
    <text class="t s" x="458" y="86">而不是偷偷发。</text>
  </svg>
  <figcaption>预览那一步不联网，可以随便点开看要发什么</figcaption>
</figure>"""

INSERTS: list[tuple[str, str]] = [
    ('<section id="intro">', HERO),
    ('<section id="quick">', QUICK),
    ('<section id="paths">', PATHS),
    ('<section id="conflict">', CONFLICT),
    ('<section id="order">', LAYERS),
    ('<section id="patch">', PATCH),
    ('<section id="ai-token">', TOKEN),
    ('<section id="ai-privacy">', PRIVACY),
]


def main() -> int:
    html = GUIDE.read_text(encoding="utf-8")

    # 样式插在第一个 </style> 前
    if "class=\"dgm\"" not in html:
        html = html.replace("</style>", CSS + "</style>", 1)

    added = 0
    for anchor, svg in INSERTS:
        i = html.find(anchor)
        if i < 0:
            print(f"  ! 找不到 {anchor}")
            continue
        # 插在 section 开头之后、第一个 <h2>…</h2> 之后
        h2_end = html.find("</h2>", i)
        if h2_end < 0:
            continue
        pos = h2_end + len("</h2>")
        # 已经插过就不重复
        seg = html[pos:pos + 200]
        if 'class="dgm"' in seg:
            continue
        html = html[:pos] + "\n  " + svg + html[pos:]
        added += 1

    GUIDE.write_text(html, encoding="utf-8", newline="\n")
    size = len(GUIDE.read_bytes())
    print(f"  插入 {added} 张图，guide.html 现在 {size:,} 字节")
    return 0


if __name__ == "__main__":
    sys.exit(main())
