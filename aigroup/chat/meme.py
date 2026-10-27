"""26.0 先读懂 + 查梗。

10-27 复盘（项目文档《调研-10-27-AI味从哪来-热梗-分析判断》）：
- 同一个模型单独问“压力一个 ai 是什么意思”“你手疼不疼什么意思”都答得对；放进小满的流程就按字面接了。
  主提示词二十多段、聊天记录 30~44 条，梗只是其中一行，没有专门“读懂”这一步。
- 黑话表 101 个词有 92 个从没猜过意思：计数到 4 才猜，小群里大多停在 1~2 次；平时碰到不懂的词也不会去查。
- 依据：CIBuzzBench（3001 个中文网络梗）：错误大多是按字面、或理解成意思相近但用法不对；
  Cheer / RESS（ACL 2025）：没见过的梗准确率掉 33%，只靠上下文猜也有限 → 不确定就查，别猜。

做法：
1. read_turn：她要接话前，用便宜模型把最新几句群友的话读一遍（实际想说什么、语气、冲谁、里面的梗和把握），
   结果放进主提示词（“刚才这几句的意思”）和发前自检；和“想一想”同时跑，不多等。
2. 把握不够的词（或者没查过的梗）放进后台 meme_lookup：中文搜索“X 是什么梗”，再让模型照搜到的写意思；
   查到的写进 qq_jargon（is_jargon=2 = 网络梗，src=web），以后聊到就带上；查不到也记一笔，七天内不再查。
3. 拿不准的词告诉她：别硬接，可以像真人一样问“这啥梗”，或者只接确定的部分。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time

from aigroup.chat.address import TAG
from aigroup.core.store import ensure_columns
from aigroup.llm.client import parse_json

log = logging.getLogger("aigroup.meme")

CUE = re.compile(r"有懂的|懂的都懂|啥梗|什么梗|何意味|是什么意思|啥意思")
LITERAL = re.compile(r"^(字面|字面意思|就是字面|同上|就是这个意思)")


def clean_term(t: str) -> str:
    t = re.sub(r"[「」“”\"'《》\s]", "", str(t or ""))
    return t[:16]


def parse_read(d) -> dict:
    """模型读出来的 JSON → {lines: [...], memes: [...]}，缺什么补什么。"""
    d = d if isinstance(d, dict) else {}
    lines = [x for x in (d.get("lines") or []) if isinstance(x, dict)][:6]
    memes = []
    for m in d.get("memes") or []:
        if not isinstance(m, dict):
            continue
        term = clean_term(m.get("term"))
        if len(term) < 1:
            continue
        try:
            sure = float(m.get("sure", 0) or 0)
        except (TypeError, ValueError):
            sure = 0.0
        kind = str(m.get("kind") or "")
        if kind and re.search(r"普通|字面|日常", kind):          # 27.0 10-27“看个眼睛”“不见了四百块”被拿去查：普通说法不列
            continue
        memes.append({"term": term, "means": str(m.get("means") or "")[:50], "sure": max(0.0, min(1.0, sure)), "kind": kind})
    return {"lines": lines, "memes": memes[:5]}


def read_text(parsed: dict, known: dict[str, str], sure_min: float = 0.7) -> tuple[str, list[str]]:
    """给她看的一段 + 要去查的词。字面意思、语气认真的句子不写（省地方）。"""
    out, unsure = [], []
    for ln in parsed["lines"]:
        means = str(ln.get("means") or "").strip()
        tone = str(ln.get("tone") or "").strip()
        if (not means or LITERAL.match(means)) and tone in ("", "认真"):
            continue
        who, txt = str(ln.get("who") or "").strip(), str(ln.get("text") or "").strip()[:24]
        head = f"{who}「{txt}」" if txt else (who or "这句")
        out.append(f"- {head}：{(means or '字面意思')[:60]}" + (f"（{tone}）" if tone and tone != "认真" else ""))
    out = out[-4:]                                  # 最新的几句就够了
    for m in parsed["memes"][:4]:
        if m["term"] in known:
            out.append(f"- 「{m['term']}」：{known[m['term']]}（查过）")
        elif m["sure"] >= sure_min:
            out.append(f"- 「{m['term']}」：{m['means']}")
            if not re.fullmatch(r"[A-Za-z0-9 .\-]+", m["term"]):
                unsure.append(m["term"])          # 有把握也后台核一下（英文缩写多是战队、选手名，不查）
        else:
            out.append(f"- 「{m['term']}」你拿不准是什么意思" + (f"（猜是：{m['means']}）" if m["means"] else "")
                       + "：别硬接这个词，也别编它的意思；可以像真人一样问一句“这啥梗”，或者只接你确定的部分")
            if not re.fullmatch(r"[A-Za-z0-9 .\-]+", m["term"]):
                unsure.append(m["term"])
    return "\n".join(out), unsure


class MemeMixin:
    def _meme_init(self):
        ensure_columns(self.db.conn, "qq_jargon", {"src": "TEXT NOT NULL DEFAULT ''"})
        self._read_txt: dict[int, str] = {}
        self._meme_running: set[str] = set()

    def _known_memes(self, gid: int, terms: list[str]) -> dict[str, str]:
        out = {}
        for t in terms:
            r = self.db.conn.execute("SELECT meaning FROM qq_jargon WHERE group_id=? AND term=? AND meaning!='' "
                                     "AND (src='web' OR status='ok')", (gid, t)).fetchone()
            if r:
                out[t] = str(r["meaning"])
        return out

    async def read_turn(self, gid: int, mentioned: bool = False) -> str:
        """最新几句群友的话（她上次说话以后）：读懂再接。结果存在 self._read_txt[gid]。"""
        if not hasattr(self, "_read_txt"):
            self._meme_init()
        self._read_txt.pop(gid, None)
        rows = list(self._rows(gid, 12))
        new = []
        for r in reversed(rows):
            if r["role"] != "user":
                break
            new.insert(0, r)
        new = new[-5:]
        if not new or not self._allow(f"read_{gid}", int(self.gk(gid).get("read_per_hour", 60) or 0)):
            return ""
        names = self.names(gid)
        lab = self._speaker_labels(gid, rows) if hasattr(self, "_speaker_labels") else {}
        ids = {int(r["id"]) for r in new}

        def who(r):
            return "你" if r["role"] == "assistant" else (lab.get(int(r["user_id"] or 0)) or names.get(str(r["user_id"]), r["name"]))
        ctx = "\n".join(f"{'▶ ' if int(r['id']) in ids else ''}{who(r)}：{TAG.sub('', str(r['content'] or ''))[:140]}" for r in rows)
        prompt = (f"下面是一个朋友群的聊天记录（最后几条最新，“你”是群里的 AI 群友{self.brain.persona.name}）。"
                  "把标了 ▶ 的几句群友的话读懂：\n"
                  "- 每句：他实际想说什么（就是字面意思就写“字面意思”）、语气（认真 / 玩笑 / 反话 / 调侃 / 吐槽 / 抬杠 / 追问 / 夸张）\n"
                  "- ▶ 那几句里有网络梗、圈内黑话、外号、缩写、谐音、反话的词（只看 ▶ 的句子）：这个词在这里是什么意思、你有几成把握（0~1）。"
                  "不知道就老实写低把握，别编；普通词、人名、战队名不用列\n\n"
                  f"{ctx}\n\n"
                  '只输出 JSON：{"lines": [{"who": "谁", "text": "原话前十几个字", "means": "实际想说的", "tone": "语气"}], '
                  '"memes": [{"term": "词", "kind": "网络梗 / 圈内黑话 / 外号 / 缩写 / 谐音 / 反话 / 普通说法", "means": "在这里的意思", "sure": 0.5}]}')
        try:
            out = await asyncio.wait_for(self.brain.llm.chat([{"role": "user", "content": prompt}], role="gate", temperature=0.0,
                                                             max_tokens=700, json_mode=True), 8)
            d = parse_json(out, {})
        except Exception as e:  # noqa: BLE001
            log.info("群 %s：读这几句失败：%s", gid, e)
            return ""
        parsed = parse_read(d)
        known = self._known_memes(gid, [m["term"] for m in parsed["memes"]])
        txt, unsure = read_text(parsed, known, float(self.gk(gid).get("meme_sure", 0.7) or 0.7))
        if txt:
            self._read_txt[gid] = txt
            log.info("群 %s 读懂：%s", gid, txt.replace("\n", " ")[:200])
        if unsure and self.gk(gid).get("meme_lookup", True):
            sample = " ".join(TAG.sub("", str(r["content"] or ""))[:60] for r in new)
            for t in unsure[:2]:
                if t not in self._meme_running:
                    asyncio.ensure_future(self.meme_lookup(gid, t, sample))
        return txt

    async def meme_lookup(self, gid: int, term: str, example: str = "") -> str:
        """中文搜“X 是什么梗”，照搜到的写意思，存进黑话表。返回意思（没查到返回空）。"""
        if not hasattr(self, "_read_txt"):
            self._meme_init()
        term = clean_term(term)
        if len(term) < 1 or term in self._meme_running:
            return ""
        row = self.db.conn.execute("SELECT meaning, src, updated FROM qq_jargon WHERE group_id=? AND term=?",
                                   (gid, term)).fetchone()
        now = time.time()
        if row and row["src"] == "web" and row["meaning"]:
            return str(row["meaning"])
        if row and row["src"] == "web-none" and now - float(row["updated"] or 0) < 7 * 86400:
            return ""
        key = f"meme_n_{self.brain.now():%Y-%m-%d}"
        if int(self.db.get(key, 0) or 0) >= int(self.gk(gid).get("meme_per_day", 30) or 0):
            return ""
        web = getattr(self, "web", None)
        if web is None or not getattr(web, "enabled", True):
            return ""
        self._meme_running.add(term)
        try:
            self.db.set(key, int(self.db.get(key, 0) or 0) + 1)
            results = []
            for q in (f"{term} 是什么梗", f"{term} 梗 意思 出处"):
                try:
                    got, _eng = await web.agent_search(q, n=6, prefer="deepseek", question=f"网络用语「{term}」是什么意思")
                except Exception as e:  # noqa: BLE001
                    log.info("查梗「%s」搜索失败：%s", term, e)
                    got = []
                results += got or []
                if len(results) >= 4:
                    break
            src = "\n".join(f"- {r.get('title', '')}：{str(r.get('snippet', ''))[:200]}" for r in results[:8])
            if not src:
                self._save_meme(gid, term, "", "web-none", example)
                return ""
            prompt = (f"群友聊天里用了「{term}」，原话：{example[:120]}\n\n下面是搜「{term} 是什么梗」看到的：\n{src}\n\n"
                      "只照上面搜到的写（搜到的没讲清就写 known=false，别凭印象编）：它是不是一个网络梗 / 圈内黑话，"
                      "意思（30 字以内）、一般怎么用（20 字以内）。\n"
                      '只输出 JSON：{"known": true, "meme": true, "means": "...", "usage": "...", "sure": 0.8}')
            d = await self.brain.llm.json([{"role": "user", "content": prompt}], default={}, role="helper")
            d = d if isinstance(d, dict) else {}
            if d.get("known") is not True or not str(d.get("means") or "").strip():
                self._save_meme(gid, term, "", "web-none", example)
                log.info("查梗「%s」：搜到的没讲清", term)
                return ""
            try:
                sure = float(d.get("sure", 0.6) or 0.6)
            except (TypeError, ValueError):
                sure = 0.6
            means = str(d.get("means")).strip()[:40]
            usage = str(d.get("usage") or "").strip()[:24]
            meaning = means + (f"（{usage}）" if usage else "")
            self._save_meme(gid, term, meaning, "web", example, is_meme=bool(d.get("meme", True)) and sure >= 0.5)
            log.info("查梗「%s」：%s", term, meaning)
            return meaning
        except Exception as e:  # noqa: BLE001
            log.info("查梗「%s」出错：%s", term, e)
            return ""
        finally:
            self._meme_running.discard(term)

    def _save_meme(self, gid: int, term: str, meaning: str, src: str, example: str = "", is_meme: bool = True):
        now = time.time()
        row = self.db.conn.execute("SELECT id, examples FROM qq_jargon WHERE group_id=? AND term=?", (gid, term)).fetchone()
        flag = 2 if (meaning and is_meme) else (0 if meaning else -1)
        if row:
            self.db.conn.execute("UPDATE qq_jargon SET meaning=?, src=?, is_jargon=CASE WHEN ?=-1 THEN is_jargon ELSE ? END, "
                                 "updated=? WHERE id=?", (meaning, src, flag, flag, now, row["id"]))
        else:
            ex = json.dumps([example[:80]] if example else [], ensure_ascii=False)
            self.db.conn.execute("INSERT INTO qq_jargon(group_id, term, count, meaning, examples, is_jargon, src, created, updated) "
                                 "VALUES (?,?,?,?,?,?,?,?,?)", (gid, term, 1, meaning, ex, flag, src, now, now))
        self.db.conn.commit()
