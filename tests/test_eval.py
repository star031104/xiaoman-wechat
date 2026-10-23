"""评估工具：回放、导出脱敏包、二选一、评审、质检误伤统计、trace。"""
import asyncio
import json
import random
import time

from conftest import (
    FRIEND,
    FRIEND2,
    GROUP,
    FakeLLM,
    J,
    _gc,
)


def test_ignored_daily_and_replay(cfg, brain):
    gc, ob = _gc(cfg, brain)
    now = time.time()
    for i in range(2):
        brain.db.conn.execute("INSERT INTO qq_reply_effect(ts, group_id, mentioned, done, score) VALUES (?,?,0,1,0)",
                              (now - 600 + i, GROUP))
    brain.db.conn.commit()
    assert gc.ignored_count(GROUP) == 2
    m = asyncio.run(gc.build_messages(GROUP, False, False, ""))
    assert "都没人接" in m[1]["content"] and "都没人接" not in m[0]["content"]
    day = brain.now()
    for i in range(12):
        gc._record(GROUP, FRIEND if i % 2 else FRIEND2, "小王" if i % 2 else "阿杰", f"今天聊点什么 {i}", 300 + i)
    rep = asyncio.run(gc.daily_report(GROUP, day))
    assert rep["msgs"] == 12 and rep["people"] == 2 and rep["top"]
    from aigroup.eval.replay import replay_gate
    brain.db.conn.execute("INSERT INTO qq_gate_log(ts, group_id, mentioned, p, threshold, passed, feats) VALUES (?,?,0,0.5,0.4,1,?)",
                          (now - 60, GROUP, json.dumps({"question": 1, "ln_gap": 7})))
    brain.db.conn.commit()
    r = replay_gate(brain.db, gc.g, 7)
    assert r["rows"] == 1 and r["same"] + r["new_pass"] + r["new_block"] == 1


def test_anonymize_and_export(cfg, brain):
    from aigroup.eval.replay import anonymize, export
    m = {}
    out = anonymize("QQ号12345678的小王说群777777", m, {"小王": "12345678"})
    assert "12345678" not in out and "777777" not in out and "小王" not in out
    gc, ob = _gc(cfg, brain, talkativeness=1.0, cooldown_seconds=0)
    gc._record(GROUP, FRIEND, "小王", "在吗", 1)
    asyncio.run(gc.speak(GROUP, mentioned=True, owner_spoke=False, trigger=1, who="小王"))
    p = export(brain.db, cfg["root"])
    line = json.loads(p.read_text(encoding="utf-8").splitlines()[0])
    assert "raw" not in line and "system" not in line and str(GROUP) not in json.dumps(line)


def test_pairs_calibrate_and_judge(cfg, brain):
    from aigroup.eval.replay import answer_pair, calibrate, judge, next_pair, pair_stats
    gc, ob = _gc(cfg, brain, talkativeness=1.0, cooldown_seconds=0)
    gc._record(GROUP, FRIEND, "小王", "推荐个番", 1)
    brain.llm.reply = J(act="say", say=["芙莉莲看就完事"], alt=[["芙莉莲！"]])
    asyncio.run(gc.speak(GROUP, mentioned=False, owner_spoke=False, trigger=None, who=""))
    p = next_pair(brain.db, GROUP, random.Random(1))
    assert p and len(p["context"]) >= 1 and {p["a"][0], p["b"][0]} == {"芙莉莲看就完事", "芙莉莲！"}
    answer_pair(brain.db, p["trace"], p["a"], p["b"], p["sent"], p["sent"])
    assert pair_stats(brain.db)["agree_rate"] == 1.0 and next_pair(brain.db, GROUP) is None
    c = calibrate(brain.db)
    assert c["n"] >= 1 and c["hard"] == 0

    class JudgeLLM(FakeLLM):
        async def json(self, messages, default=None, role="helper"):
            return {"defects": [{"type": "太长", "severity": 2, "why": "x"}]}
    res = asyncio.run(judge(brain.db, JudgeLLM(), brain.persona, 5))
    assert res["n"] == 1 and res["avg"] == 90 and res["kinds"] == {"太长": 1}


def test_trace_dedupes_system_prompt(cfg, brain):
    gc, ob = _gc(cfg, brain, talkativeness=1.0, cooldown_seconds=0)
    gc._record(GROUP, FRIEND, "小王", "在吗", 1)
    for _ in range(3):
        asyncio.run(gc.speak(GROUP, mentioned=True, owner_spoke=False, trigger=1, who="小王"))
    assert brain.db.conn.execute("SELECT COUNT(*) n FROM qq_prompt").fetchone()["n"] <= 2
    row = brain.db.conn.execute("SELECT messages FROM qq_trace").fetchone()
    assert all(m["role"] != "system" for m in json.loads(row["messages"]))
