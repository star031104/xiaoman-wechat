"""35.2 按 11-03 日报 P1～P8 补的几样（都能在小屋里单独关）：

- P1 安静期一律不主动：hush 不只拦接话，提醒、跟进、想念、分享、等等再说也拦，发出前再查一次（小屋手动点的除外）；
  发前检查判过“不说”的那个意思，15 分钟内换个说法、加个称呼也不说（11-03 10:44 被拦 6 秒后在前面加了个称呼照发）
- P2 群里当着别人别叫昵称（偏心的称呼留给想念 @、半夜被叫醒）；表情包也算一条
- P3 写稿那几十秒里她在回的人又说了话：发出前重新看；他已经说了还问“说来听听”（check_ask_told）
- P4 @ 别人 / 回复别人的这一批不接；紧跟别人 @ 的短回应（“好”）算回那个人
- P5 报的价格要有出处（check_price_src）
- P7 只有占位、不带内容的话（check_filler）；查资料先回的那句不再说“手机”
- P8 主动开口额度按时段摊（4 小时最多三分之一），用完了门控日志里写明“额度用完”
"""
from __future__ import annotations

import logging
import math
import re
import time

log = logging.getLogger("aigroup.v352")

# P7 只有占位、不带内容（“我找找看，等我一下”“我查查手机哈”“稍等”）
FILLER_RX = re.compile(r"^\s*(好+[，,]?|行+[，,]?|嗯+[，,]?)?(我|让我)?(先|再|去)?(找找|查查|翻翻|搜搜|看看|瞅瞅|找一?下|查一?下|翻一?下|搜一?下|看一?下|问问)"
                       r"(看|手机|哈|啊|呀|哦)?[，,。~～!！]*(等我(一下|一会儿?|下|会儿)?|稍等(一下)?|马上(回来)?)?[哈啊呀哦嘛~～。!！]*\s*$|"
                       r"^\s*(等我(一下|一会儿?|下|会儿)|稍等(一下)?|马上(回来)?)[哈啊呀哦嘛~～。!！，,]*\s*$")
# P3 让对方“说说”：他已经说了还这么问，就是没看到刚进来的话
ASK_TOLD_RX = re.compile(r"说来听听|说说看|展开(说说|讲讲)|细说|快说|别吊(我)?胃口|然后呢|后来呢|咋了|怎么了|啥情况|什么情况|讲讲")
# P2 问的是群里的人自己（“你们……一般干啥”“大家平时……”）
ASK_HUMANS_RX = re.compile(r"(你们|大家|各位|有没有人).{0,16}(一般|平时|都|通常|会).{0,10}(干啥|干嘛|做什么|干什么|在干|怎么(过|打发)|玩什么|看什么)")
SELF_REPORT_RX = re.compile(r"^\s*(我|俺|本人)(就|一般|平时|都|也|嘛|呢|啊)?")
# P5 价格
CN = "零一二两三四五六七八九十百千万几"
PRICE_RX = re.compile(rf"([0-9.]+|[{CN}]{{1,6}})\s*(块钱?|元|rmb|RMB)|[￥¥]\s*[0-9.]+|"
                      rf"([0-9.]+|[{CN}]{{1,6}})\s*(上下|左右|出头)|([{CN}]{{0,4}}[百千])(?=$|[，,。！!？?~～\s吧啊呀哈的])")
