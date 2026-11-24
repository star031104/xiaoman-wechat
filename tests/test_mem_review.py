"""38.1 记忆复核：导出、操作单校验、写入、留痕、退回；白天的群笔记不盖掉复核过的；重要度影响淡忘。"""
import json
import time

from aigroup.memory import review as R

from conftest import FRIEND, FRIEND2, GROUP, SELF, _gc


def _setup(cfg, brain):
    gc, ob = _gc(cfg, brain)
    m1 = gc._record(GROUP, FRIEND, "小王", "我下个月21号考研，408 真的难")
    m2 = gc._record(GROUP, FRIEND2, "老李", "小王天天熬夜复习")
    m3 = gc._record(GROUP, SELF, "小满", "我最喜欢喝冰美式", role="assistant")
    m4 = gc._record(GROUP, FRIEND, "小王", "我现在在腾讯实习")
    return gc, (m1, m2, m3, m4)


def _apply(gc, sheet, dry=False):
    ap = R.Applier(gc.db, gc.memory, GROUP, gc.cfg.tz, dry=dry)
    return ap.run({"gid": GROUP, **sheet})


def test_export_has_messages_facts_and_text(cfg, brain):
    gc, (m1, m2, m3, m4) = _setup(cfg, brain)
    gc.memory.add_fact(GROUP, "小王在准备考研", subject=FRIEND, source="self", msg_id=m1)
    d = R.export(gc.db, GROUP, gc.cfg.tz)
    assert [m[0] for m in d["messages"]] == [m1, m2, m3, m4]
    assert d["messages"][2][2] == "她" and d["messages"][0][2] == FRIEND
    assert d["facts"][0]["review"] == "" and d["facts"][0]["text"] == "小王在准备考研"
    assert str(FRIEND) in d["members"] and d["to_id"] == m4 and d["more"] is False
    txt = R.render_text(d)
    assert "考研" in txt and "现在记着的事" in txt


def test_add_needs_verbatim_quote_and_right_author(cfg, brain):
    gc, (m1, m2, m3, m4) = _setup(cfg, brain)
    res = _apply(gc, {"to_id": m4, "facts": [
        {"op": "add", "about": FRIEND, "kind": "fact", "source": "self", "importance": 3, "attr": "exam",
         "text": "在备考研究生 408，12 月 21 号考", "msg": m1, "quote": "下个月21号考研", "when": "2026-12-21"},
        {"op": "add", "about": FRIEND, "source": "self", "importance": 2, "text": "天天熬夜复习", "msg": m2, "quote": "天天熬夜复习"},
        {"op": "add", "about": FRIEND, "source": "other", "importance": 2, "text": "天天熬夜复习考研", "msg": m2, "quote": "天天熬夜"},
        {"op": "add", "about": FRIEND, "source": "self", "importance": 2, "text": "在腾讯实习", "msg": m4, "quote": "在字节实习"},
        {"op": "add", "about": -1, "kind": "experience", "importance": 2, "text": "最喜欢喝冰美式", "msg": m3, "quote": "最喜欢喝冰美式"},
        {"op": "add", "about": -1, "importance": 2, "text": "最喜欢考研", "msg": m1, "quote": "考研"},
        {"op": "add", "about": FRIEND, "source": "self", "importance": 2, "text": "家住在幸福小区附近", "msg": m4, "quote": "腾讯"},
        {"op": "add", "about": 12345, "source": "self", "importance": 2, "text": "某人的事情", "msg": m4, "quote": "腾讯"},
    ]})
    assert res["ok"] == 3 and res["bad"] == 5
    whys = " ".join(r["why"] for r in res["rejected"])
    assert "不是他说的" in whys and "找不到" in whys and "她自己的事" in whys and "隐私" in whys and "认不出" in whys
    f = {r["text"]: dict(r) for r in gc.db.conn.execute("SELECT * FROM qq_facts")}
    exam = f["在备考研究生 408，12 月 21 号考"]
    assert exam["review"] == "claude" and exam["importance"] == 3 and exam["attr"] == "exam" and exam["t_event"]
    assert exam["msg_id"] == m1 and exam["quote"] == "下个月21号考研"
    assert f["天天熬夜复习考研"]["speaker_id"] == FRIEND2 and f["天天熬夜复习考研"]["source"] == "other"
    assert f["最喜欢喝冰美式"]["subject_id"] == -1 and f["最喜欢喝冰美式"]["kind"] == "experience"
    assert int(gc.db.get(R.MARK.format(GROUP))) == m4
    assert R.export(gc.db, GROUP, gc.cfg.tz)["messages"] == []           # 下次从后面开始


def test_dry_run_writes_nothing(cfg, brain):
    gc, (m1, *_r) = _setup(cfg, brain)
    res = _apply(gc, {"to_id": m1, "facts": [{"op": "add", "about": FRIEND, "source": "self", "importance": 3,
                                             "text": "在备考研究生", "msg": m1, "quote": "考研"}]}, dry=True)
    assert res["ok"] == 1
    assert gc.db.conn.execute("SELECT COUNT(*) FROM qq_facts").fetchone()[0] == 0
    assert not gc.db.get(R.MARK.format(GROUP))


