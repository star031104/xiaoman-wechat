"""21.1 一周一张表：每个问题有没有变好，分开看，不算总分（深度研究评测的经验：总分会把坏掉的那一环盖住）。

| 维度 | 指标 | 目标 |
| 时机 | 你判“时机不对”里她说了的比例（fir）；你判“该接”里她没说的比例（mir） | fir ≤ 20%、mir ≤ 20% |
| 听者 | 和你的一致率 | ≥ 0.7 |
| 效果 | 主动说的被接率；被叫到到她第一句的中位 / 90% 分位秒数 | ≥ 65%；≤ 12 / ≤ 45 |
| 像人 | 她 / 群友一条长度的中位比；连发 ≥ 3 条的次数；同一接法连用 ≥ 3 次 | ≤ 1.8；少；少 |
| 份额 | 她的消息份额 / 活跃群友平均份额 | ≤ 1.0（目标 share_target） |
| 人格 | 一天里前 / 后 1/3 时段：平均长度、助手腔命中率 | 后段不比前段差 |
| 研究 | 自己停下的比例（证实了 / 自己交卷）；对不上的来源 | ≥ 70%；0 |
| 规则 | 命中最多的几条、建议关掉的 | — |
| 底线 | 编线下 / 说幕后 / 对方难受时损他的命中 | 0 |
"""
from __future__ import annotations

import json
import re
import statistics
import time
from datetime import datetime

ASSIST = re.compile(r"希望(这|以上|对你)|有(什么|任何)(问题|需要)|很高兴(为你|帮你)|如果你(需要|愿意)的话|此外|总而言之|综上所述|值得注意的是|"
                    r"我(理解|明白)你的(感受|心情)")


def _pct(a: int, b: int):
    return round(a / b, 3) if b else None


def timing_rates(db, gid: int) -> dict:
    rows = db.conn.execute("SELECT spoke, human, IFNULL(human_why,'') hw FROM qq_gate_log WHERE group_id=? AND human IN (0,1)",
                           (gid,)).fetchall()
    t0 = [r for r in rows if r["human"] == 0 and r["hw"] != "说的不对"]
    t1 = [r for r in rows if r["human"] == 1]
    return {"fir": _pct(sum(1 for r in t0 if r["spoke"]), len(t0)), "n0": len(t0),
            "mir": _pct(sum(1 for r in t1 if not r["spoke"]), len(t1)), "n1": len(t1),
            "content_wrong": sum(1 for r in rows if r["human"] == 0 and r["hw"] == "说的不对")}


def mention_latency(db, gid: int, since: float) -> dict:
    rows = db.conn.execute("SELECT ts, role, to_her FROM qq_group_messages WHERE group_id=? AND ts>? ORDER BY id",
                           (gid, since)).fetchall()
    lat, pend = [], None
    for r in rows:
        if r["role"] == "user" and r["to_her"] and pend is None:
            pend = float(r["ts"])
        elif r["role"] == "assistant" and pend is not None:
            lat.append(float(r["ts"]) - pend)
            pend = None
    lat = sorted(x for x in lat if x < 3600)
    if not lat:
        return {"n": 0}
    return {"n": len(lat), "p50": round(statistics.median(lat)), "p90": round(lat[min(len(lat) - 1, int(len(lat) * 0.9))])}


