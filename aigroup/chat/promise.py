"""25.0 五件事（10-26 日报 + 10-27 调研 B 站 / DSH 的几个项目）里和“说了要做到”有关的部分：

1. 承诺对账（dsh-world 的 plan_next + 工具日志对账）：
   她说“我去看看 / 我翻翻赛程 / 等我核一下 / 晚点告诉你”时记一笔（qq_promises：答应了谁、他问的是什么、多久内给）。
   之后她说了有内容的话就算兑现；到点还没兑现 → 空闲检查里补上：再查一次，查到就说，查不到就认一句“刚才说去看，没看成”。
   10-26：12:49“我这就去看看啊”→ 12:50“还没核完”→ 25 分钟没下文，群友催“预测结果呢”+ 戳一戳。
2. 说查到了就得有查资料记录：话里有“核好了 / 查到了 / 翻到了”，这次的查资料简报却是空的或者“什么都没查到” → 重写。
   10-26 13:18“核好了”+ 自己编的开赛时间和赛制。
3. 已经答应过还没给，又来一句“我再翻翻”：不说（10-26 两次“我去看”紧跟“还没核完”，撞出刷屏）。
4. 像自己以前（fishtalk check 的“像自己以前”、Coopanion 的“别模仿旧消息的措辞”）：
   和她 24 小时内说过的某句太像（换了几个词、同一个句架子）→ 换个意思或者不说。10-26 夜里连着三句自怜。
5. 照抄（fishtalk check 的“照抄语料”）：和这次给她看过的“真人接法参考”、想起来的原话连续 10 个字以上一样
   （或 6 个字以上、占原句八成）→ 重写。重合部分不到 4 个汉字（品牌名、数字）不算；放在引号里是在引用，不算。
6. 接错人：她点名说“X 你……”，话里的关键说法却是另一个人刚说的（10-26 16:01“X 你别拿它当正常体温”，
   “正常体温”是 Y 说的，X 说的是“低烧”）。
"""
from __future__ import annotations

import logging
import re
import time
from difflib import SequenceMatcher

from aigroup.chat.address import CJK, TAG

log = logging.getLogger("aigroup.group")

SCHEMA = """
CREATE TABLE IF NOT EXISTS qq_promises (       -- 25.0 她答应了要去看 / 去查 / 晚点告诉的事
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    group_id INTEGER NOT NULL,
    target_uid INTEGER,
    target_name TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL,                         -- 她说的那句（“我这就去看看啊”）
    question TEXT NOT NULL DEFAULT '',          -- 对方问的（“明天凌晨四场再预测一下”）
    due REAL NOT NULL,                          -- 到这个时候还没给结果就补
    state TEXT NOT NULL DEFAULT 'open',         -- open / done 兑现了 / followed 补过了 / expired 过太久不补了
    done_ts REAL,
    note TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_promises ON qq_promises(group_id, state, ts);
"""

# 她答应去看 / 去查 / 晚点给（只认她自己说的；“你去看看”不算）
PROMISE_RX = re.compile(
    r"(我|让我|等我)?(这就|马上|先|去|再)?(去)?(看看|瞅(一?眼|瞅)|翻(一?翻|一下|下)|戳(一?下)|核(一?下|对一下)|查(一?查|一下|下)|搜(一?搜|一下|下))"
    r"(去|再说|再给你|再回你|告诉你|啊|哈)?|"
    r"我(这就|马上|先)?去(看|查|翻|搜|核)|等我(看|查|翻|核|搜|问)|晚点(告诉|跟你说|给你|发你|回你)|回头(告诉|发你|跟你说)|"
    r"还没(核|查|看|翻)完|(核|查|看)完(再|了再)(给|告诉|回)你|稍等|等一?下我")
# 只是在讨论“看看”（“你看看这个”“看看再说吧”）不算：前面得是她自己
_NOT_PROMISE = re.compile(r"你(去|先|再)?(看看|查查|翻翻|搜搜)|一起看看|看看再说吧|看看就好|去看看医生|看看病")
# 说自己已经查到 / 核好了
DONE_RX = re.compile(r"(核|查|翻|搜)(好|到|完)了|核实了|查清楚了|查着了")
_NOTHING = "（什么都没查到）"


