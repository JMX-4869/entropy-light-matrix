"""补丁管理：AI 生成、玩家手写、落盘为模组、校验与维护。

对外暴露 :mod:`smd.patch.service` 里的全部能力。
"""

from __future__ import annotations

from .service import (PATCH_PREFIX, TEMPLATES, PatchRecord,  # noqa: F401
                      delete_patch, get_templates, list_patches,
                      make_patch_name, read_patch, save_patch,
                      update_patch_file, validate_text)

__all__ = [
    "PATCH_PREFIX", "TEMPLATES", "PatchRecord", "save_patch", "list_patches",
    "read_patch", "update_patch_file", "delete_patch", "validate_text",
    "get_templates", "make_patch_name",
]
