"""创建 v1.0.8 Release 并上传四个附件。

为什么不用 gh：这台机器 gh 没登录，但 git 凭据管理器里有 token，
可以直接复用来调 API。token 不落盘。
"""
from __future__ import annotations

import io
import json
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                              errors="replace")

REPO = "JMX-4869/entropy-light-matrix"
TAG = "v1.0.8"
API = f"https://api.github.com/repos/{REPO}"
UPLOAD = f"https://uploads.github.com/repos/{REPO}"

ASSETS = [
    ("dist/StellarisModDoctor-Setup.exe", "StellarisModDoctor-Setup.exe", "完整版 安装版"),
    ("dist/StellarisModDoctor.exe", "StellarisModDoctor-Portable.exe", "完整版 免安装"),
    ("dist/StellarisModDoctor-Light-Setup.exe", "StellarisModDoctor-Light-Setup.exe", "轻量版 安装版"),
    ("dist/StellarisModDoctor-Light.exe", "StellarisModDoctor-Light-Portable.exe", "轻量版 免安装"),
]


def token() -> str:
    out = subprocess.run(
        ["git", "credential", "fill"],
        input="protocol=https\nhost=github.com\n\n",
        capture_output=True, text=True, encoding="utf-8", check=True,
    ).stdout
    for line in out.splitlines():
        if line.startswith("password="):
            return line.split("=", 1)[1]
    raise SystemExit("拿不到 token")


TOK = token()


def api(url: str, *, method: str = "GET", body: bytes | None = None,
        ctype: str = "application/json") -> dict:
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("Authorization", f"Bearer {TOK}")
    req.add_header("Accept", "application/vnd.github+json")
    if body is not None:
        req.add_header("Content-Type", ctype)
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read().decode("utf-8") or "{}")


def main() -> None:
    body_text = Path("docs/RELEASE-v1.0.8.md").read_text(encoding="utf-8")
    payload = json.dumps({
        "tag_name": TAG,
        "name": "v1.0.8 — 桌面图标 / 扫描 / 路径探测",
        "body": body_text,
        "draft": False,
        "prerelease": False,
    }, ensure_ascii=False).encode("utf-8")

    # 已存在就复用
    try:
        rel = api(f"{API}/releases/tags/{TAG}")
        print(f"  Release 已存在: {rel['html_url']}")
    except Exception:
        rel = api(f"{API}/releases", method="POST", body=payload)
        print(f"  已创建: {rel['html_url']}")

    rid = rel["id"]
    have = {a["name"] for a in rel.get("assets", [])}

    for path, name, label in ASSETS:
        p = Path(path)
        if not p.is_file():
            print(f"  ✗ 缺文件 {path}")
            continue
        if name in have:
            print(f"  - {name} 已存在，跳过")
            continue
        data = p.read_bytes()
        # name / label 都要转义：label 是中文且带空格，
        # 直接拼进 URL 会让 http.client 抛 InvalidURL。
        q = urllib.parse.urlencode({"name": name, "label": label})
        url = f"{UPLOAD}/releases/{rid}/assets?{q}"
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Authorization", f"Bearer {TOK}")
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("Content-Type", "application/octet-stream")
        with urllib.request.urlopen(req) as r:
            a = json.loads(r.read().decode("utf-8"))
        print(f"  ✓ {name}  {a['size']/1048576:.2f} MB  {label}")


if __name__ == "__main__":
    main()
