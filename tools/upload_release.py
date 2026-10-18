"""给已有的 GitHub Release 上传附件。

为什么单独写而不是用 gh：
``gh`` 需要先 ``gh auth login``，而这台机器上它没登录；但 git 推送用的
凭据已经在 Windows 凭据管理器里了，可以直接复用来调 API。
这样不用再走一次浏览器授权，也不会把 token 写到磁盘上。

用法::

    python tools/upload_release.py --list
    python tools/upload_release.py --file "dist/x.exe" --name "y.exe" \
        --label "免安装版"
    python tools/upload_release.py --file ... --name ... --replace

``--replace`` 会先删掉同名附件再传（GitHub 不允许同名附件并存）。
不传 ``--replace`` 时遇到同名会拒绝，避免误删。
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import mimetypes
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                              errors="replace")

REPO = "JMX-4869/entropy-light-matrix"
TAG = "v1.0.3"
API = f"https://api.github.com/repos/{REPO}"
UPLOAD = f"https://uploads.github.com/repos/{REPO}"


def token() -> str:
    """从 git 凭据管理器里取出 GitHub token。

    走 ``git credential fill``（它自己会去问 Windows 凭据管理器），
    而不是读某个明文文件 —— token 不落盘。
    """
    try:
        out = subprocess.run(
            ["git", "credential", "fill"],
            input="protocol=https\nhost=github.com\n\n",
            capture_output=True, text=True, encoding="utf-8", timeout=30,
            cwd=str(Path(__file__).resolve().parent.parent))
    except (OSError, subprocess.SubprocessError) as e:
        raise SystemExit(f"  取凭据失败：{e}")
    for line in (out.stdout or "").splitlines():
        if line.startswith("password="):
            t = line[len("password="):].strip()
            if t:
                return t
    raise SystemExit("  凭据里没有 github.com 的 token。\n"
                     "  先手动推送一次（git push），让凭据管理器记住它。")


def call(url: str, tok: str, *, method: str = "GET",
         data: bytes | None = None, ctype: str | None = None):
    """调一次 API，返回 (状态码, 解析后的 JSON 或原始文本)。"""
    h = {"Authorization": f"Bearer {tok}",
         "User-Agent": "smd-upload",
         "Accept": "application/vnd.github+json"}
    if ctype:
        h["Content-Type"] = ctype
    r = urllib.request.Request(url, method=method, data=data, headers=h)
    try:
        with urllib.request.urlopen(r, timeout=600) as resp:
            body = resp.read()
            try:
                return resp.status, json.loads(body.decode("utf-8"))
            except ValueError:
                return resp.status, body.decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "ignore")
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, raw


def sha256_file(p: Path) -> tuple[str, int]:
    h = hashlib.sha256()
    n = 0
    with p.open("rb") as f:
        while True:
            b = f.read(1 << 20)
            if not b:
                break
            h.update(b)
            n += len(b)
    return h.hexdigest(), n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true", help="列出当前附件")
    ap.add_argument("--file", help="要上传的本地文件")
    ap.add_argument("--name", help="在 Release 上显示的文件名（默认用原名）")
    ap.add_argument("--label", default="", help="附件说明文字")
    ap.add_argument("--notes", default="",
                    help="创建新 Release 时用的说明文字")
    ap.add_argument("--replace", action="store_true",
                    help="同名附件已存在时先删除再上传")
    args = ap.parse_args()

    tok = token()
    print(f"  凭据 OK（token 长度 {len(tok)}）")

    # 找 release；不存在就**直接创建**（发新版时用得上）
    #
    # 为什么要自动创建：发版本是个"改版本号 → 重新构建 → 上传"的流程，
    # 中间手动去网页点"Create a new release"很容易忘，忘了就出现
    # "构建好了但 Release 还是旧的"这种不一致。让脚本自己建更稳。
    st, rel = call(f"{API}/releases/tags/{TAG}", tok)
    if st == 404:
        print(f"  Release {TAG} 不存在，正在创建…")
        body = json.dumps({
            "tag_name": TAG,
            "name": f"熵光矩阵 {TAG}",
            "body": args.notes or f"版本 {TAG}",
            "draft": False,
            "prerelease": False,
        }).encode("utf-8")
        st, rel = call(f"{API}/releases", tok, method="POST", data=body,
                       ctype="application/json")
        if st not in (200, 201):
            print(f"  ！创建失败 HTTP {st}\n    {str(rel)[:300]}")
            return 1
        print(f"  已创建：{rel.get('html_url')}")
        # 新建的 release 必须是 latest，否则界面上它还标着旧版
        call(f"{API}/releases/{rel['id']}", tok, method="PATCH",
             data=json.dumps({"make_latest": "true"}).encode("utf-8"),
             ctype="application/json")
    elif st != 200:
        print(f"  取不到 Release {TAG}（HTTP {st}）")
        return 1
    rid = rel["id"]

    st, assets = call(f"{API}/releases/{rid}/assets", tok)
    have = {a["name"]: a for a in (assets if isinstance(assets, list) else [])}

    print(f"\n  当前附件（{len(have)} 个）：")
    for n, a in have.items():
        print(f"    {n:38} {a['size'] / 1048576:7.2f} MB  "
              f"下载 {a['download_count']} 次")

    if args.list or not args.file:
        return 0

    src = Path(args.file)
    if not src.is_file():
        print(f"\n  找不到文件：{src}")
        return 1
    name = args.name or src.name

    if name in have:
        if not args.replace:
            print(f"\n  附件「{name}」已存在。要覆盖请加 --replace")
            return 1
        print(f"\n  删除同名旧附件：{name}")
        st, _ = call(f"{API}/releases/assets/{have[name]['id']}", tok,
                     method="DELETE")
        print(f"    删除返回 HTTP {st}")

    h, n = sha256_file(src)
    print(f"\n  上传 {src.name}")
    print(f"    目标名 {name}")
    print(f"    大小   {n / 1048576:.2f} MB")
    print(f"    SHA256 {h}")

    ctype = (mimetypes.guess_type(name)[0]
             or "application/octet-stream")
    url = f"{UPLOAD}/releases/{rid}/assets?name={urllib.parse.quote(name)}"
    st, res = call(url, tok, method="POST", data=src.read_bytes(),
                   ctype=ctype)
    if st not in (200, 201):
        print(f"\n  上传失败 HTTP {st}")
        print(f"    {str(res)[:400]}")
        return 1

    print(f"\n  上传成功 HTTP {st}")
    print(f"    下载地址 {res.get('browser_download_url')}")

    # 回读校验：确认服务端保存的字节数与本地一致
    st2, assets2 = call(f"{API}/releases/{rid}/assets", tok)
    got = next((a for a in assets2 if a["name"] == name), None) \
        if isinstance(assets2, list) else None
    if got:
        same = got["size"] == n
        print(f"    服务端大小 {got['size'] / 1048576:.2f} MB  "
              f"（{'与本地一致' if same else '！！不一致'}）")
        if not same:
            return 1
    print("\n  完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
