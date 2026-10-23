"""20.2 事实账本：查资料查到的硬事实存下来，带有效期，新值来了把旧值标成“被取代”。

为什么不直接用 qq_knowledge：那是“话题 + 一两句”的谈资，好聊但没法判断过没过期、谁取代了谁。
这里按（实体, 属性）存一个值，参考：
- MemStrata（2026-06）：按（主体, 关系）当键，新值进来关闭旧值的有效期、标被谁取代；读到过期事实答错 0.15~0.40 → 0~0.03
- Zep / Graphiti：双时间（什么时候成立、什么时候失效），历史保留
- STALE（2026-05）：模型自己判断记忆过没过期只有 55% → 过期交给 valid_to，不交给模型

有效期按 kind：实时（比分、在线人数）2 小时、价格 1 天、赛程 / 版本 7 天、所属 / 状态 30 天、常识 365 天。
所属 / 状态变了（转会、退役）→ 同一个实体的“实时 / 赛程”一起关掉（连带失效）。
"""
from __future__ import annotations

import re
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS qq_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id INTEGER NOT NULL,
    entity TEXT NOT NULL,
    attr TEXT NOT NULL,
    value TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT '常识',
    as_of TEXT NOT NULL DEFAULT '',
    valid_to REAL NOT NULL,
    source_site TEXT NOT NULL DEFAULT '',
    superseded_by INTEGER,
    created REAL NOT NULL,
    hits INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_qq_ledger ON qq_ledger(group_id, entity, attr);
