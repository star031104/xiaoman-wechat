"""回放（2.0）：每次“问大模型要不要说、说什么”都存一份，改了门控或提示词以后先拿最近的记录回放一遍再上线。

做法参考 AstrBot + NapCat 的“可回放消息链路”：每一批消息一个 trace_id；记下特征、概率、阈值、发给大模型的消息、
大模型原文、最后发出去的话、质检命中了什么。回放时不真发、固定随机种子。

    python run.py replay                 最近 7 天：用现在的门控重新判断一遍，看放行 / 拦下的变化（不调大模型）
    python run.py replay --llm 20        再挑 20 条用现在的模型和质检重跑一遍（会调大模型，不发群）
    python run.py replay --export        导出回放包到 data/replay/（默认只有门控特征和结果；--with-text 带上文字，名字和号码换成编号）
    python run.py replay --judge 20      挑 20 条她发出去的话，让大模型按“扣分制”找毛病（CoSER：只找缺陷，每个 1~5 分，100−5×总分）
    python run.py replay --compare 8     4.3 挑 8 段聊天，用上一版和这一版人设各生成一次，盲比哪句更像真朋友
    python run.py calibrate              拿群友自己打的字过一遍质检：看规则会不会误伤正常聊天

结果写在 data/replay/report-时间.md。
"""
from __future__ import annotations

import asyncio
import json
import random
import re
import time
from datetime import datetime
from pathlib import Path

from aigroup.chat.trace import Tracer, prompt_text, trace_messages


def next_pair(db, gid: int, rand=None) -> dict | None:
    """找一条还没标过、大模型给了两种说法的回放；两种说法随机左右放。"""
    import random as _r
    rows = db.conn.execute("SELECT * FROM qq_trace WHERE group_id=? AND plan LIKE '%\"cands\"%' AND trace NOT IN "
                           "(SELECT trace FROM qq_pairs) ORDER BY id DESC LIMIT 50", (gid,)).fetchall()
    for r in rows:
        try:
            plan = json.loads(r["plan"] or "{}")
        except ValueError:
            continue
        cands = [c for c in plan.get("cands") or [] if c]
        if len(cands) < 2:
            continue
        sent = json.loads(r["sent"] or "[]")
        a, b = cands[0], cands[1]
        sent_idx = 0 if sent and sent[0] in " ".join(a) else 1 if sent and sent[0] in " ".join(b) else -1
        if (rand or _r).random() < 0.5:
            a, b = b, a
            sent_idx = {0: 1, 1: 0}.get(sent_idx, -1)
        msgs = trace_messages(db, r)
        user = next((m["content"] for m in msgs if m.get("role") == "user"), "")
        ctx = user.split("# 最近的群聊记录", 1)[-1].split("# 此刻", 1)[0].strip().splitlines()[1:][-6:]
        return {"trace": r["trace"], "context": ctx, "a": a, "b": b, "sent": sent_idx}
    return None


def answer_pair(db, trace: str, a: list, b: list, sent: int, choice: int):
    db.conn.execute("INSERT OR REPLACE INTO qq_pairs(trace, a, b, sent, choice, ts) VALUES (?,?,?,?,?,?)",
                    (trace, json.dumps(a, ensure_ascii=False), json.dumps(b, ensure_ascii=False), int(sent), int(choice),
                     time.time()))
    db.conn.commit()


def pair_stats(db) -> dict:
    r = db.conn.execute("SELECT COUNT(*) n, SUM(choice IN (0,1)) decided, SUM(choice IN (0,1) AND choice=sent) agree, "
                        "SUM(choice=-1) bad FROM qq_pairs").fetchone()
    d = r["decided"] or 0
    return {"n": r["n"] or 0, "decided": d, "agree": r["agree"] or 0, "bad": r["bad"] or 0,
            "agree_rate": round((r["agree"] or 0) / d, 3) if d else None}


