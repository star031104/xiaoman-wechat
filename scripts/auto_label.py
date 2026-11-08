"""36.0 按你在小屋里的标注，补标剩下没标的（该不该说 / 给她的话打分 / 二选一 / 每天抽查 / 规则拦对没）。

做法：拿你标过的当例子（每类按标签分层抽），先在你标过、但没拿来当例子的那部分上试一遍，算和你的一致率；
一致率够了再标剩下的。补的标注都记 src='claude'，和你标的分开：门控重新拟合时你的权重 2、补的权重 1；
“和你一致”这类统计只算你自己标的。

  python scripts/auto_label.py --home home/wechat --dry        # 只在你标过的上试，算一致率
  python scripts/auto_label.py --home home/wechat --write      # 试完再写剩下的
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import re
import sqlite3
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

from aigroup.eval.label import _pack  # noqa: E402

TZ = ZoneInfo("Asia/Shanghai")
MODEL = "deepseek-v4-pro"
EFFORT = "low"
# 她开口后 3 分钟里冲她的嫌弃 / 纠正（和日报“确认不满”同一口径）：有就标 0，不管模型怎么判
NEG = re.compile(r"闭嘴|闭麦|小嘴巴|别说了|话(好|太|真)多|何意味|你在说啥|答非所问|学了些啥|太皮|催命鬼|管管你|控制你|高情商|别太荒谬|"
                 r"^[？?]{1,5}$|这ai|这个ai|人机|胡说|不对吧|不是吧|你看不出来")
URL = "https://api.deepseek.com/v1/chat/completions"


class DB:
    def __init__(self, path):
        self.conn = sqlite3.connect(path, timeout=180)
        self.conn.row_factory = sqlite3.Row


def key(home: Path) -> str:
    for line in (home / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith("LLM_MY_DEEPSEEK_DEEPSEEK_FLASH_API_KEY="):
            return line.split("=", 1)[1].strip().strip('"')
    raise SystemExit("没找到 DeepSeek key")


async def ask(cli, k, prompt, sem, tries=3) -> dict:
    async with sem:
        for i in range(tries):
            try:
                r = await cli.post(URL, headers={"Authorization": f"Bearer {k}"}, timeout=120, json={
                    "model": MODEL, "temperature": 0, "max_tokens": 6000, "reasoning_effort": EFFORT,
                    "messages": [{"role": "user", "content": prompt}]})
                txt = r.json()["choices"][0]["message"]["content"]
                return json.loads(re.search(r"\{.*\}", txt, re.S).group(0))
            except Exception:  # noqa: BLE001
                await asyncio.sleep(2 + 3 * i)
    return {}


def ensure_cols(c):
    for t, col in (("qq_gate_log", "human_src"), ("qq_human_score", "src"), ("qq_pairs", "src"), ("qq_spot", "src")):
        cols = [r[1] for r in c.execute(f"PRAGMA table_info({t})")]
        if col not in cols:
            c.execute(f"ALTER TABLE {t} ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")
    c.commit()


def after_lines(db, gid, ts, n=4, secs=180):
    rows = db.conn.execute("SELECT name, role, content FROM qq_group_messages WHERE group_id=? AND ts>? AND ts<? ORDER BY id LIMIT ?",
                           (gid, ts, ts + secs, n)).fetchall()
    return [f"{'她' if r['role'] == 'assistant' else r['name']}：{str(r['content'] or '')[:80]}" for r in rows]


# ---------------- 1 该不该说 ----------------
GATE_RULE = """你在帮一个微信朋友群（三四个熟人）给群里的 AI 成员做标注：在某一刻，她该不该主动开口（这一刻没人 @ 她）。
标签：
- 1 该接：她不接就像没听见 / 冷场，或者接一句明显让聊天更好。再选“该怎么接”：接住 / 回答 / 追问 / 接梗 / 纠错 / 补充 / 关心
- 2 可接可不接：接了不奇怪、不接也正常
- 0 不该插：在跟别人说、话没说完、插进去突兀或者抢话。如果她当时说了，再选原因：时机不对（这一刻就不该开口）/ 说的不对（可以说，但她说的那句不行：编了、读错、梗用错、顶人、答非所问）
- -1 看不出来：上下文不够
下面是标注人自己标过的例子，严格照他的尺度（他给“可接可不接”用得多，给“该接”时多半是群友在接她的话、问她、或者明显在等人回应）。"""


def gate_item(db, r) -> str:
    d = _pack(db, r, TZ)
    ctx = "\n".join(d["context"][-10:])
    s = f"【当时的群聊】\n{ctx}\n【她怎么做的】{d['did']}"
    if r["spoke"]:
        al = after_lines(db, r["group_id"], float(r["ts"]))
        if al:
            s += "\n【之后几条】\n" + "\n".join(al)
    return s


def gate_ans(r) -> str:
    h = int(r["human"])
    if h == 1:
        return f"1（{r['human_act'] or '接住'}）"
    if h == 0:
        return f"0（{r['human_why'] or '时机不对'}）" if r["spoke"] else "0"
    return str(h)


def gate_rows(db, labeled: bool):
    cond = "g.human IS NOT NULL AND IFNULL(g.human_src,'')=''" if labeled else "g.human IS NULL"
    return db.conn.execute("SELECT g.*, e.score, e.text her_text FROM qq_gate_log g LEFT JOIN qq_reply_effect e ON e.gate_id=g.id "
                           f"WHERE g.mentioned=0 AND {cond} ORDER BY g.id").fetchall()


def parse_gate(d: dict):
    try:
        v = int(d.get("label"))
    except (TypeError, ValueError):
        return None
    if v not in (1, 2, 0, -1):
        return None
    how = str(d.get("how") or "") if v == 1 else ""
    why = str(d.get("why0") or "") if v == 0 else ""
    return v, how if how in ("接住", "回答", "追问", "接梗", "纠错", "补充", "关心") else ("接住" if v == 1 else ""), \
        why if why in ("时机不对", "说的不对") else ""


# ---------------- 2 打分 ----------------
SCORE_RULE = """你在帮标注人给微信朋友群里的 AI 成员说过的话打 1~5 分（像不像一个真朋友会说的、合不合适）：
1 很差 / 2 差 / 3 一般 / 4 好 / 5 很好。标注人打分偏严，3 分最多，5 分很少。严格照下面他打过的例子的尺度。"""


# ---------------- 3 二选一 ----------------
PAIR_RULE = """标注人在两种说法里挑更好的那个（在朋友群里更像真人、更合适）：0 选 A、1 选 B、2 都行、-1 都不好。照他的例子尺度。"""


# ---------------- 4 抽查 ----------------
SPOT_RULE = """抽查她说出去的话：她“想说的”（打草稿时的原话）和“发出去的”（发前检查改过以后）。
ok：1 发出去的没问题；2 原本想说的更好（被检查改坏了）；0 发出去的有毛病。照标注人的例子尺度。"""
MEME_RULE = """抽查她查到的网络梗 / 黑话意思对不对：1 对，0 不对。你自己知道这个梗就照你知道的判断，拿不准判 1。"""
RULE_RULE = """她的发前规则拦下 / 改写了她的一句话。判断规则拦得对不对：true 拦得对（原句确实有那个毛病），false 拦错了（原句没毛病）。"""


def shots(items, k_per, keyf, fmt):
    by = {}
    for x in items:
        by.setdefault(keyf(x), []).append(x)
    out = []
    for _, xs in sorted(by.items(), key=lambda kv: str(kv[0])):
        out += random.sample(xs, min(k_per, len(xs)))
    random.shuffle(out)
    return "\n\n".join(fmt(x) for x in out), {id(x) for x in out}


async def run(args):
    random.seed(7)
    home = Path(args.home)
    db = DB(home / "data" / "group.db")
    ensure_cols(db.conn)
    k = key(home)
    sem = asyncio.Semaphore(args.conc)
    report = {}
    async with httpx.AsyncClient() as cli:
        # ===== 1 该不该说 =====
        lab = list(gate_rows(db, True))
        random.shuffle(lab)
        hold = lab[:args.hold]
        pool = lab[args.hold:]
        def prop(rows, k):
            by = {}
            for r in rows:
                by.setdefault(int(r["human"]), []).append(r)
            tot = len(rows) or 1
            out = []
            for v, xs in by.items():
                out += random.sample(xs, min(len(xs), max(1, round(k * len(xs) / tot))))
            random.shuffle(out)
            return out
        z = [r for r in pool if int(r["human"]) == 0]
        sp0 = prop([r for r in pool if not r["spoke"] and int(r["human"]) != 0], 30) + [r for r in z if not r["spoke"]]
        sp1 = prop([r for r in pool if r["spoke"] and int(r["human"]) != 0], 26) + [r for r in z if r["spoke"]]
        random.shuffle(sp0)
        random.shuffle(sp1)
        rate = lambda rows: "、".join(f"{v} 占 {round(100 * sum(1 for r in rows if int(r['human']) == v) / max(1, len(rows)))}%" for v in (1, 2, 0))  # noqa: E731
        ex = ("## 她当时没开口的时刻（他标的比例：" + rate([r for r in lab if not r["spoke"]]) + "）\n\n"
              + "\n\n".join(f"{gate_item(db, r)}\n→ 标注：{gate_ans(r)}" for r in sp0)
              + "\n\n## 她当时开口了的时刻（他标的比例：" + rate([r for r in lab if r["spoke"]]) + "）\n\n"
              + "\n\n".join(f"{gate_item(db, r)}\n→ 标注：{gate_ans(r)}" for r in sp1))
        head = (f"{GATE_RULE}\n他的习惯：她开口了的时刻，只要那句话本身不离谱、不抢话，他多半标 1（就算没人接）；"
                "她没开口的时刻，群友在聊自己的、她接不接都行就标 2，群友在等人回应、话题明显能接、或者在接她之前的话才标 1。\n"
                "0 很重要、别漏：两个群友正一问一答、话没说完、她插进去突兀；她开口后有人回“？”“闭嘴”“何意味”“你在说啥”、纠正她、嫌她；"
                "她说的那句编了事、读错了图、答非所问、顶人——这些标 0（她开口了的再选时机不对 / 说的不对）。\n\n"
                f"# 他标过的例子\n{ex}\n\n# 现在要标的\n")
        tail = ("\n\n只输出 JSON：{\"label\": 1/2/0/-1, \"how\": \"该接时怎么接\", \"why0\": \"不该插且她说了时：时机不对/说的不对\", "
                "\"reason\": \"一句话\"}")
        res = [] if args.skip_eval else await asyncio.gather(*[ask(cli, k, head + gate_item(db, r) + tail, sem) for r in hold])
        agree = n = agree3 = 0
        conf = {}
        for r, d in zip(hold, res):
            p = parse_gate(d)
            if not p:
                continue
            n += 1
            h = int(r["human"])
            conf[(h, p[0])] = conf.get((h, p[0]), 0) + 1
            agree += p[0] == h
            bucket = lambda v: 1 if v == 1 else (0 if v == 0 else 2)  # noqa: E731
            agree3 += bucket(p[0]) == bucket(h)
        report["gate"] = {"held": n, "exact": round(agree / max(1, n), 3), "three_way(1/0/其余)": round(agree3 / max(1, n), 3), "confusion(人,补)": {f"{a},{b}": c for (a, b), c in sorted(conf.items())}}
        print("gate", report["gate"], flush=True)
        if args.write:
            todo = list(gate_rows(db, False))
            out = await asyncio.gather(*[ask(cli, k, head + gate_item(db, r) + tail, sem) for r in todo])
            w = 0
            for r, d in zip(todo, out):
                p = parse_gate(d)
                if not p:
                    continue
                db.conn.execute("UPDATE qq_gate_log SET human=?, human_act=?, human_why=?, human_src='claude' WHERE id=? AND human IS NULL",
                                (p[0], p[1], p[2], r["id"]))
                w += 1
            db.conn.commit()
            report["gate"]["written"] = w
            print("gate written", w, "of", len(todo), flush=True)

        # ===== 2 打分 =====
        from aigroup.eval.calib import _context
        sc = [dict(r) for r in db.conn.execute("SELECT * FROM qq_human_score WHERE IFNULL(src,'')=''").fetchall()]
        random.shuffle(sc)
        hold, pool = sc[:args.hold_small], sc[args.hold_small:]
        dist = "、".join(f"{v} 分 {sum(1 for x in sc if x['score'] == v)} 条" for v in range(1, 6))
        ex = "\n\n".join(f"【前面】\n{x['context']}\n【她说】{x['said']}\n→ {x['score']} 分" for x in pool)
        ex = f"（他一共打了：{dist}）\n\n" + ex
        head = f"{SCORE_RULE}\n\n# 他打过的例子\n{ex}\n\n# 现在要打的\n"
        tail = "\n\n只输出 JSON：{\"score\": 1~5, \"reason\": \"一句话\"}"
        res = [] if args.skip_eval else await asyncio.gather(*[ask(cli, k, head + f"【前面】\n{x['context']}\n【她说】{x['said']}" + tail, sem) for x in hold])
        pairs = [(x["score"], int(d.get("score") or 0)) for x, d in zip(hold, res) if d.get("score")]
        within1 = sum(1 for a, b in pairs if abs(a - b) <= 1)
        exact = sum(1 for a, b in pairs if a == b)
        report["score"] = {"held": len(pairs), "exact": round(exact / max(1, len(pairs)), 3), "within1": round(within1 / max(1, len(pairs)), 3)}
        print("score", report["score"], flush=True)
        if args.write:
            todo = db.conn.execute("SELECT * FROM qq_trace WHERE outcome='say' AND trace NOT IN (SELECT trace FROM qq_human_score)").fetchall()
            items = []
            for r in todo:
                said = " / ".join(json.loads(r["sent"] or "[]"))
                if said.strip():
                    items.append((r, said, "\n".join(_context(db, r))))
            out = await asyncio.gather(*[ask(cli, k, head + f"【前面】\n{c}\n【她说】{s}" + tail, sem) for r, s, c in items])
            w = 0
            for (r, s, c), d in zip(items, out):
                try:
                    v = max(1, min(5, int(d.get("score"))))
                except (TypeError, ValueError):
                    continue
                db.conn.execute("INSERT OR IGNORE INTO qq_human_score(trace, group_id, said, context, score, ts, src) VALUES (?,?,?,?,?,?,'claude')",
                                (r["trace"], r["group_id"], s, c, v, time.time()))
                w += 1
            db.conn.commit()
            report["score"]["written"] = w
            print("score written", w, flush=True)

        # ===== 3 二选一 =====
        from aigroup.eval.replay import trace_messages
        pr = [dict(r) for r in db.conn.execute("SELECT p.*, t.messages, t.ts tts, t.group_id, t.id tid, t.trace ttrace FROM qq_pairs p LEFT JOIN qq_trace t ON t.trace=p.trace "
                                               "WHERE IFNULL(p.src,'')=''").fetchall()]

        def pctx(row):
            try:
                row = dict(row)
                msgs = trace_messages(db, row)
                user = next((m["content"] for m in msgs if m.get("role") == "user"), "")
                return "\n".join(user.split("# 最近的群聊记录", 1)[-1].split("# 此刻", 1)[0].strip().splitlines()[1:][-6:])
            except Exception as e:  # noqa: BLE001
                print("pctx", e, flush=True)
                return ""

        def pfmt(ctx, a, b):
            return f"【前面】\n{ctx}\n【A】{' / '.join(map(str, a))}\n【B】{' / '.join(map(str, b))}"
        for x in pr:
            x["ctx"] = pctx(x) if x.get("tid") else ""
            x["A"], x["B"] = json.loads(x["a"] or "[]"), json.loads(x["b"] or "[]")
        random.shuffle(pr)
        hold, pool = pr[:args.hold_small], pr[args.hold_small:]
        ex, _ = shots(pool, 5, lambda x: x["choice"], lambda x: pfmt(x["ctx"], x["A"], x["B"]) + f"\n→ {x['choice']}")
        head = f"{PAIR_RULE}\n\n# 他选过的例子\n{ex}\n\n# 现在要选的\n"
        tail = "\n\n只输出 JSON：{\"choice\": 0/1/2/-1, \"reason\": \"一句话\"}"
        res = [] if args.skip_eval else await asyncio.gather(*[ask(cli, k, head + pfmt(x["ctx"], x["A"], x["B"]) + tail, sem) for x in hold])
        ok = [(x["choice"], d.get("choice")) for x, d in zip(hold, res) if d.get("choice") is not None]
        print("pairs pool", len(pr), "hold", len(hold), "empty", sum(1 for d in res if not d), flush=True)
        report["pairs"] = {"held": len(ok), "exact": round(sum(1 for a, b in ok if int(a) == int(b)) / max(1, len(ok)), 3)}
        print("pairs", report["pairs"], flush=True)
        if args.write:
            from aigroup.eval.replay import next_pair  # noqa: F401
            todo = db.conn.execute("""SELECT * FROM qq_trace WHERE plan LIKE '%"cands"%' AND trace NOT IN (SELECT trace FROM qq_pairs)""").fetchall()
            items = []
            for r in todo:
                try:
                    plan = json.loads(r["plan"] or "{}")
                    sent = json.loads(r["sent"] or "[]")
                except ValueError:
                    continue
                cands = [c for c in plan.get("cands") or [] if c]
                if len(cands) < 2:
                    continue
                a, b = cands[0], cands[1]
                si = 0 if sent and sent[0] in " ".join(map(str, a)) else 1 if sent and sent[0] in " ".join(map(str, b)) else -1
                items.append((r, a, b, si, pctx(r)))
            out = await asyncio.gather(*[ask(cli, k, head + pfmt(c, a, b) + tail, sem) for r, a, b, si, c in items])
            w = 0
            for (r, a, b, si, c), d in zip(items, out):
                try:
                    ch = int(d.get("choice"))
                except (TypeError, ValueError):
                    continue
                if ch not in (0, 1, 2, -1):
                    continue
                db.conn.execute("INSERT OR IGNORE INTO qq_pairs(trace, a, b, sent, choice, ts, src) VALUES (?,?,?,?,?,?,'claude')",
                                (r["trace"], json.dumps(a, ensure_ascii=False), json.dumps(b, ensure_ascii=False), si, ch, time.time()))
                w += 1
            db.conn.commit()
            report["pairs"]["written"] = w
            print("pairs written", w, flush=True)

        # ===== 4 抽查（说的 / 梗）=====
        def spot_say(r):
            try:
                plan = json.loads(r["plan"] or "{}")
                sent = json.loads(r["sent"] or "[]")
            except ValueError:
                return None
            if not sent:
                return None
            prev = db.conn.execute("SELECT name, content FROM qq_group_messages WHERE group_id=? AND role='user' AND ts<=? ORDER BY id DESC LIMIT 3",
                                   (r["group_id"], float(r["ts"]))).fetchall()
            before = "\n".join(f"{p['name']}：{str(p['content'] or '')[:80]}" for p in reversed(prev))
            return f"【前面】\n{before}\n【想说的】{' / '.join(plan.get('say') or [])}\n【发出去的】{' / '.join(map(str, sent))}"
        lab = db.conn.execute("SELECT s.ok, t.* FROM qq_spot s JOIN qq_trace t ON t.id=s.ref WHERE s.kind='say' AND IFNULL(s.src,'')=''").fetchall()
        exs = [(spot_say(r), r["ok"]) for r in lab]
        exs = [x for x in exs if x[0]]
        head = f"{SPOT_RULE}\n\n# 他点过的例子\n" + "\n\n".join(f"{t}\n→ {o}" for t, o in exs) + "\n\n# 现在要点的\n"
        tail = "\n\n只输出 JSON：{\"ok\": 1/2/0, \"reason\": \"一句话\"}"
        if args.write:
            todo = db.conn.execute("SELECT * FROM qq_trace WHERE outcome='say' AND id NOT IN (SELECT ref FROM qq_spot WHERE kind='say')").fetchall()
            items = [(r, spot_say(r)) for r in todo]
            items = [x for x in items if x[1]]
            out = await asyncio.gather(*[ask(cli, k, head + t + tail, sem) for r, t in items])
            w = 0
            for (r, t), d in zip(items, out):
                try:
                    o = int(d.get("ok"))
                except (TypeError, ValueError):
                    continue
                if o in (0, 1, 2):
                    db.conn.execute("INSERT INTO qq_spot(ts, group_id, kind, ref, ok, note, src) VALUES (?,?,?,?,?,?,'claude')",
                                    (time.time(), r["group_id"], "say", r["id"], o, str(d.get("reason") or "")[:200]))
                    w += 1
            memes = db.conn.execute("SELECT id, group_id, term, meaning FROM qq_jargon WHERE src IN ('web','web-weekly') AND meaning!='' "
                                    "AND id NOT IN (SELECT ref FROM qq_spot WHERE kind='meme')").fetchall()
            out = await asyncio.gather(*[ask(cli, k, f"{MEME_RULE}\n\n梗：{m['term']}\n查到的意思：{m['meaning']}\n\n只输出 JSON：{{\"ok\": 1/0, \"reason\": \"一句话\"}}", sem) for m in memes])
            wm = 0
            for m, d in zip(memes, out):
                try:
                    o = int(d.get("ok"))
                except (TypeError, ValueError):
                    continue
                if o in (0, 1):                 # 判错的只记下来，不像你点“不对”那样清掉意思（等你确认）
                    db.conn.execute("INSERT INTO qq_spot(ts, group_id, kind, ref, ok, note, src) VALUES (?,?,?,?,?,?,'claude')",
                                    (time.time(), m["group_id"], "meme", m["id"], o, str(d.get("reason") or "")[:200]))
                    wm += 1
            db.conn.commit()
            report["spot"] = {"say_written": w, "meme_written": wm}
            print("spot", report["spot"], flush=True)

            # ===== 5 规则拦对没 =====
            row = db.conn.execute("SELECT v FROM kv WHERE k='rule_samples'").fetchone()
            sm = json.loads(row["v"]) if row else []
            judged = [x for x in sm if x.get("ok") is not None]
            exr = "\n".join(f"规则 {x.get('rules')}（{x.get('action')}）：「{x.get('text')}」→ {'true' if x['ok'] else 'false'}" for x in judged[:25])
            todo = [x for x in sm if x.get("ok") is None]
            out = await asyncio.gather(*[ask(cli, k, f"{RULE_RULE}\n\n# 他判过的\n{exr}\n\n# 现在要判的\n规则 {x.get('rules')}（{x.get('action')}）：「{x.get('text')}」"
                                             "\n\n只输出 JSON：{\"ok\": true/false, \"reason\": \"一句话\"}", sem) for x in todo])
            wr = 0
            for x, d in zip(todo, out):
                if isinstance(d.get("ok"), bool):
                    x["ok"], x["src"] = d["ok"], "claude"
                    wr += 1
            db.conn.execute("UPDATE kv SET v=? WHERE k='rule_samples'", (json.dumps(sm, ensure_ascii=False),))
            db.conn.commit()
            report["rules"] = {"written": wr}
            print("rules", report["rules"], flush=True)
    print(json.dumps(report, ensure_ascii=False))
    (home / "data" / "auto_label_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--home", default="home/wechat")
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--hold", type=int, default=100)
    ap.add_argument("--hold-small", type=int, default=30)
    ap.add_argument("--conc", type=int, default=8)
    ap.add_argument("--skip-eval", action="store_true")
    asyncio.run(run(ap.parse_args()))
