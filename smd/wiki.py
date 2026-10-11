"""时之虫档案——群星知识库。

数据来自「4.2.4 星系热症天灾宝典」表格，由 ``tools/build_wiki.py``
抽取成 ``smd/data/wiki.json``（随程序分发，运行时只读）。

为什么做成软件内置而不是直接看表格
----------------------------------
那份表格 4.5 MB、27 个工作表，Excel 打开慢、手机上更打不开，
而且**没法搜索**——想找一条命令只能靠肉眼扫几千行。
内置成 JSON 之后可以秒级全文检索、按分类过滤，还能把命令一键送进游戏。

「指令台」不再单独存在，它是本档案的一个功能区：
档案管"查知识 / 查命令"，查到的命令直接进待送队列。
"""

from __future__ import annotations

import json
import re
import sys
from functools import lru_cache
from pathlib import Path


def _bundle_root() -> Path:
    """随程序分发的静态资源根目录。

    三种打包布局（与 ``smd/web`` 完全一致，所以共用同一套判断）：

    * 开发模式：``<项目>/smd``
    * PyInstaller onefile：解包到 ``sys._MEIPASS/smd``
    * PyInstaller onedir：``<exe目录>/_internal/smd``
    """
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        p = Path(meipass) / "smd"
        if p.is_dir():
            return p
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        for cand in (exe_dir / "_internal" / "smd", exe_dir / "smd"):
            if cand.is_dir():
                return cand
    return Path(__file__).resolve().parent


def _data_dir() -> Path:
    """wiki.json 所在目录。"""
    return _bundle_root() / "data"


@lru_cache(maxsize=1)
def load() -> dict:
    """读入档案（只读一次，之后缓存）。"""
    p = _data_dir() / "wiki.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"meta": {"entries": 0, "sections": 0}, "sections": []}


def meta() -> dict:
    """档案元信息（来源、版本、条目数）。"""
    return dict(load().get("meta") or {})


def sections() -> list[dict]:
    """全部条目分类（只给目录信息，不含行数据——那个太大）。"""
    out = []
    for s in load().get("sections") or []:
        out.append({
            "key": s.get("key", ""),
            "title": s.get("title", ""),
            "desc": s.get("desc", ""),
            "columns": list(s.get("columns") or []),
            "count": len(s.get("rows") or []),
        })
    return out


def section_rows(key: str, *, offset: int = 0, limit: int = 300) -> dict:
    """取某个分类的行（分页）。

    分页是必须的：全局修正有 2867 行、舰船组件 1427 行，
    一次全下发给界面会让 WebView 卡住。
    """
    for s in load().get("sections") or []:
        if s.get("key") != key:
            continue
        rows = s.get("rows") or []
        page = rows[offset:offset + limit] if limit else rows[offset:]
        return {"ok": True, "key": key, "title": s.get("title", ""),
                "desc": s.get("desc", ""), "columns": list(s.get("columns") or []),
                "rows": page, "offset": offset, "total": len(rows),
                "has_more": offset + len(page) < len(rows)}
    return {"ok": False, "error": f"没有这个分类：{key}"}


def search(query: str, *, limit: int = 200, match_all: bool = True) -> dict:
    """全文检索。

    命中规则（按优先级累计打分，分高的排前面）：
    * 任意单元格**完全等于**关键词   → 100 分（最精确）
    * 单元格**以关键词开头**         → 40 分
    * 单元格**包含**关键词           → 12 分
    * 该行任一格命中即可，分值取该行最高

    ``match_all=True``（默认）要求**所有关键词都命中**（AND）——
    搜"能源 代码"时只命中一个词的几千行没有意义。

    ``match_all=False`` 改成"命中任意一个即可"（OR）。这是给
    **自然语言提问**用的：用户问"怎么快速解锁全部科技"，
    按 AND 切词后一个条目都匹配不到（档案里没有"怎么"这种词），
    检索结果为空就等于白白浪费了一次 AI 调用。
    """
    q = (query or "").strip().lower()
    if not q:
        return {"ok": True, "query": "", "hits": [], "total": 0}
    terms = [t for t in q.split() if t]
    hits: list[dict] = []
    for s in load().get("sections") or []:
        for i, row in enumerate(s.get("rows") or []):
            cells = [str(c or "") for c in row]
            low = [c.lower() for c in cells]
            if match_all:
                if not all(any(t in c for c in low) for t in terms):
                    continue
            else:
                if not any(any(t in c for c in low) for t in terms):
                    continue
            score = 0
            for c in low:
                for t in terms:
                    if c == t:
                        score = max(score, 100)
                    elif c.startswith(t):
                        score = max(score, 40)
                    elif t in c:
                        score = max(score, 12)
            hits.append({
                "section": s.get("key", ""), "section_title": s.get("title", ""),
                "index": i, "cells": cells,
                "columns": list(s.get("columns") or []), "score": score,
            })
    hits.sort(key=lambda h: -h["score"])
    total = len(hits)
    return {"ok": True, "query": query, "hits": hits[:limit], "total": total}


