"""并发压力测试：边轮询边狂点开关，抓出只在特定时序出现的错误。

界面上曾出现过 ``RuntimeError: dictionary changed size during iteration``，
但顺序调用复现不出来——说明它是并发引起的。这个脚本模拟真实界面：
后台每 0.6 秒 poll() 一次（和前端循环一致），同时高频调用开关接口。

用法::  python tools\stress_test.py
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
locks = threading.Lock()


def note(msg: str) -> None:
    with locks:
        errors.append(msg)


def parse(raw) -> dict:
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except ValueError:
            return {"error": "bad-json", "raw": raw[:200]}
    return raw if isinstance(raw, dict) else {"error": "bad-type"}


def main() -> int:
    app = App()
    bridge = WebBridge(app)
    app.load_mods()

    st = parse(bridge.getSwitchState())
    rows = st.get("rows") or []
    print(f"模组数: {len(rows)}")
    if not rows:
        print("没有模组，无法测试")
        return 1
    keys = [r["key"] for r in rows if r["actual"]][:12]
    dlc = parse(bridge.getDlcState())
    dlc_keys = [d["key"] for d in (dlc.get("dlcs") or [])][:6]

    stop = threading.Event()
    counts = {"poll": 0, "switch": 0, "list": 0, "dlc": 0, "status": 0}

    def poller():
        cur = "0"
        while not stop.is_set():
            r = parse(bridge.poll(cur))
            if "error" in r:
                note(f"poll: {r['error']}\n{r.get('traceback','')[-600:]}")
            cur = str(r.get("cursor", cur))
            counts["poll"] += 1
            time.sleep(0.05)

    def lister():
        while not stop.is_set():
            for name, fn in (("mods_list", bridge.getMods),
                             ("status", bridge.getStatus)):
                r = parse(fn())
                if "error" in r:
                    note(f"{name}: {r['error']}\n{r.get('traceback','')[-600:]}")
            counts["list"] += 1
            counts["status"] += 1
            time.sleep(0.05)

    def switcher():
        i = 0
        while not stop.is_set():
            k = keys[i % len(keys)]
            on = (i % 2 == 0)
            r = parse(bridge.setModSwitch(k, "true" if on else "false"))
            if "error" in r:
                note(f"setModSwitch: {r['error']}\n{r.get('traceback','')[-600:]}")
            r2 = parse(bridge.getSwitchState())
            if "error" in r2:
                note(f"getSwitchState: {r2['error']}\n{r2.get('traceback','')[-600:]}")
            counts["switch"] += 1
            i += 1

    def dlcer():
        i = 0
        if not dlc_keys:
            return
        while not stop.is_set():
            k = dlc_keys[i % len(dlc_keys)]
            r = parse(bridge.setDlcSwitch(k, "true" if i % 2 else "false"))
            if "error" in r:
                note(f"setDlcSwitch: {r['error']}\n{r.get('traceback','')[-600:]}")
            r2 = parse(bridge.getDlcState())
            if "error" in r2:
                note(f"getDlcState: {r2['error']}\n{r2.get('traceback','')[-600:]}")
            counts["dlc"] += 1
            i += 1

    threads = [threading.Thread(target=f, name=n) for f, n in
               ((poller, "poll"), (lister, "list"),
                (switcher, "switch"), (dlcer, "dlc"))]
    for t in threads:
        t.daemon = True
        t.start()
    print("并发压测 12 秒 …")
    time.sleep(12)
    stop.set()
    for t in threads:
        t.join(timeout=3)

    print("调用次数:", counts)
    print()
    if errors:
        print(f"!! 捕获到 {len(errors)} 个错误：")
        seen = set()
        for e in errors:
            head = e.split("\n", 1)[0]
            if head in seen:
                continue
            seen.add(head)
            print("  -", head)
            print("   ", e.split("\n", 1)[-1][-400:].replace("\n", "\n    "))
        # 打印桥接层记录的完整栈
        log = Path.home() / "AppData" / "Local" / "StellarisModDoctor" / "logs" / "bridge_errors.log"
        if log.is_file():
            print(f"\n桥接错误日志（{log}）最后一段：")
            print(log.read_text(encoding="utf-8")[-1500:])
        return 1
    print("[OK] 未捕获到任何错误")
    # 清理暂存，避免影响后续
    bridge.switchBulk("reset")
    bridge.dlcBulk("reset")
    return 0


if __name__ == "__main__":
    sys.exit(main())
