"""22.0 每天的确定性指标（不调模型、不花钱），和“个性保留”指标。

每晚算昨天一天、每个群一份，存 kv metrics_daily（最近 60 天）。看两类东西：
1. 毛病有没有少：22.0 各道检查命中了几次（qq_check_log，影子模式也算）、她说自己经历的句子里有几句没来源、
   重复问 / 叮嘱、叫错人、热聊时插话……
2. 个性有没有被磨平（检查越多，越容易把她改成一个安全但没味道的人）：口头禅、调侃、反问、自嘲、表情包这些
   “她的味道”的出现率，和她刚开始那一周（基线，kv metrics_baseline）比；掉到一半以下就在日志和小屋里提醒。
   再加两个和群友比的：句长分布的 JSD（越小越像群友的长度）、语言风格匹配 LSM（虚词用法像不像，0~1）。
"""
from __future__ import annotations

import json
import math
import re
import time
from datetime import datetime, timedelta

LEN_BINS = (5, 10, 20, 40)
FUNC = {
    "我": re.compile(r"我"), "你": re.compile(r"你"), "他": re.compile(r"[他她它]"),
    "语气": re.compile(r"[啊吧呢嘛哈呀啦哦噢诶欸]"), "否定": re.compile(r"[不没别]"), "疑问": re.compile(r"[吗？?]|什么|啥|咋|怎么"),
    "程度": re.compile(r"太|好|真|超|巨|挺|特别"),
}
TEASE = re.compile(r"笑死|菜|就这|你也有今天|离谱|绷不住|好家伙|什么鬼|拉胯|下饭|不会吧")
SELF_MOCK = re.compile(r"我(也)?(不行|菜|废|摆烂|社恐|没救)|我这(脑子|记性)|怪我")
STICKER = re.compile(r"^\[表情包")


def _hist(lens: list[int]) -> list[float]:
    b = [0] * (len(LEN_BINS) + 1)
    for n in lens:
        b[next((i for i, x in enumerate(LEN_BINS) if n <= x), len(LEN_BINS))] += 1
    s = sum(b) or 1
    return [(x + 0.5) / (s + 0.5 * len(b)) for x in b]


def jsd(a: list[int], b: list[int]) -> float | None:
    if len(a) < 5 or len(b) < 5:
        return None
    p, q = _hist(a), _hist(b)
    m = [(x + y) / 2 for x, y in zip(p, q)]

    def kl(x, y):
        return sum(xi * math.log2(xi / yi) for xi, yi in zip(x, y) if xi > 0)
    return round((kl(p, m) + kl(q, m)) / 2, 3)


def lsm(a: list[str], b: list[str]) -> float | None:
    """语言风格匹配（Ireland & Pennebaker）：每类虚词的使用率，1 - |pa - pb| / (pa + pb) 的平均。"""
    if len(a) < 5 or len(b) < 5:
        return None
    ta, tb = "".join(a), "".join(b)
    la, lb = max(1, len(ta)), max(1, len(tb))
    out = []
    for rx in FUNC.values():
        pa, pb = len(rx.findall(ta)) / la, len(rx.findall(tb)) / lb
        if pa + pb > 0:
            out.append(1 - abs(pa - pb) / (pa + pb))
    return round(sum(out) / len(out), 3) if out else None


def flavor(texts: list[str], catchphrases: list[str]) -> dict:
    """她的味道：每 100 句里有几句带口头禅 / 调侃 / 反问 / 自嘲 / 表情包。"""
    n = len(texts)
    if not n:
        return {}
    words = [str(c).strip() for c in catchphrases or [] if str(c).strip()]

    def rate(f):
        return round(100 * sum(1 for t in texts if f(t)) / n, 1)
    return {"口头禅": rate(lambda t: any(w in t for w in words)) if words else None,
            "调侃": rate(lambda t: bool(TEASE.search(t))),
            "反问": rate(lambda t: bool(re.search(r"[？?]", t))),
            "自嘲": rate(lambda t: bool(SELF_MOCK.search(t))),
            "表情包": rate(lambda t: bool(STICKER.match(t)))}


