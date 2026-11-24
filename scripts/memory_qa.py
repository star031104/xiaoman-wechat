"""38.1 记忆问答抽查：拿一套“群友会怎么问”的题，在数据库副本上走一遍线上同样的事实召回，看答案在不在给出的几条里。

题目放在 <home>/data/memory_questions.json（群友的事，不进仓库）：[{"q": "小王什么时候考试", "key": "21"}, ...]
key 是答案里一定会出现的字。每次复核后跑一次，结果追加到 data/memory_eval_runs.jsonl（kind=qa）。
用法：python scripts/memory_qa.py [--home home/wechat] [--db 另一个数据库文件]（--db 用来比复核前后）
"""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--home", default="home/wechat")
    ap.add_argument("--db")
    ap.add_argument("--gid", type=int)
    ap.add_argument("--name", default="小满")
    ap.add_argument("-v", action="store_true")
    a = ap.parse_args()
    home = ROOT / a.home
    qs = json.loads((home / "data" / "memory_questions.json").read_text(encoding="utf-8"))
    tmp = Path(tempfile.mkdtemp(prefix="memqa_"))
    try:
        src = sqlite3.connect(f"file:{a.db or home / 'data' / 'group.db'}?mode=ro", uri=True)
        with sqlite3.connect(tmp / "g.db") as dst:            # WAL 库用 backup 拿一份一致的，召回会写 last_hit，别动原库
            src.backup(dst)
        src.close()
        from aigroup.core.store import Store
        from aigroup.memory.facts import Memory
        db = Store(tmp / "g.db")
        m = Memory(db)
        m.self_aliases = [a.name]
        gid = a.gid or db.conn.execute("SELECT group_id FROM qq_group_messages LIMIT 1").fetchone()[0]
        hit = 0
        for x in qs:
            f = m.recall(gid, x["q"])["facts"]
            db.conn.execute("UPDATE qq_facts SET last_hit=NULL")
            ok = any(str(x["key"]).replace(" ", "") in str(r["text"]).replace(" ", "") for r in f)
            hit += ok
            if a.v or not ok:
                print(("✓ " if ok else "✗ ") + x["q"], "|", " / ".join(str(r["text"])[:30] for r in f))
        res = {"ts": time.time(), "kind": "qa", "db": a.db or "live", "n": len(qs), "hit": hit, "rate": round(hit / max(1, len(qs)), 3)}
        print(json.dumps(res, ensure_ascii=False))
        if not a.db:
            with open(home / "data" / "memory_eval_runs.jsonl", "a", encoding="utf-8") as fh:
                fh.write(json.dumps(res, ensure_ascii=False) + "\n")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
