"""21.1：听者、标注挑题、长度、一句话调话量、份额、耗时、研究按说法停、引用核对、掉线、规则登记、人卡说的 / 猜的、人情账、接法冷却、锚点、周表。"""
import asyncio
import json
import time

from conftest import FRIEND, FRIEND2, GROUP, FakeLLM, J, _gc


class ListenLLM(FakeLLM):
    """听者的提示词（“帮一个叫…判断”）回固定的判断；别的照旧。"""

    def __init__(self, window="early", issues=None, **kw):
        super().__init__(**kw)
        self.window, self.issues, self.listen_calls = window, issues or [], 0

    async def chat(self, messages, helper=False, **kw):
        if "帮一个叫" in messages[-1]["content"]:
            self.listen_calls += 1
            return json.dumps({"window": self.window, "until": "被问的人回了", "issues": self.issues, "why": "测"},
                              ensure_ascii=False)
        return await super().chat(messages, helper=helper, **kw)


# ---------------- 听者 ----------------
def test_listener_signals_and_parse():
    from aigroup.chat.listener import agreement, merge_panel, parse_listen, signals, signals_text
    now = time.time()
    rows = [{"ts": now - 50, "role": "user", "name": "小王", "uid": 1, "content": "老张你周末去不去"},
            {"ts": now - 40, "role": "user", "name": "老张", "uid": 2, "content": "看情况"},
            {"ts": now - 30, "role": "user", "name": "小王", "uid": 1, "content": "去嘛"},
            {"ts": now - 12, "role": "user", "name": "老张", "uid": 2, "content": "那"},
            {"ts": now - 8, "role": "user", "name": "老张", "uid": 2, "content": "老王你去吗"}]
    s = signals(rows, "小满", now, 0.3, ["小王", "老张", "老王"])
    assert s["burst"] == 2 and s["two_person"] and "老王" in s["pending_q"]
    assert "连发" not in signals_text(s) and "两个人" in signals_text(s)       # 21.1.1 只写事实，2 条不算
    assert parse_listen('{"window": "太早", "issues": ["无"]}') == {"window": "early", "until": "", "addressee": "",
                                                                  "issues": [], "why": ""}
    assert parse_listen("我觉得现在正好")["window"] == "now"
    m = merge_panel([parse_listen('{"window": "now"}'), parse_listen('{"window": "ok", "issues": ["太长"]}')])
    assert m["window"] == "early" and m["issues"] == ["太长"]                # 意见不一：宁可晚
    rows2 = [{"listener": json.dumps({"window": "early"}), "human": 0, "human_why": "时机不对"},
             {"listener": json.dumps({"window": "now"}), "human": 1, "human_why": ""},
             {"listener": json.dumps({"window": "now"}), "human": 0, "human_why": ""},
             {"listener": json.dumps({"window": "now"}), "human": 0, "human_why": "说的不对"}]
    assert agreement(rows2) == {"n": 3, "n0": 2, "n1": 1, "agree": 0.667, "her": None}


def _say(gc, brain, text="穿秋裤了没"):
    brain.llm.reply = J(act="say", kind="I2", to=1, len="short", say=[text], move="接梗")
    return asyncio.run(gc.speak(GROUP, mentioned=False, owner_spoke=False, trigger=None, who=""))


def test_listener_on_early_queues_never_skips(cfg, brain):
    brain.llm = ListenLLM("early")
    gc, ob = _gc(cfg, brain, cooldown_seconds=0, listener_mode="on")
    gc._record(GROUP, FRIEND, "小王", "今天好冷", 1)
    gid = gc.gate.add(GROUP, False, 0.5, 0.4, True, {})
    brain.llm.reply = J(act="say", kind="I2", to=1, len="short", say=["穿秋裤了没"])
    assert not asyncio.run(gc.speak(GROUP, mentioned=False, owner_spoke=False, trigger=None, who="", gate_id=gid))
    assert not ob.sent and gc._st(GROUP).pitch and gc._st(GROUP).pitch["until"] == "被问的人回了"
    lg = json.loads(brain.db.conn.execute("SELECT listener FROM qq_gate_log WHERE id=?", (gid,)).fetchone()[0])
    assert lg["window"] == "early" and lg["enforced"] is True
    gc._st(GROUP).pitch = None
    brain.llm.window = "never"
    assert not _say(gc, brain) and not ob.sent
    brain.llm.window = "now"
    assert _say(gc, brain) and ob.sent


