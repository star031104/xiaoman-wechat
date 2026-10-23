"""她自己的生活：今天的安排里此刻在干嘛、是不是在忙、她感兴趣的话题。"""
from __future__ import annotations

import logging
import re
from datetime import datetime

from aigroup.chat.plan import BUSY_RE, TRAIT_WORDS

log = logging.getLogger("aigroup.group")


class LifeMixin:
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
