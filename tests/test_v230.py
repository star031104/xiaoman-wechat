"""23.0：看到就回（不装没看到、不模拟打字）、听者和发前检查同时跑、“可以”就说、放着的那句再判不丢、拿不准时放宽、晒图不算“只是表情”。"""
import asyncio
import json
import time

from conftest import FRIEND, GROUP, FakeLLM, J, _gc


class ListenLLM(FakeLLM):
    def __init__(self, window="ok", delay=0.0, **kw):
        super().__init__(**kw)
        self.window, self.delay, self.listen_calls = window, delay, 0

    async def chat(self, messages, helper=False, **kw):
        if "帮一个叫" in messages[-1]["content"]:
            self.listen_calls += 1
            if self.delay:
                await asyncio.sleep(self.delay)
            return json.dumps({"window": self.window, "until": "", "issues": [], "why": "测"}, ensure_ascii=False)
        return await super().chat(messages, helper=helper, **kw)


def _say(gc, brain, text="穿秋裤了没", **kw):
    brain.llm.reply = J(act="say", kind="I2", to=1, len="short", say=[text], move="接梗")
    return asyncio.run(gc.speak(GROUP, mentioned=False, owner_spoke=False, trigger=None, who="", **kw))


def test_instant_no_delays(cfg, brain):
    gc, ob = _gc(cfg, brain, pace=1.0)
    from aigroup.chat.settings import DEFAULTS
    assert gc.gk(GROUP)["instant"] is True and DEFAULTS["merge_wait_seconds"] == 2
    assert gc.human_delay(GROUP, "read_delay", 0.5) == 0 and gc.secs("notice_delay", GROUP) == 0
    assert gc.secs("busy_delay_minutes", GROUP) == 0
    gc2, _ = _gc(cfg, brain, instant=False, read_delay=[3, 3], pace=1.0, human_latency=False)
    assert gc2.secs("read_delay", GROUP) == 3


def test_instant_sends_without_typing_wait(cfg, brain):
    brain.llm = ListenLLM("now")
    gc, ob = _gc(cfg, brain, pace=1.0, typing_chars_per_sec=1, cooldown_seconds=0)
    gc._record(GROUP, FRIEND, "小王", "今天好冷", 1)
    t = time.time()
    assert _say(gc, brain, "这一句有十几个字的样子吧要打好久")
    assert ob.sent and time.time() - t < 3                  # 以前按 1 字 / 秒要等 6 秒


def test_listener_ok_says_and_early_queues(cfg, brain):
    brain.llm = ListenLLM("ok")
    gc, ob = _gc(cfg, brain, cooldown_seconds=0, listener_mode="on")
    gc._record(GROUP, FRIEND, "小王", "今天好冷", 1)
    assert _say(gc, brain) and ob.sent and gc._st(GROUP).pitch is None     # “可以”就说，不再先放着
    ob.sent.clear()
    brain.llm.window = "early"
    assert not _say(gc, brain, "要不要加件衣服") and not ob.sent and gc._st(GROUP).pitch


def test_pitch_retry_not_dropped_when_ok(cfg, brain):
    brain.llm = ListenLLM("early")
    gc, ob = _gc(cfg, brain, cooldown_seconds=0, listener_mode="on", self_score=False)
    gc._record(GROUP, FRIEND, "小王", "今天好冷", 1)
    for k, w in enumerate(("early", "ok")):                               # 再判是“太早”或“可以”都发（只有“别说”才不发）
        ob.sent.clear()
        gc._record(GROUP, FRIEND, "小王", f"是啊{k}", 10 + k)
        brain.llm.window = w
        assert _say(gc, brain, ["那你多喝热水", "风好大出门戴帽子"][k], why="再看看", pitch=True) and ob.sent
    ob.sent.clear()
    gc._record(GROUP, FRIEND, "小王", "嗯嗯", 20)
    brain.llm.window = "never"
    assert not _say(gc, brain, "冷死了", why="再看看", pitch=True) and not ob.sent


def test_listener_runs_alongside_checks(cfg, brain):
    brain.llm = ListenLLM("now", delay=0.3)
    gc, ob = _gc(cfg, brain, cooldown_seconds=0, listener_mode="on")
    gc._record(GROUP, FRIEND, "小王", "今天好冷", 1)
    seen = {}
    orig = gc.checks22

    async def slow_checks(*a, **k):
        await asyncio.sleep(0.05)
        seen["listen_calls_when_checking"] = brain.llm.listen_calls      # 检查的时候听者已经在问了
        await asyncio.sleep(0.25)
        return await orig(*a, **k)
    gc.checks22 = slow_checks
    t = time.time()
    assert _say(gc, brain) and ob.sent
    assert seen["listen_calls_when_checking"] == 1 and time.time() - t < 0.55


def test_listener_never_cancels_and_skips(cfg, brain):
    brain.llm = ListenLLM("never")
    gc, ob = _gc(cfg, brain, cooldown_seconds=0, listener_mode="on")
    gc._record(GROUP, FRIEND, "小王", "今天好冷", 1)
    assert not _say(gc, brain) and not ob.sent


def test_judge_prompt_relaxed(cfg, brain):
    from aigroup.chat.speak import JUDGE_ACTS

    class JL(FakeLLM):
        async def chat(self, messages, **kw):
            self.last = messages[-1]["content"]
            return '{"why": "在晒图", "act": "捧场"}'
    brain.llm = JL()
    gc, ob = _gc(cfg, brain)
    gc._record(GROUP, FRIEND, "小王", "[图片]", 1)
    assert asyncio.run(gc.quick_judge(GROUP)) == "say"
    assert "大多数时候是不说话" not in brain.llm.last and "别人在互相聊”不是不说的理由" in brain.llm.last
    assert JUDGE_ACTS["捧场"][1] == "say" and "插进去突兀" not in JUDGE_ACTS["不说"][0]
    assert gc.gk(GROUP)["borderline_judge"] is False and gc.gk(GROUP)["gate_judge_band"] == 0.25


def test_judge_examples_include_maybe(cfg, brain):
    from aigroup.chat.speak import judge_examples
    gc, ob = _gc(cfg, brain)
    gc._record(GROUP, FRIEND, "小王", "看我拍的晚霞", 1)
    for lab in (0, 1, 2):
        g = gc.gate.add(GROUP, False, 0.5, 0.4, False, {})
        brain.db.conn.execute("UPDATE qq_gate_log SET human=? WHERE id=?", (lab, g))
    ex = judge_examples(brain.db, GROUP)
    assert "→ 该接" in ex and "→ 可以接" in ex and "→ 不该插" in ex


def test_new_messages_no_rethink_by_default(cfg, brain):
    class D(FakeLLM):
        async def chat(self, messages, **kw):
            last = messages[-1]["content"]
            if "最近的群聊记录" in last:
                gc._record(GROUP, 88888, "小王", "满满你说呢", 2, to_her=True)
                gc._record(GROUP, 66666, "阿杰", "对啊", 3)
            return await super().chat(messages, **kw)
    brain.llm = D(reply=J(act="say", kind="I1", to=1, len="short", say=["周六吧"]))
    gc, ob = _gc(cfg, brain)
    gc._record(GROUP, 88888, "小王", "周六还是周日", 1)
    assert asyncio.run(gc.speak(GROUP, mentioned=True, owner_spoke=False, trigger=None, who="小王")) is True
    assert len(brain.llm.group_calls) == 1 and ob.sent                   # 写好了就发，新消息留给下一轮
