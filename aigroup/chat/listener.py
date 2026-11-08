"""21.1 独立的“听者”：她没人叫、自己想插话时，另一个不带人设的便宜模型看聊天和她写好的那句，判断“插在这里合不合适”。

为什么不让她自己判（21.0 的九问自评）：
- 线上 10 次自评 10 次“现在说”；自评置信度辨别力 AUROC 0.545，独立模型评审 0.832（NeurIPS 2026，16 个模型）
- PITCH（2026-10）：模型边聊边决定时最佳时机命中 19%、46% 太早，理由 93% 只说“相关”——问题要问“窗口”，不是“相关”
- PoLL（2024）：几个小模型组成评审团比单个大模型更接近人、更便宜、没有偏袒自己

流程：确定信号（程序算：他还在连发吗、被问的人回了吗、是不是两个人在聊、她最近占多少）+ 最近 12 条（带“多少秒前”）+ 她的草稿
+ 你判过的例子 → {"window": now|ok|early|never, "until", "issues", "addressee", "why"}。
上线方式（listener_mode）：shadow 只记不拦；on 直接管；auto（默认）先只记，你的标注里“时机不对 / 该接”各 ≥ 15 条、
听者和你一致率 ≥ listener_agree（0.7）以后自动接管。
"""
from __future__ import annotations

import json
import logging
import re
import time

log = logging.getLogger("aigroup.group")

WINDOWS = ("now", "ok", "early", "never")
CN_WIN = {"正好": "now", "现在": "now", "可以": "ok", "太早": "early", "等等": "early", "不该说": "never", "不说": "never"}

PROMPT = """你在旁边看一个微信群聊天，帮一个叫“{name}”的群友判断：她想说的这句，插在最后一条消息后面合不合适。
真人在群里大多数时候不说话；同一句话，早了会打断别人、晚了就冷了。你不是在判断这句话相不相关，而是在判断**时机**：
- 正好（now）：有人在等回应、话题停在一个能接的口子上、她接一句明显让聊天更好
- 可以（ok）：插进去不突兀，但不说也完全没问题（真人这时候多半不说）
- 太早（early）：问的是别人而那人还没回、两个人正一来一回聊着而且跟她无关、话题还没到她能接的地方
  （只是对方刚连发了几条不算太早：程序已经等他说完了；对方在跟她说话、在等她回应时更不算）
- 不该说（never）：跟她无关、会打断、重复别人、说了会冷场或让人尴尬
另外顺手看这句有没有毛病（没有就空着）：说漏了别人私下的事或幕后的事；让人当众下不来台（纠错、拒绝、损人却没给台阶）；
只是顺着别人说；把别人刚说的再说一遍；跟她最近的接法一样（{recent_acts}）；这句明显是在接别人的话却当成冲她来的。

程序看到的情况：
{signals}

{shots}最近的聊天（[多少秒前] 谁：说了什么）：
{chat}

她想说：{draft}

只输出 JSON：{{"window": "now/ok/early/never", "until": "太早的话要等到什么时候：他说完 / 被问的人回了 / 群里停一下 / 空", "addressee": "最后一条是在对谁说（她 / 某某 / 大家）", "issues": ["毛病，一条一句，没有就空数组"], "why": "一句理由"}}"""


def signals(rows: list[dict], her_name: str, now: float, her_share: float, names: list[str] | None = None) -> dict:
    """程序算的确定信号（rows：最近的消息，旧 → 新，每条 {ts, role, name, uid, content}）。"""
    users = [r for r in rows if r.get("role") != "assistant"]
    out = {"secs_since_last": int(now - float(rows[-1]["ts"])) if rows else 9999, "burst": 0, "pending_q": "",
           "two_person": False, "her_share": round(her_share, 2)}
    if users:
        last = users[-1]
        n = 0
        for r in reversed(rows):
            if r.get("role") == "assistant" or r.get("uid") != last.get("uid") or float(last["ts"]) - float(r["ts"]) > 60:
                break
            n += 1
        out["burst"] = n if now - float(last["ts"]) < 20 else 0
        txt = str(last.get("content") or "")
        if re.search(r"[？?]|吗|呢$|啥|什么|怎么", txt):
            who = next((nm for nm in (names or []) if nm and nm != last.get("name") and nm in txt), "")
            if (who or "@" in txt) and her_name not in txt:
                out["pending_q"] = f"{last.get('name')}在问{who or '别人'}，还没回"
        tail = users[-6:]
        speakers = {r.get("uid") for r in tail}
        if len(tail) >= 4 and len(speakers) == 2 and not any(her_name in str(r.get("content") or "") for r in tail):
            out["two_person"] = True
    return out


