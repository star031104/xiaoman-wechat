"""22.0 发之前的几道检查（都按你群里真出过的毛病来）。

每道检查一个开关，三档：off 关 / shadow 只记录（qq_check_log，不拦）/ on 命中就让她重写，重写还不过、没被叫到就不说。
- source_gate     亲历闸门：说自己“昨天 / 这周 / 刚……”做了什么，设定、今天的安排、她以前说过的里都没有 → 编的
- self_consistency 发前矛盾检查：说自己的事和她以前说过的对不上（多问一次便宜模型，只在句子里有“我……”时才问）
- ask_repeat      问过账：同一件事 24 小时内问过 / 叮嘱过，或者对方说过“别问了” → 别再问
- template        跨天套路：她这一周反复用的开头和句式（“行行行，改成 X 版”）
- addressee       叫错人：回的是 A，嘴里叫的是 B；或者那句是 A 在问 B
- time_sense      时间感：几个小时前的话说成“刚才”
- tease_risk      损人风险：不熟、他被损以后不接、他今天状态不好时还损他
- responsive      回应性：别人分享好事，只回一句“恭喜 / 厉害”
- room_read       热聊别插：两个人正一来一回聊得快，跟她无关
"""
from __future__ import annotations

import json
import logging
import re
import time

from aigroup.chat.address import CJK, COMMON2, TAG

log = logging.getLogger("aigroup.group")

SCHEMA = """
CREATE TABLE IF NOT EXISTS qq_check_log (      -- 22.0 发前检查命中的记录（影子模式也记）
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    group_id INTEGER NOT NULL,
    gate_id INTEGER,
    name TEXT NOT NULL,
    mode TEXT NOT NULL DEFAULT 'shadow',
    text TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_check_log ON qq_check_log(group_id, ts);
CREATE TABLE IF NOT EXISTS qq_asks (           -- 22.0 她问过 / 叮嘱过谁什么
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    group_id INTEGER NOT NULL,
    target_uid INTEGER,
    kind TEXT NOT NULL DEFAULT 'ask',          -- ask 问 / care 叮嘱
    text TEXT NOT NULL,
    terms TEXT NOT NULL DEFAULT '[]',
    closed_until REAL                           -- 对方说了“别问了”：到这个时候之前都别再提
);
CREATE INDEX IF NOT EXISTS idx_asks ON qq_asks(group_id, ts);
"""

CHECKS = ("source_gate", "self_consistency", "ask_repeat", "template", "addressee", "time_sense", "tease_risk",
          "responsive", "room_read")

# 这些词太常见，不算“说的是哪件事”
STOP2 = COMMON2 | set("""我这 我那 这边 那边 一会 等会 今天 昨天 前天 这周 上周 最近 以前 之前 刚才 刚刚 上次 那天 周末 早上 晚上 中午
下午 今早 昨晚 还没 没有 了吗 了没 怎么 咋了 啥时 什么 多少 几点 记得 别忘 注意 小心 一定 好好 早点 有空 回来 回去 一下 好了 行行 哈哈""".split())

# ---- 说自己经历的句子 ----
_TIME = r"(这周|这几天|这两天|昨天|昨晚|前天|今天|今早|早上|中午|下午|晚上|刚才|刚刚|刚|才|上次|上周|上个月|以前|小时候|之前|最近|那天|周末|去年)"
_LIFE = (r"(去|蹲|交|写|赶|采访|考|跑|学|玩|养|住|喝|吃|买|逛|见|拿|搬|上课|下课|上班|下班|加班|通勤|出门|回家|到家|开会|做饭|"
         r"洗澡|健身|旅游|排队|打工|实习|面试|看病|生病|感冒|发烧|熬夜|通宵)")
_SELF_LIFE = re.compile(r"我(的|们)?(老师|同学|室友|宿舍|学校|食堂|稿子|作业|论文|公司|老板|同事|领导|妈|爸|爷爷|奶奶|男朋友|女朋友|"
                        r"导师|课|考试|工资|房东|楼下|小区)")