def test_attr_replaces_old_and_keep_drop_forget_undo(cfg, brain):
    gc, (m1, m2, m3, m4) = _setup(cfg, brain)
    old = gc.memory.add_fact(GROUP, "小王在阿里实习", subject=FRIEND, source="self", msg_id=m1)
    gc.db.conn.execute("UPDATE qq_facts SET attr='intern' WHERE id=?", (old,))
    joke = gc.memory.add_fact(GROUP, "小王是外星人派来的", subject=FRIEND, source="guess", msg_id=m2)
    small = gc.memory.add_fact(GROUP, "小王今天有点困", subject=FRIEND, source="self", msg_id=m1)
    keep = gc.memory.add_fact(GROUP, "小王考研考 408", subject=FRIEND, source="self", msg_id=m1)
    res = _apply(gc, {"to_id": m4, "facts": [
        {"op": "add", "about": FRIEND, "source": "self", "importance": 3, "attr": "intern", "text": "在腾讯实习",
         "msg": m4, "quote": "在腾讯实习"},
        {"op": "drop", "id": joke, "why": "玩笑", "forget": True},
        {"op": "drop", "id": small, "why": "琐碎"},
        {"op": "drop", "id": keep, "why": "随便"},
        {"op": "keep", "id": keep, "importance": 3, "text": "在考研，专业课考 408"},
    ]})
    assert res["ok"] == 4 and res["bad"] == 1
    row = lambda i: gc.db.conn.execute("SELECT * FROM qq_facts WHERE id=?", (i,)).fetchone()   # noqa: E731
    assert row(old)["t_invalid"] and row(old)["replaced_by"]                        # 同一个 attr：旧的实习被替掉
    assert row(joke) is None                                                        # 玩笑：删了、留指纹
    assert gc.memory.add_fact(GROUP, "小王是外星人派来的", subject=FRIEND, source="guess") is None
    assert row(small)["review"] == "drop" and row(small)["t_invalid"]
    assert row(keep)["text"] == "在考研，专业课考 408" and row(keep)["importance"] == 3 and row(keep)["review"] == "ok"
    n = R.undo(gc.db, res["run"])
    assert n == 4
    assert row(old)["t_invalid"] is None and row(joke)["text"] == "小王是外星人派来的"
    assert row(small)["review"] == "" and row(keep)["text"] == "小王考研考 408"
    assert gc.db.conn.execute("SELECT COUNT(*) FROM qq_facts WHERE text='在腾讯实习'").fetchone()[0] == 0
    assert gc.memory.add_fact(GROUP, "小王是外星人派来的", subject=FRIEND, source="guess")    # 指纹也退了


def test_notes_daytime_goes_to_new_and_review_replaces(cfg, brain):
    gc, (m1, *_r) = _setup(cfg, brain)
    gc.db.set(f"qq_group_notes_{GROUP}", "- （11-20）周六约了唱 K")
    R.add_new_notes(gc.db, "group_notes", GROUP, ["小王在备考", "周六约了唱 K"])
    txt = R.notes_text(gc.db, "group_notes", GROUP)
    assert "周六约了唱 K" in txt and "小王在备考" in txt and txt.count("唱 K") == 1 and "还没细想过" in txt
    res = _apply(gc, {"group_notes": ["（11-24）小王在备考，12-21 考完", "（11-20）周六约了唱 K"],
                      "profiles": {str(FRIEND): {"近况": "备考 408（11-24）", "印象": "嘴硬的考研人"}}})
    assert res["ok"] == 2
    assert gc.db.get(R.new_notes_key("group_notes", GROUP)) is None
    assert "12-21" in R.notes_text(gc.db, "group_notes", GROUP)
    p = json.loads(gc.db.conn.execute("SELECT profile FROM qq_profiles WHERE user_id=? AND group_id=?",
                                      (FRIEND, GROUP)).fetchone()["profile"])
    assert p["近况"] == "备考 408（11-24）"
    bad = _apply(gc, {"profiles": {str(FRIEND): {"住址": "x"}}})
    assert bad["bad"] == 1


def test_digest_in_review_mode_keeps_reviewed_notes(cfg, brain):
    from conftest import FakeLLM

    class L(FakeLLM):
        async def chat(self, messages, role="chat", **kw):
            if role == "memory":
                return "群|小王在备考\n规|不刷屏\n我|喜欢冰美式"
            return await super().chat(messages, role=role, **kw)
    brain.llm = L()
    gc, _ = _gc(cfg, brain)
    for i in range(3):
        gc._record(GROUP, FRIEND, "小王", f"第{i}句话")
    gc.db.set(f"qq_group_notes_{GROUP}", "- （11-20）周六约了唱 K")
    import asyncio
    asyncio.run(gc._digest_one(GROUP))
    assert gc.db.get(f"qq_group_notes_{GROUP}") == "- （11-20）周六约了唱 K"
    assert any("小王在备考" in x for x in gc.db.get(R.new_notes_key("group_notes", GROUP)))
    assert "喜欢冰美式" in gc.self_notes(GROUP)


def test_importance_changes_forgetting_and_upcoming_first(cfg, brain):
    gc, _ = _gc(cfg, brain)
    m = gc.memory
    now = time.time()
    old = now - 20 * 86400
    assert m.strength(old, now=now, importance=1) < 0.2 < m.strength(old, now=now, importance=2) < m.strength(old, now=now, importance=3)
    a = {"created": old, "importance": 2, "t_event": None, "evidence": 1, "hits": 0, "last_said": None}
    b = dict(a, t_event=now + 2 * 86400)
    assert m._prio(b, now) > m._prio(a, now)


def test_status_counts(cfg, brain):
    gc, (m1, *_r) = _setup(cfg, brain)
    gc.memory.add_fact(GROUP, "小王在准备考研", subject=FRIEND, source="self", msg_id=m1)
    st = R.status(gc.db, GROUP, gc.cfg.tz)
    assert st["mark"] == 0 and st["left"] == 4 and st["facts"] == {"临时": 1}
