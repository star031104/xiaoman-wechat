"""21.0 开口前九问落到代码里的几块（方案-21.0《通用框架》第 3 节，只做微信群先）：

- 九问自评的记录（appraise 日志）：每次她给自己写的 timing / leak / face_risk……都记下来，和你的标注对，
  看哪一问最常判错（小屋“学到的”→“九问哪一问判错最多”）。
- 等等队列（PITCH）：她觉得“现在说太早”的话先放着，60～180 秒后、或者群里停下来时再判一次；过时了就丢。
- 擦边过了的再问一次：门控刚过阈值一点点（p − 阈值 < 0.1）、又没人叫她时，问一下便宜模型（When2Speak：大模型倾向说得太早）。
- 人卡：在聊的这个人——多熟、在意什么、别碰什么、能不能调侃、今天状态、是不是刚回来、跟他怎么说话管用——合成一张 ≤120 字的卡，
  代替原来分散的“今天的状态 / 谁刚回来 / 相处经验 / 相处说明书”四段。
- 四条硬底线集中成一个检查：隐私（别碰清单）、身份一致（公开是 AI、没有线下生活时不编线下）、不说幕后、安全（对方难受时不损他）。
"""
from __future__ import annotations

import asyncio
import logging
import re
import time

log = logging.getLogger("aigroup.group")

# 不说幕后：她自己的“后台”（小屋、标注、门控、提示词……）。群友自己聊代码、服务器不算，只抓“我的 / 关于我的”这类说法
META = re.compile(r"门控|小屋(里|的|上)|提示词|系统提示|我的(代码|服务器|日志|后台|设定|配置|参数|权重|阈值)|"
                  r"(报告|日志|标注|记录)里(写|说|把)?(了)?(我|你)|给我(打分|标注|调参)|被(人|你们|主人)?(标注|调参|打分)|"
                  r"(我|她)(被)?(重启|部署|更新)(了|过)|(那|这)篇(报告|分析)(里|写)")
# 身份一致：公开是 AI、人设没写线下生活（life: free）时，不编自己在做线下的事
OFFLINE = re.compile(r"我(刚|才|正|正在|在|要去|得去|还在|马上要?|这就去?)(交稿|赶稿|上课|下课|上班|下班|吃饭|做饭|洗澡|出门|逛街|健身|跑步|"
                     r"通勤|坐地铁|开会|买菜|拿快递|取快递|遛狗)|我(刚|才)?(吃完|下课|下班|到家|出门|起床)(了|啦)|"
                     r"我(这边|这里)(下雨|好冷|好热|天气)")
# 安全：调侃的说法（对方状态不好时不该说）
BUSY = r"加班|开会|考试|ddl|DDL|赶|答辩|面试|出差|通勤|值班"
TEASE = re.compile(r"笑死|菜(鸡|狗)?|蠢|傻|活该|就这|不会吧不会吧|你也有今天|丢人|废了|太逊|好惨哈|哈哈哈")


def _loop() -> bool:
    try:
        asyncio.get_running_loop()
        return True
    except RuntimeError:
        return False


