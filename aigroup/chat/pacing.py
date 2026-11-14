"""节奏：攒一批再回、说多了收着、看群里直接发的视频、群友查 / 删自己的记录（原 v35.py）。

35.0 几样小机制（都能在小屋里单独关）：

- 时机：攒一批再回（MaiBot natural 档的思路）——被叫到 1 秒、平时 2 秒、群里正热闹 5 秒，一批从第一条算起最多等 8 秒
  （以前固定 2 秒、每来一条都重新计时，一直有人说话就一直不回）
- 此刻：她这一小时说得太多（占比超过 share_hint_ratio）就在提示词里提醒多听（真人群聊里一个人很少占到四分之一）
- 视频：群友直接发的视频（微信）——有 mp4 就均匀截 4 帧拼一张 2×2 看；只有封面就看封面
- 群友的记录：/我的记录 看她记了自己多少东西；/删除我的记录 确认 → 删掉自己的聊天记录、画像、记的事
  （参考《人工智能拟人化互动服务管理暂行办法》第 16 条：交互数据可复制、可删除；小屋里管理员还能导出）
"""
from __future__ import annotations

import asyncio
import base64
import logging
import re
import shutil
import time

log = logging.getLogger("aigroup.pacing")

MY_RX = re.compile(r"^\s*[/／]\s*我的记录\s*$")
DEL_RX = re.compile(r"^\s*[/／]\s*删除我的记录\s*(确认)?\s*$")

VIDEO_PROMPT = ("这是群友{who}在群聊里发的一段视频的{what}。用一两句话说清视频里大概是什么（场景、人在干什么、有没有字幕 / 图上的字），"
                "60 字以内；看不清的别猜，别写人名。只输出这段描述。")


