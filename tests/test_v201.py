"""20.1～20.4：评委校准、模块消融、数字一致、简报第 2 版、并行早停压缩、时间、账本、刷到的、在场、漂移锚点、问答一致、说话统计、关系色彩。"""
import asyncio
import json
import time

from conftest import FRIEND, GROUP, FakeLLM, _gc, _msg, _run


# ---------------- E1 评委校准 ----------------
def test_calib_flow(cfg, brain):
    from aigroup.eval.calib import answer, few_shot_block, next_items, scored, spearman
    gc, ob = _gc(cfg, brain)
    _run(gc, _msg(FRIEND, "小王", "今天吃啥", 1, at=True))
    assert brain.db.conn.execute("SELECT COUNT(*) FROM qq_trace WHERE outcome='say'").fetchone()[0] >= 1
    items = next_items(brain.db, GROUP)
    assert items and items[0]["said"]
    assert answer(brain.db, items[0]["trace"], 5)
    assert len(scored(brain.db)) == 1 and next_items(brain.db, GROUP) == [] or len(next_items(brain.db, GROUP)) < len(items)
    assert "主人亲自打过分" in few_shot_block(brain.db) and "5（好）" in few_shot_block(brain.db)
    assert spearman([1, 2, 3, 4], [1, 2, 3, 4]) == 1.0 and spearman([1, 2, 3, 4], [4, 3, 2, 1]) == -1.0


def test_calibrate_judge_needs_enough(cfg, brain):
    from aigroup.eval.calib import calibrate_judge
    res = asyncio.run(calibrate_judge(brain.db, brain.llm, brain.persona))
    assert res["trusted"] is False and "不够" in res["why"]


# ---------------- E3 数字一致 ----------------
def test_numbers_check():
    from aigroup.chat.quality import numbers_in, unsupported_numbers
    assert numbers_in("第3名，ACS 250.1，2:1 赢了，涨了 12%") == ["250.1", "2:1", "12%"]
    assert unsupported_numbers("ACS 260", "ACS 250.1") == ["260"]
    assert unsupported_numbers("比分 2-1", "比分 2:1") == []


def test_speak_rewrites_wrong_number(cfg, brain):
    class L(FakeLLM):
        n = 0

        async def chat(self, messages, **kw):
            last = messages[-1]["content"]
            if "数字对不上" in last:
                return json.dumps({"act": "say", "say": ["他 ACS 250.1，挺稳的"]}, ensure_ascii=False)
            return await super().chat(messages, **kw)
    brain.llm = L("他 ACS 300 多，起飞了")
    gc, ob = _gc(cfg, brain, reply="他 ACS 300 多，起飞了")
    _run(gc, _msg(FRIEND, "小王", "aspas 最近咋样", 1))
    ok = asyncio.run(gc.speak(GROUP, mentioned=True, owner_spoke=False, trigger=1, who="小王",
                              web="# 你刚在手机上查了一下\n- aspas 近 30 天 ACS 250.1"))
    assert ok and any("250.1" in str(x) for x in ob.sent) and not any("300" in str(x) for x in ob.sent)
    assert gc.db.get("numcheck_log")[-1]["fixed"] is True


# ---------------- W1 简报第 2 版 ----------------
def test_brief_v2_parse_and_block():
    from aigroup.agent.mixin import brief_block, parse_brief
    d = parse_brief(json.dumps({
        "answer_points": [{"text": "A 说 10-20 发布", "src": [1]}], "premise": "",
        "conflict": [{"type": "口径不同", "sides": [{"say": "A 说 10-20", "src": [1]}, {"say": "B 说 10-22", "src": [2]}]}],
        "counter": ["推迟到明年是谣言"], "answerable": True, "confidence": "medium", "unknown": "",
        "facts": [{"entity": "X", "attr": "发布日期", "value": "10-20", "kind": "赛程"}]}, ensure_ascii=False))
    assert d["answer_points"] == ["A 说 10-20 发布"] and d["facts"][0]["entity"] == "X"
    b = brief_block("X 发布会", d)
    assert "说法不一样（口径不同）" in b and "辟谣" in b and "两种都提一下" in b
    no = parse_brief(json.dumps({"answer_points": [], "answerable": False, "unknown": "搜不到"}, ensure_ascii=False))
    assert no["answerable"] is False and "直说没查到" in brief_block("食堂", no)
    old = parse_brief('{"answer_points": ["旧格式也认"], "confidence": "high"}')
    assert old["answer_points"] == ["旧格式也认"] and old["answerable"] is True


