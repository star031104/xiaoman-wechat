"""跟着群里学（1.4）：表达方式 + 黑话。做法参考 MaiBot 的 expression_learner / jargon_miner，和 AstrBot self_learning 的审核队列。

表达方式（qq_expressions）：“当 X 时，可以说 Y”。每整理一次群聊，从群友（不含她自己）的话里学 3~5 条；同一条再出现就计数 +1。
    聊天时：出现过 2 次以上、或在小屋里点了“用”的，挑 3 条最常见的 + 3 条随机的，提示她“视情况自然地用”。
黑话（qq_jargon）：缩写、群里反复用的词。每出现一次计数 +1；计数到 4 / 8 / 25 / 100 时猜一次意思：
    一次只看词本身猜、一次看上下文猜，两次意思明显不一样才算“真黑话”（离开这个群就看不懂）。
    聊天时：最近的聊天里出现了的真黑话（最多 10 条）告诉她意思，注明“仅供理解，不一定对”。
小屋“学到的”页可以 用 / 不用 / 改 / 删。点了“不用”的永远不会进提示词。

2.0：
- 表达方式按相似度合并（以前按原文一字不差去重，大模型每次措辞都不一样，计数永远到不了 2，自动学到的一条都用不上）
- 记下是哪些人说的：至少 2 个不同的人用过才自动用（一个人刷屏教不坏她）；黑话也一样
- 大模型调用成功了才往前挪“学到哪了”（以前失败了这批消息就跳过了）
"""
from __future__ import annotations

import json
import logging
import random
import re
import time
from difflib import SequenceMatcher

from aigroup.core.config import chan_name
from aigroup.core.store import ensure_columns

log = logging.getLogger("aigroup.learn")

SCHEMA = """
CREATE TABLE IF NOT EXISTS qq_expressions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id INTEGER NOT NULL,
    situation TEXT NOT NULL,
    style TEXT NOT NULL,
    count INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'new',      -- new（自动）/ ok（点了用）/ no（点了不用）
    created REAL, updated REAL,
    UNIQUE(group_id, situation, style)
);
CREATE TABLE IF NOT EXISTS qq_jargon (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id INTEGER NOT NULL,
    term TEXT NOT NULL,
    count INTEGER NOT NULL DEFAULT 0,
    meaning TEXT NOT NULL DEFAULT '',
    guess TEXT NOT NULL DEFAULT '',           -- 只看词本身猜的意思
    examples TEXT NOT NULL DEFAULT '[]',      -- 最近几句用到它的话
    is_jargon INTEGER NOT NULL DEFAULT -1,    -- -1 还没判断 / 0 普通词 / 1 真黑话
    checked_at INTEGER NOT NULL DEFAULT 0,    -- 上次在计数多少时猜过
    status TEXT NOT NULL DEFAULT 'new',
    created REAL, updated REAL,
    UNIQUE(group_id, term)
);
"""
CHECKPOINTS = (4, 8, 25, 100)
MIN_USERS = 2


def similar(a: str, b: str) -> float:
    a, b = re.sub(r"\W", "", a or ""), re.sub(r"\W", "", b or "")
    return SequenceMatcher(None, a, b).ratio() if a and b else 0.0


def due_checkpoint(count: int, checked_at: int) -> int | None:
    """计数越过了哪个还没猜过的节点（4/8/25/100）。"""
    hit = [c for c in CHECKPOINTS if checked_at < c <= count]
    return hit[-1] if hit else None


