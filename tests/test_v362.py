"""36.2：《复盘 11-10 突然翻旧账》+ 11-09 日报 P1～P4（都不联网）。"""
import asyncio
import time
from datetime import date, datetime, timedelta

from conftest import FRIEND, FRIEND2, GROUP, SELF, _gc, _msg, _run

from aigroup.chat.plan import decide
from aigroup.chat.settings import DEFAULTS
from aigroup.chat.v362 import cue_dates, plan_evidence

D = date(2026, 11, 3)          # 星期二


def test_cue_dates():
    assert cue_dates("我明天二面", D) == {D + timedelta(days=1)}
    assert cue_dates("后天考试", D) == {D + timedelta(days=2)}
    assert cue_dates("大后天出发", D) == {D + timedelta(days=3)}
    assert cue_dates("周四晚上开黑", D) == {date(2026, 11, 5)}
    assert cue_dates("下周二面试", D) == {date(2026, 11, 10)}
    assert cue_dates("周一体检", D) == {date(2026, 11, 9)}                  # 周二说“周一”：下周一
    assert cue_dates("11.8打球，单打十二点、双打六点", D) == {date(2026, 11, 8)}
    assert cue_dates("11月8号擂台赛", D) == {date(2026, 11, 8)}
    assert cue_dates("8号去", D) == {date(2026, 11, 8)}
    assert cue_dates("晚上八点开黑", D) == {D, D + timedelta(days=1)}
    assert cue_dates("双11再买", D) == {date(2026, 11, 11)}
    # 定不了哪天的、图上的字、折扣：都不算
    for t in ("买完记得说说效果", "过几天再看", "一周见效", "8.5折起", "[图片：天猫双11促销页，「1周淡眼圈」「7天 改善黑眼圈」]",
              "淡化黑眼圈的眼霜还有美白精华"):
        assert not cue_dates(t, D), t


def _rows(*items):
    base = datetime(2026, 11, 3, 9, 14).timestamp()
    return [{"ts": base + i * 60, "role": r, "content": c} for i, (r, c) in enumerate(items)]


def test_plan_evidence_eye_cream():
    """11-03 聊推荐眼霜，没人说日期，整理却记成 11-10 —— 现在不记。"""
    rows = _rows(("user", "@你 淡化黑眼圈的眼霜还有美白精华"), ("assistant", "眼霜看咖啡因的，精华挑烟酰胺"),
                 ("user", "你直接推荐几个牌子过来"), ("assistant", "买完记得说说效果"),
                 ("user", "[图片：天猫双11促销页，「平滑泡泡眼 1周淡眼圈」「5%咖啡因眼部精华液」「7天 改善黑眼圈」]"),
                 ("user", "我俩可以一起买凑单"))
    due = datetime(2026, 11, 10, 21, 0)
    assert plan_evidence("买眼霜精华", due, "", rows) == ""
    assert plan_evidence("买眼霜精华", due, "买完记得说说效果", rows) == ""          # 她自己说的不算，也没日期
    assert plan_evidence("买眼霜精华", due, "我俩可以一起买凑单", rows) == ""        # 原话在，但没说哪天


def test_plan_evidence_ok():
    rows = _rows(("user", "我明天二面，紧张"), ("user", "11.8打球，单打十二点、双打六点"))
    assert plan_evidence("二面", datetime(2026, 11, 4, 21), "我明天二面", rows)
    assert not plan_evidence("二面", datetime(2026, 11, 5, 21), "我明天二面", rows)      # 日期对不上
    assert plan_evidence("打球", datetime(2026, 11, 8, 12), "", rows)                   # 老格式没原话：在说到的消息里找
    assert not plan_evidence("二面", datetime(2026, 11, 4, 21), "我后天二面", rows)      # 原话不在聊天里


