r"""生成 GitHub Pages 介绍站（docs/index.html）。

为什么用脚本生成而不是手写 HTML
-------------------------------
页面里的**数字**（模组数、冲突数、索引文件数、知识库条数、测试项数）
会随开发变动。硬编码进 HTML 的话，每次改数据都要手工同步两处，
迟早对不上。这里从真实来源读：

* 知识库条数 ← smd/data/wiki.json
* 测试项数   ← tools/*_test.py 里 check( 的出现次数
* 版本号     ← smd/__init__.py

其余数字（模组数、冲突数）来自一次真实扫描，写在 STATS 常量里并注明来源。

图标内嵌成 base64：省一次请求，也不用担心相对路径在 Pages 上出错。

用法::

    python tools/make_site.py
"""
from __future__ import annotations

import base64
import io
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "index.html"
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                              errors="replace")

REPO = "https://github.com/JMX-4869/entropy-light-matrix"
SITE = "https://jmx-4869.github.io/entropy-light-matrix/"

# 这几个数来自一次真实扫描（2026-10，94 个模组的那次）
STATS = {
    "mods_total": 94,
    "mods_enabled": 76,
    "conflicts": 510,
    "critical": 64,
    "indexed": 46715,
}


def read_numbers() -> dict:
    """从真实来源读出版本、知识库条数与测试项数。"""
    out = {}
    # 版本
    try:
        src = (ROOT / "smd" / "__init__.py").read_text(encoding="utf-8")
        m = re.search(r'__version__\s*=\s*["\']([^"\']+)', src)
        out["version"] = m.group(1) if m else "1.0.7"
    except OSError:
        out["version"] = "1.0.7"
    # 知识库
    try:
        d = json.loads((ROOT / "smd" / "data" / "wiki.json")
                       .read_text(encoding="utf-8"))
        out["wiki_entries"] = d["meta"]["entries"]
        out["wiki_sections"] = d["meta"]["sections"]
        out["wiki_version"] = d["meta"]["version"]
    except (OSError, ValueError, KeyError):
        out["wiki_entries"] = 0
        out["wiki_sections"] = 0
        out["wiki_version"] = "?"
    # 测试项数：数 check( 调用，近似断言数
    n = 0
    files = 0
    for p in (ROOT / "tools").glob("*_test.py"):
        files += 1
        try:
            n += p.read_text(encoding="utf-8").count("check(")
        except OSError:
            pass
    out["tests"] = n
    out["test_files"] = files
    return out


def icon_data_uri() -> str:
    """把软件图标转成 data URI 内嵌。"""
    p = ROOT / "smd" / "web" / "icon.png"
    if not p.is_file():
        return ""
    b64 = base64.b64encode(p.read_bytes()).decode("ascii")
    return f"data:image/png;base64,{b64}"


# 图标用内联 SVG：不引外部图标库（Pages 上少一次请求，也不会有 404）
def svg(paths: str, size: int = 22) -> str:
    return (f'<svg viewBox="0 0 24 24" width="{size}" height="{size}" '
            f'fill="none" stroke="currentColor" stroke-width="1.8" '
            f'stroke-linecap="round" stroke-linejoin="round">{paths}</svg>')


ICONS = {
    "conflict": svg('<path d="M12 3v6"/><path d="M12 15v6"/>'
                    '<circle cx="12" cy="12" r="2.4"/>'
                    '<path d="M5 8 3 6M19 8l2-2M5 16l-2 2M19 16l2 2"/>'),
    "order": svg('<path d="M4 6h10M4 12h15M4 18h7"/>'
                 '<path d="M18 3v6M15 6l3 3 3-3"/>'),
    "patch": svg('<path d="M14 4l6 6-9.5 9.5a2.1 2.1 0 0 1-3-3z"/>'
                 '<path d="M4 20h5"/>'),
    "crash": svg('<path d="M12 2 3 7v6c0 5 4 8 9 9 5-1 9-4 9-9V7z"/>'
                 '<path d="M12 8v5M12 16h.01"/>'),
    "archive": svg('<path d="M4 5h16v4H4z"/><path d="M6 9v10h12V9"/>'
                   '<path d="M10 13h4"/>'),
    "ai": svg('<path d="M12 3v3M12 18v3M3 12h3M18 12h3M5.6 5.6l2.1 2.1'
              'M16.3 16.3l2.1 2.1M18.4 5.6l-2.1 2.1M7.7 16.3l-2.1 2.1"/>'
              '<circle cx="12" cy="12" r="3.2"/>'),
}


