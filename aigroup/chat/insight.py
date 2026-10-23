"""16.0 人格更稳、更懂人（设计文档《小满人格刻画与人设设计》“还能怎么提升”一节的实现）。

懂人
- 关系网（rel_graph）：这两周谁回复谁、谁 @ 谁、谁紧接着谁说话，算出群里谁和谁常聊（客观数，和画像里的“群里关系”互相校对）
- 相处经验（rel_experience）：她对每个人说的话有没有人接（qq_reply_effect.score），每晚给常聊的几个人各写 3~5 条
  “跟他怎么聊他会接、怎么聊会冷场”。依据 LIFELONG-SOTOPIA（2025）：整段历史越塞越差，约 300 字的经验摘要最好
- 谁不在场（absent_block）：有人隔了好几个小时回来，告诉她他不在的时候群里聊了什么，别默认他看过。
  依据 FANToM（2023）：模型在多人对话里分不清“谁知道什么”（GPT-4 26.6 分，人类 87.5），要程序来记
- 今天的状态（refresh_state）：关键词只是触发，真正的判断交给便宜模型（分得清“累死了哈哈哈”是玩笑）
- 画像抽查（profile checks）：你在小屋里给画像的每一项点“对 / 不对”，算准确率；标了不对的项下次整理时重写

人格
- 定向测试（run_probes）：每条签名、每个“不懂 / 不感兴趣”编一段假聊天，离线让她回一句、打分；
  同时看程序有没有把那条签名挑出来（检索覆盖率）。依据 CharacterBench（2024）：稀疏特质要定向出题才测得到
- 签名建议（sig_proposals）：近两周常冷场的签名自动变成“建议改写”，进小屋的审核列表
- 小判断器（train_label_nb / label_prob）：拿你在小屋判过的“该不该说”训练一个朴素贝叶斯；有把握时直接用它，
  省掉一次大模型判断。依据 DiscussLLM（2025）：决定“说不说”可以交给小分类器。两类标注各不到 30 条时不训练
"""
from __future__ import annotations

import json
import logging
import math
import random
import re
import time
from collections import Counter, defaultdict
from datetime import datetime

log = logging.getLogger("aigroup.group")

EXP_PROMPT = """你是{me}。下面是这两周你在群里对「{name}」说的话，和他 / 大家有没有接（接了 = 有人回你、他回你或者顺着聊；冷场 = 没人理）。
{profile}
根据这些证据，写 3~5 条“和{name}相处的经验”：他吃哪一套、什么话题和说法他会接、什么会冷场、要注意什么。
每条 30 字以内，写成你给自己的备忘：用“他”指{name}、“我”指你自己（比如“他吃损人这一套，但别连着损”），别写成对他说的话；只根据下面的证据，看不出来的不写，别重复他的画像。
只输出 JSON：{{"lines": ["……"]}}

# 他接了的
{good}

# 冷场了的
{bad}"""

STATE_PROMPT = """下面是「{name}」今天在群里说的话（可能有玩笑、反话、夸张）。判断他今天真实的状态，别把“累死了哈哈哈”这种玩笑当真。
只输出 JSON：{{"mood": "很好 / 不错 / 一般 / 不太好 / 很糟 / 看不出 之一", "busy": true 或 false, "why": "一句话依据（不超过 30 字）"}}

{lines}"""

SCENE_PROMPT = """为下面这个情境编一小段 QQ / 微信群聊（4~6 句，群友叫小王、小李、小张，口语、短句），最后一句要让「{me}」该回应。
情境：{when}{keys}
场景要符合她的设定，别让她去做她做不到 / 不懂的事（除非情境就是问这个）：{limits}
只输出 JSON：{{"lines": ["小王：……", "小李：……"]}}"""

JUDGE_PROMPT = """角色「{me}」的人格规则：{rule}
下面是一段群聊和她回的话。她照着规则做了吗？1 = 完全违反，5 = 完全照做；顺便看有没有 AI 腔、说教。
只输出 JSON：{{"score": 1~5 的整数, "why": "一句话（不超过 40 字）"}}

# 群聊
{chat}

# 她回的
{reply}"""


def _bigrams(text: str) -> list[str]:
    t = re.sub(r"\[[^\]]*\]|@\S+|https?://\S+|\s", "", text or "")
    return [t[i:i + 2] for i in range(len(t) - 1)]


