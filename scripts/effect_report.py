"""35.0 上线效果对比（只读数据库，不花钱）：每天的几项指标 + 每次发版前后 3 天平均对比。

看的东西（33.0~35.0 改的就是这些）：
- 她说了几条、占比；没人叫她自己开口几次、被接住的比例（score ≥ 0.6）；被叫到几次
- 发出去的表情包、发前检查拦了几次、查资料几次；大模型花费和缓存命中
- 每次发版（deploy/releases.log）前后各 3 天的平均：变好 / 变差一眼能看出来

用法（在 app 目录、用 app 的虚拟环境）：
    python scripts/effect_report.py --home home/wechat [--days 14] [--json]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

KEYS = (("msgs", "群友"), ("her", "她"), ("share", "她占比"), ("unprompted", "自己开口"), ("engaged", "被接住"),
        ("mentioned", "被叫到"), ("stickers", "表情包"), ("blocked", "发前拦"), ("searches", "查资料"),
        ("usd", "花费$"), ("cache", "缓存"))


def kv(c, k):
    r = c.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
    if not r:
        return None
    try:
        return json.loads(r[0])
    except (TypeError, ValueError):
        return r[0]


def day_row(c, a: float, z: float, day: str) -> dict:
    one = lambda sql, *args: (c.execute(sql, args).fetchone() or [0])[0] or 0      # noqa: E731
    u = one("SELECT COUNT(*) FROM qq_group_messages WHERE role='user' AND ts>=? AND ts<?", a, z)
    her = one("SELECT COUNT(*) FROM qq_group_messages WHERE role='assistant' AND ts>=? AND ts<?", a, z)
    eff = c.execute("SELECT SUM(mentioned=0) n, SUM(mentioned=0 AND done=1) d, SUM(mentioned=0 AND done=1 AND score>=0.6) e, "
                    "SUM(mentioned=1) m FROM qq_reply_effect WHERE ts>=? AND ts<?", (a, z)).fetchone()
    st = kv(c, f"llm_stats_{day}") or {}
    hit, miss = int(st.get("tok_hit", 0) or 0), int(st.get("tok_miss", 0) or 0)
    tabs = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    return {
        "day": day[5:], "msgs": u, "her": her, "share": round(her / (u + her), 3) if u + her else None,
        "unprompted": eff["n"] or 0, "engaged": round((eff["e"] or 0) / eff["d"], 3) if eff["d"] else None,
        "mentioned": eff["m"] or 0,
        "stickers": one("SELECT COUNT(*) FROM qq_actions WHERE kind='sticker' AND ok=1 AND ts>=? AND ts<?", a, z)
        if "qq_actions" in tabs else 0,
        "blocked": one("SELECT COUNT(*) FROM qq_check_log WHERE ts>=? AND ts<?", a, z) if "qq_check_log" in tabs else 0,
        "searches": int(st.get("ds_search", 0) or 0) + int(st.get("tavily", 0) or 0),
        "usd": round(float(st.get("usd", 0) or 0), 3), "cache": round(hit / (hit + miss), 3) if hit + miss else None,
    }


def releases(app: Path) -> list[tuple[datetime, str]]:
    out = []
    f = app / "deploy" / "releases.log"
    for ln in (f.read_text(encoding="utf-8").splitlines() if f.exists() else []):
        p = ln.split()
        if len(p) >= 2:
            try:
                out.append((datetime.fromisoformat(p[0]), p[1]))
            except ValueError:
                pass
    return out


def avg(rows: list[dict], k: str):
    v = [r[k] for r in rows if r.get(k) is not None]
    return round(sum(v) / len(v), 3) if v else None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--home", default="home/wechat")
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--releases", type=int, default=4, help="只比最近几次发版（默认 4）")
    a = ap.parse_args(argv)
    home = Path(a.home)
    dbp = next((p for p in (home / "data" / "group.db", home / "data" / "ailife_group.db") if p.exists()), None)
    if not dbp:
        print("没找到数据库", file=sys.stderr)
        return 1
    c = sqlite3.connect(f"file:{dbp}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    today = datetime.now().replace(hour=5, minute=0, second=0, microsecond=0)      # 一天从早上 5 点算
    if datetime.now() < today:
        today -= timedelta(days=1)
    rows = []
    for i in range(a.days - 1, -1, -1):
        d0 = today - timedelta(days=i)
        rows.append(day_row(c, d0.timestamp(), (d0 + timedelta(days=1)).timestamp(), f"{d0:%Y-%m-%d}"))
    app = next((x for x in (home.resolve().parents[1], Path(__file__).resolve().parents[1])
                if (x / "deploy" / "releases.log").exists()), Path(__file__).resolve().parents[1])
    rel = [(t, v) for t, v in releases(app) if t >= today - timedelta(days=a.days)]
    cmp = []
    for t, v in rel:
        before = [r for r in rows if 0 < (t - datetime.strptime(f"{t.year}-{r['day']}", "%Y-%m-%d")).days <= 3]
        after = [r for r in rows if 0 <= (datetime.strptime(f"{t.year}-{r['day']}", "%Y-%m-%d") - t.replace(hour=0)).days < 3]
        cmp.append({"version": v, "at": f"{t:%m-%d %H:%M}", "before": {k: avg(before, k) for k, _ in KEYS},
                    "after": {k: avg(after, k) for k, _ in KEYS}, "days_after": len(after)})
    cmp = cmp[-a.releases:] if a.releases > 0 else cmp
    if a.json:
        print(json.dumps({"days": rows, "releases": cmp}, ensure_ascii=False, indent=1))
        return 0
    print("| 日期 | " + " | ".join(n for _, n in KEYS) + " |")
    print("|" + "---|" * (len(KEYS) + 1))
    for r in rows:
        print(f"| {r['day']} | " + " | ".join("—" if r[k] is None else str(r[k]) for k, _ in KEYS) + " |")
    for x in cmp:
        print(f"\n## {x['version']}（{x['at']}）发版前 3 天 → 后 {x['days_after']} 天（平均）")
        for k, n in KEYS:
            b, z = x["before"][k], x["after"][k]
            if b is None and z is None:
                continue
            print(f"- {n}：{'—' if b is None else b} → {'—' if z is None else z}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