def is_promise(text: str) -> bool:
    t = TAG.sub("", text or "").strip()
    if not t or _NOT_PROMISE.search(t):
        return False
    return bool(PROMISE_RX.search(t))


def is_bare_promise(text: str) -> bool:
    """整句就是“我去看看 / 还没核完我再翻翻”这种，没有别的内容。"""
    t = TAG.sub("", text or "").strip()
    if not is_promise(t):
        return False
    rest = PROMISE_RX.sub("", t)
    rest = re.sub(r"[，,。！!~～…\s啊哈呀吧呢嘛哦噢嗯]|我|你|给|再|就|这|那|准数|结果|赛程|比赛|资料|手机|一下|看", "", rest)
    return len(rest) <= 6


def found_something(web: str) -> bool:
    """这次放进提示词的资料里有没有真查到的东西（简报、刚才查过的、账本）。"""
    w = str(web or "")
    if not w.strip():
        return False
    if _NOTHING in w and not re.search(r"^- (?!（什么都没查到）)", w, re.M):
        return False
    return True


def longest_common(a: str, b: str) -> str:
    sm = SequenceMatcher(None, a, b, autojunk=False)
    m = sm.find_longest_match(0, len(a), 0, len(b))
    return a[m.a:m.a + m.size]


def _strip_quotes(t: str) -> str:
    return re.sub(r"[“\"「『][^”\"」』]{0,60}[”\"」』]", "", t)


def _bigrams(t: str) -> set[str]:
    s = "".join(CJK.findall(t))
    return {s[i:i + 2] for i in range(len(s) - 1)}


