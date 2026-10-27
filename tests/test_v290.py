"""29.0：天气放后台查；备选说法的检查同时跑；群友说过“别提 X 了”这几天都别提；每周热梗；小屋每天抽查；九问自评 / 五项打分删掉。"""
import asyncio
import json
import time

from conftest import FRIEND, GROUP, J, _gc


def test_closed_topics_block_any_mention(cfg, brain):
    gc, _ = _gc(cfg, brain)
    gc._record(GROUP, 10001, "小满", "@小王 面试稳住，别把面试官当组员骂", 1, role="assistant")
    gc._record(GROUP, FRIEND, "小王", "别提面试了好吗压力很大的", 2)
    c = gc.closed_topics(GROUP)
    assert c and "面试" in c[0]["terms"]
    assert "别再提" in gc._ask_repeat(GROUP, "那就祝你面试官温柔点", FRIEND, time.time())
    assert gc._ask_repeat(GROUP, "那我也报个班", FRIEND, time.time()) == ""


def test_closed_topic_from_her_last_line(cfg, brain):
    gc, _ = _gc(cfg, brain)
    gc._record(GROUP, 10001, "小满", "体检报告出来没", 1, role="assistant")
    gc._record(GROUP, FRIEND, "小王", "别问了", 2)
    assert "别再提" in gc._ask_repeat(GROUP, "体检报告咋样了", FRIEND, time.time())


def test_parallel_alt_checks_are_quiet(cfg, brain):
    gc, ob = _gc(cfg, brain, cooldown_seconds=0, check_critic="off")
    gc._record(GROUP, FRIEND, "小王", "今天好冷", 1)
    for i, t in enumerate(["我小本本记岔了", "这条记进小本本"]):
        gc._record(GROUP, 10001, "小满", t, 10 + i, role="assistant")
    brain.llm.reply = J(act="say", kind="I2", to=1, len="short", say=["冷就多穿点"], alt=[["小本本上再记一笔"]])
    assert asyncio.run(gc.speak(GROUP, mentioned=False, owner_spoke=False, trigger=None, who=""))
    names = [r["name"] for r in gc.db.conn.execute("SELECT name FROM qq_check_log").fetchall()]
    assert "catchphrase" not in names                         # 没选上的备选说法有毛病，不记命中


def test_meme_weekly(cfg, brain):
    gc, _ = _gc(cfg, brain)

    class Web:
        enabled = True
        qs = []

        async def agent_search(self, q, **kw):
            Web.qs.append(q)
            return [{"title": "本周热梗盘点", "snippet": "“班味”指上班久了身上的疲惫感；“city不city”问洋不洋气"}], "DeepSeek"
    gc.web = Web()

    async def js(messages, default=None, role="helper"):
        assert "只照这些整理" in messages[-1]["content"]
        return {"memes": [{"term": "班味", "means": "上班久了身上的疲惫感", "usage": "吐槽打工"}, {"term": "x", "means": ""}]}
    gc.brain.llm.json = js
    assert asyncio.run(gc.meme_weekly(GROUP)) == 1
    r = gc.db.conn.execute("SELECT * FROM qq_jargon WHERE group_id=? AND term='班味'", (GROUP,)).fetchone()
    assert r["src"] == "web-weekly" and r["is_jargon"] == 2
    assert "班味" in gc.learner.jargon_block(GROUP, "我身上班味好重")
    assert "班味" not in gc.learner.jargon_block(GROUP, "今天吃啥")           # 先懂后用：没人说就不带
    n = len(Web.qs)
    assert asyncio.run(gc.meme_weekly(GROUP)) == 0 and len(Web.qs) == n      # 一周一次


def test_spot_items_and_answer(cfg, brain):
    gc, _ = _gc(cfg, brain)
    gc._record(GROUP, FRIEND, "小王", "压力一个 ai？", 1)
    gc.tracer.add(trace="t1", gid=GROUP, gate_id=None, mentioned=False, why="", messages=[], raw="",
                  plan={"say": ["白得这么彻底"]}, sent=["这图我这边全白"], quality="", outcome="say", timing={})
    gc._save_meme(GROUP, "压力", "给人施压、刁难", "web")
    d = gc.spot_items(GROUP)
    assert d["says"] and d["says"][0]["planned"] == "白得这么彻底" and d["memes"][0]["term"] == "压力"
    assert gc.spot_answer(GROUP, "say", d["says"][0]["ref"], 2)
    mid = d["memes"][0]["ref"]
    assert gc.spot_answer(GROUP, "meme", mid, 0)
    r = gc.db.conn.execute("SELECT meaning, src FROM qq_jargon WHERE id=?", (mid,)).fetchone()
    assert r["meaning"] == "" and r["src"] == "web-none"                     # 点了不对：先不带给她
    d2 = gc.spot_items(GROUP)
    assert not d2["says"] and not d2["memes"] and d2["planned_better"] == 1
    assert not gc.spot_answer(GROUP, "say", 1, 5)


def test_weather_prefetch_in_background(cfg, brain):
    gc, _ = _gc(cfg, brain)
    calls = []

    class W:
        def all(self):
            return [{"user_id": FRIEND}]

        async def prefetch(self, uids):
            await asyncio.sleep(0.3)
            calls.append(uids)
    gc.where = W()
    gc._record(GROUP, FRIEND, "小王", "hi", 1)

    async def go():
        t = time.time()
        await gc.build_messages(GROUP, True, False, "小王", None)
        took = time.time() - t
        await asyncio.sleep(0.4)
        return took
    assert asyncio.run(go()) < 0.25 and calls == [[FRIEND]]


def test_appraise_removed(cfg, brain):
    from aigroup.chat import prompt as P
    from aigroup.chat.settings import DEAD_KNOBS, DEFAULTS
    assert not hasattr(P, "appraise_verdict") and "appraise" not in DEFAULTS and "appraise" in DEAD_KNOBS
    gc, _ = _gc(cfg, brain)
    gc._record(GROUP, FRIEND, "小王", "hi", 1)
    sysp = asyncio.run(gc.build_messages(GROUP, False, False, "", None))[0]["content"]
    assert '"appraise"' not in sysp and '"score"' not in sysp
    assert json.loads(J(a=1))


def test_drop_bad_bubble():
    from aigroup.chat.speak import drop_bad_bubble
    b = ["我连手都没有，疼什么", "倒是AL那几位，手可以歇歇了"]
    assert drop_bad_bubble(b, ["自检：同一个梗——又在自嘲（有问题的是「我连手都没有」）"]) == ["倒是AL那几位，手可以歇歇了"]
    assert drop_bad_bubble(b, ["自检：说了没依据的数字"]) == []                  # 没引用原话：不动
    assert drop_bad_bubble(b, ["「我连手都没有」", "「AL那几位」"]) == []         # 全有毛病：不动
