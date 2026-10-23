"""说话：被叫到一定回、计划 JSON、多选一、不被打断、复读、点表情、回复节奏、说完的效果。"""
import asyncio
import json
import time

from aigroup.chat.feedback import effect_score
from aigroup.chat.plan import (
    bigram_overlap,
)

from conftest import (
    FRIEND,
    FRIEND2,
    GROUP,
    OWNER,
    SELF,
    FakeLLM,
    J,
    _gc,
    _msg,
    _run,
)


def test_mentioned_reply_and_prompt(cfg, brain):
    gc, ob = _gc(cfg, brain, reply="[@小王] 哈哈哈你也太惨了|||下次叫上我")
    brain.llm.raw = True
    _run(gc, _msg(FRIEND, "小王", " 我论文又被打回来了", 3, at=True))
    assert len(ob.sent) == 2
    first = ob.sent[0][1]
    assert first[0] == {"type": "reply", "data": {"id": "3"}} and first[1] == {"type": "at", "data": {"qq": str(FRIEND)}}
    sys = brain.llm.group_calls[-1][0]["content"]
    assert "画画群" in sys and "关系很好的朋友" in sys and "小满" in sys
    rows = brain.db.conn.execute("SELECT role FROM qq_group_messages WHERE group_id=? ORDER BY id", (GROUP,)).fetchall()
    assert [r["role"] for r in rows] == ["user", "assistant", "assistant"]


def test_other_group_and_self_ignored(cfg, brain):
    gc, ob = _gc(cfg, brain)
    _run(gc, {**_msg(FRIEND, "小王", "在吗", 1, at=True), "group_id": 999}, _msg(SELF, "我", "自己", 2, at=True))
    assert not ob.sent


def test_repeat_follow(cfg, brain):
    gc, ob = _gc(cfg, brain, repeat_chance=1.0)
    _run(gc, _msg(FRIEND, "小王", "开摆", 1), _msg(FRIEND2, "阿杰", "开摆", 2))
    assert ob.sent and ob.sent[-1][1] == [{"type": "text", "data": {"text": "开摆"}}]
    assert not brain.llm.group_calls


def test_react_tag_and_similar(cfg, brain):
    gc, ob = _gc(cfg, brain, reply="[回应:大笑]")
    _run(gc, _msg(FRIEND, "小王", " 我今天摔了个狗吃屎", 5, at=True))
    assert any(a == "set_msg_emoji_like" and kw["message_id"] == 5 for a, kw in ob.calls)
    brain.db.conn.execute("INSERT INTO qq_group_messages(ts, group_id, user_id, name, role, content) VALUES (?,?,?,?,?,?)",
                          (time.time(), GROUP, SELF, "小满", "assistant", "周末一起去看展吧！"))
    assert gc.too_similar(GROUP, "周末一起去看展吧") and not gc.too_similar(GROUP, "我想喝奶茶")


def test_mention_survives_new_messages(cfg, brain):
    gc, ob = _gc(cfg, brain, pace=1.0, read_delay=[0.3, 0.3], notice_delay=[0, 0], talkativeness=0,
                 react_chance=0, repeat_chance=0)
    brain.llm.raw = True                # 模型没按 JSON 输出、但不像 JSON：被叫到时照样回（引用叫她的那条）

    async def go():
        await gc.on_event(_msg(FRIEND, "小王", "小满你周末去不去", 21))
        await asyncio.sleep(0.1)
        await gc.on_event(_msg(FRIEND2, "阿杰", "我去", 22))
        for _ in range(100):
            await asyncio.sleep(0.02)
            if ob.sent:
                break
    asyncio.run(go())
    assert ob.sent and ob.sent[0][1][0] == {"type": "reply", "data": {"id": "21"}}


def test_plan_json_target_quote_and_attention(cfg, brain):
    gc, ob = _gc(cfg, brain, talkativeness=1.0, cooldown_seconds=0)
    for i, (uid, name, text) in enumerate([(FRIEND, "小王", "我新买的耳机到了"), (FRIEND2, "阿杰", "今天食堂好难吃"),
                                           (FRIEND2, "阿杰", "红烧肉是甜的"), (FRIEND2, "阿杰", "谁懂")], 1):
        gc._record(GROUP, uid, name, text, 100 + i)
    brain.llm.reply = json.dumps({"think": "接小王耳机的话", "act": "say", "to": 1, "len": "short",
                                  "say": ["哪款，展开说说", "我也想换耳机"]}, ensure_ascii=False)
    ok = asyncio.run(gc.speak(GROUP, mentioned=False, owner_spoke=False, trigger=None, who=""))
    assert ok and len(ob.sent) == 2                                   # len 只是提示：写了两条就发两条（2.0）
    assert ob.sent[0][1][0] == {"type": "reply", "data": {"id": "101"}}   # 回的是往回好几条的那句：引用
    sys_user = brain.llm.group_calls[-1][1]["content"]
    assert "#1 [" in sys_user and "只输出 JSON" in sys_user
    assert gc._st(GROUP).attention.get(FRIEND) > 0.3 and gc._st(GROUP).attention.get(FRIEND2) == 0
    eff = brain.db.conn.execute("SELECT * FROM qq_reply_effect").fetchone()
    assert eff["target_uid"] == FRIEND and eff["done"] == 0


