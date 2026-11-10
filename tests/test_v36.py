"""36.0：局面判断、被嫌那句不回嘴、拆开发的闭嘴、冷场和图、祝福不被“回过了 / 叫错人”拦、自检逐条晋级、别理我、每周少用的说法。"""
import asyncio
import json
import time

from aigroup.chat.settings import DEFAULTS
from aigroup.chat.social import SocialMixin
from aigroup.chat.v36 import is_media_only, joined_short, lead_words

from conftest import FRIEND, FRIEND2, GROUP, _gc


def _rec(gc, uid, name, text, ts, role="user"):
    cur = gc.db.conn.execute("INSERT INTO qq_group_messages(ts, group_id, user_id, name, role, content) VALUES(?,?,?,?,?,?)",
                             (ts, GROUP, uid, name, role, text))
    gc.db.conn.commit()
    return cur.lastrowid


def test_defaults():
    assert DEFAULTS["social_read"] == "on" and DEFAULTS["social_read_speak"] == "shadow"
    assert DEFAULTS["critic_enforce"] == [4, 5, 19, 21, 24]        # 36.2 加 19（吐槽时说风凉话）
    assert DEFAULTS["agent_filler"] is False and DEFAULTS["agent_filler_gen"] is False


def test_huh_new_words():
    for t in ("别太荒谬", "这不是肆吗", "你说我知不知道好不好玩", "太皮了", "等我有空好好管管你", "这ai到底学了些啥", "验证码是什么"):
        assert SocialMixin.HUH.search(t), t
    for t in ("吃啥好呢今天", "这不是挺好的一个方案嘛我觉得"):
        assert not SocialMixin.HUH.search(t), t


def test_split_hush(cfg, brain):
    gc, _ = _gc(cfg, brain)
    now = time.time()
    _rec(gc, 0, "她", "呼噜这东西真不归你管", now - 30, role="assistant")
    gc._st(GROUP).last_reply_ts = now - 30
    for i, t in enumerate(("你", "小嘴女", "小嘴巴")):
        _rec(gc, FRIEND, "老张", t, now - 12 + i * 3)
    assert joined_short(gc._rows(GROUP, 8), FRIEND, now) == "你小嘴女小嘴巴"
    _rec(gc, FRIEND, "老张", "闭起来", now)
    assert gc.huh_signal(GROUP, FRIEND, "闭起来", now) is True
    assert gc.hushed(GROUP, now)


def test_closed_eyes_not_hush(cfg, brain):
    gc, _ = _gc(cfg, brain)
    now = time.time()
    gc._st(GROUP).last_reply_ts = now - 20
    _rec(gc, FRIEND, "老张", "把眼睛", now - 2)
    _rec(gc, FRIEND, "老张", "闭上", now)
    assert gc.huh_signal(GROUP, FRIEND, "闭上", now) is False


def test_hush_reply_guard(cfg, brain):
    gc, _ = _gc(cfg, brain)
    assert gc.hush_reply_guard(GROUP, [(1, FRIEND, "你闭嘴", {})])
    assert gc.hush_reply_guard(GROUP, [(1, FRIEND, "催命鬼又来了", {})])
    assert not gc.hush_reply_guard(GROUP, [(1, FRIEND, "你觉得明天ig能赢geng吗", {})])
    assert not gc.hush_reply_guard(GROUP, [(1, FRIEND, "小满 你在干嘛", {})])          # 她没刚说话：“你在干嘛”是正常问
    gc._st(GROUP).last_reply_ts = time.time() - 20
    assert gc.hush_reply_guard(GROUP, [(1, FRIEND, "？？", {})])


def test_room_verdict(cfg, brain):
    gc, _ = _gc(cfg, brain, social_read="on")
    now = time.time()
    s, n = gc.room_verdict(GROUP, {"about_her": "hush", "why": "叫她闭嘴"}, mentioned=False, guess="", now=now)
    assert s and gc.hushed(GROUP, now)
    s, n = gc.room_verdict(GROUP, {"pair": "老张,小王", "to_whom": "小王", "about_her": "none"}, mentioned=False, guess="", now=now)
    assert "一问一答" in s
    s, n = gc.room_verdict(GROUP, {"pair": "老张,小王", "to_whom": "小王"}, mentioned=True, guess="", now=now)
    assert not s
    s, n = gc.room_verdict(GROUP, {"venting": "小王", "about_her": "none"}, mentioned=False, guess="", now=now)
    assert not s and "站他那边" in n
    s, n = gc.room_verdict(GROUP, {"open_ask": "那你觉得哪个队更有竞争力", "about_her": "none"}, mentioned=False, guess="", now=now)
    assert "先答" in n
    gc.db.set(f"qq_hush_{GROUP}", {"until": now - 60, "why": "x", "ts": now - 700})     # 刚安静完：旧的闭嘴不再算
    s, n = gc.room_verdict(GROUP, {"about_her": "hush"}, mentioned=False, guess="", now=now)
    assert not s and not gc.hushed(GROUP, now)
    gc.g["social_read"] = "shadow"
    s, n = gc.room_verdict(GROUP, {"pair": "老张,小王", "to_whom": "小王"}, mentioned=False, guess="", now=now + 5000)
    assert not s and not n


