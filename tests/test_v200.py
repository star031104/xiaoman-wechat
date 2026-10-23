"""20.0：拿不准时“理由 + 动作”、三档标注 + 批量 + 弱标签、打字时话题走了就不发、DeepSeek 搜索。"""
import asyncio
import json
import time

from conftest import GROUP, FakeLLM, _gc


def test_parse_judge():
    from aigroup.chat.speak import parse_judge
    assert parse_judge('{"why": "小王在问大家", "act": "回答"}') == ("回答", "小王在问大家")
    assert parse_judge('好的 {"why":"两个人在约","act":"不说"}')[0] == "不说"
    assert parse_judge("说")[0] == "接住" and parse_judge("等")[0] == "等" and parse_judge("乱七八糟")[0] == "不说"


def test_quick_judge_act_and_examples(cfg, brain):
    seen = {}

    class D(FakeLLM):
        async def chat(self, messages, **kw):
            c = messages[-1]["content"]
            if "动作只能选一个" in c:
                seen["p"] = c
                return '{"why": "小王在问大家，她知道", "act": "回答"}'
            return await super().chat(messages, **kw)
    brain.llm = D()
    gc, ob = _gc(cfg, brain)
    gc._record(GROUP, 88888, "小王", "今晚谁打瓦", 1)
    gid = gc.gate.add(GROUP, False, 0.3, 0.4, False, {})
    brain.db.conn.execute("UPDATE qq_gate_log SET human=0 WHERE id=?", (gid,))
    brain.db.conn.commit()
    gc._record(GROUP, 88888, "小王", "有人知道这个怎么弄吗", 2)
    assert asyncio.run(gc.quick_judge(GROUP)) == "say"
    assert gc._judged[GROUP]["act"] == "回答"
    assert "今晚谁打瓦" in seen["p"] and "不该插" in seen["p"]           # 你判过的例子进了 few-shot


def test_label_three_levels_batch_weak(cfg, brain):
    from aigroup.chat.gate import labeled_rows
    from aigroup.eval.label import answer, batch, stats, weak_labels
    gc, ob = _gc(cfg, brain)
    gc._record(GROUP, 88888, "小王", "有人打球吗", 1)
    ids = [gc.gate.add(GROUP, False, 0.3 + i / 100, 0.35, i % 2 == 0, {"question": 1.0}) for i in range(6)]
    for i in ids[:3]:
        gc.gate.mark(i, spoke=True)
    items = batch(brain.db, GROUP, n=10)
    assert len(items) == 6 and all(it["steps"] for it in items)
    answer(brain.db, ids[0], 1, "回答")
    answer(brain.db, ids[1], 2)
    answer(brain.db, ids[2], 0, "回答")                 # 不该插时“怎么接”不存
    s = stats(brain.db)
    assert s["n"] == 2 and s["n2"] == 1 and s["n1"] == 1 and s["n0"] == 1
    r = brain.db.conn.execute("SELECT human_act FROM qq_gate_log WHERE id=?", (ids[2],)).fetchone()
    assert r["human_act"] == ""
    assert not any(w == 2.0 and y not in (0.0, 1.0) for _, y, w in labeled_rows(brain.db))
    # 弱标签：她说了、没人接
    g4 = gc.gate.add(GROUP, False, 0.5, 0.35, True, {})
    gc.gate.mark(g4, spoke=True)
    brain.db.conn.execute("INSERT INTO qq_reply_effect(ts, group_id, gate_id, mentioned, done, score) VALUES (?,?,?,0,1,0)",
                          (time.time(), GROUP, g4))
    brain.db.conn.commit()
    assert weak_labels(brain.db) == 1
    assert stats(brain.db)["w0"] == 1


def test_steps_wording():
    from aigroup.eval.label import steps
    r = {"p": 0.3, "threshold": 0.5, "feats": json.dumps({"reply_other": 1, "n5m": 20}), "explore": 0, "act": "不说",
         "why": "两个人在约时间", "judge": "skip", "passed": 0, "spoke": 0}
    out = steps(r)
    assert "没到门槛" in out[0] and "在回复别人" in out[0] and "群里正热闹" in out[0]
    assert "两个人在约时间" in out[1] and "没开口" in out[-1]


def test_typed_over_drops_unprompted(cfg, brain):
    gc, ob = _gc(cfg, brain)
    gc._record(GROUP, 88888, "小王", "a", 1)
    last = int(brain.db.conn.execute("SELECT MAX(id) m FROM qq_group_messages").fetchone()["m"])
    assert not gc._typed_over(GROUP, last)
    for i in range(3):
        gc._record(GROUP, 88888, "小王", f"新的{i}", 10 + i)
    assert gc._typed_over(GROUP, last)


