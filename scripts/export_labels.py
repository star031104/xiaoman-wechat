"""36.0 B1 把“这一刻该不该说”的标注导成 jsonl（自己的 When2Speak 数据集）：每行一刻，带当时的聊天、她做了什么、之后几条、
你的标注（src=''）或照你补的（src='claude'）、门控当时的判断。用来评门控 / 局面判断 / 听者，攒够了也能拿去微调。

  python scripts/export_labels.py --home home/wechat --out data/when2speak.jsonl [--mine-only]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aigroup.eval.label import _pack  # noqa: E402


class DB:
    def __init__(self, path):
        self.conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        self.conn.row_factory = sqlite3.Row


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--home", default="home/wechat")
    ap.add_argument("--out", default="")
    ap.add_argument("--mine-only", action="store_true")
    a = ap.parse_args()
    home = Path(a.home)
    db = DB(home / "data" / "group.db")
    cond = "IFNULL(g.human_src,'')=''" if a.mine_only else "1=1"
    rows = db.conn.execute("SELECT g.*, e.score, e.text her_text FROM qq_gate_log g LEFT JOIN qq_reply_effect e ON e.gate_id=g.id "
                           f"WHERE g.mentioned=0 AND g.human IN (0,1,2) AND {cond} ORDER BY g.id").fetchall()
    out = Path(a.out or home / "data" / "when2speak.jsonl")
    n = 0
    with out.open("w", encoding="utf-8") as f:
        for r in rows:
            d = _pack(db, r, ZoneInfo("Asia/Shanghai"))
            after = [f"{'她' if x['role'] == 'assistant' else x['name']}：{str(x['content'] or '')[:100]}" for x in db.conn.execute(
                "SELECT name, role, content FROM qq_group_messages WHERE group_id=? AND ts>? AND ts<? ORDER BY id LIMIT 4",
                (r["group_id"], float(r["ts"]), float(r["ts"]) + 180)).fetchall()]
            f.write(json.dumps({"id": r["id"], "ts": r["ts"], "context": d["context"], "did": d["did"], "spoke": d["spoke"],
                                "after": after, "label": int(r["human"]), "how": r["human_act"] or "", "why0": r["human_why"] or "",
                                "src": r["human_src"] if "human_src" in r.keys() else "", "gate": {"p": d["p"], "thr": d["thr"],
                                "passed": bool(r["passed"]), "why": r["why"] or ""}}, ensure_ascii=False) + "\n")
            n += 1
    print(f"导出 {n} 条 → {out}")


if __name__ == "__main__":
    main()
