"""21.0 通用框架：研究循环（类型力度、简报 v3、重大说法反搜、快查走 DeepSeek）、开口前九问、等等队列、擦边再问、人卡、硬底线、标注拆分。"""
import asyncio
import json
import time

from conftest import FRIEND, GROUP, J, _gc


# ---------------- 研究循环 ----------------
def test_type_budget_and_note():
    from aigroup.agent import prompts as P
    from aigroup.agent.mixin import research_budget
    g = {"agent_quick_seconds": 15, "agent_deep_seconds": 40}
    assert research_budget(g, "quick", "fact") == (2, 15.0, "quick")
    assert research_budget(g, "quick", "compare")[2] == "deep"
    r, sec, dep = research_budget(g, "deep", "recent")
    assert dep == "deep" and r >= 3 and sec >= 30                    # 判断层说要深查：快查类型也深一点
    assert research_budget(g, "deep", "fact")[2] == "quick"           # 稳定事实不需要深查
    assert research_budget(g, "deep", "")[2] == "deep"                # 没类型：照老的
    assert "独立来源" in P.type_note("major") and P.type_note("") == ""


def test_brief_v3_claims_and_single_major():
    from aigroup.agent.mixin import brief_block, parse_brief, single_source_major
    raw = json.dumps({"answer_short": "他官宣转会了", "as_of": "2026-10-21",
                      "claims": [{"text": "aspas 官宣转会 MIBR", "sources": [{"site": "推特", "date": "2026-10-20"}],
                                  "confidence": "high"},
                                 {"text": "合同到 2027", "sources": [{"site": "vlr"}, {"site": "liquipedia"}],
                                  "confidence": "中"}],
                      "unknowns": ["转会费"], "followups": ["他去年的数据"]}, ensure_ascii=False)
    d = parse_brief(raw)
    assert d["answer_points"] == ["aspas 官宣转会 MIBR", "合同到 2027"] and d["claims"][1]["confidence"] == "medium"
    assert d["unknown"] == "转会费" and d["followups"] == ["他去年的数据"] and len(d["sources"]) == 3
    assert single_source_major(d) == ["aspas 官宣转会 MIBR"]          # 重大消息只有一个来源
    blk = brief_block("aspas 转会", d)
    assert "结论：他官宣转会了" in blk and "合同到 2027（大概）" in blk and "被追问时可以补" in blk
    old = parse_brief(json.dumps({"answer_points": ["一"], "confidence": "high"}))      # 第 2 版照样认
    assert old["answer_points"] == ["一"] and old["claims"] == []


def test_recheck_major_reverse_search(cfg, brain):
    from aigroup.agent.mixin import parse_brief
    gc, _ = _gc(cfg, brain)
    seen = {}

    class W:
        async def agent_search(self, q, n=5, **kw):
            seen.update(q=q, **kw)
            return [{"title": "官方辟谣：aspas 并未转会", "site": "vlr", "snippet": ""}], "deepseek"
    gc.web = W()
    d = parse_brief(json.dumps({"claims": [{"text": "aspas 官宣转会 MIBR", "sources": [{"site": "推特"}],
                                             "confidence": "high"}]}, ensure_ascii=False))
    d = asyncio.run(gc._recheck_major(d, ["搜：aspas 转会"]))
    assert "辟谣" in seen["q"] and seen.get("prefer") == "deepseek"
    assert d["confidence"] == "low" and d["counter"] and d["single_major"]


def test_quick_depth_prefers_deepseek():
    from aigroup.agent import tools as T
    assert T.DEPTH.get() == ""
    tok = T.DEPTH.set("quick")
    assert T.DEPTH.get() == "quick"
    T.DEPTH.reset(tok)


def test_unsupported_claims():
    from aigroup.chat.quality import unsupported_claims
    assert unsupported_claims("他退役了啊", "简报：他转会了") == ["退役"]
    assert unsupported_claims("他没退役，是谣言", "简报：他转会了") == []
    assert unsupported_claims("他转会了", "简报：他转会了") == []


# ---------------- 九问 ----------------
def test_appraise_parse_and_verdict():
    from aigroup.chat.prompt import PromptMixin, appraise_verdict
    raw = json.dumps({"act": "say", "say": ["哈哈"], "appraise": {"timing": "等等", "leak": "false", "face_risk": "2",
                                                                   "fresh": True, "tease_ok": None, "why": "他还没说完"}},
                     ensure_ascii=False)
    a = PromptMixin.parse_plan_text(raw)["appraise"]
    assert a["timing"] == "wait" and a["leak"] is False and a["face_risk"] == 2 and "tease_ok" not in a
    assert appraise_verdict(a, prompted=False)[0] == "wait"
    assert appraise_verdict({"timing": "no"}, prompted=False)[0] == "skip"
    assert appraise_verdict({"fresh": False}, prompted=False)[0] == "skip"
    assert appraise_verdict({"leak": True}, prompted=False)[0] == "skip"
    assert appraise_verdict({"leak": True}, prompted=True)[0] == "fix"      # 被叫到：改，不是不说
    assert appraise_verdict({"face_risk": 3, "timing": "now"}, prompted=False)[0] == "fix"
    assert appraise_verdict({"timing": "now", "fresh": True}, prompted=False) == ("say", [])