class PromiseMixin:
    def _promise_init(self):
        self.db.conn.executescript(SCHEMA)
        self.db.conn.commit()

    # ================= 记账 / 兑现 =================
    def open_promises(self, gid: int, now: float | None = None, hours: float = 3) -> list[dict]:
        now = now or time.time()
        return [dict(r) for r in self.db.conn.execute(
            "SELECT * FROM qq_promises WHERE group_id=? AND state='open' AND ts>? ORDER BY id", (gid, now - hours * 3600)).fetchall()]

    def note_sent(self, gid: int, sent: list[str], target_uid=None, question: str = "", target_name: str = "",
                  now: float | None = None):
        """她发出去一批话以后调：有答应的就记一笔；有实在内容的就把之前答应的记成兑现了。"""
        if not self.gk(gid).get("promise_ledger", True):
            return
        now = now or time.time()
        texts = [TAG.sub("", str(x or "")).strip() for x in sent if x and not str(x).startswith("[表情包")]
        texts = [t for t in texts if t]
        if not texts:
            return
        # 这一批里有一句是“我去看看”：整批都算答应（“还没核完，我翻翻”+“别急，这回不瞎押了”）；
        # 除非另有一句明显是结果（说了查到了，或者 15 个字以上）
        has_promise = any(is_promise(t) for t in texts)
        substantive = [t for t in texts if not is_promise(t) and (not has_promise or DONE_RX.search(t) or len(t) >= 15)
                       and len(t) >= 6]
        if substantive:
            for p in self.open_promises(gid, now):
                if now - float(p["ts"]) > 2:              # 同一批里的不算（“我去看看，xx 是……”本身就是答案）
                    self.db.conn.execute("UPDATE qq_promises SET state='done', done_ts=?, note=? WHERE id=?",
                                         (now, substantive[0][:60], p["id"]))
        bare = [t for t in texts if is_promise(t)]
        if bare and not substantive:
            if not question or not target_uid:
                row = next((r for r in reversed(self._rows(gid, 8)) if r["role"] == "user"), None)
                if row is not None:
                    question = question or str(row["content"] or "")
                    target_uid = target_uid or row["user_id"]
            if target_uid and not target_name:
                target_name = ((self.call_name(gid, target_uid) if hasattr(self, "call_name") else "")
                               or self.names(gid).get(str(target_uid), ""))
            if not self.open_promises(gid, now):         # 已经有一笔没兑现的：不重复记
                mins = float(self.gk(gid).get("promise_minutes", 8) or 8)
                self.db.conn.execute(
                    "INSERT INTO qq_promises(ts, group_id, target_uid, target_name, text, question, due) VALUES (?,?,?,?,?,?,?)",
                    (now, gid, target_uid, target_name, bare[0][:80], str(question)[:160], now + mins * 60))
        self.db.conn.commit()

    async def promise_tick(self, gid: int, st, now: float | None = None) -> bool:
        """空闲检查里调：答应了的到点还没给 → 再查一次，查到就说，查不到就认一句。补过一次就不再补。"""
        if not self.gk(gid).get("promise_ledger", True):
            return False
        now = now or time.time()
        due = [p for p in self.open_promises(gid, now, hours=6) if float(p["due"]) <= now]
        if not due:
            return False
        p = due[0]
        late = (now - float(p["ts"])) / 60
        if late > float(self.gk(gid).get("promise_expire_minutes", 90) or 90):
            self.db.conn.execute("UPDATE qq_promises SET state='expired' WHERE id=?", (p["id"],))
            self.db.conn.commit()
            return False
        self.db.conn.execute("UPDATE qq_promises SET state='followed', done_ts=? WHERE id=?", (now, p["id"]))
        self.db.conn.commit()
        web = ""
        if p["question"] and hasattr(self, "_agent_research") and getattr(self, "agent_on", lambda g: False)(gid):
            try:
                web = await self._agent_research(gid, str(p["question"])[:120], "quick", False,
                                                 question=str(p["question"])[:128]) or ""
            except Exception as e:  # noqa: BLE001
                log.info("群 %s：补答应的事时查资料出错：%s", gid, e)
        who = p["target_name"] or "他"
        found = found_something(web)
        why = (f"你 {late:.0f} 分钟前答应了{who}（他问的是「{str(p['question'])[:40]}」），说的是「{p['text'][:24]}」，"
               "到现在还没给结果。" + ("现在把你查到的直接告诉他，一两句，别再说“我去看看”。" if found else
                                  "你还是没查到：直说没查到、认一句（“刚才说去翻，结果没翻到靠谱的”），别编，也别再说“我再看看”。"))
        log.info("群 %s：答应%s的事 %.0f 分钟了还没给，补上（%s）", gid, who, late, "查到了" if found else "没查到")
        async with st.lock:
            try:
                return bool(await self.speak(gid, mentioned=False, owner_spoke=False, trigger=None, who=who, why=why,
                                             web=web or "", must=True))
            except Exception as e:  # noqa: BLE001
                log.info("群 %s 补答应的事出错：%s", gid, e)
                return False

    # ================= 发前检查 =================
    def _promise_check(self, gid: int, text: str, web: str, now: float) -> str:
        from aigroup.chat.checks import DROP
        t = TAG.sub("", text).strip()
        if DONE_RX.search(t) and not found_something(web):
            m = DONE_RX.search(t).group(0)
            return (f"你说「{m}」，可这次你并没有查到资料（查资料记录是空的 / 什么都没查到）：别说查到了，"
                    "也别报具体的时间、赛制、比分；知道的就照实说“我记得……”，不知道就直说没查到，或者问他具体是哪几个")
        if is_bare_promise(t):
            op = self.pending_promise(gid, now)
            if op:
                ago = int((now - float(op["ts"])) / 60)
                return (f"{DROP}你{ago if ago else '刚'}{'分钟前' if ago else '才'}已经说了「{op['text'][:20]}」，还没给结果："
                        "别再说一遍“我去看 / 还没核完”，有结果就直接说，没有就别说")
        return ""

    def pending_promise(self, gid: int, now: float | None = None) -> dict | None:
        """还没兑现的答应：账本里 open 的；账本没记上的（比如重启前说的），就看她 15 分钟内最后几句：
        说过“我去看看”、之后再没说过有内容的话。"""
        now = now or time.time()
        op = self.open_promises(gid, now)
        if op:
            return op[0]
        rows = [r for r in self._rows(gid, 12) if r["role"] == "assistant" and now - float(r["ts"]) < 900]
        last = None
        for r in rows:
            c = TAG.sub("", str(r["content"] or "")).strip()
            if is_promise(c):
                last = last or {"ts": float(r["ts"]), "text": c}
            elif last and len(c) >= 6:
                last = None                                  # 后来给过结果了
        return last

    def her_lines(self, gid: int, hours: float = 24, now: float | None = None) -> list[tuple[float, str]]:
        now = now or time.time()
        return [(float(r["ts"]), str(r["content"] or "")) for r in self.db.conn.execute(
            "SELECT ts, content FROM qq_group_messages WHERE group_id=? AND role='assistant' AND ts>? ORDER BY id DESC LIMIT 120",
            (gid, now - hours * 3600)).fetchall() if r["content"] and not str(r["content"]).startswith("[")]

    def _self_like(self, gid: int, text: str, now: float) -> str:
        """和她 24 小时内说过的某句太像：同一个句架子换几个词也算。群友刚说过的词不算（是在接他）。"""
        t = TAG.sub("", text).strip()
        mine = "".join(CJK.findall(t))
        if len(mine) < 6:
            return ""
        users = " ".join(str(r["content"] or "") for r in self._rows(gid, 8) if r["role"] == "user")
        a = _bigrams(t) - _bigrams(users)
        if len(a) < 4:
            return ""
        th = float(self.gk(gid).get("self_like_threshold", 0.45) or 0.45)
        for ts, old in self.her_lines(gid, 24, now):
            if now - ts < 1:
                continue
            b = _bigrams(old)
            if not b:
                continue
            j = len(a & b) / max(1, len(a | (b - _bigrams(users))))
            common = longest_common(mine, "".join(CJK.findall(old)))
            if (j >= th or (len(common) >= 5 and common not in users)) and len(old) >= 6:
                ago = int((now - ts) / 60)
                when = f"{ago} 分钟前" if ago < 120 else f"{ago // 60} 小时前"
                return (f"这句和你{when}说的「{old[:24]}」太像了（{'都有“' + common + '”' if len(common) >= 5 else '同一个句架子'}）："
                        "别模仿你以前说过的话，换个意思；没新的要说就不说")
        return ""

    def note_shown(self, gid: int, lines: list[str]):
        """这次放进提示词给她看的原句（真人接法参考、想起来的事）：发前查照抄用。"""
        d = self.__dict__.setdefault("_shown_src", {})
        d[gid] = [str(x) for x in lines if x and len(str(x)) >= 6][:30]

    def _copied(self, gid: int, text: str) -> str:
        src = (self.__dict__.get("_shown_src") or {}).get(gid) or []
        if not src:
            return ""
        t = "".join(CJK.findall(_strip_quotes(TAG.sub("", text))))
        if len(t) < 6:
            return ""
        for s in src:
            s2 = "".join(CJK.findall(s))
            if len(s2) < 6:
                continue
            c = longest_common(t, s2)
            if len(c) < 4:
                continue
            if len(c) >= 10 or (len(c) >= 6 and len(c) >= 0.8 * len(s2)):
                return f"照抄了给你参考的原话（「{c[:16]}」）：那些只是学口吻的，用你自己的话说"
        return ""

    def _misattrib(self, gid: int, text: str, now: float) -> str:
        """点名“X 你……”，话里的关键说法却是另一个人十分钟内刚说的、X 没说过。"""
        if not hasattr(self, "names_of"):
            return ""
        rows = [r for r in self._rows(gid, 14) if r["role"] == "user" and now - float(r["ts"]) < 600]
        if len(rows) < 2:
            return ""
        by_uid: dict[int, str] = {}
        for r in rows:
            by_uid[int(r["user_id"])] = by_uid.get(int(r["user_id"]), "") + " " + str(r["content"] or "")
        t = TAG.sub("", text)
        for uid in by_uid:
            mine = by_uid[uid]
            for n in self.names_of(gid, uid):
                if len(n) < 2:
                    continue
                for clause in re.split(r"[。！!？?；;\n]", t):
                    i = clause.find(n)
                    if i < 0 or "你" not in clause[i + len(n):i + len(n) + 4]:
                        continue
                    seg = "".join(CJK.findall(clause[i + len(n):]))
                    for other, said in by_uid.items():
                        if other == uid:
                            continue
                        long = longest_common(seg, "".join(CJK.findall(said)))   # 四个字以上的共同说法（“正常体温”）
                        if len(long) >= 4 and long not in mine:
                            oname = self.call_name(gid, other) if hasattr(self, "call_name") else ""
                            oname = oname or self.names(gid).get(str(other), "别人")
                            return (f"接错人了：「{long}」是{oname}说的，不是{n}说的（{n}说的是「{mine.strip()[:20]}」）。"
                                    f"要接的是{oname}就冲{oname}说；先对一遍原话是谁说的")
        return ""
