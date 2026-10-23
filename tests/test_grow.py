"""4.2 人物慢慢长：熟悉度、喜好不改口、约好的事、被纠正了就改、每月整理、分享（都不联网）。"""
import asyncio
import time

from conftest import FRIEND, GROUP, OWNER, _gc, _msg, _run


def _json(brain, answers):
    async def j(messages, default=None, role="helper"):
        last = messages[-1]["content"]
        for k, v in answers.items():
            if k in last:
                return v
        return default
    brain.llm.json = j


def test_closeness_grows_and_tiers(cfg, brain):
    gc, ob = _gc(cfg, brain)
    assert gc.closeness_label(OWNER) == "老朋友"
    for i in range(12):
        gc._record(GROUP, FRIEND, "小王", f"@你 在吗 {i}", 100 + i, to_her=True)
    gc.db.conn.execute("INSERT OR IGNORE INTO qq_people(user_id, name, msgs, last_seen) VALUES (?,?,?,?)",
                       (FRIEND, "小王", 12, time.time()))
    gc.db.conn.commit()
    before = gc.closeness(FRIEND)
    assert gc.update_closeness(brain.now()) == 1           # 今天（测试里当“昨天”算）
    after = gc.closeness(FRIEND)
    assert after > before
    assert gc.person(FRIEND)["familiarity"] == gc.closeness_label(FRIEND)
    assert "熟悉度怎么相处" in gc.roster(GROUP)


def test_prefs_and_plans_from_digest(cfg, brain):
    gc, ob = _gc(cfg, brain)
    tomorrow = time.strftime("%Y-%m-%d", time.localtime(time.time() + 86400))
    lines = ["好|香菜|讨厌，一口都不吃", f"约|群|周四开黑七日世界|{tomorrow} 20:00|提醒",
             f"约|{FRIEND}|二面|{tomorrow}|问", "约|群|乱写的|不是时间|提醒"]
    assert gc.digest_extra_apply(GROUP, lines, [FRIEND]) == (1, 2)
    assert not gc.add_pref("香菜", "其实还行")                   # 说出口的不改口
    assert "香菜：讨厌" in gc.prefs_block()
    ps = gc.plans(GROUP)
    assert [p["kind"] for p in ps] == ["remind", "ask"] or [p["kind"] for p in ps] == ["ask", "remind"]
    assert gc.digest_extra_apply(GROUP, [f"约|群|周四开黑七日世界|{tomorrow} 20:00|提醒"], [FRIEND]) == (0, 0)  # 不重复记
    msgs = asyncio.run(gc.build_messages(GROUP, False, False, "小王"))
    assert "香菜：讨厌" in msgs[0]["content"]


def test_plan_fires_once(cfg, brain):
    gc, ob = _gc(cfg, brain, reply="几点了人呢")
    soon = time.strftime("%Y-%m-%d %H:%M", time.localtime(time.time() + 20 * 60))
    pid = gc.add_plan(GROUP, 0, "大家", "开黑", soon, "remind")
    assert pid
    _run(gc, _msg(FRIEND, "小王", "随便聊聊", 1))
    ob.sent.clear()
    st = gc._st(GROUP)
    assert asyncio.run(gc.plans_tick(GROUP, st)) is True
    assert "时间快到了" in brain.llm.group_calls[-1][-1]["content"]
    assert asyncio.run(gc.plans_tick(GROUP, st)) is False          # 说过一次就不再说
    assert gc.plans(GROUP, "done")[0]["id"] == pid


def test_fix_knowledge(cfg, brain):
    gc, ob = _gc(cfg, brain)
    kid = gc.knowledge.add(GROUP, "禁忌岛", ["禁忌岛"], "七日世界新剧本，10月25日上线")
    gc._record(GROUP, 10001, "小满", "禁忌岛25号上线", 50, role="assistant")
    _json(brain, {"纠正": {"fix": True, "id": kid, "summary": "七日世界新剧本，10月22日上线"}})
    _run(gc, _msg(FRIEND, "小王", "不对，禁忌岛是22号上线", 51))
    assert "22日" in gc.knowledge.entries(GROUP)[0]["summary"]
    assert gc.knowledge.entries(GROUP)[0]["source"] == "fixed"


