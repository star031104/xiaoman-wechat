"""纯函数：消息段、要不要开口（decide）、解析大模型回复、宽松 JSON、一批消息的特征。"""
import time

from aigroup.chat.plan import (
    batch_features,
    decide,
    lenient_json,
    parse_group_reply,
    to_segments,
)
from aigroup.chat.settings import DEFAULTS
from aigroup.onebot import flatten

from conftest import (
    FRIEND,
    SELF,
    _gc,
)


def test_flatten_segments():
    msg = [{"type": "reply", "data": {"id": "321"}}, {"type": "at", "data": {"qq": str(SELF)}},
           {"type": "text", "data": {"text": " 看这个 "}}, {"type": "at", "data": {"qq": "88888"}},
           {"type": "image", "data": {"url": "https://x/1.jpg"}}, {"type": "text", "data": {"text": "[CQ:at,qq=all]"}}]
    f = flatten(msg, SELF, {"88888": "小王"})
    assert f.mentioned and f.reply_id == 321 and f.images == ["https://x/1.jpg"]
    assert "@小王" in f.text and "[图片]" in f.text and "[CQ:at,qq=all]" in f.text


def test_decide():
    now = time.time()
    kw = dict(now=now, last_reply_ts=0, replies_10min=0, quiet=False, g=dict(DEFAULTS), p=0.5, thr=0.43)
    assert decide(mentioned=True, **{**kw, "p": 1.0})[0]
    assert not decide(mentioned=True, **{**kw, "replies_10min": 12})[0]
    assert decide(mentioned=False, **kw)[0]                                       # 0.5 ≥ 0.43
    assert not decide(mentioned=False, **{**kw, "p": 0.3})[0]
    assert not decide(mentioned=False, **{**kw, "quiet": True})[0]
    assert not decide(mentioned=False, **{**kw, "last_reply_ts": now - 10})[0]     # 冷却
    assert not decide(mentioned=False, backoff_wait=30, **kw)[0]                   # 退避中
    assert decide(mentioned=False, backoff_wait=30, pending=6, **kw)[0]            # 攒了 6 条：不等
    assert decide(mentioned=True, backoff_wait=30, **kw)[0]


def test_parse_and_segments():
    assert parse_group_reply("[跳过]", "小满", 2) is None
    out = parse_group_reply("小满：[@小王] 哈哈哈[表情:大笑]|||下次叫我[语音]|||第三条", "小满", 2)
    assert out[0].startswith("[@小王] 哈哈哈") and "[语音]" not in out[1] and len(out) == 2
    segs = to_segments("[@小王] 你好 [@不认识] [CQ:at,qq=all]", {"小王": 88888})
    assert segs[0] == {"type": "at", "data": {"qq": "88888"}}
    assert sum(1 for s in segs if s["type"] == "at") == 1


def test_lenient_json():
    assert lenient_json('```json\n{"act": "say", "say": ["a",],}\n```')["say"] == ["a"]
    assert lenient_json('<think>x</think>{"action": "skip"}')["action"] == "skip"
    d = lenient_json('{"act": "say", "say": ["一", "二')
    assert d["act"] == "say" and d["say"] == ["一"] and d["_partial"]
    assert lenient_json("哈哈哈") is None


def test_batch_features():
    now = time.time()
    texts = [(1, FRIEND, "@阿杰 你周末去哪", {"at_other": True}), (2, FRIEND, "[表情:大笑]", {})]
    f = batch_features(texts, now=now, last_reply_ts=now - 100, prev_msg_ts=now - 3600, recent=[(now, "assistant"), (now, "user")],
                       owner_spoke=False, interest=False, busy=True, attention=0.4, energy=0.7, cold_minutes=15)
    assert f["at_other"] == 1 and f["self_share"] == 0.5 and f["busy"] == 1 and f["cold"] == 1 and f["batch"] == 2
    assert abs(f["tired"] - 0.3) < 1e-9 and f["emoji_only"] == 0
    f2 = batch_features([(3, FRIEND, "[动画表情]", {})], now=now, last_reply_ts=0, prev_msg_ts=now, recent=[],
                        owner_spoke=True, interest=True, busy=False, attention=0, energy=1, cold_minutes=15)
    assert f2["emoji_only"] == 1 and f2["owner"] == 1 and f2["cold"] == 0
    f3 = batch_features([(3, FRIEND, "[图片]", {})], now=now, last_reply_ts=0, prev_msg_ts=now, recent=[],
                        owner_spoke=True, interest=True, busy=False, attention=0, energy=1, cold_minutes=15)
    assert f3["emoji_only"] == 0                  # 23.0 晒照片 / 截图不算“只是表情”


def test_parse_plan_tolerant(cfg, brain):
    gc, ob = _gc(cfg, brain)
    assert gc.parse_plan("哈哈哈真的假的") is None
    p = gc.parse_plan('```json\n{"act": "说", "to": "#3", "say": "好耶"}\n```')
    assert p["act"] == "say" and p["to"] == 3 and p["say"] == ["好耶"]
    assert gc.want_quote(mentioned=False, target_is_trigger=False, target_last=False, distance=3, rand=0.99)
    assert not gc.want_quote(mentioned=False, target_is_trigger=False, target_last=True, distance=0, rand=0.5)


def test_life_ask_ai_open():
    """12.1：大家都知道她是 AI 时，她的一天写线上的事，不编上课吃饭。"""
    from types import SimpleNamespace

    from aigroup.brain import GroupBrain as Brain
    assert "没有身体" in Brain._life_ask(SimpleNamespace(ai_open=True))
    assert "高数课" in Brain._life_ask(SimpleNamespace(ai_open=False))


def test_profile_limits_seed():
    from aigroup.memory.people import PROFILE_CHARS, PROFILE_MEMORIES
    from aigroup.seed import _clean_profile
    p = _clean_profile({"性格": "好" * 300, "记得的事": [f"第{i}件" for i in range(20)]})
    assert len(p["性格"]) == PROFILE_CHARS and len(p["记得的事"]) == PROFILE_MEMORIES
