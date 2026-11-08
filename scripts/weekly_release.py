"""36.0 A3 一周一发，你只点头：

  python scripts/weekly_release.py            准备：把日报里攒的用例（tests/incidents_candidates.yaml）并进回归集、
                                              跑 release_gate --llm（不记版本），写一页摘要到 deploy/weekly/<日期>.md
  python scripts/weekly_release.py --ship 36.1 你看过摘要说“发”以后：再跑一次 release_gate --record，合进 wechat 分支、重启服务

规矩（写进日报任务和发版流程）：
- 日报把每个 P 的 yaml 用例写进电脑上 ai-life/_daily_report/candidates/<日期>.yaml；周版时拷到服务器 tests/incidents_candidates.yaml（字段同 incidents.yaml）
- 只加用例、只收紧门控（让她少说）的改动：release_gate 全过就可以当急修发，不用等周版
- 加功能、放宽门控（让她多说）的：等周版，摘要里单列
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CAND = ROOT / "tests" / "incidents_candidates.yaml"
INC = ROOT / "tests" / "incidents.yaml"
OUT = ROOT / "deploy" / "weekly"


def sh(cmd: list[str]) -> tuple[int, str]:
    r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    return r.returncode, (r.stdout + r.stderr)


def merge_candidates() -> list[str]:
    import yaml
    if not CAND.is_file() or not CAND.read_text(encoding="utf-8").strip():
        return []
    have = {c.get("id") for c in (yaml.safe_load(INC.read_text(encoding="utf-8")) or [])}
    new = [c for c in (yaml.safe_load(CAND.read_text(encoding="utf-8")) or []) if c.get("id") and c["id"] not in have]
    if new:
        with INC.open("a", encoding="utf-8") as f:
            f.write(f"\n# ---- 日报攒的用例（{time.strftime('%m-%d')} 并入）----\n")
            f.write(yaml.safe_dump(new, allow_unicode=True, sort_keys=False))
        CAND.write_text("", encoding="utf-8")
    return [c["id"] for c in new]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ship", default="")
    a = ap.parse_args()
    py = sys.executable
    if a.ship:
        code, out = sh([py, "-u", "scripts/release_gate.py", "--llm", "--hotfix", "--record", a.ship])
        print(out[-3000:])
        if code:
            sys.exit("release_gate 没过，不发")
        print("过了。接下来：git 合进 wechat、sudo -n systemctl restart ai-life-wechat（见 docs）")
        return
    ids = merge_candidates()
    code, gate = sh([py, "-u", "scripts/release_gate.py", "--llm", "--hotfix"])
    _, log = sh(["git", "log", "--oneline", "-20"])
    _, diff = sh(["git", "diff", "--stat", "HEAD~1"])
    OUT.mkdir(parents=True, exist_ok=True)
    md = OUT / f"{time.strftime('%Y-%m-%d')}.md"
    md.write_text(f"# 周版候选 {time.strftime('%Y-%m-%d %H:%M')}\n\n"
                  f"- 新并入回归集的用例：{', '.join(ids) or '无'}\n"
                  f"- release_gate：{'全过' if code == 0 else '没过'}\n\n## release_gate 输出（末尾）\n```\n{gate[-2500:]}\n```\n\n"
                  f"## 最近提交\n```\n{log}\n```\n\n## 改动\n```\n{diff[-1500:]}\n```\n\n回“发”就跑：python scripts/weekly_release.py --ship <版本>\n",
                  encoding="utf-8")
    print(md.read_text(encoding="utf-8")[:3000])


if __name__ == "__main__":
    main()
