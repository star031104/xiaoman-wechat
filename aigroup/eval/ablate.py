"""20.1 模块消融的结果：提示词里每一段到底有没有用。

她主动开口时，prompt.pick_ablation 以 ablate_rate 的概率随机去掉一段，去掉的名字记在 qq_gate_log.ablated。
这里按模块比两组的“被接率”（qq_reply_effect.score ≥ 0.6）：去掉这段的 vs 什么都没去掉的。
两组各 ≥ MIN_N 条才下结论，用两比例差的 95% 置信区间：
- 区间整体 < 0（去掉以后更差）→ 这段有用
- 区间跨 0 → 还看不出来
- 区间整体 > 0（去掉以后反而更好）→ 这段在帮倒忙，值得改或者砍
"""
from __future__ import annotations

import math
import time

NAMES = {
    "recall": "想起来的事", "lore": "群梗词条", "known": "她知道的事", "today": "今天说过的", "people_today": "大家今天的状态",
    "absent": "谁刚回来", "rel_exp": "和他相处的经验", "spec": "相处说明书", "signatures": "情境签名", "held": "憋着没说的话",
    "examples": "对话示例", "person_card": "人卡", "card_state": "人卡·今天状态", "card_absent": "人卡·刚回来",
    "card_avoid": "人卡·别碰", "card_tease": "人卡·能调侃", "card_how": "人卡·跟他怎么说", "card_favor": "人卡·人情",
    "card_care": "人卡·在意",
}
MIN_N = 30


def ablation_stats(db, days: float = 28, gid: int | None = None) -> dict:
    since = time.time() - days * 86400
    q = ("SELECT g.ablated a, e.score s FROM qq_gate_log g JOIN qq_reply_effect e ON e.gate_id=g.id AND e.done=1 "
         "WHERE g.mentioned=0 AND g.spoke=1 AND g.ts>?" + (" AND g.group_id=?" if gid else ""))
    try:
        rows = db.conn.execute(q, [since] + ([gid] if gid else [])).fetchall()
    except Exception:  # noqa: BLE001        # 老库没有 ablated 列
        return {"base": None, "mods": []}
    by: dict[str, list[float]] = {}
    for r in rows:
        by.setdefault(str(r["a"] or ""), []).append(1.0 if float(r["s"] or 0) >= 0.6 else 0.0)
    base = by.get("", [])
    pb = sum(base) / len(base) if base else None
    lens = db.get("prompt_mod_len") or {}
    mods = []
    for k, label in NAMES.items():
        xs = by.get(k, [])
        item = {"key": k, "name": label, "n": len(xs), "rate": round(sum(xs) / len(xs), 3) if xs else None,
                "chars": int(lens.get(k, 0) or 0), "verdict": "样本不够"}
        if len(xs) >= MIN_N and len(base) >= MIN_N:
            pa = sum(xs) / len(xs)
            se = math.sqrt(pa * (1 - pa) / len(xs) + pb * (1 - pb) / len(base))
            d = pa - pb
            lo, hi = d - 1.96 * se, d + 1.96 * se
            item.update(diff=round(d, 3), lo=round(lo, 3), hi=round(hi, 3),
                        verdict="有用" if hi < 0 else "帮倒忙" if lo > 0 else "看不出来")
        mods.append(item)
    return {"base": {"n": len(base), "rate": round(pb, 3) if pb is not None else None}, "mods": mods, "days": days}


def note_lengths(db, parts: dict[str, str]):
    """每段大概多长（字数，指数平均），周报里给“这段花了多少”。"""
    try:
        cur = dict(db.get("prompt_mod_len") or {})
        for k, v in parts.items():
            n = len(v or "")
            if n:
                cur[k] = round(0.9 * float(cur.get(k, n)) + 0.1 * n, 1)
        db.set("prompt_mod_len", cur)
    except Exception:  # noqa: BLE001
        pass
