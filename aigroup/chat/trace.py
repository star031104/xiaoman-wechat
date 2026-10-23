"""每一轮“问大模型要不要说、说什么”的记录（trace），给回放、评审、小屋的“二选一”用。

一批消息一个 trace_id：记下发给大模型的消息、大模型原文、解析出的计划、最后发出去的话、质检结果。
系统提示按哈希只存一份（qq_prompt）。回放和评审本身在 aigroup/eval/replay.py。
"""
from __future__ import annotations

import hashlib
import json
import random
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS qq_trace (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trace TEXT NOT NULL,
    ts REAL NOT NULL,
    group_id INTEGER NOT NULL,
    gate_id INTEGER,
    mentioned INTEGER NOT NULL DEFAULT 0,
    why TEXT NOT NULL DEFAULT '',
    prompt_hash TEXT NOT NULL DEFAULT '',
    messages TEXT NOT NULL DEFAULT '',      -- 发给大模型的消息（JSON）
    raw TEXT NOT NULL DEFAULT '',           -- 大模型原文
    plan TEXT NOT NULL DEFAULT '',          -- 解析出来的计划
    sent TEXT NOT NULL DEFAULT '[]',        -- 最后发出去的几条
    quality TEXT NOT NULL DEFAULT '',       -- 质检：罚分和命中的规则
    outcome TEXT NOT NULL DEFAULT ''        -- say / skip / react / blocked / parse_fail
);
CREATE INDEX IF NOT EXISTS idx_qq_trace ON qq_trace(group_id, ts);
CREATE TABLE IF NOT EXISTS qq_pairs (           -- 2.1：你在小屋里“二选一”的结果
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trace TEXT NOT NULL UNIQUE,
    a TEXT NOT NULL, b TEXT NOT NULL,       -- 两种说法（JSON 列表）
    sent INTEGER NOT NULL DEFAULT -1,       -- 质检当时选了哪个（0 = a，1 = b）
    choice INTEGER NOT NULL,                -- 你选的：0 a / 1 b / 2 差不多 / -1 都不好
    ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS qq_prompt (          -- 系统提示去重存（同一段 system 只存一份）
    hash TEXT PRIMARY KEY,
    text TEXT NOT NULL,
    last REAL
);
"""


KEEP_DAYS = 14


def prompt_text(db, h: str) -> str:
    r = db.conn.execute("SELECT text FROM qq_prompt WHERE hash=?", (h,)).fetchone()
    return r["text"] if r else ""


def trace_messages(db, row) -> list:
    """trace 里的消息还原成完整的 [system, user]。"""
    msgs = json.loads(row["messages"] or "[]")
    if msgs and msgs[0].get("role") != "system":
        sysm = prompt_text(db, row["prompt_hash"])
        if sysm:
            msgs = [{"role": "system", "content": sysm}] + msgs
    return msgs


def new_trace(gid: int) -> str:
    return f"{gid % 10000:04d}-{int(time.time() * 1000) % 10**10:010d}-{random.randint(0, 999):03d}"


class Tracer:
    def __init__(self, db):
        self.db = db
        db.conn.executescript(SCHEMA)
        from aigroup.core.store import ensure_columns
        ensure_columns(db.conn, "qq_trace", {"timing": "TEXT NOT NULL DEFAULT ''"})    # 21.1 每一段花了几秒
        db.conn.commit()

    def add(self, *, trace: str, gid: int, gate_id: int | None, mentioned: bool, why: str, messages: list,
            raw: str, plan: dict | None, sent: list, quality: str, outcome: str, timing: dict | None = None):
        """system 提示按哈希只存一份；messages 列里只存 user 那条（2.0 每条都存整段，一天能有好几 MB）。"""
        sysm = next((m["content"] for m in messages if m.get("role") == "system"), "")
        h = hashlib.md5(sysm.encode()).hexdigest()[:16]
        self.db.conn.execute("INSERT INTO qq_prompt(hash, text, last) VALUES (?,?,?) ON CONFLICT(hash) DO UPDATE SET "
                             "last=excluded.last", (h, sysm, time.time()))
        body = json.dumps([m for m in messages if m.get("role") != "system"], ensure_ascii=False)
        self.db.conn.execute(
            "INSERT INTO qq_trace(trace, ts, group_id, gate_id, mentioned, why, prompt_hash, messages, raw, plan, sent, "
            "quality, outcome, timing) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (trace, time.time(), gid, gate_id, int(mentioned), (why or "")[:200],
             h, body, (raw or "")[:2000],
             json.dumps(plan, ensure_ascii=False) if plan else "", json.dumps(sent, ensure_ascii=False),
             quality[:200], outcome, json.dumps(timing or {})))
        self.db.conn.commit()

    def prune(self, days: float = KEEP_DAYS):
        self.db.conn.execute("DELETE FROM qq_trace WHERE ts<?", (time.time() - days * 86400,))
        self.db.conn.execute("DELETE FROM qq_prompt WHERE hash NOT IN (SELECT prompt_hash FROM qq_trace)")
        self.db.conn.commit()

    def stats(self, since: float) -> dict:
        r = self.db.conn.execute(
            "SELECT COUNT(*) n, SUM(outcome='say') say, SUM(outcome='skip') skip, SUM(outcome='blocked') blocked, "
            "SUM(outcome='parse_fail') parse_fail, SUM(quality!='' AND quality NOT LIKE '0.0%') hit "
            "FROM qq_trace WHERE ts>=?", (since,)).fetchone()
        k = self.db.conn.execute("SELECT kind, COUNT(*) n FROM qq_gate_log WHERE ts>=? AND kind!='' GROUP BY kind",
                                 (since,)).fetchall()
        kinds = {x["kind"]: x["n"] for x in k}
        s2_said = self.db.conn.execute("SELECT COUNT(*) n FROM qq_gate_log WHERE ts>=? AND kind='S2' AND spoke=1",
                                       (since,)).fetchone()["n"]
        n = r["n"] or 0
        return {"n": n, "say": r["say"] or 0, "skip": r["skip"] or 0, "blocked": r["blocked"] or 0,
                "parse_fail": r["parse_fail"] or 0, "quality_hit": r["hit"] or 0,
                "quality_rate": round((r["hit"] or 0) / max(1, r["say"] or 0), 3) if n else None,
                "kinds": kinds, "s2_spoke": s2_said}
