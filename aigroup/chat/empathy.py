"""读情绪：痛点账（别拿别人难过的事开涮）、想念的阴阳收着、先读情绪、被问推荐先接他说过的（原 v363.py）。

36.3 按《11-10 聊天复盘：怎么让小满更高情商》做的（都能在小屋里单独关）：

1. 痛点账：群友这几天为某件事难过、受挫过（发了哭 / 委屈的表情，或者说“被门禁除名”“没过”“烦死”），
   那件事 3 天里不拿来开玩笑：提示词里列出来；发前规则检查（check_sore_tease）拦“拿这事损他”；自检第 24 条强制拦
   （11-10 上午老张草稿纸用完、进不了学校还发了大哭，傍晚她说“自己被门禁除名，倒挺会安慰人”）
2. 想念的阴阳收着：他冒头是来问事的，先认真答，不阴阳、不说“失踪”（11-10“失踪一天，回来就问这个”）；
   冒头算“隔了很久”的门槛提到醒着 12 小时（longing_back_hours）
3. 先读情绪：局面判断多看一项“最后说话的人此刻什么情绪”，他在笑就跟着乐，别说成“气人”（11-10“哈哈被迫下班了”→“最气人”）
4. 别人有好消息说他、不说自己（“这回听劝，我记一笔”）：提示词底线 + 自检第 25 条（风格类，只记录）
5. 被要建议时先接他自己刚说的：问她推荐 / 看法时，把这个人两小时里自己提过的事列给她
   （11-10 老板 11:22 说“那个主播队打进季后赛了”，12:01 问“最近有什么比赛看”，她推荐了 U19 足球）
"""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime

log = logging.getLogger("aigroup.empathy")

# ---------------- 1 痛点账 ----------------
DISTRESS_TXT = re.compile(r"难过|伤心|委屈|想哭|哭了|哭死|要哭|崩溃|烦死|好烦|难受|郁闷|心累|好惨|太惨|惨了|完蛋了|裂开|破防|emo了|"
                          r"被.{0,6}(除名|拒|刷掉|骂|挂了|裁|开除|甩|退回|打回|拉黑|踢)|挂科|没过|落榜|没考上|分手|生病|发烧|住院|"
                          r"丢了|摔了|受伤|进不去|白干|白做|全没了|没了啊")
DISTRESS_STICKER = re.compile(r"\[表情包[：:][^\]]*(哭|流泪|难过|委屈|伤心|崩溃|心碎|绝望|泪)")
MEDIA = re.compile(r"\[(图片|视频|表情包|链接|分享|位置|文件|语音|回复[^\]]*)[^\]]*\]")
# 损人、调侃的口气（“倒挺会”“自己都”“活该”“笑死”）
TEASE_RX = re.compile(r"倒(挺|是|还|也)|还挺|还真(会|能)|自己(都|被|也|还)|活该|谁让(你|他)|笑死|就这|还好意思|也就|不就是|怪不得|难怪|"
                      r"报应|该(着|啊)|哈哈|看你|你也有今天|好意思")
# 关心、帮忙的口气：提到那件事也不算拿来开涮
CARE_RX = re.compile(r"心疼|辛苦|抱抱|加油|没事|别难过|会好|我帮|给你(买|找|带)|摸摸|撑住|难为|太惨了吧|好过分|离谱|气人|过分|顶住|稳住")

# ---------------- 2 想念的阴阳 ----------------
ASKING_RX = re.compile(r"[？?]|吗$|呢$|啥|什么|怎么|咋|哪(个|里|儿|场)|谁|几(点|号|个)|帮我|你觉得|推荐|有没有|是不是|能不能|看看")

# ---------------- 5 被要建议 ----------------
REC_RX = re.compile(r"推荐|有(什么|啥).{0,6}(看|玩|吃|听|买|去)|看(什么|啥)|玩(什么|啥)|吃(什么|啥)|帮我(看看|找|挑|选|查)|你觉得|"
                    r"怎么(选|办)|选哪|买哪|去哪|建议|好无聊|无聊死")


