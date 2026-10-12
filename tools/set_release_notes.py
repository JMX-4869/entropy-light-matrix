"""更新已有 Release 的说明文字。

`upload_release.py --notes` 只在**创建**新 Release 时用得上；
给已存在的 Release 换说明需要 PATCH，这个脚本专门做这件事。

用法::

    python tools/set_release_notes.py <说明文件.md>

输出用 ``os.write`` 直接写字节，不经 ``sys.stdout``：
`upload_release` 在模块级包装过一次 stdout，我们再包第二次时，
第一个包装器会被回收**并关闭底层 buffer**，写出时抛
`ValueError: I/O operation on closed file`（已踩过两次）。
绕开 stdout 对象最省事。
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def out(*args) -> None:
    """直接往 fd 1 写 UTF-8 字节，不碰 sys.stdout。"""
    msg = "".join(str(a) for a in args) + "\n"
    try:
        os.write(1, msg.encode("utf-8", "replace"))
    except OSError:
        pass


# 复用 upload_release 的取凭据与调用函数
spec = importlib.util.spec_from_file_location(
    "_up", ROOT / "tools" / "upload_release.py")
_up = importlib.util.module_from_spec(spec)
sys.modules["_up"] = _up
spec.loader.exec_module(_up)


def main() -> int:
    if len(sys.argv) < 2:
        out("  用法: python tools/set_release_notes.py <说明文件.md>")
        return 1
    notes = Path(sys.argv[1])
    if not notes.is_file():
        out(f"  找不到说明文件：{notes}")
        return 1
    body = notes.read_text(encoding="utf-8")

    tok = _up.token()
    st, rel = _up.call(f"{_up.API}/releases/tags/{_up.TAG}", tok)
    if st != 200:
        out(f"  取不到 Release {_up.TAG}（HTTP {st}）")
        return 1
    st, res = _up.call(f"{_up.API}/releases/{rel['id']}", tok, method="PATCH",
                       data=json.dumps({"body": body}).encode("utf-8"),
                       ctype="application/json")
    if st not in (200, 201):
        out(f"  ！更新失败 HTTP {st}")
        out(f"    {str(res)[:300]}")
        return 1
    out(f"  {_up.TAG} 说明已更新（{len(body)} 字符）")
    out(f"  {rel.get('html_url')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