def test_read_room_cached(cfg, brain):
    gc, _ = _gc(cfg, brain, social_read="on")
    out = json.dumps({"to_whom": "小王", "pair": "老张,小王", "venting": "", "about_her": "none", "open_ask": "", "why": "x"},
                     ensure_ascii=False)
    calls = []

    async def chat(messages, **kw):
        calls.append(messages)
        return out
    brain.llm.chat = chat
    _rec(gc, FRIEND, "老张", "你的蓝牙耳机没降噪功能吗", time.time())
    d = asyncio.run(gc.read_room(GROUP))
    assert d.get("pair") == "老张,小王"
    assert asyncio.run(gc.read_room(GROUP)) == d and len(calls) == 1          # 同一条最新消息只问一次
    _rec(gc, FRIEND2, "小王", "没有", time.time())
    asyncio.run(gc.read_room(GROUP))
    assert len(calls) == 2


def test_media_gate(cfg, brain):
    gc, _ = _gc(cfg, brain, media_first_comment=True)
    now = time.time()
    assert is_media_only("[位置：新桥信邦华庭]") and not is_media_only("[图片：x] 好吃")
    assert gc.media_gate(GROUP, [(1, FRIEND, "[图片：缩略图，看不清细节]", {})], now)
    assert not gc.media_gate(GROUP, [(1, FRIEND, "[位置：新桥信邦华庭]", {})], now)
    gc.note_media_comment(GROUP, [(1, FRIEND, "[位置：新桥信邦华庭]", {})], now)
    assert gc.media_gate(GROUP, [(2, FRIEND, "[图片：火锅]", {})], now + 60)
    assert not gc.media_gate(GROUP, [(2, FRIEND, "[图片：火锅] 我又来吃了", {})], now + 60)


def test_cold_gate(cfg, brain):
    gc, _ = _gc(cfg, brain, cold_streak_n=2)
    now = time.time()
    for i, sc in enumerate((1.0, 0.0, 0.0)):
        gc.db.conn.execute("INSERT INTO qq_reply_effect(ts, group_id, mentioned, done, score) VALUES (?,?,?,?,?)",
                           (now - 600 + i * 60, GROUP, 0, 1, sc))
    gc.db.conn.commit()
    assert gc.cold_streak(GROUP, now) == 2
    assert gc.cold_gate(GROUP, 0.6, 0.5, now)
    assert not gc.cold_gate(GROUP, 0.7, 0.5, now)


def test_answered_greeting(cfg, brain):
    gc, _ = _gc(cfg, brain)
    now = time.time()
    rid = _rec(gc, FRIEND, "阿杰", "@你 今天小王生日，你没什么表示的吗", now - 240)
    _rec(gc, 0, "她", "哎对，小王生日快乐", now - 230, role="assistant")
    gc._st(GROUP).__dict__["answered"] = {rid: now - 230}
    row = {"id": rid, "role": "user", "content": "@你 今天小王生日，你没什么表示的吗", "ts": now - 240}
    assert gc._answered(GROUP, row, now, "[@小王] 生日快乐，擂台打赢了晚上那顿算我的") == ""
    assert gc._answered(GROUP, row, now, "哎对，小王，表示一下") != ""


def test_opt_out(cfg, brain):
    gc, _ = _gc(cfg, brain)
    assert gc.opt_signal(GROUP, FRIEND2, "小满别理我") == "out"
    assert gc.opted_out(GROUP, FRIEND2)
    assert gc.opt_signal(GROUP, FRIEND2, "小满理我") == "in"
    assert not gc.opted_out(GROUP, FRIEND2)


def test_hush_mentioned_acks_only(cfg, brain):
    from conftest import _msg, _run
    gc, ob = _gc(cfg, brain, reply="哟，这就受不了了", social_read="off")
    _rec(gc, 0, "她", "自己发祝福自己问何意味", time.time() - 20, role="assistant")
    gc._st(GROUP).last_reply_ts = time.time() - 20
    _run(gc, _msg(FRIEND, "小王", "你闭嘴", 5))                      # 没 @：不回嘴
    assert not ob.sent and gc.hushed(GROUP)


def test_lead_words():
    lines = ["哟，还真跑澳门去了", "哟，还知道冒头", "哟，这就受不了了", "行吧", "今天天气不错"]
    assert any("哟" in w for w in lead_words(lines))


def test_critic_enforce_map(cfg, brain):
    gc, _ = _gc(cfg, brain)
    gc.g["check_critic"] = "shadow"
    gc.__dict__["_critic_k"] = {GROUP: 5}
    enf = {int(x) for x in gc.gk(GROUP).get("critic_enforce")}
    assert gc.__dict__["_critic_k"][GROUP] in enf


def test_inferred_remind_not_nagged(cfg, brain):
    from conftest import _msg, _run
    gc, ob = _gc(cfg, brain, reply="十点擂台赛，别都还在被窝里", plans_infer_remind=False)
    soon = time.strftime("%Y-%m-%d %H:%M", time.localtime(time.time() + 20 * 60))
    gc.add_plan(GROUP, 0, "大家", "擂台赛", soon, "remind")
    _run(gc, _msg(FRIEND, "小王", "随便聊聊", 1))
    ob.sent.clear()
    assert asyncio.run(gc.plans_tick(GROUP, gc._st(GROUP))) is False and not ob.sent


def test_ask_waits_for_person(cfg, brain):
    gc, ob = _gc(cfg, brain, reply="比赛怎么样", plans_need_present=True)
    past = time.strftime("%Y-%m-%d %H:%M", time.localtime(time.time() - 3 * 3600))
    gc.add_plan(GROUP, FRIEND2, "老张", "擂台赛", past, "ask")
    assert asyncio.run(gc.plans_tick(GROUP, gc._st(GROUP))) is False and not ob.sent     # 老张没冒头
