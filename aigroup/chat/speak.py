"""说话：一批消息到了以后，要不要开口、说什么、怎么发。

_after_wait（等连发说完 → 算特征和概率 → decide）→ speak（拼提示词 → 问大模型 → 解析计划 → 质检挑一条 → 打字、发）。
还有复读、点表情、引用、像真人的回复延迟。
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time
from difflib import SequenceMatcher

from aigroup.chat.gate import likelihood, talk_factor, threshold_ex
from aigroup.chat.mood import Mood, turn_memory
from aigroup.chat.plan import (
    AT_RE,
    REACT_RE,
    TAG_RE,
    batch_features,
    decide,
    parse_group_reply,
    to_segments,
)
from aigroup.chat.prompt import appraise_verdict
from aigroup.chat.quality import make_typo, pick
from aigroup.chat.trace import new_trace
from aigroup.onebot import face_for, image_seg, reply_seg, text_seg

log = logging.getLogger("aigroup.group")
tlog = logging.getLogger("aigroup.text.group")       # 13.0 带聊天正文的：只进她家里的日志文件，不进控制台
SOFT_BLOCK = 6.0          # 5.0 罚分到这么多：没被叫到就不说了；被叫到就让她重写一次



# 20.0 拿不准时问出来的动作：名字 → (什么时候选, 对应 say / wait / skip, 给她的一句做法)
JUDGE_ACTS = {
    "不说": ("跟你无关、别人在互相聊、插进去突兀", "skip", ""),
    "等": ("话还没说完、在等别人回", "wait", ""),
    "接住": ("有人在接你的话、损你、提到你", "say", "接住就好，一两句、可以就一个表情，别把刚才的话再说一遍"),
    "回答": ("有人在问、你知道答案", "say", "直接给具体答案，别反问回去"),
    "追问": ("话没说完、指代不清、你想知道后续", "say", "短短问一句就行（“哪个？”“然后呢”），别自己猜着答"),
    "接梗": ("气氛轻松、你有好玩的话", "say", "一句就够，梗要接得上原意"),
    "纠错": ("有人说错了事实、你有把握", "say", "一句话指出哪里不对、给个台阶，别说教；没把握就别纠"),
    "补充": ("大家缺一个你知道的信息", "say", "只补最关键的一点，最多两句"),
    "关心": ("有人累了、难过、遇到事", "say", "先接住情绪，别急着给建议，也别连着追问"),
}


def parse_judge(out: str) -> tuple[str, str]:
    """{"why": .., "act": ..}；模型不听话只回了一个字（老格式“说 / 等 / 不”）也认。"""
    import json as _json
    import re as _re
    s = (out or "").strip()
    m = _re.search(r"\{.*\}", s, _re.S)
    act, why = "", ""
    if m:
        try:
            d = _json.loads(m.group(0))
            act, why = str(d.get("act") or "").strip(), str(d.get("why") or "").strip()
        except ValueError:
            pass
    if act not in JUDGE_ACTS:
        hit = next((k for k in JUDGE_ACTS if k in (act or s)), "")
        if not hit:
            hit = "等" if s.startswith("等") else "不说" if s.startswith("不") else "接住" if s.startswith("说") else "不说"
        act = hit
    return act, why[:80]


def judge_examples(db, gid: int, tz=None, k: int = 3) -> str:
    """你在小屋判过的这个群的例子：该接、不该插各最多 k 条，每条只带那一刻的最后 3 句。"""
    out = []
    try:
        for lab, word in ((1, "该接"), (0, "不该插")):
            rows = db.conn.execute("SELECT ts, act FROM qq_gate_log WHERE group_id=? AND human=? AND mentioned=0 "
                                   "AND NOT (human=0 AND IFNULL(human_why,'')='说的不对') "
                                   "ORDER BY id DESC LIMIT ?", (gid, lab, k)).fetchall()
            for r in rows:
                ctx = list(reversed(db.conn.execute(
                    "SELECT name, role, content FROM qq_group_messages WHERE group_id=? AND ts<=? ORDER BY id DESC LIMIT 3",
                    (gid, r["ts"] + 1)).fetchall()))
                if not ctx:
                    continue
                lines = " / ".join(f"{'你' if c['role'] == 'assistant' else c['name']}：{str(c['content'])[:40]}" for c in ctx)
                out.append(f"- {lines} → {word}")
    except Exception:  # noqa: BLE001        # 老库没有 act 列
        return ""
    return "\n".join(out)

class SpeakMixin:
    def member_names(self, gid: int) -> list[str]:
        """这个群里群友的名字、外号、称呼（“小王 / 王哥”拆开）。"""
        out: list[str] = []
        for uid, nm in list(self.names(gid).items())[:40]:
            out.append(str(nm))
            p = self.person(int(uid), gid) if str(uid).isdigit() else None
            if p:
                out += [str(a) for a in p.get("aliases") or []]
                call = str((p.get("profile") or {}).get("称呼") or "")
                out += [w for w in re.split(r"[；;，,/、（()）\s]+|她爱自称|大家都这么叫|外号", call) if w]
        return [w.strip("“”\"' ") for w in dict.fromkeys(out) if 2 <= len(w.strip("“”\"' ")) <= 12]

    def names_off(self, gid: int) -> bool:
        """5.0 这个群不叫名字吗：小屋 / 配置里明确设了 call_names 就听设的；没设就看群友自己的习惯——
        最近 300 句里（不算 @），提到群友名字、外号、称呼的不到 4%，就当这个群“平时不喊名字”（call_names_auto）。"""
        from aigroup.core.config import chan_group
        if "call_names" in (self.gover.get(int(gid)) or {}) or "call_names" in (chan_group(self.cfg) or {}):
            return not self.gk(gid).get("call_names", True)
        if not self.gk(gid).get("call_names_auto", True):
            return False
        cache = self.__dict__.setdefault("_names_habit", {})
        hit = cache.get(gid)
        if hit and time.time() - hit[0] < 3600:
            return hit[1]
        rows = [r["content"] for r in self.db.conn.execute(
            "SELECT content FROM qq_group_messages WHERE group_id=? AND role='user' ORDER BY id DESC LIMIT 300", (gid,))]
        off = False
        if len(rows) >= 150:
            names = [n for n in self.member_names(gid) if len(n) >= 2]
            said = sum(1 for t in rows if any(n in re.sub(r"@\S+|\[回复[^\]]*\]", "", t) for n in names))
            off = said / len(rows) < 0.04
            if off:
                log.info("群 %s：群友之间很少叫名字（%d/%d），她也不叫", gid, said, len(rows))
        cache[gid] = (time.time(), off)
        return off

    def stale_phrases(self, gid: int) -> list[str]:
        """5.0 她这几个小时已经用过的口头禅、招牌梗（所有群一起算：人是同一个）。"""
        from aigroup.chat.address import her_phrases
        hours = float(self.gk(gid).get("stale_hours", 3) or 0)
        if hours <= 0:
            return []
        now = time.time()
        rows = self.db.conn.execute("SELECT ts, content FROM qq_group_messages WHERE role='assistant' AND ts>? "
                                    "ORDER BY id DESC LIMIT 80", (now - 86400,)).fetchall()
        day = [r["content"] for r in rows]
        recent = [r["content"] for r in rows if now - r["ts"] < hours * 3600]
        if not recent:
            return []
        others = [r["content"] for r in self.db.conn.execute(
            "SELECT content FROM qq_group_messages WHERE group_id=? AND role='user' ORDER BY id DESC LIMIT 40", (gid,))]
        p = self.brain.persona
        names = [p.name, getattr(p, "formal_name", "") or "", *self.aliases]
        for g in self.groups:                                   # 所有群的群友名字都不算“梗”（她在别的群叫过的称呼也是）
            names += self.member_names(g)
        out = her_phrases(recent, day, others, list(p.catchphrases or []), names=names)
        gd = float(self.gk(gid).get("gag_days", 3) or 0)
        if gd > 0:                                              # 20.0 她自己的梗连着几天都在用：也先歇一歇
            rows3 = [r["content"] for r in self.db.conn.execute(
                "SELECT content FROM qq_group_messages WHERE role='assistant' AND ts>? ORDER BY id DESC LIMIT 200",
                (now - gd * 86400,)).fetchall()]
            last24 = [r["content"] for r in rows if now - r["ts"] < 86400]
            for w in her_phrases(last24, rows3, others, [], limit=4, names=names):
                if w not in out and not any(w in x or x in w for x in out):
                    out.append(w)
        return out[:10]

    def recent_sizes(self, gid: int, n: int = 3) -> list[int]:
        """她最近几次开口各发了几条（看 qq_reply_effect 里记的消息 id 数）。"""
        out = []
        for r in self.db.conn.execute("SELECT her_ids FROM qq_reply_effect WHERE group_id=? ORDER BY id DESC LIMIT ?",
                                      (gid, n)).fetchall():
            try:
                out.append(len(json.loads(r["her_ids"] or "[]")) or 1)
            except ValueError:
                out.append(1)
        return out

    def recent_her(self, gid: int, n: int = 15, hours: float = 6) -> list[str]:
        return [r["content"] for r in self.db.conn.execute(
            "SELECT content FROM qq_group_messages WHERE group_id=? AND role='assistant' AND ts>? ORDER BY id DESC LIMIT ?",
            (gid, time.time() - hours * 3600, n)).fetchall()]

    def too_similar(self, gid: int, text: str) -> bool:
        t = re.sub(r"\s+", "", text)
        if len(t) < 4:
            return False
        th = float(self.gk(gid).get("similar_threshold", 0.75))
        return any(SequenceMatcher(None, t, re.sub(r"\s+", "", r)).ratio() >= th for r in self.recent_her(gid))

    async def react(self, mid, emotion: str = "") -> bool:
        """给一条群消息点个表情（NapCat 的 set_msg_emoji_like）。"""
        fid = face_for(emotion) if emotion else random.choice([76, 13, 66, 182, 178])
        if not mid or fid is None:
            return False
        try:
            await self.ob.call("set_msg_emoji_like", message_id=int(mid), emoji_id=str(fid))
            return True
        except Exception as e:  # noqa: BLE001
            log.debug("点表情失败：%s", e)
            return False

    async def maybe_repeat(self, gid: int) -> bool:
        """群里两个以上的人在复读同一句（短句）：她偶尔也跟一句。不过大模型。"""
        rows = self.db.conn.execute(
            "SELECT user_id, role, content FROM qq_group_messages WHERE group_id=? ORDER BY id DESC LIMIT 3",
            (gid,)).fetchall()
        if len(rows) < 2 or any(r["role"] != "user" for r in rows[:2]):
            return False
        a, b = rows[0], rows[1]
        text = a["content"].strip()
        st = self._st(gid)
        if (text != b["content"].strip() or a["user_id"] == b["user_id"] or not text or len(text) > 15
                or "@" in text or text.startswith("[") or st.last_repeat == text or "http" in text):
            return False
        st.last_repeat = text
        if random.random() >= float(self.gk(gid).get("repeat_chance", 0)):
            return False
        await asyncio.sleep(self.secs("read_delay", gid))
        try:
            mid = await self.ob.send_group(gid, [text_seg(text)])
        except Exception as e:  # noqa: BLE001
            log.info("复读没发出去：%s", e)
            return False
        self._record(gid, self.ob.self_id or 0, self.brain.persona.name, text, mid, role="assistant")
        st.last_reply_ts = time.time()
        st.replies.append(st.last_reply_ts)
        tlog.info("群 %s：跟着复读了「%s」", gid, text)
        return True

    def human_delay(self, gid: int, key: str, scale: float) -> float:
        """她“看到消息”要多久：有 50 个以上的样本时，从这个群群友自己的回复间隔里抽一个（乘 scale，限制在 2~90 秒）；
        不够就用配置里的区间。"""
        lat = self.latency_stats(gid) if self.gk(gid).get("human_latency", True) else {}
        if lat.get("n", 0) >= 50 and lat.get("q"):
            v = random.choice(lat["q"]) * scale * random.uniform(0.8, 1.2)
            return max(2.0, min(90.0, v)) * float(self.gk(gid).get("pace", 1.0))
        return self.secs(key, gid)

    async def _after_wait(self, gid: int):
        try:
            await asyncio.sleep(float(self.gk(gid)["merge_wait_seconds"]))
        except asyncio.CancelledError:
            return
        st = self._st(gid)
        mentioned, owner, trigger, who = st.batch_mentioned, st.batch_owner, st.batch_trigger, st.batch_who
        texts = list(st.batch_texts)
        guess = "" if mentioned else st.batch_guess
        callers = list(st.batch_callers)
        st.batch_guess, st.batch_callers = "", []
        st.batch_mentioned = st.batch_owner = False
        st.batch_trigger, st.batch_who, st.batch_texts = None, "", []
        if self.muted(gid):                              # 小屋里让这个群先别说话：只记消息
            return
        if not mentioned and await self.maybe_repeat(gid):
            return
        now = time.time()
        from aigroup.core.schedule import in_quiet
        quiet = in_quiet(self.brain.now(), self.cfg.get("quiet_hours"))
        if quiet and mentioned and self.gk(gid).get("night_catch_up", True):
            old = self.db.get(f"qq_night_{gid}") or []
            old = [old] if isinstance(old, dict) else list(old)
            self.db.set(f"qq_night_{gid}", (old + [{"who": who, "mid": trigger, "ts": now}])[-6:])
            log.info("群 %s：%s 半夜叫她，她睡了，早上再回", gid, who or "有人")
            return
        if not mentioned and not guess and texts and self._quiet_for_unprompted(gid, now):
            return
        activity = self.her_activity()
        busy = self.busy(activity)
        joined = " ".join(t[2] for t in texts)
        words = self.interests()
        interest = bool(words) and any(w in joined for w in words)
        n10 = sum(1 for t in st.replies if now - t < 600)
        recent = [(r["ts"], r["role"]) for r in self.db.conn.execute(
            "SELECT ts, role FROM qq_group_messages WHERE group_id=? AND ts>?", (gid, now - 300)).fetchall()]
        att = max([st.attention.get(t[1], now) for t in texts] or [0.0])
        waited = bool(st.wait and now < st.wait.get("until", 0) and any(t[1] == st.wait.get("uid") for t in texts))
        if st.wait and (waited or now >= st.wait.get("until", 0)):
            st.wait = None
        waited = waited or bool(guess)                 # 5.0 多半是冲她说的：和“等的人开口了”一样，不受冷却和阈值限制
        ignored = self.ignored_count(gid, now)
        moodf = Mood.feature(self.mood.get(gid, now)) if self.mood is not None else 0.0
        if self.mood is not None:
            self.mood.on_lively(gid, len(recent))
        feats = batch_features(texts, now=now, last_reply_ts=st.last_reply_ts, prev_msg_ts=st.batch_start_prev_ts,
                               recent=recent, owner_spoke=owner, interest=interest, busy=busy, attention=att,
                               energy=st.energy.get(now), cold_minutes=float(self.gk(gid).get("cold_minutes", 15)),
                               ignored=ignored, waited=waited, turns=turn_memory(self.turns_since_her(gid)),
                               mood=moodf)
        p = 1.0 if mentioned else likelihood(feats, self.weights)
        probs, spoke = self.gate.window(gid, now)
        talk = float(self.gk(gid).get("talkativeness", 0.3)) * talk_factor(self.gk(gid).get("talk_curve"), f"{self.brain.now():%H:%M}")
        if hasattr(self, "talk_mult"):                    # 21.1 群里有人让她少说 / 多说；份额超了收紧
            talk *= self.talk_mult(gid, now) * self.share_factor(gid)
        thr, keep, tie = threshold_ex(probs, min(1.0, talk), spoke)
        ok, why = decide(mentioned=mentioned, now=now, last_reply_ts=st.last_reply_ts, replies_10min=n10, quiet=quiet,
                         g=self.gk(gid), p=p, thr=thr, backoff_wait=st.backoff.waiting(now), pending=len(texts), tie=tie,
                         rand=random.random(), waited=waited)
        explore = False
        if (not ok and not mentioned and not quiet and why.startswith("不太想插话") and talk > 0
                and random.random() < float(self.gk(gid).get("explore_rate", 0.02))):
            ok, explore, why = True, True, f"随机试一次（{why}）"      # 少量放行本该拦下的：以后重新拟合时不偏
        judged = ""
        judge_act = judge_why = ""
        nb = None
        if (not ok and not mentioned and not quiet and why.startswith("不太想插话") and talk > 0 and texts
                and self.gk(gid).get("label_nb", True) and hasattr(self, "label_prob")
                and p >= thr - float(self.gk(gid).get("gate_judge_band", 0.15))):
            nb = self.label_prob(gid, feats, now)                 # 16.0 你判过的标注训练的小判断器：有把握就不用再问大模型
            if nb is not None and nb >= 0.85:
                judged, ok, explore, why = "say", True, True, f"小判断器说该说（{nb:.2f}）"
            elif nb is not None and nb <= 0.15:
                judged = "skip"
        if (not ok and not judged and not mentioned and not quiet and why.startswith("不太想插话") and talk > 0 and texts
                and self.gk(gid).get("gate_judge", True) and p >= thr - float(self.gk(gid).get("gate_judge_band", 0.15))
                and self._allow(f"judge_{gid}", int(self.gk(gid).get("gate_judge_per_hour", 20)))):
            try:
                judged = await self.quick_judge(gid)       # 13.0 拿不准：问一下便宜模型“说 / 等 / 不”
            except asyncio.CancelledError:                 # 问的时候又来了新消息：这批留给下一轮
                st.batch_texts = texts + st.batch_texts
                st.batch_owner = st.batch_owner or owner
                st.batch_callers = callers + st.batch_callers
                st.batch_guess = st.batch_guess or guess
                return
            jd = (getattr(self, "_judged", {}) or {}).pop(gid, None) or {}
            judge_act, judge_why = jd.get("act", ""), jd.get("why", "")
            if judged == "say":
                ok, explore, why = True, True, f"拿不准，问了一下：{judge_act or '说'}（{p:.2f} / 阈值 {thr:.2f}）"
            elif judged == "wait" and texts:
                st.wait = {"uid": int(texts[-1][1]), "until": now + 150}
                log.info("群 %s：拿不准，先等等看 %s 还说不说", gid, texts[-1][1])
        if (ok and texts and hasattr(self, "borderline")
                and self.borderline(gid, p=p, thr=thr, mentioned=mentioned, explore=explore, judged=judged, quiet=quiet)
                and not waited and self._allow(f"judge_{gid}", int(self.gk(gid).get("gate_judge_per_hour", 20)))):
            try:                                           # 21.0 刚过阈值一点点：再问一下（大模型倾向说得太早）
                judged = await self.quick_judge(gid)
            except asyncio.CancelledError:
                st.batch_texts = texts + st.batch_texts
                st.batch_owner = st.batch_owner or owner
                st.batch_callers = callers + st.batch_callers
                st.batch_guess = st.batch_guess or guess
                return
            jd = (getattr(self, "_judged", {}) or {}).pop(gid, None) or {}
            judge_act, judge_why = jd.get("act", ""), jd.get("why", "")
            if judged != "say":
                ok, why = False, f"刚过阈值（{p:.2f} / {thr:.2f}），再问了一下：{judge_act or '不说'}"
                if judged == "wait":
                    st.wait = {"uid": int(texts[-1][1]), "until": now + 150}
        gate_id = None
        if texts and not quiet:
            gate_id = self.gate.add(gid, mentioned, p, thr, ok, feats, explore=explore, judge=judged,
                                    act=judge_act, why=judge_why or ("" if mentioned else why))
        if not ok:
            log.debug("群 %s：不说话（%s）", gid, why)
            if not mentioned and texts and not quiet and random.random() < float(self.gk(gid).get("react_chance", 0)):
                await asyncio.sleep(self.secs("notice_delay", gid))
                await self.react(texts[-1][0])
            return
        log.info("群 %s：%s", gid, why)
        if mentioned:
            delay = self.secs("busy_delay_minutes", gid) * 60 if busy else self.human_delay(gid, "read_delay", 0.5)
            if not busy:                                 # 21.1 被叫到：回得快 = 有连接感（Templeton 2022），看到消息最多拖这么久
                delay = min(delay, float(self.gk(gid).get("mention_read_cap", 20)))
        else:
            delay = self.human_delay(gid, "notice_delay", 1.0)
        if st.mention_due and now - st.mention_due > 120:
            st.mentioned_since = st.mention_due = 0.0    # 上次定的时刻早过了（中间没回成）：这次重新算
        if mentioned and st.mention_due:
            delay = max(0.0, st.mention_due - now)       # 18.1 已经定好什么时候回：新消息不让她重新等
        if mentioned and st.mentioned_since and now - st.mentioned_since > 600:
            delay = 0                                    # 叫她已经等了 10 分钟（中间一直有新消息）：别再拖
        st.last_wait = delay + (now - st.mentioned_since if mentioned and st.mentioned_since else 0)
        if delay > 0:
            if mentioned and not st.mentioned_since:
                st.mentioned_since = now
            if mentioned and not st.mention_due:
                st.mention_due = now + delay
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                # 等着回的时候群里又来了新消息：把“被叫到”这些留给下一轮，别丢了
                st.batch_mentioned = st.batch_mentioned or mentioned
                st.batch_owner = st.batch_owner or owner
                if mentioned and not st.batch_trigger:
                    st.batch_trigger, st.batch_who = trigger, who
                st.batch_texts = texts + st.batch_texts
                st.batch_callers = callers + st.batch_callers
                self.gate.drop(gate_id)
                return
        waited_for = (now - st.mentioned_since) if (mentioned and st.mentioned_since) else 0.0
        st.mentioned_since = st.mention_due = 0.0
        note = ""
        if getattr(st, "talk_note", ""):                 # 21.1 有人让她少说点 / 多说点
            note += st.talk_note
            st.talk_note = ""
        if waited_for and hasattr(self, "offline_note"):
            note += self.offline_note(gid, waited_for)
        if busy and activity:
            note = f"你现在正在：{activity}。" + ("偷空看了眼手机，回一句就好。" if mentioned else "抽空看了眼群。")
        many = list(dict.fromkeys(n for n, _ in callers))
        if len(many) >= 2:
            note += ("这一批里好几个人在叫你：" + "；".join(f"{n}：「{t}」" for n, t in callers[-4:])
                     + "。每个人都回应到（一条里一起回、或者分两条都行），别只回一个。")
        may_ignore = False
        if mentioned and texts:
            cu = next((t[1] for t in texts if t[0] == trigger), texts[-1][1])
            calls, answered = self.pester_count(gid, cu, now)
            lim = int(self.gk(gid).get("pester_calls", 4) or 0)
            if lim and calls >= lim:
                # 13.2 同一个人一会儿工夫叫了她好几次：多半在逗她 / 刷屏，回得越来越短，可以不回
                may_ignore = True
                note += (f"{who or '他'}这 15 分钟里已经叫了你 {calls} 次（你回了 {answered} 次）。"
                         "要是他在逗你、刷屏、绕圈子、想让你说你不想说的话：这次可以不回（act 写 skip），"
                         "要回就一条、几个字，别接他的茬往下聊，也别再追问你问过的事；他要是正经在问事情，就照常好好回。")
                log.info("群 %s：%s 15 分钟里叫了她 %d 次，降温", gid, who or cu, calls)
            asked = self.her_questions(gid, now)
            if asked:
                note += ("你刚才已经问过：" + "；".join(f"「{q}」" for q in asked)
                         + "。对方没正面回答就算了，别再问一遍同样的问题。")
        if judge_act and judge_act in JUDGE_ACTS and JUDGE_ACTS[judge_act][2]:
            note += f"（这次适合「{judge_act}」：{JUDGE_ACTS[judge_act][2]}）"     # 20.0 判断时选的动作 → 一句做法
        if guess:
            how, _, gname = guess.partition(":")
            if how == "bid":
                gname, _, her_line = gname.partition(":")
                note += (f"{gname}刚才那句在接你之前对他说的话（你说的是「{her_line}」）。接住他：回一句，哪怕很短、哪怕只是笑一下、"
                         "认个怂、怼回去；别装没看见，也别把你那句再说一遍。真的是在跟别人说，才 skip。")
            elif how == "about":
                note += (f"{gname}刚才在说你（“它”“机器人”说的是你）。想回就回一句（被说不聪明可以不服气地怼回去、被夸就得意一下），"
                         "当成群里的梗，别解释自己是怎么做的；没什么好说的就 skip。")
            else:
                note += (f"{gname}刚才那句没 @ 你，但多半是冲你说的（你刚说完话，他接着说）：是的话就接住、给个具体的回答；"
                         "要是其实在跟别人说，就 skip。")
            who = who or gname
        st.speaking = True                               # 从这里开始不会被新消息打断
        try:
            async with st.lock:
                try:
                    said = await self.speak(gid, mentioned=mentioned, owner_spoke=owner, trigger=trigger, who=who,
                                     note=note, interest=interest, gate_id=gate_id, focus=list(dict.fromkeys([t[1] for t in texts] + self.alias_hits(joined))),
                                     query=joined, last_is_sticker=bool(texts and texts[-1][3].get("sticker")),
                                     may_ignore=may_ignore, bid=guess.startswith("bid:"))
                    if guess.startswith("bid:") and hasattr(self, "note_bid"):
                        self.note_bid(gid, "", "", answered=bool(said))
                except Exception as e:  # noqa: BLE001
                    log.warning("群 %s 回复失败：%s", gid, e)
        finally:
            st.speaking = False
            if st.batch_texts and (not st.pending or st.pending.done() or st.pending is asyncio.current_task()):
                st.pending = self._spawn(self._after_wait(gid))      # 她说话时群里来的新消息：接着看

    async def speak(self, gid: int, mentioned: bool, owner_spoke: bool, trigger: int | None, who: str,
                    why: str | None = None, note: str = "", interest: bool = False, gate_id: int | None = None,
                    focus: list[int] | None = None, query: str = "", last_is_sticker: bool = False, web: str = "",
                    must: bool = False, _rethink: int = 0, may_ignore: bool = False, pitch: bool = False,
                    bid: bool = False) -> bool:
        """问大模型、质检、发。why 为空 = 在接群友的话（跳过时才算进退避）；冒泡、欢迎新人、小屋 nudge 会给 why。
        must：生日、约好的事、打气这类“这次一定要说”的，不受每天主动开口的预算限制，新消息来了也照样说（会重新想）。"""
        b = self.brain
        st = self._st(gid)
        start = time.time()
        trace = new_trace(gid)
        tm: dict = {"wait": round(float(getattr(st, "last_wait", 0) or 0), 1)}   # 21.1 每一段花了几秒（小屋回放里看慢在哪）
        st.last_wait = 0.0
        replying = why is None and not mentioned
        cap = int(self.gk(gid).get("unprompted_per_day", 0) or 0)
        if not mentioned and not must and cap and self.unprompted_today(gid) >= cap:      # 13.0 每天主动开口的总预算（插话、冒泡、分享共用）
            log.info("群 %s：今天主动说的够多了（%d 次），这次不说", gid, cap)
            self.gate.mark(gate_id, spoke=False)
            return False
        if why is not None and not mentioned and not must and hasattr(self, "lonely_reason"):
            lonely = self.lonely_reason(gid)                 # 19.1 说完没人接不追发、深夜没人不主动
            if lonely:
                log.info("群 %s：%s，这次不主动说", gid, lonely)
                self.gate.mark(gate_id, spoke=False)
                return False
        last_id = int(self.db.conn.execute("SELECT IFNULL(MAX(id), 0) m FROM qq_group_messages WHERE group_id=?",
                                           (gid,)).fetchone()["m"])
        if why is None and not web:                                   # 接群友话时：有她得先查一下的事就搜一搜
            texts = []
            for r in reversed(list(self._rows(gid, 6))):     # 只看她上次说话以后的新消息（问过的不再重复搜）
                if r["role"] != "user":
                    break
                texts.insert(0, r["content"])
            web = None
            if hasattr(self, "agent_on") and self.agent_on(gid):        # 19.0 先想一想：要不要查、要不要设提醒
                try:
                    t_a = time.time()
                    web = await self.agent_think(gid, mentioned, who, trigger)
                    tm["think"] = round(time.time() - t_a, 1)
                except Exception as e:  # noqa: BLE001
                    log.warning("群 %s：想一想出错，退回只搜一次：%s", gid, e)
                    web = None
            if web is None:
                web = await self.web_block(gid, texts, mentioned)
        messages = await self.build_messages(gid, mentioned, owner_spoke, who, why, note=note, interest=interest,
                                             query=query, focus=focus, web=web,
                                             flags={"pestered"} if may_ignore else None)
        if gate_id:                                      # 20.1 这次去掉了哪段 / 加没加锚点：周报里比被接率
            try:
                self.db.conn.execute("UPDATE qq_gate_log SET ablated=?, drift=? WHERE id=?",
                                     ((getattr(self, "_ablated", {}) or {}).get(gid, ""),
                                      (getattr(self, "_drift", {}) or {}).get(gid, ""), gate_id))
                self.db.conn.commit()
            except Exception:  # noqa: BLE001
                pass
        t_l = time.time()
        raw = await b.llm.chat(messages, role=str(self.gk(gid).get("route") or "chat"),
                               json_mode=bool(self.gk(gid).get("plan_json", True)))
        tm["llm"] = round(time.time() - t_l, 1)
        tlog.info("群 %s 原文：%s", gid, raw)
        fresh = self.db.conn.execute("SELECT COUNT(*) n, IFNULL(MAX(to_her), 0) h FROM qq_group_messages "
                                     "WHERE group_id=? AND role='user' AND id>?", (gid, last_id)).fetchone()
        if fresh["n"] and (fresh["h"] or fresh["n"] >= int(self.gk(gid).get("rethink_min_new", 2))):
            if why is not None and not mentioned and not must:  # 冒泡、分享时群里突然热闹了：这句不说了，照常看新消息
                log.info("群 %s：想说的时候群里来了 %d 条新消息，这句先不说", gid, fresh["n"])
                self.gate.mark(gate_id, spoke=False)
                return False
            if _rethink < int(self.gk(gid).get("rethink_max", 2)):
                # 13.0 想好了还没发、群里又说了几句（或者有人叫她）：带上新消息重新想（MaiBot 的“新消息打断重想”）
                log.info("群 %s：还没发出去就来了 %d 条新消息，重新想一遍", gid, fresh["n"])
                if st.batch_mentioned:
                    mentioned, who = True, who or st.batch_who
                    trigger = trigger or st.batch_trigger
                owner_spoke = owner_spoke or st.batch_owner
                if st.batch_callers:
                    note += ("（刚才又有人叫你：" + "；".join(f"{n}：「{t}」" for n, t in st.batch_callers[-4:])
                             + "，都回应到）")
                st.batch_mentioned = st.batch_owner = False
                st.batch_trigger, st.batch_who, st.batch_texts, st.batch_callers, st.batch_guess = None, "", [], [], ""
                new = [int(r["user_id"]) for r in self.db.conn.execute(
                    "SELECT user_id FROM qq_group_messages WHERE group_id=? AND role='user' AND id>?", (gid, last_id))]
                return await self.speak(gid, mentioned=mentioned, owner_spoke=owner_spoke, trigger=trigger, who=who,
                                        why=why, note=note, interest=interest, gate_id=gate_id,
                                        focus=list(dict.fromkeys([*(focus or []), *new])), query=self._recent_text(gid),
                                        last_is_sticker=last_is_sticker, web=web, must=must, _rethink=_rethink + 1,
                                        may_ignore=may_ignore, pitch=pitch, bid=bid)
        mb = 1 if may_ignore else int(self.gk(gid)["max_bubbles"])
        use_plan = bool(self.gk(gid).get("plan_json", True))
        tmap = self._tmap.get(gid, {}) if use_plan else {}
        plan = self.parse_plan(raw) if use_plan else None
        quote, target_uid = (trigger if mentioned else None), None
        cands: list[list[str]] = []
        sticker = ""
        kind = ""
        fix_notes: list[str] = []          # 21.0 九问自评里要改的地方（说漏、伤面子、玩笑不合适、讨好）：交给重写

        def done(outcome: str, sent: list | None = None, quality: str = ""):
            if self.gk(gid).get("trace", True):
                try:
                    tm["total"] = round(time.time() - start, 1)
                    self.tracer.add(trace=trace, gid=gid, gate_id=gate_id, mentioned=mentioned, why=why or "",
                                    messages=messages, raw=raw, plan=plan, sent=sent or [], quality=quality,
                                    outcome=outcome, timing=tm)
                except Exception as e:  # noqa: BLE001
                    log.debug("存回放失败：%s", e)

        def skip(outcome: str = "skip", quality: str = "") -> bool:
            self.gate.mark(gate_id, spoke=False, llm_skip=True, kind=kind)
            if replying:
                st.backoff.skipped()
                log.info("群 %s：她想了想没说（连续 %d 次）", gid, st.backoff.n)
            done(outcome, quality=quality)
            return False

        if use_plan and plan is None:
            # 解析失败：绝不把原文发出去（1.4 会把 {"think": …} 当成普通回复发进群）
            log.warning("群 %s：大模型没按格式输出，这次不发原文", gid)
            if not mentioned:
                return skip("parse_fail")
            looks_json = "{" in (raw or "") or re.search(r'"(think|act|say)"', raw or "")
            fb = None if looks_json else parse_group_reply(raw, b.persona.name, mb)
            cands = [fb] if fb else [[random.choice(["？", "嗯？", "在呢"])]]
        elif plan is not None:
            kind = plan["kind"]
            if plan["think"]:
                tlog.info("群 %s 她想（%s）：%s", gid, kind or "-", plan["think"])
            if plan.get("hold") and self.gk(gid).get("hold_thoughts", True):
                self.hold_thought(gid, plan["hold"], plan.get("think", ""))
            nxt = plan.get("next")
            if nxt and isinstance(nxt, dict):
                try:
                    wt = tmap.get(int(str(nxt.get("wait_for")).lstrip("#")))
                except (TypeError, ValueError):
                    wt = None
                if wt and wt["role"] == "user":
                    try:
                        want = float(re.sub(r"[^\d.]", "", str(nxt.get("timeout") or "")) or 180)
                    except ValueError:                       # “3分钟”这种：按 180 秒
                        want = 180.0
                    if "分" in str(nxt.get("timeout") or ""):
                        want *= 60
                    secs = min(float(self.gk(gid).get("wait_max_seconds", 600)), max(30.0, want))
                    st.wait = {"uid": int(wt["uid"]), "until": time.time() + secs}
                    log.info("群 %s：她在等 %s 开口（%d 秒内）", gid, wt["name"], secs)
            tgt = tmap.get(plan["to"]) if plan["to"] else None
            if tgt and tgt["role"] == "user":
                target_uid = int(tgt["uid"])
            if replying and plan["act"] == "say" and kind in ("S1", "S2"):     # 只管接群友话；冒泡、欢迎、nudge 不管
                log.info("群 %s：她判断这和她无关（%s），不说", gid, kind)
                plan["act"] = "skip"
            sc = plan.get("score") or {}
            if (sc and plan["act"] == "say" and not mentioned and not must and not bid and self.gk(gid).get("self_score", True)
                    and (replying or why is not None)):
                avg = sum(sc.values()) / len(sc)
                low_new = sc.get("新意", 5) <= 2
                if avg < float(self.gk(gid).get("self_score_min", 3.0)) or low_new:
                    # 19.1 Inner Thoughts：念头分不够（尤其新意 ≤ 2：说过的、复述别人的）就不说
                    log.info("群 %s：念头分不够（平均 %.1f%s），不说", gid, avg, "，新意低" if low_new else "")
                    plan["act"] = "skip"
            ap = plan.get("appraise") or {}
            if ap and plan["act"] == "say" and self.gk(gid).get("appraise", True):
                # 别人在接她的话、回答她刚问的（bid）：和被叫到一样，只改说法、不会不说（Gottman：接住搭话）
                verdict, notes = appraise_verdict(ap, prompted=bool(mentioned or must or bid))
                self._appraise_log(gid, gate_id, ap, verdict)
                if not self.gk(gid).get("appraise_enforce", False):
                    verdict = "say"              # 21.1 自评只记录（不可信，线上 10/10 “现在”），时机交给听者
                if verdict == "wait" and (pitch or not self.gk(gid).get("pitch_queue", True)):
                    verdict = "skip"                        # 已经等过一次了 / 没开等待队列：这次就不说
                if verdict == "skip":
                    log.info("群 %s：九问自评 → 不说（%s；%s）", gid, "、".join(notes), ap.get("why", ""))
                    plan["act"] = "skip"
                elif verdict == "wait":
                    st.pitch = {"say": list(plan["say"])[:3], "ts": time.time(), "last_id": last_id,
                                "think": plan.get("think", ""), "why": ap.get("why", ""), "gate_id": gate_id}
                    log.info("群 %s：九问自评 → 再等等，先放着：%s", gid, " / ".join(plan["say"])[:60])
                    self._pitch_outcome(gid, "queued", plan["say"])
                    return skip("defer")
                elif verdict == "fix":
                    fix_notes = notes
                    log.info("群 %s：九问自评 → 要改（%s）", gid, "、".join(notes))
            if (plan["act"] == "say" and not mentioned and not must and not bid and (replying or pitch)
                    and hasattr(self, "listen") and str(self.gk(gid).get("listener_mode", "auto")) != "off"):
                # 21.1 独立的听者判时机（她自己的自评不可信）；影子模式只记不拦
                res = await self.listen(gid, plan["say"])
                enforced = bool(res) and self.listener_enforced(gid)
                self.listener_log(gate_id, res, enforced)
                if res:
                    tlog.info("群 %s 听者：%s（%s）%s%s", gid, res["window"], res.get("why", ""),
                              "；毛病：" + "、".join(res["issues"]) if res.get("issues") else "", "" if enforced else "【只记录】")
                if enforced and res.get("window") == "never":
                    plan["act"] = "skip"
                elif enforced and res.get("window") in ("early", "ok"):
                    # 21.1.1 “可以（不说也行）”也先放着：真人这时候多半不说；等一会儿再判还只是“可以”就算了
                    if pitch or not self.gk(gid).get("pitch_queue", True):
                        plan["act"] = "skip"
                    else:
                        st.pitch = {"say": list(plan["say"])[:3], "ts": time.time(), "last_id": last_id,
                                    "think": plan.get("think", ""), "why": res.get("why", ""), "until": res.get("until", ""),
                                    "gate_id": gate_id}
                        log.info("群 %s：听者说太早（等到：%s），先放着", gid, res.get("until") or "群里停一下")
                        self._pitch_outcome(gid, "queued", plan["say"])
                        return skip("defer")
                if enforced and res.get("issues") and plan["act"] == "say":
                    fix_notes = list(dict.fromkeys([*fix_notes, *res["issues"]]))
            if plan["act"] == "react" and not getattr(self.ob, "can_react", True):
                plan["act"] = "say" if plan["cands"] else "skip"          # 微信点不了表情：有话就说，没话就算了
            if plan["act"] == "react":
                mid = (tgt or {}).get("mid") or trigger or self._last_user_mid(gid)
                await self.react(mid, plan["react"] or "开心")
                self.gate.mark(gate_id, spoke=False, llm_skip=not mentioned, kind=kind)
                st.backoff.spoke()
                done("react")
                if mentioned:                                  # 被叫到、点了表情就算回应了，不再补一句“嗯？”
                    st.last_reply_ts = time.time()
                    return True
                return False
            if plan["act"] == "skip" and (not mentioned or may_ignore):
                if mentioned:
                    log.info("群 %s：被同一个人连着叫，这次不回", gid)
                return skip()
            cap = {"short": 1, "mid": min(2, mb), "long": mb}.get(plan["len"], mb)
            for c in plan["cands"][:max(1, int(self.gk(gid).get("candidates", 2)))]:
                # len 只是提示：模型写了两条（比如分别回两个人）就照两条发，别硬拼成一条
                bb = parse_group_reply("|||".join(c), b.persona.name, max(1, min(mb, max(cap, len(c)))))
                if bb:
                    cands.append(bb)
            sticker = plan.get("sticker") or ""
            quote = None
            no_names = self.names_off(gid)
            if tgt and tgt.get("mid") and tgt["role"] == "user":
                idx = plan["to"]
                distance = sum(1 for k, v in tmap.items() if k > idx and v["role"] == "user")
                if self.want_quote(mentioned=mentioned, target_is_trigger=bool(trigger) and tgt["mid"] == trigger,
                                   target_last=bool(tgt.get("last")), distance=distance, rand=random.random()):
                    quote = tgt["mid"]
                elif no_names and not tgt.get("last"):
                    quote = tgt["mid"]                 # 5.0 不叫名字的群：回的不是最新那条，就引用他那条（不然不知道在回谁）
            elif mentioned and trigger and self.want_quote(mentioned=True, target_is_trigger=True, target_last=True,
                                                            distance=0, rand=random.random()):
                quote = trigger
        else:
            m = REACT_RE.search(raw or "")
            if m:                                  # [回应:表情]：给最新那条（或叫她的那条）点个表情
                await self.react(trigger or self._last_user_mid(gid), m.group(1))
                raw = REACT_RE.sub("", raw)
                if not mentioned and len(TAG_RE.sub("", raw).strip()) < 2:
                    self.gate.mark(gate_id, spoke=False, llm_skip=True)
                    done("react")
                    return False
            bb = parse_group_reply(raw, b.persona.name, mb)
            if bb:
                cands = [bb]
        if self.names_off(gid):     # 5.0 不叫名字的群：[@名字] 也不用，靠引用
            cands = [c2 for c2 in ([b2 for b2 in (AT_RE.sub("", x).strip() for x in c) if b2] for c in cands) if c2]
        # ---- 发之前检查：挑最像群友的那条 ----
        recent = self.recent_her(gid)
        qtxt = ""
        if self.gk(gid).get("quality_check", True):
            stale = self.stale_phrases(gid)
            no_names = self.member_names(gid) if self.names_off(gid) else []
            others_txt = " ".join(str(r["content"] or "") for r in self._rows(gid, 8) if r["role"] == "user")
            echo = ((lambda t: self.echo_of(gid, t, others_txt)) if hasattr(self, "echo_of") else None)
            avoid = self.avoid_words(gid, focus, target_uid)
            if hasattr(self, "spec_avoid"):               # 19.1 相处说明书里“别拿来开玩笑的”
                avoid += self.spec_avoid(gid, list(dict.fromkeys([*(focus or []), *([target_uid] if target_uid else [])])))
            qargs = dict(avoid=avoid, detector=self.detector,
                         ai_open=bool(getattr(b.persona, "ai_open", False)), stale=stale, no_names=no_names, echo=echo)
            sim = float(self.gk(gid).get("similar_threshold", 0.75))
            from aigroup.chat import quality as _q
            _q.OFF = set(self.gk(gid).get("rules_off") or [])           # 21.1 小屋里关掉的规则
            orig = " / ".join(cands[0]) if cands else ""
            i, score, hits = pick(cands, recent, sim, **qargs)
            rewrote = False
            rule_hits: list = []
            if hasattr(self, "hard_issues"):          # 21.0 四条硬底线：隐私、编线下、说幕后、安全
                hard = self.hard_issues(gid, cands[i] if i >= 0 else (cands[0] if cands else []), focus)
                if hard:
                    hits = list(dict.fromkeys([*hard, *hits]))
                    score = max(score, SOFT_BLOCK)
            if fix_notes:
                hits = list(dict.fromkeys([*hits, *fix_notes]))
            if cands and (i < 0 or score >= SOFT_BLOCK or fix_notes) and (mentioned or why is not None or fix_notes) and use_plan:
                # 5.0 被叫到（或者是她自己要说的）却只有毛病很大的说法：告诉她哪里不对，重写一次
                fix = await self._rewrite(messages, raw, hits, gid)
                if fix:
                    i2, s2, h2 = pick(fix, recent, sim, **qargs)
                    if hasattr(self, "hard_issues") and i2 >= 0:
                        h3 = self.hard_issues(gid, fix[i2], focus)
                        if h3:
                            s2, h2 = max(s2, SOFT_BLOCK), list(dict.fromkeys([*h3, *h2]))
                    if i2 >= 0 and (i < 0 or s2 < score or fix_notes):
                        rule_hits = list(hits)
                        cands, i, score, hits = fix, i2, s2, h2
                        fix_notes = []
                        rewrote = True
                if fix_notes and not mentioned and not must:
                    log.info("群 %s：九问自评要改、重写没成，没被叫到就不说了", gid)
                    i = -1
            if i >= 0 and hasattr(self, "hard_issues") and not mentioned and not must and self.hard_issues(gid, cands[i], focus):
                log.info("群 %s：重写以后还是碰了硬底线，不说", gid)
                i = -1
            if i >= 0 and score >= SOFT_BLOCK and not mentioned and why is None:
                log.info("群 %s 质检：%.1f %s，没被叫到，这句不说了", gid, score, "、".join(hits))
                i = -1
            if i >= 0 and hasattr(self, "too_long") and self.too_long(
                    gid, cands[i], mentioned=bool(mentioned or bid), long_ok=bool(plan and plan.get("len") == "long")):
                # 21.1 主动说却比群友长好几倍（她中位 20 字、群友 7 字）：换短的候选，没有就让她只说一半
                short = [j for j, c in enumerate(cands) if not self.too_long(gid, c, mentioned=False, long_ok=False)]
                if short:
                    i = short[0]
                elif use_plan:
                    L = self.group_len(gid)
                    fix = await self._rewrite(messages, raw, [f"太长了：群里一条一般 {L} 个字，你这句砍到一半，只留最想说的"], gid)
                    ok2 = [c for c in fix if not self.too_long(gid, c, mentioned=False, long_ok=False)]
                    if ok2:
                        cands, i = ok2, 0
                hits = list(dict.fromkeys([*hits, "太长"]))
            bubbles = cands[i] if i >= 0 else None
            qtxt = f"{score:.1f} {'、'.join(hits)}".strip()
            try:                                           # 21.1 规则登记：哪条规则命中了、最后怎么处理的
                from aigroup.chat.rules import note as _rnote
                if rewrote:
                    _rnote(self.db, gid, rule_hits or hits, "重写", orig)
                elif i < 0 and cands:
                    _rnote(self.db, gid, hits, "拦下", orig)
                elif hits:
                    _rnote(self.db, gid, hits, "罚分")
            except Exception as e:  # noqa: BLE001
                log.debug("记规则命中失败：%s", e)
            if hits:
                log.info("群 %s 质检：%s（%d 个候选，选了第 %d 个）", gid, qtxt, len(cands), i + 1)
        else:
            bubbles = [x for x in (cands[0] if cands else []) if not self.too_similar(gid, x)]
        if bubbles and web and self.gk(gid).get("number_check", True):
            bubbles = await self._check_numbers(gid, bubbles, web, messages, raw, mentioned, use_plan)
        sticker_only = bool(not bubbles and sticker and plan and plan["act"] == "say"
                            and not last_is_sticker and self.stickers.choose(gid, sticker))
        if not bubbles and not sticker_only:
            if mentioned and not may_ignore:  # 被叫到却没有能发的：给一个最短的回应
                bubbles = [random.choice(["？", "嗯？", "在呢"])]
            else:
                return skip("blocked" if cands else "skip", qtxt)
        bubbles = list(bubbles or [])
        if len(bubbles) >= 2 and self.gk(gid).get("merge_bubbles", True):     # 5.0 大多数时候一条：两条都短、或者最近老是分两条发，就合成一条
            from aigroup.chat.address import shape_bubbles
            bubbles = shape_bubbles(bubbles, self.recent_sizes(gid), random.random(),
                                    long=bool(plan and plan.get("len") == "long"))
        # ---- 偶尔打错一个字，再补一句“*正确的字” ----
        if bubbles and random.random() < float(self.gk(gid).get("typo_rate", 0)):
            j = random.randrange(len(bubbles))
            ty = make_typo(bubbles[j])
            if ty:
                bubbles[j] = ty[0]
                if random.random() < float(self.gk(gid).get("typo_fix_chance", 0)):
                    bubbles.insert(j + 1, ty[1])
        if target_uid is None and mentioned and trigger:
            row = self.db.conn.execute("SELECT user_id FROM qq_group_messages WHERE group_id=? AND qq_msg_id=? "
                                       "ORDER BY id DESC LIMIT 1", (gid, trigger)).fetchone()
            target_uid = int(row["user_id"]) if row else None
        name_to_qq = {v: int(k) for k, v in self.names(gid).items()}
        cps = float(self.gk(gid).get("typing_chars_per_sec", 6))
        sent_ids: list = []
        sent_txt: list[str] = []
        interrupted = False
        pace = float(self.gk(gid).get("pace", 1.0))
        for i, text in enumerate(bubbles):
            await asyncio.sleep((min(6.0, len(text) / max(1.0, cps)) * random.uniform(0.6, 1.1) + (0.8 if i else 0))
                                * min(1.0, pace) if pace else 0)
            if i and st.to_her_ts > start and not text.startswith("*"):
                log.info("群 %s：说到一半有人找她，剩下的先不发", gid)
                interrupted = True
                break
            if (i == 0 and not mentioned and not must and self.gk(gid).get("typing_abort", True)
                    and self._typed_over(gid, last_id)):
                # 20.0（HUMA）：她主动想说的这句还在“打字”，群里话题已经往前走了几条：这句不发了，照常看新消息
                log.info("群 %s：打字的时候群里又说了好几句，这句不发了", gid)
                self.gate.mark(gate_id, spoke=False)
                return False
            segs = to_segments(text, name_to_qq)
            if i == 0 and quote:
                segs = [reply_seg(quote)] + segs
            mid = None
            try:
                if mid is None:
                    mid = await self.ob.send_group(gid, segs)
            except Exception as e:  # noqa: BLE001
                log.warning("群消息没发出去：%s", e)
                break
            plain = AT_RE.sub(lambda m: "@" + m.group(1), text)
            self._record(gid, self.ob.self_id or 0, b.persona.name, plain, mid, role="assistant")
            sent_ids.append(mid)
            sent_txt.append(plain)
        # ---- 13.0 有人让她“发语音 / 说句话听听”：把第一句也发成语音（本地合成，固定的合成音色） ----
        vo = getattr(self, "voice", None)
        if (sent_txt and mentioned and not interrupted and vo is not None and getattr(self.ob, "can_voice", True)
                and self.gk(gid).get("voice_reply", True)):
            from aigroup.net.voice import ASK_VOICE
            vkey = f"qq_voice_{self.brain.now():%Y-%m-%d}"
            if ASK_VOICE.search(query or "") and vo.ready and int(self.db.get(vkey, 0) or 0) < vo.per_day:
                audio = await vo.synth(sent_txt[0])
                if audio:
                    from aigroup.onebot import record_seg
                    try:
                        mid = await self.ob.send_group(gid, [record_seg(audio)])
                        self._record(gid, self.ob.self_id or 0, b.persona.name, f"[语音：{sent_txt[0]}]", mid,
                                     role="assistant")
                        self.db.set(vkey, int(self.db.get(vkey, 0) or 0) + 1)
                        sent_ids.append(mid)
                    except Exception as e:  # noqa: BLE001
                        log.info("语音没发出去：%s", e)
        # ---- 表情包：跟在文字后面，一次最多一张；别人刚发表情包时不拿表情包回 ----
        if (sticker and self.gk(gid).get("stickers", True) and not last_is_sticker and not interrupted
                and (sent_ids or not bubbles)):
            pick_s = self.stickers.choose(gid, sticker)
            if pick_s:
                try:
                    await asyncio.sleep(random.uniform(0.5, 1.5) * min(1.0, pace) if pace else 0)
                    mid = await self.ob.send_group(gid, [image_seg(pick_s["file"].read_bytes(), sticker=True)])
                    self._record(gid, self.ob.self_id or 0, b.persona.name, f"[表情包:{pick_s['emotion']}]", mid,
                                 role="assistant")
                    self.stickers.used(pick_s["id"])
                    sent_ids.append(mid)
                    sent_txt.append(f"[表情包:{pick_s['emotion']}]")
                except Exception as e:  # noqa: BLE001
                    log.info("表情包没发出去：%s", e)
        if not sent_ids and mentioned and not bubbles:          # 被叫到、只想发表情包却没发出去：至少回一句
            try:
                fb = random.choice(["？", "嗯？", "在呢"])
                mid = await self.ob.send_group(gid, [text_seg(fb)])
                self._record(gid, self.ob.self_id or 0, b.persona.name, fb, mid, role="assistant")
                sent_ids.append(mid)
                sent_txt.append(fb)
            except Exception as e:  # noqa: BLE001
                log.warning("群消息没发出去：%s", e)
        if sent_ids:
            now = time.time()
            st.last_reply_ts = now
            st.replies.append(now)
            st.last_msg_ts = now
            st.backoff.spoke()
            if not mentioned:
                st.energy.spend(now)
            everyone = [int(r["user_id"]) for r in self.db.conn.execute(
                "SELECT DISTINCT user_id FROM qq_group_messages WHERE group_id=? AND role='user' AND ts>?",
                (gid, now - 1800)).fetchall()]
            st.attention.talked_to(target_uid, everyone, now)
            self.gate.mark(gate_id, spoke=True, kind=kind)
            cur = self.db.conn.execute(
                "INSERT INTO qq_reply_effect(ts, group_id, gate_id, mentioned, target_uid, her_ids, text) "
                "VALUES (?,?,?,?,?,?,?)", (now, gid, gate_id, int(mentioned), target_uid,
                                           json.dumps([x for x in sent_ids if x is not None]), " ".join(sent_txt)[:300]))
            self.db.conn.commit()
            sigs = (getattr(self, "_last_sigs", None) or {}).get(gid) if isinstance(getattr(self, "_last_sigs", None), dict) else None
            if sigs:                                        # 15.0 记下这次用了哪几条人格签名，每周看哪条老是冷场
                log_ = list(self.db.get("sig_log") or [])
                log_.append({"id": int(cur.lastrowid), "gid": gid, "sigs": sigs, "ts": now})
                self.db.set("sig_log", log_[-600:])
            self.use_thoughts(gid, " ".join(sent_txt))
            if plan and plan.get("move") and hasattr(self, "note_move"):
                self.note_move(gid, plan["move"], now)           # 21.1 接法冷却：记下这次怎么接的
            if hasattr(self, "send_ok"):
                self.send_ok(gid)
            done("say", sent_txt, qtxt)
        else:
            if hasattr(self, "send_failed"):
                self.send_failed(gid)                            # 21.1 连着发不出去：小屋 / 小管家提醒
            done("send_fail", [], qtxt)
        return bool(sent_ids)

    async def _check_numbers(self, gid: int, bubbles: list[str], web: str, messages: list[dict], raw: str,
                             mentioned: bool, use_plan: bool) -> list[str]:
        """20.1 转述走样检查：她说的数字要在简报 / 最近聊天 / 想起来的事里出现过。对不上 → 重写一次 → 还不行就删掉那几句。"""
        from aigroup.chat.quality import unsupported_claims, unsupported_numbers
        src = web + "\n" + " ".join(str(r["content"] or "") for r in self._rows(gid, 20))

        def bad_of(t: str) -> list[str]:          # 21.0 数字 + 重大说法（退役、转会、去世……）
            return unsupported_numbers(t, src) + [f"说法“{w}”" for w in unsupported_claims(t, src)]
        bad = bad_of(" ".join(bubbles))
        if not bad:
            return bubbles
        log.info("群 %s：她说的简报里没有：%s", gid, "、".join(bad))
        fixed = False
        if use_plan:
            fix = await self._rewrite(messages, raw, [f"数字对不上 / 说法没依据：{'、'.join(bad)}（简报里没有这些数和说法，照简报原样说，没有的别说）"], gid)
            for c in fix:
                if not bad_of(" ".join(c)):
                    bubbles, fixed = c, True
                    break
        if not fixed:
            keep = [x for x in bubbles if not bad_of(x)]
            bubbles = keep if keep else ([] if not mentioned else bubbles)
        nl = list(self.db.get("numcheck_log") or [])[-199:]
        nl.append({"ts": time.time(), "gid": gid, "bad": bad[:4], "fixed": fixed})
        self.db.set("numcheck_log", nl)
        return bubbles

    async def _rewrite(self, messages: list[dict], raw: str, hits: list[str], gid: int) -> list[list[str]]:
        """质检毛病很大时重写一次：把她刚写的和毛病告诉她。返回新的候选。"""
        why = "、".join(hits[:4]) or "不太对"
        tip = {"推回去": "直接给一个具体答案", "假装在场": "你人不在那，别说得像真在现场", "说了幕后的事": "别提测试、代码、服务器这些",
               "叫了名字": "这个群不叫名字，直接说",
               "今天说过": "这个意思你今天已经说过了，换个角度、换个梗，或者只回一句很短的", "碰了雷区": "别提这个"}
        extra = "；".join(v for k, v in tip.items() if any(h.startswith(k) for h in hits))
        m2 = messages + [{"role": "assistant", "content": raw or ""},
                         {"role": "user", "content": f"（系统：这句有问题：{why}。{extra}。换个说法重写，还是只输出同样格式的 JSON。）"}]
        try:
            raw2 = await self.brain.llm.chat(m2, role=str(self.gk(gid).get("route") or "chat"), json_mode=True)
        except Exception as e:  # noqa: BLE001
            log.info("重写失败：%s", e)
            return []
        plan = self.parse_plan(raw2) or {}
        mb = int(self.gk(gid)["max_bubbles"])
        out = []
        for c in plan.get("cands") or []:
            bb = parse_group_reply("|||".join(c), self.brain.persona.name, mb)
            if bb and self.names_off(gid):
                bb = [x for x in (AT_RE.sub("", y).strip() for y in bb) if x]
            if bb:
                out.append(bb)
        tlog.info("群 %s 重写：%s", gid, raw2)
        return out

    def avoid_words(self, gid: int, focus: list[int] | None, target: int | None) -> list[str]:
        """这次在聊的人画像里“别碰”的话题 → 关键词（2~6 个字的片段）。"""
        out: list[str] = []
        for uid in dict.fromkeys([*(focus or []), *([target] if target else [])]):
            p = self.person(int(uid), gid) if uid else None
            v = str(((p or {}).get("profile") or {}).get("别碰") or "")
            for w in re.split(r"[，,、；;。/\s]+|和|或者|还有", v):
                w = re.sub(r"^(他的|她的|他|她|别提|不要提|别说|不喜欢被提起|被提起)", "", w.strip())
                if 2 <= len(w) <= 6:
                    out.append(w)
        return out[:12]

    def _quiet_for_unprompted(self, gid: int, now: float) -> bool:
        """13.1 没人叫她时先不插话的两种情况：这批最新的一条已经是好一会儿以前的了（补进来的旧消息）；
        不在这个群的 unprompted_hours 里（比如深夜）。"""
        g = self.gk(gid)
        row = self.db.conn.execute("SELECT MAX(ts) t FROM qq_group_messages WHERE group_id=? AND role='user'",
                                   (gid,)).fetchone()
        stale = float(g.get("stale_batch_minutes", 30) or 0)
        if stale and row and row["t"] and now - float(row["t"]) > stale * 60:
            log.info("群 %s：最新的消息已经是 %d 分钟以前的了，不插话", gid, (now - float(row["t"])) / 60)
            return True
        hrs = str(g.get("unprompted_hours") or "").strip()
        if hrs:
            from datetime import timedelta

            from aigroup.core.schedule import parse_range
            nd = self.brain.now()
            inside = False
            for base in (nd, nd - timedelta(days=1)):               # 跨午夜的时段（08:30-00:30）
                r = parse_range(hrs, base)
                inside = inside or bool(r and r[0] <= nd < r[1])
            if not inside:
                log.debug("群 %s：不在主动开口的时段（%s），不插话", gid, hrs)
                return True
        return False

    # ---------------- 15.0 想法池：想说但时机不对的话先留着（Inner Thoughts, Liu et al. 2025） ----------------
    def hold_thought(self, gid: int, text: str, why: str = "", now: float | None = None):
        text = re.sub(r"\s+", " ", str(text or "")).strip()[:80]
        if len(text) < 4:
            return
        from aigroup.chat.plan import bigram_overlap
        now = now or time.time()
        pool = [x for x in (self.db.get(f"qq_thoughts_{gid}") or []) if now - float(x.get("ts") or 0) < 6 * 3600]
        if any(bigram_overlap(x["text"], text) >= 0.6 for x in pool):
            return
        pool.append({"ts": now, "text": text, "why": str(why or "")[:80]})
        self.db.set(f"qq_thoughts_{gid}", pool[-5:])
        log.info("群 %s：她先憋着一句：%s", gid, text)

    def held_thoughts(self, gid: int, text: str, idle: bool = False, now: float | None = None) -> list[str]:
        """和现在聊的有关（字面重合）、或者群里冷了：拿出来给她考虑；一次最多两句。"""
        from aigroup.chat.plan import bigram_overlap
        now = now or time.time()
        pool = [x for x in (self.db.get(f"qq_thoughts_{gid}") or []) if now - float(x.get("ts") or 0) < 6 * 3600]
        if not pool or not self.gk(gid).get("hold_thoughts", True):
            return []
        hit = [x for x in pool if idle or bigram_overlap(x["text"], text or "") >= 0.2]
        return [f"{x['text']}（{int((now - x['ts']) // 60)} 分钟前想到的）" for x in hit[-2:]]

    def use_thoughts(self, gid: int, said: str):
        """她说出口的话和池子里的某句很像：那句算说过了。"""
        from aigroup.chat.plan import bigram_overlap
        pool = list(self.db.get(f"qq_thoughts_{gid}") or [])
        keep = [x for x in pool if bigram_overlap(x["text"], said or "") < 0.35]
        if len(keep) != len(pool):
            self.db.set(f"qq_thoughts_{gid}", keep)

    # ---------------- 15.0 在聊的人今天怎么样（短期的心智推测） ----------------
    def people_today(self, gid: int, focus: list, k: int = 3) -> str:
        """这几个人今天说过的话里有没有“累 / 崩溃 / 忙 / 考试 / 开心”之类：给她一句提醒，免得在人家状态差的时候还去损。"""
        from aigroup.traits import FLAG_WORDS
        if not focus:
            return ""
        now = self.brain.now()
        t0 = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        busy = r"加班|开会|考试|ddl|DDL|赶|答辩|面试|出差|通勤|值班"
        happy = r"哈哈哈|太好了|开心|爽|赢了|上分|过了|拿到了|offer"
        lines = []
        for uid in [u for u in focus if isinstance(u, int) and u > 0][:k]:
            rows = self.db.conn.execute("SELECT name, content FROM qq_group_messages WHERE group_id=? AND user_id=? "
                                        "AND role='user' AND ts>? ORDER BY id DESC LIMIT 40", (gid, uid, t0)).fetchall()
            if not rows:
                continue
            txt = " ".join(str(r["content"] or "") for r in rows)
            words = []
            for name, pat in (("状态不太好", FLAG_WORDS["sad"]), ("在忙", busy), ("心情不错", happy)):
                hits = list(dict.fromkeys(re.findall(pat, txt)))[:3]
                if hits:
                    words.append(f"{name}（说过：{'、'.join(hits)}）")
            if not words:
                continue
            st = self.state_of(gid, uid, len(rows)) if hasattr(self, "state_of") else None
            if st:                                        # 16.0 便宜模型看过他今天的话：分得清玩笑和真的
                bad = st["mood"] in ("不太好", "很糟")
                if st["mood"] in ("看不出",) and not st.get("busy"):
                    continue
                desc = ("状态不太好" if bad else f"心情{st['mood']}") + ("，在忙" if st.get("busy") else "")
                lines.append(f"- {rows[0]['name']}今天：{desc}（{st.get('why') or '从他今天的话看出来的'}）")
                continue
            if self.gk(gid).get("state_judge", True) and hasattr(self, "refresh_state") and self._allow(f"state_{gid}", 20):
                self._spawn(self.refresh_state(gid, uid))     # 先按关键词说一句，模型的判断下一轮就用上
            lines.append(f"- {rows[0]['name']}今天：{'；'.join(words)}（只是关键词，可能是玩笑）")
        if not lines:
            return ""
        return "# 在聊的这几个人今天怎么样（从他们今天的话里看出来的，别直接点破）\n" + "\n".join(lines)

    def pester_count(self, gid: int, uid, now: float, minutes: float = 15) -> tuple[int, int]:
        """13.2 这个人最近 minutes 分钟里叫了她几次（@ 她 / 冲她说）、她这段时间说了几句。"""
        t0 = now - minutes * 60
        calls = int(self.db.conn.execute("SELECT COUNT(*) n FROM qq_group_messages WHERE group_id=? AND user_id=? "
                                         "AND role='user' AND to_her=1 AND ts>?", (gid, uid, t0)).fetchone()["n"])
        said = int(self.db.conn.execute("SELECT COUNT(*) n FROM qq_group_messages WHERE group_id=? AND role='assistant' "
                                        "AND ts>?", (gid, t0)).fetchone()["n"])
        return calls, said

    def her_questions(self, gid: int, now: float, minutes: float = 20, k: int = 3) -> list[str]:
        """13.2 她最近问过的问题（带问号的），提醒她别追问第二遍。"""
        rows = self.db.conn.execute("SELECT content FROM qq_group_messages WHERE group_id=? AND role='assistant' AND ts>? "
                                    "ORDER BY id DESC LIMIT 12", (gid, now - minutes * 60)).fetchall()
        out = []
        for r in rows:
            for part in re.split(r"(?<=[？?])", str(r["content"] or "")):
                part = part.strip()
                if part.endswith(("？", "?")) and 3 <= len(part) <= 40 and part not in out:
                    out.append(part)
        return out[:k]

    def _typed_over(self, gid: int, last_id: int) -> bool:
        """想好以后到真发出去之间（打字的那几秒），群友又说了 rethink_min_new 条以上，或者有人在叫她。"""
        try:
            r = self.db.conn.execute("SELECT COUNT(*) n, IFNULL(MAX(to_her), 0) h FROM qq_group_messages "
                                     "WHERE group_id=? AND role='user' AND id>?", (gid, int(last_id or 0))).fetchone()
        except Exception:  # noqa: BLE001
            return False
        return bool(r["n"] >= int(self.gk(gid).get("rethink_min_new", 2)) + 1 and not r["h"])

    def unprompted_today(self, gid: int) -> int:
        """今天她在这个群主动开口了几次（插话、冒泡、分享；被叫到的不算）。"""
        now = self.brain.now()
        t0 = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        return int(self.db.conn.execute("SELECT COUNT(*) n FROM qq_reply_effect WHERE group_id=? AND mentioned=0 AND ts>=?",
                                        (gid, t0)).fetchone()["n"])

    async def quick_judge(self, gid: int) -> str:
        """本地门控拿不准时问一下便宜模型。20.0：先写一句理由、再选一个动作（GroupGPT 的 6 类 + 等 / 追问），
        few-shot 用你在小屋判过的这个群的例子（纠正模型“爱插嘴”的偏向）。返回 say / wait / skip（出错按 skip）；
        动作和理由放在 self._judged[gid]，记进门控日志、也会变成给她的一句提示。"""
        p = self.brain.persona
        hist = self.transcript(gid, 14)
        acts = "、".join(f"{k}（{v[0]}）" for k, v in JUDGE_ACTS.items())
        shots = judge_examples(self.db, gid, self.cfg.tz)
        prompt = (f"下面是{p.name}所在的群最近的聊天（“你”是{p.name}）。最后几句之后，{p.name}要不要接一句？\n"
                  "像一个普通群友那样判断：真人在群里大多数时候是不说话的；只有这话冲你来、或者你接一句明显让聊天更好时才开口。\n"
                  f"动作只能选一个：{acts}。\n"
                  "先用一句话写理由（谁在跟谁说、你插进去会怎样），再选动作。只输出 JSON："
                  '{"why": "一句理由", "act": "动作"}\n'
                  + (f"\n你的主人以前判过的例子（照这个标准）：\n{shots}\n" if shots else "")
                  + "\n现在的聊天：\n" + hist)
        try:
            out = await self.brain.llm.chat([{"role": "user", "content": prompt}], role="gate", temperature=0.0,
                                            max_tokens=120)
        except Exception as e:  # noqa: BLE001
            log.info("问要不要说失败：%s", e)
            return "skip"
        act, why = parse_judge(out)
        if not hasattr(self, "_judged"):
            self._judged = {}
        self._judged[gid] = {"act": act, "why": why}
        verdict = JUDGE_ACTS.get(act, ("", "skip"))[1]
        log.info("群 %s：拿不准，问了一下 → %s（%s）", gid, act, why[:40])
        return verdict

    def _last_user_mid(self, gid: int):
        last = self.db.conn.execute("SELECT qq_msg_id FROM qq_group_messages WHERE group_id=? AND role='user' "
                                    "AND qq_msg_id IS NOT NULL ORDER BY id DESC LIMIT 1", (gid,)).fetchone()
        return last["qq_msg_id"] if last else None

    def turns_since_her(self, gid: int) -> int:
        r = self.db.conn.execute("SELECT MAX(id) m FROM qq_group_messages WHERE group_id=? AND role='assistant'",
                                 (gid,)).fetchone()
        last = int(r["m"] or 0)
        if not last:
            return 99
        return int(self.db.conn.execute("SELECT COUNT(*) n FROM qq_group_messages WHERE group_id=? AND role='user' AND id>?",
                                        (gid, last)).fetchone()["n"])