def test_deepseek_search(monkeypatch, cfg):
    import httpx

    from aigroup.net import search as S
    from aigroup.agent.mixin import format_results
    cfg["net"] = {**dict(cfg.get("net") or {}), "deepseek_key": "sk-test", "tavily_key": ""}
    w = S.WebSearch(cfg)
    assert w.ds_key == "sk-test" and not w.ds_first          # 20.4.3 默认 Tavily 在前（这里没配 Tavily，直接 DeepSeek）
    sent = {}

    class R:
        status_code = 200
        text = ""

        def raise_for_status(self):
            pass

        def json(self):
            return {"results": [
                {"title": "低分", "url": "https://a.com/1", "summary": "x" * 50, "rerank_score": 0.5, "authority": 1,
                 "date": "2026-10-01T02:00:00Z", "site_name": "A"},
                {"title": "aspas 合同", "url": "https://www.vlr.gg/p", "summary": "合同到 2027", "rerank_score": 2.1,
                 "authority": 3, "date": "2026-10-18T02:00:00Z", "site_name": "VLR"}]}

    class C:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            if "body" not in sent:                       # 第一次的请求（结果不够 3 条时会再不带日期搜一次）
                sent.update(url=url, body=json, h=headers)
            sent.setdefault("all", []).append(json)
            return R()
    monkeypatch.setattr(httpx, "AsyncClient", C)
    res, eng = asyncio.run(w.agent_search("aspas 近况", 2, days=30, site="vlr.gg", question="aspas 最近状态怎么样"))
    assert eng == "DeepSeek" and res[0]["title"] == "aspas 合同" and res[0]["date"] == "2026-10-18"
    assert "site:vlr.gg" in sent["body"]["query"] and "after:" in sent["body"]["query"]
    assert len(sent["all"]) == 2 and "after:" not in sent["all"][1]["query"]          # 20.1 带日期结果太少：不带日期补一次
    assert sent["body"]["ori_question"] == "aspas 最近状态怎么样"
    assert sent["h"]["Authorization"] == "Bearer sk-test" and sent["url"].endswith("/v1/tools/search")
    t = format_results(res, eng)
    assert '网站权威="高"' in t and "没有发布日期" not in t
    sent.clear()
    res, eng = asyncio.run(w.agent_search("长" * 200, 2, days=7, site="vlr.gg"))
    assert len(sent["body"]["query"]) <= 128 and sent["body"]["query"].endswith(sent["body"]["query"].split()[-1])
    assert "after:" in sent["body"]["query"]


def test_deepseek_key_from_llm_profile(cfg):
    from aigroup.net.search import deepseek_key
    cfg["net"] = {}
    cfg["llm"] = {"profiles": {"ds": {"base_url": "https://api.deepseek.com/v1", "api_key": "sk-abc", "model": "x"}}}
    assert deepseek_key(cfg) == "sk-abc"


def test_tavily_first_then_deepseek(monkeypatch, cfg):
    """20.4.3：配了 Tavily 先用它的免费额度；这个月用完（432）以后改用 DeepSeek，后台刷候选直接用 DeepSeek。"""
    from aigroup.net import search as S
    cfg["net"] = {**dict(cfg.get("net") or {}), "deepseek_key": "sk-test", "tavily_key": "tv-test"}
    w = S.WebSearch(cfg)
    kv = {}

    class KV:
        def get(self, k, d=None):
            return kv.get(k, d)

        def set(self, k, v):
            kv[k] = v
    w.kv = KV()
    used = []

    async def tav(q, n, days=0, snippet=220, site=""):
        used.append("Tavily")
        if kv.get("quota_gone"):
            kv["tavily_out"] = w._month()
            raise RuntimeError("Tavily 额度用完")
        return [{"title": "t", "url": "https://a.com", "snippet": "s", "date": ""}]

    async def ds(q, n, days=0, snippet=220, site="", question=""):
        used.append("DeepSeek")
        return [{"title": "d", "url": "https://b.com", "snippet": "s", "date": ""}]
    w._tavily, w._deepseek = tav, ds
    assert asyncio.run(w.agent_search("aspas 近况", 3))[1] == "Tavily"
    kv["quota_gone"] = True
    assert asyncio.run(w.agent_search("TenZ 近况", 3))[1] == "DeepSeek"
    assert w.tavily_out()
    used.clear()
    assert asyncio.run(w.agent_search("EDG 比分", 3))[1] == "DeepSeek" and used == ["DeepSeek"]   # 用完了这个月不再试 Tavily
    kv.pop("tavily_out")
    kv.pop("quota_gone")
    used.clear()
    assert asyncio.run(w.agent_search("KPL 最新", 3, prefer="deepseek"))[1] == "DeepSeek" and used == ["DeepSeek"]
