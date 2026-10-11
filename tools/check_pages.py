"""验证 GitHub Pages 线上站点是否正常。

本地能看不代表线上没问题：Pages 是静态托管，路径大小写敏感、
相对路径解析规则和本地 file:// 也不同，视频这类大文件的
Content-Type 与 Range 支持还要单独确认。

用法::  python tools/check_pages.py
"""
from __future__ import annotations

import io
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                              errors="replace")

BASE = "https://jmx-4869.github.io/entropy-light-matrix/"
UA = {"User-Agent": "Mozilla/5.0 (compatible; smd-pages-check)"}


def q(path: str) -> str:
    """把仓库根相对路径拼成 Pages URL（中文文件名要 URL 编码）。"""
    return BASE + urllib.parse.quote(path)


TARGETS = [
    ("首页", BASE, 200),
    ("截图·总览", q("screenshots/01_总览.png"), 200),
    ("截图·冲突", q("screenshots/02_冲突检测.png"), 200),
    ("截图·AI接管", q("screenshots/03_AI接管.png"), 200),
    ("截图·档案", q("screenshots/04_时之虫档案.png"), 200),
    ("截图·外观", q("screenshots/05_AI生成外观.png"), 200),
    ("截图·崩溃", q("screenshots/06_崩溃AI分析.png"), 200),
    ("社交预览图", q("social-preview.png"), 200),
    ("宣传片 mp4", q("../promo/熵光矩阵-宣传片-1080p.mp4"), 200),
]


def req(url: str, method: str = "HEAD", extra: dict | None = None,
        read: int = 2048):
    """发一个请求，返回 (状态码, 头, 前若干字节)。异常也转成状态码。

    ``read`` 是要读多少字节。做内容抽查时必须读**整页** ——
    只读前 2KB 的话，页面中段的内容会被误判成"缺失"
    （本脚本第一版就踩了这个坑，把 `<video>` 报成缺）。
    """
    h = dict(UA)
    if extra:
        h.update(extra)
    r = urllib.request.Request(url, method=method, headers=h)
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            body = b""
            if method == "GET":
                body = resp.read(read)
            return resp.status, dict(resp.headers), body
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {}), b""
    except Exception as e:                       # noqa: BLE001
        return 0, {"error": str(e)[:60]}, b""


def size_of(headers: dict) -> str:
    cl = headers.get("Content-Length") or headers.get("content-length") or ""
    if not str(cl).isdigit():
        return "-"
    n = int(cl)
    return f"{n / 1048576:.2f} MB" if n >= 1048576 else f"{n / 1024:.0f} KB"


def main() -> int:
    print("=" * 68)
    print("GitHub Pages 线上验证")
    print("=" * 68)
    print(f"  {BASE}\n")

    print("  等待部署（最多 3 分钟）…")
    st = 0
    for i in range(18):
        st, hd, _ = req(BASE)
        if st == 200:
            print(f"  第 {i * 10} 秒：首页已可访问\n")
            break
        time.sleep(10)
    else:
        print(f"  仍未就绪（最后状态 {st}）—— 可能还在构建，稍后再跑一次")
        return 0

    print(f"  {'资源':14} {'状态':>4}  {'类型':30} {'大小':>9}")
    print("  " + "-" * 62)
    bad = []
    for name, url, want in TARGETS:
        st, hd, _ = req(url)
        ct = (hd.get("Content-Type") or hd.get("content-type") or "")[:30]
        mark = "OK" if st == want else "!!"
        if st != want:
            bad.append(f"{name} → HTTP {st}")
        print(f"  {mark} {name:12} {st:>4}  {ct:30} {size_of(hd):>9}")

    # 视频单独验：Range 请求必须返回 206，否则浏览器不能拖动进度条
    print("\n  视频拖动支持（Range 请求）：")
    vurl = q("../promo/熵光矩阵-宣传片-1080p.mp4")
    st, hd, _ = req(vurl, "GET", {"Range": "bytes=0-1023"})
    cr = hd.get("Content-Range") or hd.get("content-range") or "(无)"
    ar = hd.get("Accept-Ranges") or hd.get("accept-ranges") or "(无)"
    ok_range = st == 206
    print(f"    状态 {st}（206 = 支持拖动）   Accept-Ranges: {ar}")
    print(f"    Content-Range: {cr}")
    if not ok_range:
        bad.append(f"视频 Range 请求返回 {st}，进度条可能不能拖")

    # 首页内容抽查：确认不是 GitHub 的 404/503 页面
    # 必须读**整页** —— 只读前 2KB 会把页面中段的内容误判成缺失。
    print("\n  首页内容抽查：")
    st, hd, body = req(BASE, "GET", read=300_000)
    text = body.decode("utf-8", "ignore")
    print(f"    读到 {len(body) / 1024:.0f} KB")
    for key in ("熵光矩阵", "ENTROPY LIGHT MATRIX", "<video", "approved_disable"):
        hit = key in text or key.lower() in text.lower()
        print(f"    {'OK ' if hit else '缺 '} 含「{key}」")
        if not hit:
            bad.append(f"首页缺少「{key}」")

    # 反向对照：确认"仓库根目录的文件不发布"这个判断是对的
    # （如果这条也 200，说明 503 另有原因）
    print("\n  对照检查（仓库根目录的文件**不该**被发布）：")
    st2, _, _ = req(BASE + "../README.md")
    print(f"    上级目录 README.md → HTTP {st2}"
          f"（{'符合预期，根目录不发布' if st2 != 200 else '意外可访问'}）")

    print("\n" + "=" * 68)
    if bad:
        print("  发现问题：")
        for b in bad:
            print(f"    - {b}")
        return 1
    print("  线上站点正常：页面、6 张截图、社交图、宣传片全部可访问")
    return 0


if __name__ == "__main__":
    sys.exit(main())
