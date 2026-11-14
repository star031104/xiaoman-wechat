"""约定和跟进：记约定要有原话依据、打气只给考试比赛这类事、有结果就作废、翻旧账关话题（原 v362.py）。

36.2 按《复盘 11-10 突然翻旧账》和 11-09 日报 P1～P4 做的（都能在小屋里单独关）：

- A 翻旧账（约定 qq_plans）：
  · 整理时记约定，日期要在原话里找得到依据（今天 / 明天 / 周四 / 11.8 / 晚上八点……），而且算出来的那天要对得上；
    找不到就不记（11-03 聊推荐眼霜，没人说日期，整理编了个 11-10，到点“@小王 咖啡因那支，闭眼买”→“那不是很久以前的事情了吗”）
  · 同一批记的同一件事不按人各记一条（眼霜给小王、老板各记了一条）
  · 事前打气只给考试、面试、自己打的比赛这类事，记下超过 3 天的不打气，人不在不打气
  · 打气 / 事后问之前：这事记下以后群里已经有了结果（买了、考完、发了截图……）就作废；有人说过“很久以前 / 翻旧账”也作废
- B 刷屏：“等的人开口了”（含“多半在跟她说”）一轮只免一次冷却（11-09 13:32 39 秒 4 条、18:14 42 秒 3 条都是这条路）
- D 编自己的过去（“我上学那会儿”“我当年”）：规则直接拦（记在 check_life_done 名下，和“打算说成做过”同属编经历）
- E 看不清的图认出牌子、型号（“就是那个银绿色包装的YY比赛球吧”，图说明里写着看不清）：规则直接拦（记在 check_price_src 名下，同属“具体说法没出处”）
- C 吐槽时说风凉话：critic_enforce 加第 19 条（自检整体是影子时这条照样拦）
- F 一句一回：B 收紧以后，再加 I“有人吐槽只接一句”
- G “刷到的”：只分享群里最近真聊过的项目、当天的新闻
- I 局面判断看到有人吐槽：提示只接情绪、不劝不催；没人叫她时 3 分钟里只接一句
- K FAILURE_MODES 第 23 条“主动开口没由头”（自检影子记录）
- H 主动的事（提醒、天气、刷到的、打气）不搭在有人刚问她、她还没回的那一轮里说
- J 每天的安排只照最近 3 天定下 / 有结果的事写（眼霜被写进 11-09 的安排，她更理直气壮地去提）
"""
from __future__ import annotations

import logging
import re
import time
from datetime import date, datetime, timedelta

log = logging.getLogger("aigroup.followups")

# ---------------- A 约定的日期要有依据 ----------------
CN_NUM = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
          "十一": 11, "十二": 12}
WEEKDAY = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6, "末": 5}
TODAY_RX = re.compile(r"今(天|晚|早|儿|日|夜|天晚上|天下午|天中午)|待会|一会儿?|等下(?!班)|等会儿?|马上|稍后|晚点|这会儿|今晚|"
                      r"(?<![明后昨前])(早上|上午|中午|下午|傍晚|晚上|凌晨)")
TOMORROW_RX = re.compile(r"明(天|早|晚|儿|日|天早上|天晚上)")
AFTER2_RX = re.compile(r"(?<!大)后天")
AFTER3_RX = re.compile(r"大后天")
WEEK_RX = re.compile(r"(下下|下个?|这个?|本)?(周|星期|礼拜)([一二三四五六日天末])")
NEXT_WEEK_RX = re.compile(r"下(个)?(周|星期|礼拜)(?![一二三四五六日天末])")
MD_RX = re.compile(r"(\d{1,2}|[一二三四五六七八九十]{1,3})\s*月\s*(\d{1,2})\s*(日|号)?")
DOT_RX = re.compile(r"(?<![\d.])(\d{1,2})[./](\d{1,2})(?![\d.]|\s*折|\s*[kK倍万%])")
DAY_RX = re.compile(r"(?<![\d月./])(\d{1,2})\s*(号|日)(?![子本记期])")
DOUBLE_RX = re.compile(r"双\s*(11|十一|12|十二)")
CLOCK_RX = re.compile(r"(\d{1,2}|[一二三四五六七八九十两]{1,3})\s*点|\d{1,2}\s*[:：]\s*\d{2}")


