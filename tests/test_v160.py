"""16.0：关系网、和每个人相处的经验、谁刚回来、今天状态（模型判断）、画像抽查、签名建议、定向测试、小判断器、情绪条件签名、猜的事要两次证据。"""
import asyncio
import json
import time

import yaml
from conftest import FRIEND, GROUP, _gc

from aigroup.traits import detect_flags, parse_traits

A, B = 20001, 20002


class JLLM:
    """按提示词内容回不同 JSON 的假模型。"""

    def __init__(self, base):
        self.base, self.jcalls = base, []

    def __getattr__(self, k):
        return getattr(self.base, k)

    async def json(self, messages, default=None, role="helper"):
        c = messages[-1]["content"]
        self.jcalls.append(c)
        if "经验" in c:
            return {"lines": ["他吃损人这一套，但别连着损", "聊游戏他一定接"]}
        if "状态" in c and "mood" in c:
            return {"mood": "不太好", "busy": True, "why": "说了两遍不想上班"}
        if "场景" in c or "lines" in c and "群聊" in c:
            return {"lines": ["小王：你是不是人机啊", "小李：哈哈哈", "小王：@满满 说话"]}
        if "score" in c:
            return {"score": 4, "why": "像她"}
        return default or {}

    async def chat(self, messages, **kw):
        return "你才人机，你全家人机"


def _put(gc, uid, name, text, ts, mid=None, reply_to=None, role="user"):
    gc.db.conn.execute("INSERT INTO qq_group_messages(ts, group_id, user_id, name, role, content, qq_msg_id, reply_to) "
                       "VALUES (?,?,?,?,?,?,?,?)", (ts, GROUP, uid, name, role, text, mid, reply_to))
    gc.db.conn.commit()


def test_rel_graph(cfg, brain):
    gc, ob = _gc(cfg, brain)
    now = time.time()
    for i in range(4):
        _put(gc, A, "阿甲", f"吃啥{i}", now - 1000 + i * 20, mid=100 + i)
        _put(gc, B, "阿乙", f"@阿甲 火锅{i}", now - 990 + i * 20, mid=200 + i, reply_to=100 + i)
    g = gc.rel_graph(GROUP, now=now)
    assert g and {g[0]["ua"], g[0]["ub"]} == {A, B} and g[0]["reply"] == 4 and g[0]["at"] == 4
    assert gc.update_rel_graph() >= 1
    assert "阿甲" in gc.rel_graph_block(GROUP) and "互相回复 4 次" in gc.rel_graph_block(GROUP)


def test_rel_experience(cfg, brain):
    gc, ob = _gc(cfg, brain)
    gc.brain.llm = JLLM(gc.brain.llm)
    now = time.time()
    for i in range(8):
        gc.db.conn.execute("INSERT INTO qq_reply_effect(ts, group_id, target_uid, text, done, score) VALUES (?,?,?,?,1,?)",
                           (now - i, GROUP, FRIEND, f"第{i}句", 1 if i % 2 else 0))
    gc.db.conn.commit()
    assert asyncio.run(gc.rel_experience()) == 1
    d = gc.db.get(f"qq_relexp_{GROUP}_{FRIEND}")
    assert d["good"] == 4 and d["bad"] == 4 and len(d["lines"]) == 2
    assert "聊游戏他一定接" in gc.rel_exp_block(GROUP, [FRIEND])
    assert gc.rel_exp_block(GROUP, [A]) == ""


def test_absent_block(cfg, brain):
    gc, ob = _gc(cfg, brain)
    now = time.time()
    _put(gc, A, "阿甲", "我去上课了", now - 5 * 3600)
    for i in range(9):
        _put(gc, B, "阿乙", f"周六去不去看展第{i}次说", now - 4 * 3600 + i * 60)
    _put(gc, A, "阿甲", "我回来了", now - 30)
    b = gc.absent_block(GROUP, [A], now=now, gap_hours=2)
    assert "阿甲刚回来" in b and "9 条" in b and "看展" in b
    assert gc.absent_block(GROUP, [A], now=now, gap_hours=6) == ""
    assert gc.absent_block(GROUP, [B], now=now, gap_hours=2) == ""