# ---------------- 回放 ----------------
def replay_gate(db, g: dict, days: float = 7) -> dict:
    """用现在的权重和阈值，把最近的门控日志重新判断一遍。"""
    from aigroup.chat.gate import active_weights, likelihood, passes, threshold_ex
    w, src = active_weights(db)
    rows = db.conn.execute("SELECT * FROM qq_gate_log WHERE ts>? AND mentioned=0 ORDER BY ts", (time.time() - days * 86400,)).fetchall()
    rnd = random.Random(42)
    by_gid: dict[int, list] = {}
    flips = {"new_pass": 0, "new_block": 0, "same": 0}
    examples = []
    for r in rows:
        f = json.loads(r["feats"] or "{}")
        p = likelihood(f, w)
        hist = [x for x in by_gid.get(r["group_id"], []) if r["ts"] - x[0] < 3600]
        thr, _, tie = threshold_ex([x[1] for x in hist], float(g.get("talkativeness", 0.3)),
                                   sum(1 for x in hist if x[2]))
        now_pass = passes(p, thr, tie, rnd.random())
        was = bool(r["passed"])
        key = "same" if now_pass == was else ("new_pass" if now_pass else "new_block")
        flips[key] += 1
        if key != "same" and len(examples) < 10:
            examples.append({"ts": r["ts"], "old": (r["p"], r["threshold"]), "new": (round(p, 3), round(thr, 3)), "kind": key})
        by_gid.setdefault(r["group_id"], []).append((r["ts"], p, bool(r["spoke"])))
    return {"rows": len(rows), "weights": src, **flips, "examples": examples}


async def replay_llm(db, llm, n: int = 20, days: float = 7, recent_her: int = 15) -> list[dict]:
    """挑最近 n 条 trace，用现在的模型重跑，过一遍现在的质检（不发群）。"""
    from aigroup.chat.group import GroupChat
    from aigroup.chat.quality import pick
    rows = db.conn.execute("SELECT * FROM qq_trace WHERE ts>? AND messages!='' ORDER BY RANDOM() LIMIT ?",
                           (time.time() - days * 86400, n)).fetchall()
    out = []
    for r in rows:
        msgs = trace_messages(db, r)
        try:
            raw = await llm.chat(msgs, role="chat", background=True)
        except Exception as e:  # noqa: BLE001
            out.append({"trace": r["trace"], "error": str(e)[:100]})
            continue
        plan = GroupChat.parse_plan_text(raw)
        cands = (plan or {}).get("cands") or []
        her = [x["content"] for x in db.conn.execute(
            "SELECT content FROM qq_group_messages WHERE group_id=? AND role='assistant' AND ts<? ORDER BY id DESC LIMIT ?",
            (r["group_id"], r["ts"], recent_her)).fetchall()]
        i, score, hits = pick(cands, her) if cands else (-1, 0.0, [])
        out.append({"trace": r["trace"], "old_outcome": r["outcome"], "old_sent": json.loads(r["sent"] or "[]"),
                    "new_act": (plan or {}).get("act", "parse_fail"), "new_kind": (plan or {}).get("kind", ""),
                    "new_sent": cands[i] if i >= 0 else [], "score": round(score, 2), "hits": hits})
    return out


JUDGE_PROMPT = """你是严格的审稿人，只找毛病，不夸。下面是微信群里一个叫「{name}」的角色（人设见下）在一段群聊后发的话。
找出它的缺陷，每条写类型和严重度（1 轻微 ~ 5 严重）。类型只能是：
- AI腔：像客服 / 百科 / 总结 / 列点，不像朋友随手打字
- 人设不符：和人设的性格、说话方式不一致
- 前后矛盾：和聊天记录里她自己说过的、或事实矛盾
- 答非所问：没接上要接的那句话
- 越界：冒犯人、拿外貌身材家乡开玩笑、说别人隐私、说自己是 AI
- 太长：群聊里说得太多
没有毛病就输出空列表。只输出 JSON：{{"defects": [{{"type": "...", "severity": 3, "why": "一句话"}}]}}

# 人设（节选）
{persona}

# 群聊记录（最后几条）
{context}

# 她发的话
{said}"""