def _cn(s: str) -> int:
    s = str(s or "")
    if s.isdigit():
        return int(s)
    if s in CN_NUM:
        return CN_NUM[s]
    if s.startswith("十") and s[1:] in CN_NUM:
        return 10 + CN_NUM[s[1:]]
    if len(s) >= 2 and s[0] in CN_NUM and s[1] == "十":
        return CN_NUM[s[0]] * 10 + (CN_NUM.get(s[2:], 0) if s[2:] else 0)
    return 0


def _md(base: date, m: int, d: int) -> date | None:
    """几月几号 → 离 base 最近的那一个（不往回找超过 60 天，那就是明年）。"""
    try:
        x = date(base.year, m, d)
    except ValueError:
        return None
    if (base - x).days > 60:
        try:
            x = date(base.year + 1, m, d)
        except ValueError:
            return None
    return x


def cue_dates(text: str, base: date) -> set[date]:
    """一句话里说到的“哪天”（按说这话的那天 base 算）。没说到哪天返回空集。
    只认说得出口的时间（今天 / 明天 / 后天 / 周四 / 下周四 / 11月8号 / 11.8 / 8号 / 双11 / 下周 / 晚上八点）；
    “过几天 / 改天 / 回头 / 以后 / 一周后效果”这种不算（它们定不了哪天）。"""
    t = re.sub(r"\[(图片|视频|表情包|链接|分享)[：:][^\]]*\]", " ", str(text or ""))     # 图说明里的“7 天改善”“1 周淡眼圈”不算
    out: set[date] = set()
    if AFTER3_RX.search(t):
        out.add(base + timedelta(days=3))
    if AFTER2_RX.search(t):
        out.add(base + timedelta(days=2))
    if TOMORROW_RX.search(t):
        out.add(base + timedelta(days=1))
    for m in WEEK_RX.finditer(t):
        pre, wd = m.group(1) or "", WEEKDAY[m.group(3)]
        monday = base - timedelta(days=base.weekday())
        if pre.startswith("下下"):
            d = monday + timedelta(days=14 + wd)
        elif pre.startswith("下"):
            d = monday + timedelta(days=7 + wd)
        else:
            d = monday + timedelta(days=wd)
            if not pre and d < base:                  # “周四”说在周五：一般指下周四
                d += timedelta(days=7)
        out.add(d)
        if m.group(3) == "末":
            out.add(d + timedelta(days=1))
    for m in NEXT_WEEK_RX.finditer(t):
        monday = base - timedelta(days=base.weekday()) + timedelta(days=7)
        out |= {monday + timedelta(days=i) for i in range(7)}
    for m in MD_RX.finditer(t):
        d = _md(base, _cn(m.group(1)), int(m.group(2)))
        if d:
            out.add(d)
    for m in DOT_RX.finditer(t):
        mo, dd = int(m.group(1)), int(m.group(2))
        if 1 <= mo <= 12 and 1 <= dd <= 31:
            d = _md(base, mo, dd)
            if d and abs((d - base).days) <= 45:          # “8.5 折”“3.5 分”这种离得远的不算日期
                out.add(d)
    for m in DAY_RX.finditer(t):
        dd = int(m.group(1))
        if 1 <= dd <= 31:
            d = _md(base, base.month, dd)
            if d and d < base:
                nm = base.month % 12 + 1
                d = _md(base, nm, dd) or d
            if d:
                out.add(d)
    for m in DOUBLE_RX.finditer(t):
        d = _md(base, 11 if m.group(1) in ("11", "十一") else 12, 11 if m.group(1) in ("11", "十一") else 12)
        if d:
            out.add(d)
    if not out and (TODAY_RX.search(t) or CLOCK_RX.search(t)):
        out |= {base, base + timedelta(days=1)}           # 只说了“待会 / 晚上八点”：今天（过了点就是明天）
    elif TODAY_RX.search(t) and re.search(r"今(天|晚|早|儿|日|夜)", t):
        out.add(base)
    return out


def _norm(s: str) -> str:
    return re.sub(r"[\s，,。.!！?？~～、：:“”\"'‘’「」\[\]（）()]+", "", str(s or ""))


