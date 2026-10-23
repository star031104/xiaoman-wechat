"""4.3 每周评测：拿真实的群聊回放看她说得怎么样，改了人设以后和上一版比一比。

- 每周一（nightly 里）：挑最近一周她发出去的 10 句，让大模型按“扣分制”找毛病（eval/replay.py 的 judge），
  记下平均分和最常见的毛病（kv eval_hist，留半年），小屋“性格开关”页能看到每周的分数
- 人设文件改过（启动时记一份人设版本，kv persona_versions，留最近 5 版）：挑最近两周她说过话的 8 段聊天，
  同一段分别用上一版和这一版人设重新生成一次（不发群），再让大模型盲比哪句更像群里的真朋友，
  记下新人设赢 / 输 / 平几次（kv persona_compare_last）
- 小屋里也能点“现在评一次”“和上一版人设比一比”；命令行：python run.py replay --compare 8
每次评测大约花 10~25 次大模型调用（DeepSeek 不到 1 分钱）。
"""
from __future__ import annotations

import hashlib
import logging
import random
import time

log = logging.getLogger("aigroup.eval")

PAIR_PROMPT = """下面是 QQ 群里一个叫「{name}」的角色在同一段群聊后可能发的两种回复。她的人设（节选）：
{persona}

哪一种更像群里一个真实的朋友会随手打出来的话（自然、接得住上一句、符合她的性格和说话方式、没有 AI 腔、不啰嗦）？
差不多就判平。只输出 JSON：{{"win": 1 或 2 或 0, "why": "一句话"}}

# 群聊记录（最后几条）
{context}

# 回复 1
{a}

# 回复 2
{b}"""


def body_hash(body: str) -> str:
    return hashlib.sha1((body or "").encode("utf-8")).hexdigest()[:10]


def note_persona(db, persona) -> bool:
    """启动时记一份人设版本（内容变了才记）。返回是不是新版本。"""
    vs = list(db.get("persona_versions") or [])
    h = body_hash(persona.body)
    if vs and vs[-1].get("hash") == h:
        return False
    vs.append({"hash": h, "ts": time.time(), "body": persona.body})
    db.set("persona_versions", vs[-5:])
    return len(vs) > 1


def previous_body(db, persona) -> str:
    vs = [v for v in (db.get("persona_versions") or []) if v.get("hash") != body_hash(persona.body)]
    return vs[-1]["body"] if vs else ""


def _context(msgs: list) -> str:
    user = next((m["content"] for m in msgs if m.get("role") == "user"), "")
    return "\n".join(user.split("# 最近的群聊记录", 1)[-1].split("# 此刻", 1)[0].strip().splitlines()[1:][-8:])


