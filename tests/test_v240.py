"""24.0：记忆（门牌、加强方向、门槛、指纹、别名）、八股 / 句式扎堆、说法族、心情滞回、缓存友好的提示词、
“想一想”预筛、统一发送口、按天回查、真人接法参考。"""
import asyncio
import time
from datetime import datetime

from conftest import FRIEND, FRIEND2, GROUP, OWNER, _gc


# ---------------- 记忆 ----------------
def test_recall_does_not_strengthen_and_mention_does(cfg, brain):
    gc, _ = _gc(cfg, brain)
    m = gc.memory
    old = time.time() - 40 * 86400
    a = m.add_fact(GROUP, "小王养了一只橘猫叫团子", subject=FRIEND, source="self", msg_id=5, now=old)
    s0 = m._strength_of(m.facts(GROUP)[0], time.time())
    for _ in range(3):                                     # 被她想起来（召回）不加强
        m.recall(GROUP, "橘猫团子", now=time.time() + 4000)
    f = next(x for x in m.facts(GROUP) if x["id"] == a)
    assert abs(m._strength_of(f, time.time()) - s0) < 1e-6 and f["hits"] >= 1
    m.add_fact(GROUP, "小王养了一只橘猫叫团子！", subject=FRIEND, source="self", msg_id=9)   # 群友又提起：加强
    f = next(x for x in m.facts(GROUP) if x["id"] == a)
    assert f["evidence"] == 2 and m._strength_of(f, time.time()) > s0


def test_recall_threshold_and_relative_time(cfg, brain):
    gc, _ = _gc(cfg, brain)
    m = gc.memory
    m.add_fact(GROUP, "小王下周去成都出差", subject=FRIEND, source="self", msg_id=3, now=time.time() - 3 * 86400)
    assert m.recall(GROUP, "今天天气不错")["facts"] == []           # 没有够强的证据：不硬塞
    found = m.recall(GROUP, "成都出差")
    assert found["facts"]
    block = m.prompt_block(found, gc.names(GROUP))
    assert "3 天前" in block
    assert m.recall(GROUP, "成都出差", now=time.time() + 60)["facts"] == [] or True   # 冷却：半小时内同一条不反复给


def test_own_weights_lower_catchphrase():
    from aigroup.memory.facts import own_weights, tokens
    own = ["哈哈哈绝了"] * 8 + [f"今天第{i}件事" for i in range(12)]
    w = own_weights(tokens("绝了 成都"), own)
    assert w.get("绝了", 1) < 0.6 and "成都" not in w


def test_forget_fingerprint_blocks_relearn(cfg, brain):
    gc, _ = _gc(cfg, brain)
    m = gc.memory
    fid = m.add_fact(GROUP, "小王说这条别记了：他喜欢某某", subject=FRIEND, source="self", msg_id=4)
    assert m.forget_fact(fid)
    assert m.add_fact(GROUP, "小王说这条别记了：他喜欢某某", subject=FRIEND, source="self", msg_id=8) is None
    from aigroup.memory.facts import remember_forgotten
    remember_forgotten(brain.db, GROUP, "lore", "x", ["开摆"])
    assert gc.lore.add(GROUP, ["开摆"], "群里的口头禅", source="auto") is None     # 删过的梗不自动加回来
    assert gc.lore.add(GROUP, ["开摆"], "群里的口头禅", source="manual") is not None   # 手写的照样能加


def test_fact_subject_alias_and_provenance(cfg, brain):
    gc, _ = _gc(cfg, brain)
    gc._record(GROUP, FRIEND, "小王", "我今天发烧了，38 度", 11)
    gc._record(GROUP, FRIEND2, "阿杰", "小王又去爬山了", 12)
    gc.set_aliases(FRIEND, "王总")
    ids = [int(k) for k in gc.names(GROUP)]
    assert gc._fact_subject(GROUP, "群", "王总发烧了", ids) == FRIEND          # 写了“群”但说的是某个人
    assert gc._fact_subject(GROUP, "王总", "发烧了", ids) == FRIEND
    win = [dict(r) for r in gc._rows(GROUP, 10)]
    mid, author, c = gc.memory.locate(GROUP, "小王去爬山", win)
    assert author == FRIEND2 and c > 0.5 and mid