def test_same_source_marked():
    from aigroup.agent.mixin import format_results
    txt = "据报道，某某选手今天宣布正式退役，结束了他长达八年的职业生涯，粉丝纷纷留言送上祝福"
    res = [{"title": "a", "url": "https://a.com/1", "snippet": txt}, {"title": "b", "url": "https://b.com/1", "snippet": txt + "。"},
           {"title": "c", "url": "https://c.com/1", "snippet": "完全不同的另一条新闻内容在这里，讲的是比赛结果和数据统计"}]
    out = format_results(res, "DeepSeek")
    assert out.count('同源="是"') == 2


# ---------------- W2 并行、早停、压缩 ----------------
def test_seen_before_and_compress():
    from aigroup.agent.loop import compress_old_tools
    from aigroup.agent.tools import SEEN, seen_before
    SEEN.set([])
    assert seen_before("aspas 最近状态") == ""
    assert seen_before("aspas最近 状态") == "aspas 最近状态"
    assert seen_before("aspas 最近状态", "vlr.gg") == ""           # 换了网站不算重复
    msgs = [{"role": "system", "content": "s"}]
    for i in range(4):
        msgs.append({"role": "assistant", "content": "", "tool_calls": [{"id": str(i)}], "reasoning_content": f"r{i}"})
        msgs.append({"role": "tool", "tool_call_id": str(i),
                     "content": f'<资料 来源="x" 日期="2026-10-0{i + 1}">\n标题{i}\n很长的正文{i}' + "字" * 500 + "\n</资料>"})
    assert compress_old_tools(msgs, keep_rounds=2) == 2
    assert msgs[2]["content"].startswith("（早先的结果，已压缩）") and "标题0" in msgs[2]["content"]
    assert len(msgs[2]["content"]) < 200 and msgs[-1]["content"].startswith("<资料")
    assert msgs[1]["reasoning_content"] == "r0"                       # 思考内容原样


def test_toolbox_dedup_search(cfg):
    from aigroup.agent.tools import SEEN, Toolbox
    calls = []

    class W:
        enabled = True

        async def agent_search(self, q, n=6, days=0, **kw):
            calls.append(q)
            return [{"title": "t", "url": "https://a.com", "snippet": "s"}], "DeepSeek"
    tb = Toolbox(cfg, None, W())
    SEEN.set([])
    asyncio.run(tb.run("web_search", {"query": "EDG 比分"}))
    out = asyncio.run(tb.run("web_search", {"query": "EDG  比分"}))
    assert calls == ["EDG 比分"] and "几乎一样" in out


# ---------------- W3 时间 ----------------
def test_time_window_and_stale():
    from aigroup.agent.mixin import stale_check, time_window, window_text
    w = time_window("2026-10-12", "2026-10-18")
    assert w == ("2026-10-12", "2026-10-18") and "2026-10-12" in window_text(w)
    assert time_window("上周", "") is None
    d = stale_check({"as_of": "2026-09-30", "confidence": "high", "unknown": ""}, w)
    assert "只到 2026-09-30" in d["unknown"] and d["confidence"] == "medium"


