"""待保存补丁的**唯一账本**。

为什么需要这个
--------------
补丁有三个来源：

* 本地规则批量生成（``generate_local_patches`` 的草稿）
* 「AI 生成补丁」按冲突条目生成
* AI 分析结论驱动生成的补丁（崩溃 / 日志 / 加载顺序）

原本这三处各自存一份，保存时按来源走不同的分支、各自维护
"存过没有"的标记。结果是同一个概念写了三遍，
加一个来源就要改四个地方——而补丁保存是**会写用户磁盘**的路径，
在那里留重复逻辑迟早出问题。

这里把它们统一成一张表：**一条待保存的补丁就是一个 Entry**，
不关心它从哪来。保存、计数、去重、隐藏都只看这张表。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class PatchEntry:
    """一条"已生成、待保存"的补丁。"""

    engine: str                 # ai | local
    target: str                 # 冲突条目 / 修改目标（稳定标识）
    title: str
    files: list[Any] = field(default_factory=list)   # PatchFile 列表
    conflict: str = ""
    description: str = ""
    provider: str = ""
    risk: str = ""
    # 保存前的硬性拒绝原因（为空表示可以保存）
    block: str = ""
    saved: bool = False
    # 用于把"这一条"映射回冲突候选列表，做"处理过的就别再显示"
    aliases: set[str] = field(default_factory=set)

    @property
    def savable(self) -> bool:
        """没有阻塞原因、也没保存过，才可以写盘。"""
        return not self.block and not self.saved

    def to_dict(self) -> dict:
        """序列化，供界面显示待保存列表。"""
        return {
            "engine": self.engine, "key": self.target, "title": self.title,
            "conflict": self.conflict, "files": len(self.files),
            "savable": self.savable, "reason": self.block,
            "risk": self.risk,
        }


class PatchLedger:
    """待保存补丁的账本。

    全部**只存在内存里**：这些是"这一轮生成、还没落盘"的中间产物，
    持久化的是保存后的补丁模组本身。软件关掉就清空才符合直觉。
    """

    def __init__(self) -> None:
        self._by_key: dict[tuple[str, str], PatchEntry] = {}
        self._order: list[tuple[str, str]] = []

    # -- 写入 --------------------------------------------------------
    def add(self, entry: PatchEntry) -> PatchEntry:
        """登记一条补丁；同 (engine, target) 会被**覆盖**（后生成的更新）。

        覆盖而不是并存：同一个目标反复生成时，用户要的是最新那一版，
        留着旧的会让"待保存 2 条"里有一条永远不该被保存。

        注意**不要**沿用旧条目的 ``saved``：重新生成出来的是一个
        待保存的新补丁。照搬旧标记会让它一登记就"已保存"，
        于是待保存列表空着、批量保存保存 0 个——看起来像功能坏了。
        """
        k = (entry.engine, entry.target)
        old = self._by_key.get(k)
        if old is not None:
            # 只保留别名（去重信息），saved 以新条目为准
            entry.aliases |= old.aliases
            entry.saved = False
        else:
            self._order.append(k)
        self._by_key[k] = entry
        return entry

    def get(self, engine: str, target: str) -> PatchEntry | None:
        """按来源与目标取一条。"""
        return self._by_key.get((engine, target))

    def mark_saved(self, engine: str, target: str) -> bool:
        """标记为已保存；返回是否确实找到了这条。"""
        e = self._by_key.get((engine, target))
        if e is None:
            return False
        e.saved = True
        return True

    def clear(self, engine: str = "") -> int:
        """清空账本（可按来源）。返回清掉的条数。"""
        if not engine:
            n = len(self._order)
            self._by_key.clear()
            self._order.clear()
            return n
        keys = [k for k in self._order if k[0] == engine]
        for k in keys:
            self._by_key.pop(k, None)
            self._order.remove(k)
        return len(keys)

    # -- 读取 --------------------------------------------------------
    def pending(self, engine: str = "") -> list[PatchEntry]:
        """列出来保存的（已保存的不再列出）。"""
        out = []
        for k in self._order:
            e = self._by_key.get(k)
            if e is None or e.saved:
                continue
            if engine and e.engine != engine:
                continue
            out.append(e)
        return out

    def all_entries(self) -> list[PatchEntry]:
        """全部条目（含已保存），供诊断与测试。"""
        return [self._by_key[k] for k in self._order if k in self._by_key]

    def __len__(self) -> int:
        return len(self._order)

    # -- 去重：这条冲突是不是已经被处理过 ----------------------------
    def is_handled(self, engine: str, *, target: str = "",
                   title: str = "") -> bool:
        """按目标判断某条冲突是否已经生成过补丁。

        ``target`` 与 ``title`` 都试：文件覆盖类候选给的是原文件名，
        而补丁文件名带 ``zzz_SMD_`` 前缀，两边对不上；
        对象重定义类候选给的是对象名，只有标题里才带它。
        所以登记时会同时记住几个别名，这里逐个比。
        """
        want = {(target or "").strip().lower(),
                (title or "").strip().lower()}
        want.discard("")
        if not want:
            return False
        for e in self._by_key.values():
            if e.engine != engine and engine:
                continue
            cands = {e.target.lower(), e.conflict.lower()} | {
                a.lower() for a in e.aliases}
            cands.discard("")
            for w in want:
                if w in cands:
                    return True
                # 末段比对：剥掉补丁前缀后再比一次，处理
                # ``common/x/zzz_SMD_auto_merge_a.txt`` vs ``common/x/a.txt``
                base = w.rsplit("/", 1)[-1]
                for c in cands:
                    if base and base == c.rsplit("/", 1)[-1]:
                        return True
                    if len(w) > 3 and ("/" in c or "." in c) and w in c:
                        return True
        return False
