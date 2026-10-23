"""门控：概率、阈值、1 小时配额、注意力 / 精力 / 退避、按时段、重新拟合。"""
import json
import random

from aigroup.chat.gate import (
    WEIGHTS,
    Attention,
    Backoff,
    Energy,
    cold_threshold,
    fit,
    likelihood,
    logloss,
    passes,
    talk_factor,
    threshold,
    threshold_ex,
)

from conftest import FRIEND, FRIEND2, _gc, _msg, _run


def test_threshold_and_feedback():
    assert cold_threshold(0.3) == 0.43 and cold_threshold(0.1) == 0.605 and cold_threshold(0) > 0.9
    assert abs(cold_threshold(0.2) - (0.605 + 0.43) / 2) < 1e-9
    assert threshold([], 0.3, 0)[0] == 0.43                           # 没样本：查表
    t5 = threshold([0.5] * 5, 0.3, 0)[0]                              # 样本少：查表值和分位数按 n/20 混合
    assert abs(t5 - (0.75 * 0.43 + 0.25 * 0.5)) < 1e-9
    t19, t20 = threshold([0.2] * 19, 0.3, 0)[0], threshold([0.2] * 20, 0.3, 0)[0]
    assert abs(t19 - t20) < 0.02                                      # 第 20 条时不再跳变
    probs = [i / 100 for i in range(1, 101)]                          # 0.01 … 1.00，总和 50.5
    thr, keep = threshold(probs, 0.3, spoke=0)
    assert keep == 0.3 and 0.8 < thr < 0.9                            # 最高的那些加起来到 15.15
    thr2, keep2 = threshold(probs, 0.3, spoke=30)                     # 目标 15.15 次，说了 30 次：收紧
    assert keep2 < 0.3 and thr2 > thr
    assert threshold(probs, 0.3, spoke=3)[1] == 0.3                   # 说少了：不放宽
    assert threshold(probs, 0, 0)[0] > 1                              # 话痨度 0：只回被叫到的


def test_likelihood_direction():
    base = dict(at_other=0, reply_other=0, question=0, emoji_only=0, self_share=0, n5m=3, ln_gap=7, batch=1,
                owner=0, interest=0, busy=0, attention=0, tired=0, cold=0)
    p0 = likelihood(base)
    assert likelihood({**base, "question": 1}) > p0 and likelihood({**base, "attention": 1}) > p0
    assert likelihood({**base, "at_other": 1}) < p0 and likelihood({**base, "busy": 1}) < p0
    assert likelihood({**base, "emoji_only": 1}) < p0 and likelihood({**base, "tired": 0.5}) < p0


def test_attention_energy_backoff():
    a, now = Attention(half=300), 1000.0
    a.talked_to(FRIEND, [FRIEND, FRIEND2], now)
    assert abs(a.get(FRIEND, now) - 0.4) < 1e-9 and a.get(FRIEND2, now) == 0
    assert abs(a.get(FRIEND, now + 300) - 0.2) < 1e-9                  # 半衰期
    a.talked_to(FRIEND2, [FRIEND, FRIEND2], now)
    assert abs(a.get(FRIEND, now) - 0.3) < 1e-9 and a.top(now)[0][0] == FRIEND2
    e = Energy()
    e.ts = now
    e.spend(now)
    e.spend(now)
    assert abs(e.get(now) - 0.8) < 1e-9 and abs(e.get(now + 1500) - 0.9) < 1e-9
    b = Backoff()
    b.skipped(now)
    assert b.waiting(now) == 0
    b.skipped(now)
    assert b.waiting(now) == 15
    b.skipped(now)
    b.skipped(now)
    assert b.waiting(now) == 60
    for _ in range(10):
        b.skipped(now)
    assert b.waiting(now) == 300
    b.spoke()
    assert b.waiting(now) == 0


def test_gate_logs_and_quota(cfg, brain):
    gc, ob = _gc(cfg, brain, talkativeness=0)
    _run(gc, _msg(FRIEND, "小王", "今天好热啊", 1))
    assert not ob.sent
    row = brain.db.conn.execute("SELECT * FROM qq_gate_log").fetchone()
    assert row["passed"] == 0 and row["threshold"] > 1 and "ln_gap" in json.loads(row["feats"])
    gc.g["talkativeness"] = 1.0                                       # 什么都想说
    _run(gc, _msg(FRIEND, "小王", "有没有人去吃火锅？", 2))
    assert ob.sent
    last = brain.db.conn.execute("SELECT * FROM qq_gate_log ORDER BY id DESC").fetchone()
    assert last["passed"] == 1 and last["spoke"] == 1


def test_ties_and_curve():
    thr, keep, tie = threshold_ex([0.39] * 25, 0.3, 0)
    assert abs(thr - 0.39) < 1e-9 and 0.25 < tie < 0.35                 # 以前全放行
    n = sum(passes(0.39, thr, tie, random.Random(i).random()) for i in range(1000))
    assert 200 < n < 400
    assert talk_factor({"09:00-11:59": 0.8, "22:00-01:00": 1.3}, "10:30") == 0.8
    assert talk_factor({"22:00-01:00": 1.3}, "00:30") == 1.3 and talk_factor({}, "10:00") == 1.0


def test_refit_moves_toward_data():
    base = dict(at_other=0, reply_other=0, question=0, emoji_only=0, self_share=0, n5m=3, ln_gap=7, batch=1,
                owner=0, interest=0, busy=0, attention=0, tired=0, cold=0, ignored=0, waited=0)
    rows = [({**base, "question": 1}, 1.0, 1.0)] * 60 + [({**base, "question": 0}, 0.0, 1.0)] * 60
    w = fit(rows, steps=300)
    assert w["question"] > WEIGHTS["question"]
    assert likelihood({**base, "question": 1}, w) > likelihood({**base, "question": 1})


def test_fit_converges_on_realistic_features():
    r = random.Random(0)
    rows = []
    for _ in range(600):
        f = {k: 0.0 for k in WEIGHTS if k != "bias"}
        f.update(question=float(r.random() < .3), n5m=float(r.randint(0, 40)), ln_gap=r.uniform(3, 11),
                 batch=float(r.randint(1, 6)), attention=r.random(), self_share=r.random() * .5)
        rows.append((f, 1.0 if r.random() < (0.6 if f["question"] else 0.2) else 0.0, 1.0))
    a, b = fit(rows, steps=300), fit(rows, steps=320)
    ma = sum(likelihood(f, a) for f, _, _ in rows) / len(rows)
    mb = sum(likelihood(f, b) for f, _, _ in rows) / len(rows)
    assert 0.2 < ma < 0.5 and abs(ma - mb) < 0.01                      # 2.0 这里在 0.0001 和 0.93 之间跳
    assert a["question"] > WEIGHTS["question"]
    assert logloss(rows, a) < logloss(rows, WEIGHTS)
