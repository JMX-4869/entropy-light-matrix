/* ============================================================================
 * 界面语言（i18n）
 * ============================================================================
 *
 * 做法：给需要翻译的元素加 `data-i18n="key"`，由 applyI18n() 统一替换文本；
 * 占位符用 `data-i18n-ph`，提示用 `data-i18n-title`。
 *
 * 为什么是「按 key 查表」而不是「按中文原文查表」：
 *   按原文查表在改动文案时容易漏改——改了中文却忘了改表，
 *   英文界面就会残留旧文案。按 key 则一眼能看出哪个没翻译。
 *
 * 动态拼接的文本（例如「显示 94 个 · 其中 76 个将加载」）走 t() 函数，
 * 支持 {n} 这种占位符替换。
 */
(function () {
  'use strict';

  var STRINGS = {
    zh: {
      /* 侧栏 */
      'app.name': '熵光矩阵',
      'app.tagline': '群星模组冲突诊断与智能排序',
      'nav.dash': '总览', 'nav.mods': '模组列表', 'nav.dlc': 'DLC 管理',
      'nav.conflict': '冲突检测', 'nav.solution': '最优方案',
      'nav.order': '加载顺序', 'nav.ai': 'AI 接入', 'nav.patch': '补丁工坊',
      'nav.console': '时之虫档案',
      'nav.monitor': '运行时监控', 'nav.crash': '崩溃报告',
      'nav.logs': '日志分析', 'nav.appearance': '界面外观', 'nav.settings': '设置',
      'status.ready': '就绪',
      'conflict.subtitle': '扫描模组之间的文件覆盖、脚本重定义、依赖与版本问题',
      'solution.subtitle': '分层排序 + 拓扑求解 + 兼容补丁生成',
      'monitor.subtitle': '游戏运行期间实时盯住模组报错、内存与崩溃',

      /* 通用 */
      'common.refresh': '刷新', 'common.cancel': '取消', 'common.confirm': '确认',
      'common.apply': '应用', 'common.close': '关闭', 'common.save': '保存',
      'common.delete': '删除', 'common.reset': '重置', 'common.open': '打开',
      'common.copy': '复制', 'common.details': '详情', 'common.all': '全部',
      'common.search': '搜索', 'common.enabled': '已启用', 'common.disabled': '未启用',
      'common.problem': '有问题', 'common.loading': '加载中…',
      'common.selectAll': '全选', 'common.selectNone': '全不选',
      'common.review': '查看内容', 'common.confidence': '置信度',
      'common.lines': '行', 'common.warning': '注意', 'common.reason': '理由',
      'common.failed': '失败', 'common.done': '完成', 'common.start': '开始',
      'common.order': '序', 'common.name': '名称', 'common.source': '来源',
      'common.size': '大小', 'common.auto': '自动',
      'order.col.tier': '分层/标记', 'order.col.adjust': '调整',

      /* 模组列表 */
      'mods.title': '模组列表',
      'mods.subtitle': '勾选滑块选择要加载的模组，改动先暂存',
      'mods.search': '搜索模组名 / 标签…',
      'mods.refresh': '刷新清单',
      'mods.filter.all': '全部', 'mods.filter.enabled': '已启用',
      'mods.filter.disabled': '未启用', 'mods.filter.problem': '有问题',
      'mods.filter.translation': '汉化', 'mods.filter.heavy': '大型模组',
      'mods.filter.unusable': '磁盘无内容（不可启用）',
      'mods.bulk': '批量操作', 'mods.bulk.sub': '先暂存，确认后再写入',
      'mods.enableAll': '全部启用', 'mods.disableAll': '全部关闭',
      'mods.invert': '反选',
      'mods.enableFiltered': '只启用筛选结果',
      'mods.disableFiltered': '只关闭筛选结果',
      'mods.showUnusable': '只看不可用的',
      'mods.discardAll': '放弃全部改动',
      'mods.hint': '这里的开关只改「加载哪些模组」，不改加载顺序。顺序请到「加载顺序」页调。写入前会自动备份 dlc_load.json。',
      'mods.hint.pick': '勾选左侧滑块选择要加载的模组。',
      'mods.hint.staged': '改动会先暂存，点「写入群星」才生效——不会手滑就把模组关掉。',
      'mods.col.load': '加载', 'mods.col.name': '模组名称',
      'mods.col.version': '支持版本', 'mods.col.size': '体量',
      'mods.col.files': '文件', 'mods.col.content': '内容',
      'mods.col.status': '状态',
      'mods.status.missingFiles': '缺文件', 'mods.status.problem': '有问题',
      'mods.status.on': '启用', 'mods.status.off': '未启用',
      'mods.status.unusable': '不可用',
      'mods.changed': '已改',
      'mods.pending': '有 {n} 处改动还没写入',
      'mods.willEnable': '将启用 {n} 个', 'mods.willDisable': '将关闭 {n} 个',
      'mods.writeToGame': '写入群星', 'mods.discard': '放弃改动',
      'mods.showGhost': '显示已退订的空条目',
      'mods.workshop': '打开创意工坊页面',
      'mods.noMatch': '没有匹配的模组',
      'mods.hiddenGhosts': '已隐藏 {n} 个已退订的空条目',
      'mods.stats': '显示 {shown} 个 · 其中 {on} 个将加载 · 共 {all} 个模组',
      'mods.stats.dead': '{n} 个磁盘无内容（不可启用）',

      /* DLC */
      'dlc.title': 'DLC 管理',
      'dlc.subtitle': '控制游戏加载哪些 DLC。改动先暂存，确认后再写入',
      'dlc.installed': '已安装 DLC', 'dlc.willLoad': '将加载',
      'dlc.willNotLoad': '将不加载', 'dlc.expansions': '大型资料片',
      'dlc.bulk': '批量操作', 'dlc.enableAll': '全部启用',
      'dlc.disableAll': '全部关闭', 'dlc.discardAll': '放弃全部改动',
      'dlc.catEnable': '本类全开', 'dlc.catDisable': '本类全关',
      'dlc.openFolder': '打开 DLC 目录',
      'dlc.affectsCompat': '影响存档兼容',
      'dlc.changed': '已改动',

      /* 补丁工坊 */
      'patch.title': '补丁工坊',
      'patch.subtitle': '让 AI 生成补丁、用本地规则自动生成，或自己动手写',
      'patch.mode': '模式',
      'patch.mode.ai': '✦ AI 生成补丁',
      'patch.mode.auto': '⚙ 本地自动补丁',
      'patch.mode.manual': '✎ 我自己写',
      'patch.mode.batch': '⚡ 批量生成补丁',
      'patch.mode.trans': '🌐 一键翻译',
      'patch.mode.list': '▤ 已安装的补丁',
      'patch.auto.scope': '可生成的补丁',
      'patch.auto.generate': '生成草稿',
      'patch.auto.apply': '写入选中的补丁',
      'patch.auto.drafts': '生成的草稿',
      'patch.tr.target': '目标语言',
      'patch.tr.limit': '试翻条数（0 = 全部）',
      'patch.tr.start': '开始翻译',
      'patch.tr.entries': '待翻译条目', 'patch.tr.chars': '字符总数',
      'patch.tr.modsWithText': '有文本的模组',
      'patch.tr.skipped': '已有译文跳过',
      'patch.tr.dontRetranslate': '不会重复翻译',

      /* 设置 */
      'settings.title': '设置',
      'settings.subtitle': '路径、监控、排序策略与收尾行为',
      'settings.language': '界面语言',
      'settings.language.sub': '只影响本软件的界面，不影响游戏内语言',
      'settings.ghost': '在模组列表显示已退订的空条目',
      'settings.ghost.sub': '这些条目在磁盘上没有内容，勾选也不会加载',

      /* 提示 */
      'toast.saved': '已保存', 'toast.copied': '已复制',
      'toast.failed': '操作失败',
      'lang.switched': '界面语言已切换为简体中文'
    },

    en: {
      'app.name': 'Entropy Light Matrix',
      'app.tagline': 'Stellaris mod conflict diagnosis & load-order solver',
      'nav.dash': 'Overview', 'nav.mods': 'Mods', 'nav.dlc': 'DLC',
      'nav.conflict': 'Conflicts', 'nav.solution': 'Best Plan',
      'nav.order': 'Load Order', 'nav.ai': 'AI', 'nav.patch': 'Patch Studio',
      'nav.console': 'Worm Archive',
      'nav.monitor': 'Live Monitor', 'nav.crash': 'Crash Reports',
      'nav.logs': 'Log Analysis', 'nav.appearance': 'Appearance',
      'nav.settings': 'Settings',
      'status.ready': 'Ready',
      'conflict.subtitle': 'Scans file overwrites, script redefinitions, dependencies and version issues between mods',
      'solution.subtitle': 'Tiered ordering + topological solve + compatibility patch generation',
      'monitor.subtitle': 'Watches mod errors, memory and crashes while the game runs',

      'common.refresh': 'Refresh', 'common.cancel': 'Cancel',
      'common.confirm': 'Confirm', 'common.apply': 'Apply',
      'common.close': 'Close', 'common.save': 'Save',
      'common.delete': 'Delete', 'common.reset': 'Reset', 'common.open': 'Open',
      'common.copy': 'Copy', 'common.details': 'Details', 'common.all': 'All',
      'common.search': 'Search', 'common.enabled': 'Enabled',
      'common.disabled': 'Disabled', 'common.problem': 'Problem',
      'common.loading': 'Loading…',
      'common.selectAll': 'Select all', 'common.selectNone': 'Select none',
      'common.review': 'View content', 'common.confidence': 'Confidence',
      'common.lines': 'lines', 'common.warning': 'Note', 'common.reason': 'Reason',
      'common.failed': 'Failed', 'common.done': 'Done', 'common.start': 'Start',
      'common.order': '#', 'common.name': 'Name', 'common.source': 'Source',
      'common.size': 'Size', 'common.auto': 'Auto',
      'order.col.tier': 'Tier / tag', 'order.col.adjust': 'Adjust',

      'mods.title': 'Mods',
      'mods.subtitle': 'Toggle which mods load. Changes are staged first.',
      'mods.search': 'Search name / tag…',
      'mods.refresh': 'Reload list',
      'mods.filter.all': 'All', 'mods.filter.enabled': 'Enabled',
      'mods.filter.disabled': 'Disabled', 'mods.filter.problem': 'Problems',
      'mods.filter.translation': 'Translations', 'mods.filter.heavy': 'Large mods',
      'mods.filter.unusable': 'No content on disk',
      'mods.bulk': 'Bulk actions',
      'mods.bulk.sub': 'Staged first — nothing is written until you confirm',
      'mods.enableAll': 'Enable all', 'mods.disableAll': 'Disable all',
      'mods.invert': 'Invert',
      'mods.enableFiltered': 'Enable filtered only',
      'mods.disableFiltered': 'Disable filtered only',
      'mods.showUnusable': 'Show unusable only',
      'mods.discardAll': 'Discard all changes',
      'mods.hint': 'These switches only control WHICH mods load, not their order. Use the Load Order page for that. dlc_load.json is backed up before writing.',
      'mods.hint.pick': 'Use the switches on the left to choose which mods load.',
      'mods.hint.staged': 'Changes are staged first — nothing is written until you click "Write to game".',
      'mods.col.load': 'Load', 'mods.col.name': 'Mod',
      'mods.col.version': 'Version', 'mods.col.size': 'Size',
      'mods.col.files': 'Files', 'mods.col.content': 'Content',
      'mods.col.status': 'Status',
      'mods.status.missingFiles': 'Missing', 'mods.status.problem': 'Problem',
      'mods.status.on': 'On', 'mods.status.off': 'Off',
      'mods.status.unusable': 'Unusable',
      'mods.changed': 'edited',
      'mods.pending': '{n} change(s) not written yet',
      'mods.willEnable': 'Enable {n}', 'mods.willDisable': 'Disable {n}',
      'mods.writeToGame': 'Write to game', 'mods.discard': 'Discard',
      'mods.showGhost': 'Show unsubscribed empty entries',
      'mods.workshop': 'Open Workshop page',
      'mods.noMatch': 'No matching mods',
      'mods.hiddenGhosts': '{n} unsubscribed empty entries hidden',
      'mods.stats': 'Showing {shown} · {on} will load · {all} mods total',
      'mods.stats.dead': '{n} with no content on disk',

      'dlc.title': 'DLC',
      'dlc.subtitle': 'Choose which DLC the game loads. Staged until you confirm.',
      'dlc.installed': 'Installed DLC', 'dlc.willLoad': 'Will load',
      'dlc.willNotLoad': 'Will not load', 'dlc.expansions': 'Expansions',
      'dlc.bulk': 'Bulk actions', 'dlc.enableAll': 'Enable all',
      'dlc.disableAll': 'Disable all', 'dlc.discardAll': 'Discard all changes',
      'dlc.catEnable': 'Enable category', 'dlc.catDisable': 'Disable category',
      'dlc.openFolder': 'Open DLC folder',
      'dlc.affectsCompat': 'Affects save compatibility',
      'dlc.changed': 'edited',

      'patch.title': 'Patch Studio',
      'patch.subtitle': 'Generate patches with AI, local rules, or by hand',
      'patch.mode': 'Mode',
      'patch.mode.ai': '✦ AI-generated',
      'patch.mode.auto': '⚙ Local auto-patch',
      'patch.mode.manual': '✎ Write my own',
      'patch.mode.batch': '⚡ Batch generate',
      'patch.mode.trans': '🌐 Batch translate',
      'patch.mode.list': '▤ Installed patches',
      'patch.auto.scope': 'Available patches',
      'patch.auto.generate': 'Generate drafts',
      'patch.auto.apply': 'Write selected patches',
      'patch.auto.drafts': 'Generated drafts',
      'patch.tr.target': 'Target language',
      'patch.tr.limit': 'Test batch size (0 = all)',
      'patch.tr.start': 'Start translating',
      'patch.tr.entries': 'Entries to translate', 'patch.tr.chars': 'Characters',
      'patch.tr.modsWithText': 'Mods with text',
      'patch.tr.skipped': 'Already translated',
      'patch.tr.dontRetranslate': 'skipped, not re-translated',

      'settings.title': 'Settings',
      'settings.subtitle': 'Paths, monitoring, load-order policy and shutdown behaviour',
      'settings.language': 'Interface language',
      'settings.language.sub': 'Affects this app only, not the in-game language',
      'settings.ghost': 'Show unsubscribed empty entries in the mod list',
      'settings.ghost.sub': 'These have no content on disk; enabling them does nothing',

      'toast.saved': 'Saved', 'toast.copied': 'Copied',
      'toast.failed': 'Action failed',
      'lang.switched': 'Interface language set to English'
    }
  };

  var current = 'zh';

  function t(key, vars) {
    var table = STRINGS[current] || STRINGS.zh;
    var s = table[key];
    if (s === undefined) s = (STRINGS.zh[key] !== undefined ? STRINGS.zh[key] : key);
    if (vars) {
      Object.keys(vars).forEach(function (k) {
        s = s.split('{' + k + '}').join(String(vars[k]));
      });
    }
    return s;
  }

  /** 把 data-i18n 标注过的元素全部刷新一遍。 */
  function applyI18n(root) {
    var scope = root || document;
    scope.querySelectorAll('[data-i18n]').forEach(function (el) {
      el.textContent = t(el.getAttribute('data-i18n'));
    });
    scope.querySelectorAll('[data-i18n-ph]').forEach(function (el) {
      el.setAttribute('placeholder', t(el.getAttribute('data-i18n-ph')));
    });
    scope.querySelectorAll('[data-i18n-title]').forEach(function (el) {
      el.setAttribute('title', t(el.getAttribute('data-i18n-title')));
    });
    // 未标注 data-i18n 但需要在两种语言下都正确的少量元素
    document.documentElement.setAttribute('lang',
      current === 'en' ? 'en' : 'zh-CN');
    var tag = document.querySelector('.brand .t');
    if (tag) tag.textContent = t('app.name');
  }

  function setLang(lang) {
    current = (lang === 'en') ? 'en' : 'zh';
    try { localStorage.setItem('smd_lang', current); } catch (e) { /* 忽略 */ }
    applyI18n();
  }

  /**
   * 监视 DOM 新增节点并自动翻译。
   *
   * 为什么需要：界面里很多区块是 innerHTML 动态重建的（模组表、批量提示、
   * DLC 卡片…）。靠"在每个渲染函数末尾手动补一次 applyI18n"很容易漏，
   * 而且漏了只在切换语言后才看得出来。用观察器兜住，
   * 无论谁往 DOM 里插什么，带 data-i18n 的部分都会被翻好。
   */
  function autoApply() {
    if (typeof MutationObserver !== 'function') return;
    var obs = new MutationObserver(function (records) {
      for (var i = 0; i < records.length; i++) {
        var added = records[i].addedNodes;
        for (var j = 0; j < added.length; j++) {
          var n = added[j];
          if (n.nodeType !== 1) continue;
          if (n.hasAttribute && n.hasAttribute('data-i18n')) {
            n.textContent = t(n.getAttribute('data-i18n'));
          }
          if (n.querySelectorAll) {
            applyI18n(n);
            // 动态插入的块里可能也有图标占位，一并画上
            if (window.paintIcons) window.paintIcons();
          }
        }
      }
    });
    obs.observe(document.body, { childList: true, subtree: true });
  }

  function getLang() { return current; }

  window.I18N = { t: t, applyI18n: applyI18n, setLang: setLang, getLang: getLang,
                  autoApply: autoApply, strings: STRINGS };
  window.t = t;

  // DOM 就绪后挂上观察器（脚本在 <head> 里，此时 body 还不存在）
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', autoApply);
  } else {
    autoApply();
  }
})();