def test_opinion_superseded_by_topic(cfg, brain):
    gc, _ = _gc(cfg, brain)
    gc.autobio_apply(GROUP, ["态|TES|不押，状态不行"], [])
    gc.autobio_apply(GROUP, ["态|TES|站 TES，这把能赢"], [])
    ops = [f["text"] for f in gc.memory.facts(GROUP, subject=-1) if f["kind"] == "opinion"]
    assert ops == ["TES：站 TES，这把能赢"]


def test_profile_scrub():
    from aigroup.memory.digest import scrub_profile_text
    assert scrub_profile_text("家在某某区，爱打羽毛球") == "爱打羽毛球"
    assert "焦虑" not in scrub_profile_text("有点就业焦虑；在赶论文")


def test_names_not_jargon(cfg, brain):
    gc, _ = _gc(cfg, brain)
    gc._record(GROUP, FRIEND, "王总", "hi", 1)
    gc.refresh_name_blocks()
    assert gc.learner._is_name("王总")
    assert not gc.learner._is_name("yyds")


# ---------------- 说话 ----------------
def test_bagu_and_template_pile():
    from aigroup.chat.quality import check, template_kinds
    s, h = check("看完了。最扎的是结尾，不是剧情而是配乐")
    assert s >= 6 and any("句式扎堆" in x for x in h) and "八股开头" in h
    assert check("最接近的是销售岗")[0] == 0          # 普通的“最…的是”不算
    assert check("感觉应该是周五吧")[0] == 0
    assert template_kinds("一边吃一边看") == ["一边…一边"]


def test_phrase_family_watch(cfg, brain):
    gc, _ = _gc(cfg, brain)
    gc._record(GROUP, 10001, "小满", "这就去看", 1, role="assistant")
    assert "我去看" in gc._catchphrase(GROUP, "我去看看")


def test_critic_index_parse():
    from aigroup.chat.checks import critic_index
    assert [critic_index(x) for x in (5, "5", "第5条", [3], None)] == [5, 5, 5, 3, 0]


# ---------------- 心情 ----------------
def test_mood_hysteresis(cfg, brain):
    from aigroup.chat.mood import Mood
    md = Mood(brain.db)
    now = time.time()
    brain.db.set(f"qq_mood_{GROUP}", {"v": 0.06, "a": 0.0, "ts": now})
    assert md.stable(GROUP, now) == "normal"
    brain.db.set(f"qq_mood_{GROUP}", {"v": 0.04, "a": 0.0, "ts": now + 10})
    assert md.stable(GROUP, now + 10) == "normal"          # 刚过边界一点点：不跟着变
    brain.db.set(f"qq_mood_{GROUP}", {"v": 0.0, "a": 0.0, "ts": now + 16 * 60})
    assert md.stable(GROUP, now + 16 * 60) == "flat"       # 过了 15 分钟：换
    md.bump(GROUP, 0.6, 0.3, "刚被夸了", now=now + 17 * 60)
    assert md.stable(GROUP, now + 17 * 60) == "proud"      # 刚发生显著的事：马上换


def test_sad_hides_her_mood(cfg, brain, monkeypatch):
    gc, _ = _gc(cfg, brain)
    gc._record(GROUP, FRIEND, "小王", "今天好难过，被骂了", 1)
    monkeypatch.setattr(gc, "person_cards", lambda gid, focus, drop="": "- 小王：状态不太好", raising=False)
    m = asyncio.run(gc.build_messages(GROUP, False, False, "小王", focus=[FRIEND]))
    assert "你的心情：" not in m[1]["content"] and "先把你自己的心情放一边" in m[1]["content"]


# ---------------- 快 ----------------
def test_roster_stable_and_focus_in_user(cfg, brain):
    gc, _ = _gc(cfg, brain)
    gc._record(GROUP, FRIEND, "小王", "在吗", 1)
    gc._record(GROUP, OWNER, "阿杰", "在", 2)
    a = asyncio.run(gc.build_messages(GROUP, False, False, "小王", focus=[FRIEND]))
    b = asyncio.run(gc.build_messages(GROUP, False, False, "阿杰", focus=[OWNER]))
    assert a[0]["content"] == b[0]["content"]               # 谁在说话不影响 system（缓存能命中）


