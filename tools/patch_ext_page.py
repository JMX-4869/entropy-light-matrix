"""给前端加「扩展」页（侧栏入口 + 页面 + 渲染逻辑）。

一次改三处：导航项、页面容器、JS 函数。用脚本改而不是手改，
是因为 index.html 有五千多行，手改容易插错位置。
"""
import io
import sys
from pathlib import Path

OUT = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
HTML = Path("smd/web/index.html")
src = HTML.read_text(encoding="utf-8")

# ---------- 1) 侧栏导航项 ----------
NAV_ANCHOR = '      <div class="nav" data-p="settings">'
NAV_NEW = (
    '      <div class="nav" data-p="ext"><span class="ico" data-icon="ext">'
    '</span><span data-i18n="nav.ext">扩展</span>'
    '<span class="cnt" id="cExt">—</span></div>\n'
)
assert NAV_ANCHOR in src, "侧栏锚点没找到"
assert 'data-p="ext"' not in src, "扩展导航项已存在"
src = src.replace(NAV_ANCHOR, NAV_NEW + NAV_ANCHOR, 1)

# ---------- 2) 页面容器 ----------
PAGE_ANCHOR = '    <section class="page" id="p-settings">'
PAGE_NEW = '''    <section class="page" id="p-ext">
      <div class="card">
        <div class="card-h">
          <h2>扩展</h2>
          <span class="hint" id="extRoot"></span>
        </div>
        <p class="hint" style="margin:6px 0 14px">
          扩展是**独立程序**，不装也能正常用主程序。点「启动」会开一个新窗口；
          需要管理员权限的扩展会弹一次 UAC。
        </p>
        <div class="row" style="gap:8px;margin-bottom:14px">
          <button class="btn" id="extRefresh">重新扫描</button>
          <button class="btn ghost" id="extOpenDir">打开扩展目录</button>
        </div>
        <div id="extList"></div>
      </div>
    </section>

'''
assert PAGE_ANCHOR in src, "页面锚点没找到"
assert 'id="p-ext"' not in src, "扩展页已存在"
src = src.replace(PAGE_ANCHOR, PAGE_NEW + PAGE_ANCHOR, 1)

# ---------- 3) JS ----------
JS_ANCHOR = "function goPage(name){"
JS_NEW = '''// ---- 扩展 ----------------------------------------------------------
// 每次进页面都重扫：用户可能刚把扩展目录拷进来，重启才显示会显得"装了没反应"。
async function refreshExt(){
  const box = $('extList');
  if(!box) return;
  box.innerHTML = '<div class="hint">扫描中…</div>';
  let r;
  try{ r = JSON.parse(await env.listExtensions()); }
  catch(e){ box.innerHTML = '<div class="hint err">读取失败：'+esc(e)+'</div>'; return; }
  const items = r.items || [];
  const c = $('cExt'); if(c) c.textContent = items.length? String(items.length) : '0';
  const root = $('extRoot'); if(root) root.textContent = r.root || '';
  if(!items.length){
    box.innerHTML = '<div class="hint" style="padding:18px 0">'
      + '还没装扩展。把扩展目录放进上面那个路径就行，不用安装程序。</div>';
    return;
  }
  box.innerHTML = items.map(d=>{
    const state = !d.ok ? '<span class="tag err">不可用</span>'
      : (d.running ? '<span class="tag on">运行中</span>' : '<span class="tag">就绪</span>');
    const reason = d.ok ? '' : '<div class="hint err" style="margin-top:4px">'+esc(d.reason||'')+'</div>';
    const act = d.running
      ? '<button class="btn ghost" data-ext-stop="'+esc(d.id)+'">停止</button>'
      : '<button class="btn" data-ext-run="'+esc(d.id)+'"'+(d.ok?'':' disabled')+'>启动</button>';
    return '<div class="extrow" style="display:flex;align-items:center;gap:12px;'
      + 'padding:12px 0;border-top:1px solid var(--line)">'
      + '<div style="flex:1;min-width:0">'
      + '<div style="display:flex;align-items:center;gap:8px">'
      + '<b>'+esc(d.name_zh||d.name)+'</b>'
      + '<span class="hint">v'+esc(d.version)+'</span>'+state+'</div>'
      + '<div class="hint" style="margin-top:3px">'+esc(d.description_zh||d.name)+'</div>'
      + reason + '</div>' + act + '</div>';
  }).join('');
  box.querySelectorAll('[data-ext-run]').forEach(b=>{
    b.onclick = async()=>{
      b.disabled = true; b.textContent = '启动中…';
      const x = JSON.parse(await env.launchExtension(b.dataset.extRun));
      toast(x.ok? '已启动' : '启动失败', x.message || '', x.ok? '' : 'err');
      setTimeout(refreshExt, 1200);
    };
  });
  box.querySelectorAll('[data-ext-stop]').forEach(b=>{
    b.onclick = async()=>{
      b.disabled = true; b.textContent = '停止中…';
      const x = JSON.parse(await env.stopExtension(b.dataset.extStop));
      toast(x.ok? '已停止' : '停止失败', x.message || '', x.ok? '' : 'err');
      setTimeout(refreshExt, 800);
    };
  });
}
if($('extRefresh')) $('extRefresh').onclick = refreshExt;
if($('extOpenDir')) $('extOpenDir').onclick = async()=>{
  const x = JSON.parse(await env.openExtensionsDir());
  if(!x.ok) toast('打不开', x.message || x.path, 'err');
};

function goPage(name){'''
assert JS_ANCHOR in src, "goPage 锚点没找到"
assert "refreshExt" not in src, "JS 已存在"
src = src.replace(JS_ANCHOR, JS_NEW, 1)

HTML.write_text(src, encoding="utf-8", newline="\n")
print("  已插入：侧栏项 + 扩展页 + refreshExt()", file=OUT)

# 验证
s = HTML.read_text(encoding="utf-8")
for k in ('data-p="ext"', 'id="p-ext"', 'function refreshExt', 'extOpenDir'):
    print(f"  {k:24} {'✓' if k in s else '✗'}", file=OUT)