def test_plan_skip_backoff_and_react(cfg, brain):
    gc, ob = _gc(cfg, brain)
    gc._record(GROUP, FRIEND, "小王", "哈哈哈", 201)
    brain.llm.reply = '{"think": "插不上话", "act": "skip", "to": null, "say": []}'
    for _ in range(2):
        assert not asyncio.run(gc.speak(GROUP, mentioned=False, owner_spoke=False, trigger=None, who=""))
    st = gc._st(GROUP)
    assert st.backoff.n == 2 and st.backoff.waiting() > 0 and not ob.sent
    brain.llm.reply = '{"think": "", "act": "react", "to": 1, "react": "大笑", "say": []}'
    assert not asyncio.run(gc.speak(GROUP, mentioned=False, owner_spoke=False, trigger=None, who=""))
    assert any(a == "set_msg_emoji_like" and kw["message_id"] == 201 for a, kw in ob.calls)
    brain.llm.reply = '{"act": "skip", "say": []}'                    # 被叫到却想跳过：给个最短的回应
    assert asyncio.run(gc.speak(GROUP, mentioned=True, owner_spoke=False, trigger=201, who="小王"))
    assert ob.sent[-1][1][-1]["data"]["text"] in ("？", "嗯？", "在呢")


def test_new_message_does_not_cancel_speaking(cfg, brain):
    gc, ob = _gc(cfg, brain, pace=1.0, read_delay=[0, 0], notice_delay=[0, 0], typing_chars_per_sec=30,
                 talkativeness=0, react_chance=0, repeat_chance=0)

    class SlowLLM(FakeLLM):
        async def chat(self, messages, **kw):
            if "最近的群聊记录" in messages[-1]["content"]:
                await asyncio.sleep(0.3)                     # 大模型想得慢
            return await super().chat(messages, **kw)
    brain.llm = SlowLLM(J(act="say", say=["在的在的"]))

    async def go():
        await gc.on_event(_msg(FRIEND, "小王", "小满在吗", 31))
        await asyncio.sleep(0.1)                              # 她已经在想了
        await gc.on_event(_msg(FRIEND2, "阿杰", "她肯定在睡觉", 32))
        for _ in range(150):
            await asyncio.sleep(0.02)
            if ob.sent and not gc._bg:
                break
        await asyncio.gather(*list(gc._bg), return_exceptions=True)
    asyncio.run(go())
    assert ob.sent and "在的在的" in json.dumps(ob.sent, ensure_ascii=False)       # 1.4 这里什么都不发
    eff = brain.db.conn.execute("SELECT COUNT(*) n FROM qq_reply_effect").fetchone()["n"]
    assert eff == 1


def test_parse_fail_never_sends_raw(cfg, brain):
    gc, ob = _gc(cfg, brain, talkativeness=1.0, cooldown_seconds=0)
    brain.llm.raw = True
    brain.llm.reply = '{"think": "大家在聊游戏，我想接一下小王", "act": "say", "say": ["好耶"]'   # 被截断了
    gc._record(GROUP, FRIEND, "小王", "打游戏去", 1)
    ok = asyncio.run(gc.speak(GROUP, mentioned=False, owner_spoke=False, trigger=None, who=""))
    sent = json.dumps(ob.sent, ensure_ascii=False)
    assert "think" not in sent and "大家在聊" not in sent                       # 救回 say，原文不发
    assert ok and "好耶" in sent
    ob.sent.clear()
    brain.llm.reply = '{"think": "嗯…", "act": '                                  # 救不回来
    assert not asyncio.run(gc.speak(GROUP, mentioned=False, owner_spoke=False, trigger=None, who=""))
    assert not ob.sent
    assert asyncio.run(gc.speak(GROUP, mentioned=True, owner_spoke=False, trigger=1, who="小王"))
    assert "think" not in json.dumps(ob.sent, ensure_ascii=False)                # 被叫到：给个短回应
    t = brain.db.conn.execute("SELECT outcome FROM qq_trace ORDER BY id").fetchall()
    assert [r["outcome"] for r in t][:2] == ["say", "parse_fail"]