class PacingMixin:
    # ---------------- 时机：攒一批再回 ----------------
    def merge_wait(self, gid: int) -> float:
        g = self.gk(gid)
        base = float(g.get("merge_wait_seconds", 2) or 0)
        if not g.get("merge_adaptive", True) or base <= 0:          # merge_wait_seconds = 0：不等（测试、演练）
            return base
        st = self._st(gid)
        now = time.time()
        if st.batch_mentioned:
            w = float(g.get("merge_wait_mention", 1))
        else:
            lively = self.db.conn.execute(
                "SELECT COUNT(*) n FROM qq_group_messages WHERE group_id=? AND role='user' AND ts>?",
                (gid, now - 30)).fetchone()["n"]
            w = float(g.get("merge_wait_busy", 5)) if lively >= 4 else base
        first = float(getattr(st, "batch_first_ts", 0) or 0) or now
        left = first + float(g.get("merge_wait_max", 8)) - now      # 一批最多等这么久：一直有人说也得看一眼
        return max(0.3, min(w, left))

    # ---------------- 此刻：说多了提醒多听 ----------------
    def v35_state(self, gid: int, *, mentioned: bool) -> list[str]:
        g = self.gk(gid)
        ratio = float(g.get("share_hint_ratio", 0.25) or 0)
        if not ratio:
            return []
        rows = self.db.conn.execute("SELECT role FROM qq_group_messages WHERE group_id=? AND ts>?",
                                    (gid, time.time() - 3600)).fetchall()
        n = len(rows)
        her = sum(1 for r in rows if r["role"] == "assistant")
        if n < 8 or her / n <= ratio:
            return []
        pct = round(her * 100 / n)
        if mentioned:
            return [f"这一小时群里 {n} 条里你说了 {her} 条（{pct}%），有点多了：这次被叫到就好好回，但短一点，一条说完"]
        return [f"这一小时群里 {n} 条里你说了 {her} 条（{pct}%），比一般群友多很多：多听少说，没被叫到可以不接（[跳过]）"]

    # ---------------- 视频 ----------------
    async def _look_video(self, url: str, who: str = "群友", gid: int | None = None) -> str:
        if not hasattr(self.ob, "fetch_video"):
            return ""
        thumb, mp4 = await self.ob.fetch_video(url)
        key = "qq_vid_" + re.sub(r"\W", "", url.split(":")[2] if url.count(":") >= 2 else url)[:32]
        old = self.db.get(key)
        if old and (not mp4 or str(old).startswith("[mp4]")):
            return str(old).removeprefix("[mp4]")
        data, what = b"", "封面（视频本身还没下下来，只看得到封面）"
        if mp4:
            try:
                data = await self._video_grid(mp4)
                what = "4 张截图（按时间从左到右、从上到下）"
            except Exception as e:  # noqa: BLE001
                log.info("视频截图没成：%s", e)
        if not data:
            data = thumb or b""
            mp4 = ""
        if not data:
            return ""
        try:
            desc = await self.brain.llm.describe_image(base64.b64encode(data).decode(), "",
                                                       prompt=VIDEO_PROMPT.format(who=who, what=what))
        except Exception as e:  # noqa: BLE001
            log.info("群视频没看成：%s", e)
            return ""
        desc = re.sub(r"\s+", " ", str(desc or "")).strip(" 。")[:80]
        if not desc:
            return ""
        if not mp4:
            desc += "（只看了封面）"
        self.db.set(key, ("[mp4]" if mp4 else "") + desc)
        return desc

    async def _video_grid(self, path: str) -> bytes:
        ff, fp = shutil.which("ffmpeg"), shutil.which("ffprobe")
        if not ff or not fp:
            return b""
        p = await asyncio.create_subprocess_exec(fp, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path,
                                                 stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(p.communicate(), 15)
        dur = float((out or b"0").decode().strip() or 0)
        if dur <= 0:
            return b""
        shots = []
        for k, frac in enumerate((0.1, 0.37, 0.63, 0.9)):
            t = dur * frac
            q = await asyncio.create_subprocess_exec(ff, "-v", "error", "-ss", f"{t:.2f}", "-i", path, "-frames:v", "1",
                                                     "-vf", "scale=480:-2", "-f", "image2pipe", "-vcodec", "mjpeg", "-",
                                                     stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            jpg, _ = await asyncio.wait_for(q.communicate(), 20)
            if jpg:
                shots.append((k + 1, jpg, f"{int(t) // 60}:{int(t) % 60:02d}"))
        if not shots:
            return b""
        from aigroup.net import storyboard as sb
        return sb.compose(shots, cols=2, cell_w=480, cell_h=270)

    # ---------------- 群友自己的记录 ----------------
    def member_counts(self, uid: int) -> dict:
        c = self.db.conn
        one = lambda sql: int(c.execute(sql, (uid,)).fetchone()[0] or 0)      # noqa: E731
        return {"messages": one("SELECT COUNT(*) FROM qq_group_messages WHERE user_id=? AND role='user'"),
                "facts": one("SELECT COUNT(*) FROM qq_facts WHERE subject_id=?"),
                "profile": bool(one("SELECT COUNT(*) FROM qq_people WHERE user_id=? AND profile!=''")),
                "plans": one("SELECT COUNT(*) FROM qq_plans WHERE user_id=?")}

    async def records_cmd(self, gid: int, uid: int, name: str, text: str, mid=None) -> bool:
        """/我的记录、/删除我的记录 确认：只管发命令的这个人自己的记录。"""
        if not self.gk(gid).get("member_records_cmd", True):
            return False
        my, de = MY_RX.match(text or ""), DEL_RX.match(text or "")
        if not my and not de:
            return False
        self._record(gid, uid, name, text, mid)
        n = self.member_counts(uid)
        if my:
            reply = (f"{name}：我这边记着你 {n['messages']} 条聊天、{n['facts']} 件关于你的事"
                     + ("、一段画像" if n["profile"] else "") + (f"、{n['plans']} 个约定" if n["plans"] else "") + "。"
                     "想全删就发：/删除我的记录 确认")
        elif not de.group(1):
            reply = "要删的话发：/删除我的记录 确认（聊天记录、画像、我记的关于你的事都会删掉，删了找不回来）"
        else:
            from aigroup.memory.forget import forget_person
            got = await asyncio.to_thread(forget_person, self.db, uid)
            for attr in ("_tmap", "_hx_cache"):
                if isinstance(getattr(self, attr, None), dict):
                    getattr(self, attr).clear()
            log.info("群 %s：%s 让她删掉自己的记录：%s", gid, name, got)
            reply = f"删好了，{name}：关于你的聊天记录、画像和记的事都删了（{got.get('qq_group_messages', 0)} 条聊天）"
        from aigroup.onebot import text_seg
        try:
            sid = await self.ob.send_group(gid, [text_seg(reply)], _control=True)
            self._record(gid, self.ob.self_id or 0, self.brain.persona.name, reply, sid, role="assistant")
        except Exception as e:  # noqa: BLE001
            log.info("群 %s：记录命令的回复没发出去：%s", gid, e)
        return True
