"""38.3 多写几句挑一句：每句过规则（底线不能用、其余扣分）+ 一次便宜模型挑；没被叫到最好的也不及格就不说；
被叫到照发最好的；全撞底线才重新写一批。复核时给她的话打分。"""
import asyncio
import json

from conftest import FRIEND, GROUP, J, _gc


def _say(gc, brain, say, alt=None, mentioned=False, **kw):
    brain.llm.reply = J(act="say", kind="I1" if mentioned else "I2", to=1, len="short", say=say, alt=alt or [], move="接梗")
    return asyncio.run(gc.speak(GROUP, mentioned=mentioned, owner_spoke=False, trigger=1 if mentioned else None,
                                who="小王", **kw))


def _sent(ob):
    return "".join(s["data"].get("text", "") for _, segs in ob.sent for s in segs)


def test_parse_judge_and_penalties():
    from aigroup.chat.choose import judge_penalty, length_penalty, parse_judge
    j = parse_judge('好的 {"cands": [{"k": 1, "kind": "玩笑", "bad": 5, "why": "x", "score": 2}, '
                    '{"k": 2, "kind": "事实", "bad": "第5条", "why": "编了比分", "score": 4}, {"k": 9}], "best": 2}', 2)
    assert j["best"] == 2 and j["cands"][1]["bad"] == 0          # 玩笑不查第 5 条
    assert j["cands"][2]["bad"] == 5 and 9 not in j["cands"]
    p1, _ = judge_penalty(j["cands"][1])
    p2, h2 = judge_penalty(j["cands"][2])
    assert p1 == 2.4 and p2 == 4.8 and "挑选" in h2[0]
    assert parse_judge("不是 JSON", 2) == {}
    assert length_penalty(10, 10, False) == 0 and length_penalty(40, 10, False) == 3.0
    assert length_penalty(25, 10, True) == 0                     # 被叫到可以长一点


def test_judge_prompt_has_principles():
    from aigroup.chat.choose import PRINCIPLES, judge_prompt
    p = judge_prompt("小王：今天好冷", ["多穿点", "冷就对了"], group_len=8, mentioned=False)
    assert all(x.split("：")[0] in p for x in PRINCIPLES) and "best 就写 0" in p and "8 个字" in p
    assert "一定要回" in judge_prompt("x", ["a"], mentioned=True)


def test_judge_picks_best_of_three(cfg, brain):
    gc, ob = _gc(cfg, brain, cooldown_seconds=0)
    gc._record(GROUP, FRIEND, "小王", "今天好冷", 1)
    orig = brain.llm.chat

    async def chat(messages, **kw):
        if "帮她挑一句最好的" in messages[-1]["content"]:
            return json.dumps({"cands": [{"k": 1, "score": 2, "why": "评人"}, {"k": 2, "score": 5}, {"k": 3, "score": 3}],
                               "best": 2})
        return await orig(messages, **kw)
    brain.llm.chat = chat
    assert _say(gc, brain, ["你今天真会说话"], alt=[["冷就多穿点，别硬扛"], ["冷死"]])
    assert "多穿点" in _sent(ob)
    q = json.loads(brain.db.conn.execute("SELECT quality FROM qq_trace ORDER BY id DESC LIMIT 1").fetchone()[0])
    assert q["chosen"] == 1 and len(q["cands"]) == 3


def test_unprompted_skips_when_judge_says_silence(cfg, brain):
    gc, ob = _gc(cfg, brain, cooldown_seconds=0)
    gc._record(GROUP, FRIEND, "小王", "今天好冷", 1)
    orig = brain.llm.chat

    async def chat(messages, **kw):
        if "帮她挑一句最好的" in messages[-1]["content"]:
            return json.dumps({"cands": [{"k": 1, "score": 4}, {"k": 2, "score": 4}], "best": 0})
        return await orig(messages, **kw)
    brain.llm.chat = chat
    assert not _say(gc, brain, ["是挺冷"], alt=[["冷"]]) and not ob.sent


def test_mentioned_sends_best_even_if_flawed_no_rewrite(cfg, brain):
    gc, ob = _gc(cfg, brain, cooldown_seconds=0, check_critic="off")
    gc._record(GROUP, FRIEND, "小王", "@你 冷不冷", 1)
    for i, t in enumerate(["我小本本记岔了", "这条记进小本本"]):
        gc._record(GROUP, 10001, "小满", t, 10 + i, role="assistant")
    calls = []

    async def rw(messages, raw, hits, gid):
        calls.append(hits)
        return [["重写的"]]
    gc._rewrite = rw
    assert _say(gc, brain, ["小本本上再记一笔：冷"], alt=[["小本本又多一页"]], mentioned=True)
    assert "小本本" in _sent(ob) and not calls                  # 被叫到：不重写，照发最好的


