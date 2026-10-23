"""19.1 更像人：接住搭话、今天说过的不再说、没人理不追发、念头打分、相处说明书、像人的指标、盲测。"""
import asyncio
import time

from aigroup.chat.address import echo_today, plan_terms
from aigroup.chat.quality import check

from conftest import FRIEND, FRIEND2, GROUP, J, _gc, _msg, _run


def test_echo_today_and_plan_terms():
    morning = ["阿杰你 AG 的签运我还没看，别到时候又看纪录片看哭"]
    assert echo_today("@阿杰 AG 赢了别又看纪录片看哭", morning, names=["阿杰"]) == "又看纪录片看哭"
    assert echo_today("那你今天准备干嘛呀", ["你今天准备吃什么"]) == ""          # 只撞了常用词
    assert echo_today("阿杰早", ["阿杰你好"], names=["阿杰"]) == ""
    plan = "12:00-13:00 云看饭点，查了群里提过那家顺德桑拿鸡的评价，发现要排队，先记着"
    assert "桑拿鸡" in plan_terms(plan, ["周末有人有安排吗，我查了下顺德那家桑拿鸡要排队"])
    assert plan_terms(plan, []) == []


def test_quality_echo_penalty():
    s, hits = check("AG 赢了别又看纪录片看哭", echo=lambda t: "纪录片看哭" if "纪录片" in t else "")
    assert s >= 6 and any(h.startswith("今天说过") for h in hits)
    assert check("中午吃猪脚饭吧", echo=lambda t: "")[0] == 0


def test_bid_is_answered(cfg, brain):
    """她点过名的人，之后说的第一句没 @ 她也接（阿杰那句“你够了，哪壶不开提哪壶”）。"""
    gc, ob = _gc(cfg, brain, cooldown_seconds=600)
    brain.llm.reply = J(act="say", kind="I1", to=2, len="short", say=["小王今天别又看纪录片看哭"])
    _run(gc, _msg(FRIEND, "小王", "我今天去广州看比赛", 1), _msg(FRIEND2, "老张", "他今天去看比赛", 2, at=True))
    assert len(ob.sent) == 1
    brain.llm.reply = J(act="say", kind="I1", to=3, len="short", say=["哈哈好好好不提了"])
    _run(gc, _msg(FRIEND, "小王", "你够了哪壶不开提哪壶", 3))
    assert len(ob.sent) == 2
    user = brain.llm.group_calls[-1][1]["content"]
    assert "在接你之前对他说的话" in user
    m = gc.humane_metrics(GROUP)
    assert m["bids"] >= 1 and m["bids_caught"] >= 1


def test_no_monologue_and_today_block(cfg, brain):
    gc, ob = _gc(cfg, brain)
    gc._record(GROUP, FRIEND, "小王", "早", 1)
    gc._record(GROUP, 0, "小满", "LOUD 拿冠军了", 2, role="assistant")
    ok = asyncio.run(gc.speak(GROUP, mentioned=False, owner_spoke=False, trigger=None, who="", why="想冒个泡"))
    assert not ok and not ob.sent                                   # 她上一句还没人接：不追发
    assert gc.lonely_reason(GROUP) == "她上一句还没人接"
    gc._record(GROUP, FRIEND, "小王", "？", 3)
    assert gc.lonely_reason(GROUP) == ""
    gc._tmap[GROUP] = {}
    blk = gc.today_block(GROUP)
    assert "LOUD 拿冠军了" in blk


def test_self_score_skips_low_thoughts(cfg, brain):
    gc, ob = _gc(cfg, brain, cooldown_seconds=0, talkativeness=1.0)
    gc._record(GROUP, FRIEND, "小王", "今天好冷", 1)
    brain.llm.reply = J(act="say", kind="I2", to=1, len="short", say=["是啊好冷"],
                        score={"相关": 2, "信息差": 1, "效果": 1, "新意": 1, "时机": 3})
    ok = asyncio.run(gc.speak(GROUP, mentioned=False, owner_spoke=False, trigger=None, who=""))
    assert not ok and not ob.sent
    brain.llm.reply = J(act="say", kind="I2", to=1, len="short", say=["穿秋裤了没"],
                        score={"相关": 4, "信息差": 3, "效果": 4, "新意": 4, "时机": 4})
    ok = asyncio.run(gc.speak(GROUP, mentioned=False, owner_spoke=False, trigger=None, who=""))
    assert ok and ob.sent


def test_spec_block_and_blind(cfg, brain):
    gc, _ = _gc(cfg, brain)
    gc.db.set(f"qq_spec_{GROUP}_{FRIEND}", {"ts": time.time(), "name": "小王", "how": ["他连发问号是在吐槽，不是真问"],
                                             "tease_ok": ["游戏菜"], "tease_no": ["体重"], "care": "加班时想被关心"})
    blk = gc.spec_block(GROUP, [FRIEND])
    assert "连发问号" in blk and "体重" in blk
    assert "体重" in gc.spec_avoid(GROUP, [FRIEND])
    for i in range(6):
        gc._record(GROUP, 0, "小满", f"她说的第{i}句话挺长的", 100 + i, role="assistant")
        gc._record(GROUP, FRIEND, "小王", f"群友说的第{i}句话挺长的", 200 + i)
    d = gc.blind_set(GROUP, n=6)
    assert len(d["items"]) == 6
    truth = [x["i"] for x in d["items"] if x["text"].startswith("她说")]
    r = gc.blind_answer(GROUP, truth)
    assert r["ok"] and r["hit"] == r["total"] == len(truth)


def test_persona_gaps():
    from aigroup.persona import parse_persona
    p = parse_persona("---\nname: 小满\ngaps: [汽车, 股票]\n---\n# 她是谁\n小满")
    assert p.gaps == ["汽车", "股票"]
