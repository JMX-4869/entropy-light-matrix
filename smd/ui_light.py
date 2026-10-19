"""轻量界面 —— 极简 Tkinter 版，不加载 WebView2。

**为什么存在**

网页版 570 MB 里主进程只占 10 MB，剩下全在 Edge WebView2 —— Chromium 的
架构成本，调参压不下去（实测 `--js-flags` / `--renderer-process-limit` /
`--disable-gpu` 全在 ±1%，且查命令行确认参数根本没传进去，被 pywebview 的
显式赋值覆盖）。不跟 Chromium 较劲，另起一套原生界面：**同一个后端，两套壳**。

**功能覆盖**

这不是阉割版。原版的核心链路全部保留：

    模组    列表 + 逐项开关（写回前必确认）
    冲突    检测结果 + 双击看成因与建议
    排序    最优顺序 + 手动上下移动
    诊断    崩溃报告 / 日志分析 / 成就影响 / 联机校验
    补丁    候选 / 自动生成 / 已安装管理
    知识库  时之虫档案搜索 + 提问
    AI      接管排查 / 分析排序 / 分析日志 / 分析崩溃
    设置    路径、API Key、模型、语言

**不做**：主题系统（CSS 专属）、界面动效、星图背景。

**视觉**：色值直接取 `theme.BUILTIN["stellaris"]` —— 和网页版同一套
深空蓝底 / 青金描边 / 淡蓝白字。两套界面共用一个配色源头，改主题不会掉队。

**底线**：绝不自动关闭模组。应用时只关 `approved_disable()` 里的，
开关模组必须逐个点、且写回前弹确认。
"""

from __future__ import annotations

import json
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Any, Callable

from . import APP_NAME, __version__
from .theme import BUILTIN

# ── 视觉：取网页版「群星（默认）」那套色值，不另抄一份
_T = BUILTIN["stellaris"]["tokens"]
BG, BG2 = _T["bg"], _T["bg2"]
PANEL, PANEL2, PANEL3 = _T["panel"], _T["panel2"], _T["panel3"]
LINE, LINE2 = _T["line"], _T["line2"]
FG, DIM = _T["fg"], _T["dim"]
ACC, ACC2, ACC3 = _T["acc"], _T["acc2"], _T["acc3"]
ACCTEXT = _T["acctext"]

SEV_COLOR = {"critical": _T["crit"], "high": _T["high"],
             "medium": _T["med"], "low": _T["low"], "info": _T["info"]}
SEV_LABEL = {"critical": "致命", "high": "严重", "medium": "中等",
             "low": "轻微", "info": "提示"}
SEV_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
FONT = ("Microsoft YaHei UI", 9)


