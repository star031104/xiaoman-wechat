"""19.1 更像人：接住搭话、不重复自己、没人理不追发、相处说明书、像人的几个周指标。

依据（docs/更像人.md 有完整说明和出处）：
- Gottman：长久的关系里 86% 的“搭话”（bid）会被接住，离婚的只有 33%——别人回她、损她，她不接就是“转身走开”
- Inner Thoughts（Liu et al. 2025）：先有念头，按相关、信息差、效果、新意、时机打分，过线才说
- Beyond Recall（2026）：“他怎么想问题”的行为说明比只检索事实更能预测他的反应
- When2Speak（2026）：该说没说（MIR）和不该说却说（FIR）分开量

真实群聊里测出来的问题（2026-10-21 微信群）：群友回她“你够了，哪壶不开提哪壶”她没接；“AG 纪录片看哭”一天说两遍；
凌晨 1:42、5:16、6:40 群里没人也连着说；“桑拿鸡”主动提了两次；“消毒”叮嘱了三遍。
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timedelta

from aigroup.chat.address import echo_today, plan_terms, shared_terms

log = logging.getLogger("aigroup.group")

SPEC_PROMPT = """你是{me}，在一个群里和大家聊天。下面是群友「{name}」最近在群里说的话，以及你对他说过的话（后面标了他有没有接）。
请写一份“和他相处的说明书”，给你自己以后看：不是罗列他的资料，而是写他说话的潜台词、什么时候想要什么。
只根据下面这些话写；猜他的年龄、家庭、心理状态这类“背景”不要写进来（猜错了比不写更伤人）。

他最近说的（新的在后）：
{his}

你对他说的话和效果：
{mine}

