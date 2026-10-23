"""20.2 “刷到的”重做：按群友在意的话题攒候选、同一件事只分享一次、先用不花钱的规则打分，过线才让模型写话。

以前（13.2～19.x）：每次想分享都先问模型“挑个话题、写个搜索词”→ 搜 → 再问模型“值不值得分享”，每次两三次调用，
而且不知道群友到底在意什么，同一件事换个标题还会再说一遍。

现在：
1. 兴趣表（feed_interests_{gid}）：群友画像里的“兴趣 / 在意”（几个人都在意的权重高）+ 她自己的兴趣（权重低一点）+ 她查过、被聊到多的话题。
   每晚重算；分享后有没有人接会回过头调权重（接得好 +0.1，冷场 −0.1，最低 0.1）。小屋“学到的”能看能删。
   依据：Ask Now, Use Later（2026-05）：让智能体问用户偏好，问中的只有 6~10%；从聊天里抽更靠谱。
2. 候选池（qq_feed）：每 6 小时在后台给权重前几的话题各搜一次最近 3 天的新闻（DeepSeek 搜索约 ¥0.005 / 次），不调大模型。
3. 按事件去重：向量（bge-m3）余弦 > 0.82、或者标题字二元组重合 > 0.5，10 天内算同一件事（event_id）。
   依据：实时新闻事件识别（2025-08）：BGE-M3 + 实体聚类 AMI 0.84。分享过的事件再来新消息，只有出现新数字才算“后续”。
4. 打分（规则）：兴趣权重 × 新鲜度（半衰期 1 天）× 新颖度（新事件 1、后续 0.5）。过线（feed_min，默认 0.25）才进下一步：
   照旧让模型判断值不值得、写一句。依据：主动触发研究（2026）：小模型 / 规则决定“要不要推”比大模型准，推送精度 15% → 57%。
5. 一点意外：每 5 次里有 1 次挑权重低的话题（SOLAR, EMNLP 2025 Findings 的“相关 + 意外”）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import time

log = logging.getLogger("aigroup.group")

SCHEMA = """
CREATE TABLE IF NOT EXISTS qq_feed (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id INTEGER NOT NULL,
    topic TEXT NOT NULL,
    title TEXT NOT NULL,
    url TEXT NOT NULL DEFAULT '',
    date TEXT NOT NULL DEFAULT '',
    snippet TEXT NOT NULL DEFAULT '',
    vec BLOB,
    event_id INTEGER,
    shared INTEGER NOT NULL DEFAULT 0,
    ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_qq_feed ON qq_feed(group_id, ts);
"""
GENERIC = re.compile(r"他|她|有人|被|最近|话题|工具|^比赛$|^游戏$|^东西$|很感兴趣|感兴趣|^聊天$|想")
SPLIT = re.compile(r"[、，,/；;：:\s]+|和|与|及")


def _bg(t: str) -> set:
    t = re.sub(r"\W+", "", t or "")
    return {t[i:i + 2] for i in range(len(t) - 1)}


def _nums(t: str) -> set:
    return set(re.findall(r"\d+(?:[.:]\d+)?%?", t or ""))


def age_days(date: str, today) -> float | None:
    try:
        from datetime import date as _d
        y, m, d = (int(x) for x in str(date)[:10].split("-"))
        return max(0.0, float((today - _d(y, m, d)).days))
    except (ValueError, TypeError):
        return None


class FeedMixin:
    def _feed_init(self):
        if not getattr(self, "_feed_ok", False):
            self.db.conn.executescript(SCHEMA)
            self.db.conn.commit()
            self._feed_ok = True

    # ---------------- 兴趣表 ----------------
    def build_interests(self, gid: int) -> list[dict]:
        """群友画像的兴趣 / 在意 + 她自己的兴趣 + 她被问得多的话题 → [{topic, who, w}]（每晚算一次）。"""
        from collections import defaultdict
        score, who = defaultdict(float), defaultdict(set)
        try:
            for r in self.db.conn.execute("SELECT p.user_id, p.name, g.profile FROM qq_people p JOIN qq_profiles g "
                                          "ON g.user_id=p.user_id WHERE g.group_id=?", (gid,)).fetchall():
                try:
                    prof = json.loads(r["profile"] or "{}")
                except ValueError:
                    continue
                for field in ("兴趣",):          # “在意”写的是他图什么（被认可、有人接梗），不是能搜的话题
                    raw = re.sub(r"[（(][^）)]*[）)]?", "", str(prof.get(field) or ""))   # 括号里的补充说明不当话题
                    for t in SPLIT.split(raw):
                        t = re.sub(r"^(爱|喜欢|常|玩|看|聊)", "", t.strip("。.的 "))
                        if 2 <= len(t) <= 8 and not GENERIC.search(t):
                            score[t] += 1.0
                            who[t].add(str(r["name"] or ""))
        except Exception as e:  # noqa: BLE001
            log.info("群 %s 整理兴趣出错：%s", gid, e)
        for t in (self.interests() if hasattr(self, "interests") else [])[:15]:
            if 2 <= len(t) <= 8 and not GENERIC.search(t):
                score[t] += 0.5
        kn = getattr(self, "knowledge", None)
        if kn is not None:
            for e in sorted(kn.entries(gid, 60), key=lambda e: -int(e["hits"] or 0))[:8]:
                if int(e["hits"] or 0) >= 2 and 2 <= len(e["topic"]) <= 10:
                    score[e["topic"]] += 0.3
        learned = self.db.get(f"feed_w_{gid}") or {}
        removed = set(self.db.get(f"feed_removed_{gid}") or [])
        top = max(score.values()) if score else 1.0
        out = []
        for t, v in score.items():
            if t in removed:
                continue
            w = min(1.0, v / top) * float(learned.get(t, 1.0))
            out.append({"topic": t, "who": sorted(x for x in who[t] if x)[:4], "w": round(max(0.1, w), 3)})
        out.sort(key=lambda x: -x["w"])
        self.db.set(f"feed_interests_{gid}", out[:30])
        return out[:30]

    def feed_interests(self, gid: int) -> list[dict]:
        got = self.db.get(f"feed_interests_{gid}")
        return got if isinstance(got, list) else self.build_interests(gid)

    def remove_interest(self, gid: int, topic: str):
        rm = list(self.db.get(f"feed_removed_{gid}") or [])
        if topic not in rm:
            rm.append(topic)
        self.db.set(f"feed_removed_{gid}", rm[-100:])
        self.db.set(f"feed_interests_{gid}", [x for x in self.feed_interests(gid) if x.get("topic") != topic])

    def learn_interest(self, gid: int, topic: str, score: float):
        w = dict(self.db.get(f"feed_w_{gid}") or {})
        cur = float(w.get(topic, 1.0))
        w[topic] = round(max(0.1, min(1.5, cur + (0.1 if score >= 0.6 else -0.1 if score < 0.2 else 0))), 2)
        self.db.set(f"feed_w_{gid}", w)

    # ---------------- 候选池 ----------------
    def feed_tick(self, gid: int, now: float):
        """idle_tick 里每分钟调：每 feed_hours 小时（默认 6）在后台刷一次。"""
        g = self.gk(gid)
        if not g.get("feed", True) or not getattr(self, "web", None) or not self.web.enabled:
            return
        k = f"feed_last_{gid}"
        if now - float(self.db.get(k, 0) or 0) < float(g.get("feed_hours", 6)) * 3600:
            return
        if not 8 <= self.brain.now().hour < 23:
            return
        self.db.set(k, now)
        self._spawn(self.feed_refresh(gid))

    async def feed_refresh(self, gid: int, k: int | None = None) -> int:
        self._feed_init()
        g = self.gk(gid)
        ints = self.feed_interests(gid)
        k = k or int(g.get("feed_topics", 5))
        rr = int(self.db.get(f"feed_rr_{gid}", 0) or 0)           # 轮着来：每次刷前几个里的一部分 + 一个靠后的
        pool = ints[:max(k * 2, 6)]
        if not pool:
            return 0
        pick = [pool[(rr + i) % len(pool)] for i in range(min(k, len(pool)))]
        self.db.set(f"feed_rr_{gid}", rr + k)
        n = 0
        for it in pick:
            try:
                res, _ = await self.web.agent_search(f"{it['topic']} 最新", 5, days=int(g.get("share_fresh_days", 3) or 3),
                                                     prefer="deepseek")   # 后台刷得多：用便宜的，别吃掉 Tavily 免费额度
            except Exception as e:  # noqa: BLE001
                log.info("群 %s 刷「%s」出错：%s", gid, it["topic"], e)
                continue
            for r in res:
                if await self._feed_add(gid, it["topic"], r):
                    n += 1
        self.db.conn.execute("DELETE FROM qq_feed WHERE ts<?", (time.time() - 20 * 86400,))
        self.db.conn.commit()
        if n:
            log.info("群 %s：刷到 %d 条新的", gid, n)
        return n

    async def _feed_add(self, gid: int, topic: str, r: dict) -> bool:
        title = str(r.get("title") or "").strip()
        if len(title) < 4:
            return False
        if self.db.conn.execute("SELECT 1 FROM qq_feed WHERE group_id=? AND (url=? OR title=?) LIMIT 1",
                                (gid, r.get("url") or "-", title)).fetchone():
            return False
        vec = None
        emb = getattr(getattr(self, "memory", None), "embedder", None)
        if emb is not None:
            try:
                import numpy as np  # type: ignore
                v = (await asyncio.to_thread(emb.encode, [title + "。" + str(r.get("snippet") or "")[:200]]))[0]
                vec = np.asarray(v, dtype="float32").tobytes()
            except Exception:  # noqa: BLE001
                vec = None
        cur = self.db.conn.execute(
            "INSERT INTO qq_feed(group_id, topic, title, url, date, snippet, vec, ts) VALUES (?,?,?,?,?,?,?,?)",
            (gid, topic, title[:120], str(r.get("url") or ""), str(r.get("date") or ""), str(r.get("snippet") or "")[:400],
             vec, time.time()))
        fid = int(cur.lastrowid)
        self.db.conn.execute("UPDATE qq_feed SET event_id=? WHERE id=?", (self._event_of(gid, fid, title, vec), fid))
        self.db.conn.commit()
        return True

    def _event_of(self, gid: int, fid: int, title: str, vec) -> int:
        """10 天内向量余弦 > 0.82 或标题字二元组 Jaccard > 0.5 → 同一件事。"""
        rows = self.db.conn.execute("SELECT id, title, vec, event_id FROM qq_feed WHERE group_id=? AND id!=? AND ts>?",
                                    (gid, fid, time.time() - 10 * 86400)).fetchall()
        b = _bg(title)
        try:
            import numpy as np  # type: ignore
            v = np.frombuffer(vec, dtype="float32") if vec else None
        except Exception:  # noqa: BLE001
            v = None
        for r in rows:
            if v is not None and r["vec"]:
                try:
                    import numpy as np  # type: ignore
                    u = np.frombuffer(r["vec"], dtype="float32")
                    if len(u) == len(v):
                        cos = float(u @ v) / (float(np.linalg.norm(u) * np.linalg.norm(v)) or 1.0)
                        if cos > 0.82:
                            return int(r["event_id"] or r["id"])
                except Exception:  # noqa: BLE001
                    pass
            ob = _bg(r["title"])
            if b and ob and len(b & ob) / len(b | ob) > 0.5:
                return int(r["event_id"] or r["id"])
        return fid

    # ---------------- 挑一条 ----------------
    def feed_candidates(self, gid: int, today=None) -> list[dict]:
        self._feed_init()
        today = today or self.brain.now().date()
        w = {x["topic"]: float(x["w"]) for x in self.feed_interests(gid)}
        fresh_days = int(self.gk(gid).get("share_fresh_days", 3) or 3)
        rows = [dict(r) for r in self.db.conn.execute(
            "SELECT id, topic, title, url, date, snippet, event_id, shared FROM qq_feed WHERE group_id=? AND ts>?",
            (gid, time.time() - 10 * 86400)).fetchall()]
        shared_ev: dict[int, set] = {}
        for r in rows:
            if r["shared"]:
                shared_ev.setdefault(int(r["event_id"] or r["id"]), set()).update(_nums(r["title"] + r["snippet"]))
        out = []
        for r in rows:
            if r["shared"]:
                continue
            age = age_days(r["date"], today)
            if age is None or age > fresh_days:
                continue
            ev = int(r["event_id"] or r["id"])
            if ev in shared_ev:
                novelty = 0.5 if (_nums(r["title"] + r["snippet"]) - shared_ev[ev]) else 0.0
            else:
                novelty = 1.0
            if not novelty:
                continue
            sc = float(w.get(r["topic"], 0.2)) * math.pow(0.5, age) * novelty
            out.append({**r, "age": age, "novelty": novelty, "score": round(sc, 3), "w": w.get(r["topic"], 0.2)})
        out.sort(key=lambda x: -x["score"])
        return out

    def feed_has_pool(self, gid: int, days: float = 3) -> bool:
        self._feed_init()
        return bool(self.db.conn.execute("SELECT 1 FROM qq_feed WHERE group_id=? AND ts>? LIMIT 1",
                                         (gid, time.time() - days * 86400)).fetchone())

    def feed_pick(self, gid: int) -> dict | None:
        cands = self.feed_candidates(gid)
        if not cands:
            return None
        n = int(self.db.get(f"feed_picks_{gid}", 0) or 0)
        self.db.set(f"feed_picks_{gid}", n + 1)
        if n % 5 == 4:                                     # 每 5 次留 1 次给权重低的话题（一点意外）
            low = [c for c in cands if c["w"] <= 0.5 and c["novelty"] == 1.0]
            if low:
                return low[0]
        best = cands[0]
        return best if best["score"] >= float(self.gk(gid).get("feed_min", 0.25)) else None

    def feed_mark(self, gid: int, item: dict):
        ev = int(item.get("event_id") or item.get("id"))
        self.db.conn.execute("UPDATE qq_feed SET shared=1 WHERE group_id=? AND (id=? OR event_id=?) AND ts<=?",
                             (gid, int(item["id"]), ev, time.time()))
        self.db.conn.commit()

    def feed_learn(self, days: float = 3) -> int:
        """分享之后 2 分钟内她那句的接话效果 → 调兴趣权重。每晚跑一次。"""
        n = 0
        done = set(self.db.get("feed_learned") or [])
        for x in (self.db.get("qq_shares") or []):
            if x.get("kind") != "news" or not x.get("feed") or float(x.get("ts") or 0) < time.time() - days * 86400:
                continue
            key = f"{x.get('gid')}:{int(float(x['ts']))}"
            if key in done:
                continue
            r = self.db.conn.execute("SELECT score FROM qq_reply_effect WHERE group_id=? AND mentioned=0 AND done=1 "
                                     "AND ts BETWEEN ? AND ? ORDER BY ts LIMIT 1",
                                     (x["gid"], float(x["ts"]) - 5, float(x["ts"]) + 180)).fetchone()
            if r is None or r["score"] is None:
                continue
            self.learn_interest(int(x["gid"]), str(x.get("topic") or ""), float(r["score"]))
            done.add(key)
            n += 1
        self.db.set("feed_learned", list(done)[-300:])
        return n
