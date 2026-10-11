"""测试 AI 接入页：模型下拉、拉取来源标注、测试连接、未保存值生效。"""
import io
import json
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# --- 测试沙箱：必须在 import smd 之前生效，否则配置会被测试写脏 ---
sys.path.insert(0, str(Path(__file__).resolve().parent))
from sandbox import enter_sandbox          # noqa: E402
enter_sandbox()

from smd.app import App  # noqa: E402
from smd.ui import INDEX, WINDOW_KWARGS, WINDOW_TITLE  # noqa: E402
from smd.webui import WebBridge  # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"  [{'OK' if ok else '!!'}] {name}" + (f" — {detail}" if detail else ""))


def js(window, expr, timeout=40.0):
    slot = f"__a_{int(time.time() * 1e6) % 10**9}"
    window.evaluate_js(
        "(function(){\n"
        f"window.{slot}=undefined;\n"
        f"Promise.resolve({expr})\n"
        f".then(function(v){{window.{slot}={{ok:1,v:v}};}})\n"
        f".catch(function(e){{window.{slot}={{ok:0,e:String(e)}};}});\n"
        "return 1;\n})()")
    end = time.time() + timeout
    while time.time() < end:
        raw = window.evaluate_js(f"JSON.stringify(window.{slot}||null)")
        if raw and raw != "null":
            window.evaluate_js(f"delete window.{slot}")
            b = json.loads(raw)
            return b.get("v") if b.get("ok") else {"__error__": b.get("e")}
        time.sleep(0.1)
    return {"__error__": "timeout"}


def main() -> int:
    print("=" * 70)
    print("AI 接入页测试")
    print("=" * 70)

    # 备份用户真实的 AI 配置：下面的界面测试会调用
    # aiManualModel / aiProviderChanged 之类会**写盘**的接口，
    # 如果不还原，被测者自己的模型设置就被测试值覆盖了
    # （之前确实把用户的模型改成了测试用的 my-custom-model-v9）。
    from smd.config import Settings as _S
    _saved_ai = _S.load().ai.to_dict()
    print(f"（已备份当前 AI 配置：{_saved_ai.get('provider')} / "
          f"{_saved_ai.get('model')}）")

    try:
        return _run()
    finally:
        try:
            s = _S.load()
            for k, v in _saved_ai.items():
                if hasattr(s.ai, k):
                    setattr(s.ai, k, v)
            s.save()
            print(f"\n[清理] AI 配置已还原：{s.ai.provider} / {s.ai.model}")
        except Exception as exc:
            print(f"\n[清理] 还原 AI 配置失败：{exc}")


