"""23.1：别刷屏（5 分钟条数、接话也要过门槛）、被嫌了先安静、这条回过了、装看过视频、口头禅、被回避过的问题别再问、
拿着截图反驳时别硬顶。"""
import asyncio
import time

from conftest import FRIEND, FRIEND2, GROUP, J, _gc

from aigroup.chat.plan import decide
from aigroup.chat.settings import DEFAULTS


def _g(**kw):
    return {**DEFAULTS, **kw}


def test_decide_burst_and_hush():
    now = time.time()
    base = dict(mentioned=False, now=now, last_reply_ts=now - 5, replies_10min=1, quiet=False, p=0.95, thr=0.5)
    assert decide(g=_g(), waited=True, burst=1, **base) == (True, "等的人开口了")
    ok, why = decide(g=_g(cooldown_seconds=10), waited=True, burst=3, **base)
    assert not ok and why == "冷却中"                                   # 说到 3 条以后：接她的话也要过冷却
    ok, why = decide(g=_g(cooldown_seconds=0), waited=True, burst=3, **{**base, "p": 0.3})
    assert not ok and "不太想插话" in why                              # 也要过门槛
    ok, why = decide(g=_g(cooldown_seconds=0), waited=True, burst=5, **base)
    assert not ok and "5 分钟" in why
    ok, why = decide(g=_g(), waited=True, hush="你为什么要回答两次", **base)
    assert not ok and "安静" in why
    assert decide(g=_g(), hush="x", burst=9, **{**base, "mentioned": True})[0]     # 被叫到照样回


def test_huh_signal_hushes(cfg, brain):
    gc, _ = _gc(cfg, brain)
    st = gc._st(GROUP)
    st.last_reply_ts = time.time() - 10
    assert not gc.huh_signal(GROUP, FRIEND, "？")                      # 一次“？”：只记着
    assert gc.huh_signal(GROUP, FRIEND2, "？？")                       # 10 分钟里第二次：安静
    assert gc.hushed(GROUP)
    gc2, _ = _gc(cfg, brain)
    gc2._st(GROUP).last_reply_ts = time.time() - 10
    assert gc2.huh_signal(GROUP, FRIEND, "你为什么要回答两次")          # 一句就够
    gc3, _ = _gc(cfg, brain)
    gc3._st(GROUP).last_reply_ts = time.time() - 300
    assert not gc3.huh_signal(GROUP, FRIEND, "？")                     # 她好一会儿没说话：不是在嫌她


def _say(gc, brain, text, **kw):
    brain.llm.reply = J(act="say", kind="I2", to=1, len="short", say=[text], move="接梗")
    return asyncio.run(gc.speak(GROUP, mentioned=False, owner_spoke=False, trigger=None, who="", **kw))


def test_answered_check(cfg, brain):
    gc, ob = _gc(cfg, brain, cooldown_seconds=0, self_score=False)
    gc._record(GROUP, FRIEND, "小王", "片尾换首歌吧", 1)

    async def rw(messages, raw, hits, gid):
        return [["好"]] if not any("已经回过了" in h for h in hits) else []
    gc._rewrite = rw
    assert _say(gc, brain, "换首歌整个味道就变了") and ob.sent
    ob.sent.clear()
    assert not _say(gc, brain, "这提议靠谱，换首歌调子就不一样了") and not ob.sent
    r = gc.db.conn.execute("SELECT detail FROM qq_check_log WHERE name='answered'").fetchone()
    assert "已经回过了" in r["detail"]


def test_media_claim_and_catchphrase(cfg, brain):
    gc, _ = _gc(cfg, brain)
    gc._record(GROUP, FRIEND, "小王", "[视频]", 1)
    assert "打不开" in gc._media_claim(GROUP, "前面那段可以砍一半", time.time())
    assert gc._media_claim(GROUP, "等你说说讲了啥", time.time()) == ""
    gc._record(GROUP, FRIEND, "小王", "[图片：一张截图]", 2)
    for i, t in enumerate(["我备忘录记岔了", "这条记进备忘录"]):
        gc._record(GROUP, 10001, "小满", t, 10 + i, role="assistant")
    assert "备忘录" in gc._catchphrase(GROUP, "行，备忘录上划掉")
    gc._record(GROUP, FRIEND, "小王", "你的备忘录里有什么", 20)
    assert gc._catchphrase(GROUP, "备忘录里都是你") == ""               # 人家在问小本本：接着说不算口头禅


def test_rebuffed_plan_not_asked_again(cfg, brain):
    gc, _ = _gc(cfg, brain)
    now = time.time()
    gc.db.conn.execute("INSERT INTO qq_plans(group_id, user_id, who, what, due, kind, status, created, fired) "
                       "VALUES (?,?,?,?,?,?,?,?,?)", (GROUP, FRIEND, "小王", "问那场比赛到底赢没赢，被回“别鞭尸”",
                                                      now - 86400, "ask", "done", now - 2 * 86400, now - 86400))
    gc.db.conn.commit()
    assert "对方不想说" in gc._ask_repeat(GROUP, "那场结果出了没，不说也行", FRIEND, now)
    assert gc._ask_repeat(GROUP, "晚上吃啥？", FRIEND, now) == ""


def test_disputed_by_screenshot_even_with_web(cfg, brain):
    gc, _ = _gc(cfg, brain)
    gc._record(GROUP, 10001, "小满", "这活动9月底就结束了", 1, role="assistant")
    gc._record(GROUP, FRIEND, "小王", "上面不是写着到10月31日吗", 2)
    w = gc._disputed(GROUP, "买一赠一9月底就撤了", web="……买一赠一 9月底 结束……")
    assert "看着图" in w
