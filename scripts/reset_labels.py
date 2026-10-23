"""20.4.1 把你在小屋里判过、打过分的全部清掉，从头再来（先备份数据库）。

清掉的：
- “这一刻她该不该接话”的标注（qq_gate_log.human / human_act）和程序推的弱标签（weak）
- 小判断器（kv: label_nb、label_nb_status）——两类各 30 条以后重新训练
- “给她的话打分”（qq_human_score）和评委校准结果（kv: judge_calib）
- “二选一”（qq_pairs）
- 用标注重新拟合的门控权重（kv: gate_weights、gate_refit_last）——回到默认权重，攒够了再拟合
不动的：聊天记录、画像、记忆、门控日志本身（只是去掉你的判断）。

用法（服务停着跑最稳；不停也行，SQLite 会等锁）：
    python scripts/reset_labels.py --home home/wechat [--yes]
"""
from __future__ import annotations

import argparse
import sqlite3
import time
from pathlib import Path

KV_KEYS = ("label_nb", "label_nb_status", "judge_calib", "gate_weights", "gate_refit_last")


def reset(db_path: Path, backup: bool = True) -> dict:
    if backup:
        bk = db_path.with_name(f"{db_path.stem}.before-reset-labels-{time.strftime('%Y%m%d-%H%M%S')}.db")
        src = sqlite3.connect(db_path)
        dst = sqlite3.connect(bk)
        src.backup(dst)
        dst.close()
        src.close()
    c = sqlite3.connect(db_path, timeout=30)
    tabs = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    out = {"backup": str(bk) if backup else ""}
    cols = {r[1] for r in c.execute("PRAGMA table_info(qq_gate_log)")} if "qq_gate_log" in tabs else set()
    if "human" in cols:
        out["labels"] = c.execute("SELECT COUNT(*) FROM qq_gate_log WHERE human IS NOT NULL").fetchone()[0]
        sets = ["human=NULL"] + (["human_act=''"] if "human_act" in cols else []) + (["weak=NULL"] if "weak" in cols else []) \
            + (["human_why=''"] if "human_why" in cols else [])
        c.execute(f"UPDATE qq_gate_log SET {', '.join(sets)}")
    for t in ("qq_human_score", "qq_pairs"):
        if t in tabs:
            out[t] = c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            c.execute(f"DELETE FROM {t}")
    if "kv" in tabs:
        out["kv"] = c.execute(f"DELETE FROM kv WHERE k IN ({','.join('?' * len(KV_KEYS))})", KV_KEYS).rowcount
    c.commit()
    c.close()
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--home", required=True, help="她的家，比如 home/wechat、home/qq")
    ap.add_argument("--yes", action="store_true", help="不问了，直接清")
    a = ap.parse_args()
    db = Path(a.home).expanduser().resolve() / "data" / "group.db"
    if not db.exists():
        print("找不到", db)
        return 1
    if not a.yes and input(f"要清掉 {db} 里所有的标注和打分吗？（先自动备份）输入 yes：").strip() != "yes":
        print("没清")
        return 1
    print(reset(db))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