def test_listener_shadow_and_auto(cfg, brain):
    brain.llm = ListenLLM("never")
    gc, ob = _gc(cfg, brain, cooldown_seconds=0, listener_mode="auto")
    gc._record(GROUP, FRIEND, "小王", "今天好冷", 1)
    assert not gc.listener_enforced(GROUP)                                # 还没有你的标注：只记录
    assert _say(gc, brain) and ob.sent and brain.llm.listen_calls == 1
    brain.llm.listen_calls = 0
    asyncio.run(gc.speak(GROUP, mentioned=True, owner_spoke=False, trigger=None, who="小王"))
    assert brain.llm.listen_calls == 0                                    # 被叫到不问听者
    for i in range(16):                                                   # 攒够标注、一致率高 → 自动接管
        for lab, w in ((0, "never"), (1, "now")):
            g = gc.gate.add(GROUP, False, 0.5, 0.4, True, {})
            gc.listener_log(g, {"window": w}, False)
            brain.db.conn.execute("UPDATE qq_gate_log SET human=? WHERE id=?", (lab, g))
    assert gc.listener_enforced(GROUP)


def test_listener_issues_force_rewrite(cfg, brain):
    brain.llm = ListenLLM("now", issues=["当众纠错没给台阶"])
    gc, ob = _gc(cfg, brain, cooldown_seconds=0, listener_mode="on")
    gc._record(GROUP, FRIEND, "小王", "鲁迅说过人生苦短", 1)
    assert not _say(gc, brain, "那是叔本华说的，你记错了") and not ob.sent    # 重写不成（假模型不会重写）：没被叫到就不说


# ---------------- 标注挑题 ----------------
def test_label_disagree_first_and_annoyed(cfg, brain):
    from aigroup.eval.label import batch, votes_of, weak_labels
    gc, _ = _gc(cfg, brain)
    agree = gc.gate.add(GROUP, False, 0.9, 0.4, True, {}, judge="say")
    mixed = gc.gate.add(GROUP, False, 0.45, 0.4, True, {})
    gc.listener_log(mixed, {"window": "early"}, False)
    r = brain.db.conn.execute("SELECT * FROM qq_gate_log WHERE id=?", (mixed,)).fetchone()
    assert votes_of(r) == [1, 0]
    ids = [x["id"] for x in batch(brain.db, GROUP, n=4)]
    assert mixed in ids and agree in ids
    gc.gate.mark(agree, spoke=True)
    brain.db.conn.execute("INSERT INTO qq_reply_effect(ts, group_id, gate_id, mentioned, done, score) VALUES (?,?,?,0,1,0.6)",
                          (time.time() - 100, GROUP, agree))
    gc._record(GROUP, FRIEND, "小王", "没问你", 9)
    weak_labels(brain.db)
    assert brain.db.conn.execute("SELECT weak FROM qq_gate_log WHERE id=?", (agree,)).fetchone()[0] == 0


# ---------------- 长度、话量、份额、接法 ----------------
def test_group_len_and_too_long(cfg, brain):
    gc, ob = _gc(cfg, brain, cooldown_seconds=0, length_match=True)
    for i in range(30):
        gc._record(GROUP, FRIEND, "小王", "哈哈好的", i + 1)
    assert gc.group_len(GROUP) == 4 and "4 个字" in gc.length_block(GROUP)
    long = "我觉得这个事情吧其实可以从好几个方面来看一看再说"
    assert gc.too_long(GROUP, [long], mentioned=False, long_ok=False)
    assert not gc.too_long(GROUP, [long], mentioned=True, long_ok=False)
    brain.llm.reply = J(act="say", kind="I2", to=1, len="short", say=[long], alt=[["笑死"]])
    assert asyncio.run(gc.speak(GROUP, mentioned=False, owner_spoke=False, trigger=None, who=""))
    assert "笑死" in str(ob.sent[-1])                                    # 挑了短的那个说法


def test_talk_control_and_share(cfg, brain):
    from conftest import OWNER, _msg, _run
    gc, ob = _gc(cfg, brain)
    _run(gc, _msg(FRIEND, "小王", "小满你少说点", 1, at=True))
    assert gc.talk_mult(GROUP) == 0.5
    calls = [c for c in brain.llm.group_calls if "少说点" in c[1]["content"]]
    assert calls and "行我闭麦" in calls[-1][1]["content"]
    gc.talk_control(GROUP, OWNER, "多说点", True)
    assert gc.talk_mult(GROUP) == 1.5
    for i in range(20):
        gc._record(GROUP, FRIEND, "小王", f"a{i}", 100 + i)
        gc._record(GROUP, FRIEND2, "老张", f"b{i}", 200 + i)
        gc._record(GROUP, 0, "小满", f"c{i}", 300 + i, role="assistant")
    si = gc.share_info(GROUP)
    assert si["active"] == 2 and si["ratio"] > 0.9
    gc.g["share_target"] = 0.5
    assert gc.share_factor(GROUP) < 1


