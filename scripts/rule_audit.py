"""32.0 规则体检：拿线上最新的 system 提示词（qq_prompt 里最后一份）和人格文件，查“自己推自己踩 / 前后打架 / 写了两遍”。

用法：python scripts/rule_audit.py [--home home/wechat] [--json]
只读，不改任何东西。说明见 aigroup/chat/rule_audit.py。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from aigroup.chat.rule_audit import audit, report, split_sections  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--home", default="home/wechat")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    home = ROOT / a.home
    sources: dict[str, str] = {}
    db = home / "data" / "group.db"
    if db.exists():
        c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        row = c.execute("SELECT text FROM qq_prompt ORDER BY last DESC LIMIT 1").fetchone()
        if row:
            sources.update({f"提示词·{k}": v for k, v in split_sections(row[0]).items()})
    for f in sorted((home / "persona").glob("*.traits.yaml")):
        sources[f"人格文件·{f.name}"] = f.read_text(encoding="utf-8")
    if not sources:
        print("没找到提示词（qq_prompt 是空的）和人格文件")
        return 1
    r = audit(sources)
    print(json.dumps(r, ensure_ascii=False, indent=1) if a.json else report(r))
    return 0


if __name__ == "__main__":
    sys.exit(main())
