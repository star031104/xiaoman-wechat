"""23.3：被纠正先认一句（不受刹车）、顶嘴改写、统一发前自检、掉线回来补回没发出去不丢。"""
import asyncio
import time

from conftest import FRIEND, GROUP, SELF, _gc

from aigroup.chat.plan import decide
from aigroup.chat.settings import DEFAULTS


def test_corrected_passes_brakes():
    now = time.time()
    base = dict(mentioned=False, now=now, last_reply_ts=now - 2, replies_10min=9, quiet=False, p=0.1, thr=0.9,
                g={**DEFAULTS, "max_per_10min": 8}, burst=6, hush="你为什么")
    assert not decide(**base)[0]
    assert decide(**base, corrected=True) == (True, "被纠正，认一句")
    assert not decide(**{**base, "quiet": True}, corrected=True)[0]          # 免打扰时段照样不说


def test_corrected_now(cfg, brain):
    gc, _ = _gc(cfg, brain)
    now = time.time()
    gc._record(GROUP, SELF, "小满", "AL被推平，24分钟就没了", 1, role="assistant", ts=now - 20)
    assert gc.corrected_now(GROUP, [(2, FRIEND, "al不是打了33分钟吗", {})], now)
    assert not gc.corrected_now(GROUP, [(2, FRIEND, "笑死", {})], now)
    gc._st(GROUP).__dict__["admit_ts"] = now - 30
    assert not gc.corrected_now(GROUP, [(2, FRIEND, "不对吧", {})], now)    # 刚认过


def test_critic(cfg, brain):
    gc, _ = _gc(cfg, brain)
    gc._record(GROUP, FRIEND, "小王", "你觉得哪个队最烂", 1)
    out = {"v": '{"bad": true, "which": 5, "why": "24分钟资料里没有", "fix": "别报具体时长"}'}

    async def chat(messages, **kw):
        assert "毛病" in messages[-1]["content"]
        return out["v"]
    brain.llm.chat = chat
    w = asyncio.run(gc._critic(GROUP, "AL吧，24分钟就没了", False, []))
    assert w.startswith("自检：说了查到的资料里没有的数字") and "别报具体时长" in w
    assert asyncio.run(gc._critic(GROUP, "x", False, ["pushback"])) == ""      # 规则已经命中：不再问
    out["v"] = '{"bad": false}'
    assert asyncio.run(gc._critic(GROUP, "AL吧", False, [])) == ""


def test_night_catch_up_keeps_when_not_ready(cfg, brain):
    gc, ob = _gc(cfg, brain)
    gc.db.set(f"qq_night_{GROUP}", [{"who": "小王", "mid": 1, "ts": time.time() - 60, "kind": "offline"}])
    ob.ready = False
    assert not asyncio.run(gc._night_catch_up(GROUP, gc._st(GROUP), time.time()))
    assert gc.db.get(f"qq_night_{GROUP}")                                     # 还留着，接上了再补


def test_quick_research_skips_filler(cfg, brain, monkeypatch):
    """23.3 查得快（10 秒内）就不先说“等我看看”。"""
    from test_v190 import BRIEF, ToolLLM, call, msg
    from conftest import _msg, _run
    gc, ob = _gc(cfg, brain, agent=True)

    async def fake_search(q, n=6, days=0, **kw):
        return [], ""
    monkeypatch.setattr(gc.web, "agent_search", fake_search)
    _run(gc, _msg(FRIEND, "小王", "TenZ最近数据怎么样", 1, at=True))
    ob.sent.clear()
    brain.llm = ToolLLM([msg(calls=[call("research", goal="TenZ 最近数据", depth="deep")]), msg(BRIEF)])
    asyncio.run(gc.agent_think(GROUP, True, "小王", None))
    assert ob.sent == []
