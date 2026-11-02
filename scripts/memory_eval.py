"""34.0 记忆测试集（照 HaluMem 分三步看：抽取 / 更新 / 召回），每次改记忆前后各跑一次比。

用法：python scripts/memory_eval.py [--home home/wechat] [--n 40] [--rebuild]
- 第一次跑：从她记下的事里挑 n 条，让模型出“群友会怎么问”的题（不照抄原话），存 data/memory_eval.json；以后都用这一套题
- 召回：每道题走一遍线上同样的 recall，看那条事实或它的原消息在不在前 4 条里（hit@4）
- 抽取：抽 30 句像在说自己事的群友原话（一天以前的），看有没有记成事实（覆盖率）
- 更新：同一个人同一种单值属性（学校、工作、住哪……）还有两条以上都“有效”的，算漏改
在数据库副本上跑，不改线上数据。结果追加到 data/memory_eval_runs.jsonl。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import re
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SELF_RX = re.compile(r"我(明天|下周|这周|周[一二三四五六日天]|今晚|最近|在|要|去|考|面试|实习|搬|买|报|换|生日|住|学校|公司|打算|准备)")
QPROMPT = """下面是一个朋友群里记下的几件事。每件事出一道“群友之后可能会怎么问起它”的问题：口语、短，不要照抄原句的词（换个说法），
答案就是这件事。只输出 JSON：{"编号": "问题"}。

%s"""


def bigrams(s: str) -> set:
    s = re.sub(r"[^一-鿿A-Za-z0-9]", "", s or "")
    return {s[i:i + 2] for i in range(len(s) - 1)}


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--home", default="home/wechat")
    ap.add_argument("--n", type=int, default=40)
    ap.add_argument("--rebuild", action="store_true")
    a = ap.parse_args()
    home = ROOT / a.home
    tmp = Path(tempfile.mkdtemp(prefix="memeval_"))
    try:
        for name in ("config.yaml", "config.llm.yaml", ".env"):
            if (home / name).exists():
                shutil.copy(home / name, tmp / name)
        if (home / "persona").exists():
            shutil.copytree(home / "persona", tmp / "persona")
        (tmp / "data").mkdir()
        src = sqlite3.connect(f"file:{home / 'data' / 'group.db'}?mode=ro", uri=True)   # WAL：用 backup 拿一份一致的
        with sqlite3.connect(tmp / "data" / "group.db") as dst:
            src.backup(dst)
        src.close()
        os.environ["AIGROUP_HOME"] = str(tmp)
        from aigroup.brain import GroupBrain
        from aigroup.core.config import load_config
        from aigroup.memory.facts import Memory, load_embedder, single_attr
        cfg = load_config(tmp / "config.yaml")
        brain = GroupBrain(cfg)
        db = brain.db
        mem = Memory(db, cfg.tz, load_embedder("", cfg))
        gid = int(db.conn.execute("SELECT group_id FROM qq_group_messages GROUP BY group_id ORDER BY COUNT(*) DESC LIMIT 1").fetchone()[0])
        qfile = home / "data" / "memory_eval.json"
        qs = json.loads(qfile.read_text(encoding="utf-8")) if qfile.exists() and not a.rebuild else []
        if not qs:
            facts = [dict(r) for r in db.conn.execute(
                "SELECT id, text, msg_id, subject_id FROM qq_facts WHERE group_id=? AND t_invalid IS NULL AND msg_id IS NOT NULL "
                "AND kind IN ('fact','event','plan') AND IFNULL(source,'')!='guess' ORDER BY RANDOM() LIMIT ?", (gid, a.n))]
            for i in range(0, len(facts), 10):
                chunk = facts[i:i + 10]
                d = await brain.llm.json([{"role": "user", "content": QPROMPT % "\n".join(f"{j}. {f['text']}" for j, f in enumerate(chunk))}],
                                         default={}, role="helper")
                for j, f in enumerate(chunk):
                    q = str((d or {}).get(str(j)) or "").strip()
                    if q:
                        qs.append({"fact_id": f["id"], "msg_id": f["msg_id"], "answer": f["text"], "question": q})
            qfile.write_text(json.dumps(qs, ensure_ascii=False, indent=1), encoding="utf-8")
            print(f"出了 {len(qs)} 道题，存在 {qfile}")
        hit = 0
        misses = []
        for q in qs:
            found = mem.recall(gid, q["question"])
            fids = {int(f.get("id") or 0) for f in found.get("facts") or []}
            mids = {int(m.get("id") or 0) for m in found.get("messages") or []}
            ok = q["fact_id"] in fids or (q["msg_id"] and int(q["msg_id"]) in mids) or any(
                len(bigrams(q["answer"]) & bigrams(f.get("text") or "")) >= max(3, len(bigrams(q["answer"])) // 2)
                for f in found.get("facts") or [])
            hit += bool(ok)
            if not ok:
                misses.append(f"{q['question']} → {q['answer']}")
        day_ago = time.time() - 86400
        rows = [dict(r) for r in db.conn.execute(
            "SELECT id, content, ts FROM qq_group_messages WHERE group_id=? AND role='user' AND ts<? AND length(content)>=10 "
            "AND content NOT LIKE '[%' ORDER BY id DESC LIMIT 3000", (gid, day_ago))]
        cand = [r for r in rows if SELF_RX.search(str(r["content"] or ""))]
        random.Random(7).shuffle(cand)
        cand = cand[:30]
        covered = 0
        for r in cand:
            near = db.conn.execute("SELECT text FROM qq_facts WHERE group_id=? AND (msg_id=? OR (t_valid BETWEEN ? AND ?))",
                                   (gid, r["id"], r["ts"] - 600, r["ts"] + 6 * 3600)).fetchall()
            if any(len(bigrams(r["content"]) & bigrams(x["text"])) >= 3 for x in near):
                covered += 1
        valid = [dict(r) for r in db.conn.execute("SELECT id, subject_id, text FROM qq_facts WHERE group_id=? AND t_invalid IS NULL "
                                                  "AND subject_id IS NOT NULL AND subject_id NOT IN (0, -1)", (gid,))]
        seen, stale = {}, 0
        for f in valid:
            at = single_attr(f["text"])
            if at:
                key = (f["subject_id"], at)
                seen[key] = seen.get(key, 0) + 1
        stale = sum(n - 1 for n in seen.values() if n > 1)
        replaced = db.conn.execute("SELECT COUNT(*) FROM qq_facts WHERE group_id=? AND t_invalid IS NOT NULL", (gid,)).fetchone()[0]
        res = {"ts": time.strftime("%Y-%m-%d %H:%M"), "questions": len(qs), "recall_hit@4": round(hit / max(1, len(qs)), 3),
               "extract_coverage": round(covered / max(1, len(cand)), 3), "extract_sampled": len(cand),
               "update_missed": stale, "facts_valid": len(valid), "facts_replaced": replaced}
        print(json.dumps(res, ensure_ascii=False, indent=1))
        if misses:
            print("没召回的（前 10 条）：\n" + "\n".join("  - " + x for x in misses[:10]))
        with open(home / "data" / "memory_eval_runs.jsonl", "a", encoding="utf-8") as fp:
            fp.write(json.dumps(res, ensure_ascii=False) + "\n")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