async def judge_one(db, llm, persona, ctx: list[str], said: str, shots: bool = True) -> tuple[int, list]:
    """给一句话挑毛病 → (0~100 分, 毛病列表)。20.1：shots=True 时带上主人亲自打过分的例子（评委校准）。"""
    from aigroup.core.config import chan_name
    prompt = JUDGE_PROMPT.replace("微信群", chan_name()).format(
        name=persona.name, persona=persona.body[:1200], context="\n".join(ctx), said=said)
    if shots:
        from aigroup.eval.calib import few_shot_block
        fs = few_shot_block(db)
        if fs:
            prompt += fs + "\n（主人打 4~5 分的，一般没有毛病或只有轻微的；打 1~2 分的，一定有严重度 ≥ 3 的毛病）"
    d = await llm.json([{"role": "user", "content": prompt}], default={}, role="memory")
    defects = [x for x in (d or {}).get("defects", []) if isinstance(x, dict)] if isinstance(d, dict) else []
    sev = sum(max(1, min(5, int(x.get("severity") or 1))) for x in defects)
    return max(0, 100 - 5 * sev), defects


async def judge(db, llm, persona, n: int = 20, days: float = 7) -> dict:
    rows = db.conn.execute("SELECT * FROM qq_trace WHERE ts>? AND outcome='say' ORDER BY RANDOM() LIMIT ?",
                           (time.time() - days * 86400, n)).fetchall()
    out, kinds = [], {}
    for r in rows:
        msgs = trace_messages(db, r)
        user = next((m["content"] for m in msgs if m.get("role") == "user"), "")
        ctx = user.split("# 最近的群聊记录", 1)[-1].split("# 此刻", 1)[0].strip().splitlines()[-8:]
        said = " / ".join(json.loads(r["sent"] or "[]"))
        score, defects = await judge_one(db, llm, persona, ctx, said)
        for x in defects:
            kinds[str(x.get("type"))] = kinds.get(str(x.get("type")), 0) + 1
        out.append({"trace": r["trace"], "said": said, "score": score, "defects": defects})
    avg = round(sum(x["score"] for x in out) / len(out), 1) if out else None
    return {"n": len(out), "avg": avg, "kinds": kinds, "items": out}


def calibrate(db, days: float = 14) -> dict:
    """群友自己打的字也会被质检拦下多少？（误伤率；按规则分开统计）"""
    from aigroup.chat.quality import HARD, check
    rows = [r["content"] for r in db.conn.execute(
        "SELECT content FROM qq_group_messages WHERE role='user' AND ts>? AND length(content)>=2",
        (time.time() - days * 86400,)).fetchall() if not r["content"].startswith("[")]
    by: dict = {}
    hard = 0
    for t in rows:
        s, hits = check(t)
        if s >= HARD:
            hard += 1
        for h in hits:
            by.setdefault(h, []).append(t)
    return {"n": len(rows), "hard": hard, "hard_rate": round(hard / len(rows), 4) if rows else None,
            "rules": {k: {"n": len(v), "rate": round(len(v) / len(rows), 4), "examples": v[:5]} for k, v in
                      sorted(by.items(), key=lambda x: -len(x[1]))}}


def anonymize(text: str, mapping: dict, names: dict | None = None) -> str:
    """QQ 号 / 群号（5~12 位数字，汉字挨着也算）换成编号；认识的人名换成同一个编号的“某某”。"""
    def sub(m):
        q = m.group(0)
        if q not in mapping:
            mapping[q] = f"U{len(mapping) + 1:03d}"
        return mapping[q]
    out = re.sub(r"(?<!\d)\d{5,12}(?!\d)", sub, text or "")
    for name, uid in sorted((names or {}).items(), key=lambda x: -len(x[0])):
        if len(name) >= 2:
            if uid not in mapping:
                mapping[uid] = f"U{len(mapping) + 1:03d}"
            out = out.replace(name, f"群友{mapping[uid]}")
    return out