CLAIM_RX = [
    re.compile(r"我" + r"[^，,。！!？?\s]{0,2}" + _TIME + r"[^，,。！!？?]{0,10}"),
    re.compile(r"我" + r"[^，,。！!？?\s]{0,3}" + _LIFE + r"(?!不了|不动|不下)[^，,。！!？?]{0,3}(了|过|完|着|呢|还没|没)"),
    re.compile(r"我(这边|这里)[^，,。！!？?]{1,12}(还没|没|了|呢)"),
    re.compile(r"(老师|同学|室友|老板|导师|同事|领导|我妈|我爸)(让|叫|带|喊|说)我"),
]
# 这些“我……”不算经历（说群里的事、说自己在想、在看消息）
NOT_CLAIM = re.compile(r"我(刚|才)?(看|看到|说|问|想|觉得|记得|猜|查|搜|发|回|打错|理解|懂|明白|知道|以为)|我(也)?(不|没)(知道|懂|想)|"
                       r"我(记|写)(下来|进小本本|在小本本)|我去(查|搜|看看|翻)|群里|群友|聊天|小本本|你们|消息|宣布|惦记|想着|画像|资料|数据|学成|记性|脑子")


def content_terms(text: str, extra_stop: set | None = None) -> set[str]:
    """一句话里“说的是哪件事”的词：两个字的中文片段，去掉太常见的。"""
    stop = STOP2 | (extra_stop or set())
    out = {w.lower() for w in re.findall(r"[A-Za-z][A-Za-z0-9]{1,11}", TAG.sub("", text or ""))}   # AG、KPL 这种也算
    for run in CJK.findall(TAG.sub("", text or "")):
        for i in range(len(run) - 1):
            g = run[i:i + 2]
            if g not in stop and not g.startswith("我") and not g.endswith("了") and not g.startswith("了"):
                out.add(g)
    return out


def self_claims(text: str) -> list[str]:
    """她这句里说自己经历 / 生活的小句。"""
    out = []
    for clause in re.split(r"[，,。！!？?；;~～\n]|\s{2,}", TAG.sub("", text or "")):
        c = clause.strip()
        if ("我" not in c) or NOT_CLAIM.search(c):
            continue
        if any(rx.search(c) for rx in CLAIM_RX) or _SELF_LIFE.search(c):
            if content_terms(c):
                out.append(c)
    return out


def grounded(claim: str, sources: str) -> bool:
    """这句经历在来源里有没有：至少一个“说的是哪件事”的词在来源里出现过。"""
    terms = content_terms(claim)
    return bool(terms) and any(t in sources for t in terms)


# ---- 问 / 叮嘱 ----
ASK_RX = re.compile(r"[？?]|(了吗|了没|没有|吗|呢|啥时候|几点|多少|咋样|怎么样|赢了没|打完了|结束了)$")
CARE_RX = re.compile(r"记得|别忘|注意|小心|多喝|早点睡|擦药|上药|别熬夜|好好休息|悠着点|慢点|别太累")
CLOSE_RX = re.compile(r"别问了|别再问|能别|别鞭尸|鞭尸|不想说|不想提|别提了|又问|问了好几遍|问过了|天天问|每天都要问|一天问|烦不烦|够了")

# ---- 好事 ----
GOOD_NEWS = re.compile(r"(过了|上岸|拿到|中了|考上|录取|offer|涨薪|升职|赢了|通过|发表|毕业了|领证|成功了|抢到|做完了|搞定了|好耶|终于)", re.I)
FLAT_PRAISE = re.compile(r"^(恭喜|厉害|牛|nb|好耶|太强了|可以啊|不错|666|强|绝了)[!！啊呀了哦~～。\s]*$", re.I)
TEASE_RX = re.compile(r"笑死|菜(鸡|狗)?|蠢|傻|活该|就这|丢人|废了|太逊|好惨|你也有今天|鞭尸|又输|又挂|不行啊|拉胯|下饭")
JUST_NOW = re.compile(r"刚(才|刚)?(说|问|讲|提|发)|你刚")


