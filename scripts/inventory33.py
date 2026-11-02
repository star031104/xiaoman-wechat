"""33.0 盘点：提示词每一段多少字、每道检查是底线还是风格、现在是 on / shadow / off，以及最近 7 天的高光指标。

用法：python scripts/inventory33.py [--home home/wechat] [--days 7] [--json]
只读，不改任何东西。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from aigroup.chat.checks import CHECKS, FAILURE_MODES, STYLE_IDX  # noqa: E402
from aigroup.chat.rule_audit import split_sections  # noqa: E402
from aigroup.chat.settings import DEFAULTS  # noqa: E402

# 规则检查归到哪一层（见方案文档“现有规则逐条归类”）
LAYER = {"ask_repeat": "时机", "addressee": "底线", "answered": "时机", "catchphrase": "出口（防重复）", "video_pending": "底线",
         "seen_missing": "底线", "promise": "底线", "dodge": "底线（要下判断）", "copy": "出口（防照抄）", "stale_ask": "时机",
         "misattrib": "底线", "life_done": "底线", "bet_unknown": "底线", "link_unopened": "底线", "self_echo": "时机",
         "old_backlog": "时机", "critic": "底线（风格类 9/11/14/16/17 只记录）"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--home", default="home/wechat")
    ap.add_argument("--days", type=float, default=7)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    db = ROOT / a.home / "data" / "group.db"
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    out: dict = {"prompt": {}, "checks": [], "failure_modes": [], "hits": {}}
    row = c.execute("SELECT text FROM qq_prompt ORDER BY last DESC LIMIT 1").fetchone()
    if row:
        out["prompt"] = {k: len(v) for k, v in split_sections(row[0]).items()}
        out["prompt_total"] = len(row[0])
    over = {}
    try:
        over = json.loads((c.execute("SELECT v FROM kv WHERE k='group_settings'").fetchone() or ["{}"])[0])
    except Exception:  # noqa: BLE001
        pass
    for name in CHECKS:
        out["checks"].append({"name": name, "layer": LAYER.get(name, "?"), "default": DEFAULTS.get(f"check_{name}", "off")})
    for i, m in enumerate(FAILURE_MODES, 1):
        out["failure_modes"].append({"n": i, "layer": "风格（影子）" if i in STYLE_IDX else "底线", "text": m.split("（")[0]})
    since = time.time() - a.days * 86400
    for r in c.execute("SELECT name, mode, COUNT(*) n FROM qq_check_log WHERE ts>? GROUP BY name, mode", (since,)):
        out["hits"][f"{r['name']}·{r['mode']}"] = r["n"]
    out["settings_override"] = over
    if a.json:
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return 0
    print(f"# 提示词（最近一份 system，共 {out.get('prompt_total', 0)} 字）")
    for k, v in sorted(out["prompt"].items(), key=lambda x: -x[1]):
        print(f"  {v:6d}  {k}")
    print("\n# 规则检查")
    for x in out["checks"]:
        print(f"  {x['name']:14s} {x['layer']:24s} 默认 {x['default']}")
    print("\n# 统一自检清单")
    for x in out["failure_modes"]:
        print(f"  {x['n']:2d}. [{x['layer']}] {x['text']}")
    print(f"\n# 最近 {a.days:g} 天命中（名字·模式：次数）")
    for k, v in sorted(out["hits"].items(), key=lambda x: -x[1]):
        print(f"  {v:5d}  {k}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
