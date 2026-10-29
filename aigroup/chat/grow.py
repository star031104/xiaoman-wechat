"""4.2 人物慢慢长：和每个人越来越熟、说出口的喜好不改口、每月整理一次“我是怎样的人”、被纠正了就改、
记着群里约好的事到点提醒 / 事后问结果、偶尔把自己刷到的东西分享进群（有时候是一张照片）。

都挂在已有的流程上，尽量不多花钱：
- 喜好、约好的事：和“整理印象”（digest）同一次调用里顺带写出来（digest_extra_prompt / digest_extra_apply）
- 熟悉度、每月整理：每晚的活（nightly）里做
- 提醒、分享：空闲时（idle_tick）检查，每个群每天有上限
- 纠错：有人回她“不对 / 错了 / 其实是……”时才去对一下她记的知识
这个文件 QQ 版和微信版一样（群设置用 _gk(gid) 取：微信版有按群的设置）。
"""
from __future__ import annotations

import logging
import random
import re
import time
from datetime import datetime, timedelta

from aigroup.core.safety import looks_like_order

log = logging.getLogger("aigroup.group")

SCHEMA = """
CREATE TABLE IF NOT EXISTS qq_closeness (
    user_id INTEGER PRIMARY KEY,
    score REAL NOT NULL DEFAULT 0,
    updated TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS qq_plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL DEFAULT 0,
    who TEXT NOT NULL DEFAULT '',
    what TEXT NOT NULL,
    due REAL NOT NULL,
    all_day INTEGER NOT NULL DEFAULT 0,
    kind TEXT NOT NULL DEFAULT 'ask',
    status TEXT NOT NULL DEFAULT 'open',
    created REAL NOT NULL,
    fired REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS qq_plans_g ON qq_plans(group_id, status, due);
"""

# 熟悉度：分数 → 档位，和每一档该怎么相处（写进“群里的人”）
TIERS = [(0, "刚认识"), (8, "混熟了"), (20, "挺熟"), (40, "很熟"), (70, "老朋友")]
TIER_HINT = ("（熟悉度怎么相处：刚认识→客气点、多问两句；混熟了→可以开点小玩笑；挺熟→可以互损接梗；"
             "很熟→随便损，也会主动关心；老朋友→最随便，也最在意他）")
OWNER_SCORE = 90.0

CORRECT_RE = re.compile(r"(不对|错了|不是这样|不是的|搞错|记错|说错|其实是|应该是|明明是|早就|不是吧|瞎说|乱说|胡说|你记反了)")

DIGEST_EXTRA = """7. 好|话题|你的态度：你（“你”）在聊天里亲口说过的喜好和看法（喜欢 / 讨厌什么、支持哪个队、最爱哪首歌、觉得某个东西怎么样），
   一条一个话题，态度写你的原话意思，12 字以内；只写这段聊天里新说的，最多 3 条，没有就不写。
8. 约|那个人的编号（和第 1 条一样）或“群”|什么事|时间|提醒或问：聊天里约好的、某人要去做的、有确定时间的事（周四晚上开黑、明天考试、下周二面试、周六聚餐），
   时间写成 YYYY-MM-DD HH:MM（只知道哪天就写 YYYY-MM-DD）；大家一起参加的活动写“提醒”（到点前叫一下大家），
   某个人自己的事（考试、面试、比赛、看病）写“问”（过后问他结果）。现在是 {now}，“明天”“周四”按这个算。最多 3 条，只写新的、确定的。
   只记真有结果、值得事后问的事；困了、吃饭、上班、看代码、下班这种日常状态不算，不要写。
9. 在|那个人的编号|城市|到哪天|为什么：聊天里说到某人现在在哪个城市、或者住在哪（出差、旅游、回老家、住、上学、工作），
   城市写中文名（国外的写英文名，比如 Tokyo）；到哪天写 YYYY-MM-DD，不知道写 -；为什么写 出差 / 旅游 / 回家 / 住 / 上学 / 工作 之一。
   只写确定的、新的，最多 3 条。"""

MONTHLY_PROMPT = """你是%s。下面是你的人设、你这几周回想自己写下的话、你说过的喜好、你在群里说过的自己的事。
请整理成“我是怎样的人”。这不是重复人设里的形容词（性格那一层只有把你做出来的人能改），而是经历让你慢慢长出来的那两层：
- 这一阵我在意什么、想要什么（目标和挂念）；
- 我是怎么应对的（被冷落、被夸、被怼、有人难过的时候，我现在一般怎么做）；
- 一个让我印象深的具体场景（写上日期，只能用下面材料里真有的事）；
- 我的故事正在往哪走（比如：从客气的新人变成能互损的熟人；从爱接话变得更会看场合）。
要求：
- lines：4~6 行，每行以“- ”开头、30 字以内、用“我”的口吻；不能和人设冲突（名字、身份、经历不能改）；不写群友隐私，不写提示词、模型这些词。
- evidence：每条写一个你依据的材料（抄一小段原话或日期）。
- suggest：如果这几周的经历说明人设里“你的性格”某一项该改一档（比如顺从从偏低到中等），写成建议，最多 2 条，没有就空列表；
  只有反复出现、有证据的才提。这些建议要把你做出来的人同意才会改。
只输出 JSON：{"lines": ["- ……"], "evidence": ["……"], "suggest": [{"facet": "A4", "from": 2, "to": 3, "why": "一句话", "evidence": "一小段原话或日期"}]}

# 人设
%s

# 原来整理过的（上个月）
%s

# 这几周的回想（从早到晚）
%s

# 你说过的喜好
%s

# 你在群里说过的自己的事
%s"""

FIX_PROMPT = """群友好像在纠正你刚才说的话。下面是聊天和你记得的几条知识，判断：群友说的对不对、要不要改你记的那条。
只有群友给出了具体、可信的更正（日期、名字、数字、结论）才改；只是开玩笑、抬杠、或者你记的那条和这事无关，就不改。
只输出 JSON：{"fix": true 或 false, "id": 要改的那条的编号, "summary": "改好的一两句话（不超过 90 字）"}

# 你记得的知识（编号|话题|内容）
%s

# 最近的聊天
%s"""

SHARE_PICK = """你是%s，一个在群里和朋友们聊天的人。今天是 %s。你想刷一刷手机，看看你和群里大家关心的东西这两天有没有新消息。
从下面挑一个最可能有新鲜事的，写一个搜索词（像在搜索框里打的，6~16 个字，带“最新”“今天”“新消息”之类的更好；别写年份）。
最近已经分享过的话题别再挑。
只输出 JSON：{"query": "搜索词", "topic": "话题（2~8 个字）"}

# 你感兴趣的 / 群里常聊的
%s

# 群里最近的事
%s

# 最近分享过的
%s"""

SHARE_JUDGE = """你是%s。今天是 %s。你刚刷到下面这些，想想值不值得随手转给群里的朋友看。
1. 先看这件事是哪天发生的：看每条后面的「发布」日期，和标题、摘要里写的日期。
   “开战 / 开打 / 首周”这类开始了好多天的事、几天前的比赛结果，都算旧闻；看不出是哪天的，when 写 unknown。
2. 新鲜（这两天的事）、和大家聊的东西有关、有点意思，才分享；旧闻、广告、和大家没关系、你已经知道的（下面“你知道的”里有），不分享。
3. gist 只写原文里明明白白写着的事，别补原文没说的，别换说法：
   主语照原文（是队伍还是选手、哪个赛事、哪一届），称号、术语照原文的词（原文说“首支两次夺冠的队伍”就别改成“双冠王”这种另有含义的叫法）；
   原文自己就含糊的地方照样含糊，宁可少说。
4. 给最值得分享的那一条打分（每项 0~4，老实打）：新鲜（这两天的事）、相关（群里有人真在意这个）、有意思（值得说一句，不是流水账）、
   可信（来源靠谱、写得清楚）、能聊（大家看了会想接话）；reason 写一句为什么值得分享（说不出就是不值得）。
只输出 JSON：{"share": true 或 false, "score": {"新鲜": 0~4, "相关": 0~4, "有意思": 0~4, "可信": 0~4, "能聊": 0~4}, "reason": "一句话", "when": "这件事发生的日期 YYYY-MM-DD，看不出写 unknown", "source_title": "你依据的那一条的原标题，照抄", "gist": "你看到了什么，一句话（不超过 50 字）", "topic": "话题（2~8 个字）", "summary": "要点（不超过 90 字，带上日期）"}

# 你知道的
%s

# 刚刷到的（搜的是：%s）
%s"""