class ChecksMixin:
    def _checks_init(self):
        self.db.conn.executescript(SCHEMA)
        self.db.conn.commit()

    def check_mode(self, gid: int, name: str) -> str:
        v = str(self.gk(gid).get(f"check_{name}", "shadow") or "off").lower()
        return v if v in ("off", "shadow", "on") else "shadow"

    def check_log(self, gid: int, name: str, mode: str, text: str, detail: str, gate_id=None):
        try:
            self.db.conn.execute("INSERT INTO qq_check_log(ts, group_id, gate_id, name, mode, text, detail) VALUES (?,?,?,?,?,?,?)",
                                 (time.time(), gid, gate_id, name, mode, str(text)[:300], str(detail)[:300]))
            self.db.conn.commit()
        except Exception as e:  # noqa: BLE001
            log.debug("记检查失败：%s", e)

    def call_name(self, gid: int, uid) -> str:
        """平时怎么叫他：画像里的“称呼”第一个，没有就用群名片（太长、带符号的不叫）。"""
        p = self.person(int(uid), gid) or {}
        call = str((p.get("profile") or {}).get("称呼") or "")
        call = re.split(r"[；;，,/（(、\s]", call)[0].strip("“”\"' ") if call else ""
        if not call:
            from aigroup.core.safety import clean_name
            call = clean_name(self.names(gid).get(str(uid), ""))
        return call if 1 < len(call) <= 6 and CJK.fullmatch(call) else ""

    # ================= 来源 =================
    def own_sources(self, gid: int) -> str:
        """她自己的事能从哪里来：人设、今天的安排、长出来的自我、她以前在群里说过的（自述账本）。"""
        p = self.brain.persona
        parts = [str(getattr(p, "body", "") or ""), str(getattr(p, "identity", "") or ""),
                 str(self.db.get(f"life_{self.brain.now():%Y-%m-%d}", "") or ""),
                 str(self.db.get(f"qq_selfcore_{gid}") or "")]
        try:
            parts += [f["text"] for f in self.memory.facts(gid, subject=-1, limit=80)]
        except Exception:  # noqa: BLE001
            pass
        return "\n".join(parts)

    def persona_sources(self) -> str:
        p = self.brain.persona
        return "\n".join([str(getattr(p, "body", "") or ""), str(getattr(p, "identity", "") or ""),
                          str(self.db.get(f"life_{self.brain.now():%Y-%m-%d}", "") or "")])

    # ================= 问过账 =================
    def note_asks(self, gid: int, bubbles: list[str], target_uid, now: float | None = None):
        now = now or time.time()
        for b in bubbles:
            t = TAG.sub("", str(b or "")).strip()
            if not t:
                continue
            kind = "care" if CARE_RX.search(t) else ("ask" if any(ASK_RX.search(c.strip()) for c in re.split(r"[，,。！!]", t) if c.strip())
                                                       else "")
            terms = sorted(content_terms(t))
            if not kind or not terms:
                continue
            self.db.conn.execute("INSERT INTO qq_asks(ts, group_id, target_uid, kind, text, terms) VALUES (?,?,?,?,?,?)",
                                 (now, gid, target_uid, kind, t[:120], json.dumps(terms, ensure_ascii=False)))
        self.db.conn.commit()

    def recent_asks(self, gid: int, hours: float = 72, now: float | None = None) -> list[dict]:
        """她最近问过 / 叮嘱过的；顺便看问完以后有没有人说“别问了”（有就关上这件事 7 天）。"""
        now = now or time.time()
        days = float(self.gk(gid).get("ask_closed_days", 7) or 7)
        rows = [dict(r) for r in self.db.conn.execute(
            "SELECT * FROM qq_asks WHERE group_id=? AND (ts>? OR closed_until>?) ORDER BY id DESC LIMIT 60",
            (gid, now - hours * 3600, now)).fetchall()]
        for r in rows:
            if r["closed_until"]:
                continue
            after = self.db.conn.execute("SELECT content FROM qq_group_messages WHERE group_id=? AND role='user' AND ts>? AND ts<? "
                                         "ORDER BY id LIMIT 8", (gid, r["ts"], r["ts"] + 1800)).fetchall()
            if any(CLOSE_RX.search(str(a["content"] or "")) for a in after):
                r["closed_until"] = r["ts"] + days * 86400
                self.db.conn.execute("UPDATE qq_asks SET closed_until=? WHERE id=?", (r["closed_until"], r["id"]))
        self.db.conn.commit()
        for r in rows:
            try:
                r["terms"] = set(json.loads(r["terms"] or "[]"))
            except ValueError:
                r["terms"] = set()
        return rows

    def asks_block(self, gid: int) -> str:
        if self.check_mode(gid, "ask_repeat") == "off":
            return ""
        now = time.time()
        rows = self.recent_asks(gid, 48, now)
        names = self.names(gid)
        lines = []
        for r in rows[:8]:
            who = names.get(str(r["target_uid"]), "") if r["target_uid"] else ""
            ago = (now - r["ts"]) / 3600
            when = f"{int(ago)} 小时前" if ago >= 1 else "刚才"
            tag = "（对方说过别再问了）" if r["closed_until"] and r["closed_until"] > now else ""
            lines.append(f"- {when}{'对' + who if who else ''}{'问' if r['kind'] == 'ask' else '叮嘱'}：「{r['text'][:30]}」{tag}")
        if not lines:
            return ""
        return "你这两天问过 / 叮嘱过的（同一件事别再问第二遍；对方说过“别问了”的这周都别提）：\n" + "\n".join(lines)

    def _ask_repeat(self, gid: int, text: str, target_uid, now: float) -> str:
        if not (ASK_RX.search(text.strip()) or CARE_RX.search(text) or "？" in text or "?" in text):
            return ""
        terms = content_terms(text)
        if not terms:
            return ""
        hours = float(self.gk(gid).get("ask_repeat_hours", 24) or 24)
        for r in self.recent_asks(gid, max(hours, 72), now):
            common = terms & r["terms"]
            if not common:
                continue
            closed = r["closed_until"] and r["closed_until"] > now
            fresh = now - r["ts"] < hours * 3600
            same_kind = (r["kind"] == "care") == bool(CARE_RX.search(text))
            if closed or (fresh and same_kind and (len(common) >= 2 or (target_uid and r["target_uid"] == target_uid))):
                ago = (now - r["ts"]) / 3600
                why = "对方说过别再问了" if closed else (f"{int(ago)} 小时前" if ago >= 1 else "刚才") + "已经问过 / 叮嘱过"
                return f"这件事{why}（你那句是「{r['text'][:24]}」）：别再问 / 再叮嘱，换个话题或者不说"
        return ""

    # ================= 跨天套路 =================
    def her_templates(self, gid: int, days: float = 7, now: float | None = None) -> list[str]:
        """她这一周反复用的开头（≥3 次、≥2 天）和“改成 X 版 / 模式”这种句式。每小时算一次。"""
        now = now or time.time()
        ck = f"_tpl_{gid}"
        cache = getattr(self, "_tpl_cache", None)
        if not isinstance(cache, dict):
            cache = self._tpl_cache = {}
        hit = cache.get(ck)
        if hit and now - hit[0] < 3600:
            return hit[1]
        rows = self.db.conn.execute("SELECT ts, content FROM qq_group_messages WHERE group_id=? AND role='assistant' AND ts>?",
                                    (gid, now - days * 86400)).fetchall()
        heads: dict[str, set] = {}
        cnt: dict[str, int] = {}
        frames = 0
        frame_days: set = set()
        for r in rows:
            t = TAG.sub("", str(r["content"] or "")).strip()
            if not t or t.startswith("["):
                continue
            day = time.strftime("%Y-%m-%d", time.localtime(float(r["ts"])))
            head = re.split(r"[，,。！!？?\s~～]", t)[0].strip()
            if 2 <= len(head) <= 6:
                cnt[head] = cnt.get(head, 0) + 1
                heads.setdefault(head, set()).add(day)
            if re.search(r"(改成|换成|切换成|开启|进入).{0,8}(版|模式)", t):
                frames += 1
                frame_days.add(day)
        out = [h for h, n in sorted(cnt.items(), key=lambda x: -x[1]) if n >= 3 and len(heads[h]) >= 2][:6]
        if frames >= 3 and len(frame_days) >= 2:
            out.append("改成…版 / …模式")
        cache[ck] = (now, out)
        return out

    def _template_hit(self, gid: int, text: str, now: float) -> str:
        t = TAG.sub("", text).strip()
        for h in self.her_templates(gid, now=now):
            if h == "改成…版 / …模式":
                if re.search(r"(改成|换成|切换成|开启|进入).{0,8}(版|模式)", t):
                    return "又是“改成 X 版 / X 模式”这个套路（这周已经用了好几次，一听就是模板）：换个说法"
            elif t.startswith(h):
                return f"又用「{h}」开头（这周已经用了好几次）：换个开头"
        return ""

    # ================= 叫错人 / 时间感 =================
    def _addressee(self, gid: int, text: str, target_uid, target_row: dict | None) -> str:
        if not target_uid or not hasattr(self, "names_of"):
            return ""
        names = self.names(gid)
        mine = set(self.names_of(gid, int(target_uid)))
        recent = [r for r in self._rows(gid, 10) if r["role"] == "user"]
        recent_txt = " ".join(str(r["content"] or "") for r in recent)
        for uid in names:
            try:
                u = int(uid)
            except ValueError:
                continue
            if u == int(target_uid):
                continue
            for n in self.names_of(gid, u):
                if n and len(n) >= 2 and n not in mine and n in text[:12] and n not in recent_txt:
                    tn = names.get(str(target_uid), "他")
                    return f"叫错人了：你在回{tn}，嘴里叫的是「{n}」"
        if target_row and target_row.get("content"):
            tc = str(target_row["content"])
            for uid in names:
                if str(uid) in (str(target_uid),):
                    continue
                for n in self.names_of(gid, int(uid)):
                    if n and len(n) >= 2 and re.match(rf"^\s*(@)?{re.escape(n)}", tc) and re.search(r"[？?吗呢啥咋]", tc):
                        return f"那句是{names.get(str(target_uid), '他')}在问{n}，不是问你：别抢着答"
        return ""

    def _time_sense(self, text: str, target_row: dict | None, now: float) -> str:
        if not target_row or not JUST_NOW.search(text):
            return ""
        age = now - float(target_row.get("ts") or now)
        if age >= 3600:
            return f"你接的那句是 {int(age / 3600)} 个多小时以前的：别说“刚说 / 刚问”，可以说“早上 / 之前说的”"
        return ""

    # ================= 损人 / 回应性 / 热聊 =================
    def _tease_risk(self, gid: int, text: str, target_uid) -> str:
        if not target_uid or not TEASE_RX.search(text):
            return ""
        uid = int(target_uid)
        if self.owner_id and uid == self.owner_id:
            score = 0.0
        else:
            close = self.closeness(uid) if hasattr(self, "closeness") else 30.0
            score = 0.0 if close >= 40 else (1.0 if close >= 12 else 2.0)     # 距离：很熟 0、挺熟 1、刚认识 2
        stt = self.db.get(f"qq_state_{gid}_{uid}") or {}
        if stt.get("date") == f"{self.brain.now():%Y-%m-%d}" and stt.get("mood") in ("不太好", "很糟"):
            score += 2.0                                                       # 他今天状态不好
        cold = self.db.conn.execute(
            "SELECT COUNT(*) n, SUM(CASE WHEN score<0.2 THEN 1 ELSE 0 END) c FROM qq_reply_effect WHERE group_id=? AND "
            "target_uid=? AND done=1 AND ts>?", (gid, uid, time.time() - 14 * 86400)).fetchone()
        if cold and int(cold["n"] or 0) >= 4 and int(cold["c"] or 0) / int(cold["n"]) >= 0.6:
            score += 1.0                                                       # 她对他说的话多半没人接
        recent = " ".join(str(r["content"] or "") for r in self._rows(gid, 8) if r["role"] == "user" and int(r["user_id"] or 0) == uid)
        if CLOSE_RX.search(recent) or re.search(r"别(说|损|笑)了|生气|难受|烦死|不好笑", recent):
            score += 2.0                                                       # 他刚表示不爽
        if score >= 2.0:
            return f"这时候别损{self.names(gid).get(str(uid), '他')}（不够熟 / 他状态不好 / 他刚不爽）：换成接住或者关心"
        return ""

    def _responsive(self, text: str, target_row: dict | None) -> str:
        if not target_row or target_row.get("role") != "user":
            return ""
        tc = TAG.sub("", str(target_row.get("content") or ""))
        if GOOD_NEWS.search(tc) and FLAT_PRAISE.match(TAG.sub("", text).strip()):
            return "他在分享好事：别只回一句恭喜 / 厉害，追问一个具体的细节（几分？哪家？怎么做到的？）"
        return ""

    def room_hot(self, gid: int, now: float | None = None) -> str:
        """两个人正一来一回聊得快（最近 8 条里只有这两个人、交替 ≥4 次、间隔都在 40 秒内、没人叫她）。"""
        now = now or time.time()
        rows = self._rows(gid, 8)
        if len(rows) < 6 or any(r["role"] != "user" for r in rows[-6:]):
            return ""
        last = rows[-6:]
        if now - float(last[-1]["ts"]) > 90 or any(int(r["to_her"] or 0) for r in last):
            return ""
        who = [int(r["user_id"] or 0) for r in last]
        if len(set(who)) != 2:
            return ""
        switches = sum(1 for a, b in zip(who, who[1:]) if a != b)
        gaps = [float(b["ts"]) - float(a["ts"]) for a, b in zip(last, last[1:])]
        if switches >= 4 and max(gaps) <= 40:
            names = self.names(gid)
            return f"{names.get(str(who[0]), '他')}和{names.get(str(who[1]), '他')}正一来一回聊得快"
        return ""

    # ================= 汇总 =================
    async def checks22(self, gid: int, bubbles: list[str], *, target_uid=None, target_row: dict | None = None,
                       mentioned: bool = False, gate_id=None) -> list[str]:
        """返回要交给重写的问题（只包含开关是 on 的）；shadow 的只记录。"""
        if not bubbles:
            return []
        text = " ".join(str(b) for b in bubbles if b and not str(b).startswith("[表情包"))
        if not text.strip():
            return []
        now = time.time()
        found: list[tuple[str, str]] = []
        mode = {c: self.check_mode(gid, c) for c in CHECKS}
        claims = self_claims(text) if (mode["source_gate"] != "off" or mode["self_consistency"] != "off") else []
        if claims and mode["source_gate"] != "off":
            p = self.brain.persona
            offline_free = bool(getattr(p, "ai_open", False)) and str(getattr(p, "life", "") or "free") == "free"
            srcs = self.persona_sources() if offline_free else self.own_sources(gid)
            bad = [c for c in claims if not grounded(c, srcs)]
            if bad:
                found.append(("source_gate", f"编了你自己没有的经历（「{bad[0][:24]}」）：你的设定、今天的安排、你以前说过的里都没有这件事。"
                                             "别编自己的生活细节；想接就说你真有的，或者把话题放回对方身上"))
        if claims and mode["self_consistency"] != "off" and not any(n == "source_gate" for n, _ in found):
            why = await self._consistency(gid, claims)
            if why:
                found.append(("self_consistency", f"和你以前说过的对不上（{why}）：照以前说的来，真变了就说“后来改了”"))
        if mode["ask_repeat"] != "off":
            w = self._ask_repeat(gid, text, target_uid, now)
            if w:
                found.append(("ask_repeat", w))
        if mode["template"] != "off":
            w = self._template_hit(gid, str(bubbles[0]), now)
            if w:
                found.append(("template", w))
        if mode["addressee"] != "off":
            w = self._addressee(gid, text, target_uid, target_row if not mentioned else None)
            if w:
                found.append(("addressee", w))
        if mode["time_sense"] != "off":
            w = self._time_sense(text, target_row, now)
            if w:
                found.append(("time_sense", w))
        if mode["tease_risk"] != "off":
            w = self._tease_risk(gid, text, target_uid)
            if w:
                found.append(("tease_risk", w))
        if mode["responsive"] != "off":
            w = self._responsive(text, target_row)
            if w:
                found.append(("responsive", w))
        out = []
        for name, why in found:
            self.check_log(gid, name, mode[name], text, why, gate_id)
            if mode[name] == "on":
                out.append(why)
            else:
                log.info("群 %s 检查（只记录）%s：%s", gid, name, why)
        return out

    async def _consistency(self, gid: int, claims: list[str]) -> str:
        """问一次便宜模型：这几句和她以前说过的自己的事有没有矛盾。没有自述账本就不问。"""
        try:
            led = [f["text"] for f in self.memory.facts(gid, subject=-1, limit=40)]
        except Exception:  # noqa: BLE001
            led = []
        terms = set().union(*(content_terms(c) for c in claims)) if claims else set()
        rel = [x for x in led if any(t in x for t in terms)][:12]
        if not rel:
            return ""
        prompt = ("下面是一个人以前说过的关于她自己的事，和她现在要说的话。现在要说的话里，有没有和以前说过的事实**直接矛盾**的"
                  "（比如以前说不喝咖啡、现在说天天喝；以前说在北京、现在说在上海）？心情、看法随口变化不算。\n"
                  "只输出 JSON：{\"conflict\": true/false, \"why\": \"矛盾在哪，一句话\"}\n\n"
                  "# 以前说过的\n" + "\n".join(f"- {x}" for x in rel) + "\n\n# 现在要说的\n" + "\n".join(f"- {c}" for c in claims))
        try:
            out = await self.brain.llm.chat([{"role": "user", "content": prompt}], role="gate", temperature=0.0,
                                            max_tokens=120, json_mode=True)
            d = json.loads(re.search(r"\{.*\}", out or "", re.S).group(0))
        except Exception:  # noqa: BLE001
            return ""
        return str(d.get("why") or "前后不一致")[:40] if d.get("conflict") is True else ""