@lru_cache(maxsize=1)
def _frags() -> frozenset[str]:
    """档案里出现过的所有 2~4 字中文片段（以及英文单词）。

    用途：把一句中文问题切成有意义的词。

    为什么不能用"整格"当词表：档案里的格子常常是一整句话，例如
    「设定国家已完成完整巨构（用于解锁星河奇迹）」——
    整格匹配的话，"解锁"这个词永远不在词表里，问题就切不出词来。

    所以要**从单元格内容里展开 n-gram**，这样"解锁""科技""无敌"
    只要在档案里真的出现过就都是合法词。

    代价：约 50 万个片段、几十 MB 内存、首次构建 <1 秒，
    且只算一次（``lru_cache``）。
    """
    frags: set[str] = set()
    for s in load().get("sections") or []:
        for row in s.get("rows") or []:
            for c in row:
                t = str(c or "").strip().lower()
                if not t or len(t) > 200:
                    continue
                for w in re.findall(r"[a-z_][a-z0-9_]{2,}", t):
                    frags.add(w)
                for run in re.findall(r"[\u4e00-\u9fff]+", t):
                    n = len(run)
                    for size in (4, 3, 2):
                        if n < size:
                            continue
                        for i in range(n - size + 1):
                            frags.add(run[i:i + size])
    return frozenset(frags)


def _question_terms(question: str, *, cap: int = 12) -> list[str]:
    """把一句话切成"档案里真的存在"的词，用来做 OR 检索。

    做法：
    * 英文/数字按单词切
    * 中文按 **4→3→2 字**滑窗切，只保留在 :func:`_frags` 里出现过的片段

    优先取长片段：4 字片段区分度远高于 2 字片段，
    先收长的话，"解锁全部科技" 会被切成 `解锁`、`科技`
    而不是一堆两字噪音。
    """
    q = (question or "").strip().lower()
    if not q:
        return []
    frags = _frags()
    out: list[str] = []

    for w in re.findall(r"[a-z_][a-z0-9_]{1,}", q):
        if len(w) >= 2 and w not in out:
            out.append(w)

    han = re.findall(r"[\u4e00-\u9fff]+", q)
    # 4→3→2 轮流取一轮，而不是"先取完 4 字再取 3 字"。
    # 后者在"解锁全部科技"上会把预算全花在 `解锁全部`/`解锁全`/`锁全部`
    # 这些互相重叠的 4 字片段上，反而挤掉了 `科技` 这种独立信息。
    per_size: dict[int, list[str]] = {}
    for size in (4, 3, 2):
        got: list[str] = []
        for run in han:
            for i in range(len(run) - size + 1):
                frag = run[i:i + size]
                if frag in frags and frag not in got:
                    got.append(frag)
        per_size[size] = got
    for rank in range(max((len(v) for v in per_size.values()), default=0)):
        for size in (4, 3, 2):
            if rank < len(per_size[size]):
                t = per_size[size][rank]
                if t not in out:
                    out.append(t)
        if len(out) >= cap:
            break
    return out[:cap]


def search_for_question(question: str, *, limit: int = 40) -> dict:
    """为一个**自然语言问题**找相关条目。

    两段式：

    1. 先用原句做 AND 检索（问题恰好由词条关键词组成时最精确）
    2. 一条都没有 → 切成词表里存在的词，改做 OR 检索

    这样"怎么快速解锁全部科技"能靠 `快速 / 解锁 / 科技`
    找到 ``research_all_technologies`` 这类条目。
    """
    first = search(question, limit=limit, match_all=True)
    if first["total"]:
        return first
    terms = _question_terms(question)
    if not terms:
        return first
    got = search(" ".join(terms), limit=max(limit * 4, 120), match_all=False)
    # OR 检索会把"只沾一个词"的行也捞进来。用**覆盖度**重排：
    # 命中词越多越靠前，这样问"怎么让舰队无敌"时，
    # 同时含"舰队"和"无敌"的条目排在只含"舰队"的前面。
    for h in got["hits"]:
        joined = " ".join(h["cells"]).lower()
        h["covered"] = sum(1 for t in terms if t in joined)
    got["hits"].sort(key=lambda h: (-h["covered"], -h["score"]))
    got["hits"] = got["hits"][:limit]
    got["terms"] = terms
    return got


def categories() -> list[dict]:
    """档案的顶层分类（供左侧导航）。"""
    out = []
    for s in sections():
        out.append({"key": s["key"], "title": s["title"], "count": s["count"]})
    return out


def console_commands() -> list[dict]:
    """从档案里提炼出**可直接送进游戏**的命令。

    只取「扩展代码」「组合代码」两类的"代码"列——
    其余分类是游戏内 key（科技 key、特质 key…），
    它们不是控制台命令，混进待送队列只会让人困惑。
    """
    out: list[dict] = []
    for s in load().get("sections") or []:
        key = s.get("key")
        if key not in ("console_ext", "console_run"):
            continue
        cols = s.get("columns") or []
        try:
            ci = cols.index("代码") if "代码" in cols else 2
        except ValueError:
            ci = 2
        for row in s.get("rows") or []:
            if ci >= len(row):
                continue
            cmd = str(row[ci] or "").strip()
            if not cmd:
                continue
            desc = ""
            for j, c in enumerate(row):
                if j != ci and c:
                    desc = str(c)
                    break
            out.append({"cmd": cmd, "desc": desc,
                        "section": s.get("title", ""),
                        "row": [str(x or "") for x in row]})
    return out


def export_json() -> str:
    """整份档案的 JSON 文本（供导出）。"""
    return json.dumps(load(), ensure_ascii=False, indent=1)
