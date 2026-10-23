"""大模型客户端：没 Key 的接口跳过、Key 错了冷却久一点、DeepSeek 关思考和 JSON 模式。"""
import time


def test_auth_error_cools_long():
    from aigroup.llm.client import Profile
    p = Profile("x", {"base_url": "http://127.0.0.1:1/v1", "model": "m", "api_key": "k"}, None)
    p.cool(RuntimeError("Error code: 401 - Authentication failed"))
    assert p.cool_until - time.time() > 3600
    p.cool(RuntimeError("Error code: 429 rate limit"))
    assert p.cool_until - time.time() < 60


def test_llm_skips_keyless_profiles(cfg):
    from aigroup.llm.client import LLM
    cfg["llm"] = {"profiles": {"a": {"base_url": "https://x/v1", "model": "m", "api_key": "sk-1"},
                               "nokey": {"base_url": "https://y/v1", "model": "m"},
                               "local": {"base_url": "http://127.0.0.1:11434/v1", "model": "q"}},
                  "routes": {"chat": ["a"], "helper": ["nokey", "local"], "memory": ["nokey"]}}
    llm = LLM(cfg)
    assert llm.routes["helper"] == ["local"] and llm.routes["memory"] == []
    assert [p.name for p in llm._chain("memory")] == ["local"] and "nokey" in llm.skipped


def test_deepseek_thinking_off_and_json_mode(cfg):
    from aigroup.llm.client import LLM
    cfg["llm"] = {"profiles": {"ds": {"base_url": "https://api.deepseek.com/v1", "model": "deepseek-flash", "api_key": "sk-1"},
                               "other": {"base_url": "https://x/v1", "model": "m", "api_key": "k"}},
                  "routes": {"chat": ["ds", "other"]}}
    llm = LLM(cfg)
    ds, other = llm.profiles["ds"], llm.profiles["other"]
    assert ds.extra_body["thinking"] == {"type": "disabled"} and "thinking" not in other.extra_body
    assert llm._kwargs(ds, 0.9, 100, json_mode=True)["response_format"] == {"type": "json_object"}
    assert "response_format" not in llm._kwargs(other, 0.9, 100, json_mode=True)

    class U:
        prompt_cache_hit_tokens, prompt_cache_miss_tokens, completion_tokens = 1000, 200, 50
    llm._record("chat", "ds", True, usage=U(), price=ds.price)
    st = llm.stats()
    assert st["tok_hit"] == 1000 and st["tok_out"] == 50 and st["usd"] > 0