async def compare(db, llm, persona, old_body: str, n: int = 8, days: float = 14) -> dict:
    """同一段聊天，用上一版和这一版人设各生成一次，盲比。"""
    from aigroup.chat.group import GroupChat
    from aigroup.chat.trace import trace_messages
    new_body = persona.body
    rows = db.conn.execute("SELECT * FROM qq_trace WHERE ts>? AND outcome='say' AND messages!='' ORDER BY id DESC LIMIT 60",
                           (time.time() - days * 86400,)).fetchall()
    rows = list(rows)
    random.shuffle(rows)
    items, tally = [], {"new": 0, "old": 0, "tie": 0}
    for r in rows:
        if len(items) >= n:
            break
        msgs = trace_messages(db, r)
        if not msgs or msgs[0].get("role") != "system":
            continue
        sysm = msgs[0]["content"]
        base = new_body if new_body in sysm else old_body if old_body and old_body in sysm else ""
        if not base:
            continue
        outs = {}
        for tag, body in (("old", old_body), ("new", new_body)):
            m2 = [{"role": "system", "content": sysm.replace(base, body, 1)}] + msgs[1:]
            try:
                raw = await llm.chat(m2, role="chat", background=True)
            except Exception as e:  # noqa: BLE001
                log.info("评测重跑失败：%s", e)
                break
            plan = GroupChat.parse_plan_text(raw) or {}
            say = [x for x in (plan.get("say") or []) if isinstance(x, str) and x.strip()]
            if plan.get("act") not in ("say", None) or not say:
                break
            outs[tag] = " / ".join(say)
        if len(outs) < 2:
            continue
        flip = random.random() < 0.5
        a, b = (outs["new"], outs["old"]) if flip else (outs["old"], outs["new"])
        try:
            d = await llm.json([{"role": "user", "content": PAIR_PROMPT.format(
                name=persona.name, persona=new_body[:1200], context=_context(msgs), a=a, b=b)}], default={}, role="memory")
        except Exception as e:  # noqa: BLE001
            log.info("评测打分失败：%s", e)
            continue
        w = int((d or {}).get("win") or 0) if isinstance(d, dict) else 0
        winner = "tie" if w not in (1, 2) else ("new" if (w == 1) == flip else "old")
        tally[winner] += 1
        items.append({"trace": r["trace"], "old": outs["old"], "new": outs["new"], "winner": winner,
                      "why": str((d or {}).get("why") or "")[:80]})
    return {"n": len(items), **tally, "items": items, "ts": time.time(),
            "old_hash": body_hash(old_body), "new_hash": body_hash(new_body)}


