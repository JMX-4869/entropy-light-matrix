# v1.0.8

三个用户实报的问题。都不是新功能，是不出声的旧坑。

## 一、桌面图标会自己消失

**症状**：完整版的桌面图标没了，而且这不是第一次——用户的原话是「原版图标**又**没了」。

**根因**：同一个函数里，**创建**用一套名字、**删除**用另一套。

```python
# 创建时（正确，两个产品各自带后缀）
lnk = desktop_dir() / f"{DISPLAY_NAME}.lnk"    # 「熵光矩阵.lnk」/「熵光矩阵 轻量版.lnk」

# 删除时（错，两个产品共用同一个名字）
targets = [desktop_dir() / f"{APP_NAME}.lnk"]  # 永远是「熵光矩阵.lnk」
```

`APP_NAME` 是「熵光矩阵」，两个产品共用；`DISPLAY_NAME` 才带「轻量版」后缀。

所以**卸载或重装轻量版，会把完整版的桌面图标一起删掉**。上一轮修"两个产品同名互相覆盖"时只改了创建那一半，删除那半留在这里，这次炸出来。

**改法**：`remove_shortcuts()` 全部改用 `DISPLAY_NAME`，与创建端对齐。

## 二、扫描时报 `maximum recursion depth exceeded`

**症状**：点「开始检测」，任务面板弹 `maximum recursion depth exceeded`，堆栈停在 `scanner.py: in _walk`。

**根因**：`_walk` 本身早就是迭代的（`while stack`），不会递归。真正的问题是**它没有上限**——Windows 的 junction 不是符号链接，`is_dir(follow_symlinks=False)` 挡不住它，遇到成环的目录，栈会一直长下去，直到进程被拖死。

**改法**：加两道闸。

| 闸 | 阈值 | 挡住什么 |
| --- | --- | --- |
| `MAX_ENTRIES_PER_MOD` | 40 万条目 | 正常的模组最多几万个文件 |
| `MAX_DEPTH` | 64 层 | 正常模组不超过 20 层 |

撞到闸不会崩，会在该模组的 `errors` 里留一条「疑似成环，已停止扫描」，其余模组照常扫。

## 三、非 Steam 版（学习版/便携版）找不到用户数据

**症状**（用户截图）：游戏目录能自动认到 `E:\zhanchu\Stellaris\Stellaris.exe`，但

```
环境存在问题（部分功能不可用）
· 未找到群星用户数据目录
· 未找到模组目录
· 未找到日志目录
```

**根因**：`find_user_dir()` 只在标准位置找——`Documents\Paradox Interactive\Stellaris`、各种 OneDrive 重定向、`文档`。学习版常把存档和 mod 直接放在**游戏目录下**或相邻位置，这些候选一个都不中，于是整个界面报「未找到」。

**改法**：`find_user_dir(game_dir)` 现在**先查游戏目录附近**，再退回标准候选：

```
<游戏目录>/Documents/Paradox Interactive/Stellaris
<游戏目录>/../Documents/Paradox Interactive/Stellaris
<游戏目录>/user_data  ·  userdata  ·  documents
<游戏目录>/Paradox Interactive/Stellaris
```

`autodetect()` 会把已有的 `game_dir` 传进去。

---

## 没做的事

`docs/RELEASE-v1.0.7.md` 里提到的 1.0.7 修复全部保留。

Gigastructural Engineering 的 shader 崩溃**没有修**——那是 mod 本身按 4.5 写、跑在 4.4.6 上，要修得替换整份 `pdxmesh.shader`（121 KB 原版 vs 147 KB 的 mod 版逐段对比），不在这版范围。
