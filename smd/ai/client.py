"""AI 供应商适配层。

只用标准库 ``urllib`` 发请求，不引入 ``requests``/``openai`` 依赖——
打包体积小、没有版本冲突，也更容易审计「到底往哪发了什么」。

支持的协议
----------
``openai``     ``POST {base}/chat/completions``
               （OpenAI、DeepSeek、Moonshot、通义、智谱、OpenRouter、
                 vLLM、LM Studio、one-api 等一切兼容实现）
``anthropic``  ``POST {base}/v1/messages``
``ollama``     ``POST {base}/api/chat``（本地，无需密钥）
"""

from __future__ import annotations

import json
import socket
import ssl
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator

__all__ = [
    "AIError", "ChatMessage", "AIModel", "ProviderSpec", "PROVIDERS",
    "available_providers", "get_provider", "build_client", "list_models",
    "test_connection", "BaseClient",
]

USER_AGENT = "StellarisModDoctor/1.0 (+local mod manager)"
DEFAULT_TIMEOUT = 120


class AIError(Exception):
    """AI 调用相关的错误，带可读的中文说明。"""

    def __init__(self, message: str, *, status: int = 0, body: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.body = body


@dataclass
class ChatMessage:
    """一条对话消息（``role`` + ``content``）。"""
    role: str          # system | user | assistant
    content: str

    def to_dict(self) -> dict:
        """转成 OpenAI 协议要求的 ``{role, content}`` 结构。"""
        return {"role": self.role, "content": self.content}


@dataclass
class AIModel:
    """一个可选的模型。"""

    id: str
    name: str = ""
    context: int = 0

    def to_dict(self) -> dict:
        """序列化为界面用的字典。"""
        return {"id": self.id, "name": self.name or self.id,
                "context": self.context}


@dataclass
class ProviderSpec:
    """供应商描述。"""

    key: str
    name: str
    protocol: str                    # openai | anthropic | ollama
    base_url: str
    default_model: str = ""
    models: list[str] = field(default_factory=list)
    needs_key: bool = True
    docs: str = ""
    note: str = ""

    def to_dict(self) -> dict:
        """序列化为界面用的字典（供应商下拉框与说明）。"""
        return {
            "key": self.key, "name": self.name, "protocol": self.protocol,
            "base_url": self.base_url, "default_model": self.default_model,
            "models": list(self.models), "needs_key": self.needs_key,
            "docs": self.docs, "note": self.note,
        }


# ---------------------------------------------------------------------------
# 内置供应商
# ---------------------------------------------------------------------------

PROVIDERS: dict[str, ProviderSpec] = {
    "deepseek": ProviderSpec(
        key="deepseek", name="DeepSeek", protocol="openai",
        base_url="https://api.deepseek.com/v1",
        default_model="deepseek-chat",
        models=["deepseek-chat", "deepseek-reasoner"],
        docs="https://platform.deepseek.com/api_keys",
        note="性价比高，长上下文，适合做脚本合并。",
    ),
    "openai": ProviderSpec(
        key="openai", name="OpenAI", protocol="openai",
        base_url="https://api.openai.com/v1",
        default_model="gpt-4o-mini",
        # 推理模型（o 系列、GPT-6 系列）由 OpenAIClient.is_reasoning_model
        # 自动识别：它们不带 temperature，改带 reasoning_effort。
        models=["gpt-6-sol", "gpt-6.1-sol", "gpt-6-luna",
                "gpt-4o", "gpt-4o-mini", "gpt-4.1", "gpt-4.1-mini",
                "o4-mini"],
        docs="https://platform.openai.com/api-keys",
        note="GPT-6 Sol / Luna 是推理模型，会用 reasoning_effort "
             "控制思考深度；其余为普通聊天模型。",
    ),
    "anthropic": ProviderSpec(
        key="anthropic", name="Anthropic Claude", protocol="anthropic",
        base_url="https://api.anthropic.com",
        default_model="claude-sonnet-4-20250514",
        models=["claude-sonnet-4-20250514", "claude-3-7-sonnet-latest",
                "claude-3-5-haiku-latest"],
        docs="https://console.anthropic.com/settings/keys",
    ),
    "moonshot": ProviderSpec(
        key="moonshot", name="Moonshot 月之暗面 (Kimi)", protocol="openai",
        base_url="https://api.moonshot.cn/v1",
        default_model="moonshot-v1-32k",
        models=["moonshot-v1-8k", "moonshot-v1-32k", "moonshot-v1-128k"],
        docs="https://platform.moonshot.cn/console/api-keys",
        note="中文能力强。",
    ),
    "dashscope": ProviderSpec(
        key="dashscope", name="阿里通义千问", protocol="openai",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        default_model="qwen-plus",
        models=["qwen-plus", "qwen-max", "qwen-turbo", "qwen-long"],
        docs="https://bailian.console.aliyun.com/",
    ),
    "zhipu": ProviderSpec(
        key="zhipu", name="智谱 GLM", protocol="openai",
        base_url="https://open.bigmodel.cn/api/paas/v4",
        default_model="glm-4-plus",
        models=["glm-4-plus", "glm-4-air", "glm-4-flash"],
        docs="https://open.bigmodel.cn/usercenter/apikeys",
    ),
    "openrouter": ProviderSpec(
        key="openrouter", name="OpenRouter（聚合）", protocol="openai",
        base_url="https://openrouter.ai/api/v1",
        default_model="anthropic/claude-3.5-sonnet",
        models=[],
        docs="https://openrouter.ai/keys",
        note="一个密钥访问多家模型；模型名用 `厂商/模型` 格式。",
    ),
    "ollama": ProviderSpec(
        key="ollama", name="Ollama（本地）", protocol="ollama",
        base_url="http://127.0.0.1:11434",
        # 不预设具体模型名：用户装了哪个只有他自己知道。写死一个
        # 没装过的名字（例如 qwen2.5:14b）会让"测试连接"必然失败，
        # 而界面上看起来像是软件坏了。
        default_model="",
        models=[],
        needs_key=False,
        docs="https://ollama.com/download",
        note="完全离线、不联网、不需要密钥。"
             "必须先启动 Ollama（运行 `ollama serve`，或打开 Ollama 应用），"
             "再用「拉取模型」选择你**已经 pull 过**的模型；"
             "没有模型就先执行 `ollama pull <模型名>`。",
    ),
    "custom": ProviderSpec(
        key="custom", name="自定义（OpenAI 兼容）", protocol="openai",
        base_url="http://127.0.0.1:8000/v1",
        default_model="",
        models=[],
        needs_key=False,
        docs="",
        note="任何兼容 OpenAI /chat/completions 的服务，"
             "例如 LM Studio、vLLM、one-api、企业内网网关。",
    ),
}


def available_providers() -> list[dict]:
    """列出所有内置供应商（供界面渲染下拉框）。"""
    return [p.to_dict() for p in PROVIDERS.values()]


def get_provider(key: str) -> ProviderSpec:
    """按 key 取供应商描述；不存在时抛 :class:`AIError`。"""
    p = PROVIDERS.get(key)
    if p is None:
        raise AIError(f"未知的 AI 供应商：{key}")
    return p


# ---------------------------------------------------------------------------
# HTTP 基础
# ---------------------------------------------------------------------------

def _opener(verify_ssl: bool = True):
    if verify_ssl:
        return urllib.request.build_opener()
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=ctx))


