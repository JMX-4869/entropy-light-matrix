"""命令行入口。

用法
----
``python -m smd``                     启动图形界面
``python -m smd --scan``              命令行扫描并打印冲突摘要
``python -m smd --optimize --apply``  命令行一键优化并落地
``python -m smd --report``            生成崩溃报告
``python -m smd --boot-reminder``     显示上次任务的提醒（开机自启调用）
``python -m smd --selftest``          自检：解析/知识库/数据通路
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime

from . import APP_NAME, APP_NAME_EN, __version__


def _setup_console() -> None:
    """Windows 控制台按 UTF-8 输出中文。"""
    try:
        if sys.platform.startswith("win"):
            import ctypes

            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
            ctypes.windll.kernel32.SetConsoleCP(65001)
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def _banner() -> str:
    return (f"{APP_NAME} v{__version__}  ({APP_NAME_EN})\n"
            f"{'=' * 58}")


def _wait_with_progress(app, width: int = 62) -> None:
    """阻塞等待当前任务，只在状态变化时刷新一行进度。"""
    last = ""
    while app.task.running:
        line = f"   {app.task.message[:width]:<{width}} " \
               f"{app.task.progress * 100:5.1f}%"
        if line != last:
            print(f"\r{line}", end="", flush=True)
            last = line
        time.sleep(0.12)
    if last:
        print()


def cmd_scan(args: argparse.Namespace) -> int:
    """``--scan``：命令行扫描并输出冲突摘要。"""
    from .app import App

    app = App()
    print(_banner())
    st = app.status()
    print(f"用户数据：{st['paths']['user_dir']}")
    print(f"游戏目录：{st['paths']['game_dir']}")
    print(f"工坊目录：{', '.join(st['paths']['workshop_dirs']) or '(无)'}")
    if st["path_problems"]:
        print("\n路径问题：")
        for p in st["path_problems"]:
            print(f"  ! {p}")
    print()

    t0 = time.time()
    app.load_mods()
    s = app.library.summary() if app.library else {}
    print(f"模组：共 {s.get('total', 0)} 个，启用 {s.get('enabled', 0)} 个，"
          f"已启用但文件缺失 {s.get('missing_files', 0)} 个，"
          f"过期记录 {s.get('stale_records', 0)} 个")

    app.start_scan()
    _wait_with_progress(app)
    print()
    if app.task.error:
        print(f"\n扫描失败：{app.task.error}")
        return 2

    rep = app.report
    if rep is None:
        print("没有检测结果")
        return 2

    print(f"\n健康分：{rep.health_score()} / 100   耗时 {time.time()-t0:.1f}s")
    print(f"问题总数：{rep.count()}")
    for sev in ("critical", "high", "medium", "low", "info"):
        n = rep.count(sev)
        if n:
            from .conflict import SEV_LABEL
            print(f"   {SEV_LABEL[sev]:<4} {n:>6}")

    print("\n分类统计：")
    for k in app.finding_kinds():
        print(f"   {k['label']:<22} {k['count']:>6}")

    if args.limit:
        print(f"\n最严重的 {args.limit} 条：")
        from .conflict import SEV_LABEL
        for f in rep.findings[: args.limit]:
            print(f"\n[{SEV_LABEL.get(f.severity, f.severity)}] {f.title}")
            print(f"   模组：{'、'.join(f.mod_names[:4])}")
            if f.detail:
                for ln in f.detail.splitlines()[:4]:
                    print(f"   {ln}")
            if f.advice:
                print(f"   → {f.advice.splitlines()[0]}")

    print("\n统计：", json.dumps(rep.stats, ensure_ascii=False))
    return 0


def cmd_optimize(args: argparse.Namespace) -> int:
    """``--optimize``：计算最优顺序；``--apply`` 才真正写入。"""
    from .app import App

    app = App()
    print(_banner())

    def job_progress() -> None:
        while True:
            if app.task.running:
                print(f"\r   {app.task.message[:56]:<56} "
                      f"{app.task.progress * 100:5.1f}%", end="", flush=True)
            elif app.task.finished:
                break
            else:
                time.sleep(0.05)
                continue
            time.sleep(0.25)
        print()

    import threading

    t = threading.Thread(target=job_progress, daemon=True)
    t.start()
    app.one_click_optimize(apply=False)
    # 等任务真正结束，而不是固定等 1 秒
    while app.task.running:
        time.sleep(0.1)
    t.join(timeout=2)

    if app.task.error:
        print(f"\n失败：{app.task.error}")
        return 2

    sol = app.solution
    if sol is None:
        print("没有方案")
        return 2

    print(f"\n健康分：{sol.score_before} → 预计 {sol.score_after}")
    print(f"需要调整顺序：{sum(1 for p in sol.plans if p.action == 'reorder')} 项")
    print(f"兼容补丁：{len(sol.patches)} 份")
    dis = [p for p in sol.plans if p.action == "disable"]
    if dis:
        print(f"\n建议关闭的模组（{len(dis)} 个，**不会自动关闭**）：")
        for p in dis:
            mark = "×" if p.approved else " "
            print(f"   [{mark}] {p.name}")
            print(f"        理由：{p.reason}")
        if args.disable:
            print(f"\n   --disable 已指定：以上 {len(dis)} 个将在应用时被关闭")
        else:
            print("\n   要关闭它们，请加 --disable；"
                  "否则应用时它们保持启用。")

    if sol.patches:
        print("\n建议生成的补丁：")
        for p in sol.patches:
            print(f"   + {p.name}（{len(p.files)} 个文件，{p.mode}）")
            print(f"     {p.description.splitlines()[0]}")

    if args.show_order:
        print("\n新加载顺序（数字越小越先加载）：")
        for i, p in enumerate(sol.plans):
            if p.action == "disable":
                continue
            flag = {"keep": " ", "reorder": "*", "enable": "+"}.get(p.action, " ")
            print(f"   {i:3d} {flag} {p.name}")

    if args.apply:
        # --disable 表示用户明确同意关闭建议列表里的模组
        if args.disable:
            n = app.solution.decide_all(True) if app.solution else 0
            print(f"\n已确认关闭 {n} 个模组（--disable）")
        print("\n正在应用…")
        res = app.apply_current_solution()
        print(f"结果：{'成功' if res.get('ok') else '有错误'}")
        for a in res.get("actions", []):
            mark = "v" if a["ok"] else "x"
            print(f"   [{mark}] {a['description']}"
                  + (f" — {a['detail']}" if a.get("detail") else ""))
        if res.get("errors"):
            print("\n错误：")
            for e in res["errors"]:
                print(f"   ! {e}")
        print(f"\n备份位于：{res.get('backup_dir')}")
    else:
        print("\n（未应用。加 --apply 真正写入；"
              "如需关闭建议中的模组，再加 --disable）")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    """``--report``：生成崩溃分析报告。"""
    from .app import App

    app = App()
    print(_banner())
    app.load_mods()
    if args.with_conflicts:
        app.start_scan()
        while app.task.running:
            time.sleep(0.2)
    crash = args.crash or ""
    if not crash and app.library:
        dirs = app.crash_dirs()
        if dirs:
            crash = dirs[0]["path"]
            print(f"使用最近的崩溃：{dirs[0]['name']}")
    rep = app.generate_report(crash)
    print(f"\n结论：{rep['conclusion']}")
    print("\n可能原因：")
    for i, c in enumerate(rep["causes"], 1):
        print(f"  {i}. {c['title']}")
    a = rep.get("analysis") or {}
    print(f"\n分析了 {a.get('total_lines', 0):,} 行日志")
    sc = a.get("severity_counts", {})
    print(f"  致命 {sc.get('fatal', 0)} / 错误 {sc.get('error', 0)}"
          f" / 警告 {sc.get('warn', 0)} / 噪音 {sc.get('noise', 0)}")
    print(f"\nMarkdown：{rep.get('md_path')}")
    print(f"HTML　　：{rep.get('html_path')}")
    return 0


def cmd_boot_reminder(args: argparse.Namespace) -> int:
    """开机提醒：由 RunOnce 调用，也用于手动查看。"""
    _setup_console()
    from .system import load_reminder, open_url

    rem = load_reminder()
    if rem is None:
        if args.verbose:
            print("没有待展示的提醒。")
        return 0

    print("=" * 62)
    print(f"  {rem.title}")
    print("=" * 62)
    print()
    print(rem.summary_text())
    print()
    print("=" * 62)

    # 打开报告（HTML 排版最好看）
    opened = False
    target = rem.report_html or rem.report_md
    if target and os.path.isfile(target) and not args.no_open:
        ok, msg = open_url(target)
        opened = ok
        print(f"[已打开报告] {msg}")

    # 原生弹窗：打包后程序没有控制台，这是用户唯一能看到的界面
    shown = False
    try:
        import ctypes

        body = rem.summary_text()
        if rem.report_md:
            body += f"\n\n报告文件：\n{rem.report_md}"
        if not opened and target:
            body += "\n\n（报告未能自动打开，请手动查看上面的路径）"
        ctypes.windll.user32.MessageBoxW(
            None, body, f"{APP_NAME} — 上次任务已完成", 0x40)
        shown = True
    except Exception:
        pass

    if not shown:
        try:
            input("按回车关闭…")
        except (EOFError, KeyboardInterrupt, OSError):
            pass
    return 0


def cmd_selftest(args: argparse.Namespace) -> int:
    """自检：不依赖群星安装，验证核心算法与知识库。"""
    _setup_console()
    print(_banner())
    ok = True

    # 1) 解析器
    from .paradox import canonical_text, flatten_definitions, parse_string
    src = 'a = { b = 1 c = { d = yes } }\nlist = { "x" "y" }\nt = { k > 3 }'
    root = parse_string(src)
    defs = flatten_definitions(root)
    checks = [
        ("解析顶层定义", set(defs) == {"a", "list", "t"}),
        ("嵌套块", list(defs["a"].get("c").keys()) == ["d"]),
        ("列表元素", len(root.get("list").get_all("")) == 2),
        ("运算符", defs["t"].get("k").op == ">"),
        ("容错", parse_string("x = { y = 1 ").get("x") is not None),
        ("规范化", len(canonical_text(defs["a"])) > 5),
    ]
    for name, good in checks:
        print(f"  [{'OK' if good else '!!'}] 解析器：{name}")
        ok = ok and good

    # 2) 知识库
    from .logkb import SEV_ERROR, SEV_FATAL, annotate, parse_line
    cases = [
        ("[04:30:42][dlc.cpp:1840]: Could not find files for mod: "
         "E:\\steam\\x\\1121692237", SEV_ERROR),
        ("[04:30:42][trigger.cpp:635]: Wrong scope for trigger "
         "'has_starbase_size' at  common/x.txt", SEV_ERROR),
        ("[04:30:42][db.cpp:1]: my_thing is already defined", SEV_ERROR),
        ("[04:30:42][x.cpp:9]: Failed to read key reference abc from "
         "database  file: common/y.txt", "warn"),
    ]
    for raw, want in cases:
        ll = parse_line(raw)
        good = ll.severity == want
        print(f"  [{'OK' if good else '!!'}] 知识库："
              f"{ll.entry.title_zh if ll.entry else '(无匹配)'} "
              f"[{ll.severity}]")
        ok = ok and good

    # 3) 路径探测
    from .config import Settings
    s = Settings.load()
    print(f"  [{'OK' if s.paths.user_dir else '!!'}] 路径探测："
          f"{s.paths.user_dir or '未找到'}")
    if s.paths.game_dir:
        print(f"      游戏目录：{s.paths.game_dir}")
    if s.paths.workshop_dirs:
        print(f"      工坊目录：{len(s.paths.workshop_dirs)} 个")

    # 4) 日志解析（若有真实日志）
    from .paths import is_file
    lg = s.paths.logs_dir / "error.log"
    if is_file(lg):
        from .logkb import analyze_log
        import os as _os
        t0 = time.time()
        an = analyze_log(lg, max_lines=60000)
        df = time.time() - t0
        print(f"  [OK] 日志分析：{an.parsed_lines:,} 行，"
              f"{an.size_bytes/1048576:.1f} MB，{df:.1f}s")
        print(f"      规则命中 {len(an.groups)} 种，"
              f"致命 {an.severity_counts.get('fatal',0)}、"
              f"错误 {an.severity_counts.get('error',0)}、"
              f"警告 {an.severity_counts.get('warn',0)}、"
              f"噪音 {an.severity_counts.get('noise',0)}")
        top = an.sorted_groups()[:4]
        for g in top:
            print(f"      · [{g['severity_label']}] {g['title']} × {g['count']}")
    else:
        print("  [--] 没有 error.log，跳过日志分析测试")

    print()
    print("自检结果：" + ("全部通过" if ok else "存在失败项"))
    return 0 if ok else 1


def cmd_gui(args: argparse.Namespace) -> int:
    """``--gui``（默认）：启动图形界面。

    两套界面共用同一个后端（``smd.app.App``）：

    * 网页版（默认）—— pywebview + Edge WebView2，功能完整，内存 550 MB 级
    * 轻量版（``--light``）—— Tkinter，只做核心链路，内存 30 MB 级

    轻量版不是"阉割"，是给低配机/只要排序的人准备的另一条路。
    """
    if getattr(args, "light", False):
        return run_light_gui()
    from .ui import run_gui
    return run_gui(debug=args.debug, port=args.port,
                   page=getattr(args, "page", ""))


def run_light_gui() -> int:
    """启动轻量界面（Tkinter，不加载 WebView2）。"""
    from .app import App
    from .ui_light import run as run_light
    app = App()
    try:
        app.load_mods()
    except Exception:                            # noqa: BLE001
        pass
    run_light(app)
    return 0


def build_parser() -> argparse.ArgumentParser:
    """构建命令行参数解析器。"""
    p = argparse.ArgumentParser(
        prog="smd",
        description=f"{APP_NAME} —— 群星模组冲突检测与智能排序启动器",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--version", action="version",
                   version=f"{APP_NAME} {__version__}")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--scan", action="store_true", help="命令行扫描并输出冲突摘要")
    g.add_argument("--optimize", action="store_true", help="计算最优加载顺序")
    g.add_argument("--report", action="store_true", help="生成崩溃分析报告")
    g.add_argument("--selftest", action="store_true", help="运行自检")
    g.add_argument("--archive", action="store_true",
                   help="检查「时之虫档案」数据是否随程序正确打包")
    g.add_argument("--boot-reminder", action="store_true",
                   help="显示上次任务的提醒（开机自启使用）")

    p.add_argument("--apply", action="store_true",
                   help="与 --optimize 合用：真正写入 playset 与 dlc_load.json")
    p.add_argument("--disable", action="store_true",
                   help="与 --optimize --apply 合用：确认关闭建议中的模组。"
                        "不加此项时它们保持启用")
    p.add_argument("--show-order", action="store_true",
                   help="与 --optimize 合用：打印完整新顺序")
    p.add_argument("--limit", type=int, default=25, help="打印的问题条数上限")
    p.add_argument("--crash", default="", help="指定崩溃目录")
    p.add_argument("--with-conflicts", action="store_true",
                   help="与 --report 合用：先跑一次冲突检测")
    p.add_argument("--no-open", action="store_true",
                   help="与 --boot-reminder 合用：不自动打开报告")
    p.add_argument("--verbose", action="store_true")
    p.add_argument("--debug", action="store_true", help="GUI 调试模式")
    p.add_argument("--light", action="store_true",
                   help="用轻量界面（Tkinter，内存约 30 MB，"
                        "只含扫描/冲突/排序/应用）")
    p.add_argument("--port", type=int, default=0,
                   help="GUI 固定端口（0 = 自动）")
    p.add_argument("--page", default="",
                   help="启动时直接打开指定页面："
                        "dash / mods / dlc / conflict / solution / order / "
                        "ai / patch / monitor / crash / logs / appearance / "
                        "settings")
    return p


def main(argv: list[str] | None = None) -> int:
    """命令行总入口，按参数分发到各子命令。"""
    _setup_console()
    args = build_parser().parse_args(argv)
    if args.scan:
        return cmd_scan(args)
    if args.optimize:
        return cmd_optimize(args)
    if args.report:
        return cmd_report(args)
    if args.selftest:
        return cmd_selftest(args)
    if args.archive:
        return cmd_archive(args)
    if args.boot_reminder:
        return cmd_boot_reminder(args)
    return cmd_gui(args)


def cmd_archive(args) -> int:
    """检查「时之虫档案」数据是否可用。

    存在的意义：档案数据是**打包进 exe 的静态文件**，
    漏进包里的表现是界面上一片空白，而开发模式看不出问题
    （它会直接读磁盘）。所以留一条命令，装完之后能立刻验证。
    """
    from . import wiki
    m = wiki.meta()
    n = int(m.get("entries") or 0)
    print("时之虫档案")
    print(f"  来源　　：{m.get('source') or '(未找到数据文件)'}")
    print(f"  适配版本：{m.get('version') or '-'}")
    print(f"  条目总数：{n}")
    print(f"  分类数　：{m.get('sections') or 0}")
    if n:
        for c in wiki.categories():
            print(f"    {c['title']:<22}{c['count']:>6}")
        print(f"  可送进游戏的控制台命令：{len(wiki.console_commands())} 条")
        hits = wiki.search("energy")
        print(f"  检索自测：'energy' 命中 {hits['total']} 条")
        print("\n结果：档案数据完整")
        return 0
    print("\n结果：**档案数据缺失**——打包时漏了 smd/data/wiki.json")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
