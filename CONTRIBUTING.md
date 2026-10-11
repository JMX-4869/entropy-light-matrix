# 贡献指南

感谢愿意帮忙。这个项目是《群星》模组管理工具，主要用 Python + 原生
HTML/CSS/JS 写的（界面跑在 pywebview 里的 Edge WebView2 上）。

## 环境

需要 **Windows**（pywebview 走 Edge WebView2，另外几处用了 winreg /
资源管理器集成）。Python 3.11+。

```powershell
git clone <仓库地址>
cd StellarisModDoctor
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt -r requirements-dev.txt
.venv\Scripts\python.exe run.py
```

## 跑测试

测试是**独立的脚本**，不是 pytest 用例——每个文件自己 `main()` 并打印
`结果：N/M 通过`。这样单跑某一个很快，也方便在 CI 里聚合。

```powershell
# 单个
.venv\Scripts\python.exe tools\wiki_test.py

# 全部（约 20 分钟，会真的扫描一遍你的模组目录）
Get-ChildItem tools\*_test.py | ForEach-Object { .venv\Scripts\python.exe $_.FullName }
```

提交前请确保：

1. **全部测试通过**（当前基线 724/724）
2. **代码审计干净**：
   ```powershell
   .venv\Scripts\python.exe tools\code_audit.py
   ```
   会检查模块 / 类 / 函数是否有 docstring、有没有遗留 `print` /
   `TODO` / `breakpoint()` 之类的调试代码。

### 测试会动你的真实文件——请注意

有几类测试**刻意不整体沙箱**，因为它们要验证真实读写：

| 测试 | 会碰什么 |
| --- | --- |
| `bridge_test.py` | 真实读写配置文件（有备份还原 + `try/finally`） |
| `install_test.py` | 真实安装 / 卸载，**会删掉桌面快捷方式与注册表项** |
| `apply_test.py` | 真实写入 playset 与 `dlc_load.json` |
| `consent_test.py` | 同上 |
| 其余 | 走 `tools/sandbox.py`，只重定向用户数据目录 |

跑完 `install_test.py` 之后记得重新装一次，否则桌面快捷方式会没。
写新测试时**优先用沙箱**：

```python
from sandbox import enter_sandbox
enter_sandbox()                    # 重定向 user_dir
enter_sandbox(game_dir=True)       # 还要写补丁模组时必须加，否则会写进真实 mod 目录
```

> 这条是踩出来的：早先 `batch_save_test` / `unified_patch_test` 少写了
> `game_dir=True`，往真实 mod 目录里写了 169 个补丁模组。

## 代码风格

- **注释写"为什么"，不写"是什么"**。`# 把 i 加一` 这种没有价值；
  有用的注释是"为什么这里必须这样、不这样会出什么问题"。
- 每个模块、类、公开函数都要有 docstring（审计脚本会查）。
- 面向用户的文案用中文，界面文案要同时进 `smd/web/i18n.js` 的中英两表。
- 不要引入新的第三方依赖，除非确实必要——现在运行时只依赖 `pywebview`。

## 界面改动

界面是**手写的 HTML/CSS/JS**，没有构建步骤，改完直接刷新生效：

```
smd/web/index.html   界面主体与全部前端逻辑
smd/web/theme.css    样式（令牌驱动，颜色都走 CSS 变量）
smd/web/i18n.js      中英文案表
smd/data/wiki.json   「时之虫档案」知识库数据（随程序分发）
```

### 三个很容易踩的坑

**1. 把字符串塞进 `onclick` 必须用 `jsAttr()`**

```js
// 错：JSON 里的双引号会提前闭合属性，按钮看着在、点了没反应
onclick="f(${JSON.stringify(x)})"
// 对：
onclick="f(${jsAttr(x)})"
```

Windows 路径里的反斜杠会被属性解析吃掉（`C:\Users\x` 变成 `C:Usersx`），
而且**不报错**。`tools/attr_test.py` 专门盯这个。

**2. 开关的 `<input>` 本身就是可见控件**

不要在它旁边再放一个 `<i>` 当外观——兄弟元素不会转发点击，
表现就是"所有开关都点不动"。`tools/switch_click_test.py` 用真实 DOM
点击来验证这一点（早先的测试直接调 API，所以漏掉了这个 bug）。

**3. 截图 / 测试窗口的 `create_window` 参数要和生产一致**

统一从 `smd/ui.py` 的 `WINDOW_KWARGS` 取。早先各写各的，
导致截图里的窗口和生产长得不一样，看截图根本发现不了问题。

## 改「时之虫档案」的数据

数据来自一份 Excel 表格，抽取脚本在 `tools/build_wiki.py`：

```powershell
$env:SMD_WIKI_XLSX = "D:\path\to\宝典.xlsx"
python tools\build_wiki.py          # 写出 smd/data/wiki.json
```

各页表头行号、列布局差异很大（2~6 行不等、大量合并单元格），
所以脚本里是**按页手工描述的抽取规则**，没有套通用逻辑——
通用逻辑在这种人工维护的表上一定会出错。加新页时照着已有条目写一条规则。

## 提交

- 一个提交做一件事，提交信息说清**为什么**改。
- 涉及破坏性操作（删文件、写游戏配置）的改动，请在描述里说明
  失效时会怎样、怎么回滚。

## 许可

贡献的代码按 [MIT](LICENSE) 授权。

`smd/data/wiki.json` 是从第三方整理的《群星》资料表格抽取的数据，
**不属于代码的 MIT 授权范围**，版权归原整理者。