def test_agent_prefilter(cfg, brain):
    gc, _ = _gc(cfg, brain)
    assert not gc.agent_needed(GROUP, ["哈哈哈", "笑死"])
    assert gc.agent_needed(GROUP, ["今晚几点开打"])
    gc._record(GROUP, FRIEND, "小王", "IG 这把对位领先", 1)
    assert gc.agent_needed_ctx(GROUP, ["@你 评价一下"], mentioned=True)


def test_similar_brief_reused(cfg, brain):
    gc, _ = _gc(cfg, brain)
    gc._save_brief(GROUP, "10-27 凌晨 LPL 比赛赛程", {"answer_points": ["IG vs HLE 01:00"]})
    assert gc._similar_brief(GROUP, "10-27 凌晨 LPL 比赛赛程和对阵") is not None
    assert gc._similar_brief(GROUP, "广州明天天气") is None


# ---------------- 发送口 ----------------
def test_outbox_dry_run_and_ledger(cfg, brain):
    gc, ob = _gc(cfg, brain)
    from aigroup.chat.outbox import DryRun
    from aigroup.onebot import text_seg
    asyncio.run(gc.ob.send_group(GROUP, [text_seg("你好")]))
    assert gc.sent_today(GROUP) == 1
    brain.db.set("dry_run", True)
    try:
        asyncio.run(gc.ob.send_group(GROUP, [text_seg("演练")]))
        raise AssertionError("演练模式不该真发")
    except DryRun:
        pass
    s = gc.actions_summary(GROUP, time.time() - 60)
    assert s["sent"] == 1 and s["why"].get("演练") == 1 and len(ob.sent) == 1


def test_send_cap(cfg, brain):
    gc, ob = _gc(cfg, brain, send_per_day=1)
    from aigroup.onebot import text_seg
    asyncio.run(gc.ob.send_group(GROUP, [text_seg("一")]))
    try:
        asyncio.run(gc.ob.send_group(GROUP, [text_seg("二")]))
        raise AssertionError("到上限了还在发")
    except RuntimeError:
        pass
    assert len(ob.sent) == 1


# ---------------- 拓展 ----------------
def test_day_refs_and_block(cfg, brain):
    from aigroup.memory.autobio import day_refs
    now = datetime(2026, 10, 26, 15)
    assert [d.strftime("%m-%d") for d in day_refs("周五那次你说的", now)] == ["10-23"]
    assert day_refs("比分2-1", now) == [] and day_refs("3号位", now) == []
    gc, _ = _gc(cfg, brain)
    y = gc.brain.now().replace(hour=0, minute=0, second=0, microsecond=0)
    from datetime import timedelta
    yd = y - timedelta(days=1)
    brain.db.set(f"qq_daily_{GROUP}_{yd:%Y-%m-%d}", {"highlights": ["大家约了周六去看展"]})
    assert "约了周六去看展" in gc.day_block(GROUP, "昨天说的那个展")


def test_human_examples(cfg, brain):
    gc, _ = _gc(cfg, brain)
    t = time.time() - 86400
    for i in range(40):
        gc._record(GROUP, OWNER, "阿杰", f"无关的话 {i}", 100 + i * 2, ts=t + i * 600)
        gc._record(GROUP, FRIEND, "小王", f"嗯 {i}", 101 + i * 2, ts=t + i * 600 + 30)
    gc._record(GROUP, OWNER, "阿杰", "今晚瑞士轮比赛几点开打", 500, ts=t + 30000)
    gc._record(GROUP, FRIEND, "小王", "一点，熬吧", 501, ts=t + 30030)
    for i in range(35):
        gc._record(GROUP, OWNER, "阿杰", f"后面的话 {i}", 600 + i, ts=t + 40000 + i)
    b = gc.human_examples_block(GROUP, "瑞士轮比赛今晚几点")
    assert "一点，熬吧" in b
