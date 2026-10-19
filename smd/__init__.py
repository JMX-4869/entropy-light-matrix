"""熵光矩阵 (Entropy Light Matrix) —— 群星模组冲突诊断与智能排序启动器。

名字取自群星的宇宙观：熵是热寂，光是文明在熵增里维持秩序的那点能量，
矩阵则呼应「排布 / 加载顺序」。

模块划分
--------
``version``      版本常量
``config``       路径探测、配置持久化
``paradox``      Paradox 脚本 (Clausewitz) 词法/语法解析
``modinfo``      模组发现：``.mod`` 描述符 / launcher-v2.sqlite / 工坊目录
``scanner``      模组内容索引（文件级 + 语义级键）
``conflict``     冲突检测引擎
``solver``       最优排序求解（拓扑排序 + 权重打分 + 补丁生成）
``apply``        方案落地：写回 playset / dlc_load.json / 生成覆盖补丁
``monitor``      后台运行时监控（进程、error.log 增量、内存、崩溃目录）
``logkb``        日志知识库：错误翻译 + 中文注释 + 修复建议
``report``       崩溃报告生成
``system``       启动游戏、自动关机、下次开机提醒
``theme``        界面主题引擎
``i18n``         界面文案（web/i18n.js）
``ui``           图形界面
``cli``          命令行入口
"""

from __future__ import annotations

__all__ = ["__version__", "APP_NAME", "APP_NAME_EN", "APP_ID",
           "PATCH_PREFIX", "UNIFIED_PATCH_NAME"]

__version__ = "1.0.5"

# 软件显示名。改这里就够，界面 / 报告 / 补丁说明 / 安装程序都引用这几个常量。
APP_NAME = "熵光矩阵"
APP_NAME_EN = "Entropy Light Matrix"

# 补丁模组的目录名前缀。
#
# 放在这里（而不是 ``patch.service``）是为了避免循环导入：
# ``modinfo`` 需要靠它认出"这是我们自己生成的补丁模组"并把它排除在
# 冲突检测之外，而 ``patch.service`` 又依赖 ``modinfo`` 的类型。
PATCH_PREFIX = "zzz_SMD_"

# 所有补丁都合并进这一个模组（而不是一次生成一个）。
# 用户只需要管一个开关；改了什么靠模组内的 SMD_MANIFEST.json 记账。
UNIFIED_PATCH_NAME = f"{PATCH_PREFIX}熵光矩阵补丁"

# 内部标识：**故意保持历史值不变**。
#
# 它决定三件事：%LOCALAPPDATA% 下的数据目录、注册表卸载项、
# exe 与安装目录名。改名会把用户已有的配置、主题、备份全部"丢失"
# （其实是留在旧目录里没人读），也会让旧的卸载入口失效。
# 显示名与内部标识分开，是这类工具应有的做法。
APP_ID = "StellarisModDoctor"
LEGACY_NAMES = ("群星模组医生", "Stellaris Mod Doctor")