def _request(url: str, payload: dict, headers: dict[str, str], *,
             timeout: int = DEFAULT_TIMEOUT, verify_ssl: bool = True,
             method: str = "POST") -> dict:
    """发一个 JSON 请求并解析 JSON 响应。"""
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    hdrs = {
        "Content-Type": "application/json; charset=utf-8",
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }
    hdrs.update(headers)
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with _opener(verify_ssl).open(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", "replace")[:1200]
        except Exception:
            pass
        msg = _explain_http_error(exc.code, body)
        raise AIError(msg, status=exc.code, body=body) from exc
    except urllib.error.URLError as exc:
        raise AIError(_explain_url_error(exc)) from exc
    except socket.timeout as exc:
        raise AIError(f"请求超时（{timeout}s）。模型可能在处理很长的输入，"
                      f"可以调大超时或减少发送的文件数量。") from exc
    except TimeoutError as exc:
        raise AIError(f"连接超时。请检查网络或代理设置。") from exc
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise AIError(f"服务返回的不是合法 JSON（前 200 字符）：{raw[:200]}",
                      body=raw[:400]) from exc


def _explain_http_error(code: int, body: str) -> str:
    """把 HTTP 错误码翻译成人话。"""
    detail = ""
    try:
        j = json.loads(body)
        err = j.get("error")
        if isinstance(err, dict):
            detail = str(err.get("message") or err.get("type") or "")
        elif err:
            detail = str(err)
        elif j.get("message"):
            detail = str(j["message"])
    except (ValueError, TypeError):
        detail = body[:200]

    base = {
        400: "请求格式被拒绝（模型名可能不对）",
        401: "认证失败 —— API Key 无效或已过期",
        402: "余额不足 —— 请到供应商后台充值",
        403: "无权限 —— 该 Key 不能访问这个模型，或地区受限",
        404: "接口地址不存在 —— 请检查 Base URL 是否少了 /v1",
        408: "服务端超时",
        413: "请求体过大 —— 请减少发送的文件内容",
        429: "触发限流 —— 请稍后再试，或降低请求频率",
        500: "供应商服务端错误",
        502: "网关错误（供应商侧问题）",
        503: "服务暂不可用（供应商侧问题）",
        504: "网关超时（供应商侧问题）",
    }.get(code, f"HTTP {code}")
    return f"{base}。" + (f"\n服务端说明：{detail}" if detail else "")


def _explain_url_error(exc: urllib.error.URLError) -> str:
    reason = getattr(exc, "reason", exc)
    s = str(reason)
    if "Name or service not known" in s or "getaddrinfo" in s:
        return ("域名解析失败 —— 请检查 Base URL 拼写，或当前网络是否需要代理。")
    if "Connection refused" in s or "积极拒绝" in s:
        return ("连接被拒绝 —— 目标地址没有服务在监听。"
                "若用 Ollama，请先确认它已启动（`ollama serve`）。")
    if "certificate" in s.lower() or "SSL" in s:
        return ("SSL 证书校验失败。若使用自建网关，可在设置里关闭"
                "「校验 SSL 证书」（仅在你信任该内网地址时）。")
    return f"网络请求失败：{s}"


# ---------------------------------------------------------------------------
# 客户端
# ---------------------------------------------------------------------------

class BaseClient:
    """所有供应商客户端的公共部分。"""

    def __init__(self, spec: ProviderSpec, *, api_key: str = "",
                 base_url: str = "", model: str = "",
                 temperature: float = 0.2, timeout: int = DEFAULT_TIMEOUT,
                 max_tokens: int = 8192, verify_ssl: bool = True,
                 extra_headers: dict[str, str] | None = None,
                 reasoning_effort: str = "") -> None:
        self.spec = spec
        self.api_key = (api_key or "").strip()
        self.base_url = (base_url or spec.base_url).rstrip("/")
        self.model = (model or spec.default_model).strip()
        self.temperature = float(temperature)
        self.timeout = int(timeout)
        self.max_tokens = int(max_tokens)
        self.verify_ssl = bool(verify_ssl)
        self.extra_headers = dict(extra_headers or {})
        # 推理模型的思考深度：none / low / medium / high / xhigh / max。
        # 留空时由客户端按模型决定（OpenAI 兼容侧默认 medium）。
        # 普通聊天模型忽略这个字段 —— 它们用 temperature。
        self.reasoning_effort = (reasoning_effort or "").strip().lower()

        if spec.needs_key and not self.api_key:
            raise AIError(f"{spec.name} 需要 API Key，请先在「AI 接入」里填写。")
        if not self.model and spec.protocol != "openai":
            raise AIError(f"{spec.name} 需要指定模型名称。")

    # -- 子类实现 ---------------------------------------------------
    def _chat_url(self) -> str:
        raise NotImplementedError

    def _headers(self) -> dict[str, str]:
        raise NotImplementedError

    def _payload(self, messages: list[ChatMessage], stream: bool) -> dict:
        raise NotImplementedError

    def _parse(self, resp: dict) -> str:
        raise NotImplementedError

    def _parse_models(self, resp: dict | list) -> list[AIModel]:
        return []

    # -- 公共接口 ---------------------------------------------------
    def chat(self, messages: list[ChatMessage], *, stream: bool = False,
             on_delta=None) -> str:
        """发一轮对话，返回助手回复文本。"""
        if not messages:
            raise AIError("消息列表为空")
        if stream and on_delta is not None:
            try:
                return self._chat_stream(messages, on_delta)
            except AIError:
                raise
            except Exception:
                # 流式解析失败就退回一次性请求，保证功能可用
                pass
        resp = _request(self._chat_url(), self._payload(messages, False),
                        self._headers(), timeout=self.timeout,
                        verify_ssl=self.verify_ssl)
        text = self._parse(resp)
        if not text.strip():
            raise AIError("模型返回了空内容。可能被内容策略拦截，"
                          "或 max_tokens 太小。")
        return text

    def _chat_stream(self, messages: list[ChatMessage], on_delta) -> str:
        """流式输出。

        要同时兼容两种格式：

        * **SSE**（OpenAI / Anthropic）：每行以 ``data:`` 开头，
          以 ``data: [DONE]`` 结束
        * **NDJSON**（Ollama）：每行就是一个完整 JSON 对象，没有前缀

        所以这里不假设前缀，逐行尝试解析 JSON，能解析就取增量文本。
        """
        payload = self._payload(messages, True)
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = dict(self._headers())
        headers["Accept"] = "text/event-stream, application/x-ndjson"
        req = urllib.request.Request(self._chat_url(), data=data,
                                     headers=headers, method="POST")
        chunks: list[str] = []
        with _opener(self.verify_ssl).open(req, timeout=self.timeout) as resp:
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                # SSE 前缀（可能存在，也可能不存在）
                if line.startswith("data:"):
                    line = line[5:].strip()
                if not line or line == "[DONE]":
                    if line == "[DONE]":
                        break
                    continue
                if not line.startswith("{"):
                    continue
                try:
                    evt = json.loads(line)
                except ValueError:
                    continue
                if isinstance(evt, dict) and evt.get("error"):
                    raise AIError(f"服务端在流中返回错误：{evt['error']}")
                delta = self._delta_text(evt)
                if delta:
                    chunks.append(delta)
                    try:
                        on_delta(delta)
                    except Exception:
                        pass
        text = "".join(chunks)
        if not text.strip():
            raise AIError("流式响应没有内容")
        return text

    def _delta_text(self, evt: dict) -> str:
        return ""

    def list_models(self) -> list[AIModel]:
        """尽力枚举可用模型；失败时返回内置列表。

        .. deprecated:: 用 :meth:`list_models_ex` 代替
           这个方法在请求失败时会**静默退回内置列表**，调用方无法区分
           「真的问到了」和「没问到、拿预设糊弄你」。界面上表现为
           「拉取到 N 个模型」但那些名字根本不是你的账号里的。
        """
        return self.list_models_ex()["models"]

    def list_models_ex(self) -> dict:
        """拉取模型列表，并**明确告知来源**。

        返回 ``{"models": [...], "source": "api"|"fallback", "error": str}``。

        ``source`` 是关键：``fallback`` 表示这次请求没成功，返回的是
        供应商预设里的常见模型名，仅供参考——不能当作「检测到了这些模型」。
        """
        fallback = [AIModel(id=m) for m in self.spec.models]
        if self.spec.protocol == "anthropic":
            # Anthropic 没有公开的 /models 端点，只能给预设
            return {"models": fallback, "source": "fallback", "error": ""}
        if self.spec.protocol == "ollama":
            url = f"{self.base_url}/api/tags"
        else:
            url = f"{self.base_url}/models"
        try:
            resp = _request(url, {}, self._headers(),
                            timeout=20, verify_ssl=self.verify_ssl,
                            method="GET")
        except AIError as exc:
            return {"models": fallback, "source": "fallback",
                    "error": str(exc)}
        parsed = self._parse_models(resp)
        if not parsed:
            return {"models": fallback, "source": "fallback",
                    "error": "接口有响应，但没有解析出任何模型"}
        return {"models": parsed, "source": "api", "error": ""}


class OpenAIClient(BaseClient):
    """OpenAI 兼容协议。"""

    def _chat_url(self) -> str:
        return f"{self.base_url}/chat/completions"

    def _headers(self) -> dict[str, str]:
        h = dict(self.extra_headers)
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    @staticmethod
    def is_reasoning_model(model: str) -> bool:
        """这个模型是不是"推理模型"（o 系列 / GPT-6 系列）。

        推理模型和普通聊天模型在参数上**不兼容**：
        它们拒绝 ``temperature`` / ``top_p`` 这类采样参数，
        要用 ``reasoning_effort`` 控制思考深度。混着发会直接 400。

        项目里原本无条件带 ``temperature``，而模型列表里又有 ``o4-mini``
        —— 也就是选它必然报错。这个判断把那条路修通。

        判据用**前缀/片段匹配**而不是白名单：新模型（如 ``gpt-6.1-sol``、
        ``o5``）出来时不用改代码就能用。
        """
        m = (model or "").lower().strip()
        if not m:
            return False
        # o 系列：o1 / o3 / o4-mini / o5…（正则限定"o + 数字"开头，
        # 避免把 llama、command 这类名字误判）
        if re.match(r"^o\d", m):
            return True
        # GPT-6 及以后的 Sol / Luna 家族
        if m.startswith("gpt-6") or m.startswith("gpt6"):
            return True
        return False

    def _payload(self, messages: list[ChatMessage], stream: bool,
                 ) -> dict[str, Any]:
        p: dict[str, Any] = {
            "model": self.model,
            "messages": [m.to_dict() for m in messages],
            "stream": bool(stream),
        }
        # 推理模型：不带采样参数，改带 reasoning_effort
        if self.is_reasoning_model(self.model):
            p["reasoning_effort"] = self.reasoning_effort or "medium"
        else:
            p["temperature"] = self.temperature
        if self.max_tokens:
            p["max_tokens"] = self.max_tokens
        return p

    def _parse(self, resp: dict) -> str:
        try:
            choices = resp["choices"]
            if not choices:
                raise KeyError("choices 为空")
            msg = choices[0].get("message") or {}
            content = msg.get("content")
            if content is None:
                # 有些实现把内容放在 text 字段
                content = choices[0].get("text", "")
            if isinstance(content, list):
                # 多模态分段格式
                content = "".join(
                    seg.get("text", "") for seg in content
                    if isinstance(seg, dict))
            return str(content or "")
        except (KeyError, IndexError, TypeError) as exc:
            raise AIError(f"无法解析响应结构：{str(resp)[:300]}") from exc

    def _delta_text(self, evt: dict) -> str:
        try:
            return evt["choices"][0].get("delta", {}).get("content") or ""
        except (KeyError, IndexError, TypeError):
            return ""

    def _parse_models(self, resp) -> list[AIModel]:
        out: list[AIModel] = []
        if isinstance(resp, dict):
            for m in resp.get("data") or []:
                if isinstance(m, dict) and m.get("id"):
                    out.append(AIModel(id=str(m["id"]),
                                       name=str(m.get("id"))))
        return sorted(out, key=lambda x: x.id)


class AnthropicClient(BaseClient):
    """Anthropic Messages 协议。"""

    def _chat_url(self) -> str:
        return f"{self.base_url}/v1/messages"

    def _headers(self) -> dict[str, str]:
        h = dict(self.extra_headers)
        h["x-api-key"] = self.api_key
        h["anthropic-version"] = "2023-06-01"
        return h

    def _payload(self, messages: list[ChatMessage], stream: bool) -> dict:
        # Anthropic 的 system 是顶层字段，不放在 messages 里
        system = "\n".join(m.content for m in messages if m.role == "system")
        convo = [m.to_dict() for m in messages if m.role != "system"]
        p: dict[str, Any] = {
            "model": self.model,
            "messages": convo,
            "max_tokens": self.max_tokens or 8192,
            "temperature": self.temperature,
        }
        if system:
            p["system"] = system
        if stream:
            p["stream"] = True
        return p

    def _parse(self, resp: dict) -> str:
        try:
            blocks = resp.get("content") or []
            return "".join(str(b.get("text", "")) for b in blocks
                           if isinstance(b, dict))
        except (AttributeError, TypeError) as exc:
            raise AIError(f"无法解析响应结构：{str(resp)[:300]}") from exc

    def _delta_text(self, evt: dict) -> str:
        try:
            if evt.get("type") == "content_block_delta":
                return evt.get("delta", {}).get("text") or ""
        except (AttributeError, TypeError):
            pass
        return ""


class OllamaClient(BaseClient):
    """Ollama 本地服务。"""

    def _chat_url(self) -> str:
        return f"{self.base_url}/api/chat"

    def _headers(self) -> dict[str, str]:
        return dict(self.extra_headers)

    def _payload(self, messages: list[ChatMessage], stream: bool) -> dict:
        return {
            "model": self.model,
            "messages": [m.to_dict() for m in messages],
            "stream": bool(stream),
            "options": {"temperature": self.temperature,
                        "num_predict": self.max_tokens or 8192},
        }

    def _parse(self, resp: dict) -> str:
        msg = resp.get("message") or {}
        return str(msg.get("content") or resp.get("response") or "")

    def _delta_text(self, evt: dict) -> str:
        return str((evt.get("message") or {}).get("content") or "")

    def _parse_models(self, resp) -> list[AIModel]:
        out: list[AIModel] = []
        if isinstance(resp, dict):
            for m in resp.get("models") or []:
                if isinstance(m, dict) and m.get("name"):
                    out.append(AIModel(id=str(m["name"]),
                                       name=str(m["name"])))
        return sorted(out, key=lambda x: x.id)


_CLIENTS = {
    "openai": OpenAIClient,
    "anthropic": AnthropicClient,
    "ollama": OllamaClient,
}


def build_client(cfg: dict) -> BaseClient:
    """按配置构造客户端。

    ``cfg`` 字段（来自 :class:`smd.config.AiSettings` 的 ``to_dict``）::

        provider, api_key, base_url, model, temperature,
        timeout, max_tokens, verify_ssl, extra_headers, reasoning_effort
    """
    key = str(cfg.get("provider") or "deepseek")
    spec = get_provider(key)
    cls = _CLIENTS.get(spec.protocol)
    if cls is None:
        raise AIError(f"供应商 {spec.name} 的协议 {spec.protocol} 暂不支持")
    return cls(
        spec,
        api_key=str(cfg.get("api_key") or ""),
        base_url=str(cfg.get("base_url") or ""),
        model=str(cfg.get("model") or ""),
        temperature=float(cfg.get("temperature", 0.2) or 0.2),
        timeout=int(cfg.get("timeout", DEFAULT_TIMEOUT) or DEFAULT_TIMEOUT),
        max_tokens=int(cfg.get("max_tokens", 8192) or 8192),
        verify_ssl=bool(cfg.get("verify_ssl", True)),
        extra_headers=cfg.get("extra_headers") or None,
        reasoning_effort=str(cfg.get("reasoning_effort") or ""),
    )


def list_models(cfg: dict) -> list[dict]:
    """列出该供应商可用的模型（仅模型清单，不带来源信息）。"""
    client = build_client(cfg)
    return [m.to_dict() for m in client.list_models()]


def list_models_ex(cfg: dict) -> dict:
    """拉取模型列表，并带上**来源**说明。

    返回 ``{"ok", "models", "source", "error", "provider"}``。

    ``source`` 为 ``"api"`` 才是真的从服务商问到的；
    ``"fallback"`` 表示请求失败，给的是内置预设，界面必须如实说明，
    不能显示成「检测到 N 个模型」。
    """
    try:
        client = build_client(cfg)
    except AIError as exc:
        return {"ok": False, "models": [], "source": "error",
                "error": str(exc), "provider": ""}
    res = client.list_models_ex()
    return {
        "ok": res["source"] == "api",
        "models": [m.to_dict() for m in res["models"]],
        "source": res["source"],
        "error": res.get("error", ""),
        "provider": client.spec.name,
        "base_url": client.base_url,
    }


def test_connection(cfg: dict) -> dict:
    """连通性测试：发一句极短的问候，验证密钥/地址/模型是否可用。"""
    t0 = time.time()
    try:
        client = build_client(cfg)
    except AIError as exc:
        return {"ok": False, "error": str(exc), "elapsed": 0.0}
    try:
        text = client.chat([
            ChatMessage("system", "你是一个连通性测试助手，只回复四个字。"),
            ChatMessage("user", "请回复：连接成功"),
        ])
        return {
            "ok": True,
            "provider": client.spec.name,
            "model": client.model,
            "reply": text.strip()[:200],
            "elapsed": round(time.time() - t0, 2),
        }
    except AIError as exc:
        return {"ok": False, "error": str(exc),
                "elapsed": round(time.time() - t0, 2)}
    except Exception as exc:  # 兜底，保证界面不会崩
        return {"ok": False, "error": f"未预期的错误：{exc!r}",
                "elapsed": round(time.time() - t0, 2)}
