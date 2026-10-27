"""30.0：别把“压力大”当成别提；没下完的图不当真图看；冷场时只回表情；话太长换短的；每天跟进群里正在聊的事；按抽查自动调开关。"""
import asyncio
import io

from conftest import FRIEND, GROUP, _gc

from aigroup.chat.checks import CLOSE_ONLY_RX
from aigroup.chat.settings import DEAD_KNOBS, DEFAULTS


def test_close_only_not_stress():
    assert CLOSE_ONLY_RX.search("别问了")
    assert not CLOSE_ONLY_RX.search("最近压力很大")


def test_closed_topic_needs_close_words(cfg, brain):
    gc, _ = _gc(cfg, brain)
    gc._record(GROUP, 10001, "小满", "体检报告出来没", 1, role="assistant")
    gc._record(GROUP, FRIEND, "小王", "最近压力很大", 2)
    assert gc.closed_topics(GROUP) == []


def _png(color, size=(200, 200)):
    from PIL import Image
    b = io.BytesIO()
    Image.new("RGB", size, color).save(b, "PNG")
    return b.getvalue()


def test_looks_loaded():
    from aigroup.wechat.wximg import looks_loaded
    assert not looks_loaded(_png((255, 255, 255)))          # 整张白：多半没下完
    assert not looks_loaded(_png((128, 128, 128)))          # 一片灰
    assert looks_loaded(_png((200, 0, 0)))                  # 纯色真图不拦
    assert looks_loaded(_png((255, 255, 255), (60, 40)))    # 小图不判断
    assert not looks_loaded(_png((10, 200, 30))[:200])      # 截断


def test_new_keys_and_dead_knobs():
    for k in ("topic_watch", "topic_watch_max", "spot_autotune", "cold_sticker", "sticker_on_sticker", "prefer_short"):
        assert k in DEFAULTS
    assert "ablate_rate" in DEAD_KNOBS and "ablate_rate" not in DEFAULTS


def test_topic_watch(cfg, brain):
    gc, _ = _gc(cfg, brain)
    gc.agent_on = lambda gid: True
    for i in range(10):
        gc._record(GROUP, FRIEND, "小王", f"今晚比赛谁赢 {i}", i + 1)
    asked = []

    async def js(messages, default=None, role="helper"):
        assert "值得每天看一眼" in messages[-1]["content"]
        return {"topics": [{"topic": "比赛", "query": "今晚 比赛 赛果"}, {"topic": "x", "query": ""}]}

    async def research(gid, q, depth, quiet, question="", qtype=""):
        asked.append((q, qtype))
    gc.brain.llm.json = js
    gc._agent_research = research
    assert asyncio.run(gc.topic_watch(GROUP)) == ["比赛"]
    assert asked == [("今晚 比赛 赛果", "recent")]
    assert asyncio.run(gc.topic_watch(GROUP)) == []          # 一天一次


def test_spot_autotune(cfg, brain):
    gc, _ = _gc(cfg, brain)
    gc.spot_items = lambda gid, a, b: {"stats": {"meme": {"n": 6, "ok": 2}, "say": {"n": 3, "ok": 3}}}
    done = gc.spot_autotune(GROUP)
    assert done and gc.gk(GROUP)["meme_sure"] == 0.85
    assert gc.spot_autotune(GROUP) == []                     # 一周一次
