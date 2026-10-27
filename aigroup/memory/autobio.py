"""22.0 / 22.1 她自己的长期记忆：自述账本、立场账、群时间线、走样处方。

- 自述账本（self_ledger）：每晚整理时“我|…”那几行除了覆盖 qq_self_notes（最多 8 行），还一条条存进 qq_facts
  （subject=-1、kind=experience），只增不删；提示词里按这一轮在聊的事挑相关的几条，和人设、今天的安排一起当她自己的事的来源。
- 立场账（stances）：她表过的态（“我站 AG”“香菜不行”）存 kind=opinion，聊到同一个话题时提醒她别前后打架。
- 群时间线（timeline）：每晚整理出“哪天群里发生了什么”（kind=event，t_valid=那天），最近一周放进提示词，
  更早的按关键词想起来（“上周我们干嘛了”能答上）。
- 走样处方（drift_rx）：每周评测里最差的几句 + 你打了 1~2 分的几句，让模型写成 3 条具体的“别……，要……”，
  下一周放进提示词（Persona drift 研究：具体指令比重贴人设有效得多）。
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timedelta

log = logging.getLogger("aigroup.group")

EXTRA_PROMPT = (
    "10. 态|话题|你的看法：你（“你”）在聊天里明确表过的态度（站哪队、喜欢不喜欢什么、觉得某事怎么样），每条 30 字以内，最多 3 条，没有就不写。\n"
    "11. 时|MM-DD|事：这段聊天里群里真实发生的、以后可能会被提起的事（谁考完了、约了什么、一起看了什么比赛、谁生病了），"
    "日期写那件事发生的那天，每条 30 字以内，最多 4 条；只写群友身上真实发生的，不写你自己编的、不写玩笑。"
)


def _terms(text: str) -> set[str]:
    from aigroup.chat.checks import content_terms
    return content_terms(text)


# ---- 30.0 从 diary.py 并进来的（24.0 两样从 DSH“养鱼”项目里学来的：） ----
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


class AutobioMixin:
    # ================= 整理时多写两种行 =================
    def autobio_prompt(self) -> str:
        return EXTRA_PROMPT

    def autobio_apply(self, gid: int, lines: list[str], mine: list[str]) -> tuple[int, int, int]:
        """整理输出里：我|（自述）、态|（立场）、时|（时间线）。返回 (自述, 立场, 事件) 各写了几条。"""
        g = self.gk(gid)
        ns = nt = ne = 0
        if g.get("self_ledger", True):
            for x in mine[:8]:
                if self.memory.add_fact(gid, x, subject=-1, kind="experience", source="said") and self.memory.last_new:
                    ns += 1
        if g.get("stances", True):
            for ln in lines:
                m = re.match(r"^态\s*[|｜]\s*([^|｜]{1,20})\s*[|｜]\s*(.{2,60})$", ln)
                if not m:
                    continue
                topic = m.group(1).strip()
                # 24.0 同一件事的立场只留最新的一条（“TES 不押”和“站 TES”以前同时有效）：同话题的旧立场标失效
                old = [f["id"] for f in self.memory.facts(gid, subject=-1, limit=200)
                       if f.get("kind") == "opinion" and str(f.get("text") or "").split("：")[0].strip() == topic]
                if self.memory.add_fact(gid, f"{topic}：{m.group(2).strip()}", subject=-1, kind="opinion",
                                        source="said", replaces=old) and self.memory.last_new:
                    nt += 1
        if g.get("timeline", True):
            now = self.brain.now()
            for ln in lines:
                m = re.match(r"^时\s*[|｜]\s*(\d{1,2})-(\d{1,2})\s*[|｜]\s*(.{3,60})$", ln)
                if not m:
                    continue
                try:
                    day = now.replace(month=int(m.group(1)), day=int(m.group(2)), hour=12, minute=0, second=0, microsecond=0)
                except ValueError:
                    continue
                if day.date() > now.date():                # 跨年：12 月的事 1 月整理（25.0：上午整理今天的事不算“将来”）
                    day = day.replace(year=day.year - 1)
                if (now - day).days > 30:
                    continue
                if (self.memory.add_fact(gid, m.group(3).strip(), subject=0, kind="event", now=day.timestamp(), source="said")
                        and self.memory.last_new):
                    ne += 1
        return ns, nt, ne

    # ================= 提示词里的几段 =================
    def self_ledger_lines(self, gid: int, query: str, k: int = 6) -> list[str]:
        """自述账本里和这一轮在聊的事相关的几条（没有相关的就最近的几条）。"""
        if not self.gk(gid).get("self_ledger", True):
            return []
        rows = [f for f in self.memory.facts(gid, subject=-1, limit=120) if f.get("kind") == "experience"]
        if not rows:
            return []
        q = _terms(query)
        scored = sorted(rows, key=lambda f: (-len(q & _terms(f["text"])), -float(f.get("created") or 0)))
        pick = [f for f in scored if q & _terms(f["text"])][:k] or rows[:min(3, k)]
        return [f"- （{datetime.fromtimestamp(f['created'], self.cfg.tz):%m-%d}）{f['text']}" for f in pick]

    def stance_lines(self, gid: int, query: str, k: int = 4) -> list[str]:
        if not self.gk(gid).get("stances", True):
            return []
        rows = [f for f in self.memory.facts(gid, subject=-1, limit=80) if f.get("kind") == "opinion"]
        q = _terms(query)
        hit = [f for f in rows if q & _terms(f["text"])][:k]
        return [f"- {f['text']}" for f in hit]

    def autobio_block(self, gid: int, query: str) -> str:
        """每次都变的一段（放 user）：和这轮相关的、你以前说过的自己的事 + 表过的态。"""
        out = []
        sl = self.self_ledger_lines(gid, query)
        if sl:
            out.append("# 你以前在群里说过的自己的事（和这些保持一致；这里没有、设定里也没有的经历别编）\n" + "\n".join(sl))
        st = self.stance_lines(gid, query)
        if st:
            out.append("# 你表过的态（别前后打架；真改主意了就直说“我后来改了”）\n" + "\n".join(st))
        return "\n\n".join(out)

    def timeline_block(self, gid: int, days: int = 7) -> str:
        """一天才变一次（放 system）：最近一周群里发生的事，按时间排。"""
        if not self.gk(gid).get("timeline", True):
            return ""
        since = time.time() - days * 86400
        rows = self.db.conn.execute("SELECT text, t_valid FROM qq_facts WHERE group_id=? AND kind='event' AND t_valid>? "
                                    "AND (t_invalid IS NULL OR t_invalid>?) ORDER BY t_valid LIMIT 12",
                                    (gid, since, time.time())).fetchall()
        if not rows:
            return ""
        return ("# 这一周群里发生的事（按时间；别人问“上次 / 前几天”时照这个说，别记混日子）\n"
                + "\n".join(f"- {datetime.fromtimestamp(r['t_valid'], self.cfg.tz):%m-%d}：{r['text']}" for r in rows))

    # ================= 走样处方 =================
    async def make_drift_rx(self, items: list[dict] | None = None) -> list[str]:
        """每周一次：最差的几句 → 3 条具体的“别……，要……”（存 kv drift_rx，下一周放进提示词）。"""
        if not self.g.get("drift_rx", True):
            return []
        bad = []
        for x in items or []:
            if float(x.get("score") or 100) < 60:
                bad.append(f"- 「{str(x.get('said') or '')[:60]}」：" + "；".join(
                    f"{d.get('type')}（{str(d.get('why') or '')[:30]}）" for d in (x.get("defects") or [])[:2]))
        try:
            for r in self.db.conn.execute("SELECT said, context FROM qq_human_score WHERE score<=2 ORDER BY id DESC LIMIT 8"):
                bad.append(f"- 「{str(r['said'])[:60]}」：群主打了低分")
        except Exception:  # noqa: BLE001
            pass
        if len(bad) < 2:
            return []
        p = self.brain.persona
        prompt = (f"{p.name}是群里的一个群友（人设摘要：{str(getattr(p, 'identity', '') or '')[:80]}）。下面是她这周说得最不像她、"
                  "或者被人嫌弃的几句和毛病。写 3 条给她下周看的、具体的改法，每条一句，格式“别……，要……”，"
                  "要具体到场景和说法（比如“别在别人没问时编自己上课交稿的事，要把话题放回对方身上”），不要空话。\n"
                  "只输出 JSON：{\"rx\": [\"…\", \"…\", \"…\"]}\n\n" + "\n".join(bad[:12]))
        try:
            out = await self.brain.llm.chat([{"role": "user", "content": prompt}], role="memory", background=True, json_mode=True)
            rx = [str(x).strip()[:80] for x in (json.loads(re.search(r"\{.*\}", out or "", re.S).group(0)).get("rx") or [])
                  if str(x).strip()][:3]
        except Exception as e:  # noqa: BLE001
            log.info("走样处方没写成：%s", e)
            return []
        if rx:
            self.db.set("drift_rx", {"rx": rx, "ts": time.time()})
            log.info("走样处方：%s", " / ".join(rx))
        return rx

    def drift_rx_block(self) -> str:
        if not self.g.get("drift_rx", True):
            return ""
        d = self.db.get("drift_rx") or {}
        rx = d.get("rx") if isinstance(d, dict) else None
        if not rx or time.time() - float(d.get("ts") or 0) > 14 * 86400:
            return ""
        return "# 上周你走样的地方（这周照着改）\n" + "\n".join(f"- {x}" for x in rx)

    # ---------------- 30.0 并进来的：原来的 DiaryMixin（diary.py） ----------------
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