def test_appraise_in_prompt(cfg, brain):
    gc, _ = _gc(cfg, brain)
    gc._record(GROUP, FRIEND, "小王", "今天好冷", 1)
    msgs = asyncio.run(gc.build_messages(GROUP, False, False, "", None, query="今天好冷", focus=[FRIEND]))
    sysm = msgs[0]["content"]
    assert '"appraise"' in sysm and '"score"' not in sysm


def test_appraise_skip_and_wait_queue(cfg, brain):
    gc, ob = _gc(cfg, brain, cooldown_seconds=0, talkativeness=1.0)
    gc._record(GROUP, FRIEND, "小王", "今天好冷", 1)
    brain.llm.reply = J(act="say", kind="I2", to=1, len="short", say=["是啊好冷"], appraise={"timing": "不说"})
    assert not asyncio.run(gc.speak(GROUP, mentioned=False, owner_spoke=False, trigger=None, who="")) and not ob.sent
    brain.llm.reply = J(act="say", kind="I2", to=1, len="short", say=["穿秋裤了没"], appraise={"timing": "等等", "why": "他话没说完"})
    assert not asyncio.run(gc.speak(GROUP, mentioned=False, owner_spoke=False, trigger=None, who="")) and not ob.sent
    st = gc._st(GROUP)
    assert st.pitch and st.pitch["say"] == ["穿秋裤了没"]
    assert asyncio.run(gc.pitch_tick(GROUP, st)) is False                # 还没到 60 秒
    st.pitch["ts"] -= 120
    brain.llm.reply = J(act="say", kind="I2", to=1, len="short", say=["穿秋裤了没"], appraise={"timing": "等等"})
    assert asyncio.run(gc.pitch_tick(GROUP, st)) is True
    assert not ob.sent and st.pitch is None                             # 再判还说“等等”：等过一次了，不说
    brain.llm.reply = J(act="say", kind="I2", to=1, len="short", say=["穿秋裤了没"], appraise={"timing": "等等"})
    asyncio.run(gc.speak(GROUP, mentioned=False, owner_spoke=False, trigger=None, who=""))
    st.pitch["ts"] -= 120
    brain.llm.reply = J(act="say", kind="I2", to=1, len="short", say=["穿秋裤了没"], appraise={"timing": "现在", "fresh": True})
    assert asyncio.run(gc.pitch_tick(GROUP, st)) is True and ob.sent
    stats = gc.pitch_stats(GROUP)
    assert stats["queued"] == 2 and stats["sent"] == 1 and stats["skipped"] == 1


def test_pitch_dropped_when_stale(cfg, brain):
    gc, ob = _gc(cfg, brain)
    st = gc._st(GROUP)
    for i in range(10):
        gc._record(GROUP, FRIEND, "小王", f"第{i}句", i + 1)
    st.pitch = {"say": ["刚才那个"], "ts": time.time() - 90, "last_id": 1}
    assert asyncio.run(gc.pitch_tick(GROUP, st)) is True and st.pitch is None and not ob.sent
    assert gc.pitch_stats(GROUP).get("dropped_stale") == 1


def test_appraise_fix_unprompted_skips_when_rewrite_fails(cfg, brain):
    gc, ob = _gc(cfg, brain, cooldown_seconds=0, talkativeness=1.0)
    gc._record(GROUP, FRIEND, "小王", "我体检报告出来了", 1)
    brain.llm.reply = J(act="say", kind="I2", to=1, len="short", say=["哈哈你又胖了"],
                        appraise={"timing": "现在", "fresh": True, "face_risk": 3})
    assert not asyncio.run(gc.speak(GROUP, mentioned=False, owner_spoke=False, trigger=None, who="")) and not ob.sent


def test_borderline():
    class G:
        def __init__(self, **g):
            self.g = {"borderline_judge": True, "gate_judge": True, "borderline_band": 0.1, **g}

        def gk(self, gid):
            return self.g
    from aigroup.chat.social import SocialMixin
    bl = SocialMixin.borderline
    kw = dict(mentioned=False, explore=False, judged="", quiet=False)
    assert bl(G(), 1, p=0.45, thr=0.4, **kw)
    assert not bl(G(), 1, p=0.6, thr=0.4, **kw)
    assert not bl(G(), 1, p=0.45, thr=0.4, **{**kw, "mentioned": True})
    assert not bl(G(borderline_judge=False), 1, p=0.45, thr=0.4, **kw)


