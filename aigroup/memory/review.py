"""38.1 记忆复核：白天 Flash 先记（算“临时”），每天 18 点由 Claude 复核一次，复核过的才算数。

一次复核（scripts/mem_review.py）：
  export  上次复核到的那条消息之后的聊天 + 现在记着的事 / 画像 / 群笔记 → 一个 JSON
          （Claude 按 docs/记忆复核.md 判，写一张“操作单”，这一步在 Claude 那边，这里不调任何模型）
  apply   逐条校验操作单 → 先备份 → 一条条写入 → 每条记留痕（改之前的样子）→ 推进“复核到哪”
  undo    按留痕把某一次复核整个退回

操作单里每条事都要带原话（消息编号 + 逐字引文），对不上的一律不收；写库只走这里，Claude 不直接碰数据库。
事实表多了几列：review（'' 临时 / ok 复核过 / claude 复核时新记的 / drop 复核时删的）、importance（1 琐碎 2 一般 3 重要）、
attr（单值属性的名字：同一个人同一个 attr 新的一条自动替掉旧的）、t_event（这件事发生 / 截止的那天）、quote（原话）。
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime

from aigroup.core.store import ensure_columns

SCHEMA = """
CREATE TABLE IF NOT EXISTS qq_mem_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id INTEGER NOT NULL,
    ts REAL NOT NULL,
    from_id INTEGER, to_id INTEGER,
    n_ok INTEGER NOT NULL DEFAULT 0, n_bad INTEGER NOT NULL DEFAULT 0,
    note TEXT NOT NULL DEFAULT '',
    undone INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS qq_mem_ops (          -- 复核的留痕：每一条操作、成没成、改之前 / 之后的样子
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    op TEXT NOT NULL,
    ok INTEGER NOT NULL,
    why TEXT NOT NULL DEFAULT '',
    target TEXT NOT NULL DEFAULT '',
    before TEXT,
    after TEXT,
    ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_qq_mem_ops ON qq_mem_ops(run_id);
"""
FACT_COLS = {
    "review": "TEXT NOT NULL DEFAULT ''",
    "importance": "INTEGER NOT NULL DEFAULT 2",
    "attr": "TEXT NOT NULL DEFAULT ''",
    "t_event": "REAL",
    "quote": "TEXT NOT NULL DEFAULT ''",
    "reviewed_at": "REAL",
}
KINDS = {"fact": "fact", "pref": "fact", "event": "event", "plan": "plan", "experience": "experience", "opinion": "opinion"}
SOURCES = {"self", "other", "guess", "said"}
NOTE_KEYS = {"group_notes": "qq_group_notes_", "norms": "qq_group_norms_", "self_notes": "qq_self_notes_"}
NOTE_CAPS = {"group_notes": (10, 60), "norms": (6, 50), "self_notes": (12, 60)}
PROFILE_KEYS = ("印象", "称呼", "性格", "说话风格", "口头禅", "兴趣", "看法", "近况", "群里关系", "和你", "怎么聊", "他的反应",
                "在意", "别碰", "记得的事")
MARK = "mem_review_to_{}"
DROP_WHY = {"玩笑", "琐碎", "记错", "重复", "过期", "隐私", "不是他的事", "被取代"}


SCORE_COLS = {"src": "TEXT NOT NULL DEFAULT ''", "tags": "TEXT NOT NULL DEFAULT ''", "better": "TEXT NOT NULL DEFAULT ''",
              "pick": "TEXT NOT NULL DEFAULT ''", "msg_id": "INTEGER"}
# 38.3 给她的话打分时用的毛病标签（docs/记忆复核.md 有说明）；和挑句子的 4 条原则对得上
SCORE_TAGS = ("评人不接话", "自夸", "助手腔", "交报告", "太长", "连发", "读错人", "读错场面", "编", "硬接", "复读",
              "该说没说", "说教", "伤人", "答非所问", "八股", "好")
# 38.4 标了这些的话（≤2 分），之后拼聊天记录时不原样给她看（chat/prompt.self_clean），免得她跟着自己学
TONE_TAGS = ("八股", "助手腔", "交报告", "说教")


def ensure_schema(conn):
    conn.executescript(SCHEMA)
    ensure_columns(conn, "qq_facts", FACT_COLS)
    from aigroup.eval.calib import SCHEMA as SCORE_SCHEMA
    conn.executescript(SCORE_SCHEMA)
    ensure_columns(conn, "qq_human_score", SCORE_COLS)
    conn.commit()


def her_turns(db, gid: int, rows: list) -> list[dict]:
    """38.3 这批消息里她说的每一句 + 当时写了哪几句、各扣多少分、选了哪句（qq_trace.quality，38.3 以后才有）。"""
    out = []
    for r in rows:
        if r["role"] != "assistant":
            continue
        ts = float(r["ts"])
        tr = None
        for t in db.conn.execute("SELECT trace, sent, quality FROM qq_trace WHERE group_id=? AND ts BETWEEN ? AND ? "
                                 "AND outcome='say' ORDER BY ts DESC", (gid, ts - 180, ts + 5)).fetchall():
            try:
                sent = json.loads(t["sent"] or "[]")
            except ValueError:
                sent = []
            if any(plain(x) and plain(x) in plain(r["content"]) or plain(r["content"]) in plain(x) for x in sent):
                tr = t
                break
        pick = {}
        if tr is not None:
            try:
                q = json.loads(tr["quality"] or "{}")
                if isinstance(q, dict) and q.get("cands"):
                    pick = {"chosen": q.get("chosen"),
                            "cands": [{"k": c.get("k"), "text": str(c.get("text") or "")[:80],
                                       "total": c.get("total"), "hard": bool(c.get("hard"))} for c in q["cands"]]}
            except ValueError:
                pick = {}
        out.append({"msg": int(r["id"]), "trace": tr["trace"] if tr is not None else "", "text": str(r["content"] or ""),
                    **({"pick": pick} if pick else {})})
    return out


def plain(t: str) -> str:
    return re.sub(r"[\s，,。.！!？?…~～、：:；;“”\"'‘’（）()【】\[\]《》<>‹›]", "", str(t or "")).lower()


def _day(s, tz, end: bool = False) -> float | None:
    s = str(s or "").strip()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", s):
        return None
    d = datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=tz)
    if end:
        d = d.replace(hour=23, minute=59)
    return d.timestamp()


def _md(ts, tz) -> str:
    return datetime.fromtimestamp(float(ts), tz).strftime("%Y-%m-%d") if ts else ""


def new_notes_key(kind: str, gid: int) -> str:
    """Flash 白天记的新笔记（还没复核）放这里，不再直接盖掉复核过的那份。"""
    return f"{NOTE_KEYS[kind]}new_{gid}"


def add_new_notes(db, kind: str, gid: int, lines: list[str], keep: int = 8):
    """白天的整理把新的一两行加进“还没复核”的那份（去重、带日期、最多 keep 行）。"""
    old = list(db.get(new_notes_key(kind, gid)) or [])
    have = {_core(x) for x in old}
    cur = {_core(x) for x in str(db.get(f"{NOTE_KEYS[kind]}{gid}") or "").splitlines()}
    day = datetime.now().strftime("%m-%d")
    for x in lines:
        x = str(x or "").strip().lstrip("-").strip()
        if not x or _core(x) in have or _core(x) in cur:
            continue
        old.append(f"（{day}）{x}")
        have.add(_core(x))
    db.set(new_notes_key(kind, gid), old[-keep:] or None)


def _core(line: str) -> str:
    """笔记一行去掉“- ”和开头的“（11-20）”，用来判重。"""
    x = re.sub(r"^\s*-\s*", "", str(line or ""))
    x = re.sub(r"^[（(]\d{1,2}-\d{1,2}[)）]", "", x)
    return plain(x)


def notes_text(db, kind: str, gid: int) -> str:
    """提示词里用的：复核过的那份 + 这两天新的（还没复核）。"""
    cur = str(db.get(f"{NOTE_KEYS[kind]}{gid}") or "").strip()
    new = [x for x in (db.get(new_notes_key(kind, gid)) or []) if str(x).strip()]
    if not new:
        return cur
    tail = "\n".join(f"- {x}" for x in new)
    return (cur + "\n" if cur else "") + "（这两天新记的，还没细想过）\n" + tail


# ======================= 导出 =======================
def members(db, gid: int, self_id: int | None = None) -> dict:
    out = {}
    for r in db.conn.execute("SELECT p.user_id, q.name, q.aliases FROM qq_profiles p JOIN qq_people q ON q.user_id=p.user_id "
                             "WHERE p.group_id=?", (gid,)).fetchall():
        uid = int(r["user_id"])
        if self_id and uid == self_id:
            continue
        al = [a for a in re.split(r"[、,，/]", str(r["aliases"] or "")) if a.strip()]
        out[uid] = {"name": r["name"], "aliases": al}
    return out


def export(db, gid: int, tz, *, from_id: int | None = None, to_id: int | None = None, max_msgs: int = 1500,
           ctx: int = 30, self_id: int | None = None, her: str = "她") -> dict:
    ensure_schema(db.conn)
    mark = int(db.get(MARK.format(gid)) or 0)
    lo = int(from_id) if from_id is not None else mark + 1
    hi = int(to_id) if to_id else 10 ** 12
    rows = db.conn.execute("SELECT id, ts, user_id, name, role, content FROM qq_group_messages WHERE group_id=? AND id>=? "
                           "AND id<=? ORDER BY id LIMIT ?", (gid, lo, hi, max_msgs)).fetchall()
    before = list(reversed(db.conn.execute(
        "SELECT id, ts, user_id, name, role, content FROM qq_group_messages WHERE group_id=? AND id<? ORDER BY id DESC LIMIT ?",
        (gid, lo, ctx)).fetchall())) if ctx else []

    def m(r, mark_ctx=False):
        who = her if r["role"] == "assistant" else int(r["user_id"] or 0)
        x = [int(r["id"]), datetime.fromtimestamp(float(r["ts"]), tz).strftime("%m-%d %H:%M"), who, str(r["content"] or "")]
        if mark_ctx:
            x.append("上文")
        return x
    now = time.time()
    facts = []
    for f in db.conn.execute("SELECT * FROM qq_facts WHERE group_id=? AND (t_invalid IS NULL OR t_invalid>?) ORDER BY id",
                             (gid, now)).fetchall():
        f = dict(f)
        facts.append({"id": f["id"], "about": f["subject_id"], "kind": f["kind"], "source": f["source"],
                      "review": f.get("review") or "", "importance": f.get("importance") or 2, "attr": f.get("attr") or "",
                      "said": _md(f.get("last_said") or f["t_valid"], tz), "until": _md(f["t_invalid"], tz),
                      "when": _md(f.get("t_event"), tz), "msg": f["msg_id"], "evidence": f.get("evidence") or 1,
                      "text": f["text"]})
    profs = {}
    for r in db.conn.execute("SELECT user_id, profile FROM qq_profiles WHERE group_id=?", (gid,)).fetchall():
        try:
            profs[int(r["user_id"])] = json.loads(r["profile"] or "{}")
        except ValueError:
            profs[int(r["user_id"])] = {}
    notes = {k: {"now": str(db.get(f"{p}{gid}") or ""), "new": list(db.get(new_notes_key(k, gid)) or [])}
             for k, p in NOTE_KEYS.items()}
    plans = []
    try:
        for r in db.conn.execute("SELECT id, user_id, who, what, due, kind FROM qq_plans WHERE group_id=? AND status='open' "
                                 "ORDER BY due", (gid,)).fetchall():
            plans.append({"id": r["id"], "who": r["who"], "what": r["what"],
                          "due": datetime.fromtimestamp(float(r["due"]), tz).strftime("%Y-%m-%d %H:%M"), "kind": r["kind"]})
    except Exception:  # noqa: BLE001
        pass
    images = []
    try:                                                    # 38.2 这批消息里的图：看一眼描述对不对
        for r in db.conn.execute("SELECT * FROM qq_images WHERE group_id=? AND msg_row>=? AND msg_row<=? ORDER BY id",
                                 (gid, lo, int(rows[-1]["id"]) if rows else lo)).fetchall():
            info = json.loads(r["info"] or "{}")
            images.append({"id": r["id"], "msg": r["msg_row"], "who": r["who"], "status": r["status"],
                           "recovered": r["recovered"], "kind": info.get("kind", ""), "line": r["line"],
                           "text": info.get("text") or [], "sensitive": info.get("sensitive", ""),
                           "speakers": [f"{x.get('who')}：{str(x.get('said', ''))[:20]}" for x in (info.get("speakers") or [])[:4]],
                           "deep": [f"{d.get('q')} → {d.get('a')}" for d in json.loads(r["deep"] or "[]")],
                           "ocr": str(r["ocr"] or "")[:120]})
    except Exception:  # noqa: BLE001
        images = []
    last = int(rows[-1]["id"]) if rows else mark
    more = bool(rows) and db.conn.execute("SELECT COUNT(*) FROM qq_group_messages WHERE group_id=? AND id>? AND id<=?",
                                          (gid, last, hi)).fetchone()[0] > 0
    return {"gid": gid, "now": datetime.fromtimestamp(now, tz).strftime("%Y-%m-%d %H:%M"), "her": her,
            "from_id": lo, "to_id": last, "more": more, "mark": mark,
            "members": {str(k): v for k, v in members(db, gid, self_id).items()},
            "context": [m(r, True) for r in before], "messages": [m(r) for r in rows],
            "facts": facts, "profiles": {str(k): v for k, v in profs.items()}, "notes": notes, "plans": plans,
            "images": images, "her_turns": her_turns(db, gid, rows)}


# ======================= 写入 =======================
class Applier:
    def __init__(self, db, memory, gid: int, tz, *, self_id: int | None = None, dry: bool = False, note: str = ""):
        ensure_schema(db.conn)
        self.db, self.mem, self.gid, self.tz, self.dry = db, memory, gid, tz, dry
        self.self_id = self_id
        self.members = members(db, gid, self_id)
        self.results: list[dict] = []
        self.run_id = 0
        if not dry:
            self.run_id = int(db.conn.execute("INSERT INTO qq_mem_runs(group_id, ts, note) VALUES (?,?,?)",
                                              (gid, time.time(), note[:500])).lastrowid)

    # ---------- 留痕 ----------
    def _log(self, op: dict, ok: bool, why: str = "", target: str = "", before=None, after=None):
        self.results.append({"op": op, "ok": ok, "why": why, "target": target})
        if self.dry or not self.run_id:
            return
        self.db.conn.execute("INSERT INTO qq_mem_ops(run_id, op, ok, why, target, before, after, ts) VALUES (?,?,?,?,?,?,?,?)",
                             (self.run_id, json.dumps(op, ensure_ascii=False), int(ok), why, target,
                              None if before is None else json.dumps(before, ensure_ascii=False),
                              None if after is None else json.dumps(after, ensure_ascii=False), time.time()))

    def _fact(self, fid) -> dict | None:
        r = self.db.conn.execute("SELECT * FROM qq_facts WHERE id=? AND group_id=?", (int(fid), self.gid)).fetchone()
        return dict(r) if r else None

    # ---------- 校验 ----------
    def _about(self, v) -> int | None:
        try:
            v = int(v)
        except (TypeError, ValueError):
            return None
        return v if v in (0, -1) or v in self.members else None

    def _check_quote(self, about: int, source: str, msg, quote: str) -> tuple[dict | None, str]:
        try:
            mid = int(msg)
        except (TypeError, ValueError):
            return None, "没给原话的消息编号"
        r = self.db.conn.execute("SELECT id, ts, user_id, role, content FROM qq_group_messages WHERE id=? AND group_id=?",
                                 (mid, self.gid)).fetchone()
        if not r:
            return None, f"消息 {mid} 不存在"
        q = plain(quote)
        if len(q) < 2:
            return None, "引文太短"
        if q not in plain(r["content"]):
            return None, f"引文在消息 {mid} 里找不到"
        author = int(r["user_id"] or 0)
        if about == -1 and r["role"] != "assistant":
            return None, "她自己的事要引她自己说的话"
        if about > 0 and source == "self" and (r["role"] == "assistant" or author != about):
            return None, "来源写的是本人说的，可原话不是他说的"
        return dict(r), ""

    def _check_text(self, text: str) -> str:
        from aigroup.core.safety import looks_like_order
        from aigroup.memory.digest import PRIVATE_RX, PSYCH_RX
        t = str(text or "").strip()
        if not 4 <= len(t) <= 90:
            return "内容太短或太长（4~90 字）"
        if looks_like_order(t):
            return "像是让她记住的指令"
        if PRIVATE_RX.search(t) or PSYCH_RX.search(t):
            return "有隐私或心理推测"
        return ""

    # ---------- 事实 ----------
    def add(self, op: dict):
        about = self._about(op.get("about"))
        kind = KINDS.get(str(op.get("kind") or "fact"))
        src = str(op.get("source") or ("said" if about == -1 else "self"))
        imp = op.get("importance", 2)
        if about is None:
            return self._log(op, False, "about 认不出（群友编号 / 0 群 / -1 她自己）")
        if not kind:
            return self._log(op, False, "kind 不对")
        if src not in SOURCES:
            return self._log(op, False, "source 不对")
        if about == -1 and kind not in ("experience", "opinion"):
            kind = "experience"
        if about != -1 and kind in ("experience", "opinion"):
            return self._log(op, False, "experience / opinion 只给她自己")
        if imp not in (1, 2, 3):
            return self._log(op, False, "importance 只能是 1/2/3")
        why = self._check_text(op.get("text"))
        if why:
            return self._log(op, False, why)
        row, why = self._check_quote(about, src, op.get("msg"), str(op.get("quote") or ""))
        if why:
            return self._log(op, False, why)
        until = _day(op.get("until"), self.tz, end=True)
        when = _day(op.get("when"), self.tz)
        reps = []
        for rid in op.get("replaces") or []:
            f = self._fact(rid)
            if not f:
                return self._log(op, False, f"要替掉的 {rid} 不存在")
            reps.append(int(rid))
        attr = re.sub(r"[^a-z_]", "", str(op.get("attr") or "").lower())[:20]
        if attr and about not in (0,):
            for f in self.db.conn.execute("SELECT id FROM qq_facts WHERE group_id=? AND subject_id=? AND attr=? "
                                          "AND (t_invalid IS NULL OR t_invalid>?)", (self.gid, about, attr, time.time())):
                if int(f["id"]) not in reps:
                    reps.append(int(f["id"]))
        if self.dry:
            return self._log(op, True, "（试跑）")
        befores = {rid: self._fact(rid) for rid in reps}
        author = int(row["user_id"] or 0) if row["role"] != "assistant" else None
        speaker = about if (about > 0 and src == "self") else author if src == "other" else None
        fid = self.mem.add_fact(self.gid, str(op["text"]).strip(), speaker=speaker, subject=about, kind=kind,
                                msg_id=int(row["id"]), replaces=reps, now=float(row["ts"]),
                                source=src, until=until, auto_attr=False)
        if not fid:
            return self._log(op, False, "没记上（太短或以前被删过）", before=None)
        created = bool(self.mem.last_new)
        before = None if created else self._fact(fid)
        self.db.conn.execute("UPDATE qq_facts SET review=?, importance=?, attr=?, t_event=?, quote=?, reviewed_at=?, "
                             "source=? WHERE id=?",
                             ("claude" if created else "ok", imp, attr, when, str(op.get("quote") or "")[:120], time.time(),
                              src, fid))
        if until:
            self.db.conn.execute("UPDATE qq_facts SET t_invalid=? WHERE id=?", (until, fid))
        after = self._fact(fid)
        self._log(op, True, "新记" if created else "和已有的一样，确认那条", f"fact:{fid}", before,
                  {"row": after, "replaced": {str(k): v for k, v in befores.items()}})

    def keep(self, op: dict):
        f = self._fact(op.get("id"))
        if not f:
            return self._log(op, False, "这条不存在")
        sets, args = ["review=?", "reviewed_at=?"], ["ok" if f.get("review") != "claude" else "claude", time.time()]
        if "importance" in op:
            if op["importance"] not in (1, 2, 3):
                return self._log(op, False, "importance 只能是 1/2/3")
            sets.append("importance=?")
            args.append(op["importance"])
        if op.get("text"):
            why = self._check_text(op["text"])
            if why:
                return self._log(op, False, why)
            sets.append("text=?")
            args.append(str(op["text"]).strip()[:120])
        if "source" in op:
            if op["source"] not in SOURCES:
                return self._log(op, False, "source 不对")
            sets.append("source=?")
            args.append(op["source"])
        if "until" in op:
            sets.append("t_invalid=?")
            args.append(_day(op.get("until"), self.tz, end=True))
        if "when" in op:
            sets.append("t_event=?")
            args.append(_day(op.get("when"), self.tz))
        if "attr" in op:
            sets.append("attr=?")
            args.append(re.sub(r"[^a-z_]", "", str(op.get("attr") or "").lower())[:20])
        if "about" in op:
            about = self._about(op["about"])
            if about is None:
                return self._log(op, False, "about 认不出")
            sets.append("subject_id=?")
            args.append(about)
        if self.dry:
            return self._log(op, True, "（试跑）")
        self.db.conn.execute(f"UPDATE qq_facts SET {', '.join(sets)} WHERE id=?", (*args, f["id"]))
        self._log(op, True, "确认" if len(sets) == 2 else "确认并改了", f"fact:{f['id']}", f, self._fact(f["id"]))

    def drop(self, op: dict):
        f = self._fact(op.get("id"))
        if not f:
            return self._log(op, False, "这条不存在")
        why = str(op.get("why") or "").strip()
        if why not in DROP_WHY:
            return self._log(op, False, f"why 要写清是哪种：{'/'.join(sorted(DROP_WHY))}")
        if self.dry:
            return self._log(op, True, "（试跑）")
        forget = bool(op.get("forget")) and why in ("玩笑", "记错", "隐私", "不是他的事")
        if forget:                                   # 记错 / 玩笑 / 隐私：删掉并留指纹，以后白天的整理不会再学回来
            self.mem.forget_fact(int(f["id"]))
            fp = self.db.conn.execute("SELECT MAX(id) m FROM qq_forgotten").fetchone()["m"]
            return self._log(op, True, f"删了（{why}），留了指纹", f"fact:{f['id']}", f, {"deleted": True, "forgotten_id": fp})
        self.db.conn.execute("UPDATE qq_facts SET t_invalid=?, review='drop', reviewed_at=? WHERE id=?",
                             (time.time(), time.time(), f["id"]))
        self._log(op, True, f"不再用（{why}）", f"fact:{f['id']}", f, self._fact(f["id"]))

    # ---------- 笔记 / 画像 ----------
    def notes(self, kind: str, lines):
        if kind not in NOTE_KEYS or not isinstance(lines, list):
            return self._log({"notes": kind}, False, "格式不对（要一个列表）")
        n, w = NOTE_CAPS[kind]
        clean = []
        for x in lines:
            x = str(x or "").strip().lstrip("-").strip()
            if not x:
                continue
            why = self._check_text(x[:w]) if len(x) >= 4 else "太短"
            if why:
                return self._log({"notes": kind, "line": x}, False, why)
            clean.append(x[:w])
        clean = clean[:n]
        key = f"{NOTE_KEYS[kind]}{self.gid}"
        before = {"now": self.db.get(key), "new": self.db.get(new_notes_key(kind, self.gid))}
        if self.dry:
            return self._log({"notes": kind, "lines": clean}, True, "（试跑）")
        self.db.set(key, "\n".join(f"- {x}" for x in clean) or None)
        self.db.set(new_notes_key(kind, self.gid), None)
        self._log({"notes": kind, "lines": clean}, True, f"{len(clean)} 行", f"kv:{kind}", before, {"now": self.db.get(key)})

    def profile(self, uid, fields: dict, replace: bool = False):
        from aigroup.memory.digest import scrub_profile_text
        try:
            uid = int(uid)
        except (TypeError, ValueError):
            return self._log({"profile": uid}, False, "编号不对")
        if uid not in self.members or not isinstance(fields, dict):
            return self._log({"profile": uid}, False, "不是这个群的人或格式不对")
        r = self.db.conn.execute("SELECT profile FROM qq_profiles WHERE user_id=? AND group_id=?", (uid, self.gid)).fetchone()
        try:
            old = json.loads(r["profile"]) if r and r["profile"] else {}
        except ValueError:
            old = {}
        prof = {} if replace else dict(old)
        bad = [k for k in fields if k not in PROFILE_KEYS]
        if bad:
            return self._log({"profile": uid, "fields": list(fields)}, False, f"没有这些字段：{'、'.join(bad)}")
        for k, v in fields.items():
            if k == "记得的事":
                v = [scrub_profile_text(str(x))[:60] for x in (v if isinstance(v, list) else [v]) if str(x).strip()]
                v = [x for x in v if len(x) >= 4][:12]
            else:
                v = scrub_profile_text(str(v or ""))[:40 if k == "印象" else 400]
            if v:
                prof[k] = v
            else:
                prof.pop(k, None)
        if self.dry:
            return self._log({"profile": uid, "fields": list(fields)}, True, "（试跑）")
        if old:                                       # 改之前存一份（小屋“画像历史”里能看、能退回）
            try:
                self.db.conn.execute("INSERT INTO qq_profile_versions(user_id, group_id, profile, ts, source) VALUES (?,?,?,?,?)",
                                     (uid, self.gid, json.dumps(old, ensure_ascii=False), time.time(), "claude"))
            except Exception:  # noqa: BLE001
                pass
        js = json.dumps(prof, ensure_ascii=False)
        note = str(prof.get("印象") or "")[:80]
        self.db.conn.execute("UPDATE qq_profiles SET profile=?, note=CASE WHEN ?!='' THEN ? ELSE note END, updated=? "
                             "WHERE user_id=? AND group_id=?", (js, note, note, time.time(), uid, self.gid))
        self.db.conn.execute("UPDATE qq_people SET profile=?, note=CASE WHEN ?!='' THEN ? ELSE note END WHERE user_id=?",
                             (js, note, note, uid))
        self._log({"profile": uid, "fields": list(fields), "replace": replace}, True,
                  "整份重写" if replace else f"改了 {len(fields)} 项", f"profile:{uid}", old, prof)

    # ---------- 约好的事（qq_plans）：改日子 / 取消 ----------
    def plan(self, op: dict):
        try:
            r = self.db.conn.execute("SELECT * FROM qq_plans WHERE id=? AND group_id=?", (int(op.get("id")), self.gid)).fetchone()
        except Exception:  # noqa: BLE001
            r = None
        if not r:
            return self._log(op, False, "这条约定不存在")
        before = dict(r)
        if op.get("drop"):
            sets, args = ["status=?"], ["cancelled"]
        elif op.get("due"):
            try:
                due = datetime.strptime(str(op["due"]).strip()[:16], "%Y-%m-%d %H:%M").replace(tzinfo=self.tz).timestamp()
            except ValueError:
                return self._log(op, False, "due 要写成 YYYY-MM-DD HH:MM")
            sets, args = ["due=?"], [due]
        else:
            return self._log(op, False, "要么 drop，要么给 due")
        if self.dry:
            return self._log(op, True, "（试跑）")
        self.db.conn.execute(f"UPDATE qq_plans SET {', '.join(sets)} WHERE id=?", (*args, before["id"]))
        after = dict(self.db.conn.execute("SELECT * FROM qq_plans WHERE id=?", (before["id"],)).fetchone())
        self._log(op, True, "取消了" if op.get("drop") else "改了日子", f"plan:{before['id']}", before, after)

    # ---------- 38.2 看图描述改正：{"id", "gist"?, "kind"?, "sensitive"?, "text"?: [只留这几处原字]} ----------
    def image(self, op: dict):
        try:
            r = self.db.conn.execute("SELECT * FROM qq_images WHERE id=? AND group_id=?", (int(op.get("id")), self.gid)).fetchone()
        except Exception:  # noqa: BLE001
            r = None
        if not r:
            return self._log(op, False, "这张图不存在")
        from aigroup.chat.vision import KINDS, make_line, on_ocr
        info = json.loads(r["info"] or "{}")
        new = dict(info)
        if op.get("gist"):
            new["gist"] = str(op["gist"])[:40]
        if op.get("kind"):
            if op["kind"] not in KINDS:
                return self._log(op, False, f"kind 只能是：{'、'.join(KINDS)}")
            new["kind"] = op["kind"]
        if "sensitive" in op:
            from aigroup.chat.vision import SENS_ALIAS, SENSITIVE
            sv = SENS_ALIAS.get(str(op["sensitive"] or ""), str(op["sensitive"] or ""))
            if sv and sv not in SENSITIVE:
                return self._log(op, False, f"sensitive 只能是：{'、'.join(SENSITIVE)}（不是就写空）")
            new["sensitive"] = sv
            if not new["sensitive"]:
                new.pop("sensitive")
            new["ask_ok"] = not new.get("sensitive")
        if "text" in op:
            tx = [str(t) for t in op.get("text") or []]
            bad = [t for t in tx if not on_ocr(t, r["ocr"] or "")]
            if bad:
                return self._log(op, False, f"「{bad[0]}」OCR 里没有：原字只能留 OCR 认出来的")
            new["text"] = tx
            if not tx:
                new.pop("text")
        if new == info:
            return self._log(op, False, "什么都没改")
        line = make_line(new, r["status"])
        msg = self.db.conn.execute("SELECT content FROM qq_group_messages WHERE id=?", (r["msg_row"],)).fetchone() if r["msg_row"] else None
        before = {"info": info, "line": r["line"], "content": msg["content"] if msg else None}
        if self.dry:
            return self._log(op, True, f"（试跑）→ {line}")
        self.db.conn.execute("UPDATE qq_images SET info=?, line=?, gist=?, kind=?, updated=? WHERE id=?",
                             (json.dumps(new, ensure_ascii=False), line, new.get("gist", ""), new.get("kind", ""), time.time(), r["id"]))
        content = None
        if msg:
            content = re.sub(r"\[图片(?:[：:][^\]]*)?\]", f"[图片：{line[:100]}]", str(msg["content"] or ""), count=1)
            self.db.conn.execute("UPDATE qq_group_messages SET content=? WHERE id=?", (content[:500], r["msg_row"]))
        self._log(op, True, f"→ {line}", f"image:{r['id']}", before, {"info": new, "line": line, "content": content})

    # ---------- 38.3 给她说的话打分：{"msg", "score": 1~5, "tags": [...], "better": "更好的说法", "pick": "对/错"} ----------
    def score(self, op: dict):
        try:
            mid = int(op.get("msg"))
            sc = int(op.get("score"))
        except (TypeError, ValueError):
            return self._log(op, False, "msg 和 score 要是数字")
        if not 1 <= sc <= 5:
            return self._log(op, False, "score 是 1~5")
        r = self.db.conn.execute("SELECT * FROM qq_group_messages WHERE id=? AND group_id=?", (mid, self.gid)).fetchone()
        if not r or r["role"] != "assistant":
            return self._log(op, False, "这条不是她说的")
        tags = [str(t) for t in op.get("tags") or []]
        bad = [t for t in tags if t not in SCORE_TAGS]
        if bad:
            return self._log(op, False, f"标签「{bad[0]}」不认识：只能用 {'、'.join(SCORE_TAGS)}")
        pick = str(op.get("pick") or "")
        if pick not in ("", "对", "错"):
            return self._log(op, False, "pick 只能是 对 / 错（挑的那句对不对），没有就不写")
        turn = next((t for t in her_turns(self.db, self.gid, [r]) if t["msg"] == mid), {})
        trace = turn.get("trace") or f"msg:{mid}"
        ctx = [f"{x['name'] if x['role'] == 'user' else '她'}：{str(x['content'] or '')[:80]}" for x in reversed(
            self.db.conn.execute("SELECT name, role, content FROM qq_group_messages WHERE group_id=? AND id<? ORDER BY id DESC LIMIT 6",
                                 (self.gid, mid)).fetchall())]
        old = self.db.conn.execute("SELECT * FROM qq_human_score WHERE trace=?", (trace,)).fetchone()
        if self.dry:
            return self._log(op, True, "（试跑）")
        self.db.conn.execute(
            "INSERT OR REPLACE INTO qq_human_score(trace, group_id, said, context, score, ts, src, tags, better, pick, msg_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (trace, self.gid, str(r["content"] or ""), "\n".join(ctx), sc, time.time(), "claude-review", "、".join(tags),
             str(op.get("better") or "")[:120], pick, mid))
        self._log(op, True, f"{sc} 分", f"score:{trace}", dict(old) if old else None, {"score": sc})

    # ---------- 一张操作单 ----------
    def run(self, sheet: dict) -> dict:
        if int(sheet.get("gid") or self.gid) != self.gid:
            raise ValueError("操作单的群和这次不一样")
        for op in sheet.get("facts") or []:
            kind = str(op.get("op") or "")
            try:
                {"add": self.add, "keep": self.keep, "drop": self.drop}.get(kind, lambda o: self._log(o, False, "op 不认识"))(op)
            except Exception as e:  # noqa: BLE001
                self._log(op, False, f"出错：{e}")
        for k in NOTE_KEYS:
            if k in sheet:
                self.notes(k, sheet[k])
        for op in sheet.get("plans") or []:
            try:
                self.plan(op)
            except Exception as e:  # noqa: BLE001
                self._log(op, False, f"出错：{e}")
        for uid, fields in (sheet.get("profiles") or {}).items():
            self.profile(uid, fields, replace=bool(sheet.get("profiles_replace")))
        for op in sheet.get("scores") or []:
            try:
                self.score(op)
            except Exception as e:  # noqa: BLE001
                self._log(op, False, f"出错：{e}")
        for op in sheet.get("images") or []:
            try:
                self.image(op)
            except Exception as e:  # noqa: BLE001
                self._log(op, False, f"出错：{e}")
        ok = sum(1 for r in self.results if r["ok"])
        bad = len(self.results) - ok
        to_id = sheet.get("to_id")
        if not self.dry:
            if to_id:
                cur = int(self.db.get(MARK.format(self.gid)) or 0)
                self.db.set(MARK.format(self.gid), max(cur, int(to_id)))
            self.db.conn.execute("UPDATE qq_mem_runs SET from_id=?, to_id=?, n_ok=?, n_bad=? WHERE id=?",
                                 (sheet.get("from_id"), to_id, ok, bad, self.run_id))
            self.db.set(f"mem_review_last_{self.gid}", {"ts": time.time(), "run": self.run_id, "ok": ok, "bad": bad, "to": to_id})
        self.db.conn.commit()
        return {"run": self.run_id, "ok": ok, "bad": bad,
                "rejected": [{"op": r["op"], "why": r["why"]} for r in self.results if not r["ok"]]}


def undo(db, run_id: int) -> int:
    """把一次复核整个退回（倒着来）。返回退回了几条。"""
    ensure_schema(db.conn)
    run = db.conn.execute("SELECT * FROM qq_mem_runs WHERE id=?", (int(run_id),)).fetchone()
    if not run or run["undone"]:
        return 0
    gid = int(run["group_id"])
    n = 0
    cols = None
    for o in db.conn.execute("SELECT * FROM qq_mem_ops WHERE run_id=? AND ok=1 ORDER BY id DESC", (int(run_id),)).fetchall():
        target = o["target"] or ""
        before = json.loads(o["before"]) if o["before"] else None
        after = json.loads(o["after"]) if o["after"] else None
        if target.startswith("fact:"):
            fid = int(target[5:])
            if cols is None:
                cols = [r[1] for r in db.conn.execute("PRAGMA table_info(qq_facts)")]
            if isinstance(after, dict) and after.get("replaced"):
                for rid, row in after["replaced"].items():
                    if row:
                        _restore(db, row, cols)
            if before is None:
                db.conn.execute("DELETE FROM qq_facts WHERE id=?", (fid,))
            else:
                _restore(db, before, cols)
                if isinstance(after, dict) and after.get("forgotten_id"):
                    db.conn.execute("DELETE FROM qq_forgotten WHERE id=?", (int(after["forgotten_id"]),))
            n += 1
        elif target.startswith("kv:"):
            kind = target[3:]
            db.set(f"{NOTE_KEYS[kind]}{gid}", (before or {}).get("now"))
            db.set(new_notes_key(kind, gid), (before or {}).get("new"))
            n += 1
        elif target.startswith("plan:") and before:
            db.conn.execute("UPDATE qq_plans SET status=?, due=? WHERE id=?", (before["status"], before["due"], int(target[5:])))
            n += 1
        elif target.startswith("score:"):
            tr = target[6:]
            if before:
                cols_ = list(before)
                db.conn.execute(f"INSERT OR REPLACE INTO qq_human_score({', '.join(cols_)}) VALUES ({', '.join('?' * len(cols_))})",
                                [before[c] for c in cols_])
            else:
                db.conn.execute("DELETE FROM qq_human_score WHERE trace=?", (tr,))
            n += 1
        elif target.startswith("image:") and before:
            iid = int(target[6:])
            db.conn.execute("UPDATE qq_images SET info=?, line=? WHERE id=?",
                            (json.dumps(before.get("info") or {}, ensure_ascii=False), before.get("line") or "", iid))
            row = db.conn.execute("SELECT msg_row FROM qq_images WHERE id=?", (iid,)).fetchone()
            if row and row["msg_row"] and before.get("content") is not None:
                db.conn.execute("UPDATE qq_group_messages SET content=? WHERE id=?", (before["content"], row["msg_row"]))
            n += 1
        elif target.startswith("profile:"):
            uid = int(target[8:])
            js = json.dumps(before or {}, ensure_ascii=False)
            note = str((before or {}).get("印象") or "")[:80]
            db.conn.execute("UPDATE qq_profiles SET profile=?, note=CASE WHEN ?!='' THEN ? ELSE note END WHERE user_id=? AND group_id=?",
                            (js, note, note, uid, gid))
            db.conn.execute("UPDATE qq_people SET profile=? WHERE user_id=?", (js, uid))
            n += 1
    db.conn.execute("UPDATE qq_mem_runs SET undone=1 WHERE id=?", (int(run_id),))
    if run["from_id"]:
        cur = int(db.get(MARK.format(gid)) or 0)
        if cur and run["to_id"] and cur <= int(run["to_id"]):
            db.set(MARK.format(gid), int(run["from_id"]) - 1)
    db.conn.commit()
    return n


def _restore(db, row: dict, cols: list[str]):
    keep = [c for c in cols if c in row]
    db.conn.execute(f"INSERT OR REPLACE INTO qq_facts({', '.join(keep)}) VALUES ({', '.join('?' * len(keep))})",
                    [row[c] for c in keep])


def status(db, gid: int, tz) -> dict:
    ensure_schema(db.conn)
    mark = int(db.get(MARK.format(gid)) or 0)
    left = db.conn.execute("SELECT COUNT(*) FROM qq_group_messages WHERE group_id=? AND id>?", (gid, mark)).fetchone()[0]
    now = time.time()
    by = {r[0] or "临时": r[1] for r in db.conn.execute(
        "SELECT review, COUNT(*) FROM qq_facts WHERE group_id=? AND (t_invalid IS NULL OR t_invalid>?) GROUP BY review", (gid, now))}
    runs = [dict(r) for r in db.conn.execute("SELECT id, ts, from_id, to_id, n_ok, n_bad, undone FROM qq_mem_runs WHERE group_id=? "
                                             "ORDER BY id DESC LIMIT 5", (gid,))]
    for r in runs:
        r["ts"] = datetime.fromtimestamp(r["ts"], tz).strftime("%m-%d %H:%M")
    return {"mark": mark, "left": left, "facts": by, "runs": runs}


def render_text(data: dict) -> str:
    """导出给人 / Claude 读的文字版（和 JSON 内容一样，只是好读）。"""
    mem = data.get("members") or {}

    def who(w):
        if w == data.get("her", "她"):
            return "她"
        m = mem.get(str(w)) or {}
        al = (m.get("aliases") or [None])[0]
        return f"{al or m.get('name') or w}({w})"
    out = [f"# 群 {data['gid']}  导出于 {data['now']}  消息 {data['from_id']}~{data['to_id']}  后面还有：{data['more']}",
           "## 群友", *[f"- {k}：{v.get('name')}｜也叫：{'、'.join(v.get('aliases') or [])}" for k, v in mem.items()]]
    if data.get("context"):
        out.append("## 上文（已复核过，只用来看懂）")
        out += [f"{m[0]} {m[1]} {who(m[2])}：{m[3]}" for m in data["context"]]
    out.append("## 这次要复核的聊天")
    out += [f"{m[0]} {m[1]} {who(m[2])}：{m[3]}" for m in data["messages"]]
    out.append("## 现在记着的事（id｜关于谁｜kind｜来源｜复核｜重要度｜attr｜说的日子｜有效到｜日子｜原消息｜内容）")
    for f in data["facts"]:
        out.append(f"{f['id']}｜{f['about']}｜{f['kind']}｜{f['source'] or '-'}｜{f['review'] or '临时'}｜{f['importance']}｜"
                   f"{f['attr'] or '-'}｜{f['said']}｜{f['until'] or '-'}｜{f['when'] or '-'}｜{f['msg'] or '-'}｜{f['text']}")
    out.append("## 画像")
    for k, v in (data.get("profiles") or {}).items():
        out.append(f"### {k}")
        out += [f"- {fk}：{fv}" for fk, fv in v.items()]
    out.append("## 群笔记")
    for k, v in (data.get("notes") or {}).items():
        out.append(f"### {k}\n现在：\n{v.get('now') or '（空）'}\n白天新记的：\n" + ("\n".join(v.get("new") or []) or "（空）"))
    if data.get("plans"):
        out.append("## 约好的事（没到点的）")
        out += [f"- {p['due']} {p['who']}：{p['what']}（{p['kind']}）" for p in data["plans"]]
    if data.get("her_turns"):
        out.append("## 她说的话（逐句打分；有“写了几句”的看挑得对不对）")
        for h in data["her_turns"]:
            out.append(f"- {h['msg']}：{h['text']}")
            pk = h.get("pick") or {}
            for c in pk.get("cands") or []:
                mark = "→" if c.get("k") == pk.get("chosen") else "  "
                out.append(f"    {mark} {c.get('text')}（{'底线' if c.get('hard') else c.get('total')}）")
    if data.get("images"):
        out.append("## 看图（这批消息里的图：描述对不对、有没有编字 / 认错人 / 该算敏感没算）")
        for im in data["images"]:
            extra = "｜".join(x for x in [
                f"原字：{'、'.join(im['text'])}" if im.get("text") else "",
                f"敏感：{im['sensitive']}" if im.get("sensitive") else "",
                f"谁说的：{'／'.join(im['speakers'])}" if im.get("speakers") else "",
                f"细看：{'／'.join(im['deep'])}" if im.get("deep") else "",
                f"OCR：{im['ocr']}" if im.get("ocr") else ""] if x)
            out.append(f"- 图{im['id']}（消息 {im['msg']}，{im['who']}，{im['status']}{'，补到原图' if im.get('recovered') else ''}）"
                       f"{im['line']}" + (f"｜{extra}" if extra else ""))
    return "\n".join(out)
