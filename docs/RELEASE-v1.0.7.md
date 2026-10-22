# v1.0.7

## 修掉的问题

**点「开始 AI 接管」后报错、之后完全没反应。**

这不是某一个点的错，是一整类：**代码默认 AI 一定按 schema 返回**。

模型偶尔不照做 —— 返回的 `issues` 变成字符串、列表里混进 `null`、
甚至整个响应不是 JSON 对象。原来的代码直接 `res.get(...)`、直接遍历，
拿到字符串就 `'str' object has no attribute 'name'`，拿到 `None` 就
`'NoneType' object has no attribute 'get'`。

用户看到的就是"报了个错，然后再点就没反应了"。

### 改法

加了一层 `_clean_issues()`，所有从 AI 拿回来的数据先过一遍：

| 输入 | 之前 | 现在 |
| --- | --- | --- |
| `{"issues": "oops"}` | `'str' object has no attribute 'get'` | 丢弃，正常出方案 |
| `{"issues": [null]}` | `'NoneType' object has no attribute 'get'` | 跳过这一条 |
| `{"issues": 42}` | `TypeError` | 丢弃 |
| `{"issues": [{"mods": "一个模组"}]}` | 字符串被逐字符遍历 | 当成**一个**模组名 |
| 整个响应是字符串 / null | `'str'/'NoneType' object has no attribute 'get'` | 提示"AI 返回了意外的格式" |

其中最后一条最容易被忽略：`mods` 是字符串时，`for mn in "一个模组"`
会逐字符迭代，`m.name == mn` 拿单个汉字去比模组名 —— 不但匹配不上，
还可能是用户实报的 `'str' object has no attribute 'name'` 的来源。

### 顺带

- 失败面板的消息**鼠标悬停能看到完整异常栈**。之前只显示第一行，
  用户报 bug 只能说"报了个错"，排查全靠猜。
- 全部 7 种畸形输入实测通过（`tools/probe_takeover_crash.py`）。

## 测试

```
合计：785 / 785
全部通过
```

## 下载

- `StellarisModDoctor-Setup.exe` — 安装版
- `StellarisModDoctor-Portable.exe` — 免安装
- `StellarisModDoctor-Light-Setup.exe` — 轻量版安装
- `StellarisModDoctor-Light-Portable.exe` — 轻量版免安装

轻量版和完整版是两个独立程序，可以同时装。
