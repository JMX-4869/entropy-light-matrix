"""测试用的配置沙箱。

**为什么需要**：界面测试会点击真实按钮，而像「手动填写模型」这种操作
是**会写盘的**（它把模型名存进配置）。测试跑完，用户自己的 AI 配置
就被测试值覆盖了——这已经真实发生过一次（用户的 ollama 配置被改成
deepseek + 测试模型名）。

**做法**：在导入 smd 之前把 ``LOCALAPPDATA`` 指到一个临时目录，
于是 ``config.json``、日志、备份全部落在沙箱里，真实配置一个字节都不动。
同时把用户现有配置**拷进沙箱**，这样测试跑的还是他熟悉的设置，
不会因为默认值不同而误报。

用法（必须在 import smd 之前）::

    from sandbox import enter_sandbox
    enter_sandbox()
    import smd.app  # noqa: E402
"""

from __future__ import annotations

import atexit
import json
import os
import shutil
import tempfile
from pathlib import Path

_ACTIVE: list[Path] = []


def enter_sandbox(keep_real_config: bool = True,
                  game_dir: bool = False) -> Path:
    """把数据目录切到临时沙箱，返回沙箱路径。

    ``keep_real_config`` 为真时会把用户现有的 ``config.json`` 复制进去，
    保证测试环境与用户实际环境一致（例如主题、语言、AI 供应商）。

    ``game_dir=True`` 会把**用户数据目录也指向沙箱**（``user_dir``），
    这样测试就不会往用户真实的
    ``文档/Paradox Interactive/Stellaris/mod/`` 里写东西。

    为什么需要这个开关：补丁模组是写进 ``<user_dir>/mod/`` 的，
    而那不是 ``LOCALAPPDATA``——光切 ``LOCALAPPDATA`` 挡不住。
    真实发生过：批量保存的测试往用户游戏目录里塞了 89 个补丁模组。
    凡是要**保存补丁/写游戏配置**的测试都必须开这个开关。
    """
    real_base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    real = Path(real_base) / "StellarisModDoctor"

    tmp = Path(tempfile.mkdtemp(prefix="smd_sandbox_"))
    # 记下真实的 LOCALAPPDATA：后面 _redirect_game_dirs 要回去读真实配置，
    # 而此时 os.environ 里的值已经被我们改掉了。
    os.environ["_SMD_REAL_LOCALAPPDATA"] = real_base
    os.environ["LOCALAPPDATA"] = str(tmp)
    _ACTIVE.append(tmp)

    if keep_real_config:
        src = real / "config.json"
        if src.is_file():
            try:
                (tmp / "StellarisModDoctor").mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, tmp / "StellarisModDoctor" / "config.json")
            except OSError:
                pass

    if game_dir:
        _redirect_game_dirs(tmp)

    def _cleanup() -> None:
        for d in _ACTIVE:
            shutil.rmtree(d, ignore_errors=True)

    atexit.register(_cleanup)
    return tmp


def _redirect_game_dirs(tmp: Path) -> None:
    """只把**可写的** ``user_dir`` 改到沙箱，其余游戏路径保持真实。

    关键是分清"读"和"写"：

    * ``user_dir`` —— 补丁模组、``dlc_load.json``、启动器数据库都写这里，
      **必须**指向沙箱。
    * ``game_dir`` / ``workshop_dirs`` —— 只是读取游戏和工坊内容，
      指到真实路径才能扫出真实冲突；指到空目录的话
      扫描只会得到一堆"文件完整性"误报，测试等于什么都没测。

    ``user_dir`` 下还要把 ``mod/`` 建出来，否则补丁写入会因为
    目录不存在而失败（那不算真的验证过）。
    """
    real_base = Path(os.environ.get("_SMD_REAL_LOCALAPPDATA")
                     or os.path.expanduser("~")) / "AppData" / "Local"
    real_cfg = real_base / "StellarisModDoctor" / "config.json"

    # 真实配置（用于取 game_dir / workshop_dirs）
    real: dict = {}
    try:
        if real_cfg.is_file():
            real = json.loads(real_cfg.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        real = {}
    real_paths = (real.get("paths") or {}) if isinstance(real, dict) else {}

    cfg = tmp / "StellarisModDoctor" / "config.json"
    data: dict = {}
    try:
        if cfg.is_file():
            data = json.loads(cfg.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    paths = data.get("paths")
    if not isinstance(paths, dict):
        paths = {}

    fake_user = tmp / "game" / "Stellaris"
    (fake_user / "mod").mkdir(parents=True, exist_ok=True)
    os.environ["SMD_SANDBOX_USER_DIR"] = str(fake_user)

    paths["user_dir"] = str(fake_user)
    # 只读路径沿用真实值（优先取真实配置，其次取沙箱配置里已有的）
    for k in ("game_dir", "workshop_dirs", "logs_dir", "crashes_dir"):
        v = real_paths.get(k) or paths.get(k)
        if v:
            paths[k] = v
    data["paths"] = paths
    try:
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                       encoding="utf-8")
    except OSError:
        pass
