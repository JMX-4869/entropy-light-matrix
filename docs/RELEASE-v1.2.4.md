## 熵光矩阵 1.2.4

加了**手动导入播放集 JSON**。上一版我说「Irony 会自动发现，不用导入」——那是我把事情想简单了。

---

## 为什么需要导入

自动探测只认两种来源：官方启动器的 sqlite，和 Irony Mod Manager 的 `Database_*.json`（而且只在 `%APPDATA%` / `%LOCALAPPDATA%` 下面一层通配里找）。

真实情况比这杂：

- Irony 装在非默认位置 → 探测不到
- 用的是别的第三方启动器 → 格式各不相同
- 别人发来一个播放集 JSON，想直接用

这三种都落到「手动给他一个文件路径」。

---

## 怎么用

模组列表页，播放集选择器右边多了一个「导入 JSON」按钮。

点开 → 选文件 → 导入 → 选择器里立刻多出一项，来源标为「手动导入」。

导入的播放集存在：

```
%LOCALAPPDATA%\StellarisModDoctor\imported_playsets.json
```

重启不丢。

---

## 支持三种格式

**A. Irony ModManager 的 `Database_*.json`**（整个文件直接导）

```json
[{"Name": "ModCollection", "Value": [
   {"Name": "铁人局", "Game": "Stellaris",
    "ModIds": [{"SteamId": "123"}, {"SteamId": "456"}]}
]}]
```

会自动跳过 `Game` 不是 `Stellaris` 的项（Irony 的库里混着欧陆、钢四、群星）。

**B. 单条播放集**（最省事的手写格式）

```json
{"Name": "我的播放集", "ModIds": ["123", "456"]}
```

或

```json
{"name": "我的播放集", "mods": [123, 456]}
```

**C. Paradox 官方启动器导出**

```json
{"name": "...", "mods": [{"steamId": "123"}, …]}
```

三种都归一成同一份内部结构。

---

## 几个细节

**重复导入会自动跳过** —— 按「名字 + 模组列表」判重，同一个文件导两次不会出两条。

**导入的是「集合」，不是「当前生效的配置」** —— 选它等于「按这份名单和顺序去分析」，写回时仍然写官方库那套（官方库才是游戏真正读的地方）。界面上的「活动」标记不会变。

**顺序有意义** —— JSON 里模组列表的顺序就是加载顺序，选中后按这个顺序分析。

**坏文件会报错，不会静默成功** —— 不是 JSON、找不到播放集，都会在界面上给出具体原因。

---

## 验证

后端端到端：

```
import  → ok=True added=1 names=['端到端测试']
list    → 1 条  存储=...\imported_playsets.json
discover→ [手动导入] 端到端测试
remove  → ok=True removed=1
```

格式解析：

```
单条格式      ok=True added=1
Irony 数据库  ok=True added=2（欧陆被排除）
重复导入      ok=True added=0  note=这些播放集之前已经导入过了
坏文件        ok=False error=不是合法的 JSON
```

前端：六项全过 · i18n 38/38 · Python 语法 0 错 · 模块导入 0 失败

---

## 安装

| 文件 | 说明 |
| --- | --- |
| `StellarisModDoctor-Setup.exe` | 完整版 · 安装版 |
| `StellarisModDoctor-Portable.exe` | 完整版 · 免安装 |
| `StellarisModDoctor-Light-Setup.exe` | 轻量版 · 安装版 |
| `StellarisModDoctor-Light-Portable.exe` | 轻量版 · 免安装 |