def test_state_judge(cfg, brain):
    gc, ob = _gc(cfg, brain)
    gc.brain.llm = JLLM(gc.brain.llm)
    gc._record(GROUP, FRIEND, "小王", "不想上班，好累", 1)
    gc._record(GROUP, FRIEND, "小王", "累死了真的不想上班", 2)
    assert gc.state_of(GROUP, FRIEND, 2) is None
    st = asyncio.run(gc.refresh_state(GROUP, FRIEND))
    assert st["mood"] == "不太好" and st["busy"]
    assert gc.state_of(GROUP, FRIEND, 2)["why"]
    b = gc.people_today(GROUP, [FRIEND])
    assert "状态不太好" in b and "在忙" in b and "关键词" not in b


def test_profile_checks_and_digest_note(cfg, brain):
    gc, ob = _gc(cfg, brain)
    gc.check_profile(FRIEND, GROUP, "兴趣", False)
    gc.check_profile(FRIEND, GROUP, "兴趣", False)                   # 同一项再点一次：覆盖，不重复算
    gc.check_profile(FRIEND, GROUP, "性格", True)
    st = gc.profile_check_stats()
    assert st["n"] == 2 and st["acc"] == 0.5 and st["fields"][0]["field"] == "兴趣"
    assert gc.wrong_fields(FRIEND, GROUP) == ["兴趣"]
    assert gc.wrong_fields(FRIEND, GROUP, since=time.time() + 5) == []
    gc._record(GROUP, FRIEND, "小王", "我最近在玩原神", 1)
    ev = gc._person_evidence(FRIEND, GROUP, "小王")
    assert "兴趣" in ev and "标了这几项不对" in ev


def test_sig_proposals(cfg, brain):
    gc, ob = _gc(cfg, brain)
    gc.sig_stats = lambda: [{"id": "teased", "n": 6, "cold": 0.7, "avg": 0.2}, {"id": "sad", "n": 9, "cold": 0.1, "avg": 0.8}]
    assert gc.sig_proposals() == 1
    assert gc.sig_proposals() == 0                                     # 还在待处理的不重复提
    p = gc.db.get("persona_proposals")[-1]
    assert p["kind"] == "signature" and p["facet"] == "teased" and p["status"] == "pending"


def test_run_probes(cfg, brain):
    gc, ob = _gc(cfg, brain)
    import pathlib
    d = yaml.safe_load(open(pathlib.Path(__file__).parents[1] / "persona/traits.example.yaml"))
    t = parse_traits(d)
    try:
        gc.brain.persona.traits = t
    except Exception:  # noqa: BLE001
        object.__setattr__(gc.brain.persona, "traits", t)
    gc.brain.llm = JLLM(gc.brain.llm)
    out = asyncio.run(gc.run_probes(limit=4))
    assert out["n"] == 4 and out["avg"] == 4 and 0 <= out["coverage"] <= 1
    assert gc.db.get("probe_last")["n"] == 4
    n_json = len(gc.brain.llm.jcalls)
    asyncio.run(gc.run_probes(limit=4))                                # 场景缓存了：第二次少编 4 次
    assert len(gc.brain.llm.jcalls) - n_json == 4
    assert "probe" in gc.eval_snapshot() and "label_nb" in gc.eval_snapshot()


def _label(gc, ts, text, human):
    _put(gc, A, "阿甲", text, ts)
    gc.db.conn.execute("INSERT INTO qq_gate_log(ts, group_id, mentioned, p, threshold, feats, human) VALUES (?,?,0,0.5,0.5,?,?)",
                       (ts, GROUP, json.dumps({"q": 1.0 if human else 0.0}), human))