def test_kind_s2_skips_and_next_waits(cfg, brain):
    gc, ob = _gc(cfg, brain, talkativeness=1.0, cooldown_seconds=0)
    for i, (uid, name, text) in enumerate([(FRIEND, "小王", "你作业写完没"), (FRIEND2, "阿杰", "还没呢")], 1):
        gc._record(GROUP, uid, name, text, 200 + i)
    brain.llm.reply = J(kind="S2", act="say", say=["我也没写"])
    assert not asyncio.run(gc.speak(GROUP, mentioned=False, owner_spoke=False, trigger=None, who=""))
    assert not ob.sent
    assert brain.db.conn.execute("SELECT 1 FROM qq_trace WHERE outcome='skip'").fetchone()
    brain.llm.reply = J(kind="I2", act="say", to=1, say=["你俩都没写？"], next={"wait_for": 1, "timeout": 120})
    assert asyncio.run(gc.speak(GROUP, mentioned=False, owner_spoke=False, trigger=None, who=""))
    st = gc._st(GROUP)
    assert st.wait and st.wait["uid"] == FRIEND and st.wait["until"] > time.time() + 60
    # 等的人开口了：哪怕话痨度是 0、在冷却里，也直接放行
    gc.g.update(talkativeness=0, cooldown_seconds=600)
    brain.llm.reply = J(kind="I1", act="say", say=["我就知道"])
    n = len(ob.sent)
    _run(gc, _msg(FRIEND, "小王", "写完了啊", 203))
    assert len(ob.sent) > n and st.wait is None


def test_candidates_pick_most_human(cfg, brain):
    gc, ob = _gc(cfg, brain, talkativeness=1.0, cooldown_seconds=0)
    gc._record(GROUP, FRIEND, "小王", "推荐个番", 1)
    brain.llm.reply = J(act="say", len="mid", say=["作为一个AI，我推荐你看葬送的芙莉莲。"], alt=[["芙莉莲！看就完事了"]])
    assert asyncio.run(gc.speak(GROUP, mentioned=False, owner_spoke=False, trigger=None, who=""))
    sent = json.dumps(ob.sent, ensure_ascii=False)
    assert "看就完事了" in sent and "AI" not in sent
    ob.sent.clear()
    brain.llm.reply = J(act="say", say=["此外，希望对你有帮助！https://x.com"])
    assert not asyncio.run(gc.speak(GROUP, mentioned=False, owner_spoke=False, trigger=None, who=""))
    assert not ob.sent and brain.db.conn.execute("SELECT 1 FROM qq_trace WHERE outcome='blocked'").fetchone()


def test_next_timeout_text_does_not_drop_reply(cfg, brain):
    gc, ob = _gc(cfg, brain, talkativeness=1.0, cooldown_seconds=0)
    gc._record(GROUP, FRIEND, "小王", "你周末干嘛", 1)
    brain.llm.reply = J(act="say", say=["画画呗"], next={"wait_for": 1, "timeout": "3分钟"})
    assert asyncio.run(gc.speak(GROUP, mentioned=True, owner_spoke=False, trigger=1, who="小王"))
    st = gc._st(GROUP)
    assert st.wait and 170 < st.wait["until"] - time.time() <= 180


def test_mentioned_sticker_only_still_replies(cfg, brain):
    gc, ob = _gc(cfg, brain, reply=J(act="say", say=[], sticker="无语"))
    _run(gc, _msg(FRIEND, "小王", "小满你看", 1))
    assert ob.sent and ob.sent[-1][1][-1]["type"] == "text"                    # 库里没有表情包：至少回一句


def test_s2_skip_only_for_replies(cfg, brain):
    gc, ob = _gc(cfg, brain, reply=J(kind="S1", act="say", say=["在画海报，有人来聊聊吗"]))
    gc._record(GROUP, FRIEND, "小王", "早", 1)
    assert asyncio.run(gc.speak(GROUP, mentioned=False, owner_spoke=False, trigger=None, who="", why="你想冒个泡"))