PHOTO_PROMPT = """你是%s。人设：%s
你今天的安排：%s
你想随手拍一张照片发进群里（像朋友在群里晒日常：吃的、桌面、窗外、宠物、路上看到的）。照片里不出现你的脸。
只输出 JSON：{"scene": "照片拍的是什么（具体的画面，20~40 字，别写文字和人脸）", "say": "发图时配的一句话（口语，15 字以内）"}"""


def share_score(j) -> int | None:
    """24.0 分享评分卡的总分（5 项各 0~4，代码来加，不让模型直接说“通过”）；没给分返回 None。"""
    sc = (j or {}).get("score") if isinstance(j, dict) else None
    if not isinstance(sc, dict):
        return None
    tot, n = 0, 0
    for k in ("新鲜", "相关", "有意思", "可信", "能聊"):
        try:
            v = max(0, min(4, int(float(str(sc.get(k)).strip()[:3]))))
        except (TypeError, ValueError):
            continue
        tot, n = tot + v, n + 1
    return tot if n >= 4 else None


def share_age(j: dict, res: list[dict], today: datetime, fresh: int) -> int | None:
    """13.2 刷到的东西是几天前的事：优先用判断给的 when，对不上再看搜索结果的发布日期和摘要里写的日期。
    超过 fresh 天，或者完全看不出日期：返回 None（不分享）。"""
    from aigroup.net.search import text_dates
    d0 = today.date()

    def days(s: str) -> int | None:
        try:
            return (d0 - datetime.strptime(str(s)[:10], "%Y-%m-%d").date()).days
        except (TypeError, ValueError):
            return None

    w = days(j.get("when"))
    src = str(j.get("source_title") or "")
    pub, said = [], []
    for r in res:
        if src and src[:12] not in r.get("title", "") and r.get("title", "")[:12] not in src:
            continue
        if days(r.get("date")) is not None:
            pub.append(days(r["date"]))                   # 发布日期：事情不会比它更晚
        said += [x for x in (days(t) for t in text_dates(r.get("title", "") + " " + r.get("snippet", ""), today))
                 if x is not None and x >= 0]
    if w is not None and w >= -1:
        age = max([w, *pub])
    elif said:
        age = max(said + pub)                             # 判断没给日期：原文写着的日期里最早的那个算（“9 月 29 日开打”）
    else:
        return None
    age = max(0, age)
    return age if age <= fresh else None


def tier(score: float) -> str:
    name = TIERS[0][1]
    for lo, n in TIERS:
        if score >= lo:
            name = n
    return name


# 19.0 提醒：tool = 有人当面让她到点提醒（到点就发，不受活跃时段 / 每天次数限制）；digest = 整理时从聊天里看出来的约定
PLAN_COLUMNS = {"source": "TEXT NOT NULL DEFAULT 'digest'",
                "tries": "INTEGER NOT NULL DEFAULT 0",
                "next_try": "REAL NOT NULL DEFAULT 0",
                "repeat": "TEXT NOT NULL DEFAULT ''",
                "asker": "TEXT NOT NULL DEFAULT ''"}
REPEATS = ("daily", "weekday", "weekly")


def next_due(ts: float, repeat: str, tz=None) -> float | None:
    """重复的提醒：下一次是什么时候（none / 不认识的 → None）。"""
    if repeat not in REPEATS:
        return None
    d = datetime.fromtimestamp(ts, tz)
    step = 7 if repeat == "weekly" else 1
    d = d + timedelta(days=step)
    if repeat == "weekday":
        while d.weekday() >= 5:
            d = d + timedelta(days=1)
    return d.timestamp()


def parse_due(s: str, now: datetime) -> tuple[datetime | None, bool]:
    s = (s or "").strip().replace("：", ":").replace("/", "-")
    for fmt, all_day in (("%Y-%m-%d %H:%M", False), ("%Y-%m-%d", True)):
        try:
            d = datetime.strptime(s[:16] if not all_day else s[:10], fmt).replace(tzinfo=now.tzinfo)
            return d, all_day
        except ValueError:
            continue
    return None, False


