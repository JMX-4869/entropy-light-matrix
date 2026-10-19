"""复现「扫描进行中，界面同时在轮询」的竞态。

这是真实场景：用户点「开始检测」，任务在后台线程跑；
前端每 600ms 调一次 ``poll()``，而 ``poll()`` 会顺手取一次
``status()``，里面要遍历索引集合。

早期版本把 ``IndexSet`` 先发布再逐个填充，于是轮询线程会撞上
「正在扩容的 dict」，抛
``RuntimeError: dictionary changed size during iteration``。
界面上只显示一句莫名的失败提示，完全看不出原因。

本测试在扫描期间高频轮询，确保不再出现该错误。

用法::  python tools\race_test.py
"""

from __future__ import annotations

import io
import json
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from smd.app import App  # noqa: E402
from smd.webui import WebBridge  # noqa: E402

errors: list[str] = []
lock = threading.Lock()


def parse(raw) -> dict:
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except ValueError:
            return {"error": "bad-json"}
    return raw if isinstance(raw, dict) else {}


def main() -> int:
    app = App()
    bridge = WebBridge(app)

    print("=" * 66)
    print("扫描 / 轮询竞态测试")
    print("=" * 66)

    stop = threading.Event()
    polls = {"n": 0}

    def poller():
        """模拟前端轮询：poll + status + mods 一起打。"""
        cur = "0"
        while not stop.is_set():
            for fn, label in ((lambda: bridge.poll(cur), "poll"),
                              (bridge.getStatus, "getStatus"),
                              (bridge.getMods, "getMods")):
                r = parse(fn())
                if "error" in r:
                    with lock:
                        errors.append(f"{label}: {r['error']}\n"
                                      f"{r.get('traceback','')[-700:]}")
            cur = "999999"
            polls["n"] += 1

    print("\n--- 1. 扫描期间高频轮询 ---")
    t = threading.Thread(target=poller, daemon=True)
    t.start()
    t0 = time.time()
    app.start_scan(semantic=True)
    while app.task.running and time.time() - t0 < 300:
        time.sleep(0.2)
    elapsed = time.time() - t0
    stop.set()
    t.join(timeout=3)

    print(f"  扫描耗时 {elapsed:.1f}s，轮询 {polls['n']} 轮"
          f"（约 {polls['n']*3} 次调用）")
    print(f"  模组 {len(app.library.mods) if app.library else 0} 个，"
          f"索引 {len(app.indexes.indexes) if app.indexes else 0} 个")
    if errors:
        print(f"\n  [!!] 捕获到 {len(errors)} 个错误：")
        seen = set()
        for e in errors:
            head = e.split("\n")[0]
            if head in seen:
                continue
            seen.add(head)
            print("   -", head)
            print("    ", e.split("\n", 1)[-1][:500].replace("\n", "\n     "))
        return 1
    print("  [OK] 扫描期间轮询未出错")

    print("\n--- 2. 索引集合必须是原子发布的 ---")
    # 发布之后，读者看到的集合必须是完整大小。
    #
    # **和「启用的模组」比，不是和「所有有内容的模组」比** ——
    # 从 v1.0.2 起扫描只覆盖当前播放集里启用的那些（用户反馈
    # "我只想排序，怎么把我订阅的 mod 全部一起弄了"）。
    # 所以「索引 74 / 全部 76」是正确的：差的 2 个是没启用的。
    # 这条断言原来写的是 n_idx != n_mods，在只扫启用的版本上会稳定误报
    # （实测连跑两次都是 74 / 76）。
    n_idx = len(app.indexes.indexes)
    n_on = len([m for m in app.library.enabled()
                if m.resolve_content_path()])
    n_mods = len([m for m in app.library.mods.values()
                  if m.resolve_content_path()])
    print(f"  已索引 {n_idx} / 启用且有内容 {n_on}（全部有内容 {n_mods}）")
    if n_idx != n_on:
        print("  [!!] 索引数量与启用模组数量不一致")
        return 1
    if n_idx > n_mods:
        print("  [!!] 索引数超过全部有内容的模组")
        return 1
    print("  [OK] 索引集合完整")

    print("\n--- 3. 反复扫描，确认没有残留 ---")
    for i in range(2):
        app.start_scan(semantic=False)
        end = time.time() + 180
        while app.task.running and time.time() < end:
            parse(bridge.getStatus())
            parse(bridge.getMods())
            time.sleep(0.05)
        if errors:
            print(f"  [!!] 第 {i+2} 次扫描出现错误：{errors[0].splitlines()[0]}")
            return 1
    print("  [OK] 连续 3 次扫描均无错误")

    # 收尾：清掉暂存
    bridge.switchBulk("reset")
    bridge.dlcBulk("reset")

    print()
    print("=" * 66)
    print("结果：全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
