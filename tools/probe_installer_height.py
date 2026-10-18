"""量安装器 GUI 的"内容最小需要高度"，和窗口高度比。

为什么不用 `tk scaling` 模拟 DPI：那样改完 `winfo_rooty` 的坐标系会错乱
（实测出现"按钮下沿余量 399"这种不可能的值）。**布局空间不够**这件事
不需要模拟 DPI 也能量：

    frm.winfo_reqheight()  = 所有子控件的请求高度之和（+padding）

这就是"内容最少要占多少像素"。它 > 窗口高度 → 必然溢出，
而且溢出量越大，在字体更高/DPI 更大的机器上越早崩。

Tk 的 pack 分配规则：`expand=True` 的控件先按 reqheight 要空间，
不够就从**最后一个** pack 的控件开始往下压。所以按钮放在日志框之后
pack 时，是最先被牺牲的那个。
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "installer"))
out = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import tkinter as tk                       # noqa: E402
from tkinter import ttk                    # noqa: E402

import install as inst                     # noqa: E402

BS = chr(92)
TARGET = ("实际安装到：C:" + BS + "Users" + BS + "x" + BS + "AppData" + BS
          + "Local" + BS + "Programs" + BS + "StellarisModDoctor")


def build(*, bottom: bool, log_height: int = 8):
    root = tk.Tk()
    root.title("req-height probe")
    frm = ttk.Frame(root, padding=18)
    frm.pack(fill="both", expand=True)

    ttk.Label(frm, text=inst.APP_NAME,
              font=("Microsoft YaHei UI", 16, "bold")).pack(anchor="w")
    ttk.Label(frm, text="群星模组冲突检测与智能排序启动器",
              foreground="#666").pack(anchor="w", pady=(0, 14))
    ttk.Label(frm, text="安装位置（会在该位置下创建程序文件夹）"
              ).pack(anchor="w")
    row = ttk.Frame(frm)
    row.pack(fill="x", pady=(2, 4))
    ttk.Entry(row).pack(side="left", fill="x", expand=True)
    ttk.Button(row, text="浏览…").pack(side="left", padx=(6, 0))
    ttk.Label(frm, text=TARGET, foreground="#0a6"
              ).pack(anchor="w", pady=(0, 12))
    for t in ("创建桌面快捷方式", "创建开始菜单快捷方式",
              "安装完成后立即启动"):
        ttk.Checkbutton(frm, text=t).pack(anchor="w")

    logbox = tk.Text(frm, height=log_height, wrap="word", relief="solid",
                     borderwidth=1)
    btnf = ttk.Frame(frm)
    if bottom:
        btnf.pack(side="bottom", fill="x", pady=(12, 0))
        logbox.pack(fill="both", expand=True)
    else:
        logbox.pack(fill="both", expand=True)
        btnf.pack(fill="x", pady=(12, 0))
    ttk.Button(btnf, text="安装").pack(side="right", padx=(0, 8))
    ttk.Button(btnf, text="取消").pack(side="right")

    root.update_idletasks()
    need = frm.winfo_reqheight()
    log_req = logbox.winfo_reqheight()
    btn_req = btnf.winfo_reqheight()
    root.destroy()
    return need, log_req, btn_req


def main() -> int:
    # 内容里只有日志框是"可压缩"的；其余是固定高度。
    # 窗口高度 - 固定部分 = 日志框能拿到的空间。
    print("=" * 70, file=out)
    print("安装器 GUI：内容最少需要多高", file=out)
    print("=" * 70, file=out)

    old_need, log_req, btn_req = build(bottom=False)
    new_need, _, _ = build(bottom=True)
    print(f"  日志框请求高度        : {log_req} px", file=out)
    print(f"  按钮行请求高度        : {btn_req} px", file=out)
    print(f"  旧布局内容总高（顺序 pack） : {old_need} px", file=out)
    print(f"  新布局内容总高（bottom）    : {new_need} px", file=out)
    print("", file=out)

    # 旧窗口 560x400：客户区约 400 - 标题栏？Tk 的 geometry 指的是客户区
    for label, win_h in (("旧窗口 560x400", 400), ("新窗口 600x520", 520)):
        fixed = old_need - log_req          # 除日志框外的固定部分
        room = win_h - fixed                # 日志框实际能拿到多少
        shortfall = log_req - room
        print(f"  [{label}] 高 {win_h}", file=out)
        print(f"      除日志框外固定占 {fixed} px → 日志框能拿 {room} px", file=out)
        if shortfall > 0:
            print(f"      ！日志框被压缩 {shortfall} px，"
                  f"压到底还要再溢出 {shortfall - log_req + (fixed + 0)} px",
                  file=out)
        else:
            print(f"      日志框够用（富余 {-shortfall} px）", file=out)
        print(f"      内容总高 {old_need} vs 窗口 {win_h} → "
              f"{'溢出 ' + str(old_need - win_h) + ' px' if old_need > win_h else '放得下'}",
              file=out)
        print("", file=out)

    over_old = old_need - 400
    print(f"  旧版内容比窗口高 {over_old} px → 靠压缩日志框吸收", file=out)
    print(f"  日志框只有 {log_req} px 可压 → "
          f"{'压完还差 ' + str(over_old - log_req) + ' px，按钮必被推出窗口' if over_old > log_req else '刚好压完，按钮还在（余量 ' + str(log_req - over_old) + ' px）'}",
          file=out)
    print("", file=out)
    print(f"  新版内容 {new_need} px，窗口 520 px → "
          f"{'放得下，按钮恒在底部' if new_need <= 520 else '仍不够'}", file=out)
    out.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