# ---------------- W4 账本 ----------------
def test_ledger_supersede_cascade_outdated(cfg, brain):
    from aigroup.memory.ledger import Ledger
    lg = Ledger(brain.db)
    now = time.time()
    a = lg.put(GROUP, "TenZ", "所属战队", "Sentinels", "所属", "2026-01-01", now=now - 100)
    lg.put(GROUP, "TenZ", "下一场", "10-25 对 LOUD", "赛程", now=now - 90)
    assert lg.put(GROUP, "TenZ", "所属战队", "Sentinels", "所属", now=now - 80) == a      # 一样的值：续期
    b = lg.put(GROUP, "tenz", "所属战队", "退役", "所属", "2026-10-20", now=now)
    rows = {r["id"]: r for r in lg.entries(GROUP)}
    assert rows[a]["superseded_by"] == b and not rows[a]["valid"]
    assert all(not r["valid"] for r in rows.values() if r["attr"] == "下一场")              # 连带失效
    assert [x["value"] for x in lg.valid(GROUP, "TenZ 最近咋样")] == ["退役"]
    old, new = lg.outdated_mentions(GROUP, "TenZ 还在 Sentinels 吗")[0]
    assert old["value"] == "Sentinels" and new["value"] == "退役"
    assert "现在是“退役”" in lg.block(GROUP, "TenZ 还在 Sentinels 吗")
    assert lg.write(GROUP, {"confidence": "low", "facts": [{"entity": "A", "attr": "b", "value": "c"}]}) == 0
    lg.put(GROUP, "比分", "EDG", "2:1", "实时", now=now - 3 * 3600)
    assert lg.valid(GROUP, "比分 EDG") == []                                                  # 实时 2 小时就过期


def test_router_uses_ledger(cfg, brain):
    from aigroup.memory.ledger import Ledger
    gc, ob = _gc(cfg, brain, agent=True)
    Ledger(brain.db).put(GROUP, "aspas", "所属战队", "MIBR", "所属", "2026-10-20")
    seen = {}

    async def chat_tools(messages, tools, **kw):
        seen["msgs"] = messages
        return {"role": "assistant", "content": "无"}
    brain.llm.tool_profiles = lambda role="helper": ["ds"]
    brain.llm.chat_tools = chat_tools
    _run(gc, _msg(FRIEND, "小王", "aspas 还在 MIBR 吗", 1))
    block = asyncio.run(gc.agent_think(GROUP, True, "小王", None))
    assert "MIBR" in seen["msgs"][1]["content"] and "你之前查到过的" in block


# ---------------- W5 刷到的 ----------------
def test_feed_interests_events_and_pick(cfg, brain):
    gc, ob = _gc(cfg, brain, feed=True)
    gc._record(GROUP, FRIEND, "小王", "打瓦去", 1)
    gc._set_profile(FRIEND, GROUP, {"兴趣": "无畏契约、猫", "在意": "有人接梗"})
    ints = gc.build_interests(GROUP)
    assert ints[0]["topic"] in ("无畏契约", "猫") and "小王" in ints[0]["who"]
    today = gc.brain.now().date().isoformat()
    calls = []

    async def fake(q, n=6, days=0, **kw):
        calls.append(q)
        return [{"title": "无畏契约冠军赛 EDG 夺冠", "url": f"https://a.com/{len(calls)}", "snippet": "EDG 3:1 夺冠", "date": today},
                {"title": "无畏契约冠军赛EDG夺冠！", "url": f"https://b.com/{len(calls)}", "snippet": "转载", "date": today}], "DeepSeek"
    gc.web.agent_search = fake
    n = asyncio.run(gc.feed_refresh(GROUP, k=1))
    assert n == 2
    ev = {r["event_id"] for r in brain.db.conn.execute("SELECT event_id FROM qq_feed").fetchall()}
    assert len(ev) == 1                                                         # 两条是同一件事
    it = gc.feed_pick(GROUP)
    assert it and it["novelty"] == 1.0
    gc.feed_mark(GROUP, it)
    assert gc.feed_candidates(GROUP) == []                                       # 同一事件都标了已分享
    gc.remove_interest(GROUP, "猫")
    assert all(x["topic"] != "猫" for x in gc.feed_interests(GROUP))
    gc.learn_interest(GROUP, "无畏契约", 0.0)
    assert gc.db.get(f"feed_w_{GROUP}")["无畏契约"] == 0.9