class LightUI:
    """轻量界面。"""

    POLL_MS = 250

    def __init__(self, app) -> None:
        self.app = app
        self.root = tk.Tk()
        self.root.title(f"{APP_NAME} 轻量版 v{__version__}")
        self.root.geometry("1240x800")
        self.root.minsize(940, 540)
        self._busy = False
        self._err = ""
        self._playsets: list[dict] = []
        self._findings: list[dict] = []
        self._build()
        self._poll()

    # ================================================================ 样式

    def _apply_style(self) -> None:
        """把 ttk 染成群星那套视觉。

        必须用 ``clam``：Windows 默认的 ``vista`` 主题会**忽略**大多数颜色
        配置（TButton 的 background 尤其典型），想改色只能换可完全定制的
        clam。

        **极简不等于不好看。** Tkinter 没有圆角、没有阴影、没有渐变，
        能用来自造质感的只有三样东西，全部要用上：

        * **留白** —— padding 给足，控件之间不贴着
        * **层级** —— 顶栏 / 内容区 / 状态栏三段不同深浅，眼睛才有落点
        * **克制** —— 只有主操作染强调色，其余全是中性深色。
          到处高亮等于没有高亮。

        金色（``ACC3``）只用在两处：品牌名和表头文字。群星那种"切角嵌板 +
        青金描边"的高级感，八成来自**金色出现得少**。
        """
        s = ttk.Style(self.root)
        try:
            s.theme_use("clam")
        except tk.TclError:
            pass

        s.configure(".", background=BG, foreground=FG, fieldbackground=PANEL,
                    font=FONT, bordercolor=LINE, darkcolor=BG2,
                    lightcolor=BG2, troughcolor=BG2, focuscolor=ACC)
        s.configure("TFrame", background=BG)
        s.configure("Tall.TFrame", background=BG2)      # 顶栏
        s.configure("Foot.TFrame", background=BG2)      # 状态栏
        s.configure("TLabel", background=BG, foreground=FG)
        s.configure("Dim.TLabel", background=BG, foreground=DIM)
        # 字体必须用**元组**或单个家族名。写成 "{A} {B} 13 bold"（CSS 那种
        # fallback 列表）会被 Tk 当成一个不存在的字体名，退化成 1 像素 ——
        # 实测那个标签宽高都是 4px，肉眼等于没渲染。
        s.configure("Brand.TLabel", background=BG2, foreground=ACC3,
                    font=("Microsoft YaHei UI", 14, "bold"))
        s.configure("BrandSub.TLabel", background=BG2, foreground=DIM,
                    font=("Microsoft YaHei UI", 8))
        s.configure("FootLabel.TLabel", background=BG2, foreground=DIM,
                    font=("Microsoft YaHei UI", 8))

        # 普通按钮：全中性，靠悬停提亮一档
        s.configure("TButton", background=PANEL2, foreground=FG,
                    bordercolor=LINE, relief="flat", padding=(12, 6),
                    font=FONT)
        s.map("TButton",
              background=[("active", PANEL3), ("pressed", LINE2),
                          ("disabled", PANEL)],
              foreground=[("disabled", DIM)],
              bordercolor=[("active", LINE2)])
        # 主按钮：唯一染强调色的东西
        s.configure("Accent.TButton", background=ACC, foreground=ACCTEXT,
                    bordercolor=ACC, relief="flat", padding=(14, 6),
                    font=("Microsoft YaHei UI", 9, "bold"))
        s.map("Accent.TButton",
              background=[("active", ACC2), ("pressed", ACC2),
                          ("disabled", PANEL2)],
              foreground=[("disabled", DIM)])

        s.configure("TEntry", fieldbackground=PANEL, foreground=FG,
                    bordercolor=LINE, insertcolor=ACC, padding=6,
                    relief="flat")
        s.map("TEntry", bordercolor=[("focus", ACC)])
        s.configure("TCombobox", fieldbackground=PANEL, background=PANEL2,
                    foreground=FG, arrowcolor=ACC3, bordercolor=LINE,
                    padding=5, relief="flat")
        s.map("TCombobox",
              fieldbackground=[("readonly", PANEL)],
              bordercolor=[("focus", ACC), ("active", LINE2)])
        s.configure("TSeparator", background=LINE)

        # 表格：行高给到 27 —— 25 太挤，密排文字必须有呼吸
        s.configure("Treeview", background=PANEL, fieldbackground=PANEL,
                    foreground=FG, bordercolor=LINE, rowheight=27, font=FONT,
                    relief="flat")
        s.map("Treeview",
              background=[("selected", ACC)],
              foreground=[("selected", ACCTEXT)])
        s.configure("Treeview.Heading", background=BG2, foreground=ACC3,
                    relief="flat", padding=(10, 7),
                    font=("Microsoft YaHei UI", 9, "bold"),
                    bordercolor=LINE)
        s.map("Treeview.Heading", background=[("active", PANEL2)])

        # 页签：未选是暗字，选中提亮 + 金色文字
        s.configure("TNotebook", background=BG, bordercolor=LINE,
                    tabmargins=(0, 6, 0, 0), borderwidth=0)
        s.configure("TNotebook.Tab", background=BG2, foreground=DIM,
                    padding=(20, 7), bordercolor=BG2,
                    font=("Microsoft YaHei UI", 9))
        s.map("TNotebook.Tab",
              background=[("selected", PANEL), ("active", PANEL2)],
              foreground=[("selected", ACC3)],
              # clam 的选中态还会读这两个 —— 不一起盖掉，选中的页签会露
              # 出主题默认的浅色（截图里是刺眼的白底黑字）
              lightcolor=[("selected", PANEL), ("active", PANEL2)],
              darkcolor=[("selected", PANEL), ("active", PANEL2)],
              bordercolor=[("selected", ACC3)],
              font=[("selected", ("Microsoft YaHei UI", 9, "bold"))])

        s.configure("TProgressbar", background=ACC, troughcolor=BG2,
                    bordercolor=BG2, lightcolor=ACC, darkcolor=ACC,
                    thickness=6)
        s.configure("TCheckbutton", background=BG, foreground=FG,
                    indicatorcolor=PANEL2, focuscolor=BG)
        s.map("TCheckbutton",
              background=[("active", BG)],
              indicatorcolor=[("selected", ACC)])
        # clam 的滑块是用 lightcolor/darkcolor 画的，只给 background 会
        # 露出主题默认的浅灰 —— 在深底上像一道划痕
        for orient in ("Vertical", "Horizontal"):
            s.configure(f"{orient}.TScrollbar", background=PANEL3,
                        troughcolor=BG2, bordercolor=BG2, arrowcolor=DIM,
                        lightcolor=PANEL3, darkcolor=PANEL3, relief="flat",
                        width=10)
            s.map(f"{orient}.TScrollbar",
                  background=[("active", LINE2)],
                  lightcolor=[("active", LINE2)],
                  darkcolor=[("active", LINE2)],
                  arrowcolor=[("active", ACC3)])

    # ================================================================ 布局

    def _build(self) -> None:
        """主布局。一律 grid + 权重，不用 pack 链。

        上次安装器翻过车：内容 406px 塞进写死 400px 的窗口，靠压缩日志框
        勉强吸收，换个字体行高就溢出、按钮被挤出可视区。grid 的权重分配
        没有这个问题 —— 表格吃剩余空间，其余控件按请求高度拿固定份额。

        纵向三段式：**顶栏（身份 + 主操作）/ 内容 / 状态栏**。
        三段用不同底色分开，眼睛一进来就知道哪块是干什么的 ——
        这比任何装饰都更能撑起"好看"。
        """
        self._apply_style()
        self.root.configure(bg=BG)
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(3, weight=1)

        # ── 顶栏：品牌 + 版本 + 主操作
        bar = ttk.Frame(self.root, style="Tall.TFrame", padding=(16, 10))
        bar.grid(row=0, column=0, sticky="ew")
        ttk.Label(bar, text=APP_NAME, style="Brand.TLabel").pack(side="left")
        ttk.Label(bar, text=f"轻量版  v{__version__}",
                  style="BrandSub.TLabel").pack(side="left",
                                                padx=(10, 0), pady=(6, 0))
        # 主操作在右，唯一染强调色的按钮
        self.btn_apply = ttk.Button(bar, text="应用", width=8,
                                    style="Accent.TButton",
                                    command=lambda: self._safe(self.on_apply))
        self.btn_apply.pack(side="right")
        self.btn_cancel = ttk.Button(bar, text="取消", width=7,
                                     command=self.on_cancel,
                                     state="disabled")
        self.btn_cancel.pack(side="right", padx=(0, 8))
        self.btn_solve = ttk.Button(bar, text="排序", width=8,
                                    command=lambda: self._safe(self.on_solve))
        self.btn_solve.pack(side="right", padx=(0, 8))
        self.btn_scan = ttk.Button(bar, text="扫描", width=8,
                                   command=lambda: self._safe(self.on_scan))
        self.btn_scan.pack(side="right", padx=(0, 8))

        # ── 第二行：播放集与备份。压暗一档，不与主操作抢视线
        row2 = ttk.Frame(self.root, style="Tall.TFrame", padding=(16, 0))
        row2.grid(row=1, column=0, sticky="ew")
        self.cb_playset = ttk.Combobox(row2, width=40, state="readonly")
        self.cb_playset.pack(side="left", pady=(0, 10))
        self.cb_playset.bind("<<ComboboxSelected>>", self.on_playset_change)
        for txt, cmd in (("重命名", self.on_playset_rename),
                         ("备份", self.on_backup),
                         ("回滚", self.on_rollback)):
            ttk.Button(row2, text=txt, width=7,
                       command=lambda c=cmd: self._safe(c)).pack(
                side="left", padx=(8, 0), pady=(0, 10))
        self.lbl_hint = ttk.Label(row2, text="", style="Dim.TLabel")
        self.lbl_hint.pack(side="right", pady=(0, 10))

        # 顶栏与内容之间的那道细线 —— 群星的"青金描边"就靠这种线
        tk.Frame(self.root, height=1, bg=LINE2).grid(row=2, column=0,
                                                     sticky="ew")

        # ── 内容
        self.nb = ttk.Notebook(self.root)
        self.nb.grid(row=3, column=0, sticky="nsew", padx=14, pady=(10, 0))
        self._build_mods()
        self._build_conflicts()
        self._build_order()
        self._build_diagnose()
        self._build_patches()
        self._build_wiki()
        self._build_ai()
        self._build_settings()

        # ── 状态栏：压到最暗，只在需要时被看见
        foot = ttk.Frame(self.root, style="Foot.TFrame", padding=(16, 7))
        foot.grid(row=4, column=0, sticky="ew")
        self.pb = ttk.Progressbar(foot, mode="determinate", maximum=100,
                                  length=180)
        self.pb.pack(side="left")
        self.lbl_msg = ttk.Label(foot, text="就绪", style="FootLabel.TLabel",
                                 anchor="w")
        self.lbl_msg.pack(side="left", fill="x", expand=True, padx=(12, 0))

    def _btn(self, parent, text: str, cmd: Callable | None = None,
             width: int = 8) -> ttk.Button:
        """工具栏按钮（pack 到左侧）。"""
        b = ttk.Button(parent, text=text, width=width,
                       command=(lambda: self._safe(cmd)) if cmd else None)
        b.pack(side="left", padx=(0, 5))
        return b

    def _frame(self, label: str) -> ttk.Frame:
        """新建一个页签，返回内容区。"""
        f = ttk.Frame(self.nb, padding=(14, 12))
        self.nb.add(f, text=f" {label} ")
        return f

    def _table(self, parent, cols, heads, widths) -> ttk.Treeview:
        """一个带滚动条的表格（自己吃满父容器）。"""
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(1, weight=1)
        tv = ttk.Treeview(parent, columns=cols, show="headings",
                          selectmode="browse")
        for c, h, w in zip(cols, heads, widths):
            tv.heading(c, text=h)
            tv.column(c, width=w, stretch=c in ("name", "title", "text"))
        vs = ttk.Scrollbar(parent, orient="vertical", command=tv.yview)
        tv.configure(yscrollcommand=vs.set)
        tv.grid(row=1, column=0, sticky="nsew", pady=(2, 0))
        vs.grid(row=1, column=1, sticky="ns")
        tv.tag_configure("odd", background=PANEL2)
        tv.tag_configure("off", foreground=DIM)
        for sev, color in SEV_COLOR.items():
            tv.tag_configure(sev, foreground=color)
        return tv

    def _row(self, parent, row: int = 0) -> ttk.Frame:
        """一行按钮/输入的容器。"""
        f = ttk.Frame(parent)
        f.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        return f

    def _text(self, parent, row: int = 1) -> tk.Text:
        """一个只读文本区（自己吃满剩余空间）。"""
        wrap = ttk.Frame(parent)
        wrap.grid(row=row, column=0, columnspan=2, sticky="nsew")
        wrap.columnconfigure(0, weight=1)
        wrap.rowconfigure(0, weight=1)
        parent.rowconfigure(row, weight=1)
        t = tk.Text(wrap, wrap="word", height=8, relief="flat", bg=PANEL,
                    fg=FG, insertbackground=ACC, padx=12, pady=10,
                    selectbackground=ACC, selectforeground=ACCTEXT,
                    font=FONT)
        vs = ttk.Scrollbar(wrap, orient="vertical", command=t.yview)
        t.configure(yscrollcommand=vs.set)
        t.grid(row=0, column=0, sticky="nsew")
        vs.grid(row=0, column=1, sticky="ns")
        wrap._text = t
        t._wrap = wrap
        return t

    # ---------------------------------------------------------------- 页

    def _build_mods(self) -> None:
        """模组页：列表 + 开关 + 详情。"""
        f = self._frame("模组")
        top = self._row(f)
        self._btn(top, "刷新", self.refresh_mods)
        self._btn(top, "启用", lambda: self.on_mod_switch(True), width=6)
        self._btn(top, "停用", lambda: self.on_mod_switch(False), width=6)
        self._btn(top, "详情", self.on_mod_detail, width=6)
        self._btn(top, "工作坊", self.on_open_workshop, width=8)
        self.chk_ghost = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, text="显示幽灵模组", variable=self.chk_ghost,
                        command=self.refresh_mods).pack(side="left", padx=8)
        self.mods_view = self._table(
            f, ("on", "name", "version", "workshop", "status"),
            ("启用", "名称", "版本", "工坊 ID", "状态"),
            (48, 520, 100, 110, 130))
        self.mods_view.bind("<Double-1>", lambda _e: self.on_mod_detail())

    def _build_conflicts(self) -> None:
        """冲突页。"""
        f = self._frame("冲突")
        top = self._row(f)
        self._btn(top, "刷新", self.refresh_findings)
        self._btn(top, "详情", self.on_finding_detail, width=6)
        ttk.Label(top, text="双击一行看成因与建议").pack(side="left", padx=8)
        self.conf_view = self._table(
            f, ("sev", "kind", "title", "mods"),
            ("严重度", "类型", "结论", "涉及模组"), (70, 50, 470, 400))
        self.conf_view.bind("<Double-1>", self.on_finding_detail)

    def _build_order(self) -> None:
        """排序页。"""
        f = self._frame("排序")
        top = self._row(f)
        self._btn(top, "刷新", self.refresh_order)
        self._btn(top, "上移", lambda: self.on_move(-1), width=6)
        self._btn(top, "下移", lambda: self.on_move(1), width=6)
        ttk.Label(top, text="游戏里后加载的覆盖先加载的").pack(side="left",
                                                             padx=8)
        self.order_view = self._table(
            f, ("pos", "name", "why"), ("顺序", "名称", "说明"),
            (64, 560, 320))

    def _build_diagnose(self) -> None:
        """诊断页：崩溃 / 日志 / 成就 / 联机 四个子页签。"""
        f = self._frame("诊断")
        f.columnconfigure(0, weight=1)
        f.rowconfigure(0, weight=1)
        sub = ttk.Notebook(f)
        sub.grid(row=0, column=0, sticky="nsew")

        # ── 崩溃
        c = ttk.Frame(sub, padding=(14, 12))
        sub.add(c, text=" 崩溃 ")
        cr = self._row(c)
        self._btn(cr, "刷新", self.refresh_crash, width=6)
        self._btn(cr, "分析选中", self.on_analyze_crash, width=10)
        self._btn(cr, "分析日志", self.on_analyze_log, width=10)
        self.crash_view = self._table(c, ("name",), ("崩溃记录",), (700,))
        self.crash_view.bind("<Double-1>", lambda _e: self.on_show_crash())
        c_text = ttk.Frame(c)
        c_text.grid(row=2, column=0, columnspan=2, sticky="nsew")
        c.rowconfigure(2, weight=1)
        c_text.columnconfigure(0, weight=1)
        c_text.rowconfigure(0, weight=1)
        self.crash_text = self._text(c_text, 0)
        self.crash_text._wrap = c_text

        # ── 日志
        g = ttk.Frame(sub, padding=(14, 12))
        sub.add(g, text=" 日志 ")
        gr = self._row(g)
        self._btn(gr, "刷新", self.refresh_logs, width=6)
        self._btn(gr, "分析选中", self.on_analyze_log, width=10)
        self.cb_log = ttk.Combobox(gr, width=30, state="readonly")
        self.cb_log.pack(side="left", padx=8)
        g.columnconfigure(0, weight=1)
        g.rowconfigure(1, weight=1)
        gt = ttk.Frame(g)
        gt.grid(row=1, column=0, columnspan=2, sticky="nsew")
        gt.columnconfigure(0, weight=1)
        gt.rowconfigure(0, weight=1)
        self.log_text = self._text(gt, 0)

        # ── 成就
        a = ttk.Frame(sub, padding=(14, 12))
        sub.add(a, text=" 成就 ")
        ar = self._row(a)
        self._btn(ar, "扫描", self.on_scan_achievement, width=8)
        self._btn(ar, "关闭选中", self.on_disable_achievement, width=10)
        ttk.Label(ar, text="只列会破坏成就的模组；关闭前会确认").pack(
            side="left", padx=8)
        self.ach_view = self._table(
            a, ("mod", "reason"), ("模组", "影响成就的原因"), (420, 520))

        # ── 联机
        m = ttk.Frame(sub, padding=(14, 12))
        sub.add(m, text=" 联机 ")
        mr = self._row(m)
        self._btn(mr, "校验扫描", self.on_scan_checksum, width=10)
        ttk.Label(mr, text="找会导致校验码不一致的模组").pack(
            side="left", padx=8)
        self.multi_view = self._table(
            m, ("mod", "reason"), ("模组", "会导致校验码不一致"), (420, 520))

    def _build_patches(self) -> None:
        """补丁页。"""
        f = self._frame("补丁")
        f.columnconfigure(0, weight=1)
        f.columnconfigure(1, weight=1)
        f.rowconfigure(1, weight=1)
        top = ttk.Frame(f)
        top.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 6))
        self._btn(top, "候选", self.refresh_patch_candidates, width=6)
        self._btn(top, "自动生成", self.on_generate_patches, width=10)
        self._btn(top, "刷新已装", self.refresh_patches, width=10)
        self._btn(top, "删除选中", self.on_remove_patch, width=10)

        left = ttk.Frame(f)
        left.grid(row=1, column=0, sticky="nsew", padx=(0, 6))
        ttk.Label(left, text="可修补的冲突").grid(row=0, column=0, sticky="w")
        left.columnconfigure(0, weight=1)
        left.rowconfigure(1, weight=1)
        self.cand_view = self._table(left, ("kind", "title"),
                                     ("类型", "冲突"), (60, 420))
        self.cand_view.grid_configure(row=1)

        right = ttk.Frame(f)
        right.grid(row=1, column=1, sticky="nsew")
        ttk.Label(right, text="已安装的补丁").grid(row=0, column=0, sticky="w")
        right.columnconfigure(0, weight=1)
        right.rowconfigure(1, weight=1)
        self.patch_view = self._table(right, ("name", "size"),
                                      ("补丁名", "大小"), (380, 90))
        self.patch_view.grid_configure(row=1)

    def _build_wiki(self) -> None:
        """知识库页。"""
        f = self._frame("知识库")
        top = self._row(f)
        ttk.Label(top, text="搜索").pack(side="left")
        self.ent_wiki = ttk.Entry(top, width=42)
        self.ent_wiki.pack(side="left", padx=6)
        self.ent_wiki.bind("<Return>", lambda _e: self.on_wiki_search())
        self._btn(top, "搜索", self.on_wiki_search)
        self._btn(top, "提问", self.on_wiki_ask, width=6)
        ttk.Label(top, text="（提问需要先配好 AI）").pack(side="left", padx=6)
        self.wiki_view = self._table(f, ("title",), ("结果",), (860,))
        self.wiki_view.bind("<<TreeviewSelect>>", self.on_wiki_select)

    def _build_ai(self) -> None:
        """AI 页。"""
        f = self._frame("AI")
        top = self._row(f)
        self._btn(top, "接管排查", self.on_ai_takeover, width=10)
        self._btn(top, "分析排序", self.on_ai_order, width=10)
        self._btn(top, "测试连接", self.on_ai_test, width=10)
        ttk.Label(top, text="需要先在「设置」里填 API Key 并打开隐私开关").pack(
            side="left", padx=8)
        f.columnconfigure(0, weight=1)
        f.rowconfigure(1, weight=1)
        self.ai_text = self._text(f)

    def _build_settings(self) -> None:
        """设置页。"""
        f = self._frame("设置")
        f.columnconfigure(1, weight=1)

        def row(r: int, label: str, widget) -> None:
            ttk.Label(f, text=label).grid(row=r, column=0, sticky="w",
                                          pady=4, padx=(0, 10))
            widget.grid(row=r, column=1, sticky="ew", pady=4)

        self.ent_game = ttk.Entry(f)
        row(0, "游戏目录", self.ent_game)
        self.ent_mod = ttk.Entry(f)
        row(1, "模组目录", self.ent_mod)
        self.ent_key = ttk.Entry(f, show="•")
        row(2, "API Key", self.ent_key)
        self.ent_model = ttk.Entry(f)
        row(3, "模型", self.ent_model)
        self.cb_lang = ttk.Combobox(f, values=["zh", "en"], state="readonly",
                                    width=8)
        row(4, "语言", self.cb_lang)
        self.chk_ai = tk.BooleanVar(value=False)
        ttk.Checkbutton(f, text="允许把模组内容发给 AI（隐私开关）",
                        variable=self.chk_ai).grid(
            row=5, column=0, columnspan=2, sticky="w", pady=6)

        btns = ttk.Frame(f)
        btns.grid(row=6, column=0, columnspan=2, sticky="w", pady=(10, 0))
        self._btn(btns, "自动识别路径", self.on_autodetect, width=14)
        self._btn(btns, "保存", self.on_save_settings, width=8)
        ttk.Label(f, text="设置与配置文件共用：%LOCALAPPDATA%\\StellarisModDoctor",
                  foreground=DIM).grid(row=7, column=0, columnspan=2,
                                       sticky="w", pady=(14, 0))
        self._load_settings()

    # ================================================================ 刷新

    @staticmethod
    def _reset(tv: ttk.Treeview) -> None:
        tv.delete(*tv.get_children())

    @staticmethod
    def _hint(tv: ttk.Treeview, text: str, cols: int, col: int = 0) -> None:
        """空表格里放一句提示。一片纯黑会让人以为界面坏了。

        ``col`` 是提示落在第几列 —— **必须挑一个能拉伸的列**。
        落在固定宽度的首列（比如「严重度」70px）会被裁掉半句话。
        """
        tv.tag_configure("hint", foreground=DIM)
        vals = [""] * cols
        vals[col] = text
        tv.insert("", "end", values=tuple(vals), tags=("hint",))

    def refresh_mods(self) -> None:
        """填模组表。"""
        self._reset(self.mods_view)
        try:
            rows = self.app.mods_list()
        except Exception as exc:                  # noqa: BLE001
            self.set_msg(f"读模组失败：{exc}")
            return
        if not self.chk_ghost.get():
            rows = [m for m in rows if m.get("status") != "ghost"]
        for i, m in enumerate(rows):
            tags = []
            if not m.get("enabled"):
                tags.append("off")
            if i % 2:
                tags.append("odd")
            self.mods_view.insert(
                "", "end", iid=m.get("key", str(i)),
                values=("✓" if m.get("enabled") else "",
                        m.get("name", ""), m.get("version", "") or "",
                        m.get("remote_file_id", "") or "本地",
                        m.get("status", "")),
                tags=tuple(tags))
        if not rows:
            self._hint(self.mods_view,
                       "没有读到模组 —— 检查「设置」里的路径", 5, 1)
        self.mods_view._rows = rows
        on = sum(1 for m in rows if m.get("enabled"))
        self.set_msg(f"{len(rows)} 个模组 · 启用 {on}")

    def refresh_findings(self) -> None:
        """填冲突表。"""
        self._reset(self.conf_view)
        try:
            rows = self.app.findings(limit=3000)
        except Exception as exc:                  # noqa: BLE001
            self.set_msg(f"读冲突失败：{exc}")
            return
        rows.sort(key=lambda r: (SEV_ORDER.get(r.get("severity"), 9),
                                 -len(r.get("mods") or ())))
        self._findings = rows
        for i, f in enumerate(rows):
            names = f.get("mod_names") or f.get("mods") or []
            shown = " / ".join(names[:3]) + ("…" if len(names) > 3 else "")
            sev = f.get("severity", "")
            tags = [sev] if sev in SEV_COLOR else []
            if i % 2:
                tags.append("odd")
            self.conf_view.insert(
                "", "end", iid=f"f{i}",
                values=(SEV_LABEL.get(sev, ""), f.get("kind", ""),
                        f.get("title", ""), shown), tags=tuple(tags))
        if not rows:
            self._hint(self.conf_view,
                       "还没有检测结果 —— 点左上角的「扫描」", 4, 2)
        crit = sum(1 for f in rows if f.get("severity") == "critical")
        self.set_msg(f"冲突 {len(rows)} 条 · 致命 {crit}")

    def refresh_order(self) -> None:
        """填排序表。"""
        self._reset(self.order_view)
        sol = getattr(self.app, "solution", None)
        if sol is None:
            self.set_msg("还没有排序结果，先点「排序」")
            return
        data = sol.to_dict() or {}
        names = {m.get("key"): m.get("name", "") for m in self.app.mods_list()}
        for i, key in enumerate(data.get("order") or [], 1):
            self.order_view.insert("", "end",
                                   values=(i, names.get(key, key), ""),
                                   tags=("odd",) if i % 2 == 0 else ())
        if not n:
            self._hint(self.order_view,
                       "还没有排序结果 —— 先扫描，再点「排序」", 3, 1)
        n = len(data.get("order") or [])
        pend = len(sol.pending_decisions())
        self.set_msg(f"排序完成 · {n} 个模组"
                     + (f" · {pend} 项待你在网页版决定" if pend else ""))

    def refresh_playsets(self) -> None:
        """填播放集下拉。"""
        try:
            data = self.app.list_playsets()
        except Exception as exc:                  # noqa: BLE001
            self.lbl_hint.configure(text=f"播放集读取失败：{exc}")
            return
        self._playsets = data.get("playsets") or []
        labels = []
        for p in self._playsets:
            src = {"paradox": "官方", "irony": "Irony"}.get(
                p.get("source", ""), p.get("source", ""))
            mark = " ← 当前" if p.get("active") else ""
            same = ""
            for twin in (p.get("same_as") or []):
                nm = (twin.get("name") if isinstance(twin, dict)
                      else str(twin))
                if nm:
                    same += f" ↔ {nm}"
                    break
            labels.append(f"[{src}] {p.get('name', '')}"
                          f" ({p.get('enabled', 0)}/{p.get('mods', 0)})"
                          f"{mark}{same}")
        self.cb_playset.configure(values=labels)
        for i, p in enumerate(self._playsets):
            if p.get("active"):
                self.cb_playset.current(i)
                break
        if labels:
            self.lbl_hint.configure(text=f"{len(labels)} 个播放集")

    def refresh_crash(self) -> None:
        """填崩溃列表。"""
        self._reset(self.crash_view)
        try:
            rows = self.app.crash_dirs()
        except Exception as exc:                  # noqa: BLE001
            self.set_msg(f"读崩溃记录失败：{exc}")
            return
        for i, c in enumerate(rows):
            self.crash_view.insert(
                "", "end", iid=f"c{i}",
                values=(c.get("name") or c.get("path") or str(c),),
                tags=("odd",) if i % 2 else ())
        if not rows:
            self._hint(self.crash_view, "没有找到崩溃记录", 1)
        self.crash_view._rows = rows
        self.set_msg(f"{len(rows)} 条崩溃记录")

    def refresh_logs(self) -> None:
        """填日志下拉。"""
        try:
            rows = self.app.log_files()
        except Exception as exc:                  # noqa: BLE001
            self.set_msg(f"读日志失败：{exc}")
            return
        names = [r.get("name") if isinstance(r, dict) else str(r)
                 for r in rows]
        self.cb_log.configure(values=names)
        if names:
            self.cb_log.current(0)
        self.set_msg(f"{len(names)} 个日志文件")

    def refresh_patch_candidates(self) -> None:
        """填补丁候选表。"""
        self._reset(self.cand_view)
        try:
            rows = self.app.patch_candidates(200)
        except Exception as exc:                  # noqa: BLE001
            self.set_msg(f"读候选失败：{exc}")
            return
        for i, c in enumerate(rows):
            self.cand_view.insert(
                "", "end", iid=f"p{i}",
                values=(c.get("kind", ""), c.get("title", "")),
                tags=("odd",) if i % 2 else ())
        self.set_msg(f"{len(rows)} 个可修补冲突")

    def refresh_patches(self) -> None:
        """填已装补丁表。"""
        self._reset(self.patch_view)
        try:
            rows = self.app.patch_list()
        except Exception as exc:                  # noqa: BLE001
            self.set_msg(f"读补丁失败：{exc}")
            return
        for i, p in enumerate(rows):
            size = p.get("size")
            self.patch_view.insert(
                "", "end", iid=f"q{i}",
                values=(p.get("name", ""),
                        f"{size / 1024:.1f} KB" if isinstance(size, (int, float))
                        else ""),
                tags=("odd",) if i % 2 else ())
        if not rows:
            self._hint(self.patch_view, "还没有安装任何补丁", 2)
        self.patch_view._rows = rows
        self.set_msg(f"{len(rows)} 个补丁")

    def refresh_wiki(self, rows: list[dict]) -> None:
        """填知识库结果表。"""
        self._reset(self.wiki_view)
        if not rows:
            self._hint(self.wiki_view, "没搜到结果 —— 换个关键词试试", 1)
        self._wiki_rows = rows
        for i, w in enumerate(rows):
            self.wiki_view.insert(
                "", "end", iid=f"w{i}",
                values=(w.get("title") or w.get("name") or str(w),),
                tags=("odd",) if i % 2 else ())
        self.set_msg(f"{len(rows)} 条结果")

    def set_msg(self, text: str) -> None:
        self.lbl_msg.configure(text=text)

    # ================================================================ 轮询

    def _poll(self) -> None:
        """读后台任务状态。Tk 只能主线程操作，所以后端在别的线程跑、
        这里轮询 —— 不往 Tk 里跨线程塞回调。"""
        try:
            t = self.app.task
            running = bool(getattr(t, "running", False))
            pct = int(max(0.0, min(1.0, float(
                getattr(t, "progress", 0.0) or 0.0))) * 100)
            if abs(float(self.pb["value"]) - pct) > 0.5:
                self.pb["value"] = pct
            msg = getattr(t, "message", "") or ""
            if msg:
                self.lbl_msg.configure(text=f"{msg} · {pct}%")
            if running != self._busy:
                self._busy = running
                self._buttons(running)
            err = getattr(t, "error", "") or ""
            if err and err != self._err:
                self._err = err
                self.set_msg(f"出错：{err}")
            if not running:
                self._err = ""
        except Exception:                          # noqa: BLE001
            pass
        self.root.after(self.POLL_MS, self._poll)

    def _buttons(self, busy: bool) -> None:
        st = "disabled" if busy else "normal"
        for b in (self.btn_scan, self.btn_solve, self.btn_apply):
            b.configure(state=st)
        self.btn_cancel.configure(state="normal" if busy else "disabled")

    def _safe(self, fn) -> None:
        """按钮回调兜底。Tk 会静默吞掉回调异常，打包后没控制台，
        用户看到的就是"点了没反应"。"""
        try:
            fn()
        except Exception as exc:                  # noqa: BLE001
            import traceback
            self.set_msg(f"出错：{type(exc).__name__}: {exc}")
            messagebox.showerror(
                APP_NAME, f"{type(exc).__name__}: {exc}\n\n"
                + "\n".join(traceback.format_exc().splitlines()[-5:]))

    def _async(self, work: Callable[[], Any], done: Callable[[Any], None],
               *args, **kwargs) -> None:
        """后台跑 ``work``，完成后在主线程调 ``done(结果)``。"""
        def job() -> None:
            try:
                r = work(*args, **kwargs)
            except Exception as exc:              # noqa: BLE001
                r = exc
            self.root.after(0, lambda: self._deliver(done, r))
        threading.Thread(target=job, daemon=True).start()

    def _deliver(self, done: Callable, r: Any) -> None:
        try:
            if isinstance(r, Exception):
                self.set_msg(f"出错：{r}")
                messagebox.showerror(APP_NAME, str(r))
                return
            done(r)
        except Exception as exc:                  # noqa: BLE001
            self.set_msg(f"处理失败：{exc}")

    def _wait_task(self, done: Callable[[], None]) -> None:
        """等后台任务跑完再刷新。"""
        def loop() -> None:
            for _ in range(9000):
                if not getattr(self.app.task, "running", False):
                    break
                time.sleep(0.1)
            self.root.after(0, done)
        threading.Thread(target=loop, daemon=True).start()

    # ================================================================ 动作

    def on_scan(self) -> None:
        """扫描 + 冲突检测。"""
        self._reset(self.conf_view)
        self._reset(self.order_view)
        if not self.app.start_scan(semantic=True):
            self.set_msg("扫描没能启动")
            messagebox.showwarning(APP_NAME, "扫描没能启动 —— 路径可能还没识别到。")
            return
        self.set_msg("扫描中…")
        self._wait_task(self._after_scan)

    def _after_scan(self) -> None:
        self.refresh_mods()
        self.refresh_findings()

    def on_solve(self) -> None:
        """求最优排序。"""
        self.set_msg("计算中…")
        self.root.update_idletasks()
        res = self.app.compute_solution()
        if isinstance(res, dict) and res.get("error"):
            self.set_msg(str(res["error"]))
            messagebox.showinfo(APP_NAME, str(res["error"]))
            return
        self.refresh_order()

    def on_apply(self) -> None:
        """落地方案。**只关用户确认过的模组**。"""
        sol = getattr(self.app, "solution", None)
        if sol is None:
            messagebox.showinfo(APP_NAME, "先扫描并排序。")
            return
        approved = list(sol.approved_disable())
        pending = len(sol.pending_decisions())
        if pending:
            messagebox.showwarning(
                APP_NAME,
                f"还有 {pending} 项冲突要你逐项决定是否关闭。\n\n"
                "轻量版不提供逐项确认 —— 请用网页版处理，\n"
                "或先只应用排序（不关闭任何模组）。")
            return
        lines = ["把加载顺序写回群星。"]
        if approved:
            names = {m.get("key"): m.get("name", "")
                     for m in self.app.mods_list()}
            lines.append(f"\n会关闭这 {len(approved)} 个你确认过的模组：")
            lines += [f"  · {names.get(k, k)}" for k in approved[:12]]
            if len(approved) > 12:
                lines.append(f"  … 还有 {len(approved) - 12} 个")
        else:
            lines.append("\n没有待关闭的模组 —— 只调整顺序，"
                         "不动任何模组的启用状态。")
        lines.append("\n写回前自动备份。继续？")
        if not messagebox.askyesno(APP_NAME, "\n".join(lines)):
            return
        self.set_msg("写回中…")
        self._async(self.app.apply_current_solution, self._after_apply)

    def _after_apply(self, r: dict) -> None:
        txt = "已写回。" if r.get("ok") else f"失败：{r.get('message') or r}"
        self.set_msg(txt)
        messagebox.showinfo(APP_NAME, txt)

    def on_cancel(self) -> None:
        self.app.cancel_task()
        self.set_msg("取消中…")

    # ---------------------------------------------------------------- 模组

    def on_mod_switch(self, enabled: bool) -> None:
        """切换选中模组的启用状态（**写回前确认**）。"""
        sel = self.mods_view.selection()
        if not sel:
            self.set_msg("先选一个模组")
            return
        rows = getattr(self.mods_view, "_rows", [])
        m = next((x for x in rows if x.get("key") == sel[0]), None)
        if not m:
            return
        act = "启用" if enabled else "停用"
        if not messagebox.askyesno(
                APP_NAME, f"要{act}「{m.get('name', sel[0])}」吗？\n\n"
                          "这只改启用状态，不会删除任何文件。"):
            return
        try:
            self.app.set_mod_switch(sel[0], enabled)
            self.app.start_apply_mod_switches()
        except Exception as exc:                  # noqa: BLE001
            self.set_msg(f"{act}失败：{exc}")
            return
        self.set_msg(f"已{act}，刷新中…")
        self._wait_task(self.refresh_mods)

    def on_mod_detail(self) -> None:
        """看模组详情。"""
        sel = self.mods_view.selection()
        if not sel:
            return
        rows = getattr(self.mods_view, "_rows", [])
        m = next((x for x in rows if x.get("key") == sel[0]), None)
        if not m:
            return
        body = "\n".join(f"{k}: {v}" for k, v in m.items()
                         if v not in ("", None, []) and k != "tags")
        self._textwin(m.get("name", sel[0]), body)

    def on_open_workshop(self) -> None:
        """打开工坊页面。"""
        sel = self.mods_view.selection()
        if not sel:
            return
        rows = getattr(self.mods_view, "_rows", [])
        m = next((x for x in rows if x.get("key") == sel[0]), None)
        url = (m or {}).get("workshop_url")
        if not url:
            self.set_msg("这个模组没有工坊链接")
            return
        self.app.open_url(url)

    # ---------------------------------------------------------------- 冲突

    def on_finding_detail(self, _e=None) -> None:
        """看冲突详情。"""
        sel = self.conf_view.selection()
        if not sel:
            return
        idx = int(str(sel[0])[1:])
        if idx >= len(self._findings):
            return
        f = self._findings[idx]
        parts = [f.get("title", ""), "",
                 f"类型：{f.get('kind', '')}",
                 f"严重度：{SEV_LABEL.get(f.get('severity'), '')}"]
        if f.get("target"):
            parts.append(f"冲突对象：{f['target']}")
        if f.get("mod_names"):
            parts += ["", "涉及模组："] + [f"  · {n}" for n in f["mod_names"]]
        if f.get("detail"):
            parts += ["", "成因：", f["detail"]]
        if f.get("advice"):
            parts += ["", "建议：", f["advice"]]
        self._textwin(f"{f.get('kind', '')} · {f.get('title', '')[:40]}",
                      "\n".join(parts))

    # ---------------------------------------------------------------- 排序

    def on_move(self, delta: int) -> None:
        """手动上移/下移选中项。"""
        sel = self.order_view.selection()
        if not sel:
            return
        try:
            self.app.reorder_move(int(str(sel[0])[1:]) - 1
                                  if str(sel[0]).startswith("o") else 0, delta)
        except Exception:                          # noqa: BLE001
            pass
        self.refresh_order()

    # ---------------------------------------------------------------- 诊断

    def on_show_crash(self) -> None:
        """读一条崩溃报告。"""
        sel = self.crash_view.selection()
        if not sel:
            return
        rows = getattr(self.crash_view, "_rows", [])
        idx = int(str(sel[0])[1:]) if str(sel[0])[1:].isdigit() else 0
        if idx >= len(rows):
            return
        c = rows[idx]
        path = c.get("path") if isinstance(c, dict) else str(c)
        try:
            txt = self.app.read_report(path)
        except Exception as exc:                  # noqa: BLE001
            txt = f"读取失败：{exc}"
        if isinstance(txt, dict):
            txt = txt.get("text") or json.dumps(txt, ensure_ascii=False,
                                                indent=2)
        self._set_text(self.crash_text, str(txt))

    def on_analyze_crash(self) -> None:
        """翻译当前崩溃报告。"""
        sel = self.crash_view.selection()
        if not sel:
            self.set_msg("先选一条崩溃记录")
            return
        idx = int(str(sel[0])[1:]) if str(sel[0])[1:].isdigit() else 0
        rows = getattr(self.crash_view, "_rows", [])
        if idx >= len(rows):
            return
        c = rows[idx]
        path = c.get("path") if isinstance(c, dict) else str(c)
        self.set_msg("分析中…")
        self._async(self.app.read_report, self._show_crash_report, path)

    def _show_crash_report(self, r: Any) -> None:
        self._set_text(self.crash_text, json.dumps(r, ensure_ascii=False,
                                                   indent=2)
                       if isinstance(r, dict) else str(r))

    def on_analyze_log(self) -> None:
        """分析日志。"""
        name = self.cb_log.get() or "error.log"
        self.set_msg(f"分析 {name}…")
        if not self.app.start_log_analysis(name):
            self.set_msg("日志分析没能启动")
            return
        self._wait_task(self._after_log)

    def _after_log(self) -> None:
        t = self.app.task
        res = getattr(t, "result", None)
        if res is None:
            self.set_msg("没有结果")
            return
        self._set_text(self.log_text,
                       json.dumps(res, ensure_ascii=False, indent=2)
                       if isinstance(res, dict) else str(res))

    def on_scan_achievement(self) -> None:
        """扫描影响成就的模组。"""
        self.set_msg("扫描成就影响…")
        self._async(self._achieve_rows, self._fill_achieve)

    def _achieve_rows(self) -> list:
        try:
            self.app.achievement_scan()
        except Exception:                          # noqa: BLE001
            pass
        return self.app.achievement_risk_rows()

    def _fill_achieve(self, rows: list) -> None:
        self._reset(self.ach_view)
        for i, r in enumerate(rows or []):
            self.ach_view.insert(
                "", "end", iid=f"a{i}",
                values=(r.get("name") or r.get("mod", ""),
                        r.get("reason", "")),
                tags=("odd",) if i % 2 else ())
        self.ach_view._rows = rows or []
        self.set_msg(f"{len(rows or [])} 个模组会影响成就")

    def on_disable_achievement(self) -> None:
        """停用选中的成就破坏者（**必须确认**）。"""
        sel = self.ach_view.selection()
        if not sel:
            self.set_msg("先选一个模组")
            return
        rows = getattr(self.ach_view, "_rows", [])
        idx = int(str(sel[0])[1:]) if str(sel[0])[1:].isdigit() else 0
        if idx >= len(rows):
            return
        r = rows[idx]
        key = r.get("key") or r.get("id")
        name = r.get("name") or r.get("mod", "")
        if not key:
            self.set_msg("这条记录没有模组标识")
            return
        if not messagebox.askyesno(
                APP_NAME, f"停用「{name}」？\n\n"
                          "停用后这个存档拿不到成就，但不会删除文件。"):
            return
        try:
            self.app.stage_achievement_risks([key])
            self.app.start_apply_mod_switches()
        except Exception as exc:                  # noqa: BLE001
            self.set_msg(f"停用失败：{exc}")
            return
        self.set_msg("已停用，刷新中…")
        self._wait_task(self.on_scan_achievement)

    def on_scan_checksum(self) -> None:
        """联机校验扫描。"""
        self.set_msg("校验扫描中…")
        if not self.app.start_checksum_scan():
            self.set_msg("校验扫描没能启动")
            return
        self._wait_task(self._after_checksum)

    def _after_checksum(self) -> None:
        try:
            data = self.app.stage_checksum_risks()
        except Exception as exc:                  # noqa: BLE001
            self.set_msg(f"读结果失败：{exc}")
            return
        rows = (data or {}).get("rows") or (data or {}).get("risks") or []
        self._reset(self.multi_view)
        for i, r in enumerate(rows):
            self.multi_view.insert(
                "", "end", iid=f"m{i}",
                values=(r.get("name") or r.get("mod", ""),
                        r.get("reason", "")),
                tags=("odd",) if i % 2 else ())
        self.set_msg(f"{len(rows)} 个模组会影响校验码")

    # ---------------------------------------------------------------- 补丁

    def on_generate_patches(self) -> None:
        """自动生成兼容补丁。"""
        if not messagebox.askyesno(
                APP_NAME, "自动生成兼容补丁？\n\n"
                          "会新建补丁模组覆盖冲突定义，原模组不动。\n"
                          "补丁随时可以删除。"):
            return
        self.set_msg("生成补丁中…")
        self._async(self.app.generate_auto_patches, self._after_gen)

    def _after_gen(self, r: Any) -> None:
        self.set_msg("补丁已生成")
        self.refresh_patches()
        self.refresh_patch_candidates()
        if isinstance(r, dict) and r.get("error"):
            messagebox.showwarning(APP_NAME, str(r["error"]))

    def on_remove_patch(self) -> None:
        """删除选中的补丁。"""
        sel = self.patch_view.selection()
        if not sel:
            return
        rows = getattr(self.patch_view, "_rows", [])
        idx = int(str(sel[0])[1:]) if str(sel[0])[1:].isdigit() else 0
        if idx >= len(rows):
            return
        name = rows[idx].get("name", "")
        if not messagebox.askyesno(APP_NAME, f"删除补丁「{name}」？"):
            return
        try:
            self.app.remove_patches([name])
        except Exception as exc:                  # noqa: BLE001
            self.set_msg(f"删除失败：{exc}")
            return
        self.refresh_patches()

    # ---------------------------------------------------------------- 知识库

    def on_wiki_search(self) -> None:
        """搜索知识库。"""
        q = self.ent_wiki.get().strip()
        if not q:
            self.set_msg("输入关键词")
            return
        self.set_msg("搜索中…")
        self._async(self.app.wiki_search, self._after_wiki, q)

    def _after_wiki(self, r: Any) -> None:
        rows = r if isinstance(r, list) else (r or {}).get("rows") or []
        self.refresh_wiki(rows)

    def on_wiki_select(self, _e=None) -> None:
        """选中一条 → 显示正文。"""
        sel = self.wiki_view.selection()
        if not sel:
            return
        rows = getattr(self, "_wiki_rows", [])
        idx = int(str(sel[0])[1:]) if str(sel[0])[1:].isdigit() else 0
        if idx >= len(rows):
            return
        w = rows[idx]
        key = w.get("key") or w.get("id")
        if not key:
            return
        self._async(self.app.wiki_section, self._after_section, key)

    def _after_section(self, r: Any) -> None:
        txt = r.get("text") if isinstance(r, dict) else r
        if txt:
            self._textwin("知识库", str(txt))

    def on_wiki_ask(self) -> None:
        """向知识库提问（要 AI）。"""
        q = self.ent_wiki.get().strip()
        if not q:
            self.set_msg("先输入问题")
            return
        self.set_msg("提问中…")
        self._async(self.app.wiki_ask, self._after_ask, q)

    def _after_ask(self, r: Any) -> None:
        txt = r.get("answer") if isinstance(r, dict) else r
        self._textwin("知识库 · 回答", str(txt))

    # ---------------------------------------------------------------- AI

    def on_ai_takeover(self) -> None:
        """AI 接管排查。"""
        if not messagebox.askyesno(
                APP_NAME, "让 AI 全面排查？\n\n"
                          "会把模组列表、冲突摘要、日志片段发给你配置的模型。\n"
                          "需要设置里已填 Key 并打开隐私开关。"):
            return
        self.set_msg("AI 排查中…")
        if not self.app.start_ai_takeover():
            self.set_msg("没能启动（检查 Key 和隐私开关）")
            return
        self._wait_task(self._after_ai)

    def _after_ai(self) -> None:
        t = self.app.task
        res = getattr(t, "result", None)
        self._set_text(self.ai_text,
                       json.dumps(res, ensure_ascii=False, indent=2)
                       if isinstance(res, dict) else str(res or "没有结果"))

    def on_ai_order(self) -> None:
        """AI 分析排序。"""
        self.set_msg("AI 分析排序…")
        self._async(self.app.ai_analyze_order, self._after_ai_json)

    def _after_ai_json(self, r: Any) -> None:
        """把 AI 返回的 JSON 摊进文本框。"""
        self._set_text(self.ai_text,
                       json.dumps(r, ensure_ascii=False, indent=2)
                       if isinstance(r, dict) else str(r))

    def on_ai_test(self) -> None:
        """测试 AI 连接。"""
        self.set_msg("测试连接…")
        self._async(self.app.ai_test, self._after_ai_json)

    # ---------------------------------------------------------------- 播放集

    def on_playset_change(self, _e=None) -> None:
        """切换播放集。"""
        i = self.cb_playset.current()
        if i < 0 or i >= len(self._playsets):
            return
        p = self._playsets[i]
        key = p.get("key") or f"{p.get('source')}:{p.get('id')}"
        if not messagebox.askyesno(
                APP_NAME, f"切换到播放集「{p.get('name')}」？\n\n"
                          "这会重新读取启用状态和加载顺序，"
                          "当前的扫描结果会清空。"):
            self.cb_playset.selection_clear()
            return
        self._async(self.app.switch_playset, self._after_switch, key)

    def _after_switch(self, r: Any) -> None:
        """切完播放集：刷新模组表与下拉框。"""
        self.set_msg("已切换播放集")
        self.refresh_mods()
        self.refresh_playsets()

    def on_playset_rename(self) -> None:
        """重命名当前播放集（官方库才支持）。"""
        i = self.cb_playset.current()
        if i < 0 or i >= len(self._playsets):
            return
        p = self._playsets[i]
        if p.get("source") == "irony":
            messagebox.showinfo(
                APP_NAME, "Irony Mod Manager 的播放集不能在这里改名 ——\n"
                          "那要写它自己的数据库，有损坏风险。")
            return
        new = self._ask_text("重命名播放集", "新名称：", p.get("name", ""))
        if not new or new == p.get("name"):
            return
        key = p.get("key") or f"{p.get('source')}:{p.get('id')}"
        self._async(self.app.rename_playset, self._after_switch, key, new)

    # ---------------------------------------------------------------- 备份

    def on_backup(self) -> None:
        """手动备份。"""
        try:
            r = self.app.make_backup()
        except Exception as exc:                  # noqa: BLE001
            self.set_msg(f"备份失败：{exc}")
            return
        self.set_msg(f"已备份：{r}" if r else "已备份")

    def on_rollback(self) -> None:
        """回滚到某个备份。"""
        try:
            rows = self.app.list_backups()
        except Exception as exc:                  # noqa: BLE001
            self.set_msg(f"读备份失败：{exc}")
            return
        if not rows:
            self.set_msg("没有备份")
            return
        names = [r.get("name") if isinstance(r, dict) else str(r)
                 for r in rows]
        pick = self._ask_choice("回滚", "选一个备份：", names)
        if not pick:
            return
        idx = names.index(pick)
        path = rows[idx].get("path") if isinstance(rows[idx], dict) else pick
        if not messagebox.askyesno(
                APP_NAME, f"回滚到「{pick}」？\n\n当前配置会被覆盖。"):
            return
        try:
            r = self.app.rollback(path)
            self.set_msg(f"已回滚：{r}" if r else "已回滚")
        except Exception as exc:                  # noqa: BLE001
            self.set_msg(f"回滚失败：{exc}")

    # ---------------------------------------------------------------- 设置

    def _load_settings(self) -> None:
        """把当前设置填进输入框。

        ``get_settings()`` 返回的是**数据类实例**（``Paths`` / ``AiSettings``），
        不是 dict —— 所以这里按属性读，并用 ``getattr`` 兜底，免得某个
        字段改名就把整个界面打挂。
        """
        try:
            s = self.app.get_settings()
        except Exception:                          # noqa: BLE001
            return

        def g(obj, key: str, default: str = "") -> str:
            """从数据类或 dict 里取一个值，两种都支持。"""
            if obj is None:
                return default
            if isinstance(obj, dict):
                return obj.get(key, default) or default
            return getattr(obj, key, default) or default

        paths = s.get("paths") if isinstance(s, dict) else getattr(
            s, "paths", None)
        ai = s.get("ai") if isinstance(s, dict) else getattr(s, "ai", None)
        self.ent_game.insert(0, g(paths, "game_dir"))
        self.ent_mod.insert(0, g(paths, "mod_dir"))
        self.ent_key.insert(0, g(ai, "api_key"))
        self.ent_model.insert(0, g(ai, "model"))
        self.cb_lang.set(g(s, "language", "zh"))
        self.chk_ai.set(bool(g(ai, "allow_send_mod_content", False)))

    def on_autodetect(self) -> None:
        """自动识别游戏路径。"""
        try:
            r = self.app.autodetect_paths()
        except Exception as exc:                  # noqa: BLE001
            self.set_msg(f"识别失败：{exc}")
            return
        paths = r.get("paths") if isinstance(r, dict) else {}
        paths = paths or {}
        for ent, k in ((self.ent_game, "game_dir"), (self.ent_mod, "mod_dir")):
            v = paths.get(k)
            if v:
                ent.delete(0, "end")
                ent.insert(0, v)
        self.set_msg("路径已更新，记得保存")

    def on_save_settings(self) -> None:
        """保存设置。"""
        patch = {
            "paths": {"game_dir": self.ent_game.get().strip(),
                      "mod_dir": self.ent_mod.get().strip()},
            "language": self.cb_lang.get() or "zh",
            "ai": {"api_key": self.ent_key.get().strip(),
                   "model": self.ent_model.get().strip(),
                   "allow_send": bool(self.chk_ai.get())},
        }
        try:
            self.app.save_settings(patch)
        except Exception as exc:                  # noqa: BLE001
            self.set_msg(f"保存失败：{exc}")
            return
        self.set_msg("设置已保存")

    # ---------------------------------------------------------------- 小工具

    def _set_text(self, widget: tk.Text, body: str) -> None:
        """把文本塞进只读 Text。直接 insert 会被 readonly 挡掉。"""
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", body)
        widget.configure(state="disabled")

    def _textwin(self, title: str, body: str) -> None:
        """只读文本窗。"""
        win = tk.Toplevel(self.root)
        win.title(title[:60])
        win.geometry("760x560")
        win.configure(bg=BG)
        win.columnconfigure(0, weight=1)
        win.rowconfigure(0, weight=1)
        t = tk.Text(win, wrap="word", relief="flat", bg=PANEL, fg=FG,
                    insertbackground=ACC, padx=14, pady=12,
                    selectbackground=ACC, selectforeground=ACCTEXT,
                    font=FONT)
        vs = ttk.Scrollbar(win, orient="vertical", command=t.yview)
        t.configure(yscrollcommand=vs.set)
        t.grid(row=0, column=0, sticky="nsew")
        vs.grid(row=0, column=1, sticky="ns")
        t.insert("1.0", body)
        t.configure(state="disabled")

    def _ask_text(self, title: str, prompt: str, init: str = "") -> str:
        """一个简单的输入对话框。"""
        win = tk.Toplevel(self.root)
        win.title(title)
        win.configure(bg=BG)
        win.transient(self.root)
        win.grab_set()
        ttk.Label(win, text=prompt).pack(padx=14, pady=(14, 4), anchor="w")
        var = tk.StringVar(value=init)
        e = ttk.Entry(win, textvariable=var, width=42)
        e.pack(padx=14, pady=(0, 10))
        e.focus_set()
        e.select_range(0, "end")
        out: dict[str, str] = {"v": ""}

        def ok() -> None:
            out["v"] = var.get().strip()
            win.destroy()

        bf = ttk.Frame(win)
        bf.pack(padx=14, pady=(0, 14), anchor="e")
        ttk.Button(bf, text="确定", command=ok).pack(side="right")
        e.bind("<Return>", lambda _e: ok())
        self.root.wait_window(win)
        return out["v"]

    def _ask_choice(self, title: str, prompt: str,
                    options: list[str]) -> str:
        """从列表里选一个。"""
        win = tk.Toplevel(self.root)
        win.title(title)
        win.configure(bg=BG)
        win.transient(self.root)
        win.grab_set()
        win.geometry("560x420")
        ttk.Label(win, text=prompt).pack(padx=14, pady=(14, 6), anchor="w")
        lb = tk.Listbox(win, bg=PANEL, fg=FG, relief="flat",
                        selectbackground=ACC, selectforeground=ACCTEXT,
                        font=FONT, highlightthickness=0)
        lb.pack(fill="both", expand=True, padx=14)
        for o in options:
            lb.insert("end", o)
        if options:
            lb.selection_set(0)
        out: dict[str, str] = {"v": ""}

        def ok() -> None:
            sel = lb.curselection()
            if sel:
                out["v"] = options[sel[0]]
            win.destroy()

        bf = ttk.Frame(win)
        bf.pack(padx=14, pady=12, anchor="e")
        ttk.Button(bf, text="确定", command=ok).pack(side="right")
        lb.bind("<Double-1>", lambda _e: ok())
        self.root.wait_window(win)
        return out["v"]

    # ================================================================ 运行

    def run(self) -> None:
        """进主循环。

        启动时把**所有**表格都刷一遍 —— 不只是模组页。没数据的表会各自
        落一句占位提示，用户切过去不至于看到一片纯黑以为界面坏了。
        """
        for fn in (self.refresh_mods, self.refresh_playsets,
                   self.refresh_findings, self.refresh_order,
                   self.refresh_crash, self.refresh_logs,
                   self.refresh_patch_candidates, self.refresh_patches):
            try:
                fn()
            except Exception:                      # noqa: BLE001
                pass
        self.root.mainloop()


def run(app) -> None:
    """启动轻量界面。"""
    LightUI(app).run()