PRICE_CTX = re.compile(r"价|块|元|钱|￥|¥|入手|拿下|便宜|贵|买|下单|双1[12]|旗舰店|链接|页面|优惠|券|性价比|平价|牌子")
SRC_PRICE = re.compile(r"[0-9]+(\.[0-9]+)?\s*(元|块|rmb|RMB)|[￥¥]\s*[0-9]+|价格?[:：]?\s*[0-9]+")
_D = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def cn_values(s: str) -> set[int]:
    """“八十”→{80}、“六七十”→{60, 70}、“两三百”→{200, 300}、“120”→{120}；认不出返回空集。"""
    s = (s or "").strip()
    if re.fullmatch(r"[0-9.]+", s):
        try:
            return {int(float(s))}
        except ValueError:
            return set()
    unit = 1
    for u, v in (("万", 10000), ("千", 1000), ("百", 100), ("十", 10)):
        if s.endswith(u):
            unit, s = v, s[:-1]
            break
    if not s:
        return {unit}
    if all(c in _D for c in s) and len(s) <= 2:
        return {_D[c] * unit for c in s}
    if "十" in s:                                        # 二十五、十五
        a, _, b = s.partition("十")
        if all(c in _D for c in a + b) and len(b) <= 1:
            return {((_D[a] if a else 1) * 10 + (_D[b] if b else 0)) * unit}
    if "百" in s:
        a, _, b = s.partition("百")
        if len(a) == 1 and a in _D:
            rest = cn_values(b) if b else {0}
            return {(_D[a] * 100 + r) * unit for r in rest} if rest else set()
    return set()


def price_mentions(text: str, ctx: str = "") -> list[str]:
    """她话里的价格说法（“六七十块”“三十毫升八十上下”“两三百”“¥99”）。没带“块 / 元”的那种要在她这句或最近聊天里像在聊买东西。"""
    out = []
    shop = bool(PRICE_CTX.search(text or "") or PRICE_CTX.search(ctx or ""))
    for m in PRICE_RX.finditer(text or ""):
        if (m.group(3) or m.group(5)) and not shop:
            continue
        out.append(m.group(0).strip())
    return out


def unsourced_prices(text: str, src: str, ctx: str = "") -> list[str]:
    """价格在资料 / 聊天 / 图上的字里都对不上。资料里一个价格都没有 → 她报的全算没出处。"""
    pm = price_mentions(text, ctx)
    if not pm:
        return []
    have = {int(float(x)) for x in re.findall(r"[0-9]+(?:\.[0-9]+)?", " ".join(m.group(0) for m in SRC_PRICE.finditer(src or "")))}
    bad = []
    for p in pm:
        num = re.sub(r"\s*(块钱?|元|rmb|RMB|上下|左右|出头|[￥¥])\s*", "", p)
        vals = cn_values(num)
        if not have or not vals:
            bad.append(p)
            continue
        if not any(any(abs(v - h) <= max(2, 0.08 * h) for h in have) for v in vals):
            bad.append(p)
    return bad


def is_filler(bubble: str) -> bool:
    t = re.sub(r"\[[^\]]*\]", "", str(bubble or "")).strip()
    return bool(t) and bool(FILLER_RX.match(t))


