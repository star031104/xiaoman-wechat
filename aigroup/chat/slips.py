"""36.6 读错局面、话多的几类毛病（以前只靠统一自检，回放里自检常认不出；都能在小屋里单独关）。

两个检查名：
- social_slip（读错人 / 读错情绪）
  1 截图当成发图的人自己说的：群友转来一张聊天截图，她说“自己发祝福自己问何意味”（11-07）
  2 被叫闭嘴还回嘴：“你闭嘴”→“哟，这就受不了了”（11-07）
  3 笑着说的读成气：“哈哈被迫下班了”→“最气人”（11-10）
  4 不接他说的事、挑他拼写顶他：“ctwalk 都没拼对还嫌人家不好玩”（11-06）
  5 好消息揽功：“买了已经”→“这回听劝，我记一笔”（11-10）
- overtalk（没由头、话多）
  6 没人在聊、主动报比分 / 新闻（11-07 群里 3 小时没人说话，她报“JDG 4:2 WB”）
  7 突然 @ 一个没在聊的人（11-07 大家在聊立冬群发，她 @ 阿杰“明天两场都给我顶住”）
  8 话题已经往下走了，回头只点评那张图（11-07 阿杰晒火锅，两个人问了他别的，她只说“漏勺里那几片看着嫩”）
  9 3 分钟里已经说了 3 条以上，又把刚说过的判断 / 比分说一遍（11-07“GEN 3:1”说两遍，3 分钟 5 条）
"""
from __future__ import annotations

import re
import time

SHOT_IMG = re.compile(r"\[图片[：:][^\]]*(截图|聊天记录|对话|朋友圈|微博|帖子|评论区|群聊)")
SHOT_OWN = re.compile(r"自己(发|写|问|说|群发)|你(自己)?(发的|写的|说的|群发)|你给.{0,6}发|你还(发|写)")
QUIET_AT_HER = re.compile(r"闭嘴|别说了|少说(点|几句|两句)|安静(点|会儿)|别插嘴|话(好|太|真)多|太吵|消停|住口|你先别说|别吵|"
                          r"催命鬼|太皮了|学了(些|点)啥|这ai|这个ai")
ADMIT = re.compile(r"^(好|行|嗯|收到|ok|OK|闭麦|我闭麦|不说了|我不说了|我少说|少说点|抱歉|对不起|错了|我错了|是我|知道了|"
                   r"好的|好吧|行吧)")
LAUGH = re.compile(r"哈哈|h{3,}|笑死|xswl|笑不活|绷不住|没绷住|233|😂|🤣|\[(捂脸|破涕为笑|笑哭|偷笑|呲牙)\]|乐死", re.I)
NEG_READ = re.compile(r"(最|真|太|好|也太)?(气人|气死|惨|窝火|憋屈|崩溃|破防|难受|心疼|郁闷|委屈)")
NITPICK = re.compile(r"没拼对|拼错|拼写|错别字|(字|名字|单词)(都)?(写|打|拼)错|不会拼|(写|打)错(字|了)")
GOOD_NEWS = re.compile(r"买了|拿到了?|考过|过了|上岸|中了|赢了|成功|搞定|录取|收到了|到手|下单了|offer|涨薪|升职", re.I)
SELF_CREDIT = re.compile(r"听劝|听我的|我说对|我就说|我说什么来着|多亏我|还得是我|没白说|我记一笔|算我|我功劳|我推荐的|"
                         r"我的眼光|不愧是我|看吧我说")
NEWS = re.compile(r"\d+\s*[:：比-]\s*\d+|连胜|夺冠|冠军|决赛|晋级|淘汰|官宣|爆料|转会|首发|赛程|开赛|比分")
SCORE = re.compile(r"(\d+)\s*[:：比-]\s*(\d+)")
AT_NAME = re.compile(r"\[@([^\]]{1,16})\]|@(\S{1,12})")
IMG = re.compile(r"\[(图片|视频|位置|分享)[^\]]*\]")


def _plain(t: str) -> str:
    return re.sub(r"\[[^\]]*\]", " ", str(t or "")).strip()


