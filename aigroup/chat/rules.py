"""21.1 规则登记：所有“正则检查”命中一次记一次，看每条规则到底有没有用。

为什么：线上每出一个问题就补一条正则（质检、幕后、编线下、调侃……），能用，但越来越脆、不知道谁在起作用。
做法（和模块消融同一个思路：先能量再改）：
- 每次发之前的检查命中了哪些规则、最后是罚分 / 重写 / 拦下，按规则累计（kv: rule_hits）
- 被拦下 / 改写的原句留样（kv: rule_samples，最近 80 条），小屋“被拦下的话”给你判“拦得对 / 拦错了”
- 月报：30 天没命中的、你判过 ≥ 3 次且拦对率 < 50% 的，标“建议关掉”；关掉就是写进 rules_off（代码留着）
"""
from __future__ import annotations

import re
import time

KEY_HITS = "rule_hits"
KEY_SAMPLES = "rule_samples"


def rule_name(hit: str) -> str:
    """命中描述 → 规则名：“今天说过：又看纪录片” → “今天说过”；“碰了雷区（体重）” → “碰了雷区”。"""
    h = str(hit or "").strip()
    return re.split(r"[：:（(，,「]", h, maxsplit=1)[0].strip()[:12] or "其他"


def note(db, gid: int, hits: list[str], action: str, text: str = "", now: float | None = None):
    """action：罚分（照样发了）/ 重写 / 拦下。"""
    if not hits:
        return
    now = now or time.time()
    stats = dict(db.get(KEY_HITS) or {})
    names = list(dict.fromkeys(rule_name(h) for h in hits))
    for n in names:
        s = dict(stats.get(n) or {"n": 0, "罚分": 0, "重写": 0, "拦下": 0, "first": now})
        s["n"] = int(s.get("n") or 0) + 1
        s[action] = int(s.get(action) or 0) + 1
        s["last"] = now
        stats[n] = s
    db.set(KEY_HITS, stats)
    if action in ("重写", "拦下") and text:
        sm = list(db.get(KEY_SAMPLES) or [])[-79:]
        nid = max([int(x.get("id") or 0) for x in sm] + [int(db.get("rule_sample_seq") or 0)]) + 1
        db.set("rule_sample_seq", nid)
        sm.append({"id": nid, "ts": now, "gid": gid, "rules": names, "action": action,
                   "text": str(text)[:120], "ok": None})
        db.set(KEY_SAMPLES, sm)


def judge(db, sid: int, ok: bool) -> bool:
    sm = list(db.get(KEY_SAMPLES) or [])
    for x in sm:
        if int(x.get("id") or 0) == int(sid):
            x["ok"] = bool(ok)
            db.set(KEY_SAMPLES, sm)
            return True
    return False


def report(db, known: list[str] | None = None, days: float = 30, now: float | None = None) -> list[dict]:
    """每条规则：命中次数、罚分 / 重写 / 拦下、你判过的拦对率、建议。known：所有登记的规则名（没命中过的也列出来）。"""
    now = now or time.time()
    stats = dict(db.get(KEY_HITS) or {})
    judged: dict = {}
    for x in db.get(KEY_SAMPLES) or []:
        if x.get("ok") is None:
            continue
        for r in x.get("rules") or []:
            j = judged.setdefault(r, [0, 0])
            j[0] += 1
            j[1] += bool(x["ok"])
    out = []
    for name in dict.fromkeys([*(known or []), *stats]):
        s = stats.get(name) or {}
        n_j, ok_j = judged.get(name, [0, 0])
        recent = float(s.get("last") or 0) > now - days * 86400
        first = float(s.get("first") or now)
        if not s and (known and name in known):
            advice = "还没命中过"
        elif not recent and now - first > days * 86400:
            advice = f"{int(days)} 天没命中，建议关掉"
        elif n_j >= 3 and ok_j / n_j < 0.5:
            advice = "常拦错，建议关掉或改"
        else:
            advice = ""
        out.append({"rule": name, "n": int(s.get("n") or 0), "penalty": int(s.get("罚分") or 0),
                    "rewrite": int(s.get("重写") or 0), "block": int(s.get("拦下") or 0),
                    "judged": n_j, "right": round(ok_j / n_j, 2) if n_j else None, "advice": advice})
    return sorted(out, key=lambda r: -r["n"])


def known_rules() -> list[str]:
    from aigroup.chat.quality import RULES
    return [n for n, _, _ in RULES] + ["太像最近说的", "今天说过", "碰了雷区", "说了幕后的事", "编了线下的事", "叫了名字",
                                        "复读别人", "像AI", "他这会儿状态不好", "太长"]
