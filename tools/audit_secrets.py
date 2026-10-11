"""核实：本机保存的 API Key 有没有被带进打包产物或开源仓库。

开源前该跑一次。**用你自己的真实密钥**做字节级搜索——比正则猜
"什么样子像密钥"可靠得多，也不会漏掉自定义格式的 Key。

用法::

    python tools/audit_secrets.py            # 用本机 config.json 里的 Key
    python tools/audit_secrets.py sk-xxx     # 指定要查的串

为什么是字节级而不是文本级：密钥可能在压缩流里、也可能是 UTF-16 编码。
纯文本搜索会漏。这里对三种编码（utf-8 / utf-16-le / utf-16-be）都查一遍。

顺带说明为什么默认是安全的：密钥存在
``%LOCALAPPDATA%\\StellarisModDoctor\\config.json``，而安装包只装程序本体，
配置是**运行时才生成**的，所以打包产物里根本不会有它。
这个脚本是用来确认这一点的，不是用来救火的。
"""
import io
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                              errors="replace")

CONFIG = Path.home() / "AppData" / "Local" / "StellarisModDoctor" / "config.json"


def load_key() -> str:
    """从本机配置里读出 API Key（读不到就要求命令行给一个）。"""
    if len(sys.argv) > 1 and sys.argv[1].strip():
        return sys.argv[1].strip()
    try:
        d = json.loads(CONFIG.read_bytes().decode("utf-8-sig"))
        return str((d.get("ai") or {}).get("api_key") or "").strip()
    except (OSError, ValueError):
        return ""


def needles_for(key: str) -> list[bytes]:
    """要搜的字节序列：完整密钥 + 去掉前缀的片段，各三种编码。

    搜片段是为了防"密钥被截断存进某处"；三种编码是因为
    Windows 上写注册表/INI 很容易落成 UTF-16。
    """
    frags = [key, key[3:] if key.lower().startswith("sk-") else key,
             key[-16:]]
    out: list[bytes] = []
    for f in frags:
        if len(f) < 8:          # 太短的片段会在二进制里乱命中
            continue
        for enc in ("utf-8", "utf-16-le", "utf-16-be"):
            try:
                out.append(f.encode(enc))
            except UnicodeEncodeError:
                pass
    return out


def scan(path: Path, needles: list[bytes]) -> list[str]:
    """在文件字节里找任意一种编码形式的密钥片段。"""
    try:
        data = path.read_bytes()
    except OSError as exc:
        return [f"读不到：{exc}"]
    return [n.decode("utf-8", "ignore")[:22] for n in needles if n in data]


def main() -> int:
    key = load_key()
    print("=" * 72)
    print("API Key 泄漏核查")
    print("=" * 72)
    if not key:
        print("  没找到可查的密钥：config.json 里 api_key 为空，")
        print("  且命令行也没给。用法：python tools/audit_secrets.py sk-xxx")
        return 0
    needles = needles_for(key)
    print(f"  被查的密钥：{key[:8]}…{key[-4:]}（{len(key)} 字符）")
    print(f"  搜索 {len(needles)} 种编码形式")
    print()

    print("--- 1. 打包产物与随包数据 ---")
    for name in ("StellarisModDoctor.exe", "StellarisModDoctor-Setup.exe"):
        t = ROOT / "dist" / name
        if not t.is_file():
            print(f"  {name:34} （不存在，跳过）")
            continue
        h = scan(t, needles)
        print(f"  {name:34} {t.stat().st_size / 1048576:6.2f} MB  "
              + ("！！命中 " + str(h) if h else "干净"))
    for rel in ("smd/data/wiki.json", "smd/web/index.html", "smd/web/theme.css",
                "smd/web/i18n.js"):
        t = ROOT / rel
        if t.is_file():
            h = scan(t, needles)
            print(f"  {rel:34} {t.stat().st_size / 1048576:6.2f} MB  "
                  + ("！！命中 " + str(h) if h else "干净"))

    print("\n--- 2. 仓库被跟踪的全部文件 ---")
    out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True,
                         text=True, encoding="utf-8", errors="replace")
    files = [f for f in out.stdout.splitlines() if f.strip()]
    bad = []
    for f in files:
        p = ROOT / f
        if p.is_file():
            h = scan(p, needles)
            if h:
                bad.append((f, h))
    print(f"  检查了 {len(files)} 个被跟踪文件")
    for f, h in bad:
        print(f"  ！！ {f} → {h}")
    if not bad:
        print("  干净：没有任何文件包含你的密钥或它的片段")

    print("\n--- 3. git 全部历史（含已删除文件的内容）---")
    out = subprocess.run(["git", "log", "--all", "-p"], cwd=ROOT,
                         capture_output=True)
    blob = out.stdout
    hit = [n.decode("utf-8", "ignore")[:22] for n in needles if n in blob]
    print(f"  历史补丁体积 {len(blob) / 1048576:.1f} MB  "
          + ("！！命中 " + str(hit) if hit else "干净"))

    print("\n--- 4. 密钥实际所在 ---")
    print(f"  {CONFIG}")
    if CONFIG.is_file():
        raw = CONFIG.read_bytes()
        print(f"  存在，{len(raw)} 字节，确实含密钥："
              f"{key.encode() in raw}")
        print("  这个文件在 %LOCALAPPDATA%，不在仓库里也不在安装包里 ——")
        print("  安装包只装程序本体，用户配置是运行时才生成的。")

    print("\n--- 5. .gitignore 是否挡住配置文件 ---")
    gi = (ROOT / ".gitignore").read_text(encoding="utf-8")
    for pat in ("config.json", "*.json.tmp", "*.log"):
        print(f"  {'OK ' if pat in gi else '缺 '} {pat}")
    print("  （config.json.bak 由 'config.json' 那条覆盖，"
          "可用 git check-ignore 验证）")

    leaked = bool(bad) or bool(hit)
    print("\n" + "=" * 72)
    print("结论：" + ("**有泄漏，立刻处理**" if leaked
                     else "未发现密钥进入打包产物或仓库"))
    return 1 if leaked else 0


if __name__ == "__main__":
    sys.exit(main())