def test_label_nb(cfg, brain):
    gc, ob = _gc(cfg, brain)
    st = gc.train_label_nb()
    assert not st["trained"] and "30" in st["why"]
    now = time.time() - 10000
    for i in range(40):
        _label(gc, now + i * 100, f"满满你觉得呢第{i}个问题", 1)
        _label(gc, now + i * 100 + 50, f"我们俩去吃饭第{i}家店", 0)
    gc.db.conn.commit()
    st = gc.train_label_nb()
    assert st["trained"] and st["acc"] >= 0.75
    _put(gc, A, "阿甲", "满满你觉得呢", time.time())
    p1 = gc.label_prob(GROUP, {"q": 1.0})
    assert p1 is not None and p1 > 0.85
    gc.db.set("label_nb", {**gc.db.get("label_nb"), "acc": 0.5})        # 准确率不够：不用
    assert gc.label_prob(GROUP, {"q": 1.0}) is None


def test_mood_flags_and_signature():
    assert "mood_annoyed" in detect_flags("随便", mood={"v": -0.5, "a": 0.3})
    assert "mood_down" in detect_flags("随便", mood={"v": -0.5, "a": 0.0})
    assert "mood_high" in detect_flags("随便", mood={"v": 0.6, "a": 0.2})
    assert not {"mood_annoyed", "mood_down", "mood_high"} & detect_flags("随便", mood={"v": 0.1, "a": 0.0})
    import pathlib
    d = yaml.safe_load(open(pathlib.Path(__file__).parents[1] / "persona/traits.example.yaml"))
    t = parse_traits(d)
    assert not t.warnings or all("mood" not in w for w in t.warnings)
    assert "mood_annoyed" in [s.id for s in t.pick("随便", {"mood_annoyed"}, 3)]


def test_guess_fact_needs_two(cfg, brain):
    gc, ob = _gc(cfg, brain)
    m = gc.memory
    a = m.add_fact(GROUP, "小王好像喜欢打羽毛球", subject=FRIEND, source="guess", msg_id=10)
    assert m.recall(GROUP, "羽毛球")["facts"] == []                   # 只猜过一次：先不拿来用
    assert m.add_fact(GROUP, "小王好像喜欢打羽毛球！", subject=FRIEND, source="guess", msg_id=10) == a
    assert m.recall(GROUP, "羽毛球")["facts"] == []                   # 24.0 同一条消息又抽出来一次：不算第二次证据
    assert m.add_fact(GROUP, "小王好像喜欢打羽毛球！", subject=FRIEND, source="guess", msg_id=25) == a
    assert [f["id"] for f in m.recall(GROUP, "羽毛球")["facts"]] == [a]
    m.add_fact(GROUP, "小王好像喜欢打羽毛球", subject=FRIEND, source="self")
    assert m.facts(GROUP, FRIEND)[0]["source"] == "self"              # 他自己说了：升级来源
    b = m.add_fact(GROUP, "小王周末去爬山", subject=FRIEND, source="self")
    assert b in [f["id"] for f in m.recall(GROUP, "爬山")["facts"]]


def test_label_stats_live_counts(cfg, brain):
    """18.1 判一条，“该说 / 不该说”的条数马上变（小屋里的进度条不用等到第二天）。"""
    from aigroup.chat.gate import GateLog
    from aigroup.eval.label import answer, stats
    g = GateLog(brain.db)
    a = g.add(1, False, 0.4, 0.5, False, {})
    b = g.add(1, False, 0.6, 0.5, True, {})
    answer(brain.db, a, 0)
    answer(brain.db, b, 1)
    s = stats(brain.db)
    assert s["n"] == 2 and s["n0"] == 1 and s["n1"] == 1


def test_not_aliases(cfg, brain):
    """18.1 not_aliases 里的名字不算叫她（比如她的微信昵称其实是你的名字）。"""
    from aigroup.chat.group import GroupChat
    key = "wechat" if cfg.get("channel") == "wechat" else "qq"
    cfg[key] = {**dict(cfg.get(key) or {}), "not_aliases": ["star"]}
    gc = GroupChat(cfg, brain, None, 0, [1], ["star", "满满"])
    assert "star" not in gc.aliases and "满满" in gc.aliases
    assert not gc._called("star能不能请我喝奶茶") and gc._called("满满你好")
