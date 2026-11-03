"""35.0 带出处的滚动摘要（“这段时间群里聊了什么”，每条要点挂着原话的消息编号）。

以前记得的东西有三种：一句一句的事（facts，太碎）、每天的日报看点（一天一两条，太粗）、聊天原文（只有最近几十条）。
中间缺一层“那天晚上大家聊了什么、谁说了什么”。这里每攒够 episode_size 条（或者聊完停了 30 分钟）就用便宜模型
把这一段写成 1~4 条要点，每条要点写上能证明它的原话编号（#id）。之后聊到相关的事（词对得上，或者说了“上周 / 前几天”），
把要点连同一句原话一起放进提示词——她翻旧账时能引原话，不用编。

思路来自 MemGPT / Letta 的递归摘要和 LongMemEval 里“按段存 + 带时间”的做法；编号能对回原文是这里自己加的（防瞎引）。
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime

log = logging.getLogger("aigroup.episodes")

SCHEMA = """
CREATE TABLE IF NOT EXISTS qq_episodes (       -- 35.0 一段聊天的要点（带原话编号）
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id INTEGER NOT NULL,
    a_id INTEGER NOT NULL,        -- 这一段第一条消息的 id
    b_id INTEGER NOT NULL,        -- 最后一条
    a_ts REAL, b_ts REAL,
    speakers TEXT DEFAULT '',     -- 说过话的人（编号，逗号隔开）
    lines TEXT DEFAULT '[]',      -- [{"t": 要点, "ids": [原话 id]}]
    created REAL
);
CREATE INDEX IF NOT EXISTS qq_episodes_g ON qq_episodes(group_id, b_id);
"""
WEEK = "一二三四五六日"
LINE_RX = re.compile(r"^\s*(?:[-*•\d.、)]\s*)?(.{4,80}?)\s*[|｜]\s*((?:#?\d+\s*[,，、 ]?\s*)+)\s*$")


def parse_lines(out: str, ok_ids: set[int]) -> list[dict]:
    res = []
    for ln in str(out or "").splitlines():
        m = LINE_RX.match(ln.strip())
        if not m:
            continue
        ids = [int(x) for x in re.findall(r"\d+", m.group(2)) if int(x) in ok_ids][:2]
        t = m.group(1).strip(" ：:。")
        if ids and t:
            res.append({"t": t[:60], "ids": ids})
    return res[:4]


def part_of_day(ts: float) -> str:
    h = datetime.fromtimestamp(ts).hour
    return "凌晨" if h < 6 else "上午" if h < 11 else "中午" if h < 14 else "下午" if h < 18 else "晚上"


class EpisodesMixin:
    def _episodes_init(self):
        self.db.conn.executescript(SCHEMA)
        self.db.conn.commit()

    def _ep_last(self, gid: int) -> int:
        r = self.db.conn.execute("SELECT IFNULL(MAX(b_id),0) m FROM qq_episodes WHERE group_id=?", (gid,)).fetchone()
        return int(r["m"] or 0)

    def episodes_due(self, gid: int, now: float | None = None) -> list[dict]:
        """该写摘要的那一段（没到就返回空）：攒够 episode_size 条，或者至少 10 条且最后一条已经过去 30 分钟。"""
        g = self.gk(gid)
        if not g.get("episodes", True):
            return []
        now = now or time.time()
        size = int(g.get("episode_size", 40) or 40)
        last = self._ep_last(gid)
        if not last:                                    # 第一次：只往回补最近 600 条，不把全部历史都过一遍
            top = self.db.conn.execute("SELECT IFNULL(MAX(id),0) m FROM qq_group_messages WHERE group_id=?",
                                       (gid,)).fetchone()["m"]
            last = max(0, int(top or 0) - 600)
        rows = [dict(r) for r in self.db.conn.execute(
            "SELECT id, ts, user_id, name, role, content FROM qq_group_messages WHERE group_id=? AND id>? ORDER BY id LIMIT ?",
            (gid, last, size * 2)).fetchall()]
        if len(rows) >= size:
            return rows[:size]
        if len(rows) >= 10 and now - float(rows[-1]["ts"]) > 1800:
            return rows
        return []

    async def episodes_tick(self, gid: int) -> int:
        """写一段摘要。返回写了几条要点（没到时候 / 失败 = 0）。"""
        if self.__dict__.get("_ep_busy"):
            return 0
        rows = self.episodes_due(gid)
        if not rows:
            return 0
        self._ep_busy = True
        try:
            return await self._episode_write(gid, rows)
        finally:
            self._ep_busy = False

    async def _episode_write(self, gid: int, rows: list[dict]) -> int:
        from aigroup.core.safety import clean_member_text
        p = self.brain.persona
        talk = "\n".join(f"#{r['id']} {p.name if r['role'] == 'assistant' else r['name']}：{clean_member_text(r['content'], 80)}"
                         for r in rows if str(r["content"] or "").strip())
        a, b = rows[0], rows[-1]
        prompt = (f"下面是一个群聊里的一段聊天（{datetime.fromtimestamp(a['ts']):%m-%d %H:%M} 到 {datetime.fromtimestamp(b['ts']):%H:%M}），"
                  f"每行开头 #数字 是这条消息的编号，“{p.name}”是群里的 AI。\n"
                  "用 1~4 行写这段里值得以后想起来的事，每行格式：要点|编号,编号\n"
                  "- 要点写清是谁、做了 / 说了 / 定了什么（用名字，带上具体的东西：时间、地点、比分、数字），40 字以内\n"
                  "- 编号是能证明这条要点的原话编号，1~2 个，只能用上面出现过的\n"
                  "- 寒暄、刷表情、没内容的闲聊不写；开玩笑、反话别当真事写；这一段没什么可记的就只输出：无\n\n" + talk)
        try:
            out = await self.brain.llm.chat([{"role": "user", "content": prompt}], role="memory", background=True)
        except Exception as e:  # noqa: BLE001
            log.info("群 %s：这段聊天的摘要没写成：%s", gid, e)
            return 0
        lines = parse_lines(out, {int(r["id"]) for r in rows})
        spk = sorted({int(r["user_id"]) for r in rows if r["role"] == "user" and r["user_id"]})
        self.db.conn.execute("INSERT INTO qq_episodes(group_id, a_id, b_id, a_ts, b_ts, speakers, lines, created) "
                             "VALUES(?,?,?,?,?,?,?,?)",
                             (gid, a["id"], b["id"], a["ts"], b["ts"], ",".join(map(str, spk)),
                              json.dumps(lines, ensure_ascii=False), time.time()))
        self.db.conn.commit()
        self.__dict__.setdefault("_ep_cache", {}).pop(gid, None)
        log.info("群 %s：#%s~#%s 写了 %d 条要点", gid, a["id"], b["id"], len(lines))
        return len(lines)

    def _ep_items(self, gid: int) -> list[dict]:
        cache = self.__dict__.setdefault("_ep_cache", {})
        top = self._ep_last(gid)
        hit = cache.get(gid)
        if hit and hit[0] == top:
            return hit[1]
        items = []
        for r in self.db.conn.execute("SELECT id, a_id, b_id, a_ts, b_ts, lines FROM qq_episodes WHERE group_id=? "
                                      "ORDER BY id DESC LIMIT 600", (gid,)).fetchall():
            try:
                ls = json.loads(r["lines"] or "[]")
            except ValueError:
                ls = []
            for x in ls:
                items.append({"t": x.get("t", ""), "ids": x.get("ids") or [], "b_id": r["b_id"], "ts": float(r["b_ts"] or 0)})
        cache[gid] = (top, items)
        return items

    def episodes_block(self, gid: int, query: str, before_id: int | None = None, k: int = 3) -> str:
        """和这句话有关的以前的要点（不含还在聊天记录里的那段），带一句原话。"""
        if not self.gk(gid).get("episodes", True) or not str(query or "").strip():
            return ""
        items = [x for x in self._ep_items(gid) if not before_id or x["b_id"] < before_id]
        if not items:
            return ""
        from aigroup.memory.autobio import range_refs
        rg = range_refs(query, self.brain.now())
        picked: list[dict] = []
        if rg:
            lo, hi = rg[0].timestamp(), rg[1].timestamp()
            picked = [x for x in items if lo <= x["ts"] < hi][:k]
        if not picked:
            from aigroup.chat.checks import content_terms
            from aigroup.memory.facts import BM25, conf, tokens
            bm = BM25([tokens(x["t"]) for x in items])
            sc = bm.scores(tokens(query))
            q = content_terms(query)
            order = sorted(range(len(items)), key=lambda i: -sc[i])[:12]
            for i in order:
                o = q & content_terms(items[i]["t"])
                if conf(sc[i]) >= 0.7 and len(o) >= 2:
                    picked.append(items[i])
                if len(picked) >= k:
                    break
        if not picked:
            return ""
        out = []
        for x in sorted(picked, key=lambda x: x["ts"]):
            d = datetime.fromtimestamp(x["ts"])
            quote = ""
            for mid in x["ids"][:1]:
                r = self.db.conn.execute("SELECT name, role, content FROM qq_group_messages WHERE id=?", (mid,)).fetchone()
                if r and str(r["content"] or "").strip():
                    from aigroup.core.safety import clean_member_text
                    who = self.brain.persona.name if r["role"] == "assistant" else r["name"]
                    quote = f"（原话：{who}“{clean_member_text(r['content'], 40)}”）"
            out.append(f"- {d:%m-%d} 星期{WEEK[d.weekday()]}{part_of_day(x['ts'])}：{x['t']}{quote}")
        return "# 以前群里聊过的（翻旧账、引原话只能照这里和聊天记录说，没有的就说记不清）\n" + "\n".join(out)