# ---------------- 人卡 / 硬底线 ----------------
def test_person_card(cfg, brain):
    gc, _ = _gc(cfg, brain)
    gc._record(GROUP, FRIEND, "小王", "今天加班到十点，好累", 1)
    gc.db.set(f"qq_spec_{GROUP}_{FRIEND}", {"ts": time.time(), "name": "小王", "how": ["他连发问号是在吐槽"],
                                             "tease_ok": ["游戏菜"], "tease_no": ["体重"]})
    card = gc.person_card(GROUP, FRIEND)
    assert card.startswith("小王（") and "体重" in card and "在忙" in card and len(card) <= 120
    msgs = asyncio.run(gc.build_messages(GROUP, False, False, "", None, query="加班", focus=[FRIEND]))
    u = msgs[1]["content"]
    assert "# 在聊的人" in u and "# 和小王相处" not in u                  # 人卡代替分开的几段
    assert gc.pick_ablation(GROUP, False, None) == ""                   # conftest 把消融关了


def test_hard_issues(cfg, brain):
    gc, _ = _gc(cfg, brain)
    brain.persona.ai_open, brain.persona.life = True, "free"
    assert any("幕后" in x for x in gc.hard_issues(GROUP, ["那篇报告里写我了"]))
    assert any("线下" in x for x in gc.hard_issues(GROUP, ["我刚下课"]))
    assert gc.hard_issues(GROUP, ["你们服务器又挂了？"]) == []           # 群友聊服务器不算
    gc.db.set(f"qq_state_{GROUP}_{FRIEND}", {"date": f"{brain.now():%Y-%m-%d}", "mood": "很糟", "n": 1, "ts": time.time()})
    assert any("状态不好" in x for x in gc.hard_issues(GROUP, ["笑死你也有今天"], [FRIEND]))
    brain.persona.ai_open = False
    assert not any("线下" in x for x in gc.hard_issues(GROUP, ["我刚下课"]))


# ---------------- 标注拆分 ----------------
def test_label_split_and_appraise_stats(cfg, brain):
    from aigroup.eval.label import answer, appraise_stats, stats
    gc, _ = _gc(cfg, brain)
    ids = [gc.gate.add(GROUP, False, 0.5, 0.4, True, {}) for _ in range(4)]
    for i in ids:
        gc.gate.mark(i, spoke=True)
    gc._appraise_log(GROUP, ids[0], {"timing": "now"}, "say")
    gc._appraise_log(GROUP, ids[1], {"timing": "now", "fresh": False}, "say")
    gc._appraise_log(GROUP, ids[2], {"timing": "wait"}, "wait")
    answer(brain.db, ids[0], 0, why0="时机不对")
    answer(brain.db, ids[1], 0, why0="说的不对")
    answer(brain.db, ids[2], 1)
    answer(brain.db, ids[3], 1, why0="说的不对")                          # 该接时不记原因
    s = stats(brain.db)
    assert s["n0"] == 1 and s["n0_content"] == 1 and s["n"] == 3
    a = appraise_stats(brain.db)
    assert a["too_early"] == 1 and a["too_late"] == 1 and a["content"] == 1 and a["content_caught"] == 1.0
    assert brain.db.conn.execute("SELECT human_why FROM qq_gate_log WHERE id=?", (ids[3],)).fetchone()[0] == ""
    assert gc.humane_metrics(GROUP)["fir"] == 1.0                       # 说的不对的不算进“不该说却说”


# ---------------- 她问了大家一句，有人回答：接住（10-22 中午“中午吃啥”→“我自己烧饭吃”她没接） ----------------
def test_answer_to_her_open_question_is_bid(cfg, brain):
    from aigroup.chat.humane import OPEN_Q
    assert OPEN_Q.search("中午吃啥，我这会儿只能看菜单") and not OPEN_Q.search("看这表情就知道了，我不问了")
    gc, ob = _gc(cfg, brain, cooldown_seconds=0)
    gc._record(GROUP, 0, "小满", "中午吃啥，我这会儿只能看菜单", 1, role="assistant")
    assert gc.bid_from(GROUP, FRIEND, "我自己烧饭吃") == "中午吃啥，我这会儿只能看菜单"
    assert gc.bid_from(GROUP, FRIEND, "我自己烧饭吃") == ""                 # 同一句只算一次


def test_bid_not_skipped_by_self_check(cfg, brain):
    from conftest import _msg, _run
    gc, ob = _gc(cfg, brain, cooldown_seconds=0)
    gc._record(GROUP, 0, "小满", "中午吃啥，我这会儿只能看菜单", 1, role="assistant")
    brain.llm.reply = J(act="say", kind="I2", to=2, len="short", say=["你还会烧饭？烧的啥"],
                        appraise={"timing": "现在", "fresh": True, "share_high": True})
    _run(gc, _msg(FRIEND, "小王", "我自己烧饭吃", 2))
    assert any("烧的啥" in str(x) for x in ob.sent)
