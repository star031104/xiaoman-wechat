"""空闲时：群里安静太久自己冒个泡、早上补回半夜被叫到的、被戳了戳回去、按安排改签名。idle_tick 每分钟被调一次。"""
from __future__ import annotations

import asyncio
import logging
import random
import re
import time

from aigroup.chat.state import GroupState
from datetime import datetime
from aigroup.chat.plan import BUSY_RE, TRAIT_WORDS

log = logging.getLogger("aigroup.group")


# ---- 30.0 从 life.py 并进来的（她自己的生活：今天的安排里此刻在干嘛、是不是在忙、她感兴趣的话题。） ----


class IdleMixin:
    async def idle_tick(self, step_minutes: float = 1.0):
        """每分钟调一次。"""
        try:                                                    # 21.1.1 有地方写了数据库没提交：写锁一直占着，
            if getattr(self.db.conn, "in_transaction", False):  # 小管家 / 回放脚本会报 database is locked。每分钟兜底提交一次
                self.db.conn.commit()
                self.db.set("open_txn_fixed", int(self.db.get("open_txn_fixed", 0) or 0) + 1)
        except Exception:  # noqa: BLE001
            pass
        from aigroup.core.schedule import in_quiet, parse_range
        try:
            self.evaluate_effects()
        except Exception as e:  # noqa: BLE001
            log.info("统计接话效果出错：%s", e)
        if hasattr(self, "tool_reminders_tick"):               # 19.0 当面要的提醒：到点就发（免打扰、活跃时段都不管）
            try:
                await self.tool_reminders_tick()
            except Exception as e:  # noqa: BLE001
                log.warning("到点提醒出错：%s", e)
        now_dt = self.brain.now()
        if in_quiet(now_dt, self.cfg.get("quiet_hours")):
            return
        now = time.time()
        try:
            await self.maybe_signature()
        except Exception:  # noqa: BLE001
            pass
        for gid in self.groups:
            g = self.gk(gid)                                    # 2.2 每个群可以有自己的冒泡时段、冒泡概率
            r = parse_range(str(g.get("active_hours") or ""), now_dt)
            in_active = not r or (r[0] <= now_dt < r[1])
            st = self._st(gid)
            if st.lock.locked() or st.speaking or (st.pending and not st.pending.done()) or self.muted(gid):
                continue
            # 半夜有人叫她：醒了（免打扰时段一过）就补一句，不用等到冒泡时段
            if await self._night_catch_up(gid, st, now):
                continue
            if hasattr(self, "pitch_tick"):                      # 21.0 刚才觉得“再等等”的那句：到时候了再判一次
                try:
                    if await self.pitch_tick(gid, st, now):
                        continue
                except Exception as e:  # noqa: BLE001
                    log.info("群 %s 等等再说出错：%s", gid, e)
            if hasattr(self, "promise_tick"):                    # 25.0 答应了要给的结果：到点还没给就补
                try:
                    if await self.promise_tick(gid, st, now):
                        continue
                except Exception as e:  # noqa: BLE001
                    log.info("群 %s 补答应的事出错：%s", gid, e)
            if not in_active:
                continue
            if hasattr(self, "celebrate_tick") and await self.celebrate_tick(gid, st):   # 生日、节日：说一声
                continue
            if hasattr(self, "weather_tick") and await self.weather_tick(gid, st):     # 4.4 在外地的群友那边要变天
                continue
            if hasattr(self, "plans_tick") and await self.plans_tick(gid, st):     # 约好的事：到点提醒 / 事后问
                continue
            if hasattr(self, "feed_tick"):                       # 20.2 后台攒“刷到的”候选（不调大模型）
                try:
                    self.feed_tick(gid, now)
                except Exception as e:  # noqa: BLE001
                    log.info("群 %s 刷候选出错：%s", gid, e)
            if (hasattr(self, "topic_watch") and self.gk(gid).get("topic_watch", True) and now_dt.hour >= 10
                    and not self.db.get(f"topic_watch_{gid}_{now_dt:%Y-%m-%d}_go") and hasattr(self, "_spawn")):
                self.db.set(f"topic_watch_{gid}_{now_dt:%Y-%m-%d}_go", True)
                self._spawn(self.topic_watch(gid))               # 30.0 群里一直在聊的事：每天跟进一次（后台查，不挡着聊天）
            if hasattr(self, "share_due") and self.share_due(gid, now, step_minutes):  # 偶尔分享刷到的东西
                try:
                    await self.share(gid, st)
                except Exception as e:  # noqa: BLE001
                    log.info("群 %s 分享出错：%s", gid, e)
                continue
            topic = self.bubble_due(gid, now)
            if not topic:
                continue
            last = self.db.conn.execute(
                "SELECT ts FROM qq_group_messages WHERE group_id=? ORDER BY id DESC LIMIT 1", (gid,)).fetchone()
            hours = (now - float(last["ts"])) / 3600
            self.db.set(f"qq_idle_{gid}", now)
            if topic == "feed":                                 # 话题是刷到的东西：走分享（有评分卡和衰减）
                try:
                    await self.share(gid, st)
                except Exception as e:  # noqa: BLE001
                    log.info("群 %s 冒泡（分享）出错：%s", gid, e)
                continue
            why = (f"群里已经安静了 {hours:.0f} 个多小时。你心里一直有句话想说：{topic}。现在随口说出来，"
                   "一两句话，自然一点，别说“大家好”“怎么没人说话”这种。"
                   f"聊天记录里最后那些话已经是 {hours:.0f} 个多小时以前的了，提到时别说成“刚才”“刚说”")
            async with st.lock:
                try:
                    if await self.speak(gid, mentioned=False, owner_spoke=False, trigger=None, who="", why=why):
                        self.use_thoughts(gid, topic)
                except Exception as e:  # noqa: BLE001
                    log.info("群 %s 冒泡失败：%s", gid, e)

    def bubble_due(self, gid: int, now: float) -> str:
        """24.0 冒泡重做（dsh-speak-up 的零成本骰子 + 必须有话题）：
        群里静了 idle_hours 以上、最后一句不是她、隔上次冒泡 idle_gap_hours 以上、没被连着冷落，才每 30 分钟掷一次骰子
        （基础 idle_chance / 2，连着没中每次 +10%、最多 40%；静了 6 小时以上 ×1.5）。中了还要有话题：
        她憋着的一句（想法池）或者够格的刷到的；都没有就不冒（以前没话题也让模型硬找，7 天一次都没触发过）。
        返回话题（想法池里的一句 / "feed"），不冒泡返回空。"""
        g = self.gk(gid)
        last = self.db.conn.execute(
            "SELECT ts, role FROM qq_group_messages WHERE group_id=? ORDER BY id DESC LIMIT 1", (gid,)).fetchone()
        if not last or last["role"] == "assistant":        # 群里从没人说过话 / 最后一句就是她：不自言自语
            return ""
        quiet = now - float(last["ts"])
        if quiet < float(g["idle_hours"]) * 3600 or quiet > 3 * 86400:
            return ""
        streak = self.reach(gid, now)[1] if hasattr(self, "reach") and g.get("reach_gate", True) else 0
        from aigroup.chat.feedback import reach_gap_mult
        if now - float(self.db.get(f"qq_idle_{gid}", 0) or 0) < float(g["idle_gap_hours"]) * 3600 * reach_gap_mult(streak):
            return ""
        if self.ignored_count(gid, now) >= 2:              # 刚被冷落过：先别冒泡
            return ""
        key = f"qq_bubble_roll_{gid}"
        roll = self.db.get(key) or {}
        if now - float(roll.get("ts") or 0) < 30 * 60:
            return ""
        misses = int(roll.get("misses") or 0)
        p = min(0.4, float(g["idle_chance"]) / 2 + 0.1 * misses) * (1.5 if quiet > 6 * 3600 else 1.0)
        if hasattr(self, "reach_factor"):                  # 31.0 主动说的最近有没有人接
            p *= self.reach_factor(gid, now)
        if random.random() >= p:
            self.db.set(key, {"ts": now, "misses": misses + 1})
            return ""
        self.db.set(key, {"ts": now, "misses": 0})
        pool = [x for x in (self.db.get(f"qq_thoughts_{gid}") or []) if now - float(x.get("ts") or 0) < 6 * 3600]
        if pool:
            return str(pool[-1].get("text") or "")
        try:
            if (hasattr(self, "feed_candidates") and not (self.share_pause(gid, now) if hasattr(self, "share_pause") else "")
                    and any(c["score"] >= float(g.get("feed_min", 0.25)) for c in self.feed_candidates(gid))):
                return "feed"
        except Exception:  # noqa: BLE001
            pass
        log.info("群 %s：想冒个泡，但没什么想说的，算了", gid)
        return ""

    async def _night_catch_up(self, gid: int, st: GroupState, now: float, wait: float = 0) -> bool:
        night = self.db.get(f"qq_night_{gid}")
        if isinstance(night, dict):                        # 老格式：只记了一个人
            night = [night]
        if not night:
            return False
        # 23.3 微信刚登回来时补消息比“接上”早几秒，那时发不出去（10-24 13:18:05“群消息没发出去：微信没在运行”），
        # 这一句就丢了：先等到能发，没发出去就留着下次再补
        for _ in range(int(wait / 2)):
            if getattr(self.ob, "ready", True):
                break
            await asyncio.sleep(2)
        if not getattr(self.ob, "ready", True):
            log.info("群 %s：微信还没接上，补回先放着", gid)
            return False
        before = self.db.conn.execute("SELECT COALESCE(MAX(id), 0) m FROM qq_group_messages WHERE group_id=? AND role='assistant'",
                                      (gid,)).fetchone()["m"]
        self.db.set(f"qq_night_{gid}", None)
        night = [x for x in night if now - float(x.get("ts") or 0) < 12 * 3600]
        if not night:
            return False
        whos = list(dict.fromkeys(x.get("who") or "有人" for x in night))
        who = "、".join(whos)
        last = night[-1]
        if all(x.get("kind") == "pending" for x in night):
            why = (f"{who}刚才叫你的话你还没回（刚才手机卡了一下）。看聊天记录里他说了什么，直接回应他，"
                   "不用解释为什么慢，也别说“才看到”")
        elif all(x.get("kind") in ("offline", "pending") for x in night):
            busy = ("没顾上看群" if getattr(self.brain.persona, "ai_open", False) else "没看手机（在忙 / 手机没电）")
            why = (f"你刚才有一阵子{busy}，这期间{who}叫过你，你现在才看到。"
                   "看看聊天记录里他们说了什么，回应一下（可以带一句“才看到”之类的，自然一点，别解释为什么不在）")
        else:
            why = (f"昨晚你睡着以后{who}叫过你，你刚醒才看到。回应一下他说的事"
                   "（可以带一句“才看到”之类的，自然一点）")
        async with st.lock:
            try:
                await self.speak(gid, mentioned=True, owner_spoke=False, trigger=last.get("mid"), who=who, why=why)
            except Exception as e:  # noqa: BLE001
                log.info("群 %s 早上补回失败：%s", gid, e)
        after = self.db.conn.execute("SELECT COALESCE(MAX(id), 0) m FROM qq_group_messages WHERE group_id=? AND role='assistant'",
                                     (gid,)).fetchone()["m"]
        if after == before and not any(x.get("retry") for x in night):
            self.db.set(f"qq_night_{gid}", [{**x, "retry": 1} for x in night])      # 23.3 没发出去：留着，下一轮再补一次
            log.info("群 %s：补回没发出去，留着下次再补", gid)
        return True

    async def poke_back(self, gid: int, uid: int):
        if random.random() < float(self.g.get("poke_back_chance", 0)) and self._allow("poke", 3):
            try:
                await asyncio.sleep(self.secs("read_delay", gid))
                await self.ob.call("group_poke", group_id=gid, user_id=uid)
                log.info("群 %s：戳回去了", gid)
            except Exception as e:  # noqa: BLE001
                log.debug("戳回去失败：%s", e)

    async def maybe_signature(self):
        """按今天的安排改 QQ 签名（默认关；每 2 小时最多一次，内容变了才改）。"""
        if not self.g.get("update_signature"):
            return
        act = self.her_activity()
        text = (re.sub(r"^\d{1,2}[:：]\d{2}\s*[-~—–到至]+\s*\d{1,2}[:：]\d{2}\s*", "", act)[:30] if act else "")
        if not text or text == self.db.get("qq_signature") or not self._allow("signature", 1):
            return
        last = float(self.db.get("qq_signature_ts", 0) or 0)
        if time.time() - last < 2 * 3600:
            return
        try:
            await self.ob.call("set_self_longnick", longNick=text)
            self.db.set("qq_signature", text)
            self.db.set("qq_signature_ts", time.time())
        except Exception as e:  # noqa: BLE001
            log.debug("改签名失败：%s", e)

    # ---------------- 30.0 并进来的：原来的 LifeMixin（life.py） ----------------
    def her_activity(self, now: datetime | None = None) -> str:
        """她今天的安排里，此刻在干嘛（没有就空）。"""
        now = now or self.brain.now()
        from datetime import timedelta

        from aigroup.core.util import RANGE_RE as _RANGE
        plan = str(self.db.get(f"life_{now:%Y-%m-%d}") or "")
        for ln in plan.splitlines():
            m = _RANGE.search(ln)
            if not m:
                continue
            h1, m1, h2, m2 = map(int, m.groups())
            base = now.replace(hour=0, minute=0, second=0, microsecond=0)
            a, b = base + timedelta(hours=h1, minutes=m1), base + timedelta(hours=h2, minutes=m2)
            if b <= a:
                b += timedelta(days=1)
            if a <= now < b:
                return ln.strip(" -•\t")[:60]
        return ""

    def busy(self, activity: str) -> bool:
        return bool(activity and BUSY_RE.search(activity))

    def interests_from(self, words: list) -> list[str]:
        stop = set(TRAIT_WORDS) | set(self.brain.persona.catchphrases or [])
        return [w for w in words if isinstance(w, str) and len(w) >= 2 and w not in stop
                and not any(a in w for a in self.aliases)]

    def interests(self) -> list[str]:
        """她感兴趣的话题。人设里写了 interests 就用人设的；否则用自动整理的（去掉性格词、口头禅、她自己的名字）。"""
        p = self.brain.persona
        own = [w for w in (getattr(p, "interests", None) or []) if isinstance(w, str) and len(w) >= 2]
        if own:
            return own
        return self.interests_from(self.db.get("qq_interests") or [])

    async def learn_interests(self):
        """从人设里挑出她感兴趣的东西（关键词），聊到这些时她更想插话。只算一次，存起来。"""
        p = self.brain.persona
        if self.db.get("qq_interests") is not None or getattr(p, "interests", None):
            return
        prompt = (f"下面是角色「{p.name}」的人设。列出她最感兴趣、聊到就忍不住插话的话题，用 10~20 个简短的中文关键词"
                  "（2~6 个字，只要具体的东西：爱好、作品类型、游戏、食物、专业、宠物、地方），"
                  "不要性格词、说话方式、口头禅（比如“吐槽”“毒舌”“自信”都不要），用顿号隔开，只输出关键词。\n\n" + p.body[:2500])
        try:
            out = await self.brain.llm.chat([{"role": "user", "content": prompt}], role="helper", background=True)
        except Exception as e:  # noqa: BLE001
            log.info("没整理出她的兴趣：%s", e)
            return
        words = [w.strip(" 。.，,\n\"“”") for w in re.split(r"[、，,\n/]+", out or "")]
        words = self.interests_from([w for w in words if 2 <= len(w) <= 8])[:20]
        self.db.set("qq_interests", words)
        log.info("她感兴趣的话题：%s", "、".join(words))

    def _clean_interests(self):
        """1.3 之前自动整理的兴趣词里混着性格词：缓存里有就洗掉（以前只在读的时候过滤，缓存一直是脏的）。"""
        cur = self.db.get("qq_interests")
        if isinstance(cur, list):
            clean = self.interests_from(cur)
            if clean != cur:
                self.db.set("qq_interests", clean)
                log.info("兴趣词里去掉了性格词：%s", "、".join(w for w in cur if w not in clean))