def test_monthly_self(cfg, brain):
    gc, ob = _gc(cfg, brain)
    brain.db.set(f"qq_reflect_hist_{GROUP}", [{"week": "2026-W40", "text": "- 迷上七日世界"},
                                              {"week": "2026-W41", "text": "- 开始熬夜画图"}])
    _json(brain, {"我是怎样的人": {"lines": ["- 我玩游戏菜但很上头", "- 我是个 AI 模型", "我越来越爱熬夜画图"],
                                  "evidence": ["W41 开始熬夜画图"],
                                  "suggest": [{"facet": "E5", "from": 3, "to": 4, "why": "越来越爱起哄", "evidence": "W40"}]}})
    assert asyncio.run(gc.monthly_self()) == 1
    core = brain.db.get(f"qq_selfcore_{GROUP}")
    assert "熬夜画图" in core and "模型" not in core and core.count("- ") == 2    # 14.0 JSON；行首补“- ”
    assert brain.db.get("persona_proposals")[0]["facet"] == "E5"            # 性格调整只记成建议
    assert "叙事" not in core
    assert asyncio.run(gc.monthly_self()) == 0                        # 一个月一次
    assert "你是怎样的人" in asyncio.run(gc.build_messages(GROUP, False, False, "小王"))[0]["content"]


class FakeWeb:
    enabled = True

    async def search(self, q, n=5, fresh_days=0):
        self.fresh_days = fresh_days
        return [{"title": "七日世界禁忌岛今日上线", "url": "", "snippet": "全新海岛剧本，可以造船出海"}]

    block = staticmethod(__import__("aigroup.net.search", fromlist=["WebSearch"]).WebSearch.block)


def test_share_news(cfg, brain):
    gc, ob = _gc(cfg, brain, reply="诶禁忌岛今天上了，能造船")
    gc.web = FakeWeb()
    gc.photos.per_day = 0
    _run(gc, _msg(FRIEND, "小王", "周四一起玩七日世界", 1))
    _json(brain, {"搜索词": {"query": "七日世界 最新", "topic": "七日世界"},
                  "值不值得": {"share": True, "when": f"{brain.now():%Y-%m-%d}", "source_title": "七日世界禁忌岛今日上线",
                               "gist": "禁忌岛今天上线了，能造船", "topic": "禁忌岛", "summary": "海岛剧本"}})
    ob.sent.clear()
    assert asyncio.run(gc.share(GROUP, gc._st(GROUP))) is True
    assert ob.sent
    assert gc.web.fresh_days == 3                                           # 13.2 只搜最近几天的新闻
    why = brain.llm.group_calls[-1][-1]["content"]
    assert "你刚在手机上刷到" in why and "今天的事" in why and "原标题「七日世界禁忌岛今日上线」" in why
    assert gc.shares(GROUP)[-1]["kind"] == "news"
    assert any(e["topic"] == "禁忌岛" for e in gc.knowledge.entries(GROUP))
    snap = gc.life_snapshot(GROUP)
    assert snap["shares"] and snap["knowledge"]


def test_share_skips_old_or_undated_news(cfg, brain):
    """13.2 看日期：几天前开始的事（“9 月 29 日开打”）、看不出日期的，都不分享。"""
    from datetime import timedelta
    gc, ob = _gc(cfg, brain)
    gc.web = FakeWeb()
    gc.photos.per_day = 0
    _run(gc, _msg(FRIEND, "小王", "周四一起玩七日世界", 1))
    old = f"{brain.now() - timedelta(days=20):%Y-%m-%d}"
    for when in (old, "unknown"):
        _json(brain, {"搜索词": {"query": "七日世界 最新", "topic": "七日世界"},
                      "值不值得": {"share": True, "when": when, "gist": "禁忌岛上线了", "topic": "禁忌岛", "summary": "海岛剧本"}})
        ob.sent.clear()
        assert asyncio.run(gc.share(GROUP, gc._st(GROUP))) is False
        assert not ob.sent and gc.shares(GROUP)[-1]["kind"] == "skip"
    assert not any(e["topic"] == "禁忌岛" for e in gc.knowledge.entries(GROUP))   # 旧闻也不记成“刚知道的事”


