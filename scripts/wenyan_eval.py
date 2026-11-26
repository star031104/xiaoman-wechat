"""38.4 “文言 → 白话”这一句值不值得加：在真实历史时刻上离线比（真模型，不发群）。

用法：python scripts/wenyan_eval.py [--n 20] [--days 40] [--low] [--judge]
- 挑时刻：--low 优先挑 18 点复核 / 之前打过 ≤2 分的她的话（按原话对回消息），不够再按时间均匀补；否则只按时间均匀挑
- 每个时刻：数据库副本删掉她那句及以后，开着 wenyan_cand 让她想一次（同一次调用里写 3 句白话 + 1 句文言），
  文言那句翻回白话，然后 4 句一起走 38.3 的挑句子（choose），记：选中哪句、文言那句的总分和名次
- --judge：便宜模型当评委，“3 句白话里挑中的那句” vs “文言转白话那句”，正反顺序各判一次，两次一致才算赢
- 输出：文言句被挑中的比例、两边平均分 / 字数、八股命中、评委胜负、多花的秒数；明细存 data/replay_pairs/wenyan-时间.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.replay_pair import JUDGE  # noqa: E402


def pick_points(src, days: float, n: int, low: bool):
    rows = src.execute("SELECT id, ts, group_id, user_id, name, content, role, to_her FROM qq_group_messages WHERE ts>? ORDER BY id",
                       (time.time() - days * 86400,)).fetchall()
    pts = [(rows[i - 1], rows[i]) for i in range(1, len(rows))
           if rows[i]["role"] == "assistant" and rows[i - 1]["role"] == "user" and not str(rows[i - 1]["content"] or "").startswith("/")]
    chosen = []
    if low:
        bad = {str(r["said"] or "").split(" / ")[0][:20] for r in src.execute("SELECT said FROM qq_human_score WHERE score<=2")}
        chosen = [p for p in pts if str(p[1]["content"] or "")[:20] in bad]
        step = max(1, len(chosen) // max(1, n))
        chosen = chosen[::step][:n]
    if len(chosen) < n:
        rest = [p for p in pts if p not in chosen]
        step = max(1, len(rest) // max(1, n - len(chosen)))
        chosen += rest[::step][:n - len(chosen)]
    return chosen


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--home", default="home/wechat")
    ap.add_argument("--n", type=int, default=20)
    ap.add_argument("--days", type=float, default=40)
    ap.add_argument("--low", action="store_true")
    ap.add_argument("--judge", action="store_true")
    ap.add_argument("--set", nargs="*", default=[], help="另外要改的设置，比如 persona_v2_file=persona/x.md")
    a = ap.parse_args()
    home = ROOT / a.home
    src = sqlite3.connect(f"file:{home / 'data' / 'group.db'}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    pts = pick_points(src, a.days, a.n, a.low)
    tmp = Path(tempfile.mkdtemp(prefix="wy_"))
    out = []
    from scripts.replay_pair import parse_kv
    extra = parse_kv(a.set)
    try:
        for name in ("config.yaml", "config.llm.yaml", ".env"):
            if (home / name).exists():
                shutil.copy(home / name, tmp / name)
        shutil.copytree(home / "persona", tmp / "persona")
        (tmp / "data").mkdir()
        base = tmp / "base.db"
        with sqlite3.connect(base) as dst:
            src.backup(dst)
        os.environ["AIGROUP_HOME"] = str(tmp)
        from aigroup.brain import GroupBrain
        from aigroup.chat.group import GroupChat
        from aigroup.chat.quality import BAGU, check
        from aigroup.core.config import load_config
        from aigroup.chat.plan import parse_group_reply
        from aigroup.wechat.bot import WeChatBot
        for u, her in pts:
            for ext in ("-wal", "-shm"):
                (tmp / "data" / f"group.db{ext}").unlink(missing_ok=True)
            shutil.copy(base, tmp / "data" / "group.db")
            c = sqlite3.connect(tmp / "data" / "group.db")
            c.execute("DELETE FROM qq_group_messages WHERE id>?", (u["id"],))
            c.commit()
            c.close()
            cfg = load_config(tmp / "config.yaml")
            brain = GroupBrain(cfg)
            q = cfg.get("wechat") or {}
            ob = WeChatBot(dict(q), Path(cfg.root))
            gc = GroupChat(cfg, brain, ob, ob.ids.id(str(q.get("owner") or "")) if q.get("owner") else 0,
                           [ob.gid(g) for g in ob.groups], [])
            gc.g.update({"wenyan_cand": True, **extra})
            gid = int(u["group_id"])
            mentioned = bool(u["to_her"])
            ctx = "\n".join(("她" if r["role"] == "assistant" else r["name"]) + "：" + str(r["content"] or "")[:60]
                            for r in src.execute("SELECT name, role, content FROM qq_group_messages WHERE group_id=? AND id<=? "
                                                 "ORDER BY id DESC LIMIT 8", (gid, u["id"])).fetchall()[::-1])
            rec = {"ctx": ctx, "real": her["content"], "to_her": mentioned}
            t0 = time.time()
            m = await gc.build_messages(gid, mentioned, int(u["user_id"]) == gc.owner_id, u["name"], None,
                                        focus=[int(u["user_id"])], query=str(u["content"] or ""))
            temp = float(gc.g.get("chat_temperature", 1.1)) if gc.v2(gid) else None
            raw = await brain.llm.chat(m, role="chat", json_mode=True, temperature=temp)
            rec["t_main"] = round(time.time() - t0, 1)
            plan = gc.parse_plan(raw) or {}
            if plan.get("act") != "say" or not plan.get("cands"):
                rec["skip"] = True
                for task in list(getattr(gc, "_bg", []) or []):      # 后台活停掉、库关掉，下一轮才能换库
                    task.cancel()
                try:
                    brain.db.conn.close()
                except Exception:  # noqa: BLE001
                    pass
                out.append(rec)
                print(json.dumps({"skip": True, "real": rec["real"][:30]}, ensure_ascii=False), flush=True)
                continue
            cands = [parse_group_reply("|||".join(x), brain.persona.name, 2) for x in plan["cands"][:3]]
            cands = [x for x in cands if x]
            rec["wy"] = plan.get("wy", "")
            t1 = time.time()
            wtxt = await gc.wenyan_cand(gid, rec["wy"], str(u["content"] or "")) if rec["wy"] else ""
            rec["t_wy"] = round(time.time() - t1, 1)
            wb = parse_group_reply(wtxt, brain.persona.name, 2) if wtxt else []
            rec["normal"] = [" / ".join(x) for x in cands]
            rec["wy_text"] = " / ".join(wb)
            qargs = dict(avoid=[], detector=None, ai_open=True, stale=gc.stale_phrases(gid), no_names=[], echo=None,
                         count_penalty=not mentioned)
            ckw = dict(recent=gc.recent_her(gid), sim=0.75, qargs=qargs, focus=[int(u["user_id"])],
                       target_uid=int(u["user_id"]), target_row=None, mentioned=mentioned, web="")
            r3 = await gc.choose(gid, cands, **ckw)                       # 只有 3 句白话时挑哪句
            allc = cands + ([wb] if wb else [])
            r4 = await gc.choose(gid, allc, **ckw)                        # 加上文言那句一起挑
            rec["pick3"] = rec["normal"][r3["best"]] if r3.get("best") is not None and r3["i"] >= 0 else "[不说]"
            rec["pick4_is_wy"] = bool(wb) and r4.get("best") == len(cands) and r4["i"] >= 0
            tab = {t["k"]: t for t in r4.get("table") or []}
            if wb:
                tw = tab.get(len(cands)) or {}
                rec["wy_total"], rec["wy_hits"] = tw.get("total"), tw.get("hits")
                ok = sorted([t["total"] for t in tab.values() if t.get("total") is not None])
                rec["wy_rank"] = (ok.index(tw["total"]) + 1) if tw.get("total") is not None else None
            best3 = (r3.get("table") or [{}])[r3.get("best") or 0] if r3.get("table") else {}
            rec["pick3_total"] = best3.get("total")
            rec["bagu_normal"] = sum(1 for x in rec["normal"] for rx in BAGU if rx.search(x))
            rec["bagu_wy"] = sum(1 for rx in BAGU if rx.search(rec["wy_text"]))
            rec["style_wy"] = check(rec["wy_text"])[1] if rec["wy_text"] else []
            if a.judge and wb and rec["pick3"] != "[不说]":
                votes = []
                for x, y in ((rec["pick3"], rec["wy_text"]), (rec["wy_text"], rec["pick3"])):
                    v = await brain.llm.chat([{"role": "user", "content": JUDGE.format(name=brain.persona.name, a=x, b=y, ctx=ctx)}],
                                             role="helper", temperature=0.0, max_tokens=5)
                    votes.append(str(v or "").strip()[:1])
                first = {"A": "白话", "B": "文言"}.get(votes[0], "平")
                second = {"A": "文言", "B": "白话"}.get(votes[1], "平")
                rec["judge"] = first if first == second else "平"
            for task in list(getattr(gc, "_bg", []) or []):
                task.cancel()
            try:
                brain.db.conn.close()
            except Exception:  # noqa: BLE001
                pass
            out.append(rec)
            print(json.dumps({k: rec.get(k) for k in ("real", "pick3", "wy", "wy_text", "pick4_is_wy", "wy_rank", "judge")},
                             ensure_ascii=False), flush=True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    done = [r for r in out if not r.get("skip") and r.get("wy_text")]
    avg = (lambda xs: round(sum(xs) / max(1, len(xs)), 2))
    summ = {"ts": time.strftime("%Y-%m-%d %H:%M"), "n": len(out), "skip": sum(1 for r in out if r.get("skip")),
            "有文言句": len(done), "文言句被挑中": sum(1 for r in done if r["pick4_is_wy"]),
            "文言句平均名次": avg([r["wy_rank"] for r in done if r.get("wy_rank")]),
            "白话挑中句平均扣分": avg([r["pick3_total"] for r in done if r.get("pick3_total") is not None]),
            "文言句平均扣分": avg([r["wy_total"] for r in done if r.get("wy_total") is not None]),
            "白话平均字数": avg([len(r["pick3"]) for r in done if r["pick3"] != "[不说]"]),
            "文言句平均字数": avg([len(r["wy_text"]) for r in done]),
            "八股（3 句白话合计 / 文言句）": [sum(r["bagu_normal"] for r in done), sum(r["bagu_wy"] for r in done)],
            "多花秒数（翻译那次）": avg([r["t_wy"] for r in done])}
    if a.judge:
        summ["评委"] = {k: sum(1 for r in done if r.get("judge") == k) for k in ("白话", "文言", "平")}
    print(json.dumps(summ, ensure_ascii=False, indent=1))
    d = home / "data" / "replay_pairs"
    d.mkdir(exist_ok=True)
    (d / f"wenyan-{time.strftime('%Y%m%d-%H%M')}.json").write_text(
        json.dumps({"summary": summ, "items": out}, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    asyncio.run(main())
