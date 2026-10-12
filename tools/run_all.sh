#!/usr/bin/env bash
# 全量回归：跑齐 tools/ 下所有 *_test.py，汇总通过数。
#
# 为什么用脚本而不是一个个手敲：30 个测试，漏跑一个就可能放过回归。
# 输出格式统一是「结果：N/M 通过」，这里按那个格式抓数。
cd /e/yiuxi/StellarisModDoctor || exit 1
export PYTHONIOENCODING=utf-8
PY=./.venv/Scripts/python.exe

TOTAL=0
PASSED=0
FAILED=()

run() {
  local name="$1"; shift
  local out
  out="$("$PY" "$@" 2>&1)"
  local line
  line="$(echo "$out" | grep -oE '[0-9]+/[0-9]+ 通过' | tail -1)"
  if [ -z "$line" ]; then
    if echo "$out" | grep -q '全部通过'; then
      printf '  %-22s %s\n' "$name" "PASS"
      return
    fi
    printf '  %-22s %s\n' "$name" "??"
    FAILED+=("$name (无法解析)")
    return
  fi
  local p t
  p="${line%%/*}"
  t="${line##*/}"
  t="${t%% *}"
  PASSED=$((PASSED + p))
  TOTAL=$((TOTAL + t))
  if [ "$p" -lt "$t" ]; then
    printf '  %-22s %3s/%-3s  <<< 有失败\n' "$name" "$p" "$t"
    FAILED+=("$name ($p/$t)")
  else
    printf '  %-22s %3s/%-3s\n' "$name" "$p" "$t"
  fi
}

run "AI接管(全面排查)"  tools/takeover_test.py
run "影响成就"          tools/achievement_test.py
run "联机校验排查"      tools/checksum_test.py
run "统一补丁账本"      tools/ledger_test.py
run "AI分析链路"        tools/ai_analysis_test.py
run "档案AI/外观"       tools/archive_ai_test.py
run "属性转义"          tools/attr_test.py
run "时之虫档案"        tools/wiki_test.py
run "指令台/送命令"     tools/console_test.py
run "批量移除/选AI"     tools/batch_removal_test.py
run "批量保存/隐藏"     tools/batch_save_test.py
run "统一补丁模组"      tools/unified_patch_test.py
run "补丁落盘"          tools/patch_io_test.py
run "开关点击"          tools/switch_click_test.py
run "配置/自愈"         tools/config_test.py
run "任务进度/停止"     tools/task_test.py
run "AI接入页"          tools/ai_page_test.py
run "界面语言/工坊"     tools/i18n_test.py
run "一键翻译"          tools/translate_test.py
run "模组开关/DLC"      tools/switch_test.py
run "DLC交叉验证"       tools/dlc_test.py
run "竞态测试"          tools/race_test.py
run "主题系统"          tools/theme_test.py
run "安装/卸载"         tools/install_test.py
run "AI补丁校验"        tools/ai_patch_test.py
run "AI HTTP"           tools/ai_http_test.py
run "监控/崩溃"         tools/monitor_test.py
run "应用/回滚"         tools/apply_test.py --write
run "开关同意"          tools/consent_test.py --write
run "GUI桥接"           tools/bridge_test.py --scan

echo ""
echo "合计：$PASSED / $TOTAL"
if [ ${#FAILED[@]} -gt 0 ]; then
  echo "有问题的："
  for f in "${FAILED[@]}"; do echo "  - $f"; done
else
  echo "全部通过"
fi
echo ""
"$PY" tools/code_audit.py 2>&1 | grep -E "源文件|总行数|缺 docstring|调试残留|结论"