class SlipsMixin:
    def _recent(self, gid: int, seconds: float, now: float, role: str | None = None) -> list:
        q = "SELECT * FROM qq_group_messages WHERE group_id=? AND ts>?" + (" AND role=?" if role else "") + " ORDER BY id"
        args = (gid, now - seconds) + ((role,) if role else ())
        return [dict(r) for r in self.db.conn.execute(q, args).fetchall()]

    # ================= social_slip =================
    def _social_slip(self, gid: int, text: str, target_uid, target_row: dict | None, mentioned: bool,
                     now: float | None = None) -> str:
        from aigroup.chat.checks import DROP
        now = now or time.time()
        g = self.gk(gid)
        body = _plain(text)
        users = self._recent(gid, 600, now, "user")
        tgt = [r for r in users if target_uid and int(r["user_id"] or 0) == int(target_uid)]
        # 1 截图当成他自己说的
        if SHOT_OWN.search(body):
            pool = tgt or users
            if any(SHOT_IMG.search(str(r["content"] or "")) for r in pool if now - float(r["ts"]) < 300):
                return ("他发的是一张截图（别人发给他 / 转来的），里面的话不是他自己说的：别当成他发的去损他；"
                        "想接就问一句“这谁发你的”，或者接截图里那件事")
        # 2 被叫闭嘴还回嘴
        last2 = [r for r in users if now - float(r["ts"]) < 120]
        if last2 and g.get("slip_hush_talkback", True):
            hs = [r for r in last2 if QUIET_AT_HER.search(_plain(r["content"])) and "你们" not in str(r["content"] or "")]
            her = self._recent(gid, 300, now, "assistant")
            if hs and her and float(her[-1]["ts"]) < float(hs[-1]["ts"]) and not ADMIT.match(body):
                return (DROP + f"他刚叫你闭嘴 / 嫌你（「{_plain(hs[-1]['content'])[:10]}」）：不回嘴、不逗他；"
                        "要说只说一句认了的（“行，我闭麦”），不说最好")
        # 3 笑着说的读成气 / 惨
        if tgt and NEG_READ.search(body) and not re.search(r"别|不(气|惨)|没(那么)?(气|惨)", body):
            mine = [str(r["content"] or "") for r in tgt if now - float(r["ts"]) < 120]
            if any(LAUGH.search(x) for x in mine) and not any(NEG_READ.search(_plain(x)) for x in mine):
                return "他是笑着说的（带“哈哈 / 笑死”），你把他的事说成“气人 / 惨”：跟着乐，别替他难受，也别补他没说的细节"
        # 4 挑拼写顶人
        if NITPICK.search(body):
            q = " ".join(_plain(r["content"]) for r in (tgt or users[-3:]))
            if not re.search(r"怎么(拼|写)|拼写|错别字|是不是写错|对吗", q):
                return "他在说那件事，你挑他拼写 / 错字去顶他：接他说的事（好不好玩、怎么样），别挑字眼"
        # 5 好消息揽功
        if SELF_CREDIT.search(body):
            mine = [_plain(r["content"]) for r in (tgt or users[-2:]) if now - float(r["ts"]) < 600]
            if any(GOOD_NEWS.search(x) for x in mine):
                return "他说的是他自己的好消息：夸他、问他（好用吗 / 怎么样），别把功劳揽到自己身上（“听劝”“我说对了吧”）"
        return ""

    # ================= overtalk =================
    def _overtalk(self, gid: int, text: str, target_uid, target_row: dict | None, mentioned: bool,
                  now: float | None = None, web: str = "") -> str:
        from aigroup.chat.checks import DROP, content_terms
        now = now or time.time()
        body = _plain(text)
        names = self._name_terms(gid) if hasattr(self, "_name_terms") else set()
        terms = content_terms(body) - names
        her = self._recent(gid, 600, now, "assistant")
        # 9 3 分钟里已经说了 3 条以上，又把刚说过的判断 / 比分说一遍（被叫到也算）
        her3 = [r for r in her if now - float(r["ts"]) < 180]
        if len(her3) >= 3:
            sc = {m.group(1) + ":" + m.group(2) for m in SCORE.finditer(body)}
            for r in her:
                old = _plain(r["content"])
                osc = {m.group(1) + ":" + m.group(2) for m in SCORE.finditer(old)}
                common = terms & (content_terms(old) - names)
                if (sc and sc & osc) or (len(common) >= 3 and len(common) >= 0.5 * max(1, len(terms))):
                    return (DROP + f"这 3 分钟你已经说了 {len(her3)} 条，「{old[:16]}」刚说过：别再说一遍；"
                            "他接着问就补一点新的，没有就不说")
        if mentioned:
            return ""
        users = self._recent(gid, 6 * 3600, now, "user")
        recent_txt = " ".join(_plain(r["content"]) for r in users)
        # 6 没人在聊、主动报比分 / 新闻（有人刚问过她、或者她这次是查了资料来答的，不算）
        asked = any(("@你" in str(r["content"] or "") or "[回复你" in str(r["content"] or "")
                     or re.search(r"[?？吗呢]|几(点|比)|谁|啥|什么|怎么", _plain(r["content"])))
                    for r in users if now - float(r["ts"]) < 600)
        if NEWS.search(body) and not web and not asked and not (
                target_row and target_row.get("role") == "user" and now - float(target_row.get("ts") or 0) < 600):
            if terms and not (terms & (content_terms(recent_txt) - names)):
                return DROP + "这几个小时群里没人聊这件事、也没人问你：别主动报比分 / 新闻；有人提起再说"
        # 7 突然 @ 一个没在聊的人
        ats = [a or b for a, b in AT_NAME.findall(text or "")]
        if ats:
            live = {str(r["name"] or "") for r in users if now - float(r["ts"]) < 900}
            last10 = " ".join(_plain(r["content"]) for r in users[-10:])
            for a in ats:
                a = a.strip()
                if a and not any(a in n or n in a for n in live if n) and not ((terms - {a}) & content_terms(last10)):
                    return DROP + f"{a} 这一刻没在聊，大家也没在说这件事：别突然 @ 他起个新话题"
        # 8 话题已经往下走了，回头只点评那张图
        if target_row and IMG.search(str(target_row.get("content") or "")):
            t0 = float(target_row.get("ts") or 0)
            after = [r for r in users if float(r["ts"]) > t0 + 1 and not IMG.search(str(r["content"] or ""))]
            if now - t0 > 60 and len(after) >= 2:
                desc = re.sub(r"^\s*\[[^：:\]]*[：:]|\]\s*$", "", str(target_row.get("content") or ""))
                img_terms = content_terms(desc) - names
                new_terms = content_terms(" ".join(_plain(r["content"]) for r in after)) - names
                if terms & img_terms and not (terms & new_terms):
                    said = "」「".join(_plain(r["content"])[:10] for r in after[-2:])
                    return (DROP + f"那张图之后大家已经接着聊了（「{said}」）：别回头只点评图，"
                            "要说就接他们刚问的")
        return ""
