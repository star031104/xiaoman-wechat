"""空闲时：群里安静太久自己冒个泡、早上补回半夜被叫到的、被戳了戳回去、按安排改签名。idle_tick 每分钟被调一次。"""
from __future__ import annotations

import asyncio
import logging
import random
import re
import time

from aigroup.chat.state import GroupState

log = logging.getLogger("aigroup.group")


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
            if hasattr(self, "share_due") and self.share_due(gid, now, step_minutes):  # 偶尔分享刷到的东西
                try:
                    await self.share(gid, st)
                except Exception as e:  # noqa: BLE001
                    log.info("群 %s 分享出错：%s", gid, e)
                continue
            last = self.db.conn.execute(
                "SELECT ts, role FROM qq_group_messages WHERE group_id=? ORDER BY id DESC LIMIT 1", (gid,)).fetchone()
            if not last or last["role"] == "assistant":        # 群里从没人说过话 / 最后一句就是她：不自言自语
                continue
            if now - last["ts"] < float(g["idle_hours"]) * 3600:
                continue
            if now - float(self.db.get(f"qq_idle_{gid}", 0) or 0) < float(g["idle_gap_hours"]) * 3600:
                continue
            if self.ignored_count(gid, now) >= 2:              # 刚被冷落过：先别冒泡
                continue
            if random.random() >= float(g["idle_chance"]) * step_minutes / 60:
                continue
            self.db.set(f"qq_idle_{gid}", now)
            hours = (now - last["ts"]) / 3600
            why = (f"群里已经安静了 {hours:.0f} 个多小时。你想冒个泡：可以分享你今天遇到的事、正在做的事，"
                   "或者接着之前大家聊过的话题随口问一句。一两句话，自然一点，别说“大家好”“怎么没人说话”这种。"
                   f"聊天记录里最后那些话已经是 {hours:.0f} 个多小时以前的了，提到时别说成“刚才”“刚说”")
            async with st.lock:
                try:
                    await self.speak(gid, mentioned=False, owner_spoke=False, trigger=None, who="", why=why)
                except Exception as e:  # noqa: BLE001
                    log.info("群 %s 冒泡失败：%s", gid, e)

    async def _night_catch_up(self, gid: int, st: GroupState, now: float) -> bool:
        night = self.db.get(f"qq_night_{gid}")
        if isinstance(night, dict):                        # 老格式：只记了一个人
            night = [night]
        if not night:
            return False
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