def feature(icon: str, title: str, desc: str) -> str:
    return f'''      <article class="card">
        <div class="ico">{ICONS[icon]}</div>
        <h3>{title}</h3>
        <p>{desc}</p>
      </article>'''


def shot(fname: str, cap: str) -> str:
    return (f'        <figure><img src="screenshots/{fname}" '
            f'alt="{cap}" loading="lazy">'
            f'<figcaption>{cap}</figcaption></figure>')


def build(n: dict) -> str:
    w = n["wiki_entries"]
    ws = n["wiki_sections"]
    tests = n["tests"]
    icon = icon_data_uri()
    ver = n["version"]

    features = "\n".join([
        feature("conflict", "6 类冲突检测",
                "文件覆盖、脚本对象重定义、本地化键冲突、版本不兼容、"
                "文件完整性、被大量覆盖 —— 逐条指出「谁覆盖了谁」。"),
        feature("order", "一键最优排序",
                "按内容分层求解加载顺序。<b>不改动任何模组</b>，只改先后，"
                "风险最低；写回前自动备份，随时回滚。"),
        feature("patch", "补丁工坊",
                "本地规则引擎不联网、免费、快；AI 引擎处理需要理解语义的"
                "冲突。所有补丁统一存成一个补丁模组。"),
        feature("crash", "崩溃日志中文化",
                "几万行英文报错自动归类、翻译、加成因注释，"
                "产出可以直接发给别人的诊断报告。"),
        feature("archive", "时之虫档案",
                f"{w} 条群星知识库、{ws} 个分类。搜索即查，支持中文提问，"
                "命令一键送进游戏控制台。"),
        feature("ai", "AI 接管",
                f"深度解析全部模组 + 原版 + DLC（{STATS['indexed']} 个文件），"
                "先出方案、你确认后才写盘。"),
    ])

    shots = "\n".join([
        shot("01_总览.png", "总览"),
        shot("02_冲突检测.png", "冲突检测"),
        shot("03_AI接管.png", "AI 接管"),
        shot("04_时之虫档案.png", "时之虫档案"),
        shot("05_AI生成外观.png", "AI 生成外观"),
        shot("06_崩溃AI分析.png", "崩溃 AI 分析"),
    ])

    return f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>熵光矩阵 · 群星模组冲突诊断与智能排序启动器</title>
<meta name="description" content="《群星》模组冲突诊断与智能排序启动器。6 类冲突检测、一键最优排序、本地与 AI 双引擎生成兼容补丁、崩溃日志中文化，内置 {w} 条知识库。">
<meta property="og:title" content="熵光矩阵 · Entropy Light Matrix">
<meta property="og:description" content="《群星》模组冲突诊断与智能排序启动器">
<meta property="og:image" content="{SITE}social-preview.png">
<meta property="og:url" content="{SITE}">
<meta name="twitter:card" content="summary_large_image">
<link rel="icon" href="{icon}">
<style>
:root{{
  --bg:#05080f; --bg2:#0a0f1a; --panel:#111827; --panel2:#151d2e;
  --line:#1e293b; --line2:#28354d;
  --fg:#e6edf7; --dim:#9fb0cc; --dim2:#6b7d99;
  --acc:#38bdf8; --acc2:#a78bfa; --ok:#34d399; --warn:#fbbf24;
  --radius:14px;
}}
*{{box-sizing:border-box}}
html{{scroll-behavior:smooth}}
body{{
  margin:0; background:var(--bg); color:var(--fg);
  font:16px/1.75 -apple-system,BlinkMacSystemFont,"Segoe UI",
       "Microsoft YaHei UI","Microsoft YaHei",sans-serif;
  -webkit-font-smoothing:antialiased;
}}
a{{color:var(--acc);text-decoration:none}}
a:hover{{text-decoration:underline}}
code{{
  font:13px/1.5 ui-monospace,Consolas,"Cascadia Mono",monospace;
  background:var(--panel2); padding:2px 6px; border-radius:5px;
  color:#b9c6e0;
}}
.wrap{{max-width:1080px;margin:0 auto;padding:0 22px}}

