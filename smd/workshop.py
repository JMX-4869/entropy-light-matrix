"""从 Steam 创意工坊批量拉取模组描述。

## 为什么需要

排序要求只写在**工坊页面的描述**里 —— ``descriptor.mod`` 只有 name/version/tags。
真实案例：``! Core Framework of Kasako`` 的描述写着「必须在【UI Overhaul Dynamic】后面」，
而它的文件里一个字都没有（实测 4625 字描述 vs 5 行 descriptor）。

## 为什么不用浏览器

96 个模组 = 96 个页面 = 慢、脆、还要处理 Steam 的登录墙。
Steam 有一个**批量**接口::

    POST https://api.steampowered.com/ISteamRemoteStorage/GetPublishedFileDetails/v1/
    itemcount=50
    publishedfileids[0]=2466607238
    ...

一次 50 个，96 个两趟就够（实测单次响应 < 2 秒）。

## 缓存

描述很少变。缓存到 ``<data_dir>/workshop_desc.json``，
按 ``remote_file_id`` 存，带 ``fetched_at``。默认 7 天内不重拉 ——
用户天天开软件，不能天天拉 96 个页面。
"""
from __future__ import annotations

import json
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://api.steampowered.com/ISteamRemoteStorage/GetPublishedFileDetails/v1/"
BATCH = 50
CACHE_TTL = 7 * 24 * 3600      # 7 天


def cache_path(data_dir: Path) -> Path:
    return Path(data_dir) / "workshop_desc.json"


def load_cache(data_dir: Path) -> dict:
    try:
        return json.loads(cache_path(data_dir).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_cache(data_dir: Path, cache: dict) -> None:
    try:
        p = cache_path(data_dir)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def strip_bbcode(text: str) -> str:
    """去掉 Steam 的 BBCode，但**保住文字**。

    这里踩过两个坑，都记下来：

    ① ``[h1]排序[/h1]`` 不能整行删 —— 「排序」这个小标题正是 ``sortreq``
       定位小节的锚点，删了标签连标题一起没了，小节永远找不到。
    ② ``[url=https://...]UI Overhaul Dynamic[/url]`` 里的**文字是排序目标的名字**。
       早先用一条 ``\\[[a-zA-Z0-9_*=/:.\\-]+\\]`` 大正则通吃，它把
       ``[url=...]`` 里的 ``]`` 也算进标签字符集，于是整段变成
       ``?id=2411818376]Dark Blue UI`` —— 名字全废，约束一条也提不出来。
       必须先单独处理 url，**留下文字**，再清别的。
    """
    if not text:
        return ""
    s = text.replace("\r\n", "\n").replace("\r", "\n")

    # ① 媒体整段丢。
    #    **两种写法都要认**：``[img]...[/img]`` 和**自闭合**的 ``[img=url]``。
    #    小伞的描述开头就是 ``[img=https://...]`` 且**没有闭合标签** ——
    #    早先只写了「有闭合」那条，非贪婪的 .*? 于是一路吞到后面某个
    #    ``[/img]``，把整段 Sorting 都吃掉了（实测：解析出 0 条排序要求）。
    s = re.sub(r"\[img\][^[]*\[/img\]", "", s, flags=re.S | re.I)
    s = re.sub(r"\[img[^\]]*\]", "", s, flags=re.I)          # 自闭合
    s = re.sub(r"\[video[^\]]*\].*?\[/video\]", "", s, flags=re.S | re.I)
    s = re.sub(r"\[previewyoutube=[^\]]*\].*?\[/previewyoutube\]", "",
               s, flags=re.S | re.I)

    # ② [url=xxx]文字[/url] / [url]xxx[/url] → 文字（**这是排序目标名的来源**）
    s = re.sub(r"\[url=[^\]]*\](.*?)\[/url\]", r"\1", s, flags=re.S | re.I)
    s = re.sub(r"\[url\](.*?)\[/url\]", r"\1", s, flags=re.S | re.I)

    # ③ 块级标签 → 换行（保住分行结构，'_find_section' 靠整行匹配标题）
    s = re.sub(r"\[/?(h[1-6]|list|olist|p|br|hr|quote|code)[^\]]*\]",
               "\n", s, flags=re.I)
    s = re.sub(r"\[\*\]", "\n", s, flags=re.I)

    # ④ 剩下的简单内联标签直接去掉
    s = re.sub(r"\[/?(b|i|u|s|noparse|center|left|right)\]", "", s, flags=re.I)

    # ⑤ 兜底：还有残留的 [xxx] 也去掉（此时 url 已经处理完，不会再伤到名字）
    s = re.sub(r"\[[a-zA-Z0-9_*]+[^\]]*\]", "", s)

    # 收尾：压空行、去行首空白
    s = re.sub(r"[ \t]+\n", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return "\n".join(ln.strip() for ln in s.split("\n")).strip()


def fetch(ids: list[str], *, timeout: float = 25.0) -> dict[str, str]:
    """批量拉描述，返回 ``{publishedfileid: 描述}``。失败返回空字典。"""
    out: dict[str, str] = {}
    ids = [i for i in dict.fromkeys(ids) if i]
    for i in range(0, len(ids), BATCH):
        chunk = ids[i:i + BATCH]
        body = urllib.parse.urlencode({
            "itemcount": len(chunk),
            **{f"publishedfileids[{n}]": v for n, v in enumerate(chunk)},
        }).encode()
        try:
            req = urllib.request.Request(API, data=body, method="POST")
            req.add_header("Content-Type", "application/x-www-form-urlencoded")
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.loads(r.read().decode("utf-8", "replace"))
        except (OSError, ValueError):
            continue
        for it in (data.get("response", {}).get("publishedfiledetails") or []):
            fid = str(it.get("publishedfileid") or "")
            desc = it.get("description") or ""
            if fid and desc:
                out[fid] = desc
    return out


def resolve(lib, data_dir: Path, *, force: bool = False,
            log=None) -> dict[str, str]:
    """给整份模组库拉描述，返回 ``{模组名: 描述}``。

    只拉**有 remote_file_id** 的（本地 mod 没有工坊页面）。
    缓存命中就不发请求。
    """
    cache = load_cache(data_dir)
    now = time.time()

    # 收集要拉/能用的
    want: dict[str, object] = {}        # fid -> Mod
    for m in lib.mods.values():
        fid = str(getattr(m, "remote_file_id", "") or "")
        if not fid:
            continue
        ent = cache.get(fid)
        fresh = (isinstance(ent, dict)
                 and now - float(ent.get("fetched_at") or 0) < CACHE_TTL
                 and ent.get("text"))
        if fresh and not force:
            continue
        want[fid] = m

    if want:
        if log:
            log(f"拉取 {len(want)} 个模组的工坊描述…")
        got = fetch(list(want.keys()))
        for fid, desc in got.items():
            cache[fid] = {"text": desc, "fetched_at": now}
            m = want.get(fid)
            if m is not None:
                m.__dict__["description"] = desc   # 顺手挂在对象上
        # 拉不到的也记一笔时间戳，避免每次启动都重试
        for fid in want:
            if fid in got:
                continue
            cache.setdefault(fid, {"text": "", "fetched_at": now})
        save_cache(data_dir, cache)
    elif log:
        log("工坊描述命中缓存，未发请求")

    # 组装 {模组名: 纯文本描述}
    out: dict[str, str] = {}
    for m in lib.mods.values():
        fid = str(getattr(m, "remote_file_id", "") or "")
        if not fid:
            continue
        ent = cache.get(fid) or {}
        text = ent.get("text") or ""
        if text:
            out[getattr(m, "name", "")] = strip_bbcode(text)
    return out