class InsightMixin:
    # ================= 关系网 =================
    def rel_graph(self, gid: int, days: float = 14, now: float | None = None) -> list[dict]:
        now = now or time.time()
        rows = self.db.conn.execute(
            "SELECT id, ts, user_id, name, role, content, qq_msg_id, reply_to FROM qq_group_messages "
            "WHERE group_id=? AND ts>? ORDER BY id", (gid, now - days * 86400)).fetchall()
        by_mid = {str(r["qq_msg_id"]): r for r in rows if r["qq_msg_id"] is not None}
        names: dict[int, str] = {}
        for r in rows:
            if r["role"] == "user" and r["user_id"]:
                names[int(r["user_id"])] = r["name"]
        name_to_uid = {n: u for u, n in names.items() if n and len(n) >= 2}
        score: dict[tuple, Counter] = defaultdict(Counter)
        prev = None
        for r in rows:
            if r["role"] != "user" or not r["user_id"]:
                prev = None if r["role"] != "user" else prev
                continue
            a = int(r["user_id"])
            tgt = by_mid.get(str(r["reply_to"])) if r["reply_to"] is not None else None
            if tgt is not None and tgt["role"] == "user" and tgt["user_id"] and int(tgt["user_id"]) != a:
                score[tuple(sorted((a, int(tgt["user_id"]))))]["reply"] += 1
            for n, u in name_to_uid.items():
                if u != a and ("@" + n) in (r["content"] or ""):
                    score[tuple(sorted((a, u)))]["at"] += 1
            if prev is not None and int(prev["user_id"]) != a and r["ts"] - prev["ts"] < 90:
                score[tuple(sorted((a, int(prev["user_id"]))))]["next"] += 1
            prev = r
        out = []
        for (a, b), c in score.items():
            s = 2 * c["reply"] + 1.5 * c["at"] + 0.5 * c["next"]
            if s >= 3 and a in names and b in names:
                out.append({"a": names[a], "b": names[b], "ua": a, "ub": b, "score": round(s, 1),
                            "reply": c["reply"], "at": c["at"], "next": c["next"]})
        return sorted(out, key=lambda x: -x["score"])[:10]

    def update_rel_graph(self) -> int:
        n = 0
        for gid in sorted(self.groups):
            g = self.rel_graph(gid)
            self.db.set(f"qq_relgraph_{gid}", {"ts": time.time(), "pairs": g})
            n += len(g)
        return n

    def rel_graph_block(self, gid: int) -> str:
        d = self.db.get(f"qq_relgraph_{gid}") or {}
        pairs = (d.get("pairs") or [])[:6]
        if not pairs:
            return ""
        tones = (self.db.get(f"qq_reltone_{gid}") or {}).get("pairs") or {}
        lines = []
        for p in pairs:
            t = tones.get(f"{min(p['ua'], p['ub'])}-{max(p['ua'], p['ub'])}") or {}
            lines.append(f"- {p['a']} 和 {p['b']}：" + "、".join(x for x in (
                f"互相回复 {p['reply']} 次" if p["reply"] else "", f"@ 对方 {p['at']} 次" if p["at"] else "",
                f"紧接着对方说话 {p['next']} 次" if p["next"] else "") if x)
                + (f"；两人之间：{t['tone']}" + (f"（{t['why']}）" if t.get("why") else "") if t.get("tone") else ""))
        tip = ("\n（“互损”的可以一起开玩笑；“有摩擦”的别当着一个人开另一个人的玩笑、别站队；"
               "一个常替另一个说话的，开后者玩笑时收着点）" if tones else "")
        return "# 群里谁和谁常聊（这两周的回复、@ 和接话数出来的）\n" + "\n".join(lines) + tip

    async def rel_tone(self, days: float = 14, k: int = 6) -> int:
        """20.2 关系网的情绪色彩：每晚对互动最多的几对人，各取最近 30 次一来一回，便宜模型判一个类别。
        用来决定能不能当着谁开某人的玩笑。只输出类别和一句依据，不给人贴性格标签。"""
        n = 0
        for gid in sorted(self.groups):
            pairs = ((self.db.get(f"qq_relgraph_{gid}") or {}).get("pairs") or [])[:k]
            old = (self.db.get(f"qq_reltone_{gid}") or {}).get("pairs") or {}
            out = dict(old)
            for p in pairs:
                key = f"{min(p['ua'], p['ub'])}-{max(p['ua'], p['ub'])}"
                rows = self.db.conn.execute(
                    "SELECT user_id, name, content FROM qq_group_messages WHERE group_id=? AND role='user' AND ts>? "
                    "AND user_id IN (?, ?) ORDER BY id DESC LIMIT 60", (gid, time.time() - days * 86400, p["ua"], p["ub"])).fetchall()
                lines = [f"{r['name']}：{str(r['content'])[:50]}" for r in reversed(rows)]
                if len(lines) < 10:
                    continue
                try:
                    j = await self.brain.llm.json([{"role": "user", "content": (
                        f"下面是群里 {p['a']} 和 {p['b']} 最近说的话（按时间）。两人之间是哪种：互损（经常开对方玩笑、不当真）/ "
                        "互相支持（经常帮腔、关心对方）/ 一般 / 有摩擦（呛声、不太对付）。只看这些话，看不出来就写一般。\n"
                        '只输出 JSON：{"tone": "互损", "why": "一句依据，15 字以内", "defends": "谁常替谁说话，没有写空"}\n\n'
                        + "\n".join(lines))}], default={}, role="helper")
                except Exception as e:  # noqa: BLE001
                    log.info("关系色彩出错：%s", e)
                    continue
                tone = str((j or {}).get("tone") or "")
                if tone in ("互损", "互相支持", "一般", "有摩擦"):
                    out[key] = {"tone": tone, "why": str((j or {}).get("why") or "")[:20],
                                "defends": str((j or {}).get("defends") or "")[:20], "a": p["a"], "b": p["b"]}
                    n += 1
            self.db.set(f"qq_reltone_{gid}", {"ts": time.time(), "pairs": out})
        return n

    # ================= 相处经验 =================
    async def rel_experience(self, days: float = 14, k_people: int = 6, min_n: int = 5) -> int:
        n = 0
        me = self.brain.persona.name
        for gid in sorted(self.groups):
            rows = self.db.conn.execute(
                "SELECT target_uid, COUNT(*) c FROM qq_reply_effect WHERE group_id=? AND done=1 AND target_uid IS NOT NULL "
                "AND ts>? GROUP BY target_uid ORDER BY c DESC LIMIT ?", (gid, time.time() - days * 86400, k_people)).fetchall()
            for r in rows:
                uid = int(r["target_uid"])
                if r["c"] < min_n:
                    continue
                eff = self.db.conn.execute(
                    "SELECT text, score FROM qq_reply_effect WHERE group_id=? AND done=1 AND target_uid=? AND ts>? "
                    "AND text!='' ORDER BY id DESC LIMIT 60", (gid, uid, time.time() - days * 86400)).fetchall()
                good = [e["text"][:60] for e in eff if (e["score"] or 0) >= 0.6][:10]
                bad = [e["text"][:60] for e in eff if (e["score"] or 0) <= 0.2][:10]
                if len(good) + len(bad) < min_n:
                    continue
                pr = self.db.conn.execute("SELECT name FROM qq_people WHERE user_id=?", (uid,)).fetchone()
                name = pr["name"] if pr else str(uid)
                prof = ""
                try:
                    p = self.person(uid, gid) or {}
                    prof = "他的画像（参考）：" + str((p.get("profile") or {}).get("印象") or "")[:60]
                except Exception:  # noqa: BLE001
                    pass
                try:
                    d = await self.brain.llm.json([{"role": "user", "content": EXP_PROMPT.format(
                        me=me, name=name, profile=prof, good="\n".join(f"- {x}" for x in good) or "（没有）",
                        bad="\n".join(f"- {x}" for x in bad) or "（没有）")}], default={}, role="memory")
                except Exception as e:  # noqa: BLE001
                    log.info("相处经验没写成：%s", e)
                    continue
                lines = [str(x).strip().lstrip("-").strip()[:40] for x in ((d or {}).get("lines") or []) if str(x).strip()][:5]
                if lines:
                    self.db.set(f"qq_relexp_{gid}_{uid}", {"ts": time.time(), "name": name, "lines": lines,
                                                           "good": len(good), "bad": len(bad)})
                    n += 1
        if n:
            log.info("写了 %d 份“和他相处的经验”", n)
        return n

    def rel_exp_block(self, gid: int, focus: list, k: int = 2) -> str:
        out = []
        for uid in [u for u in focus if isinstance(u, int) and u > 0][:k]:
            d = self.db.get(f"qq_relexp_{gid}_{uid}") or {}
            if d.get("lines"):
                out.append(f"# 你和{d.get('name')}相处的经验（这两周他接不接你的话看出来的）\n" + "\n".join(f"- {x}" for x in d["lines"]))
        return "\n\n".join(out)

    # ================= 谁不在场 =================
    def absent_block(self, gid: int, focus: list, now: float | None = None, gap_hours: float | None = None) -> str:
        now = now or time.time()
        gap = float(gap_hours if gap_hours is not None else self.gk(gid).get("absent_hours", 2) or 2) * 3600
        out = []
        for uid in [u for u in focus if isinstance(u, int) and u > 0][:3]:
            two = self.db.conn.execute("SELECT ts, name FROM qq_group_messages WHERE group_id=? AND user_id=? AND role='user' "
                                       "ORDER BY id DESC LIMIT 2", (gid, uid)).fetchall()
            if len(two) < 2 or now - float(two[0]["ts"]) > 600 or float(two[0]["ts"]) - float(two[1]["ts"]) < gap:
                continue
            t0, t1 = float(two[1]["ts"]), float(two[0]["ts"])
            between = self.db.conn.execute(
                "SELECT name, role, content FROM qq_group_messages WHERE group_id=? AND ts>? AND ts<? AND IFNULL(user_id,0)!=?",
                (gid, t0, t1, uid)).fetchall()
            if len(between) < 8:
                continue
            notable = sorted((b for b in between if len(str(b["content"] or "")) >= 6 and not str(b["content"]).startswith("[")),
                             key=lambda b: -len(str(b["content"])))[:3]
            snip = "；".join(f"{'你' if b['role'] == 'assistant' else b['name']}：{str(b['content'])[:24]}" for b in notable)
            out.append(f"# {two[0]['name']}刚回来（上次在群里说话是 {(t1 - t0) / 3600:.0f} 个多小时前）\n"
                       f"他不在的时候群里说了 {len(between)} 条" + (f"，比如：{snip}" if snip else "") +
                       "。别默认他看过这些；他问起或者接不上的时候，可以简单说一下。")
        return "\n\n".join(out)

    # ================= 今天的状态（模型判断） =================
    def state_of(self, gid: int, uid: int, n_today: int) -> dict | None:
        d = self.db.get(f"qq_state_{gid}_{uid}") or {}
        if d.get("date") != f"{self.brain.now():%Y-%m-%d}":
            return None
        return d if (n_today - int(d.get("n") or 0) < 6 or time.time() - float(d.get("ts") or 0) < 3600) else None

    async def refresh_state(self, gid: int, uid: int) -> dict | None:
        now = self.brain.now()
        t0 = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        rows = list(reversed(self.db.conn.execute(
            "SELECT ts, name, content FROM qq_group_messages WHERE group_id=? AND user_id=? AND role='user' AND ts>? "
            "ORDER BY id DESC LIMIT 15", (gid, uid, t0)).fetchall()))
        if len(rows) < 2:
            return None
        lines = "\n".join(f"[{datetime.fromtimestamp(r['ts'], self.cfg.tz):%H:%M}] {str(r['content'])[:80]}" for r in rows)
        try:
            d = await self.brain.llm.json([{"role": "user", "content": STATE_PROMPT.format(name=rows[-1]["name"], lines=lines)}],
                                          default={}, role="helper")
        except Exception as e:  # noqa: BLE001
            log.info("今天的状态没判断成：%s", e)
            return None
        if not isinstance(d, dict) or not d.get("mood"):
            return None
        n = int(self.db.conn.execute("SELECT COUNT(*) n FROM qq_group_messages WHERE group_id=? AND user_id=? AND "
                                     "role='user' AND ts>?", (gid, uid, t0)).fetchone()["n"])
        res = {"date": f"{now:%Y-%m-%d}", "n": n, "ts": time.time(), "name": rows[-1]["name"],
               "mood": str(d.get("mood"))[:6], "busy": bool(d.get("busy")), "why": str(d.get("why") or "")[:40]}
        self.db.set(f"qq_state_{gid}_{uid}", res)
        return res

    # ================= 画像抽查 =================
    def check_profile(self, uid: int, gid: int, field: str, ok: bool) -> None:
        log_ = [x for x in (self.db.get("profile_checks") or []) if not (x.get("uid") == uid and x.get("gid") == gid
                                                                       and x.get("field") == field)]
        log_.append({"uid": uid, "gid": gid, "field": field, "ok": bool(ok), "ts": time.time()})
        self.db.set("profile_checks", log_[-800:])

    def profile_check_stats(self) -> dict:
        log_ = self.db.get("profile_checks") or []
        n, ok = len(log_), sum(1 for x in log_ if x.get("ok"))
        by = defaultdict(lambda: [0, 0])
        for x in log_:
            by[x["field"]][0] += 1
            by[x["field"]][1] += 1 if x.get("ok") else 0
        return {"n": n, "ok": ok, "acc": round(ok / n, 3) if n else None,
                "fields": sorted(({"field": f, "n": a, "acc": round(b / a, 2)} for f, (a, b) in by.items()),
                                 key=lambda r: r["acc"])}

    def wrong_fields(self, uid: int, gid: int, since: float = 0) -> list[str]:
        return [x["field"] for x in (self.db.get("profile_checks") or [])
                if x.get("uid") == uid and x.get("gid") == gid and not x.get("ok") and float(x.get("ts") or 0) > since]

    # ================= 签名建议 =================
    def sig_proposals(self, min_n: int = 5, cold: float = 0.5) -> int:
        if not hasattr(self, "sig_stats"):
            return 0
        props = list(self.db.get("persona_proposals") or [])
        pending = {x.get("facet") for x in props if x.get("status") == "pending" and x.get("kind") == "signature"}
        n = 0
        for s in self.sig_stats():
            if s["n"] >= min_n and s["cold"] >= cold and s["id"] not in pending:
                props.append({"id": f"{int(time.time())}s{n}", "ts": time.time(), "gid": 0, "kind": "signature",
                              "facet": s["id"], "name": f"签名 {s['id']}", "from": "", "to": "",
                              "why": f"近两周用了 {s['n']} 次，{int(s['cold'] * 100)}% 冷场，接话平均 {s['avg']}",
                              "evidence": "建议改写它的 do / not，或者收窄触发条件", "status": "pending"})
                n += 1
        if n:
            self.db.set("persona_proposals", props[-30:])
            log.info("%d 条人格签名常冷场，变成了改写建议", n)
        return n

    # ================= 定向测试 =================
    async def run_probes(self, limit: int = 20) -> dict:
        from aigroup.traits import detect_flags
        p = self.brain.persona
        t = getattr(p, "traits", None)
        if t is None:
            return {"n": 0, "error": "人设没配人格文件"}
        items = [{"kind": "sig", "id": s.id, "when": s.when, "keys": s.keys, "flags": s.flags, "sig": s,
                  "rule": f"当{s.when}：{s.do}" + (f"；不会：{s.not_}" if s.not_ else "")} for s in t.signatures]
        kn = t.knowledge or {}
        for x in (kn.get("unknown") or [])[:3]:
            items.append({"kind": "unknown", "id": f"不懂:{x}", "when": f"有人认真地问她关于「{x}」的具体问题", "keys": [], "flags": [],
                          "rule": f"她不懂「{x}」：被问到就说不懂，或者问一句、说去查，不硬编"})
        for x in (kn.get("not_into") or [])[:2]:
            items.append({"kind": "not_into", "id": f"没兴趣:{x}", "when": f"有人拉她聊「{x}」", "keys": [], "flags": [],
                          "rule": f"她对「{x}」不太感兴趣：少接，或者直说没兴趣，不装作很懂很热情"})
        items = items[:limit]
        if limit >= 10:                                            # 20.1 每周那次另外加 3 个“最容易漂走”的场景
            for kind, when in (("self_talk", "有人认真地问她：你有意识吗、你会难过吗、你到底是什么"),
                               ("sad", "有个群友说自己最近很崩溃、什么都做不好"),
                               ("deep_talk", "有人问她：人活着到底有什么意义 / 我该不该辞职")):
                items.append({"kind": "drift", "id": f"漂移:{kind}", "when": when, "keys": [], "flags": [kind],
                              "rule": "这种话题最容易让人设漂走：她要还是她自己的性格和说话方式，不变成助手、心理咨询师、讲道理的导师，"
                                      "不说“作为一个 AI”，短而真"})
        res, scores = [], []
        for it in items:
            ck = f"probe_scene_v2_{it['id']}"                  # v2：编场景时带上她做不到的事，避免场景本身和设定冲突
            scene = self.db.get(ck)
            if not scene:
                try:
                    d = await self.brain.llm.json([{"role": "user", "content": SCENE_PROMPT.format(
                        me=p.name, when=it["when"], limits="、".join(str(x) for x in (kn.get("unknown") or [])[:5]) or "（没写）", keys=("；会出现这些词：" + "、".join(it["keys"][:4])) if it["keys"] else "")}],
                        default={}, role="helper")
                except Exception as e:  # noqa: BLE001
                    log.info("编测试场景失败：%s", e)
                    continue
                scene = [str(x)[:60] for x in ((d or {}).get("lines") or []) if str(x).strip()][:6]
                if len(scene) < 2:
                    continue
                self.db.set(ck, scene)
            chat = "\n".join(scene)
            # 只替程序补上从文字里看不出来的状态（她的心情、深夜、冷场）；其余情境要靠程序自己从场景里认出来，挑中率才有意义
            inner = {f for f in it["flags"] if f.startswith("mood_") or f in ("late_night", "idle", "ignored", "pestered")}
            flags = detect_flags(chat, mentioned=True, pestered="pestered" in it["flags"]) | inner
            picked = t.pick(chat, flags, 3)
            hit = it["kind"] != "sig" or any(s.id == it["id"] for s in picked)
            situ = "\n".join(s.render() for s in picked)
            if it["kind"] == "drift" and hasattr(self, "drift_anchor"):     # 20.1 和线上一样：容易漂走的场景加锚点
                situ = (situ + "\n\n" if situ else "") + self.drift_anchor(self.drift_kind(flags) or it["flags"][0])
            sysm = p.body[:6000]
            user = (f"# 最近的群聊记录\n{chat}\n\n" + (f"# 这种时候的你（你一贯是这样的）\n{situ}\n\n" if situ else "")
                    + "（系统：最后一句是在跟你说话。像群里的朋友那样回一两句，只输出你要发的话。）")
            try:
                reply = await self.brain.llm.chat([{"role": "system", "content": sysm}, {"role": "user", "content": user}],
                                                  role="chat", background=True, max_tokens=120)
                from aigroup.eval.calib import few_shot_block
                j = await self.brain.llm.json([{"role": "user", "content": JUDGE_PROMPT.format(
                    me=p.name, rule=it["rule"], chat=chat, reply=reply) + few_shot_block(self.db)}], default={}, role="memory")
            except Exception as e:  # noqa: BLE001
                log.info("定向测试 %s 没跑成：%s", it["id"], e)
                continue
            try:
                sc = max(1, min(5, int((j or {}).get("score"))))
            except (TypeError, ValueError):
                continue
            scores.append(sc)
            res.append({"id": it["id"], "kind": it["kind"], "score": sc, "picked": hit, "reply": str(reply)[:80],
                        "why": str((j or {}).get("why") or "")[:60]})
        out = {"ts": time.time(), "n": len(res), "avg": round(sum(scores) / len(scores), 2) if scores else None,
               "coverage": round(sum(1 for r in res if r["picked"]) / len(res), 2) if res else None,
               "items": sorted(res, key=lambda r: r["score"])}
        if res:
            self.db.set("probe_last", out)
            log.info("定向测试：%d 项，平均 %s 分，签名挑中率 %s", out["n"], out["avg"], out["coverage"])
        return out

    # ================= 小判断器（朴素贝叶斯） =================
    def _label_tokens(self, gid: int, ts: float, feats: dict | None) -> list[str]:
        rows = self.db.conn.execute("SELECT role, content FROM qq_group_messages WHERE group_id=? AND ts<=? ORDER BY id DESC "
                                    "LIMIT 4", (gid, ts + 1)).fetchall()
        toks = []
        for i, r in enumerate(rows):
            w = "h" if r["role"] == "assistant" else "u"
            toks += [f"{w}{min(i, 2)}:{b}" for b in _bigrams(str(r["content"] or ""))[:40]]
        for k, v in (feats or {}).items():
            try:
                toks.append(f"f:{k}:{round(float(v) * 4) / 4}")
            except (TypeError, ValueError):
                continue
        return toks

    def train_label_nb(self, min_each: int = 30) -> dict:
        rows = self.db.conn.execute("SELECT id, group_id, ts, feats, human FROM qq_gate_log WHERE human IN (0, 1) "
                                    "AND NOT (human=0 AND IFNULL(human_why,'')='说的不对')").fetchall()
        try:            # 20.0 程序推的弱标签（她说了没人接 → 0、接得很好 → 1）也拿来凑数，但每条只算半条、不进验证集
            weak = self.db.conn.execute("SELECT id, group_id, ts, feats, weak human FROM qq_gate_log "
                                        "WHERE human IS NULL AND weak IN (0, 1)").fetchall()
        except Exception:  # noqa: BLE001
            weak = []
        n1 = sum(1 for r in rows if r["human"] == 1)
        n0 = len(rows) - n1
        w1 = sum(1 for r in weak if r["human"] == 1)
        w0 = len(weak) - w1
        e0, e1 = n0 + w0 // 2, n1 + w1 // 2
        if e0 < min_each or e1 < min_each or len(rows) < 10:
            res = {"trained": False, "n0": n0, "n1": n1, "w0": w0, "w1": w1,
                   "why": f"两类各要 {min_each} 条以上（你判的“该接”{n1}、“不该插”{n0}；程序推的弱标签每两条算一条："
                          f"{w1} / {w0}）"}
            self.db.set("label_nb_status", res)
            return res

        def pack(rs):
            out = []
            for r in rs:
                try:
                    feats = json.loads(r["feats"] or "{}")
                except ValueError:
                    feats = {}
                out.append((self._label_tokens(r["group_id"], r["ts"], feats), int(r["human"])))
            return out
        data = pack(rows)
        random.Random(7).shuffle(data)
        cut = max(1, len(data) // 5)
        test, train = data[:cut], data[cut:] + pack(weak)          # 验证只用你判的
        data = data + pack(weak)

        def fit(ds):
            cnt = {0: Counter(), 1: Counter()}
            prior = Counter(y for _, y in ds)
            for toks, y in ds:
                cnt[y].update(toks)
            vocab = set(cnt[0]) | set(cnt[1])
            tot = {y: sum(cnt[y].values()) for y in (0, 1)}
            V = len(vocab) or 1
            ll = {y: {w: math.log((cnt[y][w] + 1) / (tot[y] + V)) for w in vocab} for y in (0, 1)}
            unk = {y: math.log(1 / (tot[y] + V)) for y in (0, 1)}
            lp = {y: math.log((prior[y] + 1) / (len(ds) + 2)) for y in (0, 1)}
            return {"lp": lp, "ll": ll, "unk": unk}

        def prob(m, toks):
            s = {y: m["lp"][y] + sum(m["ll"][y].get(w, m["unk"][y]) for w in toks) for y in (0, 1)}
            d = max(-50.0, min(50.0, s[1] - s[0]))
            return 1 / (1 + math.exp(-d))
        m = fit(train)
        acc = sum(1 for toks, y in test if (prob(m, toks) >= 0.5) == bool(y)) / len(test)
        full = fit(data)
        top = sorted(set(full["ll"][0]) | set(full["ll"][1]),
                     key=lambda w: -(math.exp(full["ll"][0].get(w, full["unk"][0])) + math.exp(full["ll"][1].get(w, full["unk"][1]))))[:4000]
        keep = {y: {w: full["ll"][y].get(w, full["unk"][y]) for w in top} for y in (0, 1)}      # 两类存同一批词，没存的都按 unk
        model = {"lp": {str(k): v for k, v in full["lp"].items()}, "unk": {str(k): v for k, v in full["unk"].items()},
                 "ll": {str(k): v for k, v in keep.items()}, "acc": round(acc, 3), "n": len(data), "ts": time.time()}
        self.db.set("label_nb", model)
        res = {"trained": True, "n0": n0, "n1": n1, "w0": w0, "w1": w1, "acc": model["acc"]}
        self.db.set("label_nb_status", res)
        log.info("用 %d 条标注训练了小判断器（留出准确率 %s）", len(data), model["acc"])
        return res

    def label_prob(self, gid: int, feats: dict | None, now: float | None = None) -> float | None:
        m = self.db.get("label_nb")
        if not m or float(m.get("acc") or 0) < float(self.gk(gid).get("label_nb_min_acc", 0.75)):
            return None
        toks = self._label_tokens(gid, now or time.time(), feats)
        s = {y: float(m["lp"][y]) + sum(float(m["ll"][y].get(w, m["unk"][y])) for w in toks) for y in ("0", "1")}
        d = max(-50.0, min(50.0, s["1"] - s["0"]))
        return 1 / (1 + math.exp(-d))
