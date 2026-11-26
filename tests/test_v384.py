"""38.4 《用文言文打败鱼八股》对照：八股词表补“中登江湖味”；聊天记录里她自己的八股不原样给模型看；
多一句“文言 → 白话”的说法（默认关）；人设第一人称试验版。"""
import asyncio
import json

from conftest import FRIEND, GROUP, J, _gc


def test_bagu_new_families_hit():
    from aigroup.chat.quality import check
    for t in ("这话我记下了", "行，十二点是你对，我认", "比之前的都稳", "睡吧，我守着", "我不拦你",
              "你自己掂量", "这句接得漂亮", "这是他告诉你的，还是你自己想明白的", "很稳"):
        assert "八股" in check(t)[1], t


def test_bagu_no_false_positive_on_plain_talk():
    from aigroup.chat.quality import check
    for t in ("我认识他", "说上号说得比谁都响", "这个比那个重", "我记得你说过", "认真的吗", "今天挺稳的发挥",
              "稳住别浪", "我不拦车了直接走", "收不收货"):
        assert "八股" not in check(t)[1], t


def test_clean_style_keeps_meaning():
    from aigroup.chat.quality import clean_style
    assert clean_style("行，十二点是你对，我认，那我这杯奶茶算是欠老张了") == ("行，十二点是你对，那我这杯奶茶算是欠老张了", True)
    assert clean_style("下次注意，这话我记下来了，七天以后我再问一遍")[0] == "下次注意，七天以后我再问一遍"
    assert clean_style("睡吧，我守着") == ("", True)
    assert clean_style("今天好冷") == ("今天好冷", False)
    assert clean_style("要不要我帮你整理一下")[1]                 # 助手腔也算


def test_self_clean_rules_and_tags():
    from aigroup.chat.prompt import PromptMixin as P
    assert P.self_clean("睡吧，我守着") == "（你回了一句）"
    assert P.self_clean("行，十二点是你对，我认") == "行，十二点是你对"
    assert P.self_clean("先查了一下，政策十月改了，不用带身份证", tone_bad=True) == "先查了一下……"
    assert P.self_clean("今天好冷", tone_bad=False) == "今天好冷"


def test_transcript_cleans_only_her_lines(cfg, brain):
    gc, ob = _gc(cfg, brain)
    gc._record(GROUP, FRIEND, "小王", "这话我记下了", 1)                 # 群友说的不动
    gc._record(GROUP, 0, "小满", "行，这话我记下了，明天再说", 2, role="assistant")
    t = gc.transcript(GROUP)
    assert "小王" in t and "这话我记下了" in t.split("小王")[1].split("\n")[0]
    her = [ln for ln in t.splitlines() if "你：" in ln][0]
    assert "记下" not in her and "明天再说" in her
    assert "记下" in [ln for ln in gc.transcript(GROUP, n=10).splitlines() if "你：" in ln][0]   # 自检等别处看原话
    gc.g["self_clean"] = False
    assert "记下" in [ln for ln in gc.transcript(GROUP).splitlines() if "你：" in ln][0]


def test_transcript_tone_tagged_review(cfg, brain):
    gc, ob = _gc(cfg, brain)
    gc._record(GROUP, FRIEND, "小王", "办这个要带啥", 1)
    gc._record(GROUP, 0, "小满", "先说结论，手机钱包就能办，不用带身份证和现金，窗口那边也认电子的", 2, role="assistant")
    mid = brain.db.conn.execute("SELECT id FROM qq_group_messages WHERE role='assistant' ORDER BY id DESC LIMIT 1").fetchone()[0]
    brain.db.conn.execute("INSERT INTO qq_human_score(trace, group_id, said, context, score, ts, src, tags, better, pick, msg_id) "
                          "VALUES ('t1', ?, 'x', '', 2, 0, 'claude-review', '交报告', '', '', ?)", (GROUP, mid))
    her = [ln for ln in gc.transcript(GROUP).splitlines() if "你：" in ln][0]
    assert her.endswith("先说结论……")


def test_review_accepts_bagu_tag():
    from aigroup.memory.review import SCORE_TAGS, TONE_TAGS
    assert "八股" in SCORE_TAGS and set(TONE_TAGS) <= set(SCORE_TAGS)


def test_parse_plan_wy_and_prompt_field(cfg, brain):
    from aigroup.chat.prompt import PromptMixin as P
    d = P.parse_plan_text(json.dumps({"act": "say", "wy": "过在吾也", "say": ["行吧"]}, ensure_ascii=False))
    assert d["wy"] == "过在吾也" and d["cands"] == [["行吧"]]
    assert P.parse_plan_text(json.dumps({"act": "say", "wy": None, "say": ["x"]}))["wy"] == ""
    gc, ob = _gc(cfg, brain)
    gc._record(GROUP, FRIEND, "小王", "在吗", 1)
    sysmsg = lambda: asyncio.run(gc.build_messages(GROUP, True, False, "小王", None))[0]["content"]  # noqa: E731
    assert '"wy"' not in sysmsg()
    gc.g["wenyan_cand"] = True
    assert '"wy"' in sysmsg()


def test_wenyan_prompt():
    from aigroup.chat.choose import wenyan_prompt
    p = wenyan_prompt("小满", "过在吾也", "短，嘴硬", "你又说错了")
    assert "过在吾也" in p and "短，嘴硬" in p and "你又说错了" in p and "我认" in p


def test_wenyan_cand_joins_choice(cfg, brain):
    gc, ob = _gc(cfg, brain, cooldown_seconds=0, wenyan_cand=True)
    gc._record(GROUP, FRIEND, "小王", "你刚才说错了吧", 1)
    orig = brain.llm.chat

    async def chat(messages, **kw):
        c = messages[-1]["content"]
        if "先用文言写下了意思" in c:
            return "啊这次赖我"
        if "帮她挑一句最好的" in c:
            return json.dumps({"cands": [{"k": 1, "score": 2}, {"k": 2, "score": 2}, {"k": 3, "score": 5}], "best": 3})
        return await orig(messages, **kw)
    brain.llm.chat = chat
    brain.llm.reply = J(act="say", kind="I1", to=1, len="short", say=["行，我认"], alt=[["这个我记下了"]], wy="过在吾也",
                        move="自嘲")
    assert asyncio.run(gc.speak(GROUP, mentioned=True, owner_spoke=False, trigger=1, who="小王"))
    sent = "".join(s["data"].get("text", "") for _, segs in ob.sent for s in segs)
    assert "赖我" in sent
    q = json.loads(brain.db.conn.execute("SELECT quality FROM qq_trace ORDER BY id DESC LIMIT 1").fetchone()[0])
    assert q["wy_k"] == 2 and q["chosen"] == 2 and q["wy"] == "过在吾也"


def test_wenyan_off_by_default(cfg, brain):
    gc, ob = _gc(cfg, brain, cooldown_seconds=0)
    gc._record(GROUP, FRIEND, "小王", "你刚才说错了吧", 1)
    calls = []
    orig = brain.llm.chat

    async def chat(messages, **kw):
        calls.append(messages[-1]["content"])
        return await orig(messages, **kw)
    brain.llm.chat = chat
    brain.llm.reply = J(act="say", kind="I1", to=1, len="short", say=["啊是我看错了"], wy="过在吾也", move="自嘲")
    assert asyncio.run(gc.speak(GROUP, mentioned=True, owner_spoke=False, trigger=1, who="小王"))
    assert not any("先用文言写下了意思" in c for c in calls)