/* ---------- 顶栏 ---------- */
header{{
  position:sticky; top:0; z-index:20;
  background:rgba(5,8,15,.86); backdrop-filter:blur(12px);
  border-bottom:1px solid var(--line);
}}
.hd{{display:flex;align-items:center;gap:14px;height:62px}}
.hd img{{width:32px;height:32px;border-radius:9px}}
.hd b{{font-size:16px;font-weight:700;letter-spacing:.5px}}
.hd .en{{color:var(--dim2);font-size:12px;letter-spacing:2px;
  text-transform:uppercase}}
.hd nav{{margin-left:auto;display:flex;gap:22px;font-size:14px}}
.hd nav a{{color:var(--dim)}}
.hd nav a:hover{{color:var(--fg);text-decoration:none}}

/* ---------- 首屏 ---------- */
.hero{{
  position:relative; overflow:hidden;
  padding:96px 0 76px; text-align:center;
  background:
    radial-gradient(900px 420px at 22% 12%,rgba(167,139,250,.13),transparent 62%),
    radial-gradient(900px 420px at 80% 62%,rgba(56,189,248,.12),transparent 62%),
    var(--bg);
}}
.hero::after{{
  content:""; position:absolute; inset:0; pointer-events:none;
  background-image:radial-gradient(rgba(255,255,255,.5) 1px,transparent 1px),
                   radial-gradient(rgba(255,255,255,.32) 1px,transparent 1px);
  background-size:170px 170px,90px 90px;
  background-position:0 0,40px 60px; opacity:.28;
}}
.hero>*{{position:relative;z-index:1}}
.hero .mark{{width:96px;height:96px;border-radius:24px;
  box-shadow:0 0 64px rgba(56,189,248,.35);margin-bottom:26px}}
h1{{font-size:60px;line-height:1.15;margin:0 0 8px;font-weight:800;
  letter-spacing:1px}}
.hero .sub{{color:var(--acc);font-size:13px;letter-spacing:4px;
  text-transform:uppercase;margin-bottom:22px}}
.hero .tag{{color:var(--dim);font-size:19px;margin:0 0 34px}}
.btns{{display:flex;gap:14px;justify-content:center;flex-wrap:wrap}}
.btn{{
  display:inline-flex;align-items:center;gap:9px;
  padding:13px 26px;border-radius:11px;font-size:15px;font-weight:600;
  border:1px solid var(--line2);color:var(--fg);background:var(--panel);
  transition:.16s;
}}
.btn:hover{{text-decoration:none;border-color:var(--acc);
  transform:translateY(-1px)}}
