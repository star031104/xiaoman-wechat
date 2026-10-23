"""群梗词条（2.2）：照 SillyTavern 的 World Info / Lorebook 做的“聊到某个词，就把这个梗的意思告诉她”。

每条：关键词（任一命中）、排除词（命中就不触发）、内容、sticky（触发后再保持 N 条消息）、cooldown（触发后 N 条消息内不再触发）、
触发概率、来源（auto 自动 / manual 手写）、状态（new / ok / no）。
- 只扫最近 scan_depth 条消息（默认 6）；一次最多注入 max_entries 条（默认 4）、总字数 400
- 关键词按“整词”匹配：中文前后不能紧挨着别的汉字组成更长的词时才算（像 SillyTavern 的 match whole words，避免“小满足”）——
  中文没有空格，这里放宽成：英文 / 数字关键词要求前后不是字母数字；中文关键词直接子串匹配
- cooldown 解决“老梗反复说”；sticky 让一个梗在接下来几句里都还记得

来源：每晚把判断为“真黑话”的词（learn.py）和整理群聊时写的“梗|关键词|解释”自动变成词条；小屋“学到的”页可以手写、改、删。
"""
from __future__ import annotations

import json
import random
import re
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS qq_lore (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id INTEGER NOT NULL,
    keys TEXT NOT NULL DEFAULT '[]',
    exclude TEXT NOT NULL DEFAULT '[]',
    content TEXT NOT NULL,
    sticky INTEGER NOT NULL DEFAULT 3,
    cooldown INTEGER NOT NULL DEFAULT 20,
    prob REAL NOT NULL DEFAULT 1.0,
    source TEXT NOT NULL DEFAULT 'manual',
    status TEXT NOT NULL DEFAULT 'new',
    fired_at INTEGER NOT NULL DEFAULT 0,     -- 上次触发时这个群的消息 id
    fires INTEGER NOT NULL DEFAULT 0,
    created REAL, updated REAL
);
CREATE INDEX IF NOT EXISTS idx_qq_lore ON qq_lore(group_id);
"""


def key_hit(key: str, text: str) -> bool:
    key = (key or "").strip()
    if not key:
        return False
    if re.fullmatch(r"[A-Za-z0-9_]+", key):
        return re.search(rf"(?<![A-Za-z0-9_]){re.escape(key)}(?![A-Za-z0-9_])", text, flags=re.I) is not None
    return key in text


class Lore:
    def __init__(self, db, scan_depth: int = 6, max_entries: int = 4, budget: int = 400):
        self.db, self.scan_depth, self.max_entries, self.budget = db, scan_depth, max_entries, budget
        db.conn.executescript(SCHEMA)
        db.conn.commit()

    def add(self, gid: int, keys: list[str], content: str, *, exclude: list[str] | None = None, sticky: int = 3,
            cooldown: int = 20, prob: float = 1.0, source: str = "manual") -> int | None:
        keys = [k.strip()[:20] for k in keys if k and k.strip()][:8]
        content = re.sub(r"\s+", " ", str(content or "")).strip()[:150]
        if not keys or len(content) < 2:
            return None
        now = time.time()
        for r in self.db.conn.execute("SELECT id, keys, source FROM qq_lore WHERE group_id=?", (gid,)).fetchall():
            if set(json.loads(r["keys"])) & set(keys):            # 同一个梗：自动的更新内容，手写的不动
                if r["source"] == "auto" and source == "auto":
                    self.db.conn.execute("UPDATE qq_lore SET content=?, updated=? WHERE id=?", (content, now, r["id"]))
                    self.db.conn.commit()
                return int(r["id"])
        cur = self.db.conn.execute(
            "INSERT INTO qq_lore(group_id, keys, exclude, content, sticky, cooldown, prob, source, created, updated) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)", (gid, json.dumps(keys, ensure_ascii=False),
                                            json.dumps(exclude or [], ensure_ascii=False), content, int(sticky),
                                            int(cooldown), float(prob), source, now, now))
        self.db.conn.commit()
        return int(cur.lastrowid)

    def entries(self, gid: int) -> list[dict]:
        out = []
        for r in self.db.conn.execute("SELECT * FROM qq_lore WHERE group_id=? ORDER BY id", (gid,)).fetchall():
            x = dict(r)
            x["keys"], x["exclude"] = json.loads(x["keys"] or "[]"), json.loads(x["exclude"] or "[]")
            out.append(x)
        return out

    def active(self, gid: int, recent_text: str, now_id: int | None = None, rand: random.Random | None = None) -> list[dict]:
        """这一刻该注入的词条。now_id：这个群现在最新的消息 id（算 sticky / cooldown 用）。"""
        if now_id is None:
            r = self.db.conn.execute("SELECT MAX(id) m FROM qq_group_messages WHERE group_id=?", (gid,)).fetchone()
            now_id = int(r["m"] or 0)
        rnd = rand or random
        picked, used = [], 0
        for e in self.entries(gid):
            if e["status"] == "no":
                continue
            since = now_id - int(e["fired_at"] or 0) if e["fired_at"] else 10 ** 9
            sticky_on = e["fired_at"] and since <= int(e["sticky"])
            hit = any(key_hit(k, recent_text) for k in e["keys"]) and not any(key_hit(x, recent_text) for x in e["exclude"])
            if sticky_on:
                pass                                              # 刚触发过：接下来几条里还记得
            elif not hit:
                continue
            elif since <= int(e["cooldown"]) or rnd.random() >= float(e["prob"]):
                continue                                          # 冷却中：这个梗刚提过，先别再提
            if used + len(e["content"]) > self.budget or len(picked) >= self.max_entries:
                break
            picked.append(e)
            used += len(e["content"])
            if not sticky_on:
                self.db.conn.execute("UPDATE qq_lore SET fired_at=?, fires=fires+1 WHERE id=?", (now_id, e["id"]))
        if picked:
            self.db.conn.commit()
        return picked

    def prompt_block(self, gid: int, recent_text: str) -> str:
        es = self.active(gid, recent_text)
        if not es:
            return ""
        return ("# 群里的梗（刚聊到的，知道它是什么意思就行；刚提过的别再硬提）\n"
                + "\n".join(f"- {'/'.join(e['keys'][:3])}：{e['content']}" for e in es))

    def sync_auto(self, gid: int, jargon: list[dict], memes: list[tuple[list[str], str]]) -> int:
        n = 0
        for j in jargon:
            if self.add(gid, [j["term"]], f"群里的说法，意思是{j['meaning']}", source="auto", cooldown=15):
                n += 1
        for keys, text in memes:
            if self.add(gid, keys, text, source="auto"):
                n += 1
        return n
