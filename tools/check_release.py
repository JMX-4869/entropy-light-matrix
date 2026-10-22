"""验证 GitHub Release：能否下载、文件是否与本地构建完全一致。

为什么要比对哈希而不只看 HTTP 200：
上传中断、被代理截断、传错文件都会返回 200。只有逐字节一致
才能说明"用户下到的就是你构建的那个 exe"。

用法::  python tools/check_release.py
"""
from __future__ import annotations

import hashlib
import io
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                              errors="replace")

ROOT = Path(__file__).resolve().parent.parent

REPO = "JMX-4869/entropy-light-matrix"
TAG = "v1.0.7"

# 附件名 → 本地对应文件。**必须逐个映射**，不能共用一个 LOCAL：
# 早先只有一个 LOCAL 变量，拿它去比两个附件，Portable 当然对不上
# Setup 的哈希，于是脚本把**正常的上传报成了失败**。
ASSETS = {
    "StellarisModDoctor-Setup.exe":
        ROOT / "dist" / "StellarisModDoctor-Setup.exe",
    "StellarisModDoctor-Portable.exe":
        ROOT / "dist" / "StellarisModDoctor.exe",
    # 轻量版两个之前漏登记，校验时只能"跳过比对"。
    # 跳过不等于通过 —— 等于没验。
    "StellarisModDoctor-Light-Setup.exe":
        ROOT / "dist" / "StellarisModDoctor-Light-Setup.exe",
    "StellarisModDoctor-Light-Portable.exe":
        ROOT / "dist" / "StellarisModDoctor-Light.exe",
}

API = f"https://api.github.com/repos/{REPO}"
UA = {"User-Agent": "smd-release-check",
      "Accept": "application/vnd.github+json"}


def api(path: str):
    r = urllib.request.Request(API + path, headers=UA)
    with urllib.request.urlopen(r, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def sha256_local(path: str) -> tuple[str, int]:
    h = hashlib.sha256()
    n = 0
    with open(path, "rb") as f:
        while True:
            b = f.read(1 << 20)
            if not b:
                break
            h.update(b)
            n += len(b)
    return h.hexdigest(), n


def sha256_url(url: str) -> tuple[str, int, int]:
    """流式下载并算哈希，返回 (哈希, 字节数, HTTP状态)。"""
    r = urllib.request.Request(url, headers={"User-Agent": "smd"})
    h = hashlib.sha256()
    n = 0
    try:
        with urllib.request.urlopen(r, timeout=180) as resp:
            while True:
                b = resp.read(1 << 20)
                if not b:
                    break
                h.update(b)
                n += len(b)
            return h.hexdigest(), n, resp.status
    except urllib.error.HTTPError as e:
        return "", 0, e.code


def main() -> int:
    print("=" * 70)
    print(f"Release 验证  {REPO} @ {TAG}")
    print("=" * 70)

    # 1. Release 元信息
    try:
        rel = api(f"/releases/tags/{TAG}")
    except urllib.error.HTTPError as e:
        print(f"  取不到 Release（HTTP {e.code}）—— 可能还没发布")
        return 1
    print(f"\n  标题     : {rel['name']}")
    print(f"  标签     : {rel['tag_name']}")
    print(f"  最新版   : {'是' if not rel['prerelease'] and not rel['draft'] else '否'}")
    print(f"  页面     : {rel['html_url']}")

    assets = rel.get("assets") or []
    if not assets:
        print("\n  ！这个 Release 没有任何附件 —— 下载按钮会落空")
        return 1
    print(f"  附件     : {len(assets)} 个")
    for a in assets:
        print(f"    {a['name']}  {a['size'] / 1048576:.2f} MB  "
              f"下载 {a['download_count']} 次")

    # 2. 哈希比对：**逐个附件比它自己的本地文件**
    print("\n  逐字节比对（每个附件 vs 它的本地构建）：")
    ok = True
    for a in assets:
        name = a["name"]
        local = ASSETS.get(name)
        if local is None:
            print(f"    {name}: 没有登记本地对应文件，跳过比对")
            continue
        try:
            lh, ln = sha256_local(str(local))
        except OSError as e:
            print(f"    {name}: 读不到本地文件（{e}）")
            ok = False
            continue
        gh, gn, st = sha256_url(a["browser_download_url"])
        if st != 200:
            print(f"    {name}: 下载失败 HTTP {st}")
            ok = False
            continue
        same = (gh == lh and gn == ln)
        print(f"    {name}")
        print(f"      本地    {ln / 1048576:7.2f} MB  {lh[:32]}…")
        print(f"      GitHub  {gn / 1048576:7.2f} MB  {gh[:32]}…")
        print(f"      => {'完全一致' if same else '！！不一致'}")
        ok = ok and same

    # 3. 介绍站的下载按钮指向是否正确
    print("\n  介绍站「下载 Windows 版」按钮：")
    site = ("https://jmx-4869.github.io/entropy-light-matrix/")
    try:
        r = urllib.request.Request(site, headers={"User-Agent": "smd"})
        with urllib.request.urlopen(r, timeout=30) as resp:
            html = resp.read(400_000).decode("utf-8", "ignore")
        want = f"{REPO}/releases"
        print(f"    首页含 /releases 链接：{'是' if want in html else '否'}")
        if want not in html:
            print("    ！按钮指向有问题")
            ok = False
    except Exception as e:                       # noqa: BLE001
        print(f"    首页取不到：{e}")
        ok = False

    print("\n" + "=" * 70)
    print("  结论：" + ("Release 完整可用" if ok else "有问题，见上"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
