"""Isolated checksum detection, staging, apply and rollback regression checks.

覆盖点：规则匹配的目录边界/递归/大小写、replace_path 命中、
不完整 manifest 的拒绝、取消语义、暂存不改盘、写入后回滚。
"""
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from smd.app import App                       # noqa: E402
from smd.apply import rollback                # noqa: E402
from smd.checksum import ChecksumRule, scan_checksum  # noqa: E402
from smd.config import Paths, Settings        # noqa: E402
from smd.modinfo import Mod, ModLibrary       # noqa: E402
from smd.webui import WebBridge               # noqa: E402

_results = []


def check(name, ok, detail=""):
    """按项目约定输出，方便全量回归脚本统一统计。

    本项目其它测试都打印「结果：N/M 通过」，回归脚本按那个格式抓数。
    这个测试原先只打印一行 PASS，会被统计脚本当成"无法解析"，
    于是**新增功能在总账里不可见**。
    """
    _results.append((name, ok, detail))
    print(f"  [{'OK' if ok else '!!'}] {name}" + (f" — {detail}" if detail else ""))


def main():
    """Use only temporary game/config files, never the user's live playset."""
    with tempfile.TemporaryDirectory(prefix="smd_checksum_") as temp:
        root = Path(temp)
        game = root / "game"
        game.mkdir()
        manifest = game / "checksum_manifest.txt"
        manifest.write_text('directory\nname = common\nsub_directories = yes\nfile_extension = .txt\n')
        user = root / "user"
        (user / "mod").mkdir(parents=True)
        mods = []
        for key, relative, extra in [
            ("music", "music/song.ogg", ""),
            ("mixed", "common/defines/change.txt", ""),
            ("translation", "localisation/test.yml", ""),
            ("replace", "gfx/image.dds", 'replace_path="common"'),
            ("off", "common/test.txt", ""),
            ("archive", "music/a.ogg", 'archive="test.zip"'),
        ]:
            content = root / key
            file = content / relative
            file.parent.mkdir(parents=True)
            file.write_text("test")
            descriptor = user / "mod" / f"{key}.mod"
            descriptor.write_text(f'name="{key}"\npath="{content.as_posix()}"\n{extra}')
            mods.append(Mod(key=key, name=key, path=str(content), mod_file=str(descriptor),
                            game_registry_id=f"mod/{key}.mod", enabled_in_playset=key != "off"))
        report = scan_checksum(mods, str(game))
        check("六种模组的状态判定正确",
              [r["status"] for r in report["rows"]]
              == ["clear", "risk", "clear", "risk", "risk", "unknown"],
              str([r["status"] for r in report["rows"]]))
        check("命中文件被列为依据",
              report["rows"][1]["evidence"] == ["common/defines/change.txt"],
              str(report["rows"][1]["evidence"]))
        check("目录边界不误匹配（common 不该匹配 common_extra）",
              not ChecksumRule("common", True, ".txt").matches("common_extra/a.txt"))
        check("非递归规则不匹配子目录",
              not ChecksumRule("common", False, ".txt").matches("common/defines/a.txt"))
        check("路径匹配不区分大小写",
              ChecksumRule("common", True, ".txt").matches("COMMON/A.TXT"))
        check("取消语义：返回 cancelled 且不给半截结果",
              scan_checksum(mods, str(game), should_stop=lambda: True)["cancelled"])
        check("没有 manifest 时全部标为未知",
              all(r["status"] == "unknown" for r in scan_checksum(mods, "")["rows"]))
        manifest.write_text('directory\nname = common\nunknown = yes')
        check("不认识的 manifest 被拒绝而非猜测",
              scan_checksum(mods, str(game))["issue"] == "manifest_invalid")

        settings = Settings(paths=Paths(user_dir=str(user), game_dir=str(game)))
        with patch.dict(os.environ, {"LOCALAPPDATA": temp}), patch.object(Settings, "load", return_value=settings):
            app = App()
            app.library = ModLibrary(mods={m.key: m for m in mods}, paths=settings.paths)
            bridge = WebBridge(app)
            check("没排查过时拒绝暂存",
                  not json.loads(bridge.stageChecksumRisks())["ok"])
            app._checksum_report = report
            before = json.dumps({"enabled_mods": [m.game_registry_id for m in mods if m.enabled_in_playset],
                                 "disabled_dlcs": ["keep-me"]}).encode()
            settings.paths.dlc_load.write_bytes(before)
            staged = json.loads(bridge.stageChecksumRisks())
            check("暂存只挑已启用的嫌疑项", staged["count"] == 2, str(staged.get("count")))
            check("暂存本身不改磁盘（关键约束）",
                  settings.paths.dlc_load.read_bytes() == before)
            check("暂存的是命中的那两个",
                  set(app._switch_plan.desired) == {"mixed", "replace"},
                  str(set(app._switch_plan.desired)))
            app.switch_bulk("reset")
            check("可以放弃全部改动",
                  app.preview_mod_switches()["to_disable"] == 0)
            app.stage_checksum_risks()
            with patch("smd.apply.launcher_running", return_value=[]):
                result = app.apply_mod_switches(sync_launcher=False)
            check("写入成功", result["ok"], str(result)[:60])
            saved = json.loads(settings.paths.dlc_load.read_text())
            check("未命中的模组保持启用",
                  "mod/music.mod" in saved["enabled_mods"])
            check("命中的模组被关闭",
                  "mod/mixed.mod" not in saved["enabled_mods"])
            check("无关字段原样保留（disabled_dlcs）",
                  saved["disabled_dlcs"] == ["keep-me"])
            rollback(result["backup"], settings.paths)
            check("回滚后字节级还原",
                  settings.paths.dlc_load.read_bytes() == before)

    passed = sum(1 for _, ok, _ in _results if ok)
    total = len(_results)
    print()
    print("=" * 70)
    print(f"结果：{passed}/{total} 通过")
    for n, ok, d in _results:
        if not ok:
            print(f"  失败：{n} — {d}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