def _run() -> int:
    # ---------- 后端 ----------
    print("\n--- 1. 模型来源必须如实上报 ---")
    app = App()
    cfg = app.settings.ai.to_dict()
    bad = dict(cfg, provider="deepseek", api_key="sk-invalid-key-for-test",
               base_url="http://127.0.0.1:9/definitely-not-listening")
    r = app.ai_list_models(bad)
    check("请求失败时标记为 fallback", r.get("source") == "fallback",
          f"source={r.get('source')}")
    check("失败时 ok 为 False", r.get("ok") is False)
    check("仍然给出预设模型供参考", len(r.get("models") or []) > 0,
          f"{len(r.get('models') or [])} 个预设")
    check("带上失败原因", bool(r.get("error")), str(r.get("error"))[:60])

    print("\n--- 2. 未保存的密钥要能立即生效 ---")
    # 关键：界面上一填写就能测，不必先点保存。
    # 用一个必然连不上的地址，验证"确实用了我们传进去的值"。
    r2 = app.ai_list_models({"provider": "deepseek",
                             "base_url": "http://127.0.0.1:9/x",
                             "api_key": "sk-test"})
    check("override 的地址被采用", r2.get("source") == "fallback",
          f"source={r2.get('source')}")
    saved = app.settings.ai.base_url
    check("override 不写盘", app.settings.ai.base_url == saved,
          f"磁盘仍是 {saved!r}")

    print("\n--- 3. 供应商预设健全 ---")
    from smd.ai.client import available_providers
    provs = available_providers()
    check("供应商数量", len(provs) >= 9, f"{len(provs)} 个")
    check("每个都有 key 与名称",
          all(p.get("key") and p.get("name") for p in provs))
    ds = next((p for p in provs if p["key"] == "deepseek"), {})
    check("DeepSeek 预设了 2 个常见模型",
          len(ds.get("models") or []) == 2, str(ds.get("models")))
    check("预设带默认模型", bool(ds.get("default_model")),
          ds.get("default_model", ""))

    # ---------- 界面 ----------
    print("\n--- 4. 界面：模型下拉框 ---")
    import webview
    bridge = WebBridge(app)
    window = webview.create_window(WINDOW_TITLE, str(INDEX), js_api=bridge,
                                  width=1400, height=880,
                                  **WINDOW_KWARGS)
    bridge._window = window
    done = threading.Event()

    def worker():
        try:
            for _ in range(80):
                if js(window, "!!(window.pywebview && window.pywebview.api)") is True:
                    break
                time.sleep(0.25)
            for _ in range(60):
                if js(window, "Promise.resolve((S.mods||[]).length)") not in (0, None):
                    break
                time.sleep(0.5)
            js(window, "(function(){goPage('ai'); return 1;})()")
            time.sleep(2.5)

            tag = js(window, "Promise.resolve(String(document.getElementById("
                            "'aiModel').tagName))")
            check("模型是真正的下拉框（SELECT）", tag == "SELECT", str(tag))
            n = js(window, "Promise.resolve(document.getElementById("
                           "'aiModel').options.length)")
            check("下拉框有候选项", isinstance(n, int) and n > 0, f"{n} 项")
            val = js(window, "Promise.resolve(document.getElementById("
                             "'aiModel').value)")
            check("有当前选中的模型", bool(val), str(val))
            check("不再是 datalist",
                  js(window, "Promise.resolve(!!document.getElementById("
                             "'aiModelList'))") is False)

            print("\n--- 5. 界面上能换模型 ---")
            # 不假设候选数量：本地 Ollama 可能只 pull 了一个模型，
            # 那样"切换"本身无从谈起。先确保至少有两个，再验证能切。
            js(window, "(function(){"
                       "var s=document.getElementById('aiModel');"
                       "if(s.options.length<2){"
                       "  var o=document.createElement('option');"
                       "  o.value='zz-test-model'; o.textContent='zz-test-model';"
                       "  s.appendChild(o);"
                       "}"
                       "return 1;})()")
            time.sleep(0.5)
            before = js(window, "Promise.resolve(document.getElementById("
                                "'aiModel').value)")
            js(window, "(function(){var s=document.getElementById('aiModel');"
                       "s.selectedIndex=1;"
                       "s.dispatchEvent(new Event('change'));return 1;})()")
            time.sleep(0.8)
            after = js(window, "Promise.resolve(document.getElementById("
                               "'aiModel').value)")
            check("切换选项后值真的变了", after != before,
                  f"{before} → {after}")
            # 切回原来的
            js(window, f"(function(){{var s=document.getElementById('aiModel');"
                       f"s.value={json.dumps(before)};return 1;}})()")

            print("\n--- 6. 手动填写模型 ---")
            js(window, "env.aiManualModel('my-custom-model-v9')")
            time.sleep(1.2)
            okman = js(window, "Promise.resolve(!![...document.getElementById("
                               "'aiModel').options].find(o=>o.value==="
                               "'my-custom-model-v9'))")
            check("手动填写的模型进入下拉框", okman is True)
            vman = js(window, "Promise.resolve(document.getElementById("
                              "'aiModel').value)")
            check("并自动选中它", vman == "my-custom-model-v9", str(vman))

            print("\n--- 7. 拉取失败必须如实提示 ---")
            js(window, "(function(){"
                       "document.getElementById('aiBase').value="
                       "'http://127.0.0.1:9/nope';"
                       "document.getElementById('aiKey').value='sk-x';"
                       "return 1;})()")
            js(window, "env.aiLoadModels()")
            time.sleep(9.0)
            out = js(window, "Promise.resolve(document.getElementById("
                             "'aiTestOut').innerText)")
            check("提示里出现失败标记", "✗" in str(out), str(out)[:70])
            check("明确说明是内置预设、不代表账号里有",
                  "内置预设" in str(out) and "不代表" in str(out),
                  str(out)[:90])
            check("没有谎报「检测到 N 个模型」",
                  "检测到" not in str(out) and "拉到 2 个" not in str(out))

            print("\n--- 8. 状态角标含义 ---")
            badge = js(window, "Promise.resolve(document.getElementById("
                               "'cAi').textContent)")
            title = js(window, "Promise.resolve(document.getElementById("
                               "'cAi').getAttribute('title')||'')")
            check("角标有文字", badge in ("待测", "✓", "未配置"), str(badge))
            check("角标有解释性提示", len(str(title)) > 4, str(title)[:50])
        except Exception as exc:
            import traceback
            print("异常:", exc)
            traceback.print_exc()
        finally:
            done.set()

    def watcher():
        done.wait(timeout=300)
        time.sleep(0.5)
        try:
            window.destroy()
        except Exception:
            pass

    window.events.loaded += lambda: threading.Thread(
        target=worker, daemon=True).start()
    threading.Thread(target=watcher, daemon=True).start()
    webview.start(gui="edgechromium" if sys.platform.startswith("win") else None)

    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print()
    print("=" * 70)
    print(f"结果：{passed}/{total} 通过")
    for n, ok, d in results:
        if not ok:
            print(f"  失败：{n} — {d}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
