"""她从网上看来、记住的东西（4.0）：群友发的链接点开看、被问到不知道的事先搜一搜，然后把要点记下来。

- 读链接：群友发的网址 / B 站等分享卡片 → 标题、简介、正文开头写进那条消息（她能接着聊，不用假装看过）
- 搜一搜：被叫到、有人在问“××是什么 / 谁 / 讲了什么 / 怎么样 / 最新”这类她可能不知道的事，先让后台模型判断
  “要不要查、查什么”，要查就搜（net/search.py），把搜到的几条摘要给她看
- 记下来：每次搜完让后台模型把要点写成一条“知道的事”（话题、关键词、一两句话），存在 qq_knowledge 表里；
  以后聊到关键词就想起来（像群梗词条一样按关键词命中），常被用到的会变成她的兴趣和谈资——人物就这样一点点长出来
- 每周一回想一遍（weekly_reflect）：这一周学到的、群里聊的、她自己说过的 → 写几句“这一周的我”（兴趣、口味、心态的小变化），
  放进提示词，人物就跟着经历慢慢变，又不会忘了自己是谁
- 小屋“学到的”页能看到、能删
"""
from __future__ import annotations

import json
import logging
import re
import time

from aigroup.memory.lore import key_hit

log = logging.getLogger("aigroup.net")

SCHEMA = """
CREATE TABLE IF NOT EXISTS qq_knowledge (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id INTEGER NOT NULL,
    topic TEXT NOT NULL,
    keys TEXT NOT NULL DEFAULT '[]',
    summary TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'search',
    query TEXT NOT NULL DEFAULT '',
    ts REAL NOT NULL,
    hits INTEGER NOT NULL DEFAULT 0,
    last_used REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS qq_knowledge_g ON qq_knowledge(group_id, ts);
"""

# 看起来是在问一件“可以查到”的事（先用它粗筛，真要不要查再让后台模型判断）
ASK_RE = re.compile(r"(是什么|是啥|啥是|什么是|是谁|谁是|什么意思|啥意思|讲了什么|讲的什么|讲什么|讲啥|说的是什么|"
                    r"好看吗|好玩吗|值得|怎么样|咋样|多少钱|几号|哪天|什么时候|啥时候|上线|发售|更新了|最新|新闻|比分|"
                    r"赛程|结果|攻略|配置|推荐|介绍一下|科普|总结一下|查一下|查查|搜一下|搜搜|百度一下|知道.{1,12}吗|听说过)")

DECIDE_PROMPT = """你帮一个在群里聊天的人判断：下面这几句里，有没有她需要先上网查一下才能接得住的事
（作品、游戏、比赛、新闻、人物、地点、概念、书讲了什么、某样东西多少钱 / 什么时候出……）。
闲聊、问她自己的事、问群里某个人的事、表情和玩笑都不用查。
只输出 JSON：{"need": true 或 false, "query": "要搜的关键词（6~20 个字，像在搜索框里打的）"}

群聊：
"""

LEARN_PROMPT = """把下面搜到的内容整理成一条“她记住的知识”，以后聊到时能用。只写确定的事实，别编，别写网址。
只输出 JSON：{"topic": "话题（2~12 个字）", "keys": ["聊到时会出现的关键词", "…（2~5 个，越具体越好）"],
"summary": "一两句话说清楚（不超过 90 个字）"}

搜的是：%s
搜到的：
%s"""


REFLECT_PROMPT = """你是%s。下面是你这一周在群里的经历。用第一人称写 3~5 句“这一周的我”：
最近迷上 / 了解了什么、口味和看法有什么小变化、和群里谁走得更近了、心情怎么样、接下来想做什么。
要和你的人设一致（只是一点点自然的变化，不要变成另一个人），只写这一周真的发生过的，不编。每句一行，开头用“- ”。

你本来是什么样的人：
%s

上一周你写的（可能没有）：
%s

这一周记住的东西：
%s

群里这一周的事：
%s

你这一周在群里说过的话（节选）：
%s"""