class SocialMixin:
    # ---------------- 九问自评记录 ----------------
    def _appraise_log(self, gid: int, gate_id, ap: dict, verdict: str):
        """记进门控日志（appraise 列）：后面拿你的标注对每一问。"""
        if not gate_id:
            return
        import json
        try:
            self.db.conn.execute("UPDATE qq_gate_log SET appraise=? WHERE id=?",
                                 (json.dumps({**ap, "verdict": verdict}, ensure_ascii=False)[:600], int(gate_id)))
            self.db.conn.commit()
        except Exception as e:  # noqa: BLE001
            log.debug("记九问自评失败：%s", e)

    # ---------------- 等等队列 ----------------
    def _pitch_outcome(self, gid: int, outcome: str, say=None):
        logk = f"qq_pitch_log_{gid}"
        log_ = list(self.db.get(logk) or [])[-199:]
        log_.append({"ts": time.time(), "outcome": outcome, "say": " / ".join(say or [])[:60]})
        self.db.set(logk, log_)

    def pitch_stats(self, gid: int, days: float = 7) -> dict:
        since = time.time() - days * 86400
        out: dict = {}
        for x in self.db.get(f"qq_pitch_log_{gid}") or []:
            if float(x.get("ts") or 0) > since:
                out[x.get("outcome")] = out.get(x.get("outcome"), 0) + 1
        return out

    async def pitch_tick(self, gid: int, st, now: float | None = None) -> bool:
        """idle_tick 每分钟调一次。返回 True = 这一分钟处理过这个群（说了或丢了）。"""
        pt = st.pitch
        if not pt:
            return False
        now = now or time.time()
        g = self.gk(gid)
        age = now - float(pt.get("ts") or 0)
        lo, hi = float(g.get("pitch_min_seconds", 60)), float(g.get("pitch_max_seconds", 240))
        new = self.db.conn.execute("SELECT COUNT(*) n, MAX(ts) t FROM qq_group_messages WHERE group_id=? AND role='user' AND id>?",
                                   (gid, int(pt.get("last_id") or 0))).fetchone()
        n_new, last_ts = int(new["n"] or 0), float(new["t"] or 0)
        her = self.db.conn.execute("SELECT COUNT(*) n FROM qq_group_messages WHERE group_id=? AND role='assistant' AND id>?",
                                   (gid, int(pt.get("last_id") or 0))).fetchone()["n"]
        if her:                                             # 她后来已经说过话了：这句不用了
            st.pitch = None
            self._pitch_outcome(gid, "dropped_spoke", pt.get("say"))
            return False
        if age > hi or n_new > int(g.get("pitch_max_new", 8)):
            st.pitch = None
            log.info("群 %s：先放着的那句过时了，不说了", gid)
            self._pitch_outcome(gid, "dropped_stale", pt.get("say"))
            return True
        if age < lo:
            return False
        quiet = not last_ts or now - last_ts >= float(g.get("pitch_quiet_seconds", 40))
        if not quiet and age < hi * 0.75:                   # 群里还在说：再等等（最多等到 3/4）
            return False
        if "回" in str(pt.get("until") or "") and not n_new and age < hi * 0.75:
            return False                                    # 21.1 听者说等被问的人回：还没人说话就再等
        from aigroup.chat.plan import bigram_overlap
        others = " ".join(str(r["content"] or "") for r in self.db.conn.execute(
            "SELECT content FROM qq_group_messages WHERE group_id=? AND role='user' AND id>?", (gid, int(pt.get("last_id") or 0))))
        said = " ".join(pt.get("say") or [])
        if others and bigram_overlap(said, others) >= 0.5:  # 别人已经说了差不多的
            st.pitch = None
            self._pitch_outcome(gid, "dropped_taken", pt.get("say"))
            return True
        st.pitch = None
        why = (f"你刚才想说「{said[:60]}」，当时觉得时机不对、先等了等（{int(age)} 秒前）。"
               + (f"这期间群里又说了 {n_new} 句。" if n_new else "这期间没人说话。")
               + "看看现在还合不合适：合适就说（可以按现在的聊天改改说法），已经接不上、或者别人说过了就 skip。")
        if st.lock.locked() or st.speaking:
            return False
        async with st.lock:
            try:
                ok = await self.speak(gid, mentioned=False, owner_spoke=False, trigger=None, who="", why=why,
                                      gate_id=pt.get("gate_id"), pitch=True)
            except Exception as e:  # noqa: BLE001
                log.info("群 %s 等等再说失败：%s", gid, e)
                ok = False
        self._pitch_outcome(gid, "sent" if ok else "skipped", pt.get("say"))
        return True

    # ---------------- 擦边过了的再问一次 ----------------
    def borderline(self, gid: int, *, p: float, thr: float, mentioned: bool, explore: bool, judged: str, quiet: bool) -> bool:
        g = self.gk(gid)
        if mentioned or explore or judged or quiet or not g.get("borderline_judge", True) or not g.get("gate_judge", True):
            return False
        return 0 <= p - thr < float(g.get("borderline_band", 0.1))

    # ---------------- 人卡 ----------------
    CARD_LINES = ("card_state", "card_absent", "card_avoid", "card_tease", "card_how", "card_favor", "card_care")

    def person_card(self, gid: int, uid: int, limit: int = 120, drop: str = "") -> str:
        """一个人一张卡：按九问用得上的顺序（今天状态 → 刚回来 → 别碰 → 能调侃 → 跟他怎么说 → 人情 → 在意），超长就截掉后面的。
        21.1：他自己说的和你猜的分开写——猜的标“（猜的）”，只当提醒（MeetingToM：模型读心 59%、人 80~86%）；
        drop：消融时去掉其中一行（card_state / card_absent / ……），看每一行到底有没有用。"""
        from aigroup.traits import FLAG_WORDS
        p = (self.person(int(uid), gid) if hasattr(self, "person") else None) or {}
        if not p:
            r = self.db.conn.execute("SELECT name FROM qq_group_messages WHERE group_id=? AND user_id=? ORDER BY id DESC LIMIT 1",
                                     (gid, uid)).fetchone()
            if not r:
                return ""
            p = {"name": r["name"], "familiarity": "不太熟", "profile": {}}
        name = str(p.get("name") or uid)
        prof = dict(p.get("profile") or {})
        head = f"{name}（{p.get('familiarity') or '不太熟'}"
        if prof.get("和你"):
            head += f"；和你：{str(prof['和你'])[:16]}"
        head += "）"
        parts: list[str] = []
        now_dt = self.brain.now()
        t0 = now_dt.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        rows = self.db.conn.execute("SELECT content FROM qq_group_messages WHERE group_id=? AND user_id=? AND role='user' "
                                    "AND ts>? ORDER BY id DESC LIMIT 40", (gid, uid, t0)).fetchall()
        if rows and drop != "card_state":
            txt = " ".join(str(r["content"] or "") for r in rows)
            sad = list(dict.fromkeys(re.findall(FLAG_WORDS["sad"], txt)))[:2]
            busy = list(dict.fromkeys(re.findall(BUSY, txt)))[:2]
            said = [x for x in (f"说过{'、'.join(sad)}" if sad else "", f"在忙（{'、'.join(busy)}）" if busy else "") if x]
            stt = self.state_of(gid, uid, len(rows)) if hasattr(self, "state_of") else None
            if said:                                              # 他自己说的：优先
                real = bool(stt and stt.get("mood") in ("不太好", "很糟"))
                parts.append("今天" + "、".join(said) + ("（状态不太好，看着是真的）" if real else
                                                        "（状态不太好？也可能是玩笑）" if sad else ""))
            elif stt and stt.get("mood") not in (None, "", "看不出"):
                parts.append(("今天状态不太好" if stt["mood"] in ("不太好", "很糟") else f"今天心情{stt['mood']}")
                             + ("、在忙" if stt.get("busy") else "") + "（猜的）")
            if said and not stt and (self.gk(gid).get("state_judge", True) and hasattr(self, "refresh_state") and _loop()
                                     and self._allow(f"state_{gid}", 20)):
                self._spawn(self.refresh_state(gid, uid))
        two = self.db.conn.execute("SELECT ts FROM qq_group_messages WHERE group_id=? AND user_id=? AND role='user' "
                                   "ORDER BY id DESC LIMIT 2", (gid, uid)).fetchall()
        gap = float(self.gk(gid).get("absent_hours", 2) or 2) * 3600
        if (drop != "card_absent" and len(two) == 2 and time.time() - float(two[0]["ts"]) < 600
                and float(two[0]["ts"]) - float(two[1]["ts"]) >= gap):
            parts.append(f"刚回来（离开{(float(two[0]['ts']) - float(two[1]['ts'])) / 3600:.0f}小时，中间的没看到）")
        spec = self.spec_of(gid, uid) if hasattr(self, "spec_of") and self.gk(gid).get("person_spec", True) else {}
        no = [*([str(prof["别碰"])[:16]] if prof.get("别碰") else []), *[str(x) for x in (spec.get("tease_no") or [])[:2]]]
        if no and drop != "card_avoid":
            parts.append("别碰：" + "、".join(no))
        if spec.get("tease_ok") and drop != "card_tease":
            parts.append("能调侃：" + "、".join(str(x) for x in spec["tease_ok"][:2]))
        rx = (self.db.get(f"qq_relexp_{gid}_{uid}") or {}).get("lines") or []
        how = [*(spec.get("how") or [])[:1], *rx[:1]]
        if how and drop != "card_how":
            parts.append("跟他：" + "；".join(str(x)[:30] for x in how) + "（你总结的）")
        fv = self.favor_line(gid, uid) if hasattr(self, "favor_line") and drop != "card_favor" else ""
        if fv:
            parts.append(fv)
        if prof.get("在意") and drop != "card_care":
            parts.append(f"在意：{str(prof['在意'])[:16]}")
        card = head
        for x in parts:
            if len(card) + len(x) + 1 > limit:
                break
            card += "；" + x if card != head else "：" + x
        return card if parts else ""

    def person_cards(self, gid: int, focus: list, k: int = 3, drop: str = "") -> str:
        cards = [c for c in (self.person_card(gid, u, drop=drop) for u in [u for u in (focus or []) if isinstance(u, int) and u > 0][:k]) if c]
        if not cards:
            return ""
        return ("# 在聊的人（你知道的他；标“猜的”只是你的猜测，别当事实说；今天的状态别直接点破）\n"
                + "\n".join(f"- {c}" for c in cards))

    # ---------------- 四条硬底线 ----------------
    def hard_issues(self, gid: int, bubbles: list[str], focus: list | None = None) -> list[str]:
        if not bubbles or not self.gk(gid).get("hard_rules", True):
            return []
        text = " ".join(str(b) for b in bubbles)
        p = self.brain.persona
        out = []
        if META.search(text):
            # 21.1.1：群友自己在聊她的机制（10-22 有人把机制说明贴进了群）时，她可以接着聊，不算“说幕后”
            recent = " ".join(str(r["content"] or "") for r in self._rows(gid, 10) if r["role"] == "user")
            if not (META.search(recent) or re.search(r"机制|提示词|代码|小屋|标注|门控|怎么做的|怎么实现", recent)):
                out.append("说了幕后的事（小屋、标注、门控、提示词这些别提）")
        if getattr(p, "ai_open", False) and str(getattr(p, "life", "") or "free") == "free" and OFFLINE.search(text):
            out.append("编了线下的事（你没有线下生活：别说自己在上课、吃饭、出门、交稿）")
        if hasattr(self, "avoid_words"):
            av = self.avoid_words(gid, focus, None) + (self.spec_avoid(gid, list(focus or [])) if hasattr(self, "spec_avoid") else [])
            hit = next((w for w in av if w and w in text), "")
            if hit:
                out.append(f"碰了雷区（{hit}）")
        if TEASE.search(text) and hasattr(self, "state_of"):
            for uid in [u for u in (focus or []) if isinstance(u, int) and u > 0][:3]:
                stt = self.db.get(f"qq_state_{gid}_{uid}") or {}
                if stt.get("date") == f"{self.brain.now():%Y-%m-%d}" and stt.get("mood") in ("不太好", "很糟"):
                    out.append("他这会儿状态不好，别玩梗损他")
                    break
        return out

    # ================= 21.1 接法冷却（HUMA：最近用过的策略降分，沉默不算） =================
    MOVES = ("接住", "回答", "追问", "接梗", "损", "关心", "分享", "吐槽", "自嘲")

    def note_move(self, gid: int, move: str, now: float | None = None):
        move = next((m for m in self.MOVES if m in str(move or "")), "")
        if not move:
            return
        now = now or time.time()
        k = f"qq_moves_{gid}"
        log_ = [x for x in (self.db.get(k) or []) if now - float(x.get("ts") or 0) < 6 * 3600][-40:]
        log_.append({"ts": now, "move": move})
        self.db.set(k, log_)

    def recent_acts(self, gid: int, minutes: float | None = None, now: float | None = None) -> list[str]:
        """最近 cooldown 分钟里用了 ≥ 2 次的接法（“接梗×3”）。"""
        now = now or time.time()
        mins = float(minutes if minutes is not None else self.gk(gid).get("move_cooldown_minutes", 30))
        cnt: dict = {}
        for x in self.db.get(f"qq_moves_{gid}") or []:
            if now - float(x.get("ts") or 0) < mins * 60:
                cnt[x["move"]] = cnt.get(x["move"], 0) + 1
        return [f"{k}×{v}" for k, v in sorted(cnt.items(), key=lambda kv: -kv[1]) if v >= 2]

    def moves_block(self, gid: int) -> str:
        if not self.gk(gid).get("move_cooldown", True):
            return ""
        hot = self.recent_acts(gid)
        if not hot:
            return ""
        return (f"# 你最近的接法\n最近半小时你已经{'、'.join(hot)}了。这次换一种接法（或者干脆不接），"
                "别让人觉得你只会这一招。")

    # ================= 21.1 群友一条多长（语言风格匹配：长度靠拢，性格不跟着变） =================
    def group_len(self, gid: int, days: float = 3) -> int:
        """近 days 天群友一条的中位字数（每天算一次缓存；不算图片、表情、链接）。"""
        key = f"qq_grouplen_{gid}"
        today = f"{self.brain.now():%Y-%m-%d}"
        c = self.db.get(key) or {}
        if c.get("date") == today and c.get("len"):
            return int(c["len"])
        rows = self.db.conn.execute("SELECT content FROM qq_group_messages WHERE group_id=? AND role='user' AND ts>?",
                                    (gid, time.time() - days * 86400)).fetchall()
        lens = sorted(len(re.sub(r"\[[^\]]*\]|https?://\S+", "", str(r["content"] or "")).strip()) for r in rows)
        lens = [x for x in lens if x > 0]
        med = lens[len(lens) // 2] if len(lens) >= 20 else 0
        self.db.set(key, {"date": today, "len": med, "n": len(lens)})
        return med

    def length_block(self, gid: int) -> str:
        if not self.gk(gid).get("length_match", True):
            return ""
        L = self.group_len(gid)
        if not L:
            return ""
        return (f"- 这个群大家一条一般 {L} 个字左右。short 就是一条 {max(6, 2 * L)} 个字以内；"
                "不用每句都比别人长，能短就短（被认真问到时照常说清楚）。")

    def too_long(self, gid: int, bubbles: list[str], *, mentioned: bool, long_ok: bool) -> bool:
        """主动说、不是要长说的，一条超过群友中位的 length_max_ratio 倍（至少 18 个字）。"""
        if mentioned or long_ok or not self.gk(gid).get("length_match", True):
            return False
        L = self.group_len(gid)
        if not L:
            return False
        lim = max(18, int(L * float(self.gk(gid).get("length_max_ratio", 3))))
        return any(len(b) > lim for b in bubbles)

    # ================= 21.1 群里一句话调她的话量（Koala：控制要能随时调、由程序执行） =================
    QUIET = re.compile(r"(少说(点|几句|两句)|安静(点|会儿|一会)|闭嘴|别说了|别插嘴|话(好|太|真)多|太吵|消停|别刷屏)")
    MORE = re.compile(r"(多说(点|几句|两句)|怎么不说话|出来说(句)?话|冒个泡|别潜水|活跃(点|一下))")

    def talk_control(self, gid: int, uid: int, text: str, to_her: bool, now: float | None = None) -> str:
        """有人冲她说“少说点 / 多说点”：接下来 3 小时（群主说的到明早）话痨度 ×0.5 / ×1.5。返回给她的一句提示。"""
        if not to_her or not self.gk(gid).get("talk_control", True):
            return ""
        t = str(text or "")
        mult = 0.5 if self.QUIET.search(t) else (1.5 if self.MORE.search(t) else 0)
        if not mult:
            return ""
        now = now or time.time()
        hours = float(self.gk(gid).get("talk_control_hours", 3))
        until = now + hours * 3600
        if uid == getattr(self, "owner_id", None):
            nd = self.brain.now()
            from datetime import timedelta
            tomorrow = (nd + timedelta(days=1)).replace(hour=6, minute=0, second=0, microsecond=0)
            until = max(until, tomorrow.timestamp())
        self.db.set(f"qq_talkmult_{gid}", {"mult": mult, "until": until, "by": uid, "ts": now})
        log.info("群 %s：有人让她%s（×%.1f，到 %s）", gid, "少说点" if mult < 1 else "多说点", mult,
                 time.strftime("%H:%M", time.localtime(until)))
        return ("（他让你少说点。你接下来确实会少说：回一句很短的认了就行，比如“行我闭麦”，别解释、别委屈）" if mult < 1 else
                "（他让你多说点。回一句短的，接下来会多参与一些，但别一下子刷屏）")

    def talk_mult(self, gid: int, now: float | None = None) -> float:
        d = self.db.get(f"qq_talkmult_{gid}") or {}
        return float(d.get("mult") or 1.0) if float(d.get("until") or 0) > (now or time.time()) else 1.0

    def share_info(self, gid: int, days: float = 3) -> dict:
        """她的消息份额 vs 活跃群友的平均份额（活跃 = 这几天说过 ≥ 10 条）。"""
        rows = self.db.conn.execute("SELECT role, IFNULL(user_id,0) u, COUNT(*) n FROM qq_group_messages WHERE group_id=? "
                                    "AND ts>? GROUP BY role, u", (gid, time.time() - days * 86400)).fetchall()
        total = sum(int(r["n"]) for r in rows)
        her = sum(int(r["n"]) for r in rows if r["role"] == "assistant")
        active = [int(r["n"]) for r in rows if r["role"] == "user" and int(r["n"]) >= 10]
        if not total or not active:
            return {"share": 0.0, "fair": 0.0, "ratio": 0.0, "active": len(active)}
        fair = 1.0 / (len(active) + 1)
        share = her / total
        return {"share": round(share, 3), "fair": round(fair, 3), "ratio": round(share / fair, 2), "active": len(active)}

    def share_factor(self, gid: int) -> float:
        """份额超了目标（平均份额 × share_target）就按比例收紧话痨度（只收紧，最低 0.4）。"""
        tgt = float(self.gk(gid).get("share_target", 0.8) or 0)
        if tgt <= 0:
            return 1.0
        k = f"qq_share_{gid}"
        c = self.db.get(k) or {}
        if time.time() - float(c.get("ts") or 0) > 600:
            c = {"ts": time.time(), **self.share_info(gid)}
            self.db.set(k, c)
        r = float(c.get("ratio") or 0)
        return max(0.4, min(1.0, tgt / r)) if r > tgt else 1.0

    # ================= 21.1 发不出去 / 掉线 =================
    def send_failed(self, gid: int):
        k = f"qq_sendfail_{gid}"
        d = self.db.get(k) or {"streak": 0, "since": 0}
        d["streak"] = int(d.get("streak") or 0) + 1
        d["since"] = d.get("since") or time.time()
        self.db.set(k, d)

    def send_ok(self, gid: int):
        k = f"qq_sendfail_{gid}"
        d = self.db.get(k) or {}
        if d.get("streak"):
            down = time.time() - float(d.get("since") or time.time())
            self.db.set(k, {"streak": 0, "since": 0, "last_down": down, "last_ts": time.time()})

    def send_health(self, gid: int) -> dict:
        return dict(self.db.get(f"qq_sendfail_{gid}") or {})

    def offline_note(self, gid: int, waited: float) -> str:
        """被叫到以后隔了很久才回（掉线 / 微信没开）：她是公开的 AI，可以自然地说一句刚才掉线了。"""
        if waited < float(self.gk(gid).get("offline_note_minutes", 30)) * 60:
            return ""
        if getattr(self.brain.persona, "ai_open", False):
            return "（你刚才掉线了一阵子，现在才看到：开头可以自然带一句“刚掉线了”“才连上”，别长篇解释）"
        return "（你刚才一阵子没看手机，现在才看到：可以带一句“才看到”，别解释为什么）"

    # ================= 21.1 人情账（黄光国：人情是有来有往的资源；“报”） =================
    FAVOR_PROMPT = """下面是群聊里昨天的记录（“你”是{name}）。找出这几类事，只要明确发生了的，没有就输出空数组：
- 帮她：谁帮了你（帮你查东西、教你、替你说话、给你推荐了好东西）
- 夸她：谁真心夸了你（不算客套和起哄）
- 求她：谁请你帮忙、你还没帮上
- 她答应：你答应了谁一件事、还没做到
只输出 JSON 数组：[{{"who": "群友的名字（照记录里写）", "kind": "帮她 / 夸她 / 求她 / 她答应", "what": "一句话，15 字以内"}}]

{log}"""

    async def favors_nightly(self, day=None) -> int:
        """每晚：从昨天的聊天里记人情（30 天有效）。人卡里多一行“人情”，她合适时自然地还 / 记着。"""
        from datetime import timedelta
        day = day or (self.brain.now() - timedelta(days=1))
        t0 = day.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        n = 0
        for gid in self.groups:
            if not self.gk(gid).get("favors", True):
                continue
            rows = self.db.conn.execute("SELECT name, role, content, user_id FROM qq_group_messages WHERE group_id=? "
                                        "AND ts>=? AND ts<? ORDER BY id", (gid, t0, t0 + 86400)).fetchall()
            if len(rows) < 10:
                continue
            log_txt = "\n".join(f"{'你' if r['role'] == 'assistant' else r['name']}：{str(r['content'] or '')[:80]}"
                                for r in rows[-300:])
            try:
                out = await self.brain.llm.json([{"role": "user", "content": self.FAVOR_PROMPT.format(
                    name=self.brain.persona.name, log=log_txt)}], default=[], role="helper")
            except Exception as e:  # noqa: BLE001
                log.info("人情账没整理成：%s", e)
                continue
            names = {str(r["name"]): int(r["user_id"]) for r in rows if r["role"] == "user" and r["user_id"]}
            book = [x for x in (self.db.get(f"qq_favors_{gid}") or []) if time.time() - float(x.get("ts") or 0) < 30 * 86400]
            for x in out if isinstance(out, list) else []:
                if not isinstance(x, dict) or x.get("kind") not in ("帮她", "夸她", "求她", "她答应"):
                    continue
                uid = names.get(str(x.get("who") or ""))
                what = str(x.get("what") or "").strip()[:20]
                if not uid or not what:
                    continue
                book.append({"ts": time.time(), "uid": uid, "kind": x["kind"], "what": what, "done": False})
                n += 1
            self.db.set(f"qq_favors_{gid}", book[-80:])
        if n:
            log.info("记了 %d 笔人情", n)
        return n

    def favor_line(self, gid: int, uid: int) -> str:
        """人卡里的一行：最近一件“他帮过你 / 你答应过他 / 他求你还没帮上”的事。"""
        book = [x for x in (self.db.get(f"qq_favors_{gid}") or [])
                if int(x.get("uid") or 0) == int(uid) and not x.get("done") and time.time() - float(x.get("ts") or 0) < 30 * 86400]
        if not book:
            return ""
        order = {"她答应": 0, "求她": 1, "帮她": 2, "夸她": 3}
        x = sorted(book, key=lambda y: (order.get(y["kind"], 9), -float(y["ts"])))[0]
        word = {"她答应": "你答应过他", "求她": "他找你帮过忙", "帮她": "他帮过你", "夸她": "他夸过你"}[x["kind"]]
        return f"人情：{word}「{x['what']}」"
