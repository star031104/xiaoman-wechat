"""36.0 按 11-04～11-08 日报和《11-04～11-06 日报复盘与下一步》做的（都能在小屋里单独关）：

- A1 局面判断（read_room）：每批消息先用便宜模型看一眼“谁在跟谁说、有没有人在吐槽 / 纠正她 / 嫌她 / 追问她”，
  代码按结果硬管：嫌她 → 安静期；两个群友一问一答、没提她 → 不插；有人吐槽 → 提示先站他那边；有她没答的追问 → 先答。
  只会让她少说的直接生效（social_read=on）；会让她多说的（被纠正、被追问时放行）先只记录（social_read_speak=shadow）。
  以前这些靠 HUH / QUIET 正则一条条加词，11-04～11-06 九次确认不满里四次正则认不出。
- 被叫闭嘴 / 被嫌的那一句本身 @ 了她：只许不说（问了正经问题才短短认一句），不回嘴（11-07“你闭嘴”→“哟，这就受不了了”、
  11-08“催命鬼又来了”→“催命鬼也得认”）
- A4 冷场：没人问她时，群友发的图 / 位置 / 视频 / 链接一小时最多主动点评一次、看不清的不点评（media_first_comment）；
  她主动说的连着 2 次没人接，门槛 +0.15，有人接住就清零（cold_streak）
- B5 每周“别再说”：她一周里开头最常用的几个字（“哟”）、高频词，自动列进提示词
- B7 合规：群友说“小满别理我 / 别 @ 我”→ 不主动接他、不 @ 他（说“小满理我”恢复）；同一个人和她连着聊满 2 小时提醒一句；
  群里有未成年人（has_minor）时关掉偏心
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections import Counter

log = logging.getLogger("aigroup.v36")

MEDIA_ONLY = re.compile(r"^\s*(\[(图片|位置|视频|链接|分享|文件|小程序|卡片|聊天记录)[^\]]*\]\s*)+$")
MEDIA_UNCLEAR = re.compile(r"看不清|缩略图|模糊|没下完|只看了封面")
OPT_OUT = re.compile(r"(小满|你)\s*(别|不要|少)\s*(理|@|艾特|搭理|回)我|别\s*@\s*我了?")
OPT_IN = re.compile(r"(小满|你)\s*(可以|能)?\s*(理|@|搭理)我了?$|小满理我")
# 冲着她、不看她刚说没说都算嫌她的（“催命鬼又来了”离她上一句 23 分钟）
HUH_STRONG = re.compile(r"催命鬼|太皮了|管管你|控制你|学了(些|点)啥|高情商|别太荒谬|你说我知不知道|这ai|这个ai")
STOPWORDS = set("的了是我你他她吗呢吧啊哈嘛呀哦嗯就也都还又在有和这那个一不没很太好")

ROOM_PROMPT = """你在旁边看一个微信朋友群（熟人小群）。群里有个 AI 成员叫“{name}”（下面记作“你”）。只根据聊天记录判断，别猜。
看最后这一小段（最后几条最新），回答：
1. to_whom：最后这几条是在跟谁说？“她”（{name}）/ 某个群友的称呼 / “大家”
2. pair：最近 3 分钟是不是两个群友在一问一答、一来一回，而且没提{name}？是的话写这两个人的称呼（如 "小王,老张"），不是写 ""
3. venting：有没有人在吐槽、抱怨、发牢骚（作业、通勤、室友、打呼噜……）？有写那个人，没有写 ""
4. about_her：有没有人在说{name}本人？hush（叫她闭嘴 / 别说了 / 嫌她话多、嫌她吵、说她“太皮”“催命鬼”，哪怕拆成几条短消息发）/
   annoyed（嫌她答非所问、看不懂她在说啥、“？？”、“这 ai 学了些啥”）/ correct（指出她说错了）/ praise（夸她）/ none
5. open_ask：有没有群友接着{name}的话问了她问题、她还没答？有写那句原话，没有写 ""
注意：群友之间互损、开玩笑不算嫌她；只有冲着{name}的才算。about_her、open_ask、venting 只看最近 3 分钟里的消息（更早的已经过去了）。

# 聊天记录（现在 {now}）
{ctx}

