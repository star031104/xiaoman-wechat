"""自定义大模型：地址规整、列模型、试聊、写 config.llm.yaml、和 config.yaml 合并。（本机假接口，不联网）"""
import asyncio
import json

import yaml

import aigroup.llm.custom as L


def test_pure_helpers():
    assert L.normalize_url("api.deepseek.com/v1/") == "https://api.deepseek.com/v1"
    assert L.normalize_url("127.0.0.1:11434/v1/chat/completions") == "http://127.0.0.1:11434/v1"
    assert L.url_candidates("https://api.x.com") == ["https://api.x.com", "https://api.x.com/v1"]
    assert L.url_candidates("https://open.bigmodel.cn/api/paas/v4") == ["https://open.bigmodel.cn/api/paas/v4"]
    assert L.profile_name("https://api.deepseek.com/v1", "deepseek-chat") == "my_deepseek_deepseek_chat"
    assert L.profile_name("https://api.deepseek.com/v1", "deepseek-chat", {"my_deepseek_deepseek_chat"}).endswith("_2")
    assert L.chat_models(["BAAI/bge-m3", "Qwen/Qwen3-8B", "FunAudioLLM/CosyVoice2"]) == ["Qwen/Qwen3-8B"]
    r = L.new_routes({"chat": ["a", "b"], "helper": ["c"]}, "x", "first", ("helper", "memory", "night"))
    assert r == {"chat": ["x", "a", "b"], "helper": ["x", "c"], "memory": ["x", "c"], "night": ["x", "c"]}
    assert L.new_routes({"chat": ["a"]}, "x", "last", ("helper",)) == {"chat": ["a", "x"]}
    r = L.new_routes({"chat": ["a"], "helper": ["c"]}, "x", "background", ("helper", "memory", "night"))
    assert r == {"helper": ["x", "c"], "memory": ["x", "c"], "night": ["x", "c"]}
    merged = L.merge_custom({"llm": {"profiles": {"a": {}}, "routes": {"chat": ["a"], "helper": ["a"]}}},
                            {"llm": {"profiles": {"x": {"model": "m"}}, "routes": {"chat": ["x", "a"]}}})
    assert set(merged["llm"]["profiles"]) == {"a", "x"} and merged["llm"]["routes"] == {"chat": ["x", "a"], "helper": ["a"]}


async def _fake_server(models_status=200, reply="你好呀"):
    from aiohttp import web

    async def models(req):
        if req.headers.get("Authorization") != "Bearer sk-ok":
            return web.json_response({"error": "bad key"}, status=401)
        return web.json_response({"object": "list", "data": [{"id": "good-chat"}, {"id": "BAAI/bge-m3"}]},
                                 status=models_status)

    async def chat(req):
        d = await req.json()
        txt = reply if d.get("model") == "good-chat" else ""
        if d.get("enable_thinking") is False:
            txt = "关掉思考就好了"
        return web.json_response({"id": "1", "object": "chat.completion", "created": 0, "model": d["model"],
                                  "choices": [{"index": 0, "finish_reason": "stop",
                                               "message": {"role": "assistant", "content": txt}}]})
    app = web.Application()
    app.router.add_get("/v1/models", models)
    app.router.add_post("/v1/chat/completions", chat)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    return runner, site._server.sockets[0].getsockname()[1]


def test_probe_against_fake_server():
    async def go():
        runner, port = await _fake_server()
        base = f"http://127.0.0.1:{port}"
        models, url, via, err = await L.list_models(base, "sk-ok")          # 没写 /v1：自动补上
        assert url == base + "/v1" and L.chat_models(models) == ["good-chat"] and not err
        models, url, via, err = await L.list_models(base + "/v1", "sk-bad")
        assert not models and "Key" in err
        ok, secs, text, extra = await L.probe_model(url, "sk-ok", "good-chat")
        assert ok and text == "你好呀" and extra is None
        ok, secs, text, extra = await L.probe_model(url, "sk-ok", "thinker")   # 回复为空：关掉思考再试
        assert ok and extra == {"enable_thinking": False}
        await runner.cleanup()
    asyncio.run(go())


def test_save_and_load(tmp_path, monkeypatch):
    from aigroup.core import config as C
    monkeypatch.setattr(C, "ROOT", tmp_path)
    (tmp_path / "config.wechat.example.yaml").write_text(
        (C.Path(L.__file__).resolve().parents[2] / "config.wechat.example.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "config.yaml").write_text("persona_file: persona/xiaoman.md\n", encoding="utf-8")
    cfg = C.load_config(tmp_path / "config.yaml")
    name, routes = L.save_profile(cfg, url="https://api.deepseek.com/v1", key="sk-secret", model="deepseek-chat",
                                  name="", how="first", roles_bg=("helper",), root=tmp_path)
    data = yaml.safe_load((tmp_path / "config.llm.yaml").read_text(encoding="utf-8"))
    prof = data["llm"]["profiles"][name]
    assert prof["api_key"] == "${LLM_MY_DEEPSEEK_DEEPSEEK_CHAT_API_KEY}" and "sk-secret" not in json.dumps(data)
    assert "sk-secret" in (tmp_path / ".env").read_text(encoding="utf-8")
    assert routes["chat"][0] == name
    cfg2 = C.load_config(tmp_path / "config.yaml")                         # 重新读：合并进来、Key 从 .env 取
    assert cfg2.llm.profiles[name]["api_key"] == "sk-secret" and cfg2.llm.routes.chat[0] == name
    from aigroup.llm.client import LLM
    assert name in LLM(cfg2).profiles
    assert L.remove_profile(name, tmp_path) and name not in C.load_config(tmp_path / "config.yaml").llm.profiles


def test_no_preset_interfaces():
    """仓库里不预置任何接口：示例配置是空的；没配接口时 LLM 不崩，说话时给出“去 llm.bat 加”的提示。"""
    import asyncio

    import pytest
    import yaml

    from aigroup.core.config import ROOT, load_config
    from aigroup.llm.client import LLM
    ex = yaml.safe_load((ROOT / "config.wechat.example.yaml").read_text(encoding="utf-8"))
    assert not ex["llm"]["profiles"] and not ex["llm"]["routes"]["chat"] and "keys" not in ex
    c = load_config("nope.yaml")
    c["llm"] = {**c["llm"], "profiles": {}, "routes": {"chat": []}}     # 本机可能有自己加的 config.llm.yaml
    llm = LLM(c)
    assert not llm.ready
    with pytest.raises(RuntimeError, match="llm.bat"):
        asyncio.run(llm.chat([{"role": "user", "content": "hi"}]))