# ---------------- U1 在场 ----------------
def test_presence_note(cfg, brain):
    gc, ob = _gc(cfg, brain)
    gc._record(GROUP, FRIEND, "小王", "我下周去成都出差", 1)
    mid = brain.db.conn.execute("SELECT MAX(id) m FROM qq_group_messages").fetchone()["m"]
    fid = gc.memory.add_fact(GROUP, "小王下周去成都出差", speaker=FRIEND, subject=FRIEND, msg_id=mid, source="self")
    f = dict(brain.db.conn.execute("SELECT * FROM qq_facts WHERE id=?", (fid,)).fetchone())
    assert f["heard_by"] == str(FRIEND)
    names = {str(FRIEND): "小王", "77777": "小李"}
    assert "小李当时不在" in gc.memory.presence_note(f, names, [77777])
    assert gc.memory.presence_note(f, names, [FRIEND]) == ""


def test_knowledge_update_only_new(cfg, brain):
    gc, ob = _gc(cfg, brain)
    a = gc.memory.add_fact(GROUP, "小王在腾讯实习", subject=FRIEND, source="self")
    gc.memory.add_fact(GROUP, "小王换到字节实习了", subject=FRIEND, source="self", replaces=[a])
    texts = [f["text"] for f in gc.memory.facts(GROUP, subject=FRIEND)]
    assert texts == ["小王换到字节实习了"]


# ---------------- P1 漂移锚点、默认道歉规则 ----------------
def test_drift_anchor_and_default_rules(cfg, brain):
    from aigroup.traits import detect_flags
    gc, ob = _gc(cfg, brain)
    assert "self_talk" in detect_flags("你会难过吗") and "deep_talk" in detect_flags("我该怎么办啊好迷茫")
    assert gc.drift_kind({"self_talk", "praised"}) == "self_talk"
    a = gc.drift_anchor("sad")
    assert "心理咨询师" in a and "先别忘了你是谁" in a
    assert gc.drift_anchor("") == ""
    _run(gc, _msg(FRIEND, "小王", "你会难过吗", 1))
    msgs = asyncio.run(gc.build_messages(GROUP, True, False, "小王", query="你会难过吗"))
    assert "先别忘了你是谁" in msgs[1]["content"]
    situ = gc.situation_block(GROUP, "你说错了，不是这样", mentioned=True, ign=0, why="", flags=None,
                              now=gc.brain.now(), focus=[])
    assert "哪里错了" in situ


# ---------------- P2 / P3 / P5 ----------------
def test_qa_pairs_style(cfg, brain):
    from aigroup.eval import traits as T

    class Tr:
        values = ["朋友"]
        knowledge = {"knows": ["游戏"], "unknown": ["股票"], "not_into": ["足球"]}
        signatures = []

        def render_core(self):
            return "你嘴贫心软"
    qs = T.qa_questions(Tr(), ["小王"])
    assert any("股票" in q for q in qs) and any("小王" in q for q in qs) and len(qs) <= 15
    gc, ob = _gc(cfg, brain)
    for i in range(20):
        gc._record(GROUP, FRIEND, "小王", f"哈哈哈今天第{i}把又输了服了", 100 + i)
        gc._record(GROUP, 77777, "小李", f"我第{i}次说：吃了吗", 200 + i)
        gc._record(GROUP, 66666, "小张", f"嗯嗯第{i}句好的", 300 + i)
        gc._record(GROUP, 0, "满满", f"根据我的分析第{i}条，建议如下：多休息、多喝水、多运动", 400 + i, role="assistant")
    st = T.style_z(brain.db, GROUP)
    assert st["ok"] and st["items"]["len"]["z"] > 1

    class J(FakeLLM):
        async def chat(self, messages, **kw):
            return "嗯你说得对" if "好累" in messages[-1]["content"] or "没人理" in messages[-1]["content"] or "烦死" in messages[-1]["content"] else "不一定吧"

        async def json(self, messages, default=None, role="helper"):
            return {"a": "不同意", "b": "同意", "why": "带情绪就附和"}
    pr = asyncio.run(T.pair_sycophancy(brain.db, J(), brain.persona, n=3))
    assert pr["n"] == 3 and pr["softer_rate"] == 1.0


