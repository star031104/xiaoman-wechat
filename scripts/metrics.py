"""22.0 看她这几天的确定性指标、个性保留、发前检查命中了什么（只读数据库，不花钱）。

用法：
    python scripts/metrics.py --home home/wechat [--days 7] [--checks 20]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class _DB:
    def __init__(self, conn):
        self.conn = conn

    def get(self, k, default=None):
        r = self.conn.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        if not r:
            return default
        try:
            return json.loads(r[0])
        except (TypeError, ValueError):
            return r[0]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--home", required=True)
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--checks", type=int, default=20, help="列出最近几条检查命中")
    a = ap.parse_args()
    db = Path(a.home).expanduser().resolve() / "data" / "group.db"
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    from aigroup.eval.daily import summary
    d = _DB(c)
    print("== 每天的指标 ==")
    print(summary(d, None, a.days))
    base = d.get("metrics_baseline") or {}
    for gid, b in base.items():
        al = (d.get(f"flavor_alert_{gid}") or {}).get("alerts")
        print(f"\n群 {gid} 的味道基线：{b.get('flavor')}" + (f"\n  ⚠ 个性在被磨平：{'；'.join(al)}" if al else ""))
    rx = d.get("drift_rx") or {}
    if rx.get("rx"):
        print("\n== 走样处方 ==\n" + "\n".join(f"- {x}" for x in rx["rx"]))
    try:
        rows = c.execute("SELECT * FROM qq_check_log ORDER BY id DESC LIMIT ?", (a.checks,)).fetchall()
    except sqlite3.OperationalError:
        rows = []
    if rows:
        print("\n== 最近的发前检查 ==")
        for r in rows:
            print(f"{time.strftime('%m-%d %H:%M', time.localtime(r['ts']))} [{r['name']}/{r['mode']}] {r['detail'][:60]}"
                  f"\n    她原本想说：{r['text'][:80]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