def export(db, root: Path, days: float = 7, with_text: bool = False) -> Path:
    """导出回放包。默认只导出门控特征、结果和质检（不含任何聊天文字）；with_text 才带上提示词和回复（名字、号码换成编号）。"""
    mapping: dict = {}
    names = {r["name"]: str(r["user_id"]) for r in db.conn.execute("SELECT user_id, name FROM qq_people")}
    rows = db.conn.execute("SELECT * FROM qq_trace WHERE ts>? ORDER BY ts", (time.time() - days * 86400,)).fetchall()
    d = Path(root) / "data" / "replay"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"pack-{datetime.now():%Y%m%d-%H%M}.jsonl"
    with open(p, "w", encoding="utf-8") as f:
        for r in rows:
            x = {k: r[k] for k in r.keys() if k not in ("id", "messages")}
            g = db.conn.execute("SELECT feats, p, threshold, passed, kind FROM qq_gate_log WHERE id=?",
                                (r["gate_id"],)).fetchone() if r["gate_id"] else None
            x["gate"] = dict(g) if g else None
            if with_text:
                for k in ("raw", "plan", "sent", "why"):
                    x[k] = anonymize(str(x[k]), mapping, names)
                x["system"] = anonymize(prompt_text(db, x.get("prompt_hash", "")), mapping, names)
            else:
                for k in ("raw", "plan", "sent", "why"):
                    x.pop(k, None)
            x["group_id"] = anonymize(str(x["group_id"]), mapping)
            f.write(json.dumps(x, ensure_ascii=False) + "\n")
    return p


