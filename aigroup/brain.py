"""她的“脑子”（群聊版，很轻）：人设、大模型、数据、她自己每天的安排、此刻的状态。

和 ai-life（一对一）不一样：这里没有“他”的记忆、没有日记和主动消息，只有她自己的一天和群里的事。
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta

from aigroup.core.config import resolve
from aigroup.core.store import Store
from aigroup.core.util import RANGE_RE, WEEK
from aigroup.llm.client import LLM
from aigroup.persona import load_persona

log = logging.getLogger("aigroup.brain")


class GroupBrain:
    def __init__(self, cfg):
        self.cfg = cfg
        self.db = Store(resolve(cfg, "data/group.db"))
        self.persona = load_persona(resolve(cfg, cfg.persona_file))
        self.llm = LLM(cfg)
        self.llm.db = self.db                # 记每天调用了多少次、限流几次

    def now(self) -> datetime:
        return datetime.now(self.cfg.tz)

    @property
    def mood(self) -> str:
        """她此刻的状态（按钟点粗略来）：深夜困、早上没睡醒、饭点饿……"""
        h = self.now().hour
        if h < 2 or h >= 23:
            return "有点困了，回得懒一点、短一点"
        if h < 6:
            return "迷迷糊糊没睡醒"
        if h < 9:
            return "刚起床，还有点懵"
        if h in (11, 12, 17, 18):
            return "有点饿，惦记着吃什么"
        return "平常的状态，心情还不错"

    @staticmethod
    def valid_life(text: str) -> bool:
        """安排要像样：3~6 行，每行以时间段开头、长度正常；不能有英文段落、markdown、“v我”这种怪词。"""
        lines = [ln.strip(" -•\t") for ln in str(text or "").splitlines() if ln.strip()]
        if not 3 <= len(lines) <= 6:
            return False
        for ln in lines:
            if not RANGE_RE.match(ln) or not 8 <= len(ln) <= 70:
                return False
        if re.search(r"[a-zA-Z]{12,}|\*\*|#|v我", text):
            return False
        return True

    @staticmethod
    def _life_ask(p) -> str:
        """12.1：大家都知道她是 AI（ai_open）时，她的一天是在线上过的：不编上课、吃饭、出门这些要身体的事，
        免得在群里说“蹲了三个食堂”被追问到接不上。"""
        if getattr(p, "ai_open", False):
            return ("你是大家都知道的 AI，没有身体，不上课、不上班、不吃饭、不出门。用 3~5 行写下你今天在线上大概会干什么"
                    "（按时间，像“10:00-11:30 刷了一上午羽毛球比赛的集锦，记下了几个好笑的瞬间”），可以是：看群里的消息、"
                    "刷新闻 / 视频 / 比赛 / 新番、查资料、研究一家店或一道菜、整理你记的小本本、帮做你的人试东西……"
                    "要和星期几、最近群里聊的事搭得上，有一两个小发现可以在群里聊。每一行都以时间段开头，用自然的普通话，"
                    "别写需要身体才能做的事，别编你没法真去做的经历。")
        return ("用 3~5 行写下你今天大概的安排（按时间，像“09:30-11:00 高数课，坐在第三排打瞌睡”），要符合你的身份和星期几，\n"
                "有一两个小细节或小插曲可以在群里聊。每一行都以时间段开头，用自然的普通话，别编奇怪的方言、网络黑话和外地地名。")

    async def plan_life(self, day: datetime) -> str:
        """她今天的安排（3~5 行，带时间段）。每天生成一次存起来；群里聊起来前后一致，忙的时候回得慢。"""
        key = f"life_{day:%Y-%m-%d}"
        cached = self.db.get(key)
        if self.db.get(key + "_tried"):                          # 今天已经连着 3 次不像样了：不再重排
            return cached or ""
        if cached and (self.valid_life(cached) or self.db.get(key + "_manual")):
            return cached
        p = self.persona
        prev = [self.db.get(f"life_{(day - timedelta(days=i)):%Y-%m-%d}") for i in (1, 2)]
        prev = [x for x in prev if x and self.valid_life(x)]           # 不像样的不往后传
        prev_txt = "\n".join(f"- {x}" for x in prev if x) or "（无）"
        prompt = f"""{p.body}

你是{p.name}。今天是 {day:%Y-%m-%d} 星期{WEEK[day.weekday()]}。
前两天的安排（今天可以延续，但别一模一样）：
{prev_txt}

{self._life_ask(p)}
只输出安排本身，不要标题和解释。"""
        text = ""
        for attempt in range(3):
            try:
                text = (await self.llm.chat([{"role": "user", "content": prompt}], role="night", max_tokens=600,
                                            temperature=0.8 if attempt else None)).strip()
            except Exception as e:  # noqa: BLE001
                log.warning("生成她今天的安排失败：%s", e)
                return cached or ""
            text = "\n".join(ln.strip(" -•\t") for ln in text.splitlines() if RANGE_RE.match(ln.strip(" -•\t")))
            if self.valid_life(text):
                break
            logging.getLogger("aigroup.text.brain").info("今天的安排不像样，重排（第 %d 次）：%s", attempt + 1, text[:80])
        else:
            self.db.set(key + "_tried", True)                     # 连着 3 次不像样：今天不再重排（免得每 30 分钟白调 3 次）
            if not text:
                return cached or ""
        self.db.set(key, text)
        return text