def day_metrics(gc, gid: int, day: datetime) -> dict:
    from aigroup.chat.checks import grounded, self_claims
    start = day.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    end = start + 86400
    rows = gc.db.conn.execute("SELECT role, content FROM qq_group_messages WHERE group_id=? AND ts>=? AND ts<?",
                              (gid, start, end)).fetchall()
    her = [str(r["content"] or "") for r in rows if r["role"] == "assistant"]
    users = [str(r["content"] or "") for r in rows if r["role"] == "user"]
    her_txt = [t for t in her if t and not t.startswith("[")]
    usr_txt = [t for t in users if t and not t.startswith("[")]
    srcs = gc.own_sources(gid) if hasattr(gc, "own_sources") else ""
    claims = [c for t in her_txt for c in self_claims(t)]
    hits = {}
    try:
        for r in gc.db.conn.execute("SELECT name, COUNT(*) n FROM qq_check_log WHERE group_id=? AND ts>=? AND ts<? GROUP BY name",
                                    (gid, start, end)):
            hits[r["name"]] = int(r["n"])
    except Exception:  # noqa: BLE001
        pass
    eff = gc.db.conn.execute("SELECT COUNT(*) n, AVG(score) s FROM qq_reply_effect WHERE group_id=? AND mentioned=0 AND done=1 "
                             "AND ts>=? AND ts<?", (gid, start, end)).fetchone()
    p = gc.brain.persona
    return {"day": f"{day:%Y-%m-%d}", "gid": gid, "her": len(her), "users": len(users),
            "unprompted": int(eff["n"] or 0), "picked_up": round(float(eff["s"]), 2) if eff["s"] is not None else None,
            "self_claims": len(claims), "ungrounded": sum(1 for c in claims if not grounded(c, srcs)),
            "checks": hits,
            "len_jsd": jsd([len(t) for t in her_txt], [len(t) for t in usr_txt]),
            "lsm": lsm(her_txt, usr_txt),
            "flavor": flavor(her, list(getattr(p, "catchphrases", None) or []))}


def flavor_alerts(base: dict, recent: dict, min_base: float = 4.0) -> list[str]:
    """和基线比掉到一半以下的“味道”（基线太小的不比）。"""
    out = []
    for k, v in (base or {}).items():
        if v is None or v < min_base:
            continue
        r = (recent or {}).get(k)
        if r is not None and r < v / 2:
            out.append(f"{k}从每百句 {v} 掉到 {r}")
    return out


def run_daily(gc, day: datetime | None = None) -> list[dict]:
    """每晚：算昨天的；攒够 7 天、她说了 ≥ 40 句，定一次基线（以后人设大改了可以在小屋里清掉 metrics_baseline 重定）。"""
    day = day or (gc.brain.now() - timedelta(days=1))
    out = []
    hist = list(gc.db.get("metrics_daily") or [])
    for gid in sorted(gc.groups):
        m = day_metrics(gc, gid, day)
        hist = [h for h in hist if not (h.get("day") == m["day"] and h.get("gid") == gid)] + [m]
        out.append(m)
    hist = hist[-60 * max(1, len(gc.groups)):]
    gc.db.set("metrics_daily", hist)
    base = gc.db.get("metrics_baseline") or {}
    for gid in sorted(gc.groups):
        mine = [h for h in hist if h.get("gid") == gid][-7:]
        n = sum(int(h.get("her") or 0) for h in mine)
        if n < 40:
            continue
        avg = {}
        for k in ("口头禅", "调侃", "反问", "自嘲", "表情包"):
            vals = [(h.get("flavor") or {}).get(k) for h in mine]
            vals = [v for v in vals if v is not None]
            avg[k] = round(sum(vals) / len(vals), 1) if vals else None
        if str(gid) not in base:
            base[str(gid)] = {"flavor": avg, "ts": time.time(), "days": [h["day"] for h in mine]}
            gc.db.set("metrics_baseline", base)
        else:
            alerts = flavor_alerts(base[str(gid)].get("flavor") or {}, avg)
            gc.db.set(f"flavor_alert_{gid}", {"alerts": alerts, "ts": time.time()} if alerts else None)
    return out


def summary(db, gid: int | None = None, days: int = 7) -> str:
    """给人看的一段（小屋 / 命令行）。"""
    hist = [h for h in (db.get("metrics_daily") or []) if gid is None or h.get("gid") == gid][-days:]
    if not hist:
        return "还没有每天的指标（每晚算一次）"
    lines = []
    for h in hist:
        ck = "、".join(f"{k} {v}" for k, v in (h.get("checks") or {}).items()) or "无"
        fl = h.get("flavor") or {}
        lines.append(f"{h['day']}：她 {h['her']} 句（主动 {h['unprompted']}，被接 {h.get('picked_up')}）；"
                     f"说自己经历 {h['self_claims']} 句、没来源 {h['ungrounded']}；检查命中 {ck}；"
                     f"句长 JSD {h.get('len_jsd')}、LSM {h.get('lsm')}；味道 " + json.dumps(fl, ensure_ascii=False))
    return "\n".join(lines)