class V352Mixin:
    # ---------------- P1 安静期 / 拦过的意思 ----------------
    def note_blocked(self, gid: int, text: str, now: float | None = None):
        """发前检查判了“不说”：记下这个意思（按意思记，不按原句）。"""
        now = now or time.time()
        st = self._st(gid)
        keep = float(self.gk(gid).get("blocked_topic_minutes", 15) or 0) * 60
        if keep <= 0 or not str(text or "").strip():
            return
        lst = [x for x in st.__dict__.get("blocked_topics", []) if now - x[0] < keep]
        st.__dict__["blocked_topics"] = (lst + [(now, str(text)[:120])])[-12:]

    def blocked_again(self, gid: int, bubbles: list[str], now: float | None = None) -> str:
        now = now or time.time()
        keep = float(self.gk(gid).get("blocked_topic_minutes", 15) or 0) * 60
        if keep <= 0:
            return ""
        from aigroup.chat.plan import bigram_overlap
        said = " ".join(str(b) for b in bubbles or [])
        for ts, old in self._st(gid).__dict__.get("blocked_topics", []):
            if now - ts < keep and bigram_overlap(said, old) >= 0.5:
                return old
        return ""

    def preflight(self, gid: int, bubbles: list[str], *, mentioned: bool, manual: bool, now: float | None = None) -> str:
        """发出前最后一道：安静期没叫到她的一律不说；刚判过“不说”的意思换个说法也不说。返回不说的原因（"" = 照发）。"""
        if mentioned or manual or not bubbles:
            return ""
        now = now or time.time()
        st = self._st(gid)
        if now - float(st.__dict__.get("admit_ts") or 0) < 120:
            return ""                                    # 被纠正后认错那一句不拦
        if self.gk(gid).get("hush_all_paths", True) and hasattr(self, "hushed"):
            h = self.hushed(gid, now)
            if h:
                return f"安静中（{h}）"
        old = self.blocked_again(gid, bubbles, now)
        if old:
            return f"刚判过不说：「{old[:24]}」"
        return ""

    # ---------------- P2 当众别叫昵称 ----------------
    def public_names(self, gid: int, bubbles: list[str], *, must: bool, now: float | None = None) -> list[str]:
        g = self.gk(gid)
        if g.get("fav_name_public", False) or must or not bubbles or not hasattr(self, "fav_name"):
            return bubbles
        pet = self.fav_name(gid)
        if not pet or not any(pet in b for b in bubbles):
            return bubbles
        now = now or time.time()
        fav = self.fav_uid(gid) if hasattr(self, "fav_uid") else 0
        others = self.db.conn.execute("SELECT COUNT(*) n FROM qq_group_messages WHERE group_id=? AND role='user' AND user_id!=? "
                                      "AND ts>?", (gid, fav, now - 600)).fetchone()["n"]
        if not others:                                   # 只有他俩在聊：照叫
            return bubbles
        call = self.call_name(gid, fav) if (fav and hasattr(self, "call_name")) else ""
        out = []
        for b in bubbles:
            b2 = b.replace(pet + "，", call + "，" if call else "").replace(pet, call)
            b2 = re.sub(r"^[，,\s]+", "", b2)
            if b2.strip():
                out.append(b2)
        log.info("群 %s：群里还有别人，「%s」改叫「%s」", gid, pet, call or "（不叫）")
        return out or bubbles

    # ---------------- P3 写稿时又来了话 ----------------
    def late_news(self, gid: int, last_id: int, people: list) -> list[dict]:
        """她开始写以后，她在回的那几个人又说的话。"""
        ids = [int(u) for u in people or [] if isinstance(u, int) and u > 0]
        if not ids or not self.gk(gid).get("late_news_check", True):
            return []
        q = ",".join("?" * len(ids))
        return [dict(r) for r in self.db.conn.execute(
            f"SELECT id, user_id, content, ts FROM qq_group_messages WHERE group_id=? AND role='user' AND id>? AND user_id IN ({q})",
            (gid, int(last_id), *ids)).fetchall()]

    # ---------------- P4 在跟别人说 ----------------
    def note_at(self, gid: int, uid: int, targets: list, now: float | None = None):
        if not targets:
            return
        st = self._st(gid)
        d = {k: v for k, v in st.__dict__.get("last_at", {}).items() if (now or time.time()) - v[1] < 300}
        for t in targets:
            try:
                d[int(t)] = (int(uid), now or time.time())
            except (TypeError, ValueError):
                continue
        st.__dict__["last_at"] = d

    def answers_at(self, gid: int, uid: int, text: str, now: float | None = None) -> bool:
        """他 2 分钟内刚被别人 @，这句又是“好 / 行 / 收到”这种短回应：是在回那个人。"""
        from aigroup.chat.plan import BARE_ANSWER
        if not self.gk(gid).get("at_other_skip", True):
            return False
        hit = self._st(gid).__dict__.get("last_at", {}).get(int(uid))
        if not hit or (now or time.time()) - hit[1] > 120:
            return False
        t = re.sub(r"\[[^\]]*\]", "", str(text or "")).strip()
        return bool(t) and (bool(BARE_ANSWER.match(t)) or len(t) <= 3)

    def to_others(self, gid: int, texts: list) -> bool:
        """这一批每条都是 @ 别人 / 回复别人的。"""
        if not texts or not self.gk(gid).get("at_other_skip", True):
            return False
        metas = [t[3] if len(t) > 3 and isinstance(t[3], dict) else {} for t in texts]
        return all(m.get("at_other") or m.get("reply_other") for m in metas)

    # ---------------- P8 主动额度按时段摊 ----------------
    def budget_left(self, gid: int, now: float | None = None) -> str:
        """主动开口额度用完了返回原因（写进门控日志），没用完返回 ""。"""
        g = self.gk(gid)
        cap = int(g.get("unprompted_per_day", 0) or 0)
        if not cap:
            return ""
        used = self.unprompted_today(gid) if hasattr(self, "unprompted_today") else 0
        if used >= cap:
            return f"当天主动额度用完（{used}/{cap}）"
        hrs = float(g.get("unprompted_window_hours", 4) or 0)
        share = float(g.get("unprompted_window_share", 0.34) or 0)
        if hrs > 0 and 0 < share < 1:
            now = now or time.time()
            lim = max(1, math.ceil(cap * share))
            n = int(self.db.conn.execute("SELECT COUNT(*) n FROM qq_reply_effect WHERE group_id=? AND mentioned=0 AND ts>=?",
                                         (gid, now - hrs * 3600)).fetchone()["n"])
            if n >= lim:
                return f"这 {hrs:g} 小时主动额度用完（{n}/{lim}）"
        return ""

    # ---------------- 发前检查（规则） ----------------
    def _price_src(self, gid: int, text: str, web: str) -> str:
        src = (web or "") + "\n" + " ".join(str(r["content"] or "") for r in self._rows(gid, 30))
        ctx = " ".join(str(r["content"] or "") for r in self._rows(gid, 6) if r["role"] == "user")
        bad = unsourced_prices(text, src, ctx)
        if not bad:
            return ""
        return (f"价格「{'、'.join(bad[:3])}」查到的资料、图上的字、聊天里都没有：别报具体价格，"
                "说“价格以页面为准 / 你点进去看下现价”，或者只说贵不贵")

    def _ask_told(self, gid: int, text: str, target_uid, mentioned: bool, now: float) -> str:
        from aigroup.chat.checks import DROP
        t = re.sub(r"\[[^\]]*\]", "", text or "")
        if not ASK_TOLD_RX.search(t) or len(t) > 24:
            return ""
        rows = [r for r in self._rows(gid, 8)]
        her = max([float(r["ts"]) for r in rows if r["role"] == "assistant"] or [0.0])
        uid = target_uid
        if uid is None:
            us = [r for r in rows if r["role"] == "user"]
            uid = int(us[-1]["user_id"]) if us else None
        if uid is None:
            return ""
        mine = [r for r in rows if r["role"] == "user" and int(r["user_id"] or 0) == int(uid)
                and float(r["ts"]) > her and now - float(r["ts"]) < 300]
        if len(mine) < 2:
            return ""
        body = [re.sub(r"\[[^\]]*\]", "", str(r["content"] or "")).strip() for r in mine[1:]]
        if sum(len(b) for b in body) < 6:
            return ""
        told = "」「".join(b[:16] for b in body[-2:])
        return (("" if mentioned else DROP) + f"他已经接着说了「{told}」：别再让他“说说 / 然后呢”，直接接他说的")

    def _filler(self, gid: int, bubbles: list, mentioned: bool) -> str:
        from aigroup.chat.checks import DROP
        parts = [str(p) for p in bubbles or [] if str(p).strip() and not str(p).startswith("[表情包")]
        if not parts or not all(is_filler(p) for p in parts):
            return ""
        if mentioned:
            return "只说了“等我一下 / 我找找看”，没内容：直接回答他问的，知道多少说多少，不知道就直说"
        return f"{DROP}只有“等我一下 / 我找找看”这种占位话，没内容：没人叫你就不说"

    def _ask_humans(self, gid: int, text: str, target_row: dict | None, mentioned: bool) -> str:
        from aigroup.chat.checks import DROP
        if mentioned:
            return ""
        q = str((target_row or {}).get("content") or "")
        if not q:
            us = [r for r in self._rows(gid, 4) if r["role"] == "user"]
            q = str(us[-1]["content"] or "") if us else ""
        if "你" in re.sub(r"你们", "", q) or not ASK_HUMANS_RX.search(q):
            return ""
        if not SELF_REPORT_RX.match(re.sub(r"\[[^\]]*\]", "", text or "")):
            return ""
        return f"{DROP}他问的是群里的人自己平时怎么样（“{q[:20]}”），没叫你：别拿自己当答案抢着答"
