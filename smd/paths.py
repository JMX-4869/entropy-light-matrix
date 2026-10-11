"""路径工具：大小写不敏感比较、长路径、相对路径、文件哈希。"""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path, PurePath

WINDOWS = sys.platform.startswith("win")


def norm(p: str | os.PathLike[str]) -> str:
    """归一化为小写、正斜杠形式，用于跨来源（.mod 文件 / sqlite）比较。"""
    s = str(p).replace("\\", "/").strip()
    while s.endswith("/") and len(s) > 1:
        s = s[:-1]
    return s.lower()


def norm_key(p: str | os.PathLike[str]) -> str:
    """用于字典键的路径：小写 + 正斜杠。"""
    return norm(p)


def to_path(p: str | os.PathLike[str]) -> Path:
    """把任意路径表示转成 :class:`pathlib.Path`。"""
    return Path(str(p))


def long_path(p: str | os.PathLike[str]) -> str:
    r"""Windows 长路径前缀，规避 MAX_PATH 限制（工坊模组路径常常很深）。"""
    s = str(p)
    if WINDOWS and len(s) > 240 and not s.startswith("\\\\?\\"):
        if s.startswith("\\\\"):
            return "\\\\?\\UNC\\" + s[2:]
        return "\\\\?\\" + os.path.abspath(s)
    return s


def exists(p: str | os.PathLike[str]) -> bool:
    """判断路径是否存在（自动加 Windows 长路径前缀）。"""
    try:
        return os.path.exists(long_path(p))
    except (OSError, ValueError):
        return False


def is_dir(p: str | os.PathLike[str]) -> bool:
    """判断是否目录（自动加长路径前缀）。"""
    try:
        return os.path.isdir(long_path(p))
    except (OSError, ValueError):
        return False


def is_file(p: str | os.PathLike[str]) -> bool:
    """判断是否普通文件（自动加长路径前缀）。"""
    try:
        return os.path.isfile(long_path(p))
    except (OSError, ValueError):
        return False


def rel_posix(path: str | os.PathLike[str], base: str | os.PathLike[str]) -> str:
    """返回相对 base 的正斜杠小写路径；失败则返回原路径归一化结果。"""
    try:
        return PurePath(norm(path)).relative_to(PurePath(norm(base))).as_posix()
    except ValueError:
        return norm(path)


def sha1_file(path: str | os.PathLike[str], chunk: int = 1 << 20) -> str:
    """计算文件的 SHA-1（分块读取，避免大文件占内存）。"""
    h = hashlib.sha1()
    with open(long_path(path), "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def sha1_text(text: str) -> str:
    """计算一段文本的 SHA-1，用于比较定义内容是否一致。"""
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()


def read_text(path: str | os.PathLike[str], encoding: str = "utf-8-sig",
              errors: str = "replace") -> str:
    """宽容读取：Paradox 文件编码混乱（UTF-8 / UTF-8-BOM / GBK / Latin-1）。"""
    raw = read_bytes(path)
    return decode_text(raw, encoding, errors)


def read_bytes(path: str | os.PathLike[str]) -> bytes:
    """读取文件全部字节。"""
    with open(long_path(path), "rb") as f:
        return f.read()


def decode_text(raw: bytes, encoding: str = "utf-8-sig",
                errors: str = "replace") -> str:
    """按 UTF-8 -> GBK -> Latin-1 顺序尝试解码，兼容中文汉化模组。"""
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw[3:].decode("utf-8", "replace")
    if raw.startswith(b"\xff\xfe"):
        try:
            return raw.decode("utf-16-le", "replace")
        except Exception:  # pragma: no cover - 极端情况
            pass
    for enc in (encoding, "utf-8", "gbk", "cp936", "latin-1"):
        if not enc:
            continue
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")


def write_text(path: str | os.PathLike[str], text: str,
               encoding: str = "", *, bom: bool = True) -> None:
    """写入文本文件，自动创建父目录，统一用 ``\\n`` 换行。

    默认写 **UTF-8 BOM**：群星读取带 BOM 的脚本与本地化文件都正常，
    而带 BOM 能让中文在编辑器里不出现乱码。

    但**配置与主题这类 JSON 文件必须传 ``bom=False``**：
    BOM 会让很多 JSON 解析器直接报
    ``Unexpected UTF-8 BOM``（Python 的 ``json.loads`` 就是），
    用户拿别的工具读一下就会踩坑。

    ``encoding`` 显式传入时以它为准，``bom`` 被忽略。
    """
    p = to_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    enc = encoding or ("utf-8-sig" if bom else "utf-8")
    with open(long_path(p), "w", encoding=enc, newline="\n") as f:
        f.write(text)


def human_size(n: int | float) -> str:
    """把字节数格式化成 ``1.23 GB`` 这样的人类可读形式。"""
    step = 1024.0
    v = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if v < step or unit == "TB":
            return f"{v:.0f} {unit}" if unit == "B" else f"{v:.2f} {unit}"
        v /= step
    return f"{v:.2f} TB"