def her_style(db, gid: int, since: float, tz=None) -> dict:
    """长度比、连发、按一天前 / 中 / 后三段的长度和助手腔。"""
    rows = db.conn.execute("SELECT ts, role, content FROM qq_group_messages WHERE group_id=? AND ts>? ORDER BY id",
                           (gid, since)).fetchall()

    def clean(t):
        return re.sub(r"\[[^\]]*\]|https?://\S+", "", str(t or "")).strip()
    her = [clean(r["content"]) for r in rows if r["role"] == "assistant" and clean(r["content"])]
    ppl = [clean(r["content"]) for r in rows if r["role"] == "user" and clean(r["content"])]
    out: dict = {}
    if her and ppl:
        hm, pm = statistics.median(len(x) for x in her), statistics.median(len(x) for x in ppl)
        out.update(her_len=hm, people_len=pm, len_ratio=round(hm / max(1, pm), 2))
    runs, k = [], 0
    for r in rows:
        if r["role"] == "assistant":
            k += 1
        else:
            if k:
                runs.append(k)
            k = 0
    out["runs3"] = sum(1 for x in runs if x >= 3)
    segs: dict = {0: [], 1: [], 2: []}
    by_day: dict = {}
    for r in rows:
        if r["role"] == "assistant" and clean(r["content"]):
            d = datetime.fromtimestamp(float(r["ts"]), tz).strftime("%Y-%m-%d")
            by_day.setdefault(d, []).append(clean(r["content"]))
    for lines in by_day.values():
        n = len(lines)
        for i, t in enumerate(lines):
            segs[min(2, i * 3 // max(1, n))].append(t)
    out["segments"] = [{"n": len(v), "len": round(statistics.mean(len(x) for x in v), 1) if v else None,
                        "assist": _pct(sum(1 for x in v if ASSIST.search(x)), len(v))} for v in segs.values()]
    return out


def moves_runs(db, gid: int) -> int:
    log_ = db.get(f"qq_moves_{gid}") or []
    n, prev, k = 0, None, 0
    for x in log_:
        k = k + 1 if x.get("move") == prev else 1
        prev = x.get("move")
        n += k == 3
    return n


def research_stats(db, since: float) -> dict:
    log_ = [x for x in (db.get("agent_log") or []) if float(x.get("ts") or 0) > since and x.get("goal")]
    stops: dict = {}
    for x in log_:
        s = x.get("stop") or ("deadline" if x.get("forced") else "model")
        stops[s] = stops.get(s, 0) + 1
    n = len(log_)
    return {"n": n, "stops": stops, "self_stop": _pct(stops.get("covered", 0) + stops.get("model", 0), n),
            "bad_cites": sum(int(x.get("bad_cites") or 0) for x in log_),
            "p50": round(statistics.median(float(x.get("seconds") or 0) for x in log_), 1) if log_ else None}


def weekly_vector(gc, gid: int, days: float = 7) -> dict:
    db = gc.db
    since = time.time() - days * 86400
    eff = db.conn.execute("SELECT mentioned, COUNT(*) n, SUM(score>=0.6) good FROM qq_reply_effect WHERE group_id=? AND ts>? "
                          "AND done=1 GROUP BY mentioned", (gid, since)).fetchall()
    effd = {int(r["mentioned"]): (int(r["n"]), int(r["good"] or 0)) for r in eff}
    from aigroup.chat.rules import known_rules, report
    rules = report(db, known_rules())
    hard = {r["rule"]: r["n"] for r in rules if r["rule"] in ("说了幕后的事", "编了线下的事", "他这会儿状态不好")}
    return {
        "ts": time.time(), "gid": gid, "days": days,
        "timing": timing_rates(db, gid),
        "listener": gc.listener_stats(gid) if hasattr(gc, "listener_stats") else {},
        "effect": {"unprompted": _pct(effd.get(0, (0, 0))[1], effd.get(0, (0, 0))[0]), "n": effd.get(0, (0, 0))[0],
                   "mention_latency": mention_latency(db, gid, since)},
        "style": {**her_style(db, gid, since, getattr(gc.cfg, "tz", None)), "move_runs3": moves_runs(db, gid)},
        "share": gc.share_info(gid, days) if hasattr(gc, "share_info") else {},
        "research": research_stats(db, since),
        "rules": [r for r in rules if r["n"] or r["advice"]][:12],
        "hard": hard,
        "pitch": gc.pitch_stats(gid, days) if hasattr(gc, "pitch_stats") else {},
    }


def save_snapshot(db, vec: dict):
    hist = list(db.get("vector_hist") or [])[-25:]
    hist.append(json.loads(json.dumps(vec, ensure_ascii=False, default=str)))
    db.set("vector_hist", hist)