def signals_text(s: dict) -> str:
    """21.1.1：只写事实，不替听者下结论。回放你的 45 条标注时发现：写“可能还没说完”“她占比高”会把听者推向“太早”，
    该接的也判成太早（一致率 58%）；程序在问听者之前已经等过连发说完（merge_wait_seconds）。"""
    lines = [f"- 最后一条是 {s['secs_since_last']} 秒前"]
    if s.get("burst", 0) >= 3 and s.get("secs_since_last", 99) < 8:
        lines.append(f"- 最后说话的人刚连发了 {s['burst']} 条")
    if s.get("pending_q"):
        lines.append(f"- {s['pending_q']}")
    if s.get("two_person"):
        lines.append("- 最近几条只有两个人在一来一回地聊（她不在其中）")
    if s.get("her_share", 0) >= 0.35:
        lines.append(f"- 她最近 30 分钟占了群里 {int(s['her_share'] * 100)}% 的消息，有点多")
    return "\n".join(lines)


def parse_listen(out: str) -> dict:
    s = (out or "").strip()
    m = re.search(r"\{.*\}", s, re.S)
    d: dict = {}
    if m:
        try:
            d = json.loads(m.group(0))
        except ValueError:
            d = {}
    w = str(d.get("window") or "").strip().lower()
    w = CN_WIN.get(w, w)
    if w not in WINDOWS:
        w = next((v for k, v in CN_WIN.items() if k in s), "") or next((x for x in WINDOWS if x in s.lower()), "ok")
    issues = d.get("issues") if isinstance(d.get("issues"), list) else []
    issues = [str(x).strip()[:60] for x in issues if str(x).strip() and str(x).strip() not in ("无", "没有", "空")][:3]
    until = str(d.get("until") or "").strip()
    until = "" if until in ("空", "无", "没有", "null", "None") else until
    return {"window": w, "until": until[:30], "addressee": str(d.get("addressee") or "")[:20],
            "issues": issues, "why": str(d.get("why") or "")[:100]}


def merge_panel(votes: list[dict]) -> dict:
    """评审团：意见不一时宁可晚（取最保守的窗口），毛病合起来。"""
    votes = [v for v in votes if v]
    if not votes:
        return {}
    order = {w: i for i, w in enumerate(WINDOWS)}
    worst = max(votes, key=lambda v: order.get(v["window"], 1))
    out = dict(worst)
    if len({v["window"] for v in votes}) > 1 and order[worst["window"]] < order["early"]:
        out["window"] = "early"
        out["why"] = "评审意见不一，先等等：" + worst.get("why", "")
    out["issues"] = list(dict.fromkeys(i for v in votes for i in v.get("issues", [])))[:3]
    out["votes"] = [v["window"] for v in votes]
    return out


SAY = ("now",)      # 21.1.1：只有“正好”才算该说。回放你的 45 条标注：把“可以（不说也行）”也算说，一致 53%；只算“正好”，64%


def listener_examples(db, gid: int, exclude: int | None = None, k: int = 3) -> str:
    """你判过的例子（她当时说了的那种）：最后 3 句 + 她说的那句 → 你的判断。给听者当标准（few-shot）。"""
    out = []
    for lab, word in ((1, "正好（群主判：该接）"), (0, "不该说（群主判：时机不对）")):
        try:
            rows = db.conn.execute(
                "SELECT g.id, g.ts, e.text her FROM qq_gate_log g JOIN qq_reply_effect e ON e.gate_id=g.id "
                "WHERE g.group_id=? AND g.human=? AND g.mentioned=0 AND g.spoke=1 AND IFNULL(e.text,'')!='' "
                "AND NOT (g.human=0 AND IFNULL(g.human_why,'')='说的不对') AND g.id!=? ORDER BY RANDOM() LIMIT ?",
                (gid, lab, int(exclude or 0), k)).fetchall()
        except Exception:  # noqa: BLE001
            return ""
        for r in rows:
            ctx = list(reversed(db.conn.execute(
                "SELECT name, role, content FROM qq_group_messages WHERE group_id=? AND ts<=? ORDER BY id DESC LIMIT 3",
                (gid, float(r["ts"]) + 1)).fetchall()))
            if ctx:
                lines = " / ".join(f"{'她' if c['role'] == 'assistant' else c['name']}：{str(c['content'] or '')[:30]}" for c in ctx)
                out.append(f"- {lines} → 她想说「{str(r['her'])[:30]}」→ {word}")
    return "\n".join(out)


def agreement(rows: list) -> dict:
    """听者和你的标注对一对：只有 now ↔ 该接；ok / early / never ↔ 不该插（时机不对 / 没分原因的）。"""
    n = ok = n0 = n1 = her = 0
    for r in rows:
        try:
            v = json.loads(r["listener"] or "{}")
        except ValueError:
            continue
        w = v.get("window")
        if w not in WINDOWS:
            continue
        lab = int(r["human"])
        if lab == 0 and (r["human_why"] or "") == "说的不对":
            continue
        n += 1
        n0 += lab == 0
        n1 += lab == 1
        ok += (lab == 1) == (w in SAY)
        if "spoke" in r.keys():
            her += (lab == 1) == bool(r["spoke"])
    return {"n": n, "n0": n0, "n1": n1, "agree": round(ok / n, 3) if n else None,
            "her": round(her / n, 3) if n and rows and "spoke" in rows[0].keys() else None}