def _plain(t: str) -> str:
    return re.sub(r"\s+", " ", MEDIA.sub(" ", str(t or ""))).strip()


class EmpathyMixin:
    # ================= 1 痛点账 =================
    def sore_spots(self, gid: int, now: float | None = None) -> list[dict]:
        """这几天谁为什么事难过过：[{uid, name, ts, line, terms}]。每 5 分钟算一次（只读聊天记录，不建表）。"""
        g = self.gk(gid)
        if not g.get("sore_spots", True):
            return []
        now = now or time.time()
        ck = self.__dict__.setdefault("_sore_cache", {})
        hit = ck.get(gid)
        if hit and now - hit[0] < 300:
            return hit[1]
        days = float(g.get("sore_days", 3) or 3)
        rows = [dict(r) for r in self.db.conn.execute(
            "SELECT ts, user_id, name, content FROM qq_group_messages WHERE group_id=? AND role='user' AND ts>? ORDER BY id",
            (gid, now - days * 86400)).fetchall()]
        from aigroup.chat.checks import content_terms
        names = self._name_terms(gid) if hasattr(self, "_name_terms") else set()
        out: list[dict] = []
        for i, r in enumerate(rows):
            c = str(r["content"] or "")
            if not (DISTRESS_STICKER.search(c) or DISTRESS_TXT.search(_plain(c))):
                continue
            uid, t = int(r["user_id"] or 0), float(r["ts"])
            ctx = [x for x in rows if int(x["user_id"] or 0) == uid and abs(float(x["ts"]) - t) <= 180]
            words = [_plain(x["content"]) for x in ctx if _plain(x["content"])]
            words.sort(key=lambda w: 0 if DISTRESS_TXT.search(w) else 1)        # 说难过 / 受挫的那句排前面
            if not words:
                continue
            terms = content_terms(" ".join(words)) - names
            if not terms:
                continue
            same = next((s for s in out if s["uid"] == uid and t - s["ts"] <= 600), None)
            if same:                                         # 同一段（10 分钟内）的并成一条
                same["terms"] |= terms
                same["lines"] = sorted(dict.fromkeys(same["lines"] + words), key=lambda w: 0 if DISTRESS_TXT.search(w) else 1)[:4]
                continue
            out.append({"uid": uid, "name": str(r["name"] or ""), "ts": t, "lines": words[:4], "terms": terms})
        ck[gid] = (now, out)
        return out

    def sore_block(self, gid: int, now: float | None = None, about: set | None = None) -> str:
        """about：只列这些人的（36.4 自检只看这句话冲着的人和眼前在说话的人；11-11 阿杰回“我已飞升”，
        自检拿老张前一天被门禁除名的事当理由拦了她回阿杰的话）。None = 全列（给她写稿时看）。"""
        spots = self.sore_spots(gid, now)
        if about is not None:
            spots = [s for s in spots if s["uid"] in about]
        if not spots:
            return ""
        lines = []
        for s in spots[-6:]:
            who = self.call_name(gid, s["uid"]) if hasattr(self, "call_name") else s["name"]
            when = datetime.fromtimestamp(s["ts"], getattr(self.cfg, "tz", None)).strftime("%m-%d %H:%M")
            lines.append(f"- {who or s['name']}（{when}）：{' / '.join(x[:24] for x in s['lines'][:3])}")
        return ("# 这几天别拿来开玩笑的事（他们为这些难过、受挫过）\n" + "\n".join(lines) +
                "\n这些事不拿来损他、不拿来调侃；他自己拿来自嘲，你可以跟着笑一句，但别加码")

    def sore_about(self, gid: int, text: str, target_uid=None, now: float | None = None, minutes: float = 5) -> set:
        """这句话牵涉的人：她回的那个人、话里点了名的人、最近几分钟在说话的人。"""
        now = now or time.time()
        out = {int(target_uid)} if target_uid else set()
        out |= {int(r["user_id"] or 0) for r in self.db.conn.execute(
            "SELECT user_id FROM qq_group_messages WHERE group_id=? AND role='user' AND ts>?", (gid, now - minutes * 60)).fetchall()}
        if hasattr(self, "names_of"):
            for s in self.sore_spots(gid, now):
                if any(n and n in (text or "") for n in self.names_of(gid, s["uid"])):
                    out.add(s["uid"])
        return out

    def _sore_tease(self, gid: int, text: str, target_uid=None, now: float | None = None) -> str:
        """她要发的话拿某人这几天难过的事开涮：返回原因。"""
        spots = self.sore_spots(gid, now)
        if not spots or CARE_RX.search(text or ""):
            return ""
        from aigroup.chat.checks import content_terms
        names = self._name_terms(gid) if hasattr(self, "_name_terms") else set()
        mine = content_terms(_plain(text)) - names
        if not mine or not TEASE_RX.search(text or ""):
            return ""
        for s in spots:
            common = mine & s["terms"]
            if not common:
                continue
            about = bool(target_uid and int(target_uid) == s["uid"])
            if not about and hasattr(self, "names_of"):
                about = any(n and n in text for n in self.names_of(gid, s["uid"]))
            if about:
                who = self.call_name(gid, s["uid"]) if hasattr(self, "call_name") else s["name"]
                return (f"{who}这几天为「{s['lines'][0][:16]}」难过过，你这句拿「{sorted(common)[0]}」这件事损他：换个说法，"
                        "不提这件事；想接就接他刚说的，或者不说")
        return ""

    # ================= 2 想念的阴阳 =================
    def fav_asking(self, gid: int, fav: int, now: float | None = None) -> bool:
        """老板冒头这几句是在问事 / 求帮忙（不是闲聊）。"""
        now = now or time.time()
        rows = self.db.conn.execute("SELECT content, to_her FROM qq_group_messages WHERE group_id=? AND role='user' AND user_id=? "
                                    "AND ts>? ORDER BY id DESC LIMIT 3", (gid, int(fav), now - 600)).fetchall()
        return any(r["to_her"] or ASKING_RX.search(_plain(r["content"])) for r in rows)

    # ================= 3 情绪 =================
    @staticmethod
    def mood_note(d: dict) -> str:
        m, who = str(d.get("mood") or ""), str(d.get("mood_who") or d.get("to_whom") or "他")
        if "笑" in m or "得意" in m or "开心" in m:
            return (f"（36.3）{who}是笑着说的：跟着乐，别把他的事说成“气人 / 惨 / 最难受”，也别替他补他没说的细节")
        if "难过" in m:
            return f"（36.3）{who}现在有点难过：接他的情绪，不开玩笑、不讲道理、不给建议，一句就够"
        if "烦" in m:
            return f"（36.3）{who}在烦：先站他那边，别劝、别说风凉话"
        return ""

    # ================= 5 被要建议 =================
    def own_mentions_note(self, gid: int, texts: list, now: float | None = None) -> str:
        """有人问她推荐 / 看法：他两小时里自己提过的事（先从这里接）。"""
        if not texts or not self.gk(gid).get("own_mentions", True):
            return ""
        now = now or time.time()
        last = texts[-1]
        uid, body = int(last[1] or 0), _plain(last[2])
        if not uid or not REC_RX.search(body):
            return ""
        rows = self.db.conn.execute("SELECT content FROM qq_group_messages WHERE group_id=? AND role='user' AND user_id=? AND ts>? "
                                    "ORDER BY id DESC LIMIT 30", (gid, uid, now - 2 * 3600)).fetchall()
        said = []
        for r in rows:
            c = str(r["content"] or "")
            p = _plain(c) or (re.sub(r"^\[分享[：:]", "", c)[:40] if c.startswith("[分享") else "")
            if len(p) >= 6 and p != body and not REC_RX.search(p):
                said.append(p[:40])
        if not said:
            return ""
        return ("（36.3）他这两小时自己提过：" + "；".join(f"「{x}」" for x in said[:4][::-1]) +
                "。他问你推荐 / 看法时，先看能不能从他自己提过的接（他感兴趣的就在这里），再补别的")