"""
H = 3600
TTL = {"实时": 2 * H, "价格": 24 * H, "赛程": 7 * 24 * H, "所属": 30 * 24 * H, "状态": 30 * 24 * H, "常识": 365 * 24 * H}
CASCADE = {"所属": ("实时", "赛程"), "状态": ("实时", "赛程", "所属")}


def _key(s: str) -> str:
    return re.sub(r"\s+", "", str(s or "")).strip().lower()[:40]


class Ledger:
    def __init__(self, db, ttl: dict | None = None):
        self.db = db
        self.ttl = {**TTL, **(ttl or {})}
        db.conn.executescript(SCHEMA)
        db.conn.commit()

    def write(self, gid: int, brief: dict, now: float | None = None) -> int:
        """简报里的 facts 写进账本。把握低的、只有论坛来源的不写。返回写了几条。"""
        if not isinstance(brief, dict) or str(brief.get("confidence") or "") == "low":
            return 0
        srcs = [s for s in (brief.get("sources") or []) if isinstance(s, dict)]
        site = str((srcs[0].get("site") if srcs else "") or "")[:40]
        n = 0
        for f in (brief.get("facts") or [])[:5]:
            if not isinstance(f, dict):
                continue
            ent, attr, val = str(f.get("entity") or "").strip(), str(f.get("attr") or "").strip(), str(f.get("value") or "").strip()
            if not (ent and attr and val) or len(val) > 80:
                continue
            self.put(gid, ent, attr, val, str(f.get("kind") or "常识"), str(brief.get("as_of") or "")[:10], site, now=now)
            n += 1
        return n

    def put(self, gid: int, entity: str, attr: str, value: str, kind: str = "常识", as_of: str = "", site: str = "",
            now: float | None = None) -> int:
        now = now or time.time()
        kind = kind if kind in self.ttl else "常识"
        until = now + self.ttl[kind]
        c = self.db.conn
        olds = c.execute("SELECT * FROM qq_ledger WHERE group_id=? AND valid_to>? AND superseded_by IS NULL",
                         (gid, now)).fetchall()
        same = [r for r in olds if _key(r["entity"]) == _key(entity) and _key(r["attr"]) == _key(attr)]
        for r in same:
            if _key(r["value"]) == _key(value):                          # 一样的值：续期
                c.execute("UPDATE qq_ledger SET valid_to=?, as_of=CASE WHEN ?!='' THEN ? ELSE as_of END WHERE id=?",
                          (max(until, float(r["valid_to"])), as_of, as_of, r["id"]))
                c.commit()
                return int(r["id"])
        cur = c.execute("INSERT INTO qq_ledger(group_id, entity, attr, value, kind, as_of, valid_to, source_site, created) "
                        "VALUES (?,?,?,?,?,?,?,?,?)", (gid, entity[:40], attr[:30], value[:80], kind, as_of, until, site, now))
        new = int(cur.lastrowid)
        for r in same:                                                   # 值变了：旧的被取代
            c.execute("UPDATE qq_ledger SET valid_to=?, superseded_by=? WHERE id=?", (now, new, r["id"]))
        if same and kind in CASCADE:                                     # 转会 / 退役：这个人的比分、赛程也作废
            for r in olds:
                if _key(r["entity"]) == _key(entity) and r["kind"] in CASCADE[kind] and r["id"] not in [x["id"] for x in same]:
                    c.execute("UPDATE qq_ledger SET valid_to=? WHERE id=?", (now, r["id"]))
        c.commit()
        return new

    def valid(self, gid: int, text: str, k: int = 5, now: float | None = None) -> list[dict]:
        """这段话里提到的实体，账本里还有效的事实。"""
        now = now or time.time()
        t = _key(text)
        rows = self.db.conn.execute("SELECT * FROM qq_ledger WHERE group_id=? AND valid_to>? AND superseded_by IS NULL "
                                    "ORDER BY created DESC LIMIT 300", (gid, now)).fetchall()
        hit = [dict(r) for r in rows if len(_key(r["entity"])) >= 2 and _key(r["entity"]) in t][:k]
        for r in hit:
            self.db.conn.execute("UPDATE qq_ledger SET hits=hits+1 WHERE id=?", (r["id"],))
        if hit:
            self.db.conn.commit()
        return hit

    def outdated_mentions(self, gid: int, text: str, now: float | None = None) -> list[tuple[dict, dict]]:
        """话里提到了某个实体的旧值（已经被取代的），返回 (旧, 新)——比如“他还在 Sentinels 吗”而账本里他已经转会。"""
        now = now or time.time()
        t = _key(text)
        out = []
        rows = self.db.conn.execute("SELECT o.*, n.value nv, n.as_of nas, n.id nid FROM qq_ledger o JOIN qq_ledger n "
                                    "ON n.id=o.superseded_by WHERE o.group_id=? AND n.valid_to>? ORDER BY n.created DESC LIMIT 200",
                                    (gid, now)).fetchall()
        for r in rows:
            if len(_key(r["entity"])) >= 2 and _key(r["entity"]) in t and len(_key(r["value"])) >= 2 and _key(r["value"]) in t:
                out.append((dict(r), {"value": r["nv"], "as_of": r["nas"], "id": r["nid"]}))
        return out[:3]

    def block(self, gid: int, text: str, tz=None) -> str:
        from datetime import datetime
        lines = []
        for old, new in self.outdated_mentions(gid, text):
            lines.append(f"- 注意：{old['entity']}的{old['attr']}以前是“{old['value']}”，现在是“{new['value']}”"
                         f"（{new['as_of'] or '最近'}查到的）：有人说的是旧的，就顺口纠正")
        for r in self.valid(gid, text):
            lines.append(f"- {r['entity']}的{r['attr']}：{r['value']}（{r['as_of'] or '之前'}查的，"
                         f"有效到 {datetime.fromtimestamp(r['valid_to'], tz):%m-%d %H:%M}）")
        if not lines:
            return ""
        return "# 你之前查到过的（还有效；数字照这个说，过了有效期的别当真）\n" + "\n".join(lines)

    def entries(self, gid: int | None = None, limit: int = 100) -> list[dict]:
        q = "SELECT * FROM qq_ledger" + (" WHERE group_id=?" if gid is not None else "") + " ORDER BY id DESC LIMIT ?"
        rows = self.db.conn.execute(q, ([gid] if gid is not None else []) + [limit]).fetchall()
        now = time.time()
        return [{**dict(r), "valid": float(r["valid_to"]) > now and r["superseded_by"] is None} for r in rows]

    def delete(self, lid: int):
        self.db.conn.execute("DELETE FROM qq_ledger WHERE id=?", (int(lid),))
        self.db.conn.commit()