.btn.pri{{background:linear-gradient(135deg,#38bdf8,#a78bfa);
  color:#04121f;border-color:transparent}}
.btn.pri:hover{{box-shadow:0 8px 26px rgba(56,189,248,.3)}}

/* ---------- 数字条 ---------- */
.stats{{
  display:grid;grid-template-columns:repeat(4,1fr);gap:16px;
  margin:0 0 8px;
}}
.stat{{background:var(--panel);border:1px solid var(--line);
  border-radius:var(--radius);padding:20px 16px;text-align:center}}
.stat b{{display:block;font-size:32px;font-weight:800;
  font-variant-numeric:tabular-nums}}
.stat span{{color:var(--dim);font-size:13px}}
.stat:nth-child(1) b{{color:var(--acc)}}
.stat:nth-child(2) b{{color:var(--acc2)}}
.stat:nth-child(3) b{{color:var(--ok)}}
.stat:nth-child(4) b{{color:var(--warn)}}

/* ---------- 区块 ---------- */
section{{padding:76px 0}}
section+section{{border-top:1px solid var(--line)}}
h2{{font-size:32px;margin:0 0 10px;font-weight:700}}
h2 .l{{display:block;width:64px;height:4px;border-radius:2px;
  background:var(--acc2);margin-top:14px}}
.lead{{color:var(--dim);margin:0 0 34px;max-width:640px}}

/* ---------- 视频 ---------- */
.video{{border-radius:var(--radius);overflow:hidden;
  border:1px solid var(--line2);background:#000;
  box-shadow:0 22px 64px rgba(0,0,0,.6)}}
.video video{{width:100%;display:block}}

/* ---------- 功能卡 ---------- */
.grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:18px}}
.card{{background:var(--panel);border:1px solid var(--line);
  border-radius:var(--radius);padding:24px;transition:.18s}}
.card:hover{{border-color:var(--line2);transform:translateY(-2px)}}
.card .ico{{color:var(--acc);margin-bottom:14px}}
.card h3{{margin:0 0 9px;font-size:17px;font-weight:700}}
.card p{{margin:0;color:var(--dim);font-size:14px;line-height:1.72}}

/* ---------- 截图 ---------- */
.shots{{display:grid;grid-template-columns:repeat(2,1fr);gap:18px}}
.shots figure{{margin:0;background:var(--panel);
  border:1px solid var(--line);border-radius:var(--radius);
  overflow:hidden;transition:.18s}}
.shots figure:hover{{border-color:var(--line2);transform:translateY(-2px)}}
.shots img{{width:100%;display:block}}
.shots figcaption{{padding:11px 15px;color:var(--dim);font-size:13px;
  border-top:1px solid var(--line)}}

/* ---------- 安装 ---------- */
.cols{{display:grid;grid-template-columns:1fr 1fr;gap:20px}}
.box{{background:var(--panel);border:1px solid var(--line);
  border-radius:var(--radius);padding:24px}}
.box h3{{margin:0 0 14px;font-size:16px}}
.box ol,.box ul{{margin:0;padding-left:20px;color:var(--dim);font-size:14px}}
.box li{{margin-bottom:9px}}
pre{{background:#070a11;border:1px solid var(--line2);border-radius:10px;
  padding:14px 16px;overflow-x:auto;margin:12px 0 0}}
pre code{{background:none;padding:0;color:#b9c6e0;white-space:pre}}

/* ---------- 原则 ---------- */
.pledge{{background:var(--panel);border:1px solid var(--line2);
  border-left:3px solid var(--acc2);border-radius:var(--radius);
  padding:26px 28px}}
.pledge h3{{margin:0 0 14px;font-size:18px}}
.pledge ul{{margin:0;padding-left:20px;color:var(--dim);font-size:15px}}
.pledge li{{margin-bottom:11px}}
.pledge b{{color:var(--fg)}}

/* ---------- 页脚 ---------- */
footer{{border-top:1px solid var(--line);padding:38px 0;color:var(--dim2);
  font-size:13px}}
footer .row{{display:flex;gap:18px;flex-wrap:wrap;align-items:center}}
footer .sp{{flex:1}}

/* ---------- 窄屏 ---------- */
@media(max-width:900px){{
  .grid,.shots,.cols{{grid-template-columns:1fr}}
  .stats{{grid-template-columns:repeat(2,1fr)}}
  h1{{font-size:40px}}
  .hero{{padding:64px 0 52px}}
  section{{padding:56px 0}}
  .hd nav{{display:none}}
}}
</style>
</head>
<body>

<header>
  <div class="wrap hd">
    <img src="{icon}" alt="">
    <div>
      <b>熵光矩阵</b>
      <div class="en">Entropy Light Matrix</div>
    </div>
    <nav>
      <a href="#video">演示</a>
      <a href="#features">功能</a>
      <a href="#shots">界面</a>
      <a href="#install">安装</a>
      <a href="{REPO}">GitHub</a>
    </nav>
  </div>
</header>

<div class="hero">
  <div class="wrap">
    <img class="mark" src="{icon}" alt="熵光矩阵">
    <h1>熵光矩阵</h1>
    <div class="sub">Entropy Light Matrix</div>
    <p class="tag">《群星》模组冲突诊断 · 智能排序 · 崩溃翻译</p>
    <div class="btns">
      <a class="btn pri" href="{REPO}/releases">
        下载 Windows 版</a>
      <a class="btn" href="{REPO}">查看源码</a>
    </div>
  </div>
</div>

<section id="video">
  <div class="wrap">
    <h2>它做什么<span class="l"></span></h2>
    <p class="lead">装了 94 个模组，游戏老崩，你不知道该关谁 ——
      这个软件把它变成一次点击。</p>
    <div class="video">
      <video controls preload="metadata" playsinline
             poster="screenshots/01_总览.png"
             src="宣传片.mp4"></video>
    </div>
  </div>
</section>

<section>
  <div class="wrap">
    <div class="stats">
      <div class="stat"><b>{STATS['conflicts']}</b>
        <span>处冲突一次检出</span></div>
      <div class="stat"><b>{STATS['indexed']}</b>
        <span>个文件深度索引</span></div>
      <div class="stat"><b>{w}</b><span>条内置知识库</span></div>
      <div class="stat"><b>{tests}</b><span>项自动化测试</span></div>
    </div>
  </div>
</section>

<section id="features">
  <div class="wrap">
    <h2>核心功能<span class="l"></span></h2>
    <p class="lead">不讲技术细节，只说它替你解决什么。</p>
    <div class="grid">
{features}
    </div>
  </div>
</section>

<section id="shots">
  <div class="wrap">
    <h2>界面<span class="l"></span></h2>
    <p class="lead">五套可切换主题，也可以让 AI 按一句话生成配色。</p>
    <div class="shots">
{shots}
    </div>
  </div>
</section>

<section id="install">
  <div class="wrap">
    <h2>安装<span class="l"></span></h2>
    <div class="cols">
      <div class="box">
        <h3>普通使用（推荐）</h3>
        <ol>
          <li>到 <a href="{REPO}/releases">Releases</a>
            下载 <code>StellarisModDoctor-Setup.exe</code></li>
          <li>双击安装。<b>不需要管理员权限</b>，不弹 UAC</li>
          <li>桌面会出现「熵光矩阵」，双击运行</li>
        </ol>
        <p style="color:var(--dim);font-size:14px;margin:14px 0 0">
          安装位置：<code>%LOCALAPPDATA%\\Programs\\StellarisModDoctor</code>，
          控制面板里可正常卸载。需要
          <a href="https://developer.microsoft.com/microsoft-edge/webview2/">
          Edge WebView2</a>（Win10 1803+ 与 Win11 自带）。</p>
      </div>
      <div class="box">
        <h3>从源码运行</h3>
        <p style="color:var(--dim);font-size:14px;margin:0">
          运行时只依赖 <b>pywebview</b> 一个第三方库。</p>
<pre><code>git clone {REPO}.git
cd entropy-light-matrix
python -m venv .venv
.venv\\Scripts\\python.exe -m pip install -r requirements.txt
.venv\\Scripts\\python.exe run.py</code></pre>
      </div>
    </div>
  </div>
</section>

<section>
  <div class="wrap">
    <h2>两条不会让步的原则<span class="l"></span></h2>
    <p class="lead">这类工具最让人不放心的两件事，这里都做成了硬约束。</p>
    <div class="pledge">
      <h3>1 · 模组开关永远由你决定</h3>
      <ul>
        <li>求解器<b>只负责说清「为什么建议关」</b>，
          关不关由你逐项拍板</li>
        <li>代码里 <code>approved_disable()</code> 是唯一的真相来源 ——
          没经你确认的模组，<b>一个都不会被关</b></li>
        <li>即便开了「允许关闭模组」，也需要
          <b>本地求解器与 AI 同时判定该关</b>才会列进方案</li>
        <li>「关闭」只改启用状态，<b>不删除你的模组文件</b></li>
      </ul>
      <h3 style="margin-top:22px">2 · AI 内容必须你显式授权</h3>
      <ul>
        <li>隐私闸门集中在 <code>smd/ai/analyze.py</code> 的
          <code>ensure_ready()</code>，<b>不是散在各个调用点</b> ——
          漏一处就意味着模组内容会在你不知情时发出去</li>
        <li>默认<b>关闭</b>。不开这个开关，AI 相关功能一律拒绝执行</li>
        <li>API Key 只存在本机 <code>%LOCALAPPDATA%</code>，
          <b>不会进安装包、不会进仓库</b>
          （仓库里有 <code>tools/audit_secrets.py</code> 专门核查这件事）</li>
      </ul>
    </div>
  </div>
</section>

<footer>
  <div class="wrap row">
    <span>熵光矩阵 v{ver} · MIT License</span>
    <span class="sp"></span>
    <a href="{REPO}">GitHub</a>
    <a href="{REPO}/blob/main/README.md">完整文档</a>
    <a href="{REPO}/blob/main/CONTRIBUTING.md">参与开发</a>
    <a href="{REPO}/issues">反馈问题</a>
  </div>
  <div class="wrap" style="margin-top:16px;line-height:1.7">
    知识库数据（<code>smd/data/wiki.json</code>，{w} 条）是从第三方整理的
    《群星》资料表格抽取的，不属于代码的 MIT 授权范围。<br>
    本项目与 Paradox Interactive 无关。《群星》(Stellaris) 是
    Paradox Interactive 的商标。
  </div>
</footer>

</body>
</html>
'''


def main() -> int:
    # 视频必须**复制进 docs/**：GitHub Pages 只发布 docs/ 目录，
    # 用 ../promo/... 引用仓库根目录的文件会返回 503
    # （实测踩过：截图全 200、视频 503）。
    src_video = ROOT / "promo" / "熵光矩阵-宣传片-1080p.mp4"
    dst_video = ROOT / "docs" / "宣传片.mp4"
    if src_video.is_file():
        if (not dst_video.is_file()
                or dst_video.stat().st_size != src_video.stat().st_size):
            import shutil
            shutil.copy2(src_video, dst_video)
            print(f"  已复制宣传片到 docs/ "
                  f"（{dst_video.stat().st_size / 1048576:.1f} MB）")
        else:
            print("  宣传片已在 docs/，内容一致，跳过复制")
    else:
        print(f"  ！找不到宣传片：{src_video}")
        print("    Pages 上视频会 404。先跑 python tools/make_video.py")

    n = read_numbers()
    html = build(n)
    OUT.write_text(html, encoding="utf-8", newline="\n")
    print(f"\n  完成: {OUT}")
    print(f"  大小: {OUT.stat().st_size / 1024:.1f} KB")
    print(f"\n  页面里用到的真实数据：")
    print(f"    版本        v{n['version']}")
    print(f"    知识库      {n['wiki_entries']} 条 / "
          f"{n['wiki_sections']} 类（游戏版本 {n['wiki_version']}）")
    print(f"    测试        {n['tests']} 项（{n['test_files']} 个脚本）")
    print(f"    扫描统计    冲突 {STATS['conflicts']} / "
          f"索引 {STATS['indexed']}（来自一次真实扫描）")
    print(f"\n  docs/ 目录（Pages 发布的就是这个目录）：")
    for p in sorted((ROOT / "docs").iterdir()):
        if p.is_file():
            print(f"    {p.name:24} {p.stat().st_size / 1024:8.1f} KB")
        else:
            print(f"    {p.name}/")
    print(f"\n  本地预览：start {OUT}")
    print(f"  发布后   ：{SITE}")
    print(f"  线上验证 ：python tools/check_pages.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
