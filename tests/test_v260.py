"""26.0：自检分清玩笑 / 看法 / 事实、只指毛病不写替换句、重写只改有毛病的字、没说错不认错、
回过 / 翻旧事没被叫到就不说；先读懂最新几句；不懂的梗去查、记进黑话表；选购题带问的人画像。"""
import asyncio
import json

from conftest import FRIEND, FRIEND2, GROUP, _gc


def test_critic_verdict_kinds():
    from aigroup.chat.checks import FAILURE_MODES, critic_verdict
    assert len(FAILURE_MODES) >= 16 and "没说错却认错" in FAILURE_MODES[15]
    # 玩笑：拿去对图、编经历不算
    assert critic_verdict({"bad": True, "which": 3, "kind": "玩笑", "why": "替他断定"}) == ""
    assert critic_verdict({"bad": True, "which": 8, "kind": "玩笑", "why": "编线下"}) == ""
    # 玩笑也会重复、替人答
    assert "同一件事说第二遍" in critic_verdict({"bad": True, "which": 6, "kind": "玩笑", "why": "说过了"})
    # 看法不当成编
    assert critic_verdict({"bad": True, "which": 8, "kind": "看法", "why": "x"}) == ""
    w = critic_verdict({"bad": True, "which": 5, "kind": "事实", "why": "比分没依据", "span": "1-1", "fix": "改成不写比分"})
    assert "（有问题的是「1-1」）" in w and "改成" not in w                 # 不再带替换句
    assert critic_verdict({"bad": False}) == ""
    old = critic_verdict({"bad": True, "which": 5, "why": "x", "fix": "改成 y"}, v2=False)
    assert "改成 y" in old


def test_critic_prompt_has_kinds_and_examples(cfg, brain):
    gc, _ = _gc(cfg, brain)
    seen = {}

    async def chat(messages, **kw):
        seen["p"] = messages[-1]["content"]
        return json.dumps({"kind": "玩笑", "bad": True, "which": 3, "why": "替他断定图", "span": "截到灵魂"}, ensure_ascii=False)
    gc.brain.llm.chat = chat
    gc._record(GROUP, FRIEND, "小王", "[图片：一张纯白的截图，看不出内容。" + "很长的说明" * 30 + "图里写着连续两晚凌晨1点半以后还在电脑前]", 1)
    gc._record(GROUP, FRIEND, "小王", "这谁能绷住", 2)
    out = asyncio.run(gc._critic(GROUP, "白得这么彻底，是截图截到灵魂了", False, []))
    assert out == ""                                           # 玩笑：没毛病
    p = seen["p"]
    assert "事实" in p and "玩笑" in p and "截到灵魂了" in p and "fix" not in p
    assert "连续两晚凌晨1点半" in p                                # 图片说明不再截在 80 字


def test_self_blame_without_correction(cfg, brain):
    gc, _ = _gc(cfg, brain)
    gc._record(GROUP, FRIEND, "老张", "所以你觉得是hope发挥最差吗", 1)
    assert gc._dodge("是我表达含糊，我只知道Hope被针对", "", GROUP)
    assert gc._dodge("手不疼，刚才那话我说早了点", "", GROUP)
    assert not gc._dodge("我没说他最差啊，被针对不等于打得烂", "", GROUP)
    gc._record(GROUP, FRIEND, "老张", "al不是打了33分钟吗", 2)          # 真在纠正她：认错是对的
    assert not gc._dodge("啊我记错了，是33分钟", "", GROUP)
    assert not gc._dodge("是我表达含糊", "")                         # 老调用（没有 gid）照旧


def test_answered_and_stale_ask_drop(cfg, brain):
    from aigroup.chat.checks import DROP
    gc, _ = _gc(cfg, brain)
    rid = gc._record(GROUP, FRIEND, "小王", "那牛批", 1)
    gc.note_answered(GROUP, rid)
    row = dict(gc.db.conn.execute("SELECT * FROM qq_group_messages WHERE id=?", (rid,)).fetchone())
    import time
    assert gc._answered(GROUP, row, time.time()).startswith(DROP)


def test_rewrite_minimal_prompt(cfg, brain):
    gc, _ = _gc(cfg, brain)
    seen = {}

    async def chat(messages, **kw):
        seen["m"] = messages[-1]["content"]
        return json.dumps({"act": "skip"})
    gc.brain.llm.chat = chat
    out = asyncio.run(gc._rewrite([{"role": "user", "content": "x"}], "{}", ["自检：重复"], GROUP))
    assert out == [] and "只改有问题的那几个字" in seen["m"] and "act 写 skip" in seen["m"]