class Learner:
    def __init__(self, db, brain):
        self.db, self.brain = db, brain
        db.conn.executescript(SCHEMA)
        for table in ("qq_expressions", "qq_jargon"):
            ensure_columns(db.conn, table, {"users": "TEXT NOT NULL DEFAULT '[]'"})   # 2.0：哪些人说过
        ensure_columns(db.conn, "qq_jargon", {"by_user": "TEXT NOT NULL DEFAULT '{}'"})   # 20.2 不同人的不同用法
        db.conn.commit()

    @property
    def llm(self):
        return self.brain.llm

    @property
    def persona(self):
        return self.brain.persona

    # ---------------- 学 ----------------
    def new_lines(self, gid: int, limit: int = 80) -> tuple[list[dict], int]:
        """上次学过以后，群友说的话（不含她自己、不含系统占位）。"""
        last = int(self.db.get(f"qq_learn_last_{gid}", 0) or 0)
        rows = self.db.conn.execute(
            "SELECT id, user_id, name, content FROM qq_group_messages WHERE group_id=? AND role='user' AND id>? "
            "ORDER BY id DESC LIMIT ?", (gid, last, limit)).fetchall()
        rows = list(reversed(rows))
        lines = [dict(r) for r in rows if len(re.sub(r"\[[^\]]*\]|@\S+", "", r["content"]).strip()) >= 2]
        return lines, (rows[-1]["id"] if rows else last)

    async def learn(self, gid: int) -> dict:
        lines, last_id = self.new_lines(gid)
        if len(lines) < 8:
            return {"styles": 0, "terms": 0}
        text = "\n".join(f"[{i + 1}] {r['name']}：{r['content'][:120]}" for i, r in enumerate(lines))
        prompt = f"""下面是一个{chan_name()}最近的聊天（都是群友说的）。帮{self.persona.name}学这个群的说话方式。

1. styles：学 3~5 条“在什么情况下，大家会怎么说”。
   - situation 写场景（比如“对某件事表示很惊讶”“催别人快点”），style 写具体说法（比如“用 我嘞个xxx”“说 速速”）
   - 不要人名、不要具体的事物名词，要能换个话题也用得上；每个字段不超过 20 个字
   - 只学有特色的说法，普通的“好的”“哈哈哈”不要
   - from 写这种说法出现在哪几句（方括号里的编号）
2. terms：挑出可能是这个群的“黑话”的词：拼音首字母缩写（yyds、xswl）、英文缩写、中文缩略、群里反复用的梗词。
   没有就给空列表。每个词 1~10 个字，不要人名。

只输出 JSON：{{"styles": [{{"situation": "...", "style": "...", "from": [3, 7]}}], "terms": ["..."]}}

# 聊天
{text[-3500:]}"""
        data = await self.llm.json([{"role": "user", "content": prompt}], default=None, role="memory")
        if not isinstance(data, dict):                    # 失败了：不往前挪，下次连这批一起学
            return {"styles": 0, "terms": 0}
        self.db.set(f"qq_learn_last_{gid}", last_id)
        now = time.time()
        ns = 0
        existing = [dict(r) for r in self.db.conn.execute(
            "SELECT id, situation, style, users FROM qq_expressions WHERE group_id=?", (gid,)).fetchall()]
        for s in (data.get("styles") or [])[:6]:
            if not isinstance(s, dict):
                continue
            sit, sty = str(s.get("situation") or "").strip()[:30], str(s.get("style") or "").strip()[:30]
            if len(sit) < 2 or len(sty) < 2:
                continue
            src = [int(x) for x in (s.get("from") or []) if str(x).isdigit()]
            uids = {int(lines[i - 1]["user_id"]) for i in src if 1 <= i <= len(lines)}
            hit = next((e for e in existing if similar(e["style"], sty) >= 0.8 and similar(e["situation"], sit) >= 0.5), None)
            if hit:
                users = sorted(set(json.loads(hit["users"] or "[]")) | uids)
                self.db.conn.execute("UPDATE qq_expressions SET count=count+1, users=?, updated=? WHERE id=?",
                                     (json.dumps(users), now, hit["id"]))
                hit["users"] = json.dumps(users)
            else:
                cur = self.db.conn.execute(
                    "INSERT OR IGNORE INTO qq_expressions(group_id, situation, style, users, created, updated) "
                    "VALUES (?,?,?,?,?,?)", (gid, sit, sty, json.dumps(sorted(uids)), now, now))
                existing.append({"id": cur.lastrowid, "situation": sit, "style": sty, "users": json.dumps(sorted(uids))})
            ns += 1
        nt = 0
        joined = [r["content"] for r in lines]
        for t in (data.get("terms") or [])[:15]:
            term = str(t or "").strip()[:12]
            if not term or len(term) > 10 or term in (self.persona.name,):
                continue
            hits = [c for c in joined if term.lower() in c.lower()]
            n = sum(c.lower().count(term.lower()) for c in hits)
            # 20.2 例句带上是谁说的：同一个词不同人意思可能不一样（GroupMemBench：这一项最强的系统也只有 37.7%）
            hits = [(f"{r['name']}：" if "name" in r.keys() and r["name"] else "") + r["content"]
                    for r in lines if term.lower() in r["content"].lower()]
            if not n:
                continue
            uids = {int(r["user_id"]) for r in lines if term.lower() in r["content"].lower()}
            old = self.db.conn.execute("SELECT examples, users FROM qq_jargon WHERE group_id=? AND term=?",
                                       (gid, term)).fetchone()
            ex = json.loads(old["examples"]) if old and old["examples"] else []
            ex = (ex + [h[:80] for h in hits])[-4:]
            users = sorted(set(json.loads(old["users"] or "[]") if old else []) | uids)[-30:]
            self.db.conn.execute(
                "INSERT INTO qq_jargon(group_id, term, count, examples, users, created, updated) VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT(group_id, term) DO UPDATE SET count=count+excluded.count, examples=excluded.examples, "
                "users=excluded.users, updated=excluded.updated",
                (gid, term, n, json.dumps(ex, ensure_ascii=False), json.dumps(users), now, now))
            nt += 1
        self.db.conn.commit()
        log.info("群 %s：学到 %d 条说法、%d 个词", gid, ns, nt)
        await self.infer_jargon(gid)
        return {"styles": ns, "terms": nt}

    async def infer_jargon(self, gid: int, limit: int = 6) -> int:
        """计数越过 4/8/25/100 的词：两路推断，判断是不是真黑话。"""
        rows = [r for r in self.db.conn.execute(
            "SELECT * FROM qq_jargon WHERE group_id=? AND status!='no' AND checked_at<100 ORDER BY count DESC",
            (gid,)).fetchall() if due_checkpoint(r["count"], r["checked_at"])][:limit]
        if not rows:
            return 0
        terms = [r["term"] for r in rows]
        a = await self.llm.json([{"role": "user", "content":
            "下面这些词，不看任何上下文，只凭字面和常识，你觉得各是什么意思？不知道就写“不知道”。每个 20 字以内。\n"
            "只输出 JSON：{\"词\": \"意思\"}\n\n" +"\n".join(f"- {t}" for t in terms)}], default={}, role="memory")
        a = a if isinstance(a, dict) else {}
        blocks = []
        for r in rows:
            ex = "\n".join(f"  · {x}" for x in json.loads(r["examples"] or "[]"))
            blocks.append(f"## {r['term']}\n不看上下文时的猜测：{a.get(r['term'], '不知道')}\n群里用到它的话：\n{ex}")
        b = await self.llm.json([{"role": "user", "content":
            f"下面是一个{chan_name()}里的词，和它们在群里被用到的句子。根据句子判断每个词在这个群里是什么意思（20 字以内），"
            "并判断：和“不看上下文时的猜测”比，意思是不是明显不一样（明显不一样＝离开这个群就看不懂，才算群里的黑话）。\n"
            "例句前面是说话的人；要是不同的人用这个词意思明显不一样，在 by 里按人写（一样就不写 by）。\n"
            '只输出 JSON：{"词": {"meaning": "...", "differs": true, "by": {"某人": "他用它指……"}}}\n\n'
            + "\n\n".join(blocks)}], default={}, role="memory")
        b = b if isinstance(b, dict) else {}
        n = 0
        for r in rows:
            v = b.get(r["term"])
            if not isinstance(v, dict):
                continue
            cp = due_checkpoint(r["count"], r["checked_at"]) or r["checked_at"]
            by = {str(k)[:12]: str(x)[:30] for k, x in (v.get("by") or {}).items()} if isinstance(v.get("by"), dict) else {}
            self.db.conn.execute(
                "UPDATE qq_jargon SET meaning=?, guess=?, is_jargon=?, checked_at=?, updated=?, by_user=? WHERE id=?",
                (str(v.get("meaning") or "")[:40], str(a.get(r["term"]) or "")[:40], 1 if v.get("differs") else 0,
                 cp, time.time(), json.dumps(dict(list(by.items())[:4]), ensure_ascii=False), r["id"]))
            n += 1
        self.db.conn.commit()
        return n

    # ---------------- 用 ----------------
    def expressions_for(self, gid: int, k_top: int = 3, k_rand: int = 3) -> list[dict]:
        rows = [dict(r) for r in self.db.conn.execute(
            "SELECT id, situation, style, count, status, users FROM qq_expressions WHERE group_id=? AND status!='no' "
            "AND (status='ok' OR count>=2) ORDER BY count DESC, updated DESC", (gid,)).fetchall()]
        rows = [r for r in rows if r["status"] == "ok" or len(json.loads(r["users"] or "[]")) >= MIN_USERS]
        top, rest = rows[:k_top], rows[k_top:]
        rnd = random.Random(f"{gid}-{int(time.time() // 3600)}")      # 随机的那几条每小时才换：同一小时内提示词不变，缓存能命中
        return top + (rnd.sample(rest, min(k_rand, len(rest))) if rest else [])

    def jargon_in(self, gid: int, text: str, k: int = 10) -> list[dict]:
        rows = self.db.conn.execute(
            "SELECT term, meaning, status, users, by_user FROM qq_jargon WHERE group_id=? AND status!='no' AND meaning!='' "
            "AND (is_jargon=1 OR status='ok') ORDER BY count DESC", (gid,)).fetchall()
        low = (text or "").lower()

        def by(r):
            try:
                d = json.loads(r["by_user"] or "{}")
            except (ValueError, TypeError):
                d = {}
            return d if isinstance(d, dict) else {}
        return [{"term": r["term"], "meaning": r["meaning"], "by": by(r)} for r in rows if r["term"].lower() in low
                and (r["status"] == "ok" or len(json.loads(r["users"] or "[]")) >= MIN_USERS)][:k]

    def prompt_block(self, gid: int, recent_text: str) -> str:
        parts = []
        ex = self.expressions_for(gid)
        if ex:
            parts.append("# 这个群的说话习惯（视情况自然地用，不用硬套，一次最多用一个）\n"
                         + "\n".join(f"- 当{e['situation']}时，可以{e['style'] if e['style'].startswith(('用', '说')) else '说“' + e['style'] + '”'}"
                                     for e in ex))
        jg = self.jargon_in(gid, recent_text)
        if jg:
            parts.append("# 群里的黑话（下面的意思是你猜的，仅供理解聊天，不一定对）\n"
                         + "\n".join(f"- {j['term']}：{j['meaning']}" + (
                             "（不同人用法不一样：" + "；".join(f"{k}用它指{v}" for k, v in j["by"].items()) + "，按在聊的人的意思理解）"
                             if j.get("by") else "") for j in jg))
        return "\n\n".join(parts)
