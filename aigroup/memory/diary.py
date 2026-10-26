"""24.0 两样从 DSH“养鱼”项目里学来的：

1. 按天的群日记 + 提到哪天就回查那天（feiyu 的按天记忆）：
   每晚已经会给前一天写“群日报”（digest.daily_report，3~5 行看点）。现在聊天里有人提到某一天
   （“昨天”“前天”“周五那次”“10-23”“23 号”），就把那天的日报看点 + 那天记下的事放进提示词，免得她把日子记混。
   那天还没写过日报：先给那天的几句原话，后台补写日报（下次就有了）。

2. 真人接法参考（fishtalk 的“开口前找真人碰到类似话怎么接”）：
   在这个群以前的聊天里找和刚才那句最像的一句（BM25，证据分够高才算），看当时下一个群友是怎么接的，
   给她 1~2 个例子“学口吻和分寸，别照抄”。只用群友之间的真实对话，不用她自己说过的。
"""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timedelta

log = logging.getLogger("aigroup.group")

WEEK = "一二三四五六日天"
_DAY_RX = re.compile(r"(大前天|前天|昨天|昨晚|昨儿|上(个)?(周|星期|礼拜)([一二三四五六日天])|(周|星期|礼拜)([一二三四五六日天])(那天|那次|那会|晚上|下午|上午|中午)|"
                     r"(\d{1,2})(?:月|-(?=\d{2}))(\d{1,2})(日|号)?|(?<![\d月\-])(\d{1,2})(号|日)(?![位线机楼房桌种])(那天|那次|晚上|下午|上午|中午)?)")


def day_refs(text: str, now: datetime) -> list[datetime]:
    """一句话里提到的过去的某一天（今天不算；最多两天，按出现顺序）。"""
    out: list[datetime] = []
    base = now.replace(hour=0, minute=0, second=0, microsecond=0)
    for m in _DAY_RX.finditer(text or ""):
        g = m.group(0)
        d = None
        if g.startswith("大前天"):
            d = base - timedelta(days=3)
        elif g.startswith("前天"):
            d = base - timedelta(days=2)
        elif g.startswith(("昨天", "昨晚", "昨儿")):
            d = base - timedelta(days=1)
        elif m.group(4):                                  # 上周 X
            wd = WEEK.index(m.group(4)) % 7 if m.group(4) != "天" else 6
            d = base - timedelta(days=base.weekday() + 7 - wd)
        elif m.group(6):                                  # 周 X 那天（最近一个过去的周 X）
            wd = WEEK.index(m.group(6)) % 7 if m.group(6) != "天" else 6
            back = (base.weekday() - wd) % 7 or 7
            d = base - timedelta(days=back)
        elif m.group(8) and m.group(9):                   # 10-23 / 10月23日
            try:
                d = base.replace(month=int(m.group(8)), day=int(m.group(9)))
                if d > base:
                    d = d.replace(year=d.year - 1)
            except ValueError:
                d = None
        elif m.group(11):                                 # 23 号
            try:
                d = base.replace(day=int(m.group(11)))
                if d >= base:
                    d = (base.replace(day=1) - timedelta(days=1)).replace(day=int(m.group(11)))
            except ValueError:
                d = None
        if d is not None and d < base and (base - d).days <= 60 and d not in out:
            out.append(d)
    return out[:2]