def test_parse_and_read_text():
    from aigroup.chat.meme import parse_read, read_text
    d = parse_read({"lines": [{"who": "小王", "text": "压力一个 ai？", "means": "你还给 AI 上压力啊", "tone": "调侃"},
                              {"who": "老张", "text": "今晚吃啥", "means": "字面意思", "tone": "认真"}],
                    "memes": [{"term": "「压力」", "means": "施压、刁难", "sure": 0.85}, {"term": "千言万语", "means": "", "sure": 0.2},
                              {"term": "僵尸", "means": "粉丝外号", "sure": "0.4"}, "坏的"]})
    assert [m["term"] for m in d["memes"]] == ["压力", "千言万语", "僵尸"]
    txt, unsure = read_text(d, {"僵尸": "iG 某选手粉丝的外号"})
    assert "你还给 AI 上压力啊（调侃）" in txt and "今晚吃啥" not in txt
    assert "「压力」：施压、刁难" in txt and "（查过）" in txt
    assert "千言万语」你拿不准" in txt and "这啥梗" in txt
    assert unsure == ["压力", "千言万语"]                        # 查过的不再查


def test_read_turn_block_in_prompt(cfg, brain):
    gc, _ = _gc(cfg, brain)
    gc.g["meme_lookup"] = False
    real = gc.brain.llm.chat

    async def chat(messages, **kw):
        if "把标了 ▶ 的几句群友的话读懂" in messages[-1]["content"]:
            assert "▶ 小王：压力一个 ai？" in messages[-1]["content"]
            return json.dumps({"lines": [{"who": "小王", "text": "压力一个 ai？", "means": "你还给 AI 上压力啊", "tone": "调侃"}],
                               "memes": [{"term": "压力", "means": "施压", "sure": 0.9}]}, ensure_ascii=False)
        return await real(messages, **kw)
    gc.brain.llm.chat = chat
    gc._record(GROUP, FRIEND, "小王", "压力一个 ai？", 1)
    txt = asyncio.run(gc.read_turn(GROUP))
    assert "你还给 AI 上压力啊" in txt
    msgs = asyncio.run(gc.build_messages(GROUP, False, False, "小王", None))
    assert "# 刚才这几句的意思（你先读过一遍" in msgs[1]["content"] and "施压" in msgs[1]["content"]


def test_meme_lookup_saves_and_shows(cfg, brain):
    gc, _ = _gc(cfg, brain)

    class Web:
        enabled = True
        qs = []

        async def agent_search(self, q, **kw):
            self.qs.append(q)
            return [{"title": "压力是什么梗", "snippet": "“压力给到xx”指给某人施压、刁难，常用于调侃"},
                    {"title": "x", "snippet": "y"}, {"title": "a", "snippet": "b"}, {"title": "c", "snippet": "d"}], "DeepSeek"
    gc.web = Web()

    async def js(messages, default=None, role="helper"):
        assert "只照上面搜到的写" in messages[-1]["content"]
        return {"known": True, "meme": True, "means": "给某人施压、刁难", "usage": "调侃时说压力给到谁", "sure": 0.8}
    gc.brain.llm.json = js
    m = asyncio.run(gc.meme_lookup(GROUP, "压力", "压力一个 ai？"))
    assert m.startswith("给某人施压")
    r = gc.db.conn.execute("SELECT * FROM qq_jargon WHERE group_id=? AND term='压力'", (GROUP,)).fetchone()
    assert r["is_jargon"] == 2 and r["src"] == "web"
    assert "压力：给某人施压" in gc.learner.jargon_block(GROUP, "压力一个 ai")      # 一个人用也带上
    n = len(Web.qs)
    assert asyncio.run(gc.meme_lookup(GROUP, "压力")) == m and len(Web.qs) == n       # 查过的不再搜


def test_meme_lookup_unknown_not_invented(cfg, brain):
    gc, _ = _gc(cfg, brain)

    class Web:
        enabled = True
        n = 0

        async def agent_search(self, q, **kw):
            Web.n += 1
            return [{"title": "千言万语 成语", "snippet": "形容说的话很多"}], "DeepSeek"
    gc.web = Web()

    async def js(messages, default=None, role="helper"):
        return {"known": False}
    gc.brain.llm.json = js
    assert asyncio.run(gc.meme_lookup(GROUP, "千言万语", "千言万语")) == ""
    r = gc.db.conn.execute("SELECT * FROM qq_jargon WHERE group_id=? AND term='千言万语'", (GROUP,)).fetchone()
    assert r["src"] == "web-none" and r["meaning"] == ""
    k = Web.n
    asyncio.run(gc.meme_lookup(GROUP, "千言万语"))
    assert Web.n == k                                          # 七天内不再查
    assert "千言万语" not in gc.learner.jargon_block(GROUP, "千言万语")


def test_meme_daily_cap(cfg, brain):
    gc, _ = _gc(cfg, brain)
    gc.g["meme_per_day"] = 0

    class Web:
        enabled = True

        async def agent_search(self, q, **kw):
            raise AssertionError("不该搜")
    gc.web = Web()
    assert asyncio.run(gc.meme_lookup(GROUP, "压力")) == ""


def test_buy_asker_profile(cfg, brain):
    gc, _ = _gc(cfg, brain)
    gc._record(GROUP, FRIEND2, "小李", "预算两千买什么耳机适合我", 1)
    gc.person = lambda uid, gid: {"profile": {"兴趣": "通勤听歌、打游戏", "别碰": "体重", "近况": "在秋招"}}
    note = gc._asker_note(GROUP, "buy")
    assert "小李" in note and "通勤听歌" in note and "体重" not in note
    assert gc._asker_note(GROUP, "judge") == ""
