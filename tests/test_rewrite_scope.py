"""37.1：主动的 must（想念、打气、跟进）发前检查有毛病时不改写，挑不出就不说；提醒 / 答应的事 / 生日（may_rewrite）照旧改写一次。"""
import asyncio

from conftest import FRIEND, GROUP, J, _gc


def _setup(cfg, brain):
    gc, ob = _gc(cfg, brain, cooldown_seconds=0, check_critic="off")
    gc._record(GROUP, FRIEND, "小王", "今天好冷", 1)
    for i, t in enumerate(["我小本本记岔了", "这条记进小本本"]):
        gc._record(GROUP, 10001, "小满", t, 10 + i, role="assistant")
    brain.llm.reply = J(act="say", kind="I2", to=1, len="short", say=["行，小本本上再记一笔"], alt=[["小本本又多一页"]], move="接梗")
    calls = []

    async def rw(messages, raw, hits, gid):
        calls.append(hits)
        return [["冷就多穿点"]]
    gc._rewrite = rw
    return gc, ob, calls


def test_must_nudge_not_rewritten(cfg, brain):
    gc, ob, calls = _setup(cfg, brain)
    ok = asyncio.run(gc.speak(GROUP, mentioned=False, owner_spoke=True, trigger=None, who="小王", why="你有点想他，@他一下", must=True))
    assert not ok and not ob.sent and not calls


def test_must_reminder_may_rewrite(cfg, brain):
    gc, ob, calls = _setup(cfg, brain)
    asyncio.run(gc.speak(GROUP, mentioned=False, owner_spoke=False, trigger=None, who="小王", why="到点了：提醒他交作业",
                         must=True, may_rewrite=True))
    assert calls
