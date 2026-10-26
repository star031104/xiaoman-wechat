"""24.0 统一的发送口（参考 dafeiyu-hub 的写操作闸门）：她往群里发的每一条都从这里过。

以前各条路（接话、冒泡、分享、提醒、复读、表情包、语音）各管各的限额，提醒和“必须说”的会绕过上限，也没有一本总账。
现在 GroupChat 启动时把 ob.send_group 包一层：
1. 演练（dry_run）：小屋里打开后，什么都不真发，只记进账本（测试新版本用）；抛 DryRun，调用方当成“没发出去”，
   speak 不会把这句记成她说过的话，也不会算“连着发不出去”。
2. 静音：小屋里让这个群先别说话时，任何一条路都发不出去。
3. 每天总上限（send_per_day，默认 300 条）：防止哪里出了循环把群刷屏。
4. 只追加的账本 qq_actions：什么时候、发了什么、成没成、为什么没成（小屋“今天她做了什么”和日报用）。
"""
from __future__ import annotations

import json
import logging
import time

log = logging.getLogger("aigroup.group")

SCHEMA = """
CREATE TABLE IF NOT EXISTS qq_actions (        -- 24.0 她往外发的每一条（只追加）
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    group_id INTEGER NOT NULL,
    kind TEXT NOT NULL DEFAULT 'text',          -- text / image / sticker / voice / quote
    text TEXT NOT NULL DEFAULT '',
    ok INTEGER NOT NULL DEFAULT 0,
    why TEXT NOT NULL DEFAULT '',               -- 没发成的原因：演练 / 静音 / 到上限 / 出错……
    mid TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_qq_actions ON qq_actions(group_id, ts);
"""


class DryRun(RuntimeError):
    """演练模式：这一条没真发。"""


def seg_summary(segs: list[dict]) -> tuple[str, str]:
    kinds, texts = [], []
    for s in segs or []:
        t = str(s.get("type") or "")
        d = s.get("data") or {}
        if t == "text":
            texts.append(str(d.get("text") or ""))
        elif t == "at":
            texts.append(f"@{d.get('qq') or ''}")
        elif t == "image":
            kinds.append("sticker" if str(d.get("sub_type") or d.get("summary") or "") in ("1", "[动画表情]") else "image")
        elif t == "record":
            kinds.append("voice")
        elif t == "reply":
            kinds.append("quote")
    kind = next((k for k in ("voice", "sticker", "image") if k in kinds), "text")
    return kind, "".join(texts)[:300]


class OutboxMixin:
    def _outbox_init(self):
        self.db.conn.executescript(SCHEMA)
        self.db.conn.commit()
        ob = self.ob
        if getattr(ob, "_gated", False) or not hasattr(ob, "send_group"):
            return
        raw = ob.send_group
        gc = self

        async def send_group(group_id, segs, *a, **kw):
            return await gc._gated_send(raw, group_id, segs, *a, **kw)
        ob.send_group = send_group
        ob._gated = True

    def dry_run(self, gid: int | None = None) -> bool:
        return bool(self.db.get("dry_run")) or bool(gid is not None and self.gk(gid).get("dry_run", False))

    def _action(self, gid: int, kind: str, text: str, ok: bool, why: str = "", mid=None):
        try:
            self.db.conn.execute("INSERT INTO qq_actions(ts, group_id, kind, text, ok, why, mid) VALUES (?,?,?,?,?,?,?)",
                                 (time.time(), int(gid), kind, text, int(bool(ok)), why[:80], "" if mid is None else str(mid)))
            self.db.conn.commit()
        except Exception as e:  # noqa: BLE001
            log.debug("记发送账本失败：%s", e)

    def sent_today(self, gid: int) -> int:
        now = self.brain.now()
        t0 = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        return int(self.db.conn.execute("SELECT COUNT(*) n FROM qq_actions WHERE group_id=? AND ok=1 AND ts>=?",
                                        (gid, t0)).fetchone()["n"])

    async def _gated_send(self, raw, gid, segs, *a, **kw):
        kind, text = seg_summary(segs)
        if self.dry_run(gid):
            self._action(gid, kind, text, False, "演练")
            log.info("群 %s【演练，没真发】%s", gid, text[:60] or kind)
            raise DryRun("演练模式：没真发")
        if self.muted(gid):
            self._action(gid, kind, text, False, "静音")
            raise RuntimeError("这个群在小屋里被设成先别说话")
        cap = int(self.gk(gid).get("send_per_day", 300) or 0)
        if cap and self.sent_today(gid) >= cap:
            self._action(gid, kind, text, False, "到上限")
            if self._allow(f"cap_warn_{gid}", 1):
                log.warning("群 %s：今天已经发了 %d 条，到上限了（send_per_day），后面的都不发", gid, cap)
            raise RuntimeError("今天发的条数到上限了")
        try:
            mid = await raw(gid, segs, *a, **kw)
        except Exception as e:
            self._action(gid, kind, text, False, f"出错：{e}"[:80])
            raise
        self._action(gid, kind, text, True, "", mid)
        return mid

    def actions(self, gid: int, since: float, limit: int = 200) -> list[dict]:
        return [dict(r) for r in self.db.conn.execute(
            "SELECT ts, kind, text, ok, why FROM qq_actions WHERE group_id=? AND ts>=? ORDER BY id DESC LIMIT ?",
            (gid, since, limit)).fetchall()]

    def actions_summary(self, gid: int, since: float) -> dict:
        rows = self.db.conn.execute("SELECT ok, why, kind, COUNT(*) n FROM qq_actions WHERE group_id=? AND ts>=? "
                                    "GROUP BY ok, why, kind", (gid, since)).fetchall()
        out: dict = {"sent": 0, "failed": 0, "by_kind": {}, "why": {}}
        for r in rows:
            if r["ok"]:
                out["sent"] += r["n"]
                out["by_kind"][r["kind"]] = out["by_kind"].get(r["kind"], 0) + r["n"]
            else:
                out["failed"] += r["n"]
                key = str(r["why"]).split("：")[0]
                out["why"][key] = out["why"].get(key, 0) + r["n"]
        return json.loads(json.dumps(out))