class ListenerMixin:
    def listener_rows(self, gid: int, n: int = 12) -> list[dict]:
        return [dict(r) for r in reversed(self.db.conn.execute(
            "SELECT ts, role, name, user_id uid, content FROM qq_group_messages WHERE group_id=? ORDER BY id DESC LIMIT ?",
            (gid, n)).fetchall())]

    def her_share(self, gid: int, minutes: float = 30, now: float | None = None) -> float:
        now = now or time.time()
        r = self.db.conn.execute("SELECT COUNT(*) n, SUM(role='assistant') h FROM qq_group_messages WHERE group_id=? AND ts>?",
                                 (gid, now - minutes * 60)).fetchone()
        return float(r["h"] or 0) / float(r["n"]) if r["n"] else 0.0

    def listener_stats(self, gid: int | None = None) -> dict:
        q = ("SELECT listener, human, IFNULL(human_why,'') human_why, spoke FROM qq_gate_log WHERE human IN (0,1) "
             "AND IFNULL(listener,'')!=''" + (" AND group_id=?" if gid is not None else ""))
        try:
            rows = self.db.conn.execute(q, ([gid] if gid is not None else [])).fetchall()
        except Exception:  # noqa: BLE001
            return {}
        return agreement(rows)

    def listener_enforced(self, gid: int) -> bool:
        mode = str(self.gk(gid).get("listener_mode", "auto") or "auto")
        if mode == "on":
            return True
        if mode != "auto":
            return False
        st = self.listener_stats(gid)
        need = int(self.gk(gid).get("listener_min_each", 15))
        if st.get("agree") is None or st["n0"] < need or st["n1"] < need:
            return False
        # 21.1.1：和你一致率够高（listener_agree）就接管；或者比她自己现在的做法明显更对（至少高 listener_margin、且不低于 0.6）
        her = st.get("her")
        return bool(st["agree"] >= float(self.gk(gid).get("listener_agree", 0.7))
                    or (her is not None and st["agree"] >= 0.6
                        and st["agree"] - her >= float(self.gk(gid).get("listener_margin", 0.08))))

    async def listen(self, gid: int, draft: list[str], now: float | None = None) -> dict:
        """问听者（配了 routes.listener2 时两个一起问）。出错返回 {}（当作没意见）。"""
        g = self.gk(gid)
        if str(g.get("listener_mode", "auto")) == "off" or not draft:
            return {}
        now = now or time.time()
        name = self.brain.persona.name
        rows = self.listener_rows(gid)
        sig = signals(rows, name, now, self.her_share(gid, now=now),
                      self.member_names(gid) if hasattr(self, "member_names") else [])
        chat = "\n".join(f"[{int(now - float(r['ts']))}秒前] {'她（' + name + '）' if r['role'] == 'assistant' else r['name']}："
                         f"{str(r['content'] or '')[:80]}" for r in rows)
        ex = listener_examples(self.db, gid)
        shots = f"群主以前判过的例子（照这个标准）：\n{ex}\n\n" if ex else ""
        recent = "、".join(self.recent_acts(gid)) if hasattr(self, "recent_acts") else ""
        prompt = PROMPT.format(name=name, signals=signals_text(sig), shots=shots, chat=chat,
                               draft=" / ".join(draft)[:200], recent_acts=recent or "没有")
        roles = ["gate"] + (["listener2"] if (getattr(self.brain.llm, "routes", None) or {}).get("listener2") and g.get("listener_panel", True) else [])
        votes = []
        for role in roles:
            try:
                out = await self.brain.llm.chat([{"role": "user", "content": prompt}], role=role, temperature=0.0,
                                                max_tokens=200)
                votes.append(parse_listen(out))
            except Exception as e:  # noqa: BLE001
                log.info("听者（%s）没判成：%s", role, e)
        res = merge_panel(votes) if len(votes) > 1 else (votes[0] if votes else {})
        if res:
            res["signals"] = sig
        return res

    def listener_log(self, gate_id, res: dict, enforced: bool):
        if not gate_id or not res:
            return
        try:
            self.db.conn.execute("UPDATE qq_gate_log SET listener=? WHERE id=?",
                                 (json.dumps({**{k: v for k, v in res.items() if k != "signals"}, "enforced": enforced},
                                             ensure_ascii=False)[:600], int(gate_id)))
            self.db.conn.commit()
        except Exception as e:  # noqa: BLE001
            log.debug("记听者失败：%s", e)
