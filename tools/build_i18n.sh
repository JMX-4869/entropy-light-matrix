#!/usr/bin/env bash
# 重建整套 i18n：扫描 → 注入标注 → 替换 JS 字面量 → 写入翻译表。
#
# 为什么串成脚本：这四步有**严格顺序**，而且都依赖上一步的输出。
# 手工按顺序跑很容易漏一步或跑错顺序，症状是"英文界面还是中文"，
# 排查成本极高（这个项目里已经为此绕了很久）。
#
# 关键约定：每一步都从 **git 里的干净版本**开始，不叠加在上次结果上。
# 叠加会出两个问题：位置全偏（注入靠字符位置定位）、
# 备份被坏文件覆盖（出事后无法回退）。
set -e
cd /e/yiuxi/StellarisModDoctor
export PYTHONIOENCODING=utf-8
PY=./.venv/Scripts/python.exe

echo "════ 1/5 恢复干净源文件 ════"
git checkout -- smd/web/index.html smd/web/i18n.js
node --check smd/web/i18n.js && echo "  i18n.js 干净版语法 OK"

echo ""
echo "════ 2/5 扫描中文文本 → 映射表 ════"
$PY tools/build_i18n_map.py 2>&1 | grep -E "扫到|待翻译|位置自检|生成 i18n 键"

echo ""
echo "════ 3/5 注入 data-i18n 标注 ════"
$PY tools/inject_i18n.py 2>&1 | grep -E "循环统计|待注入|已写入"

echo ""
echo "════ 4/5 替换 JS 中文字面量为 t() ════"
$PY tools/plan_js_replace.py > /dev/null 2>&1
$PY tools/apply_js_replace.py 2>&1 | grep -E "替换点|实际替换|已写入|标签平衡"

echo ""
echo "════ 5/5 写入翻译表 ════"
rm -f smd/web/i18n.js.bak
$PY tools/fill_i18n_gaps.py 2>&1 | grep -E "补齐|并入|仍未翻译"
$PY tools/write_i18n_table.py 2>&1 | grep -E "插入|并入了|已写入|放弃"

echo ""
echo "════ 校验 ════"
node --check smd/web/i18n.js && echo "  i18n.js 语法 OK"
node tools/check_i18n_tables.mjs 2>/dev/null || node /tmp/checki18n.mjs 2>&1 | grep -E "zh 键数|en 缺|en 多"
$PY -c "
import io,sys,re,os,subprocess
sys.stdout=io.TextIOWrapper(sys.stdout.buffer,encoding='utf-8',errors='replace')
h=open('smd/web/index.html',encoding='utf-8').read()
bad=[t for t in ('div','span','button','h3','p','section','b','label','option')
     if len(re.findall('<'+t+r'[\s>]',h))!=len(re.findall('</'+t+'>',h))]
print('  标签平衡:', bad or '正常')
print('  data-i18n 属性:', h.count('data-i18n='))
parts=re.findall(r'<script\b[^>]*>(.*?)</script>',h,re.S)
p=os.path.join(os.environ['TEMP'],'smd_inline.js')
open(p,'w',encoding='utf-8',newline='\n').write('\n;\n'.join(parts))
r=subprocess.run(['node','--check',p],capture_output=True,text=True,encoding='utf-8',errors='replace')
print('  内联脚本语法:', 'OK' if r.returncode==0 else 'FAIL')
if r.returncode: print((r.stderr or '')[:300])
"
echo ""
echo "  下一步：/.venv/Scripts/python.exe tools/verify_i18n_en.py"