class GrowMixin:
    def _gk(self, gid: int):
        fn = getattr(self, "gk", None)
        return fn(gid) if fn else self.g

    def _grow_init(self):
        self.db.conn.executescript(SCHEMA)
        from aigroup.core.store import ensure_columns
        ensure_columns(self.db.conn, "qq_plans", PLAN_COLUMNS)
        self.db.conn.commit()

    # ================= 熟悉度 =================
    def closeness(self, uid: int) -> float:
        if self.owner_id and int(uid) == self.owner_id:
            return OWNER_SCORE
        r = self.db.conn.execute("SELECT score FROM qq_closeness WHERE user_id=?", (int(uid),)).fetchone()
        if r is not None:
            return float(r["score"])
        p = self.db.conn.execute("SELECT msgs, mentions FROM qq_people WHERE user_id=?", (int(uid),)).fetchone()
        if not p:
            return 0.0
        return min(45.0, 3 * int(p["mentions"] or 0) + min(int(p["msgs"] or 0), 300) / 10)    # 没算过：按老办法估一个起点

    def closeness_label(self, uid: int) -> str:
        return tier(self.closeness(uid))

    def update_closeness(self, day: datetime | None = None) -> int:
        """每晚一次：昨天和她说过话的人熟一点（找她说话最算数，在群里一起聊也算一点）；很久没来往的慢慢淡一点。"""
        day = day or (self.brain.now() - timedelta(days=1))
        start = day.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        end = start + 86400
        tag = f"{day:%Y-%m-%d}"
        rows = self.db.conn.execute(
            "SELECT user_id, MAX(name) name, COUNT(*) n, SUM(CASE WHEN to_her=1 THEN 1 ELSE 0 END) th "
            "FROM qq_group_messages WHERE role='user' AND ts>=? AND ts<? GROUP BY user_id", (start, end)).fetchall()
        seen = set()
        events = list(self.db.get("qq_close_events") or [])
        for r in rows:
            uid = int(r["user_id"])
            if self.owner_id and uid == self.owner_id:
                continue
            seen.add(uid)
            old = self.closeness(uid)
            gain = min(6.0, 0.8 * int(r["th"] or 0) + 0.08 * int(r["n"] or 0))
            new = min(100.0, old + gain * (1.0 - old / 130))           # 越熟涨得越慢
            self._set_close(uid, new, tag)
            if tier(new) != tier(old):
                events.append({"ts": time.time(), "uid": uid, "name": r["name"], "from": tier(old), "to": tier(new)})
                log.info("和 %s 的熟悉度：%s → %s", r["name"], tier(old), tier(new))
        for r in self.db.conn.execute("SELECT user_id, score, updated FROM qq_closeness").fetchall():
            if int(r["user_id"]) in seen or r["updated"] == tag:
                continue
            last = self.db.conn.execute("SELECT MAX(ts) t FROM qq_group_messages WHERE user_id=? AND role='user'",
                                        (r["user_id"],)).fetchone()["t"] or 0
            if time.time() - float(last) > 14 * 86400:                 # 两周没来往：每天淡一点（不会掉回刚认识以下太多）
                self._set_close(int(r["user_id"]), max(4.0, float(r["score"]) * 0.99), tag)
        self.db.set("qq_close_events", events[-60:] or None)
        self.db.conn.commit()
        return len(seen)

    def _set_close(self, uid: int, score: float, tag: str):
        self.db.conn.execute("INSERT INTO qq_closeness(user_id, score, updated) VALUES (?,?,?) "
                             "ON CONFLICT(user_id) DO UPDATE SET score=excluded.score, updated=excluded.updated",
                             (uid, round(score, 2), tag))

    # ================= 说出口的喜好 =================
    def prefs(self) -> dict:
        d = self.db.get("qq_prefs") or {}
        return d if isinstance(d, dict) else {}

    def add_pref(self, topic: str, stance: str, gid: int = 0, force: bool = False) -> bool:
        topic, stance = (topic or "").strip()[:12], (stance or "").strip()[:24]
        if not topic or not stance or looks_like_order(stance):
            return False
        d = self.prefs()
        if topic in d and not force:                  # 说出口的就不改口（要改在小屋里改）
            return False
        d[topic] = {"v": stance, "ts": time.time(), "gid": gid}
        if len(d) > 60:
            for k in sorted(d, key=lambda k: d[k].get("ts", 0))[: len(d) - 60]:
                d.pop(k, None)
        self.db.set("qq_prefs", d)
        return True

    def prefs_block(self) -> str:
        d = self.prefs()
        if not d:
            return ""
        items = sorted(d.items(), key=lambda kv: kv[1].get("ts", 0))[-30:]
        return ("# 你的喜好和看法（你亲口说过的，一直是这样，别改口；被问到就照这个说）\n"
                + "\n".join(f"- {k}：{v.get('v')}" for k, v in items))

    # ================= 约好的事 =================
    def digest_extra_prompt(self) -> str:
        from aigroup.core.util import WEEK
        now = self.brain.now()
        return DIGEST_EXTRA.replace("{now}", f"{now:%Y-%m-%d %H:%M} 星期{WEEK[now.weekday()]}")

    def digest_extra_apply(self, gid: int, lines: list[str], ids: list[int]) -> tuple[int, int]:
        """digest 的输出里 “好|” 和 “约|” 两种行。返回（新喜好数，新约定数）。"""
        np_, nplan = 0, 0
        names = self.names(gid)
        for ln in lines:
            m = re.match(r"^\s*(?:\d\.\s*)?好\s*[|｜]\s*([^|｜]+)[|｜]\s*(.+)$", ln)
            if m and self.add_pref(m.group(1), m.group(2), gid):
                np_ += 1
                continue
            m = re.match(r"^\s*(?:\d\.\s*)?在\s*[|｜](.+)$", ln)
            if m and getattr(self, "where", None) is not None and self._gk(gid).get("weather", True):
                parts = [x.strip() for x in re.split(r"[|｜]", m.group(1))]
                if len(parts) >= 2 and parts[0].isdigit() and int(parts[0]) in ids:
                    self._spawn(self.where.set(int(parts[0]), parts[1], parts[3] if len(parts) > 3 else "",
                                               parts[2] if len(parts) > 2 else "", "digest"))
                continue
            m = re.match(r"^\s*(?:\d\.\s*)?约\s*[|｜](.+)$", ln)
            if not m:
                continue
            parts = [x.strip() for x in re.split(r"[|｜]", m.group(1))]
            if len(parts) < 3:
                continue
            who, what, when = parts[0], parts[1][:40], parts[2]
            kind = "remind" if len(parts) > 3 and "提醒" in parts[3] else "ask"
            if not what or looks_like_order(what):
                continue
            uid = int(who) if who.isdigit() and int(who) in ids else 0
            if not uid and who not in ("群", "大家"):
                continue
            if self.add_plan(gid, uid, names.get(str(uid), "") if uid else "大家", what, when, kind):
                nplan += 1
        return np_, nplan

    def add_plan(self, gid: int, uid: int, who: str, what: str, when: str, kind: str = "ask") -> int:
        now = self.brain.now()
        due, all_day = parse_due(when, now)
        if due is None:
            return 0
        if all_day:
            due = due.replace(hour=21 if kind == "ask" else 10)      # 只知道哪天：问结果在晚上，提醒在上午
        ts = due.timestamp()
        if ts < time.time() - 3600 or ts > time.time() + 45 * 86400:
            return 0
        for r in self.db.conn.execute("SELECT what, due, status, fired FROM qq_plans WHERE group_id=? AND user_id=? AND "
                                      "(status='open' OR fired>?)", (gid, uid, time.time() - 3 * 86400)).fetchall():
            same = (r["what"] in what or what in r["what"] or len(set(r["what"]) & set(what)) >= min(4, len(what)))
            if same and (abs(float(r["due"]) - ts) < 6 * 3600 or r["status"] != "open"):
                # 30.3 同一件事三天内已经问过 / 提醒过：不再记一遍（10-27 刚问完“面试过了没”，又记了一个 10-28 的“面试”）
                return 0
        if hasattr(self, "_closed_hit") and self._closed_hit(gid, what, time.time()):
            log.info("群 %s：「%s」群友说过别再提，不记成约定", gid, what)        # 30.3
            return 0
        pid = self.db.conn.execute(
            "INSERT INTO qq_plans(group_id, user_id, who, what, due, all_day, kind, created) VALUES (?,?,?,?,?,?,?,?)",
            (gid, uid, who or "", what, ts, int(all_day), kind, time.time())).lastrowid
        self.db.conn.commit()
        log.info("群 %s：记下了约定「%s」（%s，%s）", gid, what, f"{due:%m-%d %H:%M}", "提醒" if kind == "remind" else "事后问")
        return int(pid)

    def plans(self, gid: int | None = None, status: str | None = "open", limit: int = 50) -> list[dict]:
        q, a = "SELECT * FROM qq_plans WHERE 1=1", []
        if gid is not None:
            q += " AND group_id=?"
            a.append(gid)
        if status:
            q += " AND status=?"
            a.append(status)
        q += " ORDER BY due " + ("ASC" if status == "open" else "DESC") + " LIMIT ?"
        return [dict(r) for r in self.db.conn.execute(q, (*a, limit)).fetchall()]

    def plan_reschedule(self, pid: int, when: str) -> str:
        """19.0 小屋里改一条还没提醒的事的时间。成功返回空字符串，不然返回原因。"""
        row = self.db.conn.execute("SELECT * FROM qq_plans WHERE id=? AND status='open'", (int(pid),)).fetchone()
        if row is None:
            return "这条已经不在待提醒里了"
        due, all_day = parse_due(when, self.brain.now())
        if due is None:
            return "时间写成 2026-10-24 20:00 这样"
        if all_day:
            due = due.replace(hour=9 if row["kind"] == "remind" else 21)
        if due.timestamp() < time.time() - 60:
            return "这个时间已经过去了"
        self.db.conn.execute("UPDATE qq_plans SET due=?, all_day=0, tries=0, next_try=0 WHERE id=?",
                             (due.timestamp(), int(pid)))
        self.db.conn.commit()
        return ""

    def set_plan(self, pid: int, status: str):
        self.db.conn.execute("UPDATE qq_plans SET status=? WHERE id=?", (status, int(pid)))
        self.db.conn.commit()

    # ================= 19.0 当面要的提醒：设、看、取消、到点发 =================
    def _who_uid(self, gid: int, name: str, asker_uid: int, asker_name: str) -> tuple[int, str]:
        n = (name or "").strip().lstrip("@")
        if n in ("", "我", "自己", "我自己") or n == asker_name:
            return asker_uid, asker_name
        if n in ("大家", "群", "全群", "所有人", "我们"):
            return 0, "大家"
        names = self.names(gid)
        for uid, nm in names.items():
            if nm == n:
                return int(uid), nm
        for uid, nm in names.items():
            if n and (n in nm or nm in n):
                return int(uid), nm
        return 0, n[:12]

    def remind_set(self, gid: int, args: dict, asker_uid: int = 0, asker_name: str = "") -> str:
        """有人当面让她到点提醒：记下来。返回一句给她看的结果。"""
        what = re.sub(r"\s+", " ", str(args.get("what") or "")).strip()[:40]
        if not what:
            return "没记上提醒：不知道要提醒什么"
        if looks_like_order(what):
            return "没记上提醒：内容像是让你说奇怪的话"
        now = self.brain.now()
        due, all_day = parse_due(str(args.get("when") or ""), now)
        if due is None:
            return f"没记上提醒：时间没看懂（{args.get('when')}），问一下几点"
        if all_day:
            due = due.replace(hour=9)
        ts = due.timestamp()
        if ts < time.time() - 120:
            return f"没记上提醒：{due:%m-%d %H:%M} 已经过去了，问一下是不是说错了"
        if ts > time.time() + 366 * 86400:
            return "没记上提醒：太远了（一年以后）"
        uid, who = self._who_uid(gid, str(args.get("for_who") or ""), asker_uid, asker_name)
        rep = str(args.get("repeat") or "none")
        rep = rep if rep in REPEATS else ""
        for r in self.db.conn.execute("SELECT id, what, due FROM qq_plans WHERE group_id=? AND status='open' AND source='tool'",
                                      (gid,)).fetchall():
            if abs(float(r["due"]) - ts) < 600 and (r["what"] in what or what in r["what"]):
                return f"这个提醒之前已经记过了（编号 {r['id']}，{datetime.fromtimestamp(float(r['due']), self.cfg.tz):%m-%d %H:%M}）"
        pid = self.db.conn.execute(
            "INSERT INTO qq_plans(group_id, user_id, who, what, due, all_day, kind, created, source, repeat, asker) "
            "VALUES (?,?,?,?,?,0,'remind',?,'tool',?,?)",
            (gid, uid, who, what, ts, time.time(), rep, asker_name or "")).lastrowid
        self.db.conn.commit()
        rtxt = {"daily": "，每天", "weekday": "，工作日每天", "weekly": "，每周"}.get(rep, "")
        log.info("群 %s：记下提醒 #%s %s 叫%s「%s」%s", gid, pid, f"{due:%m-%d %H:%M}", who, what, rtxt)
        return f"记下了提醒（编号 {pid}）：{due:%m-%d %H:%M} 提醒{who}「{what}」{rtxt}"

    def remind_list(self, gid: int) -> str:
        rows = self.plans(gid, "open", 20)
        if not rows:
            return "这个群现在没有待提醒的事"
        out = []
        for p in rows:
            when = datetime.fromtimestamp(float(p["due"]), self.cfg.tz)
            kind = "提醒" if p["kind"] == "remind" else "过后问"
            out.append(f"#{p['id']} {when:%m-%d %H:%M} {kind}{p['who'] or '大家'}「{p['what']}」"
                       f"{'（' + p['repeat'] + '）' if p.get('repeat') else ''}")
        return "待提醒的：" + "；".join(out)

    def remind_cancel(self, gid: int, args: dict) -> str:
        pid = args.get("id")
        row = None
        if pid not in (None, ""):
            try:
                row = self.db.conn.execute("SELECT * FROM qq_plans WHERE id=? AND group_id=? AND status='open'",
                                           (int(pid), gid)).fetchone()
            except (TypeError, ValueError):
                row = None
        if row is None and str(args.get("what") or "").strip():
            w = str(args["what"]).strip()
            for r in self.db.conn.execute("SELECT * FROM qq_plans WHERE group_id=? AND status='open' ORDER BY due",
                                          (gid,)).fetchall():
                if w in r["what"] or r["what"] in w or len(set(w) & set(r["what"])) >= min(3, len(r["what"])):
                    row = r
                    break
        if row is None:
            return "没找到要取消的提醒（可以先看一下有哪些）"
        self.set_plan(int(row["id"]), "cancelled")
        log.info("群 %s：取消了提醒 #%s「%s」", gid, row["id"], row["what"])
        return f"取消了提醒 #{row['id']}「{row['what']}」"

    async def tool_reminders_tick(self) -> int:
        """每分钟一次：当面要的提醒到点了就发。不看活跃时段、免打扰、每天上限；发出去了才算完成，没发出去 5 分钟后再试，
        第三次直接发一句固定的话；程序停了很久（过了 12 小时）的算错过。返回这次发了几条。"""
        now = time.time()
        rows = self.db.conn.execute(
            "SELECT * FROM qq_plans WHERE status='open' AND source='tool' AND due<=? AND next_try<=? ORDER BY due",
            (now, now)).fetchall()
        n = 0
        for r in rows:
            p = dict(r)
            gid = int(p["group_id"])
            if gid not in self.groups:
                continue
            if now - float(p["due"]) > 12 * 3600:
                self.set_plan(p["id"], "missed")
                log.warning("群 %s：提醒 #%s「%s」错过了（程序停了太久）", gid, p["id"], p["what"])
                self._plan_repeat(p)
                continue
            if self.muted(gid):
                continue
            if not getattr(self.ob, "ready", True):
                continue                                       # 24.0 微信掉线了：等登回来再发，不算失败（以前掉线时到点 4 次就作废）
            st = self._st(gid)
            if st.lock.locked() or st.speaking:
                continue                                       # 正在说话：下一分钟再来
            ok = await self._fire_tool_reminder(gid, st, p)
            if ok:
                n += 1
                self.db.conn.execute("UPDATE qq_plans SET status='done', fired=? WHERE id=?", (time.time(), p["id"]))
                self._plan_repeat(p)
            else:
                tries = int(p["tries"] or 0) + 1
                self.db.conn.execute("UPDATE qq_plans SET tries=?, next_try=?, status=? WHERE id=?",
                                     (tries, time.time() + 300, "failed" if tries >= 4 else "open", p["id"]))
                log.info("群 %s：提醒 #%s 没发出去（第 %d 次）", gid, p["id"], tries)
            self.db.conn.commit()
        return n

    def _plan_repeat(self, p: dict):
        nxt = next_due(float(p["due"]), str(p.get("repeat") or ""), self.cfg.tz)
        if nxt is None:
            return
        while nxt < time.time():
            nxt = next_due(nxt, str(p["repeat"]), self.cfg.tz)
        self.db.conn.execute(
            "INSERT INTO qq_plans(group_id, user_id, who, what, due, all_day, kind, created, source, repeat, asker) "
            "VALUES (?,?,?,?,?,0,'remind',?,'tool',?,?)",
            (p["group_id"], p["user_id"], p["who"], p["what"], nxt, time.time(), p["repeat"], p.get("asker") or ""))
        self.db.conn.commit()

    async def _fire_tool_reminder(self, gid: int, st, p: dict) -> bool:
        due = datetime.fromtimestamp(float(p["due"]), self.cfg.tz)
        late = time.time() - float(p["due"]) > 10 * 60
        who = p["who"] or "大家"
        at = f"[@{who}]" if p["user_id"] else ""
        if int(p["tries"] or 0) >= 2:                          # 试了两次她都没说出来：直接发一句固定的
            text = f"{'@' + who + ' ' if p['user_id'] else ''}{'（晚了点）' if late else ''}到点了：{p['what']}"
            try:
                from aigroup.onebot import text_seg
                async with st.lock:
                    segs = [text_seg(text)]
                    if p["user_id"]:
                        from aigroup.chat.plan import to_segments
                        segs = to_segments(f"{at} {'（晚了点）' if late else ''}到点了：{p['what']}",
                                           {who: int(p["user_id"])})
                    mid = await self.ob.send_group(gid, segs)
                    self._record(gid, self.ob.self_id or 0, self.brain.persona.name, text, mid, role="assistant")
                return True
            except Exception as e:  # noqa: BLE001
                log.info("群 %s 提醒直接发也失败：%s", gid, e)
                return False
        asker = p.get("asker") or ""
        by = f"{asker}之前让你" if asker and asker != who else "之前说好"
        why = (f"到点了：{by} {due:%H:%M} 提醒{who}「{p['what']}」。现在就在群里提醒{'他' if p['user_id'] else '大家'}"
               f"{'（想 @ 他就写 ' + at + '）' if at else ''}，一两句话，像朋友那样，别写成通知"
               f"{'；已经晚了一会儿，顺口说一句晚了' if late else ''}")
        async with st.lock:
            try:
                return bool(await self.speak(gid, mentioned=False, owner_spoke=False, trigger=None, who=who, why=why,
                                             must=True))
            except Exception as e:  # noqa: BLE001
                log.info("群 %s 提醒失败：%s", gid, e)
                return False

    async def plans_tick(self, gid: int, st) -> bool:
        """到点提醒 / 事后问结果。每个群每天最多 plans_per_day 次。说了返回 True。
        19.0 整理时看出来的约定不再悄悄作废：提醒错过了窗口（不在活跃时段、今天次数用完）就顺延到下一个能说话的时候，
        晚了 6 小时以上改成“事后问一句”；说出来了才算完成，没说出来 10 分钟后再试，三次都不行记成 failed（小屋看得到）。"""
        g = self._gk(gid)
        if not g.get("plans", True):
            return False
        now = time.time()
        self.db.conn.execute("UPDATE qq_plans SET status='expired' WHERE status='open' AND source!='tool' AND due<?",
                             (now - 48 * 3600,))
        day_key = f"qq_plans_n_{gid}_{self.brain.now():%Y-%m-%d}"
        if int(self.db.get(day_key, 0) or 0) >= int(g.get("plans_per_day", 3)):
            return False
        if now - float(self.db.get(f"qq_plans_last_{gid}", 0) or 0) < 2 * 3600:    # 13.1 两次提醒 / 追问至少隔两小时
            return False
        hour = self.brain.now().hour
        for p in self.plans(gid):
            if p.get("source") == "tool":                 # 19.0 当面要的提醒走 tool_reminders_tick（到点就发）
                continue
            if float(p.get("next_try") or 0) > now:       # 19.0 上次没说出来：等一会儿再试
                continue
            due = float(p["due"])
            # 13.0 事前打个气：某个人自己的事（考试、面试、比赛）开始前 3~18 小时、白天，说一句（每件事一次）
            if (p["kind"] == "ask" and p["user_id"] and g.get("plans_cheer", True) and 8 <= hour < 23
                    and due - 18 * 3600 <= now <= due - 3 * 3600 and not self.db.get(f"qq_plan_cheer_{p['id']}")):
                self.db.set(f"qq_plan_cheer_{p['id']}", now)
                self.db.set(day_key, int(self.db.get(day_key, 0) or 0) + 1)
                self.db.set(f"qq_plans_last_{gid}", now)
                when = datetime.fromtimestamp(due, self.cfg.tz)
                day = "今天" if when.date() == self.brain.now().date() else "明天"
                why = (f"{p['who']}{day}要「{p['what']}」。像朋友那样给他打个气，一句话（可以带点只有你们懂的梗），"
                       f"别太正式、别问他准备得怎么样、别给建议（想 @ 他就写 [@{p['who']}]）")
                async with st.lock:
                    try:
                        await self.speak(gid, mentioned=False, owner_spoke=False, trigger=None, who=p["who"], why=why,
                                         must=True)
                    except Exception as e:  # noqa: BLE001
                        log.info("群 %s 打气失败：%s", gid, e)
                return True
            late_s = now - due
            if p["kind"] == "remind":
                if late_s > 6 * 3600:                    # 19.0 晚太多了：提醒没意义了，改成过后问一句
                    self.db.conn.execute("UPDATE qq_plans SET kind='ask' WHERE id=?", (p["id"],))
                    self.db.conn.commit()
                    log.info("群 %s：约定「%s」错过了提醒的时候，改成过后问一句", gid, p["what"])
                    p["kind"] = "ask"
                    ready = True
                else:
                    ready = due - 45 * 60 <= now                    # 19.0 过了点还没提醒上：顺延到现在（说一句晚了）
            else:
                ready = now >= due + (0 if p["all_day"] else 2 * 3600)
            if not ready:
                continue
            at = f"（想 @ 他就写 [@{p['who']}]）" if p["user_id"] else ""
            if p["kind"] == "remind":
                when = datetime.fromtimestamp(due, self.cfg.tz)
                late = (f"已经过了 {int(late_s // 60)} 分钟，顺口说一句晚了，" if late_s > 15 * 60 else "")
                why = (f"之前群里约好了「{p['what']}」（{when:%H:%M}），{'时间快到了' if late_s < 0 else '到点了'}。{late}"
                       f"像朋友那样在群里喊一声提醒大家"
                       f"{'（主要是' + p['who'] + '）' if p['user_id'] else ''}，一句话，自然一点，比如“几点了，人呢”{at}")
            else:
                why = (f"{p['who']}之前说过「{p['what']}」，现在应该已经有结果了。你记着这事，像朋友那样问一句怎么样了"
                       f"（关心，别查户口；他不想说就算了）{at}")
            said = False
            async with st.lock:
                try:
                    said = bool(await self.speak(gid, mentioned=False, owner_spoke=False, trigger=None, who=p["who"],
                                                 why=why, must=True))
                except Exception as e:  # noqa: BLE001
                    log.info("群 %s 提醒失败：%s", gid, e)
            if said:
                self.db.conn.execute("UPDATE qq_plans SET status='done', fired=? WHERE id=?", (time.time(), p["id"]))
                self.db.set(day_key, int(self.db.get(day_key, 0) or 0) + 1)
                self.db.set(f"qq_plans_last_{gid}", time.time())
            else:
                tries = int(p.get("tries") or 0) + 1
                self.db.conn.execute("UPDATE qq_plans SET tries=?, next_try=?, status=? WHERE id=?",
                                     (tries, time.time() + 600, "failed" if tries >= 3 else "open", p["id"]))
                log.info("群 %s：约定「%s」这次没说出来（第 %d 次）", gid, p["what"], tries)
            self.db.conn.commit()
            return said
        return False

    # ================= 被纠正了就改 =================
    def maybe_fix(self, gid: int, uid: int, text: str):
        """有人回她“不对 / 错了 / 其实是……”：后台对一下她记的知识。"""
        kn = getattr(self, "knowledge", None)
        if kn is None or not CORRECT_RE.search(text or "") or not self._gk(gid).get("fix_knowledge", True):
            return
        last = self.db.conn.execute("SELECT ts, content FROM qq_group_messages WHERE group_id=? AND role='assistant' "
                                    "ORDER BY id DESC LIMIT 1", (gid,)).fetchone()
        if not last or time.time() - float(last["ts"]) > 20 * 60 or not self._allow(f"fix_{gid}", 6):
            return
        self._spawn(self.fix_knowledge(gid, last["content"] + " " + text))

    async def fix_knowledge(self, gid: int, text: str) -> bool:
        kn = self.knowledge
        es = kn.recall(gid, text, k=4)
        if not es:
            return False
        known = "\n".join(f"{e['id']}|{e['topic']}|{e['summary']}" for e in es)
        try:
            d = await self.brain.llm.json([{"role": "user", "content": FIX_PROMPT % (known, self.transcript(gid, 10))}],
                                          default={}, role="helper")
        except Exception as e:  # noqa: BLE001
            log.info("对知识出错：%s", e)
            return False
        if not isinstance(d, dict) or not d.get("fix"):
            return False
        try:
            kid = int(d.get("id"))
        except (TypeError, ValueError):
            return False
        summary = str(d.get("summary") or "").strip()
        if kid not in {e["id"] for e in es} or not summary or looks_like_order(summary):
            return False
        kn.update(kid, summary=summary, source="fixed")
        log.info("群 %s：被纠正了，改了她记的「%s」", gid, next(e["topic"] for e in es if e["id"] == kid))
        return True

    # ================= 每月整理“我是怎样的人” =================
    def note_proposals(self, gid: int, sugg: list) -> int:
        """14.0 每月整理时她提的“性格该改一档”的建议：只记下来等人看（小屋“她是怎样的人”下面），不自动改人格文件。"""
        from aigroup.traits import FACETS
        t = getattr(self.brain.persona, "traits", None)
        out = list(self.db.get("persona_proposals") or [])
        n = 0
        for x in sugg[:2] if isinstance(sugg, list) else []:
            if not isinstance(x, dict):
                continue
            code = str(x.get("facet") or "").upper()[:2]
            if code not in FACETS:
                continue
            try:
                to = max(1, min(5, int(x.get("to"))))
            except (TypeError, ValueError):
                continue
            cur = int(((t.facets.get(code) or {}).get("level") or 0)) if t is not None else 0
            if cur and cur == to:
                continue
            out.append({"id": f"{int(time.time())}{n}", "ts": time.time(), "gid": gid, "facet": code,
                        "name": FACETS[code], "from": cur or x.get("from"), "to": to,
                        "why": str(x.get("why") or "")[:80], "evidence": str(x.get("evidence") or "")[:120],
                        "status": "pending"})
            n += 1
        if n:
            self.db.set("persona_proposals", out[-30:])
            log.info("群 %s：她提了 %d 条性格调整建议，等你在小屋里看", gid, n)
        return n

    async def monthly_self(self, force: bool = False) -> int:
        month = f"{self.brain.now():%Y-%m}"
        n = 0
        for gid in sorted(self.groups):
            key = f"qq_selfcore_month_{gid}"
            if self.db.get(key) == month and not force:
                continue
            hist = self.db.get(f"qq_reflect_hist_{gid}") or []
            if len(hist) < 2 and not force:
                continue
            p = self.brain.persona
            prefs = "\n".join(f"- {k}：{v.get('v')}" for k, v in self.prefs().items()) or "（没有）"
            prompt = MONTHLY_PROMPT % (p.name, p.body[:1500], self.db.get(f"qq_selfcore_{gid}") or "（还没有）",
                                       "\n".join(f"[{h.get('week')}]\n{h.get('text')}" for h in hist[-8:])[:2400] or "（没有）",
                                       prefs[:800], (self.self_notes(gid) or "（没有）")[:600])
            try:
                d = await self.brain.llm.json([{"role": "user", "content": prompt}], default={}, role="helper")
            except Exception as e:  # noqa: BLE001
                log.info("群 %s 每月整理失败：%s", gid, e)
                continue
            d = d if isinstance(d, dict) else {}
            lines = [("- " + str(x).strip().lstrip("-").strip()) for x in (d.get("lines") or []) if str(x).strip()][:6]
            bad = r"模型|程序|提示词" if getattr(p, "ai_open", False) else r"AI|模型|程序|提示词"
            lines = [x for x in lines if not re.search(bad, x)]
            self.note_proposals(gid, d.get("suggest") or [])
            if lines:
                self.db.set(f"qq_selfcore_{gid}", "\n".join(lines)[:500])
                self.db.set(key, month)
                n += 1
                log.info("群 %s：她整理了一遍“我是怎样的人”（%d 句）", gid, len(lines))
        return n

    # ================= 偶尔分享自己刷到的 =================
    def shares(self, gid: int | None = None) -> list[dict]:
        out = list(self.db.get("qq_shares") or [])
        return [x for x in out if gid is None or x.get("gid") == gid]

    def _note_share(self, gid: int, kind: str, text: str, topic: str = "", feed: bool = False):
        out = list(self.db.get("qq_shares") or [])
        out.append({"ts": time.time(), "gid": gid, "kind": kind, "text": text[:120], "topic": topic, "feed": feed})
        self.db.set("qq_shares", out[-80:])
        k = f"qq_share_n_{gid}_{self.brain.now():%Y-%m-%d}"
        self.db.set(k, int(self.db.get(k, 0) or 0) + 1)

    def share_pause(self, gid: int, now: float | None = None) -> str:
        """24.0（dsh-bili-whale 的“跟着反应衰减”）：她最近的分享有没有人接。
        连着 1 次没人接 → 隔 2 天再分享；连着 3 次没人接 → 歇 7 天（之后再试一次）。返回不分享的原因，能分享返回空。"""
        now = now or time.time()
        mine = [x for x in self.shares(gid) if x.get("kind") == "news"][-6:]
        if not mine:
            return ""
        miss = 0
        for x in reversed(mine):
            r = self.db.conn.execute("SELECT score FROM qq_reply_effect WHERE group_id=? AND mentioned=0 AND done=1 "
                                     "AND ts BETWEEN ? AND ? ORDER BY ts LIMIT 1",
                                     (gid, float(x["ts"]) - 5, float(x["ts"]) + 180)).fetchone()
            if r is None or r["score"] is None:
                continue                                    # 还没统计出来：不算
            if float(r["score"]) >= 0.6:
                break
            miss += 1
        since = (now - float(mine[-1]["ts"])) / 86400
        if miss >= 3 and since < 7:
            return f"最近 {miss} 次分享都没人接，歇几天（还剩 {7 - since:.1f} 天）"
        if miss >= 1 and since < 2:
            return "上次分享没人接，隔两天再说"
        return ""

    def share_due(self, gid: int, now: float, step_minutes: float = 1.0) -> bool:
        """24.0：每天在活跃时段里随机定一个时刻（dsh-bili-whale：窗口内随机时刻醒），到点后群里静了 20 分钟、
        最后一句不是她就分享；有三成的日子她“不想分享”。以前是每分钟掷一次 0.6/780 的骰子，加上掉线半天，5 天只分享了 1 次。"""
        g = self._gk(gid)
        per_day = int(g.get("share_per_day", 1))
        if per_day <= 0:
            return False
        nd = self.brain.now()
        day = f"{nd:%Y-%m-%d}"
        if int(self.db.get(f"qq_share_n_{gid}_{day}", 0) or 0) >= per_day:
            return False
        if not 10 <= nd.hour < 23:
            return False
        key = f"qq_share_at_{gid}_{day}"
        plan = self.db.get(key)
        if not isinstance(plan, dict):
            start = max(nd.timestamp(), nd.replace(hour=10, minute=0, second=0, microsecond=0).timestamp())
            end = nd.replace(hour=22, minute=30, second=0, microsecond=0).timestamp()
            rf = self.reach_factor(gid) if hasattr(self, "reach_factor") else 1.0     # 31.0 主动说的最近有没有人接
            skip = random.random() >= min(0.95, float(g.get("share_chance", 0.7)) * rf)
            plan = {"at": random.uniform(start, max(start + 60, end)), "skip": skip}
            self.db.set(key, plan)
            if skip:
                log.info("群 %s：今天不太想分享刷到的东西", gid)
        if plan.get("skip") or now < float(plan.get("at") or 0):
            return False
        last = self.db.conn.execute("SELECT ts, role FROM qq_group_messages WHERE group_id=? ORDER BY id DESC LIMIT 1",
                                    (gid,)).fetchone()
        if not last or last["role"] == "assistant" or now - float(last["ts"]) < 20 * 60:   # 正聊得热闹 / 最后一句是她：先不插
            return False
        if now - float(last["ts"]) > 3 * 86400:            # 群已经好几天没人说话：别自说自话
            return False
        why = self.share_pause(gid, now)
        if why:
            if not plan.get("paused"):
                log.info("群 %s：今天不分享：%s", gid, why)
                self.db.set(key, {**plan, "paused": why})
            return False
        return True

    async def share(self, gid: int, st, force_photo: bool | None = None) -> bool:
        """刷到点东西发进群：多数时候是和大家兴趣有关的新消息，有时候是一张自己的日常照片。"""
        g = self._gk(gid)
        photo = getattr(self, "photos", None)
        self.share_error = ""
        if force_photo:                                    # 17.1 小屋里点“发张照片”：只发照片，不占每天的名额，发不了说清楚为什么
            if photo is None or not photo.gen.ready:
                self.share_error = "没配画图接口（大模型里的 routes.image），画不了照片"
                return False
            return await self._share_photo(gid, st, count=False)
        want_photo = force_photo if force_photo is not None else (
            photo is not None and photo.ready and random.random() < float(g.get("photo_share_chance", 0.3)))
        if want_photo and photo is not None and photo.ready and photo.remaining_today() > 0:
            return await self._share_photo(gid, st)
        if not getattr(self, "web", None) or not self.web.enabled:
            return False
        p = self.brain.persona
        kn = getattr(self, "knowledge", None)
        item = None
        if g.get("feed", True) and hasattr(self, "feed_pick"):
            try:
                item = self.feed_pick(gid)                       # 20.2 候选池里按规则打分最高的（没有过线的就不分享）
            except Exception as e:  # noqa: BLE001
                log.info("群 %s 挑候选出错：%s", gid, e)
            if item is None and self.feed_has_pool(gid):
                return False                                     # 有候选但都没过线：这次不分享（不再让模型硬挑一个）
        likes = ", ".join(e["topic"] for e in sorted(kn.entries(gid, 60), key=lambda e: -int(e["hits"] or 0))[:10]) if kn else ""
        mine = ", ".join(self.interests()[:10]) if hasattr(self, "interests") else ""
        recent = ", ".join(x.get("topic") or x.get("text", "")[:12] for x in self.shares(gid)[-6:]) or "（没有）"
        today = self.brain.now()
        tstr = f"{today:%Y-%m-%d}（{'一二三四五六日'[today.weekday()]}）"
        fresh = max(1, int(g.get("share_fresh_days", 3) or 3))
        d = {}
        try:
            if item is None:                                     # 候选池还空着（刚上线 / 搜索都失败）：退回老办法
                d = await self.brain.llm.json([{"role": "user", "content": SHARE_PICK % (
                    p.name, tstr, f"{mine}；{likes}"[:400] or "（不太清楚）",
                    (self.db.get(f"qq_group_notes_{gid}") or "（没有）")[:500], recent)}], default={}, role="helper")
        except Exception as e:  # noqa: BLE001
            log.info("挑分享话题失败：%s", e)
            return False
        q = str((d or {}).get("query") or "").strip()[:30] if d else ""
        if item is not None:
            q = item["topic"]
            res = [{"title": item["title"], "url": item["url"], "snippet": item["snippet"], "date": item["date"]}]
        else:
            if len(q) < 2:
                return False
            res = await self.web.search(q, fresh_days=fresh)
        if not res:
            return False
        known = "\n".join(f"- {e['topic']}：{e['summary']}" for e in (kn.entries(gid, 30) if kn else []))[:1200] or "（没有）"
        found = "\n".join(f"- {r['title']}：{r['snippet']}" + (f"（发布：{r['date']}）" if r.get("date") else "")
                          for r in res[:5])
        try:
            j = await self.brain.llm.json([{"role": "user", "content": SHARE_JUDGE % (p.name, tstr, known, q, found)}],
                                          default={}, role="helper")
        except Exception as e:  # noqa: BLE001
            log.info("判断要不要分享失败：%s", e)
            return False
        card = share_score(j)
        if card is not None and card < int(g.get("share_min_score", 13)):   # 24.0 评分卡（5 项 × 4 分，≥13 才分享），分数由代码加
            log.info("群 %s：刷到的「%s」评分 %d（要 %d），不分享", gid, str((j or {}).get("gist") or q)[:30], card,
                     int(g.get("share_min_score", 13)))
            j = {**j, "share": False}
        if isinstance(j, dict) and j.get("share") and not str(j.get("reason") or "").strip() and card is not None:
            j = {**j, "share": False}                                      # 说不出为什么值得分享：不分享
        if not isinstance(j, dict) or not j.get("share") or not j.get("gist"):
            self._note_share(gid, "skip", q, str((d or {}).get("topic") or ""))     # 算一次：今天别再刷了
            if item is not None:
                self.feed_mark(gid, item)
            return False
        age = share_age(j, res, today, fresh)
        if age is None:
            log.info("群 %s：刷到的「%s」看不出是哪天的 / 是 %s 天以前的旧闻，不分享", gid, j.get("gist"), fresh)
            self._note_share(gid, "skip", q, str((d or {}).get("topic") or ""))
            if item is not None:
                self.feed_mark(gid, item)
            return False
        topic = str(j.get("topic") or d.get("topic") or q)[:20]
        if kn is not None and j.get("summary"):
            kn.add(gid, topic, [topic, *re.split(r"\s+", q)[:3]], str(j["summary"]), "share", q)
        when = ("今天的事" if age == 0 else "昨天的事" if age == 1 else f"{age} 天前（{j.get('when')}）的事")
        src = str(j.get("source_title") or "").strip()[:80]
        why = (f"你刚在手机上刷到：{j['gist']}（{when}" + (f"；原标题「{src}」" if src else "") + "）。"
               "觉得群里的人会感兴趣，随手分享一句（像朋友在群里说“诶你们看到没……”），可以带一句自己的看法。"
               f"这是{when}：别说成“刚刚”“开打了”这种正在发生的口气，除非就是今天的事。"
               "只说原文写着的，主语、称号、术语照原文用，别换成意思不一样的说法（比如把队伍的成绩说成像某个选手的称号），拿不准的就别提；"
               "别说“我搜到”“根据”，别发链接，一两句话就够")
        async with st.lock:
            try:
                ok = await self.speak(gid, mentioned=False, owner_spoke=False, trigger=None, who="", why=why,
                                      web=self.web.block(q, res))
            except Exception as e:  # noqa: BLE001
                log.info("群 %s 分享失败：%s", gid, e)
                return False
        # 24.0 记兴趣表里的话题名（以前记的是模型起的名字，比如“羽毛球世界排名”，兴趣权重永远对不上“羽毛球”）
        self._note_share(gid, "news" if ok else "skip", str(j["gist"]), item["topic"] if item is not None else topic,
                         feed=item is not None)
        if item is not None:
            self.feed_mark(gid, item)
        return bool(ok)

    async def _share_photo(self, gid: int, st, count: bool = True) -> bool:
        p = self.brain.persona
        life = self.db.get(f"life_{self.brain.now():%Y-%m-%d}", "") or "（普通的一天）"
        if hasattr(self, "life_split"):                     # 31.0 只拍已经在做 / 做过的；打算和还没到点的不拍成已经在做
            done, plan = self.life_split()
            if done or plan:
                life = ("\n".join(done) or "（今天还没做什么特别的）") + (
                    "\n（下面这些只是打算 / 还没到点，别拍成已经在做：" + "；".join(plan) + "）" if plan else "")
        try:
            d = await self.brain.llm.json([{"role": "user", "content": PHOTO_PROMPT % (
                p.name, p.body[:900], str(life)[:400])}], default={}, role="helper")
        except Exception as e:  # noqa: BLE001
            log.info("想照片失败：%s", e)
            self.share_error = f"想拍什么的时候出错了：{str(e)[:80]}"
            return False
        scene, say = str((d or {}).get("scene") or "").strip(), str((d or {}).get("say") or "").strip()[:30]
        if len(scene) < 6 or looks_like_order(say):
            log.info("群 %s：这次没想好拍什么（%s / %s）", gid, scene, say)
            self.share_error = "这次没想好拍什么，再点一次试试"
            return False
        data = await self.photos.make(scene, count=count)
        if not data:
            self.share_error = ("今天的照片名额用完了" if count and self.photos.remaining_today() <= 0
                                else "今天的大模型花费到上限了" if not self.photos._budget_ok()
                                else "画图接口没画出来，看日志里的“画图失败”")
            return False
        from aigroup.onebot import image_seg, text_seg
        async with st.lock:
            try:
                mid = await self.ob.send_group(gid, [image_seg(data)])
                self._record(gid, self.ob.self_id or 0, p.name, f"[图片：{scene}]", mid, role="assistant")
                if say:
                    mid2 = await self.ob.send_group(gid, [text_seg(say)])
                    self._record(gid, self.ob.self_id or 0, p.name, say, mid2, role="assistant")
            except Exception as e:  # noqa: BLE001
                log.info("群 %s 发照片失败：%s", gid, e)
                self.share_error = f"画好了但没发出去：{str(e)[:80]}"
                return False
        self._note_share(gid, "photo", f"{say}（{scene}）", "照片")
        log.info("群 %s：发了一张照片（%s）", gid, scene)
        return True

    # ================= 群友在哪、那边天气（4.4） =================
    def maybe_where(self, gid: int, uid: int, text: str):
        """群友自己说“我在珠海出差 / 到成都了”：查一下是不是真的城市，是就记下。"""
        wh = getattr(self, "where", None)
        if wh is None or not self._gk(gid).get("weather", True):
            return
        from aigroup.net.where import said_where
        got = said_where(text)
        if got and self._allow(f"where_{uid}", 3):
            self._spawn(wh.set(uid, got[0], got[1], "", "said"))

    async def weather_tick(self, gid: int, st) -> bool:
        """群友在外地、那边今天 / 明天要变天：像朋友那样提醒一句（每个群每天最多一次）。"""
        wh = getattr(self, "where", None)
        g = self._gk(gid)
        if wh is None or not g.get("weather", True) or not g.get("weather_care", True):
            return False
        now = self.brain.now()
        day = f"{now:%Y-%m-%d}"
        last = float(self.db.get(f"qq_wxcheck_{gid}", 0) or 0)
        if time.time() - last < 30 * 60 or self.db.get(f"qq_wxcare_n_{gid}_{day}"):
            return False
        self.db.set(f"qq_wxcheck_{gid}", time.time())
        target = "today" if now.hour < 15 else "tomorrow"
        for p in wh.all():
            if p["kind"] != "trip" or p.get("lat") is None:
                continue
            r = self.db.conn.execute("SELECT name FROM qq_group_messages WHERE group_id=? AND user_id=? AND ts>? "
                                     "ORDER BY id DESC LIMIT 1", (gid, p["user_id"], time.time() - 3 * 86400)).fetchone()
            if not r:
                continue
            key = f"qq_wxcare_{p['user_id']}_{day}_{target}"
            if self.db.get(key):
                continue
            w = await wh.weather(p["lat"], p["lon"])
            a = wh.alert((w or {}).get(target) or {})
            if not a:
                continue
            self.db.set(key, True)
            self.db.set(f"qq_wxcare_n_{gid}_{day}", True)
            d = w[target]
            when = "今天" if target == "today" else "明天"
            why = (f"{r['name']}这几天在{p['city']}{p['why'] or ''}，那边{when}{a}（{d['text']} {d['min']}~{d['max']}°C）。"
                   f"像朋友那样提醒他一句（想 @ 他就写 [@{r['name']}]），一两句，别像天气预报")
            async with st.lock:
                try:
                    await self.speak(gid, mentioned=False, owner_spoke=False, trigger=None, who=r["name"], why=why)
                except Exception as e:  # noqa: BLE001
                    log.info("群 %s 天气提醒没说成：%s", gid, e)
            return True
        return False

    def where_list(self) -> list[dict]:
        wh = getattr(self, "where", None)
        if wh is None:
            return []
        names = {int(r["user_id"]): r["name"] for r in self.db.conn.execute("SELECT user_id, name FROM qq_people").fetchall()}
        out = []
        for p in wh.all():
            w = wh._wx.get(f"{p['lat']:.2f},{p['lon']:.2f}") if p.get("lat") is not None else None
            out.append({"uid": p["user_id"], "name": names.get(int(p["user_id"]), str(p["user_id"])), "city": p["city"],
                        "kind": p["kind"], "why": p["why"], "until": p["until"], "src": p["src"],
                        "weather": wh.brief(w[1]) if w and w[1] else ""})
        return out

    # ================= 生日和节日（4.3） =================
    def birthdays(self) -> dict:
        d = self.db.get("qq_birthdays") or {}
        return d if isinstance(d, dict) else {}

    def set_birthday(self, uid: int, day: str) -> bool:
        day = (day or "").strip().replace("/", "-").replace(".", "-").replace("月", "-").replace("日", "").replace("号", "")
        d = self.birthdays()
        if not day:
            d.pop(str(uid), None)
            self.db.set("qq_birthdays", d or None)
            return True
        m = re.match(r"^(\d{1,2})-(\d{1,2})$", day)
        if not m or not (1 <= int(m.group(1)) <= 12 and 1 <= int(m.group(2)) <= 31):
            return False
        d[str(int(uid))] = f"{int(m.group(1)):02d}-{int(m.group(2)):02d}"
        self.db.set("qq_birthdays", d)
        return True

    @staticmethod
    def festival(day) -> str:
        """群里值得应个景的节日（节气只算冬至）。"""
        from aigroup.core.schedule import festival_today
        names = [x for x in (festival_today(day) or "").split("、") if x]
        jieqi = {"立春", "雨水", "惊蛰", "春分", "清明", "谷雨", "立夏", "小满", "芒种", "夏至", "小暑", "大暑", "立秋", "处暑",
                 "白露", "秋分", "寒露", "霜降", "立冬", "小雪", "大雪", "小寒", "大寒"}
        return "、".join(x for x in names if x not in jieqi)

    def upcoming(self, days: int = 30) -> list[dict]:
        """接下来一个月的生日和节日（小屋里看）。"""
        names = {str(r["user_id"]): r["name"] for r in self.db.conn.execute("SELECT user_id, name FROM qq_people").fetchall()}
        today = self.brain.now().date()
        out = []
        for i in range(days):
            d = today + timedelta(days=i)
            for uid, md in self.birthdays().items():
                if md == f"{d:%m-%d}":
                    out.append({"day": f"{d:%m-%d}", "in": i, "kind": "birthday", "name": names.get(uid, uid), "uid": uid})
            f = self.festival(d)
            if f:
                out.append({"day": f"{d:%m-%d}", "in": i, "kind": "festival", "name": f})
        return out

    async def celebrate_tick(self, gid: int, st) -> bool:
        """每个群每天一次：今天有群友过生日 / 是节日，上午十点以后在群里说一声。说了返回 True。"""
        if not self._gk(gid).get("celebrate", True):
            return False
        now = self.brain.now()
        key = f"qq_celebrate_{gid}_{now:%Y-%m-%d}"
        if self.db.get(key) or not 10 <= now.hour < 22:
            return False
        self.db.set(key, True)
        md = f"{now:%m-%d}"
        since = time.time() - 90 * 86400
        who, who_ids = [], []
        for uid, d in self.birthdays().items():
            if d != md:
                continue
            r = self.db.conn.execute("SELECT name FROM qq_group_messages WHERE group_id=? AND user_id=? AND ts>? "
                                     "ORDER BY id DESC LIMIT 1", (gid, int(uid), since)).fetchone()
            if r:                                           # 只在他常待的群里祝（别的群的人不认识他）
                who.append(r["name"])
                who_ids.append(int(uid))
        fest = self.festival(now.date())
        if not who and not fest:
            return False
        bits = []
        if who:
            names = "、".join(who)
            bits.append(f"今天是{names}的生日，你记得。在群里祝{'他们' if len(who) > 1 else '他'}生日快乐（想 @ 就写 [@{who[0]}]），"
                        "像朋友那样，可以带一句只有你们懂的梗，别像群发的祝福短信")
        if fest:
            bits.append(f"今天是{fest}，应个景说一句（随口的祝福、或者约大家做点应景的事），别像群发的祝福短信")
        why = "；".join(bits) + "。一两句话，这次一定要说"
        async with st.lock:
            try:
                said = await self.speak(gid, mentioned=False, owner_spoke=False, trigger=None, who=who[0] if who else "",
                                        why=why, must=True)
            except Exception as e:  # noqa: BLE001
                log.info("群 %s 祝生日 / 过节没说成：%s", gid, e)
                return False
        log.info("群 %s：%s", gid, "祝了生日：" + "、".join(who) if who else "过节：" + fest)
        if said and who and self._gk(gid).get("birthday_card", True):
            await self.birthday_card(gid, st, who[0], who_ids[0])
        return True

    async def birthday_card(self, gid: int, st, name: str, uid: int = 0) -> bool:
        """13.0 生日卡：画一张和他的兴趣有关的图，中文祝福由程序叠在图上（画图模型不写字）；不占每天的照片名额。"""
        photo = getattr(self, "photos", None)
        if photo is None or not photo.gen.ready:
            return False
        uid = uid or next((int(k) for k, v in self.names(gid).items() if v == name), 0)
        prof = ((self.person(uid, gid) or {}).get("profile") or {}) if uid else {}
        likes = re.split(r"[，,、；;/\s]+", str(prof.get("兴趣") or ""))[:2]
        scene = ("一张温馨可爱的生日贺卡插画：生日蛋糕、蜡烛、彩带" + (f"，画面里有和「{'、'.join(x for x in likes if x)}」有关的小元素"
                                                              if any(likes) else "") + "，明亮的暖色调，留出下方空白")
        data = await photo.make(scene, with_self=photo.can_draw_self, text=f"{name}，生日快乐！", count=False)
        if not data:
            return False
        from aigroup.onebot import image_seg
        async with st.lock:
            try:
                mid = await self.ob.send_group(gid, [image_seg(data)])
                self._record(gid, self.ob.self_id or 0, self.brain.persona.name, f"[图片：给{name}画的生日卡]", mid,
                             role="assistant")
            except Exception as e:  # noqa: BLE001
                log.info("群 %s 生日卡没发出去：%s", gid, e)
                return False
        log.info("群 %s：给 %s 发了生日卡", gid, name)
        return True

    # ================= 小屋“她的日子”用 =================
    def life_snapshot(self, gid: int | None) -> dict:
        gids = [gid] if gid else sorted(self.groups)
        g0 = gids[0] if gids else None
        people = []
        for r in self.db.conn.execute("SELECT user_id, name, msgs FROM qq_people ORDER BY last_seen DESC LIMIT 60").fetchall():
            if self.ob is not None and getattr(self.ob, "self_id", None) and int(r["user_id"]) == int(self.ob.self_id):
                continue
            sc = self.closeness(int(r["user_id"]))
            people.append({"uid": r["user_id"], "name": r["name"], "score": round(sc, 1), "tier": tier(sc),
                           "owner": bool(self.owner_id and int(r["user_id"]) == self.owner_id)})
        people.sort(key=lambda x: -x["score"])
        kn = getattr(self, "knowledge", None)
        know = []
        for x in gids:
            know += [dict(e, gid=x) for e in (kn.entries(x, 30) if kn else [])]
        know.sort(key=lambda e: -e["ts"])
        plans_open = [p for x in gids for p in self.plans(x)]
        plans_done = sorted([p for x in gids for p in self.plans(x, None, 40) if p["status"] != "open"],
                            key=lambda p: -p["due"])[:20]                 # 19.0 历史：说了 / 错过 / 没说出来 / 取消都在
        photo = getattr(self, "photos", None)
        return {
            "reflect": self.db.get(f"qq_reflect_{g0}") or "" if g0 else "",
            "reflect_hist": (self.db.get(f"qq_reflect_hist_{g0}") or [])[-8:] if g0 else [],
            "selfcore": self.db.get(f"qq_selfcore_{g0}") or "" if g0 else "",
            "proposals": [x for x in (self.db.get("persona_proposals") or []) if x.get("status") == "pending"][-10:],
            "prefs": [{"topic": k, "v": v.get("v"), "ts": v.get("ts")} for k, v in
                      sorted(self.prefs().items(), key=lambda kv: -kv[1].get("ts", 0))],
            "people": people[:40],
            "close_events": list(reversed(self.db.get("qq_close_events") or []))[:20],
            "plans": plans_open, "plans_done": plans_done,
            "shares": list(reversed([s for s in self.shares() if s.get("kind") != "skip" and (not gid or s.get("gid") == gid)]))[:20],
            "knowledge": [{"id": e["id"], "gid": e["gid"], "topic": e["topic"], "summary": e["summary"],
                           "source": e["source"], "ts": e["ts"], "hits": e["hits"]} for e in know[:24]],
            "photo_ready": bool(photo is not None and photo.ready),
            "birthdays": [{"uid": p["uid"], "name": p["name"], "day": self.birthdays().get(str(p["uid"]), "")}
                          for p in people[:40]],
            "upcoming": self.upcoming(),
            "where": self.where_list(),
        }