class DiaryMixin:
    def day_block(self, gid: int, text: str) -> str:
        """聊天里提到了过去的某天：那天群里的事。"""
        if not self.gk(gid).get("day_lookup", True):
            return ""
        days = day_refs(text, self.brain.now())
        if not days:
            return ""
        from aigroup.core.safety import clean_member_text
        out = []
        for d in days:
            t0, t1 = d.timestamp(), (d + timedelta(days=1)).timestamp()
            rep = self.db.get(f"qq_daily_{gid}_{d:%Y-%m-%d}") or {}
            lines = [f"- {x}" for x in (rep.get("highlights") or [])[:5]]
            ev = self.db.conn.execute("SELECT text FROM qq_facts WHERE group_id=? AND t_valid>=? AND t_valid<? "
                                      "AND (t_invalid IS NULL OR t_invalid>?) ORDER BY t_valid LIMIT 6",
                                      (gid, t0, t1, time.time())).fetchall()
            lines += [f"- 记下的事：{r['text']}" for r in ev]
            if not rep:
                rows = self.db.conn.execute(
                    "SELECT name, role, content FROM qq_group_messages WHERE group_id=? AND ts>=? AND ts<? AND role='user' "
                    "AND length(content)>=6 AND content NOT LIKE '[%' ORDER BY length(content) DESC LIMIT 5", (gid, t0, t1)).fetchall()
                lines += [f"- {r['name']} 说过：‹{clean_member_text(r['content'], 50)}›" for r in rows]
                n = self.db.conn.execute("SELECT COUNT(*) n FROM qq_group_messages WHERE group_id=? AND ts>=? AND ts<?",
                                         (gid, t0, t1)).fetchone()["n"]
                if n >= 10 and hasattr(self, "daily_report") and self._allow(f"diary_{gid}", 4):
                    self._spawn(self.daily_report(gid, d))           # 后台补写那天的日报，下次就有了
            if not lines:
                lines = ["- 那天群里没什么记录（可能你那天不在线）：别编，说记不清了"]
            out.append(f"## {d:%m-%d}（星期{WEEK[d.weekday()]}）\n" + "\n".join(lines))
        return "# 刚才提到的那天群里的事（照这个说，别把日子记混；这里没有的就说记不清）\n" + "\n".join(out)

    def human_examples_block(self, gid: int, text: str, k: int = 2) -> str:
        """以前群里有人说过类似的话时，下一个群友是怎么接的（学口吻和分寸）。"""
        text = re.sub(r"\[[^\]]*\]|@\S+", "", str(text or "")).strip()
        if not self.gk(gid).get("human_examples", True) or len(text) < 4:
            return ""
        from aigroup.memory.facts import BM25, conf, tokens
        cache = self.__dict__.setdefault("_hx_cache", {})
        top = int(self.db.conn.execute("SELECT IFNULL(MAX(id),0) m FROM qq_group_messages WHERE group_id=?",
                                       (gid,)).fetchone()["m"])
        hit = cache.get(gid)
        if not hit or top - hit[0] > 50:
            rows = [dict(r) for r in self.db.conn.execute(
                "SELECT id, ts, user_id, name, role, content FROM qq_group_messages WHERE group_id=? ORDER BY id DESC LIMIT 3000",
                (gid,)).fetchall()][::-1]
            pairs = []
            for i in range(len(rows) - 1):
                a, b = rows[i], rows[i + 1]
                if (a["role"] == "user" and b["role"] == "user" and a["user_id"] != b["user_id"]
                        and 0 < float(b["ts"]) - float(a["ts"]) < 300 and len(str(a["content"] or "")) >= 4
                        and not str(a["content"]).startswith("[") and not str(b["content"]).startswith("[")
                        and len(str(b["content"] or "")) <= 40 and a["id"] < top - 30):
                    pairs.append((a, b))
            bm = BM25([tokens(a["content"]) for a, _ in pairs]) if pairs else None
            hit = (top, pairs, bm)
            cache[gid] = hit
        _, pairs, bm = hit
        if not pairs or bm is None:
            return ""
        sc = bm.scores(tokens(text))
        order = sorted(range(len(pairs)), key=lambda i: -sc[i])
        from aigroup.chat.checks import content_terms
        q = content_terms(text)

        def close(a) -> bool:                             # 至少两个实词对得上、占这句的四成以上：只撞上一个词（“好累”）不算像
            o = q & content_terms(a["content"])
            return len(o) >= 2 and len(o) >= 0.4 * max(1, len(q))
        picked = [pairs[i] for i in order[:20] if conf(sc[i]) >= 0.75 and close(pairs[i][0])
                  and str(pairs[i][0]["content"]).strip() != text][:k]
        if not picked:
            return ""
        from aigroup.core.safety import clean_member_text
        lines = [f"- 有人说“{clean_member_text(a['content'], 40)}”，{b['name']}回“{clean_member_text(b['content'], 40)}”"
                 for a, b in picked]
        return "# 这个群以前碰到类似的话，大家是这么接的（只学口吻和长短，别照抄，也别提这些旧话）\n" + "\n".join(lines)