def test_latency_and_human_delay(cfg, brain):
    gc, ob = _gc(cfg, brain, pace=1.0, instant=False)
    t = time.time() - 10000
    for i in range(80):
        brain.db.conn.execute("INSERT INTO qq_group_messages(ts, group_id, user_id, name, role, content) VALUES (?,?,?,?,?,?)",
                              (t + i * 20, GROUP, FRIEND if i % 2 else FRIEND2, "x", "user", "嗯"))
    brain.db.conn.commit()
    lat = gc.refresh_latency(GROUP)
    assert lat["n"] >= 70 and abs(lat["median"] - 20) < 1
    d = gc.human_delay(GROUP, "notice_delay", 1.0)
    assert 2 <= d <= 30


def test_effects(cfg, brain):
    assert bigram_overlap("周末去看展吧", "看展几点集合") >= 0.2 and bigram_overlap("好热", "吃火锅") == 0
    assert effect_score(True, False, False, 3) == 1 and effect_score(False, True, False, 1) == 0.6
    assert effect_score(False, False, False, 2) == 0.2 and effect_score(False, False, False, 0) == 0
    gc, ob = _gc(cfg, brain)
    t0 = time.time() - 2000
    db = brain.db.conn
    for her, after in (([501], [(FRIEND, "笑死", 0, 501)]),             # 回复了她
                       ([502], [(FRIEND2, "看展几点集合", 0, None)]),   # 接着话题
                       ([503], [])):                                     # 冷场
        db.execute("INSERT INTO qq_reply_effect(ts, group_id, her_ids, text) VALUES (?,?,?,?)",
                   (t0, GROUP, json.dumps(her), "周末去看展吧"))
        for uid, text, to_her, rep in after:
            db.execute("INSERT INTO qq_group_messages(ts, group_id, user_id, name, role, content, to_her, reply_to) "
                       "VALUES (?,?,?,?,?,?,?,?)", (t0 + 30, GROUP, uid, "x", "user", text, to_her, rep))
        t0 += 400
    db.commit()
    assert gc.evaluate_effects(now=time.time()) == 3
    scores = [r["score"] for r in db.execute("SELECT score FROM qq_reply_effect ORDER BY id")]
    assert scores == [1.0, 0.6, 0.0]
    st = gc.effect_stats(0)
    assert st["n"] == 3 and st["engaged"] == 2 and st["cold"] == 1 and st["rate"] == round(2 / 3, 3)


def test_fixed_bugs(cfg, brain, monkeypatch):
    gc, ob = _gc(cfg, brain, talkativeness=0, react_chance=0, repeat_chance=0)
    # 1. 主人只说过一句也是“很熟”
    _run(gc, _msg(OWNER, "老张", "hello", 1, at=True))
    assert gc.person(OWNER)["familiarity"] == "老朋友" and "熟悉度：老朋友" in gc.roster(GROUP)
    # 2. 引用别人的消息里带她名字：不算叫她
    gc._record(GROUP, FRIEND2, "阿杰", "小满今天没来", 50)
    n = len(ob.sent)
    _run(gc, {**_msg(FRIEND, "小王", "哈哈是吗", 51), "message": [{"type": "reply", "data": {"id": "50"}},
                                                                  {"type": "text", "data": {"text": "哈哈是吗"}}]})
    assert len(ob.sent) == n
    # 3. “小满足”不算叫她，“小满你”算
    assert not gc._called("今天好小满足啊") and not gc._called("小满月") and gc._called("小满你在吗") and gc._called("小满")
    # 5. 兴趣词去掉性格词、口头禅、带她名字的
    brain.db.set("qq_interests", ["画画", "吐槽", "揣测", "本小满", "笑死", "探店"])
    assert gc.interests() == ["画画", "探店"]
    brain.persona.interests = ["水彩", "黑猫"]
    assert gc.interests() == ["水彩", "黑猫"]
    brain.persona.interests = []


def test_system_prompt_stable_across_calls(cfg, brain):
    gc, ob = _gc(cfg, brain)
    gc._record(GROUP, FRIEND, "小王", "在吗", 1)
    a = asyncio.run(gc.build_messages(GROUP, False, False, ""))
    gc._record(GROUP, FRIEND2, "阿杰", "在的", 2)
    b = asyncio.run(gc.build_messages(GROUP, True, False, "阿杰"))
    sa, sb = a[0]["content"], b[0]["content"]
    k = 0
    while k < min(len(sa), len(sb)) and sa[k] == sb[k]:
        k += 1
    assert k > 0.8 * len(sa) or sa == sb                              # 变的只有最后的“群里的人”
    import re
    assert re.search(r"现在是 \d\d:\d\d", a[1]["content"]) and not re.search(r"\d\d:\d\d", sa)
    assert "记住：你是小满" in a[1]["content"]
