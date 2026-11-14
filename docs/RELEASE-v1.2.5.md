## 熵光矩阵 1.2.5

上一版加了「导入播放集 JSON」，但**只导入不排列** —— 导入的名单虽然出现在选择器里，选中之后模组顺序还是按活动播放集走。

这一版把那条路补通了。

---

## 断在哪

`load_library()` 只放行两种来源：

```python
if source not in ("paradox", "irony"):
    source, raw_id = "paradox", ""
```

`import:xxx` 会被**降级成「paradox + 空 id」**，于是照样用活动播放集的顺序。用户导入了、也选中了，排列一点没变。

现在加了第三条分支。

---

## 现在怎么工作

**选中导入的播放集时**：

- JSON 里 `ModIds` 的顺序**就是加载顺序**
- 在名单里的模组 → 启用，位置按名单下标
- 不在名单里的 → 未启用

和 Irony 那套是同一个逻辑（Irony 的 `ModIds` 也是它的加载顺序）。

**实测**（6 个模组，故意倒序写进 JSON）：

```
导入 → 选中 → 读出顺序
  pos=0  3153706012  ! Immersive Energy Shield
  pos=1  2633444661  ! IBS Animated Map Icons
  pos=2  2411818376  ! Dark Blue UI Remake
  pos=3  2466607238  ! Core Framework of Kasako
  pos=4  2475302050  ! Better Performance & Utilities
  pos=5  2660548454  ! Age of Wonder
✓ 与 JSON 倒序一致
```

---

## 写回时怎么样

导入的播放集**不是启动器里真实存在的配置**，数据库里没有那个 id，所以没法往里写。

选中它点「应用」时：排列会被写到**官方库的活动播放集**上，结果里明确写一行：

```
导入的播放集不能直接写回，已写入活动播放集「IronyModManager」
```

**这句话必须写清楚** —— 不写就是「以为改了 A 其实改了 B」，那是这软件最不该犯的错。

如果你只想用它分析、不想改任何东西：别点应用就行，选中它只影响**分析范围**。

---

## 验证

```
Python 语法 0 错 · 前端六项全过
unified_patch 28/28 · patch_io 28/28 · consent 6/6
导入→选中→排列 全链路一致
```

---

## 安装

| 文件 | 说明 |
| --- | --- |
| `StellarisModDoctor-Setup.exe` | 完整版 · 安装版 |
| `StellarisModDoctor-Portable.exe` | 完整版 · 免安装 |
| `StellarisModDoctor-Light-Setup.exe` | 轻量版 · 安装版 |
| `StellarisModDoctor-Light-Portable.exe` | 轻量版 · 免安装 |
