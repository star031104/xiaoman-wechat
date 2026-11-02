"""34.0 新旧两种设置在同一批历史时刻上配对比较（真模型，不发群）。每次发版前跑一次。

用法：python scripts/replay_pair.py --a persona_version=v1 --b persona_version=v2 [--n 12] [--days 6] [--judge]
- 从最近 days 天里挑 n 个“群友说完、她接了话”的时刻；数据库副本删掉那之后的消息，两种设置各让她想一句
- --judge：便宜模型当评委，A/B 正反顺序各判一次（位置偏差），只有两次一致才算赢
- 输出：每边说了几次、平均字数、评委胜负；明细存 data/replay_pairs/时间.json
依据：配对比较的标准误约为不配对的 0.71 倍（Anthropic Error Bars）；评委要正反各判（IJCNLP 2025）。
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
JUDGE = """朋友群里，AI 群友“{name}”在下面这段聊天之后要接话。两个候选（“[skip]”表示不说话）：
A：{a}
B：{b}

最近的聊天：
{ctx}

哪个更像群里一个有个性的真人朋友会做的（该不说就不说也算好）？看：接得对不对、自然不自然、有没有 AI 腔、有没有编东西。
只输出 A 或 B 或 平。"""


def parse_kv(xs):
    out = {}
    for x in xs or []:
        k, _, v = x.partition("=")
        v2 = {"true": True, "false": False}.get(v.lower(), v)
        try:
            v2 = float(v) if "." in v else int(v)
        except ValueError:
            pass
        out[k.strip()] = v2
    return out


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--home", default="home/wechat")
    ap.add_argument("--a", nargs="*", default=["persona_version=v1"])
    ap.add_argument("--b", nargs="*", default=["persona_version=v2"])
    ap.add_argument("--n", type=int, default=12)
    ap.add_argument("--days", type=float, default=6)
    ap.add_argument("--judge", action="store_true")
    a = ap.parse_args()
    home = ROOT / a.home
    A, B = parse_kv(a.a), parse_kv(a.b)
    src_db = home / "data" / "group.db"
    src = sqlite3.connect(f"file:{src_db}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    rows = src.execute("SELECT id, group_id, user_id, name, content, role, to_her FROM qq_group_messages WHERE ts>? ORDER BY id",
                       (time.time() - a.days * 86400,)).fetchall()
    pts = [(rows[i - 1], rows[i]) for i in range(1, len(rows))
           if rows[i]["role"] == "assistant" and rows[i - 1]["role"] == "user" and not str(rows[i - 1]["content"] or "").startswith("/")]
    step = max(1, len(pts) // max(1, a.n))
    pts = pts[::step][-a.n:]
    tmp = Path(tempfile.mkdtemp(prefix="pair_"))
    out = []
    try:
        for name in ("config.yaml", "config.llm.yaml", ".env"):
            if (home / name).exists():
                shutil.copy(home / name, tmp / name)
        shutil.copytree(home / "persona", tmp / "persona")
        (tmp / "data").mkdir()
        base = tmp / "base.db"                     # 线上库开着 WAL：直接拷文件会拷坏，用 SQLite 的 backup 拿一份一致的
        with sqlite3.connect(base) as dst:
            src.backup(dst)
        os.environ["AIGROUP_HOME"] = str(tmp)
        from aigroup.brain import GroupBrain
        from aigroup.chat.group import GroupChat
        from aigroup.core.config import load_config
        from aigroup.wechat.bot import WeChatBot
        for u, her in pts:
            for ext in ("-wal", "-shm"):                     # 上一轮留下的 WAL 文件要删掉，不然新拷的库会坏
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
            gid = int(u["group_id"])
            ctx = "\n".join(("她" if r["role"] == "assistant" else r["name"]) + "：" + str(r["content"] or "")[:60]
                            for r in src.execute("SELECT name, role, content FROM qq_group_messages WHERE group_id=? AND id<=? "
                                                 "ORDER BY id DESC LIMIT 8", (gid, u["id"])).fetchall()[::-1])
            rec = {"ctx": ctx, "real": her["content"], "to_her": u["to_her"]}
            for side, kv in (("A", A), ("B", B)):
                gc.g.update(kv)
                m = await gc.build_messages(gid, bool(u["to_her"]), int(u["user_id"]) == gc.owner_id, u["name"], None,
                                            focus=[int(u["user_id"])], query=str(u["content"] or ""))
                t = float(gc.g.get("chat_temperature", 1.1)) if gc.v2(gid) else None
                raw = await brain.llm.chat(m, role="chat", json_mode=True, temperature=t)
                try:
                    d = json.loads(raw)
                    rec[side] = " / ".join(d.get("say") or []) if d.get("act") == "say" else "[skip]"
                except Exception:  # noqa: BLE001
                    rec[side] = "[skip]"
                rec[side + "_chars"] = [len(m[0]["content"]), len(m[1]["content"])]
            if a.judge:
                votes = []
                for x, y in ((rec["A"], rec["B"]), (rec["B"], rec["A"])):
                    v = await brain.llm.chat([{"role": "user", "content": JUDGE.format(name=brain.persona.name, a=x, b=y, ctx=ctx)}],
                                             role="helper", temperature=0.0, max_tokens=5)
                    votes.append(str(v or "").strip()[:1])
                first = {"A": "A", "B": "B"}.get(votes[0], "平")
                second = {"A": "B", "B": "A"}.get(votes[1], "平")
                rec["judge"] = first if first == second else "平"
            for task in list(getattr(gc, "_bg", []) or []):      # 后台活停掉、库关掉，下一轮才能换库
                task.cancel()
            try:
                brain.db.conn.close()
            except Exception:  # noqa: BLE001
                pass
            out.append(rec)
            print(json.dumps({k: rec[k] for k in ("A", "B") if k in rec} | {"judge": rec.get("judge", "")}, ensure_ascii=False), flush=True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    def stat(side):
        said = [r[side] for r in out if r[side] != "[skip]"]
        return {"said": len(said), "avg_len": round(sum(len(x) for x in said) / max(1, len(said)), 1),
                "system_chars": round(sum(r[side + "_chars"][0] for r in out) / max(1, len(out)))}
    summ = {"ts": time.strftime("%Y-%m-%d %H:%M"), "n": len(out), "A": a.a, "B": a.b, "A_stat": stat("A"), "B_stat": stat("B")}
    if a.judge:
        summ["judge"] = {k: sum(1 for r in out if r.get("judge") == k) for k in ("A", "B", "平")}
    print(json.dumps(summ, ensure_ascii=False, indent=1))
    d = home / "data" / "replay_pairs"
    d.mkdir(exist_ok=True)
    (d / f"{time.strftime('%Y%m%d-%H%M')}.json").write_text(json.dumps({"summary": summ, "items": out}, ensure_ascii=False, indent=1),
                                                            encoding="utf-8")


if __name__ == "__main__":
    asyncio.run(main())
