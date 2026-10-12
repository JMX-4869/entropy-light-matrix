"""Read-only multiplayer checksum triage using the installed game's manifest.

Matching files are suspects, not a reproduction of the game's final checksum:
load order, identical replacements and the host's mod set still matter.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .modinfo import Mod
from .paradox import ParseError, parse_string
from .paths import long_path


@dataclass(frozen=True)
class ChecksumRule:
    """One directory/extension entry from checksum_manifest.txt."""

    directory: str
    recursive: bool
    extension: str

    def matches(self, relative: str) -> bool:
        """Match directory boundaries, extension and recursion explicitly."""
        relative = relative.replace("\\", "/").lower()
        prefix = self.directory + "/"
        if not relative.startswith(prefix):
            return False
        tail = relative[len(prefix):]
        return tail.endswith(self.extension) and (self.recursive or "/" not in tail)


def read_rules(game_dir: str) -> tuple[list[ChecksumRule], str]:
    """Reject incomplete or unfamiliar manifests instead of guessing safety."""
    if not game_dir:
        return [], "manifest_missing"
    try:
        text = (Path(long_path(game_dir)) / "checksum_manifest.txt").read_text(
            encoding="utf-8-sig")
    except (OSError, UnicodeError):
        return [], "manifest_missing"
    blocks: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        if line.lower() == "directory":
            current = {}
            blocks.append(current)
            continue
        match = re.fullmatch(r'(name|sub_directories|file_extension)\s*=\s*(.+)', line)
        if current is None or not match or match[1] in current:
            return [], "manifest_invalid"
        current[match[1]] = match[2].strip().strip('"').lower().replace("\\", "/")
    rules = []
    for block in blocks:
        directory = block.get("name", "").strip("/")
        extension = block.get("file_extension", "")
        recursive = block.get("sub_directories", "")
        if (not directory or any(p in ("", ".", "..") for p in directory.split("/"))
                or ":" in directory or recursive not in ("yes", "no")
                or not re.fullmatch(r"\.[a-z0-9_]+", extension)):
            return [], "manifest_invalid"
        rules.append(ChecksumRule(directory, recursive == "yes", extension))
    return (rules, "") if rules else ([], "manifest_invalid")


def inspect_mod(mod: Mod, rules: list[ChecksumRule],
                should_stop: Callable[[], bool]) -> dict:
    """Collect bounded evidence without reading textures, music or script bodies."""
    row = {"key": mod.key, "name": mod.name, "status": "unknown",
           "hits": 0, "files": 0, "evidence": [], "issues": []}
    root = mod.content_dir()
    if not root:
        row["issues"].append("content_missing")
        return row
    base = Path(long_path(root))

    def hit(value: str) -> None:
        """Bound UI payloads even for overhaul mods with thousands of matches."""
        row["hits"] += 1
        if len(row["evidence"]) < 12:
            row["evidence"].append(value)

    # replace_path can suppress checksum-covered files without adding any files.
    descriptors = {base / "descriptor.mod"}
    if mod.mod_file:
        descriptors.add(Path(long_path(mod.mod_file)))
    for descriptor in sorted(descriptors):
        if should_stop():
            raise InterruptedError("checksum_cancelled")
        try:
            if not descriptor.exists() and descriptor == base / "descriptor.mod":
                continue
            raw = descriptor.read_text(encoding="utf-8-sig")
            tree = parse_string(raw, strict=True)
            if tree.scalar("archive"):
                row["issues"].append("archive_unsupported")
            for node in tree.get_all("replace_path"):
                path = str(node.value).lower().replace("\\", "/").strip("/")
                if any(path == rule.directory or rule.directory.startswith(path + "/")
                       or (rule.recursive and path.startswith(rule.directory + "/"))
                       for rule in rules):
                    hit("replace_path=" + path)
        except (OSError, UnicodeError, ParseError):
            row["issues"].append("descriptor_unreadable")

    def walk_error(error: OSError) -> None:
        """An unreadable subtree must never be reported as clear."""
        row["issues"].append("content_unreadable")

    for parent, directories, files in os.walk(base, onerror=walk_error, followlinks=False):
        if should_stop():
            raise InterruptedError("checksum_cancelled")
        for name in list(directories):
            child = Path(parent) / name
            if child.is_symlink() or getattr(child, "is_junction", lambda: False)():
                directories.remove(name)
                row["issues"].append("linked_content")
        for name in files:
            if should_stop():
                raise InterruptedError("checksum_cancelled")
            row["files"] += 1
            relative = (Path(parent) / name).relative_to(base).as_posix()
            if any(rule.matches(relative) for rule in rules):
                hit(relative)
    row["issues"] = sorted(set(row["issues"]))
    if not row["files"]:
        row["issues"].append("content_empty")
    row["status"] = "risk" if row["hits"] else ("unknown" if row["issues"] else "clear")
    return row


def scan_checksum(mods: list[Mod], game_dir: str, *,
                  should_stop: Callable[[], bool] = lambda: False,
                  progress: Callable[[str, float], None] = lambda name, value: None) -> dict:
    """Scan all supplied mods; no partial report is published after cancellation."""
    rules, issue = read_rules(game_dir)
    rows = []
    for index, mod in enumerate(mods):
        if should_stop():
            return {"ok": False, "cancelled": True}
        progress(mod.name, index / max(len(mods), 1))
        try:
            row = inspect_mod(mod, rules, should_stop) if rules else {
                "key": mod.key, "name": mod.name, "status": "unknown",
                "hits": 0, "files": 0, "evidence": [], "issues": [issue]}
        except InterruptedError:
            return {"ok": False, "cancelled": True}
        rows.append(row)
    return {"ok": True, "rows": rows, "game_dir": game_dir,
            "manifest": str(Path(game_dir) / "checksum_manifest.txt") if game_dir else "",
            "issue": issue, "rules": len(rules)}