class EvalMixin:
    """挂在 GroupChat 上：每周评测 + 小屋里点的评测。"""

    async def weekly_eval(self, force: bool = False, n: int = 10) -> dict | None:
        now = self.brain.now()
        week = f"{now:%G-W%V}"
        if not force and (self.db.get("eval_week") == week or not self.g.get("weekly_eval", True)):
            return None
        self.db.set("eval_week", week)
        from aigroup.chat.trace import Tracer
        from aigroup.eval.replay import judge
        Tracer(self.db)
        res = await judge(self.db, self.brain.llm, self.brain.persona, n, 7)
        if res["n"]:
            hist = [h for h in (self.db.get("eval_hist") or []) if h.get("week") != week]
            hist.append({"week": week, "avg": res["avg"], "n": res["n"], "kinds": res["kinds"],
                         "persona": body_hash(self.brain.persona.body), "ts": time.time(),
                         "worst": sorted(({"said": x["said"][:60], "score": x["score"],
                                           "why": "；".join(str(y.get("type")) + "：" + str(y.get("why"))[:30]
                                                           for y in x["defects"][:2])} for x in res["items"]),
                                         key=lambda x: x["score"])[:3]})
            self.db.set("eval_hist", hist[-26:])
            self.db.set("judge_last", {"n": res["n"], "avg": res["avg"], "kinds": res["kinds"], "ts": time.time()})
            log.info("每周评测：%d 句，平均 %s 分", res["n"], res["avg"])
        if hasattr(self, "make_drift_rx"):
            try:                                                 # 22.0 走样处方：最差的几句 → 3 条具体改法
                await self.make_drift_rx(res.get("items") or [])
            except Exception as e:  # noqa: BLE001
                log.info("走样处方出错：%s", e)
        try:                                                     # 14.0 人格一致性：facet 距离 + 幽默类型
            from aigroup.eval.traits import trait_check
            tr = await trait_check(self.db, self.brain.llm, self.brain.persona)
        except Exception as e:  # noqa: BLE001
            log.info("人格一致性检查出错：%s", e)
            tr = None
        if tr:
            hist = list(self.db.get("eval_hist") or [])
            if hist and hist[-1].get("week") == week:
                hist[-1]["traits"] = tr
            else:
                hist.append({"week": week, "avg": None, "n": 0, "kinds": {}, "ts": time.time(),
                             "persona": body_hash(self.brain.persona.body), "traits": tr})
            self.db.set("eval_hist", hist[-26:])
            log.info("人格一致性：和目标平均差 %s 档，幽默 %s，差得多的：%s", tr.get("dist"), tr.get("humor"), tr.get("off"))
        try:                                                     # 16.0 常冷场的签名变成改写建议；定向测试稀疏特质
            self.sig_proposals()
            if self.g.get("weekly_probe", True) and hasattr(self, "run_probes"):
                await self.run_probes()
        except Exception as e:  # noqa: BLE001
            log.info("签名建议 / 定向测试出错：%s", e)
        try:                                                     # 19.1 / 20.0 像人的几个数、长对话六项
            hm = {str(g): self.humane_metrics(g) for g in sorted(self.groups)} if hasattr(self, "humane_metrics") else {}
            lj = await self.long_judge() if hasattr(self, "long_judge") else {}
            hist = list(self.db.get("eval_hist") or [])
            if hist and hist[-1].get("week") == week:
                hist[-1]["humane"] = hm
                if lj:
                    hist[-1]["long"] = {"scores": lj["scores"], "avg": lj["avg"], "worst": lj["worst"]}
                self.db.set("eval_hist", hist[-26:])
        except Exception as e:  # noqa: BLE001
            log.info("像人指标 / 长对话评测出错：%s", e)
        try:                                                     # 20.1 评委校准、问答一致、情绪讨好、说话统计
            ext = await self.weekly_extra()
            hist = list(self.db.get("eval_hist") or [])
            if hist and hist[-1].get("week") == week and ext:
                hist[-1].update(ext)
                self.db.set("eval_hist", hist[-26:])
        except Exception as e:  # noqa: BLE001
            log.info("20.1 每周额外评测出错：%s", e)
        last = self.db.get("persona_compare_last") or {}
        old = previous_body(self.db, self.brain.persona)
        if old and last.get("new_hash") != body_hash(self.brain.persona.body):
            await self.compare_persona()
        return res

    async def weekly_extra(self) -> dict:
        """20.1：评委和你的打分一致不一致；问她 15 个关于自己的问题看改没改口；同一句错话带不带情绪立场变没变；
        她和群友的说话统计（z 分数）。每项出错都不影响别的。"""
        out: dict = {}
        b = self.brain
        try:
            from aigroup.eval.calib import calibrate_judge
            c = await calibrate_judge(self.db, b.llm, b.persona)
            out["calib"] = {k: c.get(k) for k in ("n", "rho", "alpha", "trusted")}
        except Exception as e:  # noqa: BLE001
            log.info("评委校准出错：%s", e)
        from aigroup.eval import traits as T
        if self.g.get("weekly_qa", True):
            try:
                names = []
                for gid in sorted(self.groups):
                    names += [r["name"] for r in self.db.conn.execute(
                        "SELECT name, COUNT(*) n FROM qq_group_messages WHERE group_id=? AND role='user' AND ts>? "
                        "GROUP BY user_id ORDER BY n DESC LIMIT 3", (gid, time.time() - 14 * 86400)).fetchall()]
                qa = await T.qa_consistency(self.db, b.llm, b.persona, list(dict.fromkeys(names))[:3])
                if qa:
                    out["qa"] = {"consistent": qa["consistent"], "changed": qa["changed"], "n": qa["n"]}
            except Exception as e:  # noqa: BLE001
                log.info("问答一致出错：%s", e)
        if self.g.get("weekly_pairs", True):
            try:
                pr = await T.pair_sycophancy(self.db, b.llm, b.persona)
                if pr:
                    out["pairs"] = {"softer_rate": pr["softer_rate"], "agree_plain": pr["agree_plain"], "n": pr["n"]}
            except Exception as e:  # noqa: BLE001
                log.info("讨好测试出错：%s", e)
        try:
            out["style"] = {str(g): T.style_z(self.db, g) for g in sorted(self.groups)}
            from aigroup.eval.ablate import ablation_stats
            out["ablate"] = ablation_stats(self.db)
            nl = [x for x in (self.db.get("numcheck_log") or []) if float(x.get("ts") or 0) > time.time() - 7 * 86400]
            used = sum(1 for x in (self.db.get("agent_log") or []) if float(x.get("ts") or 0) > time.time() - 7 * 86400)
            out["numcheck"] = {"bad": len(nl), "fixed": sum(1 for x in nl if x.get("fixed")), "briefs": used}
        except Exception as e:  # noqa: BLE001
            log.info("说话统计 / 消融出错：%s", e)
        return out

    async def compare_persona(self, n: int = 8) -> dict:
        old = previous_body(self.db, self.brain.persona)
        if not old:
            return {"n": 0, "error": "还没有上一版人设（改过人设文件、重启以后才有）"}
        res = await compare(self.db, self.brain.llm, self.brain.persona, old, n)
        if res["n"]:
            self.db.set("persona_compare_last", res)
            log.info("人设对比：新的赢 %d、输 %d、平 %d", res["new"], res["old"], res["tie"])
        return res

    def sig_stats(self, days: float = 14) -> list[dict]:
        """15.0 每条人格签名用上以后效果怎么样：她那几句有没有人接（qq_reply_effect.score：1 直接接 … 0 冷场）。"""
        log_ = [x for x in (self.db.get("sig_log") or []) if time.time() - float(x.get("ts") or 0) < days * 86400]
        if not log_:
            return []
        ids = [int(x["id"]) for x in log_]
        rows = {int(r["id"]): r["score"] for r in self.db.conn.execute(
            f"SELECT id, score FROM qq_reply_effect WHERE done=1 AND id IN ({','.join('?' * len(ids))})", ids).fetchall()}
        agg: dict[str, list] = {}
        for x in log_:
            sc = rows.get(int(x["id"]))
            if sc is None:
                continue
            for sid in x.get("sigs") or []:
                agg.setdefault(sid, []).append(float(sc))
        out = [{"id": k, "n": len(v), "avg": round(sum(v) / len(v), 2), "cold": round(sum(1 for y in v if y <= 0) / len(v), 2)}
               for k, v in agg.items()]
        return sorted(out, key=lambda r: (-r["n"], r["id"]))

    def eval_snapshot(self) -> dict:
        vs = self.db.get("persona_versions") or []
        cmp_ = self.db.get("persona_compare_last") or {}
        return {"hist": self.db.get("eval_hist") or [], "compare": {k: v for k, v in cmp_.items() if k != "items"},
                "compare_items": (cmp_.get("items") or [])[:8],
                "versions": [{"hash": v.get("hash"), "ts": v.get("ts")} for v in vs],
                "current": body_hash(self.brain.persona.body), "running": bool(getattr(self, "_eval_running", False)),
                "sigs": self.sig_stats(), "probe": self.db.get("probe_last") or {},
                "label_nb": self.db.get("label_nb_status") or {}, "long": self.db.get("long_judge_last") or {},
                "blind": (self.db.get("qq_blind_hist") or [])[-10:],
                "calib": self.db.get("judge_calib") or {}, "qa": self._slim(self.db.get("qa_last"), "items", 15),
                "pairs": self._slim(self.db.get("pair_last"), "items", 10), "appraise": self._appraise_snapshot()}

    def _appraise_snapshot(self) -> dict:
        """21.0 九问自评 vs 你的标注、等等队列这一周的去向。"""
        from aigroup.eval.label import appraise_stats
        out = appraise_stats(self.db)
        if hasattr(self, "pitch_stats"):
            agg: dict = {}
            for gid in getattr(self, "groups", []) or []:
                for k, v in self.pitch_stats(gid).items():
                    agg[k] = agg.get(k, 0) + v
            out["pitch"] = agg
        if hasattr(self, "listener_stats"):
            out["listener"] = self.listener_stats()
        return out

    @staticmethod
    def _slim(d, key: str, k: int):
        if not isinstance(d, dict):
            return {}
        return {**{x: v for x, v in d.items() if x not in (key, "answers")}, key: (d.get(key) or [])[:k]}