只输出 JSON：
{{"how": ["4~7 条，每条一句：他……的时候，多半是在……，这时候你……（必须从上面的话里看得出来，别编）"],
  "tease_ok": ["可以拿来开玩笑的事（他自己常拿来说的、玩得起的），没有就空"],
  "tease_no": ["别拿来开玩笑的（他不接、或者一提就冷场的），没有就空"],
  "care": "他什么时候想要被关心、什么时候只想被损两句（一句话，看不出来就空）"}}"""


# 21.0 她问大家的一句（没点名）：某一小句以问号或“啥 / 什么 / 吗 / 没 / 呢 / 哪 / 咋 / 多少”结尾
_Q_END = re.compile(r"([？?]|啥|什么|吗|嘛|没|呢|几点|哪|哪儿|哪里|怎么样|咋样|咋了|多少|几个)$")


class _OpenQ:
    @staticmethod
    def search(text: str) -> bool:
        t = re.sub(r"\[[^\]]*\]", "", str(text or ""))
        return any(_Q_END.search(c.strip()) for c in re.split(r"[，,。！!～~\s]+", t) if c.strip())


OPEN_Q = _OpenQ()


class HumaneMixin:
    # ================= 19.1 搭话：别人在接她的话 =================
    def names_of(self, gid: int, uid: int) -> list[str]:
        """这个人在群里的名字、外号、称呼（她点名时会用的）。"""
        out = [str(self.names(gid).get(str(uid)) or "")]
        p = self.person(int(uid), gid) or {}
        out += [str(a) for a in p.get("aliases") or []]
        call = str((p.get("profile") or {}).get("称呼") or "")
        out += [w for w in re.split(r"[；;，,/、（()）\s]+|大家都这么叫|外号", call) if w]
        return [w.strip("“”\"' ") for w in dict.fromkeys(out) if len(w.strip("“”\"' ")) >= 2]

    def bid_from(self, gid: int, uid: int, text: str, *, at_other: bool = False, reply_other: bool = False,
                 now: float | None = None) -> str:
        """这句是不是在接她的话（“搭话”）：
        1) 她这几个小时里点过他（叫了他的名字、@ 他、回的就是他），这是他之后说的第一句；
        2) 或者这句接了她刚说过的词（≥ 4 个字的共同片段），而且是这几个小时里第一次接；
        3) 21.0 她刚问了大家一句（“中午吃啥”“赢了没”），这是 open_question_minutes 分钟里他说的第一句——在回答她。
        返回她那句话的原文（是搭话）或 ""。@ 了别人、回复别人的不算。"""
        g = self.gk(gid)
        if not g.get("bids", True) or at_other or reply_other or not text or not str(text).strip():
            return ""
        if not re.sub(r"\[[^\]]*\]", "", str(text)).strip():
            return ""          # 22.2 只发了一张图 / 一个表情包：不算在接她的话（10-23 一张价目表被当成接她，逼得她硬猜）
        now = now or time.time()
        hours = float(g.get("bid_hours", 3) or 3)
        hers = self.db.conn.execute(
            "SELECT id, ts, content FROM qq_group_messages WHERE group_id=? AND role='assistant' AND ts>? "
            "ORDER BY id DESC LIMIT 8", (gid, now - hours * 3600)).fetchall()
        if not hers:
            return ""
        st = self._st(gid)
        done = st.__dict__.setdefault("bid_done", set())
        names = self.names_of(gid, uid)
        targeted = {round(float(r["ts"])) for r in self.db.conn.execute(
            "SELECT ts FROM qq_reply_effect WHERE group_id=? AND target_uid=? AND ts>?",
            (gid, uid, now - hours * 3600)).fetchall()}
        for h in hers:
            if h["id"] in done:
                continue
            content = str(h["content"] or "")
            if content.startswith("[") and "：" not in content[:6]:
                continue
            said_since = int(self.db.conn.execute(
                "SELECT COUNT(*) n FROM qq_group_messages WHERE group_id=? AND user_id=? AND role='user' AND ts>?",
                (gid, uid, h["ts"])).fetchone()["n"])
            called = any(n in content for n in names) or any(abs(t - float(h["ts"])) <= 120 for t in targeted)
            if called and said_since == 0:
                done.add(h["id"])
                return content
            if (said_since == 0 and now - float(h["ts"]) <= float(g.get("open_question_minutes", 10)) * 60
                    and OPEN_Q.search(content)):
                done.add(h["id"])
                return content
            if said_since <= 2 and len(re.sub(r"\s", "", text)) >= 3:
                common = [w for w in shared_terms(text, [content], n=4)]
                if common:
                    done.add(h["id"])
                    return content
        return ""

    # ================= 19.1 她今天说过的 =================
    def her_today(self, gid: int | None = None, now=None) -> list[dict]:
        """她今天说过的话（gid 为空：所有群一起，人是同一个）。"""
        now = now or self.brain.now()
        t0 = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        q = "SELECT id, ts, group_id, content FROM qq_group_messages WHERE role='assistant' AND ts>=?"
        args: list = [t0]
        if gid is not None:
            q += " AND group_id=?"
            args.append(gid)
        return [dict(r) for r in self.db.conn.execute(q + " ORDER BY id", args).fetchall()]

    def today_lines(self, gid: int | None = None) -> list[str]:
        return [str(r["content"] or "") for r in self.her_today(gid) if r["content"] and not str(r["content"]).startswith("[表情包")]

    def today_block(self, gid: int) -> str:
        """提示词里的一段：她今天在这个群早些时候说过的（聊天记录里看不到的那些），和今天已经主动提过的安排里的事。"""
        if not self.gk(gid).get("no_repeat_today", True):
            return ""
        shown = {v.get("id") for v in (self._tmap.get(gid) or {}).values()} if hasattr(self, "_tmap") else set()
        rows = [r for r in self.her_today(gid) if r["id"] not in shown and not str(r["content"]).startswith("[表情包")]
        lines = []
        if rows:
            lines.append("你今天早些时候在这个群说过（同样的意思、同样的梗今天别再说；关心过的事别再叮嘱一遍）：")
            lines += [f"- {datetime.fromtimestamp(r['ts'], self.cfg.tz):%H:%M} {str(r['content'])[:40]}" for r in rows[-10:]]
        topics = self.plan_topics_used()
        if topics:
            lines.append("你今天安排里的这些事已经主动说过了，别再主动提（别人问起再说）：" + "、".join(topics))
        return ("# 你今天说过的\n" + "\n".join(lines)) if lines else ""

    def plan_topics_used(self) -> list[str]:
        life = self.db.get(f"life_{self.brain.now():%Y-%m-%d}", "") or ""
        if not life:
            return []
        names = [n for g in self.groups for n in self.member_names(g)] if hasattr(self, "member_names") else []
        return plan_terms(str(life), self.today_lines(), names=names)

    def echo_names(self, gid: int) -> list[str]:
        p = self.brain.persona
        return [p.name, getattr(p, "formal_name", "") or "", *self.aliases, *(self.member_names(gid) if hasattr(self, "member_names") else [])]

    def echo_of(self, gid: int, text: str, recent_others: str = "") -> str:
        """19.1 这句里有没有她今天说过的东西：和今天说过的某句有 ≥5 个字的共同片段，或者是今天安排里已经主动提过的事
        （别人最近几句里正好提到的不算：那是在接话）。返回命中的片段。"""
        if not self.gk(gid).get("no_repeat_today", True):
            return ""
        hit = echo_today(text, self.today_lines(), names=self.echo_names(gid))
        if hit and hit not in recent_others:
            return hit
        for w in self.plan_topics_used():
            if w in text and w not in recent_others:
                return w
        return ""

    # ================= 19.1 没人理不追发、深夜没人不主动 =================
    def lonely_reason(self, gid: int, now: float | None = None) -> str:
        """主动开口（冒泡、分享、天气、打气以外的）前看一眼：她上一句就是群里最后一句（说完没人接）→ 不再追一句；
        深夜（lonely_hours）群里 20 分钟没人说话 → 不主动说。返回不说的原因（"" = 可以说）。"""
        g = self.gk(gid)
        now = now or time.time()
        last = self.db.conn.execute("SELECT role, ts FROM qq_group_messages WHERE group_id=? ORDER BY id DESC LIMIT 1",
                                    (gid,)).fetchone()
        if g.get("no_monologue", True) and last and last["role"] == "assistant":
            return "她上一句还没人接"
        hrs = str(g.get("lonely_hours") or "").strip()
        if hrs:
            from aigroup.core.schedule import parse_range
            nd = self.brain.now()
            inside = False
            for base in (nd, nd - timedelta(days=1)):
                r = parse_range(hrs, base)
                inside = inside or bool(r and r[0] <= nd < r[1])
            lu = self.db.conn.execute("SELECT MAX(ts) t FROM qq_group_messages WHERE group_id=? AND role='user'",
                                      (gid,)).fetchone()
            if inside and (not lu or not lu["t"] or now - float(lu["t"]) > 20 * 60):
                return f"深夜（{hrs}）群里没人"
        return ""

    # ================= 19.1 相处说明书 =================
    async def build_specs(self, days: float = 14, k_people: int = 8, min_msgs: int = 30) -> int:
        """每周一次：这两周说话最多的几个人，各写一份“和他相处的说明书”（他说话的潜台词、能损什么、别碰什么、什么时候要关心）。"""
        n = 0
        me = self.brain.persona.name
        since = time.time() - days * 86400
        for gid in sorted(self.groups):
            rows = self.db.conn.execute(
                "SELECT user_id, COUNT(*) c FROM qq_group_messages WHERE group_id=? AND role='user' AND ts>? "
                "GROUP BY user_id ORDER BY c DESC LIMIT ?", (gid, since, k_people)).fetchall()
            for r in rows:
                uid, c = int(r["user_id"]), int(r["c"])
                if c < min_msgs or not uid:
                    continue
                old = self.db.get(f"qq_spec_{gid}_{uid}") or {}
                if old.get("ts") and time.time() - float(old["ts"]) < 6 * 86400:
                    continue
                his = [x["content"] for x in reversed(self.db.conn.execute(
                    "SELECT content FROM qq_group_messages WHERE group_id=? AND user_id=? AND role='user' AND ts>? "
                    "ORDER BY id DESC LIMIT 80", (gid, uid, since)).fetchall())]
                mine = self.db.conn.execute(
                    "SELECT text, score FROM qq_reply_effect WHERE group_id=? AND target_uid=? AND done=1 AND ts>? "
                    "ORDER BY id DESC LIMIT 20", (gid, uid, since)).fetchall()
                name = str(self.names(gid).get(str(uid)) or uid)
                from aigroup.core.safety import clean_member_text
                try:
                    d = await self.brain.llm.json([{"role": "user", "content": SPEC_PROMPT.format(
                        me=me, name=name,
                        his="\n".join(f"- {clean_member_text(str(x), 60)}" for x in his if x) or "（没有）",
                        mine="\n".join(f"- {str(m['text'])[:50]}（{'接了' if (m['score'] or 0) >= 0.6 else '冷场' if (m['score'] or 0) <= 0.2 else '一般'}）"
                                       for m in mine) or "（还没对他说过什么）")}], default={}, role="memory")
                except Exception as e:  # noqa: BLE001
                    log.info("相处说明书没写成：%s", e)
                    continue
                d = d if isinstance(d, dict) else {}
                how = [str(x).strip().lstrip("-").strip()[:60] for x in d.get("how") or [] if str(x).strip()][:7]
                if not how:
                    continue
                spec = {"ts": time.time(), "name": name, "how": how,
                        "tease_ok": [str(x)[:20] for x in d.get("tease_ok") or [] if str(x).strip()][:5],
                        "tease_no": [str(x)[:20] for x in d.get("tease_no") or [] if str(x).strip()][:5],
                        "care": str(d.get("care") or "")[:60], "n": c}
                self.db.set(f"qq_spec_{gid}_{uid}", spec)
                n += 1
        if n:
            log.info("写了 %d 份“和他相处的说明书”", n)
        return n

    def spec_of(self, gid: int, uid: int) -> dict:
        return self.db.get(f"qq_spec_{gid}_{uid}") or {}

    def spec_block(self, gid: int, focus: list, k: int = 2) -> str:
        if not self.gk(gid).get("person_spec", True):
            return ""
        out = []
        for uid in [u for u in focus if isinstance(u, int) and u > 0][:k]:
            d = self.spec_of(gid, uid)
            if not d.get("how"):
                continue
            body = [f"- {x}" for x in d["how"][:5]]
            if d.get("tease_ok"):
                body.append("- 能拿来开玩笑的：" + "、".join(d["tease_ok"]))
            if d.get("tease_no"):
                body.append("- 别拿来开玩笑的：" + "、".join(d["tease_no"]))
            if d.get("care"):
                body.append(f"- {d['care']}")
            out.append(f"# 和{d.get('name')}相处（你自己总结的，他说话的潜台词）\n" + "\n".join(body))
        return "\n\n".join(out)

    def spec_avoid(self, gid: int, uids: list) -> list[str]:
        out = []
        for uid in uids:
            if uid:
                out += [w for w in self.spec_of(gid, int(uid)).get("tease_no") or [] if 2 <= len(w) <= 8]
        return out[:8]

    # ================= 19.1 像人的几个周指标 =================
    def humane_metrics(self, gid: int, days: float = 7, now: float | None = None) -> dict:
        """该说没说率、不该说却说率（你的标注）、搭话接住率、当天重复、没人理还追发的次数。"""
        now = now or time.time()
        since = now - days * 86400
        lab = self.db.conn.execute("SELECT spoke, human FROM qq_gate_log WHERE group_id=? AND human IN (0,1) "
                                   "AND NOT (human=0 AND IFNULL(human_why,'')='说的不对')", (gid,)).fetchall()
        should = [r for r in lab if r["human"] == 1]
        shouldnt = [r for r in lab if r["human"] == 0]
        mir = round(sum(1 for r in should if not r["spoke"]) / len(should), 3) if should else None
        fir = round(sum(1 for r in shouldnt if r["spoke"]) / len(shouldnt), 3) if shouldnt else None
        bids = list(self.db.get(f"qq_bids_{gid}") or [])
        bids = [b for b in bids if float(b.get("ts") or 0) > since]
        caught = sum(1 for b in bids if b.get("answered"))
        rows = self.db.conn.execute("SELECT ts, content FROM qq_group_messages WHERE group_id=? AND ts>? AND role='assistant' "
                                    "ORDER BY id", (gid, since)).fetchall()
        rep, by_day = 0, {}
        names = self.echo_names(gid)
        for r in rows:
            d = datetime.fromtimestamp(r["ts"], self.cfg.tz).date()
            prev = by_day.setdefault(d, [])
            c = str(r["content"] or "")
            if c and not c.startswith("[") and echo_today(c, prev, names=names):
                rep += 1
            prev.append(c)
        allrows = self.db.conn.execute("SELECT role, ts FROM qq_group_messages WHERE group_id=? AND ts>? ORDER BY id",
                                       (gid, since)).fetchall()
        mono = 0
        for a, b in zip(allrows, allrows[1:]):
            if a["role"] == "assistant" and b["role"] == "assistant" and float(b["ts"]) - float(a["ts"]) > 1800:
                mono += 1
        return {"days": days, "labels": len(lab), "mir": mir, "fir": fir, "bids": len(bids), "bids_caught": caught,
                "bid_rate": round(caught / len(bids), 3) if bids else None, "repeats": rep, "monologues": mono,
                "her_msgs": len(rows)}

    def note_bid(self, gid: int, who: str, her: str, answered: bool | None = None):
        """记一次搭话（小屋“像人的指标”里算接住率）；answered=None 表示刚记下，说完以后补成 True / False。"""
        log_ = list(self.db.get(f"qq_bids_{gid}") or [])
        if answered is None:
            log_.append({"ts": time.time(), "who": who, "her": str(her)[:40], "answered": False})
        elif log_:
            log_[-1]["answered"] = bool(answered)
        self.db.set(f"qq_bids_{gid}", log_[-200:])

    # ================= 20.0 长对话评测（CharacterBench 的维度，从一整段聊天的后半段看） =================
    async def long_judge(self, days: float = 7, window: int = 50) -> dict:
        """挑这一周她说话最多的一段（50 条群聊里她至少说了 5 句），按记忆一致、人设行为、人设说话、情绪分寸、像真人、
        让人想接这六项打 1~5 分。单句打分看不出“同一个梗说两遍”“别人接她的话她没接”这种跨好几轮的毛病。"""
        p = self.brain.persona
        since = time.time() - days * 86400
        best = None
        for gid in sorted(self.groups):
            rows = self.db.conn.execute("SELECT ts, name, role, content FROM qq_group_messages WHERE group_id=? AND ts>? "
                                        "ORDER BY id", (gid, since)).fetchall()
            for i in range(0, max(1, len(rows) - window + 1), max(10, window // 2)):
                seg = rows[i:i + window]
                n_her = sum(1 for r in seg if r["role"] == "assistant")
                if n_her >= 5 and (best is None or n_her > best[0]):
                    best = (n_her, gid, seg)
        if not best:
            return {}
        _, gid, seg = best
        from aigroup.core.safety import clean_member_text
        txt = "\n".join(f"[{datetime.fromtimestamp(r['ts'], self.cfg.tz):%m-%d %H:%M}] "
                        f"{'【她】' if r['role'] == 'assistant' else clean_member_text(r['name'], 12)}：{clean_member_text(r['content'], 80)}"
                        for r in seg)
        prompt = (f"下面是一个群聊片段，【她】是一个叫「{p.name}」的角色（人设节选：{p.body[:600]}）。\n\n{txt}\n\n"
                  "只看【她】的表现，每项 1~5 分（5 最好），并写一句最大的毛病：\n"
                  "记忆：前后说的事一致吗；行为：做的事像她的人设吗；说话：口吻像她吗、有没有客服腔；"
                  "情绪：自己的情绪有分寸、别人难过时接得住吗；像人：像真人群友吗（会不会重复自己、别人接她的话她接没接、没人理还硬聊）；"
                  "吸引：大家愿意接她的话吗。\n"
                  '只输出 JSON：{"记忆": 分, "行为": 分, "说话": 分, "情绪": 分, "像人": 分, "吸引": 分, "worst": "一句话"}')
        try:
            d = await self.brain.llm.json([{"role": "user", "content": prompt}], default={}, role="memory")
        except Exception as e:  # noqa: BLE001
            log.info("长对话评测出错：%s", e)
            return {}
        d = d if isinstance(d, dict) else {}
        out = {}
        for k in ("记忆", "行为", "说话", "情绪", "像人", "吸引"):
            try:
                v = float(d.get(k))
            except (TypeError, ValueError):
                continue
            if 1 <= v <= 5:
                out[k] = v
        if not out:
            return {}
        res = {"ts": time.time(), "gid": gid, "scores": out, "avg": round(sum(out.values()) / len(out), 2),
               "worst": str(d.get("worst") or "")[:80]}
        self.db.set("long_judge_last", res)
        return res

    # ================= 20.0 盲测：她说的混在群友说的里，你猜哪句是她 =================
    def blind_set(self, gid: int, n: int = 10, days: float = 7) -> dict:
        import random
        since = time.time() - days * 86400
        her = [r["content"] for r in self.db.conn.execute(
            "SELECT content FROM qq_group_messages WHERE group_id=? AND role='assistant' AND ts>? AND length(content) BETWEEN 6 AND 60 "
            "AND content NOT LIKE '[%' ORDER BY RANDOM() LIMIT ?", (gid, since, n // 2)).fetchall()]
        oth = [r["content"] for r in self.db.conn.execute(
            "SELECT content FROM qq_group_messages WHERE group_id=? AND role='user' AND ts>? AND length(content) BETWEEN 6 AND 60 "
            "AND content NOT LIKE '[%' AND content NOT LIKE '%@%' ORDER BY RANDOM() LIMIT ?", (gid, since, n - len(her))).fetchall()]
        items = [{"text": t, "her": True} for t in her] + [{"text": t, "her": False} for t in oth]
        random.shuffle(items)
        key = f"qq_blind_{gid}"
        self.db.set(key, {"ts": time.time(), "items": items})
        return {"items": [{"i": i, "text": x["text"]} for i, x in enumerate(items)]}

    def blind_answer(self, gid: int, picks: list[int]) -> dict:
        d = self.db.get(f"qq_blind_{gid}") or {}
        items = d.get("items") or []
        if not items:
            return {"ok": False, "error": "先出一组题"}
        truth = {i for i, x in enumerate(items) if x["her"]}
        got = {int(p) for p in picks if 0 <= int(p) < len(items)}
        hit = len(truth & got)
        res = {"ok": True, "her": sorted(truth), "hit": hit, "total": len(truth),
               "rate": round(hit / len(truth), 3) if truth else None}
        hist = list(self.db.get("qq_blind_hist") or [])
        hist.append({"ts": time.time(), "gid": gid, **{k: res[k] for k in ("hit", "total", "rate")}})
        self.db.set("qq_blind_hist", hist[-50:])
        return res

    @staticmethod
    def _dump(x) -> str:
        return json.dumps(x, ensure_ascii=False)