def plan_evidence(what: str, due: datetime, quote: str, rows: list[dict], tz=None, terms_fn=None) -> str:
    """整理记下的约定，原话里有没有能证明“是那天”的时间。返回找到的依据（原话片段），没有返回空字符串。
    rows：这次整理看的那段聊天（ts / content / role）；quote：模型抄的原话（可以没有）。
    1) 有原话：原话要真在聊天里（群友说的），原话或那条消息里的时间按那条消息的日期算，要算出 due 那天；
    2) 没原话（老格式）：在说到这件事的群友消息里找能算出 due 那天的时间说法。"""
    want = due.date()
    users = [r for r in rows if str(r.get("role") or "user") != "assistant"]
    q = _norm(quote)
    if len(q) >= 4:
        for r in users:
            c = str(r.get("content") or "")
            if q in _norm(c):
                base = datetime.fromtimestamp(float(r["ts"]), tz).date()
                if want in cue_dates(quote, base) or want in cue_dates(c, base):
                    return quote[:40]
        return ""
    terms = terms_fn(what) if terms_fn else set()
    for r in users:
        c = str(r.get("content") or "")
        if terms and not any(x in c for x in terms):
            continue
        base = datetime.fromtimestamp(float(r["ts"]), tz).date()
        if want in cue_dates(c, base):
            return c[:40]
    return ""


# ---------------- A 打气 / 事后问之前 ----------------
# 值得事前打个气的事：自己要上场、要被考、要出发
CHEER_RX = re.compile(r"考试|考研|考公|笔试|面试|一面|二面|三面|终面|复试|答辩|比赛|决赛|半决赛|擂台|单打|双打|打球|上场|首发|"
                      r"体检|看病|手术|拔牙|出发|飞机|航班|高铁|报到|入职|上岸|演出|表演|汇报|述职|路演|驾照|科目[一二三四]|"
                      r"论文|交稿|截止|deadline|ddl|DDL|马拉松|跑步|考级|雅思|托福|四级|六级|期中|期末|开题|中期|presentation|pre\b")
# 记下以后群里已经有了结果（“买了”“考完了”“到了”“发了”……）
RESULT_RX = re.compile(r"买了|买到|下单|拍了|入手|到货|到了|收到|搞定|弄好|弄完|做完|干完|写完|交了|提交了|考完|面完|考过|过了|没过|"
                       r"上岸|结束了|结果出|出结果|出分|赢了|输了|拿到|拿了|完成了|去过了|回来了|发过了|用了|试了|"
                       r"效果|好用|不好用|还行|一般般")
# 有人说这是旧事、在翻旧账（“那不是很久以前的事情了吗”）
STALE_RX = re.compile(r"很久以前|好久以前|很久之前|好久之前|老早|早就|过去好久|过去很久|都过去了|翻旧账|旧账|上辈子|多久(以前|之前)的事|"
                      r"几百年前|陈年旧事")

# ---------------- D 编自己的过去 ----------------
FAKE_PAST_RX = re.compile(r"我(们)?(上学|读书|念书|上大学|读大学|上高中|读高中|上初中|上小学)(那会儿?|那时候?|的时候|那阵子?)|"
                          r"我(们)?(小时候|当年|年轻(的)?时候|那个年代|那一届|以前上学|以前读书)|"
                          r"我(们)?那(会儿?|时候?)(上学|读书|念书)|想当年我|我(的)?(高中|初中|小学|大学)(那会儿?|时候|同学|老师|班主任)")

# ---------------- E 看不清的图认出牌子 ----------------
UNCLEAR_RX = re.compile(r"看不清|缩略图|模糊|字迹模糊|看不出|没有清晰|没下完|只看了封面")
IMG_RX = re.compile(r"^\s*(\[回复[^\]]*\]\s*)?\[(图片|视频)[：:]")
LATIN_RX = re.compile(r"(?<![A-Za-z0-9])[A-Za-z][A-Za-z0-9\-]{1,15}(?![A-Za-z0-9])")
BRAND_CN_RX = re.compile(r"([一-鿿]{2,6})(牌|家的|款|系列|型号)")
BRAND_CLAIM = r"(就是|应该是|是不是|估计是|看着像|像是|一看就是|这是|那是|这款是|那款是|用的是|用的)"
LATIN_OK = {"ok", "hh", "hhh", "ai", "app", "vs", "pk", "mvp", "bug", "emo", "ip", "id", "qq", "wx", "pdf", "ppt", "url"}

