"""收消息：QQ 推来的群消息 / 通知 → 洗干净、记进数据库 → 交给 speak 判断要不要说。

- on_event：群消息（去重、记录、心情、学表情包、看图、复读、凑一批再判断）
- on_notice：戳一戳、新人进群
- catch_up：连上 QQ 后补上她不在时群里的消息
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time
from datetime import datetime

from aigroup.core.safety import clean_member_text, clean_name
from aigroup.memory.stickers import sticker_key
from aigroup.onebot import fetch_bytes, flatten

log = logging.getLogger("aigroup.group")
tlog = logging.getLogger("aigroup.text.group")


def trim_desc(desc: str, limit: int = 180) -> str:
    """看图结果压成一行、不超过 limit 字；截的话截在句号 / 分号 / 逗号 / 引号后面，别留半个「。"""
    d = re.sub(r"\s*\n+\s*", " ", str(desc or "")).strip()
    d = re.sub(r"(^|\s)[1-4][)）.、]\s*", r"\1", d).strip()          # 去掉“1)”“2)”这种序号
    if len(d) > limit:
        cut = d[:limit]
        k = max(cut.rfind(x) for x in "。；;，,」")
        d = cut[:k + 1] if k >= limit // 2 else cut
    if d.count("「") > d.count("」"):
        d = d[:d.rfind("「")].rstrip("，, ")
    return d.replace("]", "）").replace("[", "（")


class IngestMixin:
    def _record(self, gid: int, uid: int, name: str, text: str, mid=None, role: str = "user",
                to_her: bool = False, reply_to: int | None = None, ts: float | None = None) -> int:
        now = float(ts) if ts else time.time()
        if role == "user" and self.g.get("mask_pii", True):        # 13.0 入库即脱敏（手机号、证件号、卡号、验证码、邮箱）
            from aigroup.core.safety import mask_pii
            text = mask_pii(text)
        cur = self.db.conn.execute(
            "INSERT INTO qq_group_messages(ts, group_id, user_id, name, role, content, qq_msg_id, to_her, reply_to) "
            "VALUES (?,?,?,?,?,?,?,?,?)", (now, gid, uid, name, role, text[:500], mid, int(to_her), reply_to))
        if role == "user":
            self.db.conn.execute(
                "INSERT INTO qq_people(user_id, name, msgs, last_seen, since_update) VALUES (?,?,1,?,1) "
                "ON CONFLICT(user_id) DO UPDATE SET name=excluded.name, msgs=msgs+1, last_seen=excluded.last_seen, "
                "since_update=since_update+1", (uid, name, now))
            self.db.conn.execute(
                "INSERT INTO qq_profiles(user_id, group_id, msgs, since_update, last_seen) VALUES (?,?,1,1,?) "
                "ON CONFLICT(user_id, group_id) DO UPDATE SET msgs=msgs+1, since_update=since_update+1, "
                "last_seen=excluded.last_seen", (uid, gid, now))
            row = self.db.conn.execute("SELECT hours FROM qq_people WHERE user_id=?", (uid,)).fetchone()
            try:
                hours = json.loads(row["hours"]) if row and row["hours"] else [0] * 24
            except ValueError:
                hours = [0] * 24
            hours[datetime.fromtimestamp(now, self.cfg.tz).hour] += 1
            self.db.conn.execute("UPDATE qq_people SET hours=? WHERE user_id=?", (json.dumps(hours), uid))
        self.db.conn.commit()
        return int(cur.lastrowid)

    async def on_notice(self, ev: dict):
        gid = int(ev.get("group_id") or 0)
        if gid not in self.groups:
            return
        nt, sub = ev.get("notice_type"), ev.get("sub_type")
        uid = int(ev.get("user_id") or 0)
        if nt == "notify" and sub == "poke" and self.ob.self_id and int(ev.get("target_id") or 0) == self.ob.self_id:
            if uid == self.ob.self_id:
                return
            name = self.names(gid).get(str(uid)) or str(uid)
            self._spawn(self.poke_back(gid, uid))
            await self.on_event({"post_type": "message", "message_type": "group", "group_id": gid, "user_id": uid,
                                 "sender": {"card": name}, "_poke": True,
                                 "message": [{"type": "text", "data": {"text": "[戳了戳你]"}}]})
        elif nt == "group_increase" and uid and uid != self.ob.self_id:
            if random.random() >= float(self.gk(gid).get("welcome_chance", 0)):
                return
            from aigroup.core.schedule import in_quiet
            if in_quiet(self.brain.now(), self.cfg.get("quiet_hours")):
                return
            name = str(uid)
            try:
                info = await self.ob.call("get_group_member_info", group_id=gid, user_id=uid)
                name = clean_name(str(info.get("card") or info.get("nickname") or uid))
            except Exception:  # noqa: BLE001
                pass
            self._record(gid, uid, name, "[进群了]", None)
            await asyncio.sleep(self.secs("notice_delay", gid))
            st = self._st(gid)
            async with st.lock:
                await self.speak(gid, mentioned=False, owner_spoke=False, trigger=None, who=name,
                                 why=f"新人「{name}」刚进群。随口欢迎一下（一句话，像朋友那样，别太正式），"
                                     f"可以 [@{name}]")

    async def on_event(self, ev: dict):
        if ev.get("post_type") == "notice":
            await self.on_notice(ev)
            return
        if ev.get("post_type") != "message" or ev.get("message_type") != "group":
            return
        gid = int(ev.get("group_id") or 0)
        if gid not in self.groups:
            return
        uid = int(ev.get("user_id") or 0)
        if not uid or (self.ob.self_id and uid == self.ob.self_id):
            return
        if ev.get("message_id") and self.db.conn.execute(        # 已经记过（补历史时记了，或 NapCat 重发）
                "SELECT 1 FROM qq_group_messages WHERE group_id=? AND qq_msg_id=? LIMIT 1",
                (gid, ev.get("message_id"))).fetchone():
            return
        sender = ev.get("sender") or {}
        name = clean_name((sender.get("card") or sender.get("nickname") or str(uid)).strip())
        f = flatten(ev.get("message"), self.ob.self_id, self.names(gid, members=True))
        text = clean_member_text(f.text)        # 换行、伪造的“#编号 你：”“（系统：”都洗掉
        called = self._called(text)             # 只看他自己打的字（不看引用的内容、图片描述）
        st = self._st(gid)
        if f.images and self.gk(gid).get("see_images") and self.brain.llm.vision:
            desc = await self._look(f.images[0], name, gid)
            if desc:
                text = text.replace("[图片]", f"[图片：{clean_member_text(desc, 100)}]", 1)
        if (f.stickers and self.gk(gid).get("see_stickers", True) and self.brain.llm.vision
                and re.search(r"\[(表情包|动画表情)\]", text) and self._allow(f"stk_look_{gid}", 30)):
            desc = await self._look_sticker(f.stickers[0], (f.sticker_files or [""])[0])   # 13.1 表情包是什么意思
            if desc:
                text = re.sub(r"\[(表情包|动画表情)\]", f"[表情包：{clean_member_text(desc, 30)}]", text, count=1)
        if (f.record and self.gk(gid).get("hear_voice", True) and getattr(self, "hearing", None) is not None
                and self.hearing.ready and self._allow(f"hear_{gid}", 30)):
            heard = await self._hear(f.record, getattr(f, "record_url", None))     # 13.0 听懂语音（免费的 SenseVoice）
            if heard:
                text = text.replace("[语音]", clean_member_text(heard, 160), 1)
        reply_to_her = False
        if f.reply_id:
            row = self.db.conn.execute(
                "SELECT content, role FROM qq_group_messages WHERE group_id=? AND qq_msg_id=? ORDER BY id DESC LIMIT 1",
                (gid, f.reply_id)).fetchone()
            if row:
                reply_to_her = row["role"] == "assistant"
                who = "你" if reply_to_her else "别人"
                text = f"[回复{who}的「{clean_member_text(row['content'], 30)}」] " + text
        if not text:
            return
        if f.links or "http" in text or "BV" in text:      # 群友发的链接：点开看看（最多等十几秒）
            desc = await self.read_links(text, list(f.links))
            if desc:
                text = f"{text} {desc}"
                if self.g.get("remember_web", True):
                    self.learn_link(gid, desc)
        mentioned = f.mentioned or reply_to_her or called or bool(ev.get("_poke"))
        if hasattr(self, "talk_control"):
            tn = self.talk_control(gid, uid, text, bool(mentioned) or bool(st.last_reply_ts and time.time() - st.last_reply_ts < 60))
            if tn:
                st.talk_note = tn
        if hasattr(self, "huh_signal"):
            self.huh_signal(gid, uid, clean_member_text(f.text))       # 23.1 “？”“你为什么要回答两次”：先安静会儿
        if hasattr(getattr(self, "links", None), "take_videos") and hasattr(self, "watch_video"):
            for v in self.links.take_videos():                       # 4.4 B 站视频：点开看（字幕、弹幕、热评）
                self._spawn(self.watch_video(gid, v, mentioned, name))
        meta = {"at_other": bool([q for q in f.ats if q != "all"]) and not f.mentioned,
                "reply_other": bool(f.reply_id) and not reply_to_her,
                "sticker": bool(f.stickers) and len(re.sub(r"\[[^\]]*\]", "", text).strip()) == 0}
        bid = ""
        if not mentioned and hasattr(self, "bid_from"):
            bid = self.bid_from(gid, uid, clean_member_text(f.text), at_other=meta["at_other"], reply_other=meta["reply_other"])
            if bid:                                 # 19.1 在接她刚才的话（Gottman 的“搭话”）：接住
                st.batch_guess = f"bid:{name}:{bid[:30]}"
                meta["guess"] = "bid"
                self.note_bid(gid, name, bid)
                tlog.info("群 %s：%s 在接她的话（她说的「%s」）：%s", gid, name, bid[:24], clean_member_text(f.text, 40))
        if not bid and not mentioned and self.gk(gid).get("guess_to_her", True) and hasattr(self, "member_names"):
            from aigroup.chat.address import likely_to_her
            hers = [r["content"] for r in self.db.conn.execute(
                "SELECT content FROM qq_group_messages WHERE group_id=? AND role='assistant' ORDER BY id DESC LIMIT 3",
                (gid,)).fetchall()]
            how = likely_to_her(clean_member_text(f.text), now=time.time(), her_last_ts=st.last_reply_ts,
                                attention=st.attention.get(uid), her_recent=hers, at_other=meta["at_other"],
                                reply_other=meta["reply_other"],
                                other_names=[n for n in self.member_names(gid) if n != name])
            if how == "about" and (st.batch_guess or time.time() - st.about_ts < 600):
                how = ""
            if how == "about":
                st.about_ts = time.time()
            if how:
                st.batch_guess = f"{how}:{name}"      # 5.0 没 @ 她，但多半是冲她说的 / 在说她：这一批一定看一眼
                meta["guess"] = how
                tlog.info("群 %s：%s 这句%s：%s", gid, name, "多半是在跟她说" if how == "to" else "在说她",
                         clean_member_text(f.text, 40))
        if f.mentioned:
            text = "@你 " + text
        now = time.time()
        if not st.pending or st.pending.done():
            st.batch_start_prev_ts = st.last_msg_ts
            st.batch_texts = []
        mid_row = self._record(gid, uid, name, text, ev.get("message_id"), to_her=mentioned, reply_to=f.reply_id)
        st.batch_texts.append((ev.get("message_id"), uid, text, meta))
        st.last_msg_ts = now
        if self.mood is not None:
            self.mood.on_message(gid, name, clean_member_text(f.text), mentioned)     # 用他自己打的字（不含引用的她的话）
        if mentioned:
            self.note_mention(uid)
            st.batch_mentioned = True
            st.batch_trigger = ev.get("message_id")
            st.batch_who = name
            st.batch_callers.append((name, clean_member_text(f.text, 40)))
            st.to_her_ts = now
        if uid == self.owner_id:
            st.batch_owner = True
        if f.stickers and self.gk(gid).get("stickers", True):
            self._spawn(self._collect_sticker(gid, uid, f.stickers[0], (f.sticker_files or [""])[0], mid_row))
        self._count_for_digest(gid)
        if hasattr(self, "maybe_where"):
            self.maybe_where(gid, uid, clean_member_text(f.text))     # 4.4 “我在珠海出差”：记下他在哪
        if hasattr(self, "maybe_fix"):
            self.maybe_fix(gid, uid, clean_member_text(f.text))       # 被纠正了：对一下她记的知识
        if st.speaking:
            return                                  # 她正在说：不打断，说完会接着看这批新消息
        if st.pending and not st.pending.done():
            st.pending.cancel()
        st.pending = self._spawn(self._after_wait(gid))

    async def catch_up(self):
        """连上 NapCat 后，把她不在时（程序重启、电脑关机、掉线）群里说的话补进记录：
        用 NapCat 的 get_group_msg_history 拉最近 catch_up_count 条（默认 80），比她记下的最后一条新的才补；
        不一条条去回，只是“知道了”（之后的聊天记录、画像、群梗整理都用得上）；
        这期间有人 @ / 回复 / 叫她（12 小时内）：等她“上线”后补一句“才看到”（和半夜被叫的处理一样）。"""
        if not self.g.get("catch_up", True):
            return
        for gid in sorted(self.groups):
            try:
                n = await self._catch_up_group(gid)
                if n:
                    log.info("群 %s：补上了她不在时的 %d 条消息", gid, n)
            except Exception as e:  # noqa: BLE001
                log.info("群 %s：补历史消息失败：%s", gid, e)

    async def _catch_up_group(self, gid: int) -> int:
        data = await self.ob.call("get_group_msg_history", group_id=gid, count=int(self.gk(gid).get("catch_up_count", 80)),
                                  message_seq=0, reverse_order=False)
        msgs = [m for m in ((data or {}).get("messages") or []) if isinstance(m, dict)]
        if not msgs:
            return 0
        last = self.db.conn.execute("SELECT MAX(ts) t FROM qq_group_messages WHERE group_id=?", (gid,)).fetchone()["t"] or 0
        known = {r["qq_msg_id"] for r in self.db.conn.execute(
            "SELECT qq_msg_id FROM qq_group_messages WHERE group_id=? AND qq_msg_id IS NOT NULL ORDER BY id DESC LIMIT 500",
            (gid,)).fetchall()}
        now = time.time()
        if getattr(self.ob, "history_by_id", False):
            # 微信版：历史从本地库来、消息 id 稳定，只按 id 去重（实时收到的记的是“收到的时间”，按时间比会把晚到的旧消息漏掉）
            ids = [m.get("message_id") for m in msgs if m.get("message_id") is not None]
            known = set()
            for k in range(0, len(ids), 400):
                part = ids[k:k + 400]
                known |= {r["qq_msg_id"] for r in self.db.conn.execute(
                    f"SELECT qq_msg_id FROM qq_group_messages WHERE group_id=? AND qq_msg_id IN ({','.join('?' * len(part))})",
                    (gid, *part)).fetchall()}
            last = 0
        fresh = sorted((m for m in msgs if m.get("message_id") not in known and float(m.get("time") or 0) > last),
                       key=lambda m: (float(m.get("time") or 0), int(m.get("message_seq") or m.get("message_id") or 0)))
        prev_ts = self._st(gid).last_msg_ts
        called: list[dict] = []
        live: list[tuple] = []                     # 她不在时、但就在刚才（20 分钟内）群友说的：上线后当成刚看到的一批
        n = 0
        for m in fresh:
            uid = int(m.get("user_id") or (m.get("sender") or {}).get("user_id") or 0)
            ts = float(m.get("time") or now)
            sender = m.get("sender") or {}
            if not uid:
                continue
            if self.ob.self_id and uid == self.ob.self_id:           # 她自己（比如手机上用小号说的）
                f = flatten(m.get("message"), self.ob.self_id, self.names(gid, members=True))
                if f.text:
                    self._record(gid, uid, self.brain.persona.name, clean_member_text(f.text), m.get("message_id"),
                                 role="assistant", ts=ts)
                    n += 1
                continue
            name = clean_name((sender.get("card") or sender.get("nickname") or str(uid)).strip())
            f = flatten(m.get("message"), self.ob.self_id, self.names(gid, members=True))
            text = clean_member_text(f.text)
            if not text:
                continue
            reply_to_her = False
            if f.reply_id:
                row = self.db.conn.execute("SELECT content, role FROM qq_group_messages WHERE group_id=? AND qq_msg_id=? "
                                           "ORDER BY id DESC LIMIT 1", (gid, f.reply_id)).fetchone()
                if row:
                    reply_to_her = row["role"] == "assistant"
                    text = f"[回复{'你' if reply_to_her else '别人'}的「{clean_member_text(row['content'], 30)}」] " + text
            mentioned = f.mentioned or reply_to_her or self._called(clean_member_text(f.text))
            if f.mentioned:
                text = "@你 " + text
            self._record(gid, uid, name, text, m.get("message_id"), to_her=mentioned, reply_to=f.reply_id, ts=ts)
            n += 1
            if now - ts < 20 * 60:
                live.append((m.get("message_id"), uid, text, {"at_other": False, "reply_other": False,
                                                                "sticker": text.startswith("[表情")}))
            if mentioned:
                self.note_mention(uid)
                if now - ts < 12 * 3600:
                    called.append({"who": name, "mid": m.get("message_id"), "ts": ts, "kind": "offline"})
        if n:
            key = f"qq_digest_count_{gid}"
            self.db.set(key, int(self.db.get(key, 0)) + n)
            st = self._st(gid)
            row = self.db.conn.execute("SELECT MAX(ts) t FROM qq_group_messages WHERE group_id=?", (gid,)).fetchone()
            st.last_msg_ts = max(st.last_msg_ts, float(row["t"] or 0))
        called += self._pending_mentions(gid, {c.get("mid") for c in called}, now)
        if called:
            old = self.db.get(f"qq_night_{gid}") or []
            old = [old] if isinstance(old, dict) else list(old)
            self.db.set(f"qq_night_{gid}", (old + called)[-6:])
            log.info("群 %s：她不在的时候 %s 叫过她，等会儿补一句", gid, "、".join(dict.fromkeys(c["who"] for c in called)))
        self._after_catch_up(gid, bool(called), live, prev_ts)
        return n

    def _pending_mentions(self, gid: int, have: set, now: float) -> list[dict]:
        """5.0 重启前有人 @ 她（已经记进库了）、她还没来得及回，程序就重启了：这些补进的消息里没有，
        要从库里找——她最后一句话之后、pending_reply_minutes 分钟以内、这次启动以前收到的叫她的话。每条只补一次。"""
        mins = float(self.gk(gid).get("pending_reply_minutes", 20) or 0)
        boot = float(getattr(self, "_boot_ts", 0) or 0)
        if mins <= 0 or not boot:
            return []
        last_her = self.db.conn.execute("SELECT COALESCE(MAX(id), 0) m FROM qq_group_messages WHERE group_id=? "
                                        "AND role='assistant'", (gid,)).fetchone()["m"]
        rows = self.db.conn.execute(
            "SELECT qq_msg_id, name, ts FROM qq_group_messages WHERE group_id=? AND role='user' AND to_her=1 AND id>? "
            "AND ts>? AND ts<? ORDER BY id", (gid, last_her, now - mins * 60, boot)).fetchall()
        done = list(self.db.get(f"qq_pending_done_{gid}") or [])
        out = [{"who": r["name"], "mid": r["qq_msg_id"], "ts": r["ts"], "kind": "pending"} for r in rows
               if r["qq_msg_id"] not in have and str(r["qq_msg_id"]) not in done]
        if out:
            self.db.set(f"qq_pending_done_{gid}", (done + [str(x["mid"]) for x in out])[-40:])
            log.info("群 %s：重启前 %s 叫她还没回，等会儿回", gid, "、".join(dict.fromkeys(x["who"] for x in out)))
        return out

    def _after_catch_up(self, gid: int, called: bool, live: list[tuple], prev_ts: float = 0.0):
        """补完消息以后：有人叫过她 → 过一会儿（像刚看到手机）就回，不用等下一轮冒泡检查；
        没人叫她、但群里就在刚才还在聊 → 当成她刚刷到这一批，照平常一样判断要不要插一句。"""
        from aigroup.core.schedule import in_quiet
        if self.muted(gid) or in_quiet(self.brain.now(), self.cfg.get("quiet_hours")):
            return
        st = self._st(gid)
        if st.speaking or (st.pending and not st.pending.done()):
            return
        if called:
            async def reply_soon():
                await asyncio.sleep(self.human_delay(gid, "read_delay", 1.0))
                if not st.lock.locked():
                    await self._night_catch_up(gid, st, time.time(), wait=120)
            st.pending = self._spawn(reply_soon())
        elif live:
            st.batch_start_prev_ts = prev_ts
            st.batch_texts = live[-12:]
            st.pending = self._spawn(self._after_wait(gid))

    async def _collect_sticker(self, gid: int, uid: int, url: str, file: str, row_id: int):
        key = sticker_key(url, file)
        ctx = [f"{'她' if r['role'] == 'assistant' else r['name']}：{clean_member_text(r['content'], 40)}"
               for r in self._rows(gid, 6) if r["id"] <= row_id][-5:]
        talkers = self.db.conn.execute("SELECT COUNT(DISTINCT user_id) n FROM qq_group_messages WHERE group_id=? AND role='user' "
                                       "AND ts>?", (gid, time.time() - 7 * 86400)).fetchone()["n"]
        rec = self.stickers.seen(gid, uid, key, url, ctx, small=int(talkers or 0) <= 3)
        if rec and rec.get("need_fetch"):
            data = await fetch_bytes(url, limit=2 * 1024 * 1024)
            if data and self.stickers.save_file(key, data):
                log.info("存了一张群里常用的表情包（%s）", key[:8])
            else:                                          # 24.0 下载失败要说出来（以前悄悄失败，一张都没存下来也没人知道）
                log.info("表情包（%s）没下载下来：%s", key[:8], "空的" if not data else "不是完整的图")

    async def _hear(self, file: str | None, url: str | None) -> str:
        from aigroup.net.voice import voice_text, wav_seconds
        try:
            data = await self.hearing.fetch_qq(self.ob, file, url)
            if not data:
                return ""
            txt, tags = await self.hearing.hear(data)
        except Exception as e:  # noqa: BLE001
            log.info("语音没听清：%s", e)
            return ""
        return voice_text(txt, tags, wav_seconds(data))

    async def _look_sticker(self, url: str, file: str = "") -> str:
        """13.1 表情包：一句话说它在表达什么（同一张只看一次，按文件名 / 内容缓存）。"""
        import base64
        import hashlib
        key = "qq_stkdesc_" + (re.sub(r"[^0-9a-zA-Z]", "", file)[:32] or hashlib.md5(url.encode()).hexdigest()[:16])
        old = self.db.get(key)
        if old:
            return str(old)
        data = await fetch_bytes(url, limit=2 * 1024 * 1024)
        if not data:
            return ""
        if data[:3] == b"GIF":                                   # 动图：取第一帧（看图接口不一定认 GIF）
            try:
                import io

                from PIL import Image
                b = io.BytesIO()
                Image.open(io.BytesIO(data)).convert("RGB").save(b, "PNG")
                data = b.getvalue()
            except Exception:  # noqa: BLE001
                pass
        try:
            desc = (await self.brain.llm.describe_image(
                base64.b64encode(data).decode(), "",
                prompt="这是群聊里的一张表情包。用 12 个字以内说它在表达什么情绪 / 意思；图上有字就把字原样写进去。"
                       "只输出这一句。"))[:30]
        except Exception as e:  # noqa: BLE001
            log.info("表情包没看懂：%s", e)
            return ""
        desc = desc.strip(" 。.\n")
        if desc:
            self.db.set(key, desc)
        return desc

    async def _look(self, url: str, who: str = "群友", gid: int | None = None) -> str:
        if str(url).startswith("wximg:") and hasattr(self.ob, "fetch_image"):      # 13.1 微信本地的图（30.0 没下完的不算）
            data = await self.ob.fetch_image(url)
        else:
            data = await fetch_bytes(url)
        if not data:
            return ""
        import base64
        import hashlib

        from aigroup.llm.client import GROUP_IMAGE_PROMPT
        key = "qq_img_" + hashlib.md5(data).hexdigest()[:16]            # 同一张图（转发、复读）只看一次
        old = self.db.get(key)
        if old:
            return str(old)
        if len(data) > 1_200_000:                               # 13.1 太大的图先缩一下（看图模型反正会缩，省流量）
            try:
                import io

                from PIL import Image
                im = Image.open(io.BytesIO(data)).convert("RGB")
                im.thumbnail((1600, 1600))
                b = io.BytesIO()
                im.save(b, "JPEG", quality=85)
                data = b.getvalue()
            except Exception:  # noqa: BLE001
                pass
        names = "、".join(self.member_names(gid)[:12]) if gid is not None and hasattr(self, "member_names") else ""
        try:
            desc = (await self.brain.llm.describe_image(
                base64.b64encode(data).decode(), "",
                prompt=GROUP_IMAGE_PROMPT.format(who=who, names=names or "（不知道）")))
            desc = trim_desc(desc, 180)          # 22.2 以前硬截 100 字，会截在引号中间（“電話「5478”）
            if desc:
                self.db.set(key, desc)
            return desc
        except Exception as e:  # noqa: BLE001
            log.info("群图片没看清：%s", e)
            return ""
