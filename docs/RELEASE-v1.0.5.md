## v1.0.5 · 两个独立产品，各两个下载

> 网页版和轻量版现在是**彻底分开的两个产品** —— 各自的 exe、安装目录、
> 卸载项、桌面快捷方式。可以同时装，互不覆盖。

### 下载哪个

| 你要的 | 下载 |
| --- | --- |
| 功能完整、界面漂亮 | `StellarisModDoctor-Setup.exe`（安装版）<br>`StellarisModDoctor-Portable.exe`（免安装） |
| 省内存、老机器、不要 WebView2 | `StellarisModDoctor-Light-Setup.exe`（安装版）<br>`StellarisModDoctor-Light-Portable.exe`（免安装） |

| | 完整版 | 轻量版 |
| --- | --- | --- |
| 界面 | Edge WebView2 | 原生 Tkinter |
| 内存（打包实测） | 616 MB | **43 MB** |
| 体积 | 12.4 MB | 12.8 MB |
| 需要 WebView2 | 是 | **否** |
| 功能 | 全部 | 核心全部（8 个页签）|

体积差不多是因为两边的运行时都绕不开：完整版带 .NET/pythonnet，
轻量版带 tcl/tk。**省的是内存，不是硬盘**。

### 彻底分开的意思

```
完整版：run.py       → smd.cli → smd.ui       （pywebview）
轻量版：light_run.py →          smd.ui_light   （tkinter）
```

* 完整版的命令行里**没有** `--light` 开关，包里也不含 `ui_light.py`
* 轻量版的包里 exclude 掉 `smd.ui` / `smd.webui` / `webview` / `clr` / `pythonnet`
* 安装目录 `StellarisModDoctor` vs `StellarisModDoctor-Light`
* 卸载项、注册表键各自独立 —— **卸载一个不会动另一个**

实测（同时装两个、再卸掉轻量版）：

```
✓ 安装目录分开: StellarisModDoctor / StellarisModDoctor-Light
✓ 完整版 装出了 StellarisModDoctor.exe
✓ 轻量版 装出了 StellarisModDoctor-Light.exe
✓ 注册表有两个独立卸载项
✓ 卸载轻量版后，完整版目录还在 —— 互不误删
```

唯一共用的是用户配置（`%LOCALAPPDATA%\StellarisModDoctor\config.json`）——
同一个后端的两套界面，没必要分家。

### 轻量版有什么

8 个页签，核心功能一个不少：

```
模组    列表 / 逐项启停 / 详情 / 打开工作坊
冲突    检测结果 + 双击看成因与建议
排序    最优顺序 + 手动上下移动
诊断    崩溃报告 / 日志分析 / 成就影响 / 联机校验
补丁    候选 / 自动生成 / 已安装管理
知识库  时之虫档案搜索 + 提问
AI      接管排查 / 分析排序 / 测试连接
设置    路径 / API Key / 模型 / 语言
```

外加播放集切换与重命名、备份与回滚。

**不做的只有**：主题系统（CSS 专属）、星图背景、界面动效。

### 视觉

配色直接 `from .theme import BUILTIN`，读的是网页版「群星（默认）」那份
token —— 不另抄一份色号，以后换主题两套不会掉队。

### 修掉的两个坑

**1. 轻量版第一次打出来是坏的**

入口写在了 `smd/light_main.py`，用相对导入 `from .cli import main`。
PyInstaller 把入口当**顶层脚本**执行，直接抛
`attempted relative import with no known parent package`。

而因为 GUI 程序没有控制台，崩溃表现为一个错误对话框、进程还活着 ——
我据此报了"启动成功，内存 84 MB"，实际屏幕上是个错误框。

现在入口在仓库根（`light_run.py`）、用绝对导入、保留兜底错误框。

**2. 一个测试的假设过时了**

「索引集合原子性」这条断言比的是「索引数 == 所有有内容的模组数」，
但 v1.0.2 起扫描只覆盖当前播放集里**启用的**那些（就是那个
"我只想排序，怎么把我订阅的 mod 全部一起弄了"）——
于是稳定误报 74 ≠ 76。改成和「启用数」比，785/785 恢复全绿。

---

## 功能一览

| 功能 | 一句话 |
| --- | --- |
| 冲突检测 | 找出模组间重定义同一脚本对象的冲突，给严重度与影响条数 |
| 最优排序 | 分层 + 拓扑排序，翻译模组自动置顶 |
| 兼容补丁 | 生成覆盖补丁模组，可回滚 |
| 崩溃报告 | 翻译 `exception.txt` / `error.log`，配成因注释 |
| 日志分析 | 高频报错归类，AI 可解读 |
| 影响成就的模组 | 按游戏自带 `checksum_manifest.txt` 查，可一键或自选关闭 |
| 联机校验排查 | 找会导致校验码不一致的模组 |
| AI 接管 | 汇总 12 类数据源做全面排查 |
| 时之虫档案 | 8689 条游戏知识库，可检索/发命令 |
| 播放集管理 | 自动发现官方 + Irony，准确对应，可切换/重命名 |

系统要求：Windows 10 1803+ / Windows 11。

## 说明

* 求解器**不会自动关闭你的任何模组** —— 关不关由你逐项决定
* AI 功能默认关闭，需要你显式打开隐私开关
* API Key 只存在本机 `%LOCALAPPDATA%`，不会上传、不进安装包
* 两个产品共用同一份配置