def test_share_age_and_dates():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from aigroup.chat.grow import share_age
    from aigroup.net.search import pub_date, text_dates
    t = datetime(2026, 10, 21, 7, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    assert text_dates("擂台赛第1周9月29日开打，3.1 拿下", t) == ["2026-09-29"]
    assert text_dates("2025年12月3日", t) == ["2025-12-03"] and text_dates("比分 3.1、10/19", t) == []
    assert pub_date("Mon, 20 Oct 2026 08:00:00 GMT") == "2026-10-20" and pub_date("") == ""
    kpl = [{"title": "KPL擂台赛开战", "snippet": "擂台赛第1周9月29日开打", "date": ""}]
    assert share_age({"when": "unknown", "source_title": "KPL擂台赛开战"}, kpl, t, 3) is None
    fresh = [{"title": "LOUD夺冠", "snippet": "", "date": "2026-10-20"}]
    assert share_age({"when": "2026-10-20", "source_title": "LOUD夺冠"}, fresh, t, 3) == 1
    stale = [{"title": "LOUD夺冠", "snippet": "", "date": "2026-10-05"}]
    assert share_age({"when": "2026-10-20", "source_title": "LOUD夺冠"}, stale, t, 3) is None   # 发布日期更早：旧闻
    assert share_age({"when": "unknown"}, [{"title": "x", "snippet": "y"}], t, 3) is None


def test_share_photo(cfg, brain):
    gc, ob = _gc(cfg, brain)

    from types import SimpleNamespace

    class FakePhotos:
        ready = True
        gen = SimpleNamespace(ready=True)
        left = 1
        counted = []

        def remaining_today(self):
            return self.left

        def _budget_ok(self):
            return True

        async def make(self, scene, count=True):
            self.counted.append(count)
            return b"\x89PNG fake"
    gc.photos = FakePhotos()
    _json(brain, {"随手拍": {"scene": "书桌上的数位板，黑猫踩在上面", "say": "煤球又来捣乱"}})
    ob.sent.clear()
    assert asyncio.run(gc.share(GROUP, gc._st(GROUP), force_photo=True)) is True
    assert ob.sent[0][1][0]["type"] == "image" and "煤球" in str(ob.sent[1])
    assert gc.shares(GROUP)[-1]["kind"] == "photo"
    # 17.1 小屋里点“发张照片”：今天的名额用完了也照样发（不占名额），不会退成去刷新闻
    gc.photos.left = 0
    ob.sent.clear()
    assert asyncio.run(gc.share(GROUP, gc._st(GROUP), force_photo=True)) is True
    assert ob.sent[0][1][0]["type"] == "image" and gc.photos.counted[-1] is False
    gc.photos.gen = SimpleNamespace(ready=False)
    assert asyncio.run(gc.share(GROUP, gc._st(GROUP), force_photo=True)) is False
    assert "画图接口" in gc.share_error


def test_share_due_limits(cfg, brain):
    gc, ob = _gc(cfg, brain, share_chance=1e9)
    assert not gc.share_due(GROUP, time.time())                        # 群里没人说过话
    gc._record(GROUP, FRIEND, "小王", "hi", 1, ts=time.time() - 3600)
    h = brain.now().hour
    assert gc.share_due(GROUP, time.time()) == (10 <= h < 23)
    gc._note_share(GROUP, "news", "x")
    assert not gc.share_due(GROUP, time.time())                        # 今天分享过了


def test_photo_stops_over_budget(cfg, brain):
    """4.3 花费到上限：不画图；画了按张记账。"""
    from aigroup.llm.budget import Budget
    from aigroup.net.photo import PhotoMaker
    spent = []

    class L:
        budget = Budget({"llm": {"budget": {"daily_cny": 1}}})

        def stats(self):
            return {"usd": 0.2}

        def _today(self):
            return "2026-10-20"

        def add_cost(self, usd, what="image"):
            spent.append(usd)
    from aigroup.core.config import Cfg
    p = PhotoMaker(Cfg({**cfg, "llm": {"profiles": {"x": {"base_url": "u", "model": "m"}}, "routes": {"image": ["x"]}}}), brain.db)
    p.llm = L()

    async def gen(prompt, size="768x1024"):
        return b"img"
    p.gen.generate = gen
    assert asyncio.run(p.make("猫")) is None and not spent          # ¥1.44 ≥ ¥1
    L.budget = Budget({"llm": {"budget": {"daily_cny": 5}}})
    assert asyncio.run(p.make("猫")) == b"img" and spent == [0.01]


def test_birthday_and_festival(cfg, brain, monkeypatch):
    """4.3 群友生日（小屋里填）当天在他常待的群里祝一句；一天一次；格式不对不收。"""
    gc, ob = _gc(cfg, brain, reply="生日快乐呀")
    _run(gc, _msg(FRIEND, "小王", "hi", 1))
    today = brain.now()
    assert not gc.set_birthday(FRIEND, "13-40")
    assert gc.set_birthday(FRIEND, f"{today.month}月{today.day}日")
    assert gc.birthdays()[str(FRIEND)] == f"{today:%m-%d}"
    monkeypatch.setattr(type(gc), "festival", staticmethod(lambda day: ""))
    up = gc.upcoming()
    assert up[0]["kind"] == "birthday" and up[0]["in"] == 0
    monkeypatch.setattr(brain, "now", lambda: today.replace(hour=11))
    st = gc._st(GROUP)
    assert asyncio.run(gc.celebrate_tick(GROUP, st)) is True
    assert "小王的生日" in brain.llm.group_calls[-1][-1]["content"]
    assert asyncio.run(gc.celebrate_tick(GROUP, st)) is False                # 一天一次
    assert gc.set_birthday(FRIEND, "") and not gc.birthdays()
    snap = gc.life_snapshot(GROUP)
    assert any(b["uid"] == FRIEND for b in snap["birthdays"])


def test_festival_filter():
    from datetime import date

    from aigroup.chat.grow import GrowMixin
    assert GrowMixin.festival(date(2026, 12, 25)).startswith("圣诞节")
    assert "霜降" not in GrowMixin.festival(date(2026, 10, 23))


def test_weekly_eval_and_compare(cfg, brain):
    """4.3 每周评测：找毛病打分记进历史；人设改过就和上一版盲比。"""
    gc, ob = _gc(cfg, brain, reply="哈哈真的假的")
    for i in range(3):
        _run(gc, _msg(FRIEND, "小王", f"小满 你在干嘛 {i}", 10 + i, at=True))
    assert brain.db.conn.execute("SELECT COUNT(*) FROM qq_trace WHERE outcome='say'").fetchone()[0] >= 1
    from aigroup.eval.weekly import body_hash, note_persona, previous_body
    old = brain.persona.body
    brain.persona.body = old + "\n- 新加的一句：她最近迷上了拼豆"
    assert note_persona(brain.db, brain.persona) and previous_body(brain.db, brain.persona) == old

    async def js(messages, default=None, role="helper"):
        last = messages[-1]["content"]
        if "只找毛病" in last:
            return {"defects": [{"type": "太长", "severity": 2, "why": "说多了"}]}
        if "两种回复" in last:
            return {"win": 1, "why": "更自然"}
        return default
    brain.llm.json = js
    res = asyncio.run(gc.weekly_eval(force=True))
    assert res["n"] >= 1 and res["avg"] == 90
    snap = gc.eval_snapshot()
    assert snap["hist"][-1]["avg"] == 90 and snap["hist"][-1]["kinds"] == {"太长": res["n"]}
    cmp_ = brain.db.get("persona_compare_last")                 # 人设改过：顺带比了一次
    assert cmp_ and cmp_["n"] >= 1 and cmp_["new"] + cmp_["old"] + cmp_["tie"] == cmp_["n"]
    assert cmp_["new_hash"] == body_hash(brain.persona.body)
    assert asyncio.run(gc.weekly_eval()) is None                  # 这周评过了
