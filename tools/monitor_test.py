"""监控与崩溃流水线的集成测试。

不启动真实群星，而是用一个冒名进程模拟 ``stellaris.exe``，
再往 ``crashes/`` 里投放一个仿造的崩溃目录，验证完整的自动化链路：

    检测到游戏启动 → 增量解析 error.log → 检测崩溃目录
    → 归因 → 生成中文报告 → 写入开机提醒

用法::

    python tools/monitor_test.py

注意：测试结束后会删除自己投放的崩溃目录（只删自己创建的）。
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from smd.config import Settings  # noqa: E402
from smd.monitor import GameMonitor, MonitorEvent  # noqa: E402
from smd.report import generate_crash_report  # noqa: E402

FAKE = ROOT / "tools" / "_fakegame"
MARKER = "SMD_TEST_CRASH_"

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"  [{'OK' if ok else '!!'}] {name}" + (f" — {detail}" if detail else ""))


def make_fake_game() -> Path | None:
    """把一个 powershell 副本命名成 stellaris.exe。"""
    src = Path(os.environ.get("SystemRoot", r"C:\Windows")) / \
        "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    if not src.is_file():
        return None
    FAKE.mkdir(parents=True, exist_ok=True)
    dst = FAKE / "stellaris.exe"
    if not dst.is_file():
        shutil.copy2(src, dst)
    return dst


def make_fake_crash(crashes_dir: Path) -> Path:
    """造一个仿真的崩溃目录，内容参考真实崩溃的结构。"""
    name = f"{MARKER}{datetime.now():%Y%m%d_%H%M%S}"
    d = crashes_dir / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "exception.txt").write_text(
        "Application: Stellaris \n"
        "Version: 4.4.6 \n"
        f"Date/Time: {datetime.now():%Y-%m-%d %H:%M:%S}\n\n"
        "Unhandled Exception C0000005 (EXCEPTION_ACCESS_VIOLATION) "
        "at address 0x00007FF71E77F71B\n\n"
        "Stack Trace: \n"
        "  1                             PHYSFS_swapSLE64 (+ 11996251)\n"
        "  2                             PHYSFS_swapSLE64 (+ 12136213)\n"
        "  3                             PHYSFS_swapSLE64 (+ 9905601)\n"
        "  4                             PHYSFS_writeUBE64 (+ 5095826)\n"
        "  5                             BaseThreadInitThunk (+ 23)\n"
        "  6                             RtlUserThreadStart (+ 44)\n",
        encoding="utf-8")
    (d / "meta.yml").write_text(
        "# Crash Information\n"
        "AppName: Stellaris \n"
        "AppVersion: 4.4.6 \n"
        "DateTime: \"2026-01-01 00:00:00 +0800\" \n"
        "Platform: Windows \n"
        "SystemMemory: 32768 \n"
        "Mod_1: Test Mod Alpha \n"
        "Mod_2: Test Mod Beta \n"
        "ChecksumStatus: Modified \n",
        encoding="utf-8")
    (d / "error.log").write_text(
        "[04:30:42][dlc.cpp:1840]: Could not find files for mod: "
        "E:\\steam\\x\\9999999999\n"
        "[04:30:44][localization_reader.cpp:223]: Expected quotation mark "
        "(\") at end of line 77 in localisation/simp_chinese/Test/"
        "test_l_simp_chinese.yml\n"
        "[04:31:50][persistent.cpp:41]: Error: \"Malformed token: 0, "
        "near line: 9\n"
        "[04:33:01][eventmanager.cpp:489]: Corrupt Event Table Entry - } "
        "in events/test_events.txtline: 189\n"
        "[04:33:01][eventmanager.cpp:489]: Corrupt Event Table Entry - = "
        "in events/test_events.txtline: 190\n"
        "[04:35:00][trigger.cpp:635]: Wrong scope for trigger "
        "'has_planet_flag' at  file: common/technology/test_tech.txt line: 971\n"
        "[04:36:00][db.cpp:1]: my_shared_thing is already defined\n"
        "[04:37:00][parser_deferred_database_objects.cpp:84]: Failed to "
        "deferred read key reference pc_test_star from database  "
        "file: common/scripted_triggers/test_triggers.txt\n"
        "[04:38:00][gfx.cpp:1]: Failed to load texture: gfx/interface/nope.dds\n"
        "[04:39:00][mem.cpp:1]: Failed to allocate 12345678 bytes\n",
        encoding="utf-8")
    return d


def main() -> int:
    s = Settings.load()
    paths = s.paths
    print("=" * 60)
    print("监控 / 崩溃流水线集成测试")
    print("=" * 60)
    print(f"用户数据：{paths.user_dir}")
    print(f"崩溃目录：{paths.crashes_dir}")
    print()

    crashes_dir = Path(paths.crashes_dir)
    if not crashes_dir.is_dir():
        print("找不到崩溃目录，无法测试")
        return 2

    fake = make_fake_game()
    check("准备冒名进程", fake is not None, str(fake) if fake else "找不到 powershell")

    events: list[MonitorEvent] = []
    lock = threading.Lock()

    def on_event(ev: MonitorEvent) -> None:
        with lock:
            events.append(ev)
        print(f"    事件 [{ev.severity}] {ev.title}")

    mon = GameMonitor(paths, s, on_event=on_event)
    mon.start()
    check("监控线程已启动", mon.running)

    # 记录测试前的偏移，避免把旧日志内容算进本次
    before = set(mon._known_crash_dirs)

    proc = None
    if fake is not None:
        print("\n--- 启动冒名游戏进程 ---")
        proc = subprocess.Popen(
            [str(fake), "-NoProfile", "-Command",
             "Start-Sleep -Seconds 25"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # 等监控发现它（轮询间隔内 + 余量）
        found = False
        for _ in range(30):
            time.sleep(1.0)
            if mon.game_seen:
                found = True
                break
        check("检测到游戏进程启动", found,
              f"峰值内存记录 {mon.peak_memory_mb} MB")

    print("\n--- 投放仿造崩溃目录 ---")
    crash = make_fake_crash(crashes_dir)
    detected = False
    for _ in range(30):
        time.sleep(1.0)
        if mon.crash_detected:
            detected = True
            break
    check("自动检测到崩溃", detected, crash.name)
    check("记录了崩溃路径", mon.last_crash_dir == str(crash),
          mon.last_crash_dir)

    crash_ev = next((e for e in events if e.kind == "game_crashed"), None)
    check("推送了 crash 事件", crash_ev is not None)
    if crash_ev:
        check("事件含异常类型",
              "ACCESS_VIOLATION" in json.dumps(crash_ev.data, ensure_ascii=False),
              str(crash_ev.data.get("exception", "")))

    print("\n--- 生成崩溃报告 ---")
    rep = generate_crash_report(paths, str(crash))
    check("报告结论非空", bool(rep.conclusion), rep.conclusion[:70])
    check("读取到异常码", rep.exception.get("code") == "C0000005",
          f"{rep.exception.get('code')} {rep.exception.get('exception')}")
    check("识别出 2 个模组", len(rep.mods_in_session) == 2,
          str(rep.mods_in_session))
    check("生成了 Markdown", bool(rep.md_path) and Path(rep.md_path).is_file(),
          rep.md_path)
    check("生成了 HTML", bool(rep.html_path) and Path(rep.html_path).is_file(),
          rep.html_path)

    an = rep.analysis
    check("分析了日志", an is not None and an.parsed_lines >= 10,
          f"{an.parsed_lines if an else 0} 行")
    if an:
        sev = an.severity_counts
        check("识别出错误级问题", sev.get("error", 0) >= 3,
              f"致命{sev.get('fatal',0)} 错误{sev.get('error',0)} "
              f"警告{sev.get('warn',0)} 噪音{sev.get('noise',0)}")
        titles = " | ".join(g["title"] for g in an.sorted_groups())
        check("事件表损坏已翻译", "事件定义表损坏" in titles)
        check("模组文件缺失已翻译", "模组文件缺失" in titles)
        check("内存分配失败已翻译", "内存分配失败" in titles)
        check("贴图加载失败已翻译", "贴图" in titles)

    if rep.md_path and Path(rep.md_path).is_file():
        body = Path(rep.md_path).read_text(encoding="utf-8")
        check("报告中含中文注释", "这句话在说什么" in body and "怎么办" in body)
        check("报告中含调用栈解读", "PhysFS" in body)
        check("报告中含模组清单", "Test Mod Alpha" in body)

    print("\n--- 清理 ---")
    if proc is not None:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
    # 等监控观察到退出
    for _ in range(20):
        time.sleep(0.5)
        if not mon.game_seen:
            break
    mon.stop()

    shutil.rmtree(crash, ignore_errors=True)
    check("已清理测试崩溃目录", not crash.exists(), crash.name)
    for p in (rep.md_path, rep.html_path):
        if p and Path(p).is_file():
            try:
                Path(p).unlink()
            except OSError:
                pass
    # 删掉对应的 json
    if rep.md_path:
        j = Path(rep.md_path).with_suffix(".json")
        if j.is_file():
            try:
                j.unlink()
            except OSError:
                pass
    shutil.rmtree(FAKE, ignore_errors=True)

    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print()
    print("=" * 60)
    print(f"结果：{passed}/{total} 通过")
    for n, ok, d in results:
        if not ok:
            print(f"  失败：{n} — {d}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
