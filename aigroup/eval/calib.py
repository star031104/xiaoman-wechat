"""20.1 评委校准：你在小屋给她说过的话打 1~5 分，所有 LLM 打分都拿这些当例子，并且每周看评委和你一致不一致。

为什么：LLM 当评委对角色回复几乎分不出好坏（LLM vs 真人角色扮演 2025.9：零样本 r≈0，给 6 条人工打分样例后 0.659）；
PersonaEval 2025：最好的模型判“这句话是谁说的”只有 69%，人 91%。所以评委的分要先和你的分对一对。

- 出题：最近 14 天她真的说出口的话（qq_trace.outcome='say'），带那一刻前面几句，没打过分的随机出
- 你打的分存 qq_human_score；每周评测时用评委给同样这些话重新打分，算 Spearman 相关
- few_shot()：高中低各 2 条，塞进每周评测、定向测试、查资料评分的提示里
- 相关 < 0.6（或者打分少于 10 条）：小屋里周评测的分数标“评委还没校准 / 不可信”
"""
from __future__ import annotations

import json
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS qq_human_score (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trace TEXT NOT NULL UNIQUE,
    group_id INTEGER NOT NULL,
    said TEXT NOT NULL DEFAULT '',
    context TEXT NOT NULL DEFAULT '',
    score INTEGER NOT NULL,          -- 1 很差 ~ 5 很好（像不像她、合不合适）
    ts REAL NOT NULL
);
"""
MIN_TRUST = 0.6
MIN_N = 10


def ensure(db):
    db.conn.executescript(SCHEMA)
    db.conn.commit()


def _context(db, r, n: int = 6) -> list[str]:
    rows = list(reversed(db.conn.execute(
        "SELECT name, role, content FROM qq_group_messages WHERE group_id=? AND ts<? ORDER BY id DESC LIMIT ?",
        (r["group_id"], float(r["ts"]) - 0.5, n)).fetchall()))
    return [f"{'她' if c['role'] == 'assistant' else c['name']}：{str(c['content'])[:100]}" for c in rows]


def next_items(db, gid: int | None, n: int = 5, days: float = 14) -> list[dict]:
    ensure(db)
    q = ("SELECT * FROM qq_trace WHERE outcome='say' AND ts>? AND trace NOT IN (SELECT trace FROM qq_human_score)"
         + (" AND group_id=?" if gid else "") + " ORDER BY RANDOM() LIMIT ?")
    args = [time.time() - days * 86400] + ([gid] if gid else []) + [n]
    out = []
    for r in db.conn.execute(q, args).fetchall():
        said = " / ".join(json.loads(r["sent"] or "[]"))
        if not said.strip():
            continue
        out.append({"trace": r["trace"], "gid": r["group_id"], "said": said, "context": _context(db, r),
                    "mentioned": bool(r["mentioned"])})
    return out


def answer(db, trace: str, score: int) -> bool:
    ensure(db)
    r = db.conn.execute("SELECT * FROM qq_trace WHERE trace=?", (trace,)).fetchone()
    if not r:
        return False
    s = max(1, min(5, int(score)))
    said = " / ".join(json.loads(r["sent"] or "[]"))
    db.conn.execute("INSERT OR REPLACE INTO qq_human_score(trace, group_id, said, context, score, ts) VALUES (?,?,?,?,?,?)",
                    (trace, r["group_id"], said, "\n".join(_context(db, r)), s, time.time()))
    db.conn.commit()
    return True


def scored(db, limit: int = 200) -> list[dict]:
    ensure(db)
    return [dict(r) for r in db.conn.execute("SELECT * FROM qq_human_score ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]


def few_shot(db, k: int = 2) -> str:
    """高（4~5）、中（3）、低（1~2）各 k 条，最近打的优先。没有就返回空。"""
    try:
        ensure(db)
        out = []
        for lo, hi, word in ((4, 5, "好"), (3, 3, "一般"), (1, 2, "差")):
            for r in db.conn.execute("SELECT said, context, score FROM qq_human_score WHERE score BETWEEN ? AND ? "
                                     "ORDER BY id DESC LIMIT ?", (lo, hi, k)).fetchall():
                ctx = " / ".join(str(r["context"]).splitlines()[-3:])
                out.append(f"- 前面：{ctx[:160]}\n  她说：{r['said'][:80]}\n  主人打分：{r['score']}（{word}）")
        return "\n".join(out)
    except Exception:  # noqa: BLE001
        return ""


def few_shot_block(db) -> str:
    fs = few_shot(db)
    return f"\n\n# 主人亲自打过分的例子（1 很差 ~ 5 很好；照这个标准判，别比主人宽松）\n{fs}" if fs else ""


def spearman(a: list[float], b: list[float]) -> float | None:
    n = len(a)
    if n < 3 or len(b) != n:
        return None

    def ranks(x):
        order = sorted(range(n), key=lambda i: x[i])
        r = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and x[order[j + 1]] == x[order[i]]:
                j += 1
            for k in range(i, j + 1):
                r[order[k]] = (i + j) / 2 + 1
            i = j + 1
        return r
    ra, rb = ranks(a), ranks(b)
    ma, mb = sum(ra) / n, sum(rb) / n
    cov = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
    va = sum((x - ma) ** 2 for x in ra) ** 0.5
    vb = sum((y - mb) ** 2 for y in rb) ** 0.5
    if not va or not vb:
        return None
    return round(cov / (va * vb), 3)


def kripp_alpha(a: list[float], b: list[float]) -> float | None:
    """22.1 两个人（你 / 评委）给同一批话打分的 Krippendorff α（interval）：1 完全一致，0 和瞎蒙一样。
    α = 1 - (N-1) Σ(a-b)² / Σ_{i<j}(v_i-v_j)²，N = 2n 个分数放在一起。"""
    n = len(a)
    if n < 5 or len(b) != n:
        return None
    vals = list(a) + list(b)
    N = len(vals)
    de = sum((vals[i] - vals[j]) ** 2 for i in range(N) for j in range(i + 1, N))
    if not de:
        return None
    return round(1 - (N - 1) * sum((x - y) ** 2 for x, y in zip(a, b)) / de, 3)


def to_human_scale(js: list[float], hs: list[float]) -> list[float]:
    """评委是 0~100、你是 1~5：按名次对到你打分的分布上（评委第 k 名 → 你的分数里第 k 小的那个），再算 α。"""
    order = sorted(range(len(js)), key=lambda i: js[i])
    hsorted = sorted(hs)
    out = [0.0] * len(js)
    for rank, i in enumerate(order):
        out[i] = hsorted[rank]
    return out


MIN_ALPHA = 0.667                  # Krippendorff 建议的“可以下初步结论”的线


async def calibrate_judge(db, llm, persona, limit: int = 60) -> dict:
    """拿你打过分的话让评委重打（和每周评测同一个提示），算 Spearman。结果存 judge_calib。"""
    from aigroup.eval.replay import judge_one
    rows = scored(db, limit)
    if len(rows) < MIN_N:
        res = {"n": len(rows), "rho": None, "trusted": False, "why": f"你打的分还不够 {MIN_N} 条（现在 {len(rows)}）",
               "ts": time.time()}
        db.set("judge_calib", res)
        return res
    shot_said = set(x.split("\n  她说：", 1)[-1].split("\n", 1)[0] for x in few_shot(db).split("\n- ") if "她说：" in x)
    hs, js = [], []
    for r in rows:
        if r["said"][:80] in shot_said:                  # 当了例子的不拿来考评委
            continue
        try:
            sc, _ = await judge_one(db, llm, persona, str(r["context"]).splitlines()[-8:], r["said"], shots=True)
        except Exception:  # noqa: BLE001
            continue
        hs.append(float(r["score"]))
        js.append(float(sc))
    rho = spearman(hs, js)
    alpha = kripp_alpha(hs, to_human_scale(js, hs)) if hs else None
    ok_rho = rho is not None and rho >= MIN_TRUST
    ok_alpha = alpha is None or len(hs) < 20 or alpha >= MIN_ALPHA      # 22.1 够 20 条以后还要 α 过线
    why = ("" if ok_rho and ok_alpha else
           f"评委和你的打分相关只有 {rho}（要 ≥ {MIN_TRUST}）" if not ok_rho else
           f"评委和你的一致性 α 只有 {alpha}（要 ≥ {MIN_ALPHA}）：这周的评分只当参考")
    res = {"n": len(hs), "rho": rho, "alpha": alpha, "trusted": ok_rho and ok_alpha, "ts": time.time(), "why": why}
    db.set("judge_calib", res)
    return res