def test_hard_line_all_bad_regenerates_once_when_mentioned(cfg, brain):
    gc, ob = _gc(cfg, brain, cooldown_seconds=0, check_critic="off")
    gc._record(GROUP, FRIEND, "小王", "@你 你是谁", 1)
    calls = []

    async def rw(messages, raw, hits, gid):
        calls.append(hits)
        return [["新来的，多指教"]]
    gc._rewrite = rw
    assert _say(gc, brain, ["昨晚被拉进来测试的那个"], alt=[["来验货的吗"]], mentioned=True)
    assert "多指教" in _sent(ob) and len(calls) == 1 and "幕后" in calls[0][0]


def test_hard_line_unprompted_no_regen(cfg, brain):
    gc, ob = _gc(cfg, brain, cooldown_seconds=0, check_critic="off")
    gc._record(GROUP, FRIEND, "小王", "今天好冷", 1)
    calls = []

    async def rw(messages, raw, hits, gid):
        calls.append(hits)
        return [["x"]]
    gc._rewrite = rw
    assert not _say(gc, brain, ["我刚部署好了"]) and not ob.sent and not calls


def test_rule_findings_matches_checks22(cfg, brain):
    gc, _ = _gc(cfg, brain)
    gc._record(GROUP, FRIEND, "小王", "今天好冷", 1)
    for i, t in enumerate(["我小本本记岔了", "这条记进小本本"]):
        gc._record(GROUP, 10001, "小满", t, 10 + i, role="assistant")
    f = gc.rule_findings(GROUP, ["小本本又多一页"])
    assert any(n == "catchphrase" for n, _m, _w in f)


def test_review_scores_her_lines(cfg, brain):
    from aigroup.memory import review as R
    gc, ob = _gc(cfg, brain)
    gc._record(GROUP, FRIEND, "小王", "讲讲湍流", 1)
    m = gc._record(GROUP, 10001, "小满", "小王这套讲得还挺顺", 2, role="assistant")
    d = R.export(gc.db, GROUP, gc.cfg.tz)
    assert d["her_turns"][0]["msg"] == m and "她说的话" in R.render_text(d)
    ap = R.Applier(gc.db, gc.memory, GROUP, gc.cfg.tz)
    res = ap.run({"gid": GROUP, "scores": [{"msg": m, "score": 2, "tags": ["评人不接话"], "better": "湍流要是也能枚举我就服"},
                                           {"msg": m, "score": 9}, {"msg": 1, "score": 3}, {"msg": m, "score": 3, "tags": ["乱写"]}]})
    assert res["ok"] == 1 and len(res["rejected"]) == 3
    r = brain.db.conn.execute("SELECT * FROM qq_human_score").fetchone()
    assert r["score"] == 2 and r["src"] == "claude-review" and r["tags"] == "评人不接话" and r["msg_id"] == m
    R.undo(gc.db, res["run"])
    assert brain.db.conn.execute("SELECT COUNT(*) FROM qq_human_score").fetchone()[0] == 0


def test_rulebook_covers_every_rule():
    """38.3 规则只认一张总表：每条发前检查、每条说法检查都登记了家族和层；发前检查只并不加。"""
    from aigroup.chat.checks import CHECKS
    from aigroup.chat.quality import RULES
    from aigroup.chat.rulebook import CHECKS_BOOK, FAMILIES, LAYERS, MAX_CHECKS, QUALITY_BOOK, QUALITY_EXTRA, hard_checks, table
    assert set(CHECKS) - {"critic"} == set(CHECKS_BOOK)
    assert {n for n, _, _ in RULES} <= set(QUALITY_BOOK)
    assert len(CHECKS_BOOK) <= MAX_CHECKS
    for f, layer, *_ in [*CHECKS_BOOK.values(), *QUALITY_BOOK.values(), *QUALITY_EXTRA.values()]:
        assert f in FAMILIES and layer in LAYERS
    assert set(hard_checks()) == {"price_src", "misattrib"}
    assert len(table()) == len(CHECKS_BOOK) + len(QUALITY_BOOK) + len(QUALITY_EXTRA)


def test_merged_bagu_and_assistant_tone():
    from aigroup.chat.quality import check
    s, h = check("看完了。最扎的是结尾")
    assert h.count("八股") == 1 and s >= 4                     # 一条规则，堆几种扣几次
    assert "助手腔" in check("要不要我把要填的那几项给你列一下")[1]
    assert "助手腔" in check("先纠个前提，人类从没证出来过")[1]
    assert check("冷就多穿点")[0] == 0


def test_v2_prompt_rules_not_duplicated(cfg, brain):
    gc, _ = _gc(cfg, brain)
    if not (hasattr(gc, "v2") and gc.v2(GROUP)):
        return
    sysp = asyncio.run(gc.build_messages(GROUP, False, False, "", None))[0]["content"]
    assert sysp.count("一定要") <= 1 + sysp.count("一定要回一句") and "回话照这四条" in sysp


def test_hard_quality_matches_rulebook():
    from aigroup.chat.choose import HARD_QUALITY
    from aigroup.chat.rulebook import QUALITY_EXTRA
    assert set(HARD_QUALITY) == {k for k, (_f, layer) in QUALITY_EXTRA.items() if layer == "底线"} - {"和刚说过的一样"}