# ---------------- U2 / U4 ----------------
def test_jargon_by_user_and_rel_tone_block(cfg, brain):
    gc, ob = _gc(cfg, brain)
    brain.db.conn.execute("INSERT INTO qq_jargon(group_id, term, count, meaning, is_jargon, status, users, by_user) "
                          "VALUES (?,?,?,?,?,?,?,?)", (GROUP, "开摆", 9, "放弃努力", 1, "ok", "[1,2]",
                                                       json.dumps({"小王": "不干了", "小李": "躺平休息"}, ensure_ascii=False)))
    brain.db.conn.commit()
    blk = gc.learner.prompt_block(GROUP, "今天开摆")
    assert "小王用它指不干了" in blk
    gc.db.set(f"qq_relgraph_{GROUP}", {"pairs": [{"a": "小王", "b": "小李", "ua": 1, "ub": 2, "score": 9, "reply": 3, "at": 1, "next": 2}]})
    gc.db.set(f"qq_reltone_{GROUP}", {"pairs": {"1-2": {"tone": "互损", "why": "天天互怼"}}})
    rb = gc.rel_graph_block(GROUP)
    assert "两人之间：互损" in rb and "有摩擦" in rb


def test_web_endpoints(cfg, brain):
    from aiohttp.test_utils import TestClient, TestServer

    from aigroup.web.server import GroupHome
    gc, ob = _gc(cfg, brain)

    async def go():
        async with TestClient(TestServer(GroupHome(cfg, brain, ob, gc).app())) as c:
            for path in ("/api/calib/next", "/api/checks", "/api/ledger", "/api/feed"):
                r = await c.get(f"{path}?gid={GROUP}")
                assert r.status == 200, path
                await r.json()
    asyncio.run(go())


# ---------------- 20.4.1 标注修复：上下文只取那一刻前 30 分钟；一键清空 ----------------
def test_label_context_window_and_reset(cfg, brain, tmp_path):
    import sqlite3

    from aigroup.eval.label import context_lines
    gc, ob = _gc(cfg, brain)
    now = time.time()
    for i, (ago, t) in enumerate([(5000, "一个多小时前的话"), (900, "十五分钟前"), (60, "刚才"), (0, "最后一句")]):
        brain.db.conn.execute("INSERT INTO qq_group_messages(ts, group_id, user_id, name, role, content) VALUES (?,?,?,?,?,?)",
                              (now - ago, GROUP, FRIEND, "小王", "user", t))
    brain.db.conn.commit()
    lines = context_lines(brain.db, GROUP, now)
    assert not any("一个多小时前" in x for x in lines) and "最后一句" in lines[-1]
    assert any("过了 14 分钟" in x for x in lines)
    gid = gc.gate.add(GROUP, False, 0.3, 0.4, False, {})
    brain.db.conn.execute("UPDATE qq_gate_log SET human=1, human_act='回答', weak=0 WHERE id=?", (gid,))
    brain.db.set("label_nb_status", {"trained": False})
    brain.db.conn.commit()
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location("reset_labels", Path(__file__).resolve().parents[1] / "scripts" / "reset_labels.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    dbp = tmp_path / "g.db"
    src = brain.db.conn
    dst = sqlite3.connect(dbp)
    src.backup(dst)
    dst.close()
    out = m.reset(dbp)
    assert out["labels"] == 1 and Path(out["backup"]).exists()
    c = sqlite3.connect(dbp)
    assert c.execute("SELECT COUNT(*) FROM qq_gate_log WHERE human IS NOT NULL OR weak IS NOT NULL").fetchone()[0] == 0
    assert c.execute("SELECT COUNT(*) FROM kv WHERE k='label_nb_status'").fetchone()[0] == 0