def main(argv: list[str]) -> int:
    from aigroup.brain import GroupBrain
    from aigroup.core.config import load_config
    cfg = load_config()
    brain = GroupBrain(cfg)
    Tracer(brain.db)
    from aigroup.chat.gate import GateLog
    GateLog(brain.db)
    from aigroup.core.config import chan_group
    from aigroup.chat.settings import defaults_for
    g = {**defaults_for(cfg.get("channel")), **chan_group(cfg), **(brain.db.get("web_overrides") or {})}
    days = 7.0
    d = Path(cfg.root) / "data" / "replay"
    d.mkdir(parents=True, exist_ok=True)
    if argv and argv[0] == "calibrate" or "--calibrate" in argv:
        c = calibrate(brain.db)
        lines = [f"# 质检误伤 {datetime.now():%Y-%m-%d %H:%M}", "",
                 f"最近 14 天群友自己打的 {c['n']} 句话里，有 {c['hard']} 句会被直接拦下（{(c['hard_rate'] or 0) * 100:.1f}%）。", "",
                 "| 规则 | 命中 | 比例 | 例子 |", "| --- | --- | --- | --- |"]
        for k, v in c["rules"].items():
            lines.append(f"| {k} | {v['n']} | {v['rate'] * 100:.1f}% | {' / '.join(x[:20] for x in v['examples'][:3])} |")
        p = d / f"calibrate-{datetime.now():%Y%m%d-%H%M}.md"
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        brain.db.set("quality_calibration", {k: v for k, v in c.items() if k != "rules"} |
                     {"rules": {k: {"n": v["n"], "rate": v["rate"]} for k, v in c["rules"].items()}, "ts": time.time()})
        print("\n".join(lines[:3]))
        print("报告：", p)
        return 0
    if "--judge" in argv:
        i = argv.index("--judge")
        n = int(argv[i + 1]) if i + 1 < len(argv) and argv[i + 1].isdigit() else 20
        res = asyncio.run(judge(brain.db, brain.llm, brain.persona, n, days))
        lines = [f"# 扣分制评审 {datetime.now():%Y-%m-%d %H:%M}", "", f"{res['n']} 条，平均 {res['avg']} 分（满分 100）", "",
                 "缺陷类型：" + "、".join(f"{k} {v}" for k, v in sorted(res["kinds"].items(), key=lambda x: -x[1])), "",
                 "| 分 | 她说的 | 毛病 |", "| --- | --- | --- |"]
        for x in sorted(res["items"], key=lambda x: x["score"]):
            lines.append(f"| {x['score']} | {x['said'][:40]} | {'；'.join(str(y.get('type')) + str(y.get('severity')) + '：' + str(y.get('why'))[:30] for y in x['defects'])} |")
        p = d / f"judge-{datetime.now():%Y%m%d-%H%M}.md"
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        brain.db.set("judge_last", {"n": res["n"], "avg": res["avg"], "kinds": res["kinds"], "ts": time.time()})
        print("\n".join(lines[:5]))
        print("报告：", p)
        return 0
    if "--compare" in argv:                       # 4.3 和上一版人设盲比
        i = argv.index("--compare")
        n = int(argv[i + 1]) if i + 1 < len(argv) and argv[i + 1].isdigit() else 8
        from aigroup.eval.weekly import compare, previous_body
        old = previous_body(brain.db, brain.persona)
        if not old:
            print("还没有上一版人设（改了人设文件、启动过一次以后才有）")
            return 1
        res = asyncio.run(compare(brain.db, brain.llm, brain.persona, old, n))
        brain.db.set("persona_compare_last", res)
        lines = [f"# 人设对比 {datetime.now():%Y-%m-%d %H:%M}", "", f"{res['n']} 段聊天：新人设赢 {res['new']}、旧人设赢 {res['old']}、平 {res['tie']}", "",
                 "| 谁赢 | 新 | 旧 | 为什么 |", "| --- | --- | --- | --- |"]
        lines += [f"| {x['winner']} | {x['new'][:40]} | {x['old'][:40]} | {x['why']} |" for x in res["items"]]
        p = d / f"compare-{datetime.now():%Y%m%d-%H%M}.md"
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print("\n".join(lines[:3]))
        print("报告：", p)
        return 0
    if "--export" in argv:
        print("导出了：", export(brain.db, cfg.root, days, with_text="--with-text" in argv))
        return 0
    lines = [f"# 回放 {datetime.now():%Y-%m-%d %H:%M}", ""]
    gr = replay_gate(brain.db, g, days)
    lines += ["## 门控", f"- 最近 {days:.0f} 天没被叫到的判断 {gr['rows']} 次；权重：{gr['weights']}",
              f"- 和当时一样 {gr['same']} 次；现在会放行、当时拦了 {gr['new_pass']} 次；现在会拦、当时放了 {gr['new_block']} 次", ""]
    print("\n".join(lines[-4:]))
    n_llm = 0
    if "--llm" in argv:
        i = argv.index("--llm")
        n_llm = int(argv[i + 1]) if i + 1 < len(argv) and argv[i + 1].isdigit() else 20
    if n_llm:
        res = asyncio.run(replay_llm(brain.db, brain.llm, n_llm, days))
        lines += ["## 重跑大模型（不发群）", "", "| trace | 当时 | 现在 | 现在要发的 | 质检 |", "| --- | --- | --- | --- | --- |"]
        for x in res:
            if x.get("error"):
                lines.append(f"| {x['trace']} | 出错 | {x['error']} | | |")
                continue
            lines.append(f"| {x['trace']} | {x['old_outcome']}：{' / '.join(x['old_sent'])[:40]} | {x['new_act']} {x['new_kind']} | "
                         f"{' / '.join(x['new_sent'])[:40]} | {x['score']} {'、'.join(x['hits'])} |")
        print(f"重跑了 {len(res)} 条")
    p = d / f"report-{datetime.now():%Y%m%d-%H%M}.md"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("报告：", p)
    return 0