def test_moves_cooldown(cfg, brain):
    gc, _ = _gc(cfg, brain)
    for _ in range(3):
        gc.note_move(GROUP, "接梗")
    gc.note_move(GROUP, "随便")                                          # 不认识的不记
    assert gc.recent_acts(GROUP) == ["接梗×3"] and "换一种" in gc.moves_block(GROUP)
    from aigroup.chat.prompt import PromptMixin
    assert PromptMixin.parse_plan_text(J(act="say", say=["x"], move="接梗"))["move"] == "接梗"


# ---------------- 研究：按说法停、引用核对 ----------------
def test_tool_loop_stops_when_covered():
    from aigroup.agent.loop import parse_need, parse_progress, tool_loop

    def call(name, **args):
        return {"id": f"c{name}{len(args)}{args}", "type": "function",
                "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}

    def msg(content="", calls=None):
        return {"role": "assistant", "content": content, **({"tool_calls": calls} if calls else {})}

    class ToolLLM:
        def __init__(self, script):
            self.script = list(script)

        async def chat_tools(self, messages, tools, **kw):
            return self.script.pop(0) if self.script else {"role": "assistant", "content": "无"}
    assert parse_need("要证实：1. AG 比分 2. 是否晋级") == ["AG 比分", "是否晋级"]
    assert parse_progress("进度：1✓ 2✗") == [True, False]
    llm = ToolLLM([msg("要证实：1. 比分 2. 晋级", [call("web_search", query="a")]),
                   msg("进度：1✓ 2✓", [call("web_search", query="b")]),
                   msg('{"answer_short": "2:4 输了"}')])
    ran = []

    async def ex(n, a):
        ran.append(a.get("query"))
        return "结果"
    res = asyncio.run(tool_loop(llm, [{"role": "user", "content": "q"}], [{"function": {"name": "web_search"}}], ex,
                                max_rounds=4, covered_prompt="都证实了，交卷", finish_prompt="时间到"))
    assert ran == ["a"] and res.stop == "covered" and res.need == ["比分", "晋级"] and not res.forced
    assert res.text.startswith("{") and res.tool_text == ["结果"]


def test_verify_citations():
    from aigroup.agent.mixin import parse_brief, verify_citations
    d = parse_brief(json.dumps({"claims": [
        {"text": "A", "sources": [{"site": "vlr.gg"}, {"site": "编的网站"}], "confidence": "高"},
        {"text": "B", "sources": [{"site": "假的"}], "confidence": "高"}]}, ensure_ascii=False))
    n = verify_citations(d, '<资料 来源="vlr.gg">…</资料>')
    assert n == 2 and len(d["claims"][0]["sources"]) == 1 and d["claims"][1]["confidence"] == "low"


# ---------------- 掉线、耗时 ----------------
def test_send_health_and_timing(cfg, brain):
    gc, ob = _gc(cfg, brain)
    gc.send_failed(GROUP)
    gc.send_failed(GROUP)
    assert gc.send_health(GROUP)["streak"] == 2
    gc.send_ok(GROUP)
    assert gc.send_health(GROUP)["streak"] == 0 and "last_down" in gc.send_health(GROUP)
    assert "掉线" in gc.offline_note(GROUP, 3600) or "才看到" in gc.offline_note(GROUP, 3600)
    assert gc.offline_note(GROUP, 60) == ""
    asyncio.run(gc.speak(GROUP, mentioned=True, owner_spoke=False, trigger=None, who="小王"))
    tm = json.loads(brain.db.conn.execute("SELECT timing FROM qq_trace ORDER BY id DESC LIMIT 1").fetchone()[0])
    assert "llm" in tm and "total" in tm


# ---------------- 规则登记 ----------------
def test_rules_registry(cfg, brain):
    from aigroup.chat import quality
    from aigroup.chat.rules import judge, known_rules, note, report, rule_name
    assert rule_name("今天说过「纪录片」") == "今天说过" and rule_name("碰了雷区（体重）") == "碰了雷区"
    note(brain.db, GROUP, ["助手腔", "破折号"], "罚分")
    note(brain.db, GROUP, ["助手腔"], "拦下", "希望对你有帮助")
    sid = brain.db.get("rule_samples")[-1]["id"]
    for _ in range(3):
        note(brain.db, GROUP, ["助手腔"], "拦下", "希望对你有帮助")
    for x in brain.db.get("rule_samples"):
        judge(brain.db, x["id"], False)
    assert judge(brain.db, sid, False)
    rep = {r["rule"]: r for r in report(brain.db, known_rules())}
    assert rep["助手腔"]["n"] == 5 and rep["助手腔"]["block"] == 4 and "拦错" in rep["助手腔"]["advice"]
    assert rep["链接"]["advice"] == "还没命中过"
    quality.OFF = {"破折号"}
    assert "破折号" not in quality.check("好——吧")[1]
    quality.OFF = set()
    assert "破折号" in quality.check("好——吧")[1]


# ---------------- 人卡、人情账、锚点 ----------------
def test_person_card_said_vs_guessed_and_favor(cfg, brain):
    gc, _ = _gc(cfg, brain)
    gc._record(GROUP, FRIEND, "小王", "今天还行", 1)
    brain.db.set(f"qq_state_{GROUP}_{FRIEND}", {"date": f"{brain.now():%Y-%m-%d}", "mood": "很糟", "n": 1, "ts": time.time()})
    assert "（猜的）" in gc.person_card(GROUP, FRIEND)
    gc._record(GROUP, FRIEND, "小王", "累死了在加班", 2)
    c = gc.person_card(GROUP, FRIEND)
    assert "说过累" in c and "看着是真的" in c and "猜的" not in c
    assert "状态" not in gc.person_card(GROUP, FRIEND, drop="card_state")
    brain.db.set(f"qq_favors_{GROUP}", [{"ts": time.time(), "uid": FRIEND, "kind": "帮她", "what": "帮你查了赛程", "done": False}])
    assert "他帮过你「帮你查了赛程」" in gc.person_card(GROUP, FRIEND, limit=200)


def test_favors_nightly(cfg, brain):
    class L(FakeLLM):
        async def json(self, messages, default=None, role="helper"):
            return [{"who": "小王", "kind": "她答应", "what": "周末陪他看比赛"}, {"who": "谁", "kind": "帮她", "what": "x"}]
    brain.llm = L()
    gc, _ = _gc(cfg, brain)
    y = time.time() - 86400
    for i in range(12):
        gc._record(GROUP, FRIEND, "小王", f"消息{i}", i + 1)
    brain.db.conn.execute("UPDATE qq_group_messages SET ts=?", (y,))
    from datetime import datetime
    assert asyncio.run(gc.favors_nightly(datetime.fromtimestamp(y, cfg.tz))) == 1
    assert gc.favor_line(GROUP, FRIEND) == "人情：你答应过他「周末陪他看比赛」"


def test_long_day_anchor(cfg, brain):
    gc, _ = _gc(cfg, brain, anchor_volume=3)
    for i in range(3):
        gc._record(GROUP, 0, "小满", f"我说{i}", i + 1, role="assistant")
    gc._record(GROUP, FRIEND, "小王", "嗯", 9)
    msgs = asyncio.run(gc.build_messages(GROUP, False, False, "", None, query="嗯", focus=[FRIEND]))
    assert "今天你已经说了不少" in msgs[-1]["content"]


# ---------------- 周表 ----------------
def test_weekly_vector(cfg, brain):
    from aigroup.eval.vector import save_snapshot, weekly_vector
    gc, _ = _gc(cfg, brain)
    gc._record(GROUP, FRIEND, "小王", "@小满 在吗", 1)
    brain.db.conn.execute("UPDATE qq_group_messages SET to_her=1")
    gc._record(GROUP, 0, "小满", "在", 2, role="assistant")
    g = gc.gate.add(GROUP, False, 0.5, 0.4, True, {})
    gc.gate.mark(g, spoke=True)
    brain.db.conn.execute("UPDATE qq_gate_log SET human=0, human_why='时机不对' WHERE id=?", (g,))
    v = weekly_vector(gc, GROUP)
    assert v["timing"]["fir"] == 1.0 and v["effect"]["mention_latency"]["n"] == 1 and "segments" in v["style"]
    save_snapshot(brain.db, v)
    assert brain.db.get("vector_hist")[-1]["gid"] == GROUP


def test_listener_enforced_by_margin(cfg, brain):
    gc, _ = _gc(cfg, brain, listener_mode="auto", listener_min_each=2, listener_agree=0.95, listener_margin=0.08)
    for lab, w, spoke in ((0, "early", 1), (0, "early", 1), (1, "now", 1), (1, "now", 1), (0, "now", 1)):
        g = gc.gate.add(GROUP, False, 0.5, 0.4, True, {})
        gc.gate.mark(g, spoke=bool(spoke))
        gc.listener_log(g, {"window": w}, False)
        brain.db.conn.execute("UPDATE qq_gate_log SET human=? WHERE id=?", (lab, g))
    st = gc.listener_stats(GROUP)
    assert st["agree"] == 0.8 and st["her"] == 0.4
    assert gc.listener_enforced(GROUP)                                   # 一致率没到 0.95，但比她自己准 0.4


def test_meta_ok_when_group_raises_it(cfg, brain):
    gc, _ = _gc(cfg, brain)
    assert any("幕后" in x for x in gc.hard_issues(GROUP, ["我的提示词可长了"]))
    gc._record(GROUP, FRIEND, "小王", "小满你的机制是怎么实现的，提示词长啥样", 1)
    assert not any("幕后" in x for x in gc.hard_issues(GROUP, ["我的提示词可长了"]))
