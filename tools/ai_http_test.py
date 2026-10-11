"""用一个本地假 AI 服务验证完整的 HTTP 调用链路。

不需要真实 API Key 就能验证：

* 请求构造是否正确（URL / 头 / 消息体）
* 响应解析是否正确（OpenAI 与 Ollama 两种协议）
* 流式 SSE 解析
* 错误码翻译（401 / 404 / 429 …）
* 「生成 → 解析 → 校验」端到端是否能跑通

用法::  python tools/ai_http_test.py
"""

from __future__ import annotations

import io
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from smd.ai.client import (AIError, ChatMessage, build_client,  # noqa: E402
                           test_connection)

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"  [{'OK' if ok else '!!'}] {name}" + (f" — {detail}" if detail else ""))


# 假模型返回的「补丁」，故意包含一个真实语法结构
FAKE_PATCH = """### 说明
两个模组都想改同一个触发器，我用 OR 把双方条件合并。

### 补丁文件
```path: common/scripted_triggers/zzz_smd_patch.txt
my_trigger = {
\tOR = {
\t\thas_technology = "tech_x"
\t\tnum_pops > 25
\t}
}
```

### 风险评估
- 置信度：中
- 影响面：只影响 my_trigger
- 回退方式：删除本模组
"""

CAPTURED: list[dict] = []


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        if self.path.endswith("/models"):
            return self._json({"data": [
                {"id": "mock-model-a"}, {"id": "mock-model-b"}]})
        if self.path.endswith("/api/tags"):
            return self._json({"models": [{"name": "qwen2.5:14b"}]})
        if self.path.endswith("/fail401"):
            return self._json({"error": {"message": "Invalid API key"}}, 401)
        if self.path.endswith("/fail429"):
            return self._json({"error": {"message": "rate limit"}}, 429)
        self._json({"error": "not found"}, 404)

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n).decode("utf-8", "replace")
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = {}
        CAPTURED.append({
            "path": self.path,
            "auth": self.headers.get("Authorization", ""),
            "payload": payload,
        })

        if "/fail401" in self.path:
            return self._json({"error": {"message": "Invalid API key"}}, 401)
        if "/fail429" in self.path:
            return self._json({"error": {"message": "slow down"}}, 429)
        if "/fail404" in self.path:
            return self._json({"error": "not found"}, 404)

        if self.path.endswith("/api/chat"):        # Ollama 协议
            if payload.get("stream"):
                return self._sse_ollama()
            return self._json({"message": {"role": "assistant",
                                           "content": FAKE_PATCH}})

        # OpenAI 协议
        if payload.get("stream"):
            return self._sse_openai()
        return self._json({
            "choices": [{"message": {"role": "assistant",
                                     "content": FAKE_PATCH}}],
            "usage": {"total_tokens": 123},
        })

    def _sse_openai(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for i in range(0, len(FAKE_PATCH), 60):
            chunk = FAKE_PATCH[i:i + 60]
            evt = {"choices": [{"delta": {"content": chunk}}]}
            self.wfile.write(f"data: {json.dumps(evt, ensure_ascii=False)}\n\n"
                             .encode("utf-8"))
            self.wfile.flush()
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def _sse_ollama(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for i in range(0, len(FAKE_PATCH), 60):
            evt = {"message": {"content": FAKE_PATCH[i:i + 60]}}
            self.wfile.write((json.dumps(evt, ensure_ascii=False) + "\n")
                             .encode("utf-8"))
            self.wfile.flush()


def main() -> int:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    print("=" * 64)
    print(f"AI HTTP 链路测试（本地假服务 {base}）")
    print("=" * 64)

    # ---------- 1. OpenAI 兼容协议 ----------
    print("\n--- 1. OpenAI 兼容协议 ---")
    cfg = {"provider": "custom", "base_url": base, "model": "mock-model-a",
           "api_key": "sk-test-123", "timeout": 20}
    c = build_client(cfg)
    text = c.chat([ChatMessage("system", "sys"), ChatMessage("user", "hi")])
    check("非流式调用成功", "my_trigger" in text, f"{len(text)} 字符")
    last = CAPTURED[-1]
    check("请求发到了正确路径",
          last["path"].endswith("/chat/completions"), last["path"])
    check("Authorization 头正确",
          last["auth"] == "Bearer sk-test-123", last["auth"][:24])
    check("model 字段正确", last["payload"].get("model") == "mock-model-a")
    check("messages 结构正确",
          [m["role"] for m in last["payload"].get("messages", [])]
          == ["system", "user"])

    # ---------- 2. 流式 ----------
    print("\n--- 2. 流式（SSE）---")
    deltas: list[str] = []
    text2 = c.chat([ChatMessage("user", "hi")], stream=True,
                   on_delta=lambda d: deltas.append(d))
    check("流式调用成功", "my_trigger" in text2, f"{len(text2)} 字符")
    check("收到多个增量片段", len(deltas) > 3, f"{len(deltas)} 段")
    check("流式与整体内容一致", text2.strip() == text.strip())

    # ---------- 3. Ollama 协议 ----------
    print("\n--- 3. Ollama 协议 ---")
    cfg2 = {"provider": "ollama", "base_url": base, "model": "qwen2.5:14b",
            "timeout": 20}
    c2 = build_client(cfg2)
    t3 = c2.chat([ChatMessage("user", "hi")])
    check("Ollama 非流式成功", "my_trigger" in t3)
    check("Ollama 走 /api/chat",
          CAPTURED[-1]["path"].endswith("/api/chat"), CAPTURED[-1]["path"])
    check("Ollama 不需要 Authorization", CAPTURED[-1]["auth"] == "")

    d2: list[str] = []
    t4 = c2.chat([ChatMessage("user", "hi")], stream=True,
                 on_delta=lambda d: d2.append(d))
    check("Ollama 流式成功", "my_trigger" in t4, f"{len(d2)} 段")

    # ---------- 4. 模型列表 ----------
    print("\n--- 4. 模型列表 ---")
    ms = c.list_models()
    check("OpenAI 协议枚举模型", len(ms) == 2,
          "、".join(m.id for m in ms))
    ms2 = c2.list_models()
    check("Ollama 枚举模型", len(ms2) == 1, ms2[0].id if ms2 else "")

    # ---------- 5. 错误翻译 ----------
    print("\n--- 5. 错误码翻译 ---")
    for path, want in (("/fail401", "认证失败"), ("/fail429", "限流")):
        try:
            build_client({"provider": "custom", "base_url": base + path,
                          "model": "m", "api_key": "k", "timeout": 10}
                         ).chat([ChatMessage("user", "x")])
            check(f"错误 {path} 被抛出", False, "没有抛异常")
        except AIError as e:
            check(f"错误 {path} 被翻译", want in str(e), str(e)[:52])

    # 404：客户端会在 base 后追加 /chat/completions，
    # 所以让路径里包含该后缀才能真正落到「未知路径」分支
    try:
        build_client({"provider": "custom",
                      "base_url": base + "/fail404/chat/completions",
                      "model": "m", "api_key": "k", "timeout": 10}
                     ).chat([ChatMessage("user", "x")])
        check("404 被翻译", False, "没有抛异常")
    except AIError as e:
        check("404 被翻译", "接口地址不存在" in str(e), str(e)[:56])

    # ---------- 6. 连通性测试 ----------
    print("\n--- 6. 连通性测试 ---")
    r = test_connection(cfg)
    check("test_connection 成功", r.get("ok"), f"{r.get('elapsed')}s")

    # ---------- 7. 端到端：生成 → 解析 → 校验 ----------
    print("\n--- 7. 端到端：生成 → 解析 → 校验 ---")
    from smd.ai.patchgen import (generate_patch_with_ai,  # noqa: E402
                                 parse_ai_response)
    from smd.ai.prompt import ConflictContext
    ctx = ConflictContext(kind="S1", severity="critical",
                          title="测试冲突", target="my_trigger",
                          category="scripted_trigger",
                          mods=["ugc:1", "ugc:2"],
                          mod_names=["模组甲", "模组乙"])
    out = generate_patch_with_ai(ctx, cfg=cfg)
    check("端到端生成成功", out.ok, out.summary())
    check("解析出补丁文件", len(out.files) == 1,
          out.files[0].rel if out.files else "无")
    check("识别置信度=中", out.confidence == "medium", out.confidence)
    check("校验通过", out.verified,
          f"阻塞问题 {len(out.blocking)} 个")
    check("拿到说明文本", "OR" in out.explanation or "合并" in out.explanation,
          out.explanation[:34])
    check("拿到风险评估", "置信度" in out.risk, out.risk[:26])
    check("记录了耗时与模型", out.elapsed > 0 and out.model == "mock-model-a",
          f"{out.provider}/{out.model} {out.elapsed:.2f}s")

    # ---------- 8. 坏补丁会被拦 ----------
    print("\n--- 8. 模型返回坏补丁时能否拦住 ---")
    global FAKE_PATCH
    good_backup = FAKE_PATCH
    FAKE_PATCH = """### 补丁文件
```path: common/scripted_triggers/zzz_smd_bad.txt
my_trigger = {
\tOR = {
\t\thas_technology = "tech_x"
```
"""
    out2 = generate_patch_with_ai(ctx, cfg=cfg)
    check("坏补丁被判为不通过", not out2.ok, out2.summary())
    check("列出了阻塞问题", len(out2.blocking) >= 1,
          out2.blocking[0].message[:52] if out2.blocking else "")
    FAKE_PATCH = good_backup

    srv.shutdown()
    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print()
    print("=" * 64)
    print(f"结果：{passed}/{total} 通过")
    for n, ok, d in results:
        if not ok:
            print(f"  失败：{n} — {d}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