def test_digest_drops_invented_date_and_dupes(cfg, brain):
    gc, _ = _gc(cfg, brain)
    gc._record(GROUP, FRIEND, "小王", "@你 淡化黑眼圈的眼霜还有美白精华", 90)
    gc._record(GROUP, FRIEND2, "老张", "我俩可以一起买凑单", 91)
    week = (brain.now() + timedelta(days=7)).strftime("%Y-%m-%d")
    lines = [f"约|{FRIEND}|买眼霜精华|{week}|问", f"约|{FRIEND2}|买眼霜精华|{week}|问"]
    assert gc.digest_extra_apply(GROUP, lines, [FRIEND, FRIEND2]) == (0, 0)
    gc._record(GROUP, FRIEND, "小王", "我明天面试", 92)
    gc._record(GROUP, FRIEND2, "老张", "我也明天面试", 93)
    tm = (brain.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    lines = [f"约|{FRIEND}|面试|{tm}|问|我明天面试", f"约|{FRIEND2}|面试|{tm}|问|我也明天面试"]
    assert gc.digest_extra_apply(GROUP, lines, [FRIEND, FRIEND2]) == (0, 1)              # 同一批同一件事只记一条


def test_add_plan_dedupes_across_people(cfg, brain):
    gc, _ = _gc(cfg, brain)
    tm = (brain.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    assert gc.add_plan(GROUP, FRIEND, "小王", "买眼霜精华", tm) > 0
    assert gc.add_plan(GROUP, FRIEND2, "老张", "买眼霜精华", tm) == 0
    assert gc.add_plan(GROUP, FRIEND2, "老张", "考研考试", tm) > 0


def test_cheer_only_events_and_fresh(cfg, brain):
    gc, _ = _gc(cfg, brain)
    now = time.time()
    p = {"what": "买眼霜精华", "created": now - 3600}
    assert "不是考试" in gc.plan_cheer_ok(GROUP, p, now)
    p = {"what": "二面", "created": now - 7 * 86400}
    assert "天前记的" in gc.plan_cheer_ok(GROUP, p, now)
    assert gc.plan_cheer_ok(GROUP, {"what": "11.8打球，单打十二点", "created": now - 86400}, now) == ""
    assert gc.plan_cheer_ok(GROUP, {"what": "面试", "created": now - 86400}, now) == ""


def test_cheer_skipped_in_tick(cfg, brain):
    """11-10 10:09“今天要「买眼霜精华」打个气”：现在不说，也记下“打过气”免得反复看。"""
    gc, ob = _gc(cfg, brain, reply="咖啡因那支，闭眼买")
    hour = brain.now().hour
    due = time.time() + 8 * 3600
    pid = gc.db.conn.execute("INSERT INTO qq_plans(group_id, user_id, who, what, due, all_day, kind, created) VALUES (?,?,?,?,?,?,?,?)",
                             (GROUP, FRIEND, "小王", "买眼霜精华", due, 1, "ask", time.time() - 7 * 86400)).lastrowid
    gc.db.conn.commit()
    _run(gc, _msg(FRIEND, "小王", "早", 1))
    ob.sent.clear()
    assert asyncio.run(gc.plans_tick(GROUP, gc._st(GROUP))) is False and not ob.sent
    if 8 <= hour < 23:
        assert gc.db.get(f"qq_plan_cheer_{pid}")


def test_plan_resolved_by_result_and_stale(cfg, brain):
    gc, ob = _gc(cfg, brain)
    tm = (brain.now() + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M")
    pid = gc.add_plan(GROUP, FRIEND, "小王", "面试", tm)
    p = dict(gc.db.conn.execute("SELECT * FROM qq_plans WHERE id=?", (pid,)).fetchone())
    assert gc.plan_resolved(GROUP, p) == ""
    gc._record(GROUP, FRIEND, "小王", "面试面完了，感觉还行", 95, ts=time.time() + 120)
    assert "结果" in gc.plan_resolved(GROUP, p, time.time() + 200)
    # 有人嫌她翻旧账：同一话题的约定也作废
    gc2, _ = _gc(cfg, brain)
    pid = gc2.add_plan(GROUP, FRIEND, "小王", "买眼霜精华", tm)
    p = dict(gc2.db.conn.execute("SELECT * FROM qq_plans WHERE id=?", (pid,)).fetchone())
    gc2._record(GROUP, SELF, "小满", "咖啡因那支眼霜，闭眼买", 96, role="assistant", ts=time.time() + 100)
    gc2._record(GROUP, FRIEND2, "老张", "那不是很久以前的事情了吗", 97, ts=time.time() + 120)
    assert "旧事" in gc2.plan_resolved(GROUP, p, time.time() + 200)


def test_stale_closes_topic(cfg, brain):
    gc, _ = _gc(cfg, brain)
    gc._record(GROUP, SELF, "小满", "@小王 咖啡因那支，闭眼买", 95, role="assistant", ts=time.time() - 70)
    gc._record(GROUP, SELF, "小满", "The Ordinary那支眼精华，消肿的", 96, role="assistant", ts=time.time() - 60)
    gc._record(GROUP, FRIEND2, "老张", "那不是很久以前的事情了吗", 97, ts=time.time() - 30)
    assert gc._closed_hit(GROUP, "那瓶眼精华用了没", time.time())
    assert not gc._closed_hit(GROUP, "今晚BLG打T1", time.time())


def test_waited_only_once():
    g = {**DEFAULTS}
    now = time.time()
    kw = dict(mentioned=False, now=now, last_reply_ts=now - 13, replies_10min=1, quiet=False, g=g, p=0.9, thr=0.4, waited=True)
    assert decide(**kw, burst=0) == (True, "等的人开口了")
    ok, why = decide(**kw, burst=1, waited_recent=True)                 # 11-09 13:32:52：刚靠它说过一句
    assert not ok and why == "冷却中"
    ok, why = decide(**kw, burst=2)                                     # 5 分钟里已经 2 条
    assert not ok and why == "冷却中"
    assert decide(**{**kw, "g": {**g, "waited_once": False}}, burst=1, waited_recent=True) == (True, "等的人开口了")


def test_fake_past_blocked(cfg, brain):
    gc, _ = _gc(cfg, brain, check_critic="off")
    gc._record(GROUP, FRIEND2, "老张", "周三开始放秋假", 90)
    out = asyncio.run(gc.checks22(GROUP, ["秋假？我上学那会儿哪有这东西"]))
    assert out and "过去" in out[0]
    assert not asyncio.run(gc.checks22(GROUP, ["秋假也太爽了，九天"]))
    assert gc._fake_past(GROUP, "他说“我当年也这样”") == ""              # 引号里转述的不算


def test_brand_unseen_blocked(cfg, brain):
    gc, _ = _gc(cfg, brain, check_critic="off")
    gc._record(GROUP, FRIEND, "小王", "[图片：羽毛球馆内的照片，网架底座为红色，字迹模糊。图中未见清晰数字。看不出是干嘛的。]", 90)
    gc._record(GROUP, FRIEND2, "老张", "这啥球", 91)
    out = asyncio.run(gc.checks22(GROUP, ["就是那个银绿色包装的YY比赛球吧"]))
    assert out and "牌子" in out[0]
    assert not asyncio.run(gc.checks22(GROUP, ["看不清，啥牌子的？"]))
    gc._record(GROUP, FRIEND, "小王", "亚狮龙，一桶300", 92)
    assert gc._brand_unseen(GROUP, "亚狮龙贵是贵") == ""                   # 群里说过的照说
    assert gc._brand_unseen(GROUP, "这个超纲了，CS我平时看得少") == ""      # 10-29：不是在认图里的东西


def test_feed_only_talked_and_today(cfg, brain):
    gc, _ = _gc(cfg, brain)
    gc._feed_init()
    today = brain.now().date()
    gc.db.set(f"feed_interests_{GROUP}", [{"topic": "KPL", "w": 0.9}, {"topic": "LOL", "w": 0.8}])
    for topic, title, d in (("KPL", "JDG4:2WB 晋级年度总决赛", today), ("LOL", "BLG 3:1 T1 进决赛", today),
                            ("LOL", "昨天的 LOL 新闻", today - timedelta(days=1))):
        gc.db.conn.execute("INSERT INTO qq_feed(group_id, topic, title, date, ts) VALUES (?,?,?,?,?)",
                           (GROUP, topic, title, d.isoformat(), time.time()))
    gc.db.conn.commit()
    gc._record(GROUP, FRIEND, "小王", "明天blg能过t1吗，s赛十三年了", 90)
    titles = [c["title"] for c in gc.feed_candidates(GROUP)]
    assert titles == ["BLG 3:1 T1 进决赛"]


def test_proactive_waits_for_pending_reply(cfg, brain):
    gc, ob = _gc(cfg, brain, reply="长沙下雨带伞")
    gc._record(GROUP, FRIEND, "小王", "@你 fx是什么啊", 90, to_her=True)
    assert gc.pending_to_her(GROUP)
    past = time.strftime("%Y-%m-%d %H:%M", time.localtime(time.time() - 3 * 3600))
    gc.add_plan(GROUP, FRIEND, "小王", "考试", past, "ask")
    assert asyncio.run(gc.plans_tick(GROUP, gc._st(GROUP))) is False and not ob.sent
    gc._record(GROUP, SELF, "小满", "fx就是外汇", 91, role="assistant")
    assert gc.pending_to_her(GROUP) == ""


def test_venting_one_reply(cfg, brain):
    gc, _ = _gc(cfg, brain, social_read="on")
    now = time.time()
    s, n = gc.room_verdict(GROUP, {"venting": "小王", "about_her": "none"}, mentioned=False, guess="", now=now)
    assert not s and "不替他领导催" in n
    gc._st(GROUP).last_reply_ts = now - 20
    s, _ = gc.room_verdict(GROUP, {"venting": "小王", "about_her": "none"}, mentioned=False, guess="to:x", now=now)
    assert "先听他说" in s
    s, _ = gc.room_verdict(GROUP, {"venting": "小王", "about_her": "none"}, mentioned=False, guess="", now=now, asked=True)
    assert not s                                                         # 真问她了照答
    s, _ = gc.room_verdict(GROUP, {"venting": "小王", "about_her": "none"}, mentioned=True, guess="", now=now)
    assert not s


def test_life_known_events_recent_only(cfg, brain):
    gc, _ = _gc(cfg, brain)
    now = time.time()
    for what, created in (("买眼霜精华", now - 6 * 86400), ("考研考试", now - 86400)):
        gc.db.conn.execute("INSERT INTO qq_plans(group_id, user_id, who, what, due, all_day, kind, created) VALUES (?,?,?,?,?,?,?,?)",
                           (GROUP, FRIEND, "小王", what, now + 86400, 1, "ask", created))
    gc.db.conn.commit()
    k = brain.known_events(brain.now())
    assert "考研考试" in k and "眼霜" not in k


def test_failure_modes_and_enforce():
    from aigroup.chat.checks import FAILURE_MODES
    # E 看不清的图认牌子交给规则（check_price_src 名下）：第 5 条是强制拦的，写进去自检会把“咖啡因那款”这种正常回答也拦掉
    assert "风凉话" in FAILURE_MODES[18] and "我上学那会儿" in FAILURE_MODES[7] and "牌子" not in FAILURE_MODES[4]
    assert "没由头" in FAILURE_MODES[22]
    assert 19 in DEFAULTS["critic_enforce"] and 24 in DEFAULTS["critic_enforce"]


def test_far_plan_not_resolved_early(cfg, brain):
    gc, _ = _gc(cfg, brain)
    far = (brain.now() + timedelta(days=20)).strftime("%Y-%m-%d")
    pid = gc.add_plan(GROUP, FRIEND, "小王", "考研考试", far)
    p = dict(gc.db.conn.execute("SELECT * FROM qq_plans WHERE id=?", (pid,)).fetchone())
    gc._record(GROUP, FRIEND, "小王", "考研复习完了一半，拿到真题了", 95, ts=time.time() + 120)
    assert gc.plan_resolved(GROUP, p, time.time() + 200) == ""