class Knowledge:
    def __init__(self, db):
        self.db = db
        db.conn.executescript(SCHEMA)
        db.conn.commit()

    def add(self, gid: int, topic: str, keys: list[str], summary: str, source: str = "search", query: str = "") -> int:
        topic, summary = (topic or "").strip()[:20], (summary or "").strip()[:160]
        keys = [k.strip()[:16] for k in keys or [] if isinstance(k, str) and 2 <= len(k.strip()) <= 16][:6]
        if not topic or not summary:
            return 0
        if topic not in keys:
            keys.insert(0, topic)
        row = self.db.conn.execute("SELECT id FROM qq_knowledge WHERE group_id=? AND topic=?", (gid, topic)).fetchone()
        now = time.time()
        if row:                                        # 同一个话题：用新的覆盖（新闻、版本这类会变）
            self.db.conn.execute("UPDATE qq_knowledge SET keys=?, summary=?, source=?, query=?, ts=? WHERE id=?",
                                 (json.dumps(keys, ensure_ascii=False), summary, source, query, now, row["id"]))
            kid = row["id"]
        else:
            kid = self.db.conn.execute(
                "INSERT INTO qq_knowledge(group_id, topic, keys, summary, source, query, ts) VALUES (?,?,?,?,?,?,?)",
                (gid, topic, json.dumps(keys, ensure_ascii=False), summary, source, query, now)).lastrowid
        self.db.conn.commit()
        return int(kid)

    def entries(self, gid: int, limit: int = 200) -> list[dict]:
        rows = self.db.conn.execute("SELECT * FROM qq_knowledge WHERE group_id=? ORDER BY ts DESC LIMIT ?",
                                    (gid, limit)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["keys"] = json.loads(d["keys"] or "[]")
            except ValueError:
                d["keys"] = []
            out.append(d)
        return out

    def update(self, kid: int, summary: str | None = None, topic: str | None = None, keys: list | None = None,
               source: str | None = None):
        """改一条（小屋里改、被群友纠正了改）。"""
        sets, args = [], []
        if summary is not None:
            sets.append("summary=?")
            args.append(str(summary).strip()[:160])
        if topic is not None and str(topic).strip():
            sets.append("topic=?")
            args.append(str(topic).strip()[:20])
        if keys is not None:
            ks = [k.strip()[:16] for k in keys if isinstance(k, str) and 2 <= len(k.strip()) <= 16][:6]
            sets.append("keys=?")
            args.append(json.dumps(ks, ensure_ascii=False))
        if source:
            sets.append("source=?")
            args.append(source)
        if not sets:
            return
        sets.append("ts=?")
        args.append(time.time())
        self.db.conn.execute(f"UPDATE qq_knowledge SET {', '.join(sets)} WHERE id=?", (*args, int(kid)))
        self.db.conn.commit()

    def delete(self, kid: int):
        self.db.conn.execute("DELETE FROM qq_knowledge WHERE id=?", (int(kid),))
        self.db.conn.commit()

    def recall(self, gid: int, text: str, k: int = 3) -> list[dict]:
        """最近聊天里提到了哪些她知道的事（关键词命中），最多 k 条；命中算一次“用到”。"""
        if not text:
            return []
        hits = [e for e in self.entries(gid) if any(key_hit(x, text) for x in e["keys"])]
        hits.sort(key=lambda e: (-sum(1 for x in e["keys"] if key_hit(x, text)), -e["ts"]))
        now = time.time()
        for e in hits[:k]:
            if now - float(e["last_used"] or 0) > 600:
                self.db.conn.execute("UPDATE qq_knowledge SET hits=hits+1, last_used=? WHERE id=?", (now, e["id"]))
        self.db.conn.commit()
        return hits[:k]

    def prompt_block(self, gid: int, text: str) -> str:
        es = self.recall(gid, text)
        if not es:
            return ""
        return "# 你知道的（之前看过、了解过的；聊到了就自然地用，别背出来）\n" + "\n".join(
            f"- {e['topic']}：{e['summary']}" for e in es)

    def interests_block(self, gid: int, n: int = 6) -> str:
        """她最近常想起来的东西：变成她的谈资和兴趣（人物慢慢长出来的那一部分）。"""
        rows = self.db.conn.execute(
            "SELECT topic, summary FROM qq_knowledge WHERE group_id=? ORDER BY (hits * 2 + (ts > ?)) DESC, ts DESC LIMIT ?",
            (gid, time.time() - 7 * 86400, n)).fetchall()
        if not rows:
            return ""
        return ("# 你最近在了解、感兴趣的东西（你的谈资，可以偶尔主动聊起，也会影响你的口味）\n"
                + "、".join(r["topic"] for r in rows))


def clean_query(text: str, names: list[str]) -> str:
    t = re.sub(r"\[[^\]]*\]", " ", text or "")
    t = re.sub(r"@\S+", " ", t)
    for n in sorted(names or [], key=len, reverse=True):
        if n:
            t = t.replace(n, " ")
    t = re.sub(r"(你知道|你听说过|有人知道|谁知道|请问|问一下|话说|那个|就是|吗|呢|啊|呀|吧|哈+|？|\?|！|!|，|,|。)", " ", t)
    return re.sub(r"\s+", " ", t).strip()[:30]


class NetMixin:
    """挂在 GroupChat 上：读链接、搜一搜、记下来。用到 self.links / self.web / self.knowledge（在 __init__ 里建）。"""

    async def read_links(self, text: str, links: list[str] | None = None) -> str:
        if not self.g.get("read_links", True) or getattr(self, "links", None) is None:
            return ""
        try:
            return await self.links.enrich(text, links)
        except Exception as e:  # noqa: BLE001
            log.info("读链接出错：%s", e)
            return ""

    async def web_block(self, gid: int, texts: list[str], mentioned: bool) -> str:
        """这一批消息里有她得先查一下的事：搜一搜，返回给她看的一段（不用查就是空）。"""
        if not self.g.get("web_search", True) or getattr(self, "web", None) is None or not self.web.enabled:
            return ""
        recent = [t for t in texts if t][-4:]
        joined = "\n".join(recent)
        if not ASK_RE.search(joined):
            return ""
        if not self._allow("search", int(self.g.get("search_per_hour", 12))):
            log.info("群 %s：这小时搜得够多了，先不搜", gid)
            return ""
        query = ""
        try:
            raw = await self.brain.llm.chat([{"role": "user", "content": DECIDE_PROMPT + joined[-600:]}],
                                            role="helper", json_mode=True, max_tokens=80, temperature=0.2)
            from aigroup.chat.plan import lenient_json
            d = lenient_json(raw) or {}
            if not d.get("need"):
                return ""
            query = str(d.get("query") or "").strip()[:40]
        except Exception as e:  # noqa: BLE001
            log.info("判断要不要搜失败：%s", e)
        if not query:
            query = clean_query(recent[-1], list(self.aliases))
        if len(query) < 2:
            return ""
        res = await self.web.search(query)
        if not res:
            return ""
        if self.g.get("remember_web", True):
            self._spawn(self.learn(gid, query, res))
        return self.web.block(query, res)

    async def learn(self, gid: int, query: str, results: list[dict]):
        """把搜到的整理成一条“知道的事”存起来。"""
        if getattr(self, "knowledge", None) is None:
            return
        body = "\n".join(f"- {r['title']}：{r['snippet']}" for r in results[:5])
        try:
            raw = await self.brain.llm.chat([{"role": "user", "content": LEARN_PROMPT % (query, body)}],
                                            role="helper", json_mode=True, max_tokens=200, temperature=0.2,
                                            background=True)
            from aigroup.chat.plan import lenient_json
            d = lenient_json(raw) or {}
            kid = self.knowledge.add(gid, str(d.get("topic") or query), list(d.get("keys") or []),
                                     str(d.get("summary") or ""), "search", query)
            if kid:
                log.info("群 %s：记住了「%s」", gid, d.get("topic") or query)
        except Exception as e:  # noqa: BLE001
            log.info("整理搜到的东西失败：%s", e)

    async def watch_video(self, gid: int, info: dict, to_her: bool = False, who: str = ""):
        """4.4 群友发了 B 站视频：点开“看”——字幕、弹幕、热评整理成要点，以后聊到就能接；
        是发给她看的（@她 / 回她）就看完说两句感想。"""
        bv = info.get("bv")
        if not bv or not self.g.get("watch_videos", True) or getattr(self, "links", None) is None:
            return None
        part = int(info.get("p") or 1)
        ckey = f"qq_video_{bv}" + (f"_p{part}" if part > 1 else "")
        cache = self.db.get(ckey)
        if cache and "lines" not in cache and int(self.g.get("video_frames", 8) or 0) > 0:
            cache = None                                   # 15.0 以前看过的只有要点：重新看一遍（这次带画面和原话）
        if not cache:
            if not self._allow("video", 10):
                return None
            from aigroup.net.bili import ASR, Bili
            b = Bili(self.links.fetch_bytes, self.brain.llm, ASR(self.cfg))
            try:
                w = await b.watch(bv, part, int(self.g.get("video_frames", 8) or 0))
                s = await b.summarize(w)
            except Exception as e:  # noqa: BLE001
                log.info("视频 %s 没看成：%s", bv, e)
                return None
            if not s.get("gist"):
                return None
            cache = {"title": w["title"], "block": Bili.describe(w, s), "gist": str(s["gist"])[:80],
                     "keys": [k for k in (s.get("keys") or []) if isinstance(k, str)][:5],
                     "points": [str(x) for x in (s.get("points") or [])][:6], "subtitle": bool(w.get("subtitle") or w.get("heard")),
                     "lines": str(w.get("subtitle") or w.get("heard") or "")[:6000],      # 15.0 原话（带时间），追问时翻
                     "frames": list(w.get("frames") or [])[:12], "author": w.get("author", ""), "ts": time.time()}
            self.db.set(ckey, cache)
            log.info("群 %s：看了视频《%s》（%s，画面 %d 张）", gid, cache["title"][:30],
                     "有字幕或语音" if cache["subtitle"] else "没听到内容", len(cache["frames"]))
        vids = [v for v in (self.db.get(f"qq_videos_{gid}") or []) if v.get("bv") != bv]
        vids.append({"ts": time.time(), "bv": bv, "p": part, "key": ckey, "title": cache["title"], "block": cache["block"],
                     "who": who})
        self.db.set(f"qq_videos_{gid}", vids[-10:])
        kn = getattr(self, "knowledge", None)
        if kn is not None and self.g.get("remember_web", True):
            summary = cache["gist"] + ("；" + "；".join(cache["points"][:2]) if cache["points"] else "")
            kn.add(gid, cache["title"][:20], [cache["title"][:16], *cache["keys"]], summary[:160], "video")
        if to_her:
            st = self._st(gid)
            why = (f"{who or '有人'}发了个视频给你看，你刚看完（下面“群里最近分享的视频”里有你看到的内容）。"
                   "像朋友那样说说感想，可以提到具体哪一段、哪个梗，一两句话；没看到的画面别编")
            async with st.lock:
                try:
                    await self.speak(gid, mentioned=True, owner_spoke=False, trigger=None, who=who, why=why)
                except Exception as e:  # noqa: BLE001
                    log.info("群 %s 看完视频没说成：%s", gid, e)
        return cache

    def videos_block(self, gid: int, hours: float = 6, query: str = "") -> str:
        vids = [v for v in (self.db.get(f"qq_videos_{gid}") or []) if time.time() - float(v.get("ts") or 0) < hours * 3600]
        if not vids:
            return ""
        out = "# 群里最近分享的视频（你点开看过了）\n" + "\n".join(
            (f"{v['who']}发的：" if v.get("who") else "") + v["block"] for v in vids[-2:])
        det = self.video_detail(vids, query) if query else ""
        return out + ("\n\n" + det if det else "")

    def video_detail(self, vids: list, query: str) -> str:
        """15.0 有人追问视频里的内容（“第 3 分钟那段”“最后说了啥”“画面上那个是谁”）：翻出那附近的原话和画面。"""
        from aigroup.net.bili import ask_time
        asks = bool(re.search(r"视频|那段|那里|片段|分钟|开头|结尾|最后|后面|前面|画面|UP|up主|博主|弹幕|讲了|说了|\d{1,2}[:：]\d{2}", query))
        hit = None
        for v in reversed(vids[-2:]):
            c = self.db.get(v.get("key") or f"qq_video_{v.get('bv')}") or {}
            words = [c.get("title", "")[:6], *(c.get("keys") or [])]
            if asks or any(w and len(w) >= 2 and w in query for w in words):
                hit = c
                break
        if not hit or not (hit.get("lines") or hit.get("frames")):
            return ""
        t = ask_time(query, hit.get("lines", ""))
        lines = [ln for ln in str(hit.get("lines") or "").splitlines() if ln.strip()]
        fr = list(hit.get("frames") or [])
        if t is not None:
            from aigroup.net.bili import line_time
            near = [ln for ln in lines if (lt := line_time(ln)) is not None and abs(lt - t) <= 45]
            fnear = [f for f in fr if (ft := line_time(f"[{f.get('t')}]")) is not None and abs(ft - t) <= 90]
            body = "\n".join(near[:8]) or "（那附近没有原话）"
            fb = "；".join(f"{f['t']} {f['see']}" for f in fnear[:3])
            return (f"# 《{hit.get('title', '')}》{t // 60:02d}:{t % 60:02d} 附近（你看的时候记下的）\n{body}"
                    + (f"\n画面：{fb}" if fb else "") + "\n（只说这里有的，没有的就说没注意到）")
        body = "\n".join(lines)
        if len(body) > 1600:                                       # 太长：头、中、尾各留一点
            body = body[:700] + "\n……\n" + body[len(body) // 2: len(body) // 2 + 400] + "\n……\n" + body[-500:]
        fb = "\n".join(f"- {f['t']}：{f['see']}" for f in fr[:8])
        return (f"# 《{hit.get('title', '')}》里说的话（你看的时候记下的，带时间）\n{body or '（没听到内容）'}"
                + (f"\n# 你看到的画面\n{fb}" if fb else "") + "\n（只说这里有的，没有的就说没注意到）")

    def learn_link(self, gid: int, desc: str):
        """群友发的链接读到了内容：也记一条（标题当话题）。"""
        if getattr(self, "knowledge", None) is None or not desc or "没打开" in desc:
            return
        m = re.search(r"《([^》]{2,30})》|「([^」]{2,30})」", desc)
        title = (m.group(1) or m.group(2)) if m else ""
        if not title:
            return
        summary = re.sub(r"^\[链接：|\]$", "", desc).strip()[:150]
        self.knowledge.add(gid, title[:20], [title[:16]], summary, "link")

    async def weekly_reflect(self, force: bool = False) -> int:
        """每周一次：回想这一周，写几句“这一周的我”（存 qq_reflect_{群}，提示词里会带上）。返回写了几个群。"""
        now = self.brain.now()
        week = f"{now:%G-W%V}"
        n = 0
        for gid in sorted(self.groups):
            key = f"qq_reflect_week_{gid}"
            if self.db.get(key) == week and not force:
                continue
            since = time.time() - 7 * 86400
            kn = getattr(self, "knowledge", None)
            learned = "\n".join(f"- {e['topic']}：{e['summary']}" for e in (kn.entries(gid, 40) if kn else [])
                                if e["ts"] > since)[:1200] or "（没有）"
            mine = [r["content"] for r in self.db.conn.execute(
                "SELECT content FROM qq_group_messages WHERE group_id=? AND role='assistant' AND ts>? "
                "ORDER BY id DESC LIMIT 40", (gid, since)).fetchall()]
            if len(mine) < 3 and learned == "（没有）":
                self.db.set(key, week)
                continue
            p = self.brain.persona
            prompt = REFLECT_PROMPT % (p.name, p.body[:1500], self.db.get(f"qq_reflect_{gid}") or "（没有）", learned,
                                       (self.db.get(f"qq_group_notes_{gid}") or "（没有）")[:800],
                                       "\n".join(reversed(mine))[:1000] or "（很少说话）")
            try:
                raw = await self.brain.llm.chat([{"role": "user", "content": prompt}], role="helper", max_tokens=300,
                                                temperature=0.6, background=True)
            except Exception as e:  # noqa: BLE001
                log.info("群 %s 每周回想失败：%s", gid, e)
                continue
            lines = [ln.strip() for ln in (raw or "").splitlines() if ln.strip().startswith("-")][:5]
            if lines:
                self.db.set(f"qq_reflect_{gid}", "\n".join(lines)[:600])
                hist = [h for h in (self.db.get(f"qq_reflect_hist_{gid}") or []) if h.get("week") != week]
                self.db.set(f"qq_reflect_hist_{gid}", (hist + [{"week": week, "text": "\n".join(lines)[:600]}])[-16:])
                self.db.set(key, week)
                log.info("群 %s：她回想了这一周（%d 句）", gid, len(lines))
                n += 1
        return n
