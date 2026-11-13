## 熵光矩阵 1.2.2

体检了一遍，修掉三个真 bug。都是「平时不报，踩到才炸」那种。

---

## 1. 接管报告崩掉整条流程

`_takeover_report()` 里有一句 `len(plan.get('reorder') or [])`，而 `reorder` 在「只诊断」模式下是 `int 0`。

`0 or []` 恰好能兜住这一处，但同样的写法只要漏一处，`takeover` 返回的结果就整个变成 `None` —— 用户看到「任务失败」，**其实盘根本没动**。

现在：

- `_n()` 统一把 `list` / `int` / `None` 归一成个数
- 整个报告生成包了 `try/except`，`plan` 拿不到就降级成空 dict

报告只是「一段给人看的话」，它不该有能力把已经算好的方案一起带走。

```
takeover_test   22/24 → 30/30
```

---

## 2. 卸载时 NameError

`installer/install.py` 的 `remove_shortcuts()` 函数体里调了 `log(...)`，但这个函数**没有 `log` 参数**。

和 1.1.9 那个 `_install_bundled_extensions` 是同一类错 —— 函数体引用了作用域里没有的名字。

这次藏在「`.lnk` 不属于本产品」这一分支里，平时走不到，卸载时才炸。

现在：签名加 `log=None`，用之前判空；调用点把它传进去。

```
install_test   崩溃 → 29/33
```

剩下 4 条失败是本机已装了真实版本、快捷方式指向真实目录，而测试期望临时目录 —— 环境问题，不是代码问题。

---

## 3. 五个 toast 文案没进 i18n 表

| 文案 |
| --- |
| 去设置里填 |
| 打不开 |
| 打不开选择框 |
| 没有可打开的链接 |
| 这个路径还没探测到 |

都是运行时 toast，之前直接写中文字面量，切英文界面会露中文。`zh` 和 `en` 各补五条。

```
i18n_test   失败 → 38/38
```

---

## 体检总表

```
Python 语法          0 错（全仓库）
模块导入             0 失败
前端静态检查          六项全过
i18n.js 语法         OK
```

| 测试 | 结果 |
| --- | --- |
| consent | 6/6 |
| ai_analysis | 29/29 |
| i18n | 38/38 |
| patch_io | 28/28 |
| config | 27/27 |
| dlc | 28/28 |
| takeover | 30/30 |
| switch | 45/45 |
| batch_removal | 20/20 |
| monitor | 22/22 |
| ledger | 23/23 |
| wiki | 35/35 |
| translate | 40/40 |
| unified_patch | 28/28 |
| archive_ai | 27/27 |
| ai_patch | 21/21 |
| achievement | 18/18 |
| checksum | 18/18 |
| race | 全部通过 |
| apply | 只读演练正常 |

---

## 安装

| 文件 | 说明 |
| --- | --- |
| `StellarisModDoctor-Setup.exe` | 完整版 · 安装版 |
| `StellarisModDoctor-Portable.exe` | 完整版 · 免安装 |
| `StellarisModDoctor-Light-Setup.exe` | 轻量版 · 安装版 |
| `StellarisModDoctor-Light-Portable.exe` | 轻量版 · 免安装 |

装之前先退出正在运行的旧版。WebView2 有持久缓存，旧进程还在的时候新界面不会加载。
