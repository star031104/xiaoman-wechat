"""36.3：《11-10 聊天复盘：怎么让小满更高情商》（都不联网）。名字换成通用的：老张 = 老张，老板 = 老板（owner），小王 = 小王。"""
import asyncio
import time

from conftest import FRIEND, FRIEND2, GROUP, OWNER, _gc

from aigroup.chat.checks import FAILURE_MODES, STYLE_IDX
from aigroup.chat.settings import DEFAULTS


def _sore_morning(gc, t0):
    """11-10 11:22～11:24：草稿纸用完、大哭的表情、进不了学校。"""
    for i, text in enumerate(["我的学校草稿纸用完了", "最后一张", "[表情包：猪一直哭，表达难过流泪]", "回学校买吗", "进不去",
                              "被门禁除名了", "[表情包：无语凝视，图上无字]"]):
        gc._record(GROUP, FRIEND2, "老张", text, 200 + i, ts=t0 + i * 15)


def test_sore_spots_and_tease(cfg, brain):
    gc, _ = _gc(cfg, brain, check_critic="off")
    now = time.time()
    _sore_morning(gc, now - 7 * 3600)
    spots = gc.sore_spots(GROUP, now)
    assert len(spots) == 1 and spots[0]["uid"] == FRIEND2 and "门禁" in spots[0]["terms"]
    assert "别拿来开玩笑" in gc.sore_block(GROUP, now) and "被门禁除名了" in gc.sore_block(GROUP, now)
    # 18:11“自己被门禁除名，倒挺会安慰人”
    assert gc._sore_tease(GROUP, "自己被门禁除名，倒挺会安慰人", FRIEND2, now)
    assert gc._sore_tease(GROUP, "老张自己被门禁除名，倒挺会安慰人", None, now)       # 没回他、点了他的名也算
    assert not gc._sore_tease(GROUP, "门禁进不去也太离谱了，校友码试试", FRIEND2, now)   # 关心 / 出主意
    assert not gc._sore_tease(GROUP, "提前下班还不好，倒挺会安慰人", FRIEND2, now)      # 没提那件事
    assert not gc._sore_tease(GROUP, "自己被门禁除名，倒挺会安慰人", FRIEND, now)       # 说的是别人（也没点他名）
    gc._record(GROUP, FRIEND2, "老张", "提前下班还不好", 300, ts=now - 30)
    out = asyncio.run(gc.checks22(GROUP, ["自己被门禁除名，倒挺会安慰人"], target_uid=FRIEND2))
    assert out and "难过过" in out[0]


def test_sore_expires(cfg, brain):
    gc, _ = _gc(cfg, brain)
    now = time.time()
    _sore_morning(gc, now - 4 * 86400)
    assert gc.sore_spots(GROUP, now) == []


def test_no_sore_from_plain_chat(cfg, brain):
    gc, _ = _gc(cfg, brain)
    now = time.time()
    for i, t in enumerate(["哈哈被迫下班了", "token用完了", "干不了了", "还是科技公司好"]):
        gc._record(GROUP, FRIEND, "小王", t, 400 + i, ts=now - 600 + i * 5)
    assert gc.sore_spots(GROUP, now) == []


def _back(cfg, brain, gap_h, last):
    gc, _ = _gc(cfg, brain, longing=True, sleep_hours="")
    gc.db.conn.execute("DELETE FROM qq_group_messages")
    gc.db.set(f"v33_back_shown_{GROUP}", None)
    now = time.time()
    gc._record(GROUP, OWNER, "老板", "废话", 500, ts=now - gap_h * 3600)
    gc._record(GROUP, OWNER, "老板", last, 501, ts=now - 20)
    return gc.fav_back_line(GROUP)


def test_fav_back_asking_no_jab(cfg, brain):
    line = _back(cfg, brain, 19, "咖啡因是啥")                  # 11-10 10:10“失踪一天，回来就问这个”
    assert "来问事的" in line and "别阴阳" in line


def test_fav_back_chat_still_jabs(cfg, brain):
    line = _back(cfg, brain, 19, "早上好啊")
    assert "阴阳" in line and "来问事" not in line


def test_fav_back_short_gap(cfg, brain):
    assert _back(cfg, brain, 5, "早上好啊") == ""                # 5 小时不算“隔了很久”


def test_mood_note(cfg, brain):
    gc, _ = _gc(cfg, brain, social_read="on")
    s, n = gc.room_verdict(GROUP, {"mood": "笑", "mood_who": "小王", "about_her": "none"}, mentioned=False, guess="",
                           now=time.time())
    assert not s and "笑着说" in n and "气人" in n
    s, n = gc.room_verdict(GROUP, {"mood": "难过", "mood_who": "老张", "about_her": "none"}, mentioned=False, guess="",
                           now=time.time())
    assert "不开玩笑" in n
    assert gc.mood_note({"mood": "平常"}) == ""


def test_own_mentions_note(cfg, brain):
    gc, _ = _gc(cfg, brain)
    now = time.time()
    gc._record(GROUP, OWNER, "老板", "那个主播队，打进季后赛了", 600, ts=now - 40 * 60)
    gc._record(GROUP, OWNER, "老板", "[表情包：发呆卖萌，图上无字]", 601, ts=now - 30 * 60)
    gc._record(GROUP, OWNER, "老板", "@你 帮我看看最近有什么比赛看，好无聊", 602, ts=now - 5)
    note = gc.own_mentions_note(GROUP, [(602, OWNER, "@你 帮我看看最近有什么比赛看，好无聊", {})], now)
    assert "主播队" in note and "表情包" not in note
    assert gc.own_mentions_note(GROUP, [(603, OWNER, "哈哈哈", {})], now) == ""


def test_rules_and_settings():
    assert "难过" in FAILURE_MODES[23] and "揽" in FAILURE_MODES[24]
    assert 24 in DEFAULTS["critic_enforce"] and 25 in STYLE_IDX
    assert DEFAULTS["check_sore_tease"] == "on" and DEFAULTS["longing_back_hours"] == 12


def test_floor_mentions_sore():
    from aigroup.chat.character import FLOOR
    assert "难过、受挫过" in FLOOR