# ---------------- G 刷到的：话题在群里怎么叫 ----------------
TOPIC_ALIASES = {
    "LOL": ("lol", "英雄联盟", "s赛", "s16", "lpl", "lck", "msi", "世界赛", "瑞士轮"),
    "英雄联盟": ("lol", "英雄联盟", "s赛", "s16", "lpl", "lck", "msi", "世界赛"),
    "KPL": ("kpl", "王者", "荣耀"),
    "王者": ("kpl", "王者", "荣耀"),
    "王者荣耀": ("kpl", "王者", "荣耀"),
    "无畏契约": ("瓦", "无畏", "vct", "valorant", "瓦罗兰特"),
    "瓦": ("瓦", "无畏", "vct", "valorant"),
    "羽毛球": ("羽毛球", "打球", "单打", "双打", "球拍", "国羽", "羽球"),
    "足球": ("足球", "球赛", "欧冠", "英超", "世界杯", "国足"),
    "奶茶": ("奶茶", "茶颜", "喜茶", "霸王茶姬", "蜜雪"),
    "AI": ("ai", "大模型", "gpt", "claude", "deepseek", "智能体"),
}


def topic_words(topic: str) -> tuple[str, ...]:
    t = str(topic or "").strip()
    for k, v in TOPIC_ALIASES.items():
        if t.lower() == k.lower() or t.lower().startswith(k.lower()):
            return tuple({t.lower(), *v})
    return (t.lower(),)


