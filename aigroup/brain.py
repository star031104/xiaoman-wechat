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
    def life_repeats(text: str, prev_lines: list[str]) -> list[str]:
        """30.3 今天这几行里和前几天哪一行几乎一样（去掉时间段后，两两字组重合一半以上，或者共有 3 个以上的词）。"""
        from aigroup.chat.checks import content_terms
        from aigroup.chat.plan import bigram_overlap
        out = []
        prev = [RANGE_RE.sub("", p).strip() for p in prev_lines if p]
        for ln in [x.strip() for x in str(text or "").splitlines() if x.strip()]:
            body = RANGE_RE.sub("", ln).strip()
            t = content_terms(body)
            for p in prev:
                if bigram_overlap(body, p) >= 0.45 or len(t & content_terms(p)) >= 3:
                    out.append(ln)
                    break
        return out

    async def life_banned(self, prev: list[str]) -> list[str]:
        """30.3 前几天的安排里反复出现的事（“那家烤鸭排队”“11-08 单打混双可能撞”“某某那句快好了”）：今天不许再写。
        让便宜模型挑（中文不好切词，按字比挑出来的都是“鸡的评”这种碎片）。"""
        texts = [str(x) for x in prev if x]
        if len(texts) < 2:
            return []
        prompt = ("下面是一个人前几天每天写的安排。挑出在两天以上都出现的同一件事 / 同一个发现（不管换没换说法），"
                  "每件用 2~8 个字概括（比如“那家烤鸭排队”“11-08 撞时间”），最多 6 件。"
                  "“看群消息”“蹲比赛比分”这种每天都会做的日常不算。\n\n"
                  + "\n\n".join(f"# 第 {i + 1} 天\n{t}" for i, t in enumerate(texts))
                  + '\n\n只输出 JSON：{"items": ["..."]}')
        try:
            d = await self.llm.json([{"role": "user", "content": prompt}], default={}, role="helper")
        except Exception as e:  # noqa: BLE001
            log.info("挑重复的安排失败：%s", e)
            return []
        items = d.get("items") if isinstance(d, dict) else None
        return [str(x).strip()[:12] for x in (items or []) if str(x).strip()][:6]

    @staticmethod
    def hits_banned(line: str, banned: list[str]) -> bool:
        from aigroup.chat.checks import content_terms
        t = content_terms(RANGE_RE.sub("", line))
        for w in banned:
            core = [x for x in content_terms(w) if not x.isdigit()]
            if w in line or (core and len(t & set(core)) >= max(1, min(2, len(core) - 1))):
                return True
            if any(d in line for d in re.findall(r"\d{1,2}-\d{1,2}", w)):
                return True
        return False

    def known_events(self, day: datetime) -> str:
        """30.3 群里真定下的事：没过去的约定 / 提醒（两周内），和这三天刚有结果的。给她排安排时照着写，别猜。"""
        try:
            now = day.timestamp()
            rows = self.db.conn.execute("SELECT who, what, due, status FROM qq_plans WHERE due BETWEEN ? AND ? "
                                        "AND status IN ('open', 'done') ORDER BY due LIMIT 20",
                                        (now - 2 * 86400, now + 7 * 86400)).fetchall()
        except Exception:  # noqa: BLE001
            return ""
        out = []
        for r in rows:
            when = datetime.fromtimestamp(float(r["due"]), day.tzinfo).strftime("%m-%d %H:%M")
            st = "还没到" if str(r["status"]) == "open" and float(r["due"]) > now else "已经过去了"
            out.append(f"- {when} {r['who'] or '大家'}：{r['what']}（{st}）")
        return "\n".join(out)

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
        # 30.3 以前只给前两天的安排、说“别一模一样”：结果一周八天都是“烤鸭排队 / 11-08 单打混双可能撞 / 某某那句快好了”。
        # 现在给前三天的当“别再写”，再给群里真定下的事当“已知的事”（她只能照着写，不许猜）
        prev = [self.db.get(f"life_{(day - timedelta(days=i)):%Y-%m-%d}") for i in (1, 2, 3)]
        prev = [x for x in prev if x and self.valid_life(x)]           # 不像样的不往后传
        prev_lines = [ln.strip(" -•\t") for x in prev for ln in str(x).splitlines() if ln.strip()]
        prev_txt = "\n".join(f"- {ln}" for ln in prev_lines) or "（无）"
        known = self.known_events(day)
        banned = await self.life_banned(prev)
        # 30.3 人设里写死了日子的那几行（“11-08 打球”“圣诞去吃烤鸭”）不进每天的安排：那是背景，不是今天要做的事
        body = "\n".join(ln for ln in p.body.splitlines() if not re.search(r"\d{1,2}\s*[-月./]\s*\d{1,2}|圣诞|元旦|国庆|春节|寒假|暑假", ln))
        ban_txt = f"\n今天不许出现这些（前几天天天写）：{'、'.join(banned)}\n" if banned else ""
        prompt = f"""{body}

你是{p.name}。今天是 {day:%Y-%m-%d} 星期{WEEK[day.weekday()]}。

# 群里真定下的事（写到群友的事只能照这里写；没写的别猜，“好像会撞”“可能”这种猜测不要写；已经过去、有结果的别再写）
{known or "（没有）"}

# 前几天你已经写过的（今天别再写同一件事、同一个发现，换别的事；只有群里有了新进展才能接着写，而且要写新进展）
{prev_txt}

{self._life_ask(p)}
离今天还有一周以上的事今天别写。{ban_txt}只输出安排本身，不要标题和解释。"""
        text, best = "", None
        for attempt in range(3):
            try:
                text = (await self.llm.chat([{"role": "user", "content": prompt}], role="night", max_tokens=600,
                                            temperature=0.8 if attempt else None)).strip()
            except Exception as e:  # noqa: BLE001
                log.warning("生成她今天的安排失败：%s", e)
                return cached or ""
            text = "\n".join(ln.strip(" -•\t") for ln in text.splitlines() if RANGE_RE.match(ln.strip(" -•\t")))
            rep = self.life_repeats(text, prev_lines) + [ln for ln in text.splitlines() if self.hits_banned(ln, banned)]
            rep = list(dict.fromkeys(rep))
            if self.valid_life(text) and not rep:
                break
            if self.valid_life(text) and rep:
                logging.getLogger("aigroup.text.brain").info("今天的安排和前几天重复（%s），重排（第 %d 次）", "、".join(rep)[:60], attempt + 1)
                prompt += f"\n\n（上一版里这几行和前几天几乎一样，换成别的事：{'；'.join(rep)[:200]}）"
                if best is None or len(rep) < best[0]:
                    best = (len(rep), text, rep)
                if attempt == 2:                                  # 三次都重复：用重复最少的那版，去掉重复的行（剩下够 3 行才去）
                    _, text, rep = best
                    keep = [ln for ln in text.splitlines() if ln not in rep]
                    if len(keep) >= 3:
                        text = "\n".join(keep)
                    break
                continue
            logging.getLogger("aigroup.text.brain").info("今天的安排不像样，重排（第 %d 次）：%s", attempt + 1, text[:80])
        else:
            self.db.set(key + "_tried", True)                     # 连着 3 次不像样：今天不再重排（免得每 30 分钟白调 3 次）
            if not text:
                return cached or ""
        self.db.set(key, text)
        return text