只输出 JSON：{{"to_whom": "", "pair": "", "venting": "", "about_her": "none", "open_ask": "", "why": "一句话"}}"""


def is_media_only(text: str) -> bool:
    return bool(MEDIA_ONLY.match(str(text or "")))


def joined_short(rows: list, uid: int, now: float, window: float = 30.0, max_len: int = 4) -> str:
    """同一个人 30 秒内连发的短消息（每条 ≤4 个字）拼起来（11-06“你”“小嘴女”“小嘴巴”“闭起来”拆成 4 条发）。"""
    out = []
    for r in reversed(rows):
        if r["role"] != "user" or int(r["user_id"] or 0) != int(uid) or now - float(r["ts"]) > window:
            break
        t = re.sub(r"\[[^\]]*\]", "", str(r["content"] or "")).strip()
        if len(t) > max_len:
            break
        out.append(t)
    return "".join(reversed(out))


def lead_words(lines: list[str], k: int = 3, min_n: int = 3) -> list[str]:
    """一周里她开头最常用的字（“哟”“行”）和高频二字词，次数够多的。"""
    heads = Counter()
    grams = Counter()
    for s in lines:
        t = re.sub(r"\[[^\]]*\]|@\S+", "", str(s or "")).strip()
        if not t:
            continue
        h = re.match(r"^[一-鿿]{1,2}[，,！!。~～]?", t)
        if h and len(h.group(0).rstrip("，,！!。~～")) == 1:
            heads[h.group(0)[0]] += 1
        for i in range(len(t) - 1):
            g = t[i:i + 2]
            if re.fullmatch(r"[一-鿿]{2}", g) and not (set(g) & STOPWORDS):
                grams[g] += 1
    out = [f"开头的「{h}」" for h, n in heads.most_common(k) if n >= min_n]
    out += [f"「{g}」" for g, n in grams.most_common(k) if n >= min_n * 2]
    return out


class V36Mixin:
    # ================= A1 局面判断 =================
    def _room_ctx(self, gid: int, n: int = 15) -> str:
        rows = self._rows(gid, n)
        names = self.names(gid)
        lab = self._speaker_labels(gid, rows) if hasattr(self, "_speaker_labels") else {}
        out = []
        for r in rows:
            who = "你" if r["role"] == "assistant" else (lab.get(int(r["user_id"] or 0)) or names.get(str(r["user_id"]), r["name"]))
            out.append(f"{time.strftime('%H:%M:%S', time.localtime(float(r['ts'])))} {who}：{str(r['content'] or '')[:100]}")
        return "\n".join(out)

    def _my_name(self) -> str:
        try:
            return str(self.v2_persona().name or "她") if hasattr(self, "v2_persona") else "她"
        except Exception:  # noqa: BLE001
            return "她"

    async def read_room(self, gid: int, now: float | None = None) -> dict:
        """一批消息一次：同一条最新消息只问一次（缓存在状态里）。拿不到就返回 {}，什么都不改。"""
        g = self.gk(gid)
        if str(g.get("social_read", "on")) == "off" or not getattr(self, "brain", None):
            return {}
        now = now or time.time()
        st = self._st(gid)
        last = self.db.conn.execute("SELECT MAX(id) m FROM qq_group_messages WHERE group_id=?", (gid,)).fetchone()["m"] or 0
        c = st.__dict__.get("room") or {}
        if c.get("last") == last:
            return c.get("d") or {}
        if not self._allow(f"room_{gid}", int(g.get("social_read_per_hour", 60) or 0)):
            return {}
        try:
            out = await asyncio.wait_for(self.brain.llm.chat([{"role": "user", "content": ROOM_PROMPT.format(ctx=self._room_ctx(gid), now=time.strftime("%H:%M:%S", time.localtime(now)),
                                                name=self._my_name())}],
                                                             role="gate", temperature=0.0, max_tokens=220), 8)
            d = json.loads(re.search(r"\{.*\}", out or "", re.S).group(0))
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            return {}
        if not isinstance(d, dict):
            return {}
        d = {k: str(d.get(k) or "").strip()[:60] for k in ("to_whom", "pair", "venting", "about_her", "open_ask", "why")}
        st.__dict__["room"] = {"last": last, "d": d, "ts": now}
        self.check_log(gid, "social_read", str(g.get("social_read", "on")), "", json.dumps(d, ensure_ascii=False))
        return d

    def room_verdict(self, gid: int, d: dict, *, mentioned: bool, guess: str, now: float | None = None) -> tuple[str, str]:
        """局面 → (不说的原因, 给她的一句做法)。原因为空 = 不拦。"""
        if not d:
            return "", ""
        g = self.gk(gid)
        on = str(g.get("social_read", "on")) == "on"
        now = now or time.time()
        about = d.get("about_her", "none")
        skip, note = "", ""
        h = self.db.get(f"qq_hush_{gid}") or {}
        now_h = bool(hasattr(self, "hushed") and self.hushed(gid, now))
        stale = not now_h and now - float(h.get("ts") or 0) < 1800    # 刚安静完：模型看到的多半还是那几句旧的“闭嘴”，别再安静一轮
        if about in ("hush", "annoyed") and on and not stale:
            if hasattr(self, "hush") and not now_h:
                self.hush(gid, f"局面：{d.get('why') or about}"[:30], now=now)
            if not mentioned:
                skip = "局面：有人嫌你 / 叫你别说，先安静"
            else:
                note = ("（36.0）刚才这句是在嫌你 / 叫你闭嘴：不回嘴、不解释、不逗他。他要是还问了正经问题，"
                        "就只认一句再答（“好，我少说点”），别的不说")
        elif d.get("pair") and not mentioned and not guess and d.get("to_whom") not in ("她", "你", "小满", "大家") and on:
            skip = f"局面：{d['pair']} 在一问一答，没提你"
        if d.get("venting") and on:
            note += (f"（36.0）{d['venting']} 在吐槽：先站他那边（“这确实烦”“换我也受不了”），"
                     "别说风凉话（“不归你管”“那也是白搭”），别挑他的字眼")
        if d.get("open_ask") and on:
            note += f"（36.0）群友接着你的话问过你「{d['open_ask'][:40]}」，你还没答：先答这一句，别回嘴、别翻旧账"
        if about == "correct" and on:
            note += "（36.0）有人在指出你说错了：第一句先认（错了就说错了），别反问、别挑他字眼"
        return skip, note

    def room_speak_ok(self, gid: int, d: dict) -> bool:
        """被纠正 / 有她没答的追问时，没过门槛也放行（会让她多说，默认只记录）。"""
        if not d or d.get("about_her") not in ("correct",) and not d.get("open_ask"):
            return False
        mode = str(self.gk(gid).get("social_read_speak", "shadow"))
        if mode == "shadow":
            self.check_log(gid, "social_read_speak", "shadow", "", f"会放行：{json.dumps(d, ensure_ascii=False)[:200]}")
        return mode == "on"

    # ================= 被嫌的那句本身叫了她 =================
    def hush_reply_guard(self, gid: int, texts: list, now: float | None = None) -> str:
        """这一批里冲她来的是“闭嘴 / 嫌她”（拆开发也算）：返回原因 → 不回嘴。正经问了问题（带问号、不是嫌她）照答。"""
        if not self.gk(gid).get("hush_no_retort", True) or not texts:
            return ""
        now = now or time.time()
        rows = self._rows(gid, 12)
        fresh = now - float(self._st(gid).last_reply_ts or 0) <= 90          # 她刚说完：“？”“你在说啥”才算嫌她
        for t in texts[-4:]:
            uid, body = int(t[1] or 0), re.sub(r"\[[^\]]*\]", "", str(t[2] or "")).strip()
            joined = joined_short(rows, uid, now) or body
            for s in (body, joined):
                if s and (self.QUIET.search(s) or HUH_STRONG.search(s) or (fresh and len(s) <= 12 and self.HUH.search(s))):
                    return f"在嫌你 / 叫你闭嘴（「{s[:12]}」），不回嘴"
        return ""

    # ================= A4 冷场 =================
    def cold_streak(self, gid: int, now: float | None = None, hours: float = 6) -> int:
        """她最近主动说的（没被叫到）从最近一条往回数，连着几次没人接（效果分 < 0.2）。"""
        now = now or time.time()
        n = 0
        for r in self.db.conn.execute("SELECT score FROM qq_reply_effect WHERE group_id=? AND mentioned=0 AND done=1 AND ts>? "
                                      "ORDER BY id DESC LIMIT 6", (gid, now - hours * 3600)).fetchall():
            if r["score"] is not None and float(r["score"]) < 0.2:
                n += 1
            else:
                break
        return n

    def media_gate(self, gid: int, texts: list, now: float | None = None) -> str:
        """群友发图 / 位置 / 视频 / 链接、没配文字也没问她：看不清的不点评；一小时最多主动点评一次。"""
        g = self.gk(gid)
        if not g.get("media_first_comment", True) or not texts:
            return ""
        bodies = [str(t[2] or "") for t in texts]
        if not all(is_media_only(b) or str(b).startswith("[表情包") for b in bodies) or not any(is_media_only(b) for b in bodies):
            return ""
        if any(MEDIA_UNCLEAR.search(b) for b in bodies):
            return "图 / 视频看不清，没人问你就不点评"
        now = now or time.time()
        last = float(self._st(gid).__dict__.get("media_comment_ts") or 0)
        if now - last < float(g.get("media_comment_minutes", 60)) * 60:
            return "这一小时已经主动点评过一次图 / 位置了"
        return ""

    def note_media_comment(self, gid: int, texts: list, now: float | None = None):
        if texts and all(is_media_only(str(t[2] or "")) or str(t[2] or "").startswith("[表情包") for t in texts):
            self._st(gid).__dict__["media_comment_ts"] = now or time.time()

    def cold_gate(self, gid: int, p: float, thr: float, now: float | None = None) -> str:
        g = self.gk(gid)
        k = int(g.get("cold_streak_n", 2) or 0)
        if not k:
            return ""
        n = self.cold_streak(gid, now)
        bump = float(g.get("cold_streak_bump", 0.15) or 0)
        if n >= k and p < thr + bump:
            return f"主动说的连着 {n} 次没人接，先收着（{p:.2f} < {thr + bump:.2f}）"
        return ""

    # ================= B5 每周“别再说” =================
    def avoid_line(self, gid: int) -> str:
        if not self.gk(gid).get("avoid_weekly", True):
            return ""
        key = f"qq_avoid_{gid}"
        d = self.db.get(key) or {}
        if time.time() - float(d.get("ts") or 0) > 86400:
            rows = self.db.conn.execute("SELECT content FROM qq_group_messages WHERE group_id=? AND role='assistant' AND ts>?",
                                        (gid, time.time() - 7 * 86400)).fetchall()
            d = {"ts": time.time(), "words": lead_words([str(r["content"] or "") for r in rows])}
            self.db.set(key, d)
        w = d.get("words") or []
        return ("- 你这周最常用的说法：" + "、".join(w) + "。这几天少用，换个说法或者干脆不用") if w else ""

    # ================= B7 合规 =================
    def opt_signal(self, gid: int, uid: int, text: str) -> str:
        t = str(text or "").strip()
        key = f"qq_optout_{gid}"
        cur = set(int(x) for x in (self.db.get(key) or []))
        if OPT_OUT.search(t):
            cur.add(int(uid))
            self.db.set(key, sorted(cur))
            return "out"
        if OPT_IN.search(t) and int(uid) in cur:
            cur.discard(int(uid))
            self.db.set(key, sorted(cur) or None)
            return "in"
        return ""

    def opted_out(self, gid: int, uid) -> bool:
        try:
            return int(uid) in set(int(x) for x in (self.db.get(f"qq_optout_{gid}") or []))
        except (TypeError, ValueError):
            return False

    def long_chat_note(self, gid: int, uid, now: float | None = None) -> str:
        """同一个人和她连着聊满 2 小时（中间没断过 20 分钟）：提醒一句，一天一次。"""
        hrs = float(self.gk(gid).get("long_chat_hours", 2) or 0)
        if not hrs or not uid:
            return ""
        now = now or time.time()
        key = f"qq_longchat_{gid}_{uid}_{time.strftime('%Y%m%d', time.localtime(now))}"
        if self.db.get(key):
            return ""
        rows = self.db.conn.execute("SELECT ts FROM qq_group_messages WHERE group_id=? AND (user_id=? OR role='assistant') AND ts>? "
                                    "ORDER BY id DESC", (gid, int(uid), now - hrs * 3600 - 1200)).fetchall()
        ts = [float(r["ts"]) for r in rows]
        if not ts or now - ts[-1] < hrs * 3600:
            return ""
        start = ts[0]
        for t in ts[1:]:
            if start - t > 1200:
                return ""
            start = t
        if now - start < hrs * 3600:
            return ""
        self.db.set(key, True)
        return "（36.0）你们已经连着聊了两个多小时：这次顺口提醒一句歇会儿、喝口水，一句就够，别说教"

    def fav_allowed(self, gid: int) -> bool:
        return not self.gk(gid).get("has_minor", False)