class FollowupsMixin:
    # ================= A 约定 =================
    def plan_supported(self, gid: int, what: str, due: datetime, quote: str = "", rows: list | None = None) -> str:
        """整理时记的约定：返回依据（原话），没有依据返回空字符串（不记）。"""
        if not self.gk(gid).get("plans_need_evidence", True):
            return "（不查依据）"
        if rows is None:
            try:
                rows = [dict(r) for r in self._rows(gid, 80)]
            except Exception:  # noqa: BLE001
                rows = []
        try:
            from aigroup.chat.checks import content_terms
            names = self._name_terms(gid) if hasattr(self, "_name_terms") else set()
            terms_fn = (lambda w: content_terms(w) - names)
        except Exception:  # noqa: BLE001
            terms_fn = None
        return plan_evidence(what, due, quote, rows, tz=getattr(self.cfg, "tz", None), terms_fn=terms_fn)

    def plan_resolved(self, gid: int, p: dict, now: float | None = None) -> str:
        """记下这件事以后，群里已经有了结果 / 有人说这是旧事 / 有人说别提了：返回原因（作废），没有返回空字符串。"""
        if not self.gk(gid).get("plans_check_result", True):
            return ""
        now = now or time.time()
        from aigroup.chat.checks import TAG, content_terms
        names = self._name_terms(gid) if hasattr(self, "_name_terms") else set()
        terms = content_terms(str(p.get("what") or "")) - names
        if not terms:
            return ""
        created = float(p.get("created") or 0)
        rows = self.db.conn.execute("SELECT ts, user_id, role, content FROM qq_group_messages WHERE group_id=? AND ts>? AND ts<=? "
                                    "ORDER BY id LIMIT 600", (gid, created + 60, now)).fetchall()
        uid = int(p.get("user_id") or 0)
        # 结果只看临近那天的（due 前 1 天起）：一个月后的考试，今天说“我已经复习一半了”不算考完
        res_from = max(created + 60, float(p.get("due") or 0) - 86400)
        prev_her = ""
        for r in rows:
            c = TAG.sub("", str(r["content"] or ""))
            if r["role"] == "assistant":
                prev_her = c
                continue
            if STALE_RX.search(c) and (terms & content_terms(prev_her + c)):
                return f"有人说这是旧事（「{c[:16]}」）"
            if not (terms & content_terms(str(r["content"] or ""))):
                continue
            if float(r["ts"]) >= res_from and (not uid or int(r["user_id"] or 0) == uid) and RESULT_RX.search(c):
                return f"记下以后已经有了结果（「{c[:16]}」）"
        if hasattr(self, "_closed_hit"):
            w = self._closed_hit(gid, str(p.get("what") or ""), now)
            if w:
                return w[:40]
        return ""

    def plan_cheer_ok(self, gid: int, p: dict, now: float | None = None) -> str:
        """事前打气能不能说：返回不说的原因，空字符串 = 可以说。"""
        g = self.gk(gid)
        now = now or time.time()
        what = str(p.get("what") or "")
        if g.get("plans_cheer_only_events", True) and not CHEER_RX.search(what):
            return f"「{what[:12]}」不是考试 / 面试 / 比赛这类事，不打气"
        days = float(g.get("plans_cheer_max_days", 3) or 0)
        if days and now - float(p.get("created") or now) > days * 86400:
            return f"「{what[:12]}」是 {int((now - float(p.get('created') or now)) / 86400)} 天前记的，不打气"
        return ""

    def person_present(self, gid: int, uid: int, now: float | None = None, minutes: float = 30) -> bool:
        now = now or time.time()
        r = self.db.conn.execute("SELECT MAX(ts) t FROM qq_group_messages WHERE group_id=? AND user_id=?", (gid, int(uid))).fetchone()
        return bool(r and r["t"] and now - float(r["t"]) <= minutes * 60)

    # ================= H 有人刚问她、她还没回 =================
    def pending_to_her(self, gid: int, now: float | None = None, minutes: float = 10) -> str:
        """最近 minutes 分钟里有冲她来的话（@ 她 / 回复她），在她最后一句之后、还没回：返回那句（主动的事先别说）。"""
        if not self.gk(gid).get("proactive_wait_reply", True):
            return ""
        now = now or time.time()
        her = self.db.conn.execute("SELECT MAX(ts) t FROM qq_group_messages WHERE group_id=? AND role='assistant'", (gid,)).fetchone()
        since = max(now - minutes * 60, float((her["t"] if her else 0) or 0))
        r = self.db.conn.execute("SELECT content FROM qq_group_messages WHERE group_id=? AND role='user' AND to_her=1 AND ts>? "
                                 "ORDER BY id DESC LIMIT 1", (gid, since)).fetchone()
        return str(r["content"] or "")[:30] if r else ""

    # ================= D 编自己的过去 =================
    def _fake_past(self, gid: int, text: str) -> str:
        m = FAKE_PAST_RX.search(re.sub(r"[“\"「][^”\"」]*[”\"」]", "", str(text or "")))       # 引号里转述别人的不算
        if not m:
            return ""
        return (f"「{m.group(0)}」是在编你自己的过去：你没上过学、没有小时候和当年，别拿自己的经历比；"
                "要接就接他的事（“九天也太爽了”），或者不说")

    # ================= E 看不清的图认出牌子、型号 =================
    def _brand_unseen(self, gid: int, text: str, now: float | None = None) -> str:
        now = now or time.time()
        rows = self.db.conn.execute("SELECT ts, role, content FROM qq_group_messages WHERE group_id=? AND ts>? ORDER BY id",
                                    (gid, now - 30 * 60)).fetchall()
        imgs = [str(r["content"] or "") for r in rows if r["role"] == "user" and IMG_RX.search(str(r["content"] or ""))]
        if not imgs or not UNCLEAR_RX.search(imgs[-1]):
            return ""
        ctx = " ".join(str(r["content"] or "") for r in rows).lower()
        persona = ""
        try:
            persona = (self.persona_sources() if hasattr(self, "persona_sources") else "").lower()
        except Exception:  # noqa: BLE001
            persona = ""
        new = [w for w in LATIN_RX.findall(text) if w.lower() not in LATIN_OK and w.lower() not in ctx and w.lower() not in persona]
        new += [m.group(0) for m in BRAND_CN_RX.finditer(text) if m.group(1) not in ctx]
        # 要是“认出来”的说法：“就是 / 应该是 / 看着像……X”（同一小句里，X 前 12 个字以内）；“CS 我平时看得少”这种不算
        new = [w for w in new if re.search(BRAND_CLAIM + r"[^，。！？,.!?\s]{0,12}" + re.escape(w), text)]
        if not new:
            return ""
        return (f"图说明里写着看不清，聊天里也没人提过「{new[0]}」：别认图里东西的牌子、型号、名字；"
                "看不清就直说看不清，或者问他一句是啥")

    # ================= G 刷到的：群里最近真聊过这个项目 =================
    def topic_talked(self, gid: int, topic: str, days: float = 14, now: float | None = None) -> int:
        now = now or time.time()
        ck = self.__dict__.setdefault("_topic_talk_cache", {})
        key = (gid, topic)
        hit = ck.get(key)
        if hit and now - hit[0] < 3600:
            return hit[1]
        words = topic_words(topic)
        n = 0
        for r in self.db.conn.execute("SELECT content FROM qq_group_messages WHERE group_id=? AND role='user' AND ts>?",
                                      (gid, now - days * 86400)).fetchall():
            c = str(r["content"] or "").lower()
            if any(w and w in c for w in words):
                n += 1
        ck[key] = (now, n)
        return n
