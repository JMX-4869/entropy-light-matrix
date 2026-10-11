"""AI 接入层：把「生成模组兼容补丁」这件事交给大模型。

设计要点
--------
1. **不绑定单一供应商**。用一个统一的 ``chat()`` 接口覆盖
   OpenAI 兼容协议（OpenAI / DeepSeek / 通义 / Kimi / 智谱 /
   OpenRouter / 本地 vLLM …）与 Anthropic Messages 协议，
   另外内置 Ollama 本地模型（完全离线、无需密钥）。

2. **绝不把用户数据发到未经同意的地方**。只有在用户显式配置并
   打开「允许发送模组内容」后才会真正联网；默认关闭。
   发出去的内容也经过裁剪（只发冲突涉及的那几个文件的相关片段）。

3. **AI 的输出必须被校验**。模型会编造不存在的触发器、写错括号、
   甚至返回半截代码。所以本模块只负责「拿到候选文本」，
   真正的可信度由 :mod:`smd.ai.patchgen` 的校验层决定。

子模块
------
``client``    HTTP 客户端与供应商适配
``prompt``    提示词构建（把冲突证据整理成模型能用的上下文）
``patchgen``  补丁生成 + 语法/引用校验（防幻觉）
"""

from __future__ import annotations

from .client import (AIError, AIModel, ChatMessage, ProviderSpec,  # noqa: F401
                     available_providers, build_client, list_models,
                     test_connection)
from .patchgen import (AiPatchOutcome, generate_patch_with_ai,  # noqa: F401
                       verify_patch, verify_patch_text)
from .prompt import build_patch_prompt, gather_conflict_context  # noqa: F401

__all__ = [
    "AIError", "AIModel", "ChatMessage", "ProviderSpec",
    "available_providers", "build_client", "list_models", "test_connection",
    "AiPatchOutcome", "generate_patch_with_ai", "verify_patch",
    "verify_patch_text", "build_patch_prompt", "gather_conflict_context",
]
