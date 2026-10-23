"""21.1 用你已经判过的标注回放听者：不用等新标注，马上知道听者和你一致不一致。

对每条你判过“该接 / 不该插（时机不对）”、她当时说了的时刻：把那一刻之前的 12 条聊天和她当时说的那句交给听者，
记进 qq_gate_log.listener（标 backfill: true），最后打印一致率。之后 listener_mode=auto 会直接用这些数判断要不要接管。

用法（在服务器上跑；不发消息，只调便宜模型，几十条大约几分钱）：
    python scripts/listener_backfill.py --home home/wechat [--limit 80] [--dry]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--home", required=True)
    ap.add_argument("--limit", type=int, default=80)
    ap.add_argument("--dry", action="store_true", help="只打印，不写回数据库")
    ap.add_argument("--out", default="", help="结果另存 JSON（她在运行、数据库被锁时先存下来，用 --apply 写回）")
    ap.add_argument("--apply", default="", help="把 --out 存的结果写回数据库（服务停着的时候跑）")
    a = ap.parse_args()
    os.environ["AIGROUP_HOME"] = str(Path(a.home).expanduser().resolve())
    import sqlite3

    from aigroup.chat import listener as L
    from aigroup.core.config import load_config
    from aigroup.llm.client import LLM
    cfg = load_config()
    llm = LLM(cfg)
    name = "小满"
    try:
        from aigroup.persona import load_persona
        name = load_persona(Path(cfg["root"]) / cfg["persona_file"]).name
    except Exception:  # noqa: BLE001
        pass
    db = sqlite3.connect(Path(a.home).expanduser() / "data" / "group.db", timeout=30)
    db.row_factory = sqlite3.Row
    if a.apply:
        res = json.loads(Path(a.apply).read_text(encoding="utf-8"))
        for gid_, v in res:
            db.execute("UPDATE qq_gate_log SET listener=? WHERE id=?", (json.dumps(v, ensure_ascii=False)[:600], gid_))
        db.commit()
        print("写回了", len(res), "条")
        return
    saved = []
    rows = db.execute("SELECT g.id, g.group_id, g.ts, g.human, IFNULL(g.human_why,'') human_why, e.text her FROM qq_gate_log g "
                      "JOIN qq_reply_effect e ON e.gate_id=g.id WHERE g.human IN (0,1) AND g.spoke=1 AND g.mentioned=0 "
                      "AND IFNULL(g.listener,'')='' AND IFNULL(e.text,'')!='' ORDER BY g.id DESC LIMIT ?", (a.limit,)).fetchall()
    done = []
    for r in rows:
        if r["human"] == 0 and r["human_why"] == "说的不对":
            continue
        ctx = [dict(x) for x in reversed(db.execute(
            "SELECT ts, role, name, user_id uid, content FROM qq_group_messages WHERE group_id=? AND ts<=? "
            "ORDER BY id DESC LIMIT 12", (r["group_id"], float(r["ts"]) + 1)).fetchall())]
        if not ctx:
            continue
        now = float(r["ts"]) + 2
        sig = L.signals(ctx, name, now, 0.2, [])
        chat = "\n".join("[%d秒前] %s：%s" % (int(now - float(x["ts"])), x["name"], str(x["content"] or "")[:80]) for x in ctx)
        class _DB:                                   # listener_examples 要 .conn
            conn = db
        ex = L.listener_examples(_DB, r["group_id"], exclude=r["id"]) if hasattr(L, "listener_examples") else ""
        shots = f"群主以前判过的例子（照这个标准）：\n{ex}\n\n" if ex else ""
        prompt = L.PROMPT.format(name=name, signals=L.signals_text(sig), shots=shots, chat=chat, draft=str(r["her"])[:200],
                                 recent_acts="没有")
        try:
            out = await llm.chat([{"role": "user", "content": prompt}], role="gate", temperature=0.0, max_tokens=200)
        except Exception as e:  # noqa: BLE001
            print("出错", e)
            continue
        v = L.parse_listen(out)
        ok = (r["human"] == 1) == (v["window"] in getattr(L, "SAY", ("now", "ok")))
        done.append(ok)
        print(f"{'✓' if ok else '✗'} 你判{'该接' if r['human'] == 1 else '不该插'}，听者 {v['window']}：{v['why'][:50]}")
        saved.append([r["id"], {**v, "enforced": False, "backfill": True}])
        if not a.dry and not a.out:
            db.execute("UPDATE qq_gate_log SET listener=? WHERE id=?", (json.dumps(saved[-1][1], ensure_ascii=False)[:600], r["id"]))
            db.commit()
    if a.out:
        Path(a.out).write_text(json.dumps(saved, ensure_ascii=False), encoding="utf-8")
    rows2 = db.execute("SELECT listener, human, IFNULL(human_why,'') human_why FROM qq_gate_log WHERE human IN (0,1) "
                       "AND IFNULL(listener,'')!=''").fetchall()
    n1 = sum(1 for (i, _), ok in zip(saved, done) if ok)
    print(f"\n这次回放 {len(done)} 条，一致 {n1} 条（{n1 / max(1, len(done)):.0%}）；数据库里累计：{L.agreement(rows2)}")


if __name__ == "__main__":
    asyncio.run(main())
