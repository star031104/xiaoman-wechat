"""13.2：被同一个人连着叫时降温、别追问同一个问题、提示词里的接话和群梗规矩。"""
import asyncio
import time

from conftest import FRIEND, GROUP, _gc


def _calls(gc, n, uid=FRIEND, name="小李"):
    now = time.time()
    for i in range(n):
        gc._record(GROUP, uid, name, f"@满满 快说 {i}", 300 + i, ts=now - 60 + i, to_her=True)


def test_pester_count_and_questions(cfg, brain):
    gc, ob = _gc(cfg, brain)
    _calls(gc, 5)
    gc._record(GROUP, 10001, "满满", "张小北到底是谁？你直说", 400, role="assistant", ts=time.time() - 30)
    calls, said = gc.pester_count(GROUP, FRIEND, time.time())
    assert calls == 5 and said == 1
    assert gc.her_questions(GROUP, time.time()) == ["张小北到底是谁？"]


def test_may_ignore_lets_her_skip_and_caps_to_one(cfg, brain):
    gc, ob = _gc(cfg, brain)
    _calls(gc, 5)
    brain.llm.reply = '{"act": "skip", "say": []}'
    assert not asyncio.run(gc.speak(GROUP, mentioned=True, owner_spoke=False, trigger=304, who="小李", may_ignore=True))
    assert not ob.sent                                             # 被逗烦了可以真的不回（不补“嗯？”）
    brain.llm.reply = '{"act": "say", "len": "long", "say": ["随你", "我不说"]}'
    assert asyncio.run(gc.speak(GROUP, mentioned=True, owner_spoke=False, trigger=304, who="小李", may_ignore=True))
    texts = [s["data"]["text"] for _, segs in ob.sent for s in segs if s["type"] == "text"]
    assert len(ob.sent) == 1 and texts                             # 最多一条


def test_mentioned_skip_still_answers_normally(cfg, brain):
    gc, ob = _gc(cfg, brain)
    brain.llm.reply = '{"act": "skip", "say": []}'
    assert asyncio.run(gc.speak(GROUP, mentioned=True, owner_spoke=False, trigger=1, who="小王"))
    assert ob.sent                                                 # 没被连着逗：被叫到还是至少回一句


def test_prompt_has_new_rules(cfg, brain):
    gc, ob = _gc(cfg, brain)
    gc._record(GROUP, FRIEND, "小王", "hi", 1)
    msgs = asyncio.run(gc.build_messages(GROUP, True, False, "小王", None))
    sysp = msgs[0]["content"]
    assert "接话前先弄清那句话是什么意思" in sysp and "何意味" in sysp and "同一个问题问过一次" in sysp
