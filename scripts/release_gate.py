"""23.3 上线前的闸门：在服务器的 app 目录里、用 ai-life 的虚拟环境跑。

    python scripts/release_gate.py                 测试 + 问题回归集 + 隐私扫描 + 发版间隔
    python scripts/release_gate.py --llm           再拿线上的大模型跑一遍统一自检（check_critic），看回归集里认出几条
    python scripts/release_gate.py --record 23.3   全过以后记一笔：deploy/releases.log（日期、版本、提交）
    python scripts/release_gate.py --hotfix        这次是急修（掉线、发不出去这类）：不管发版间隔

为什么要有：10-23 下午到 10-24 下午 21 小时上了 8 个版本，群里一天才几十条消息，每个版本分到的样本都不到 20 条，
日报只能写“样本太小”。现在的规矩：
- 平时一周发一次（RELEASE_DAYS，默认 7 天），中间的改动攒着；急修（掉线、发不出去、说了不该说的）用 --hotfix
- 每发一次，日报里那一段至少要跑满 2 天再评价
- 日报确认过的问题先进 tests/incidents.yaml，再改代码
"""
from __future__ import annotations

import argparse
import asyncio
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "deploy" / "releases.log"
RELEASE_DAYS = 7


def step(name: str, cmd: list[str]) -> bool:
    t = time.time()
    r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    ok = r.returncode == 0
    tail = (r.stdout + r.stderr).strip().splitlines()[-1:] or [""]
    print(f"{'✓' if ok else '✗'} {name}（{time.time() - t:.0f} 秒）{'' if ok else '：' + tail[0][:200]}")
    if not ok:
        print((r.stdout + r.stderr)[-3000:])
    return ok


def last_release() -> tuple[float, str]:
    if not LOG.is_file():
        return 0.0, ""
    lines = [ln for ln in LOG.read_text(encoding="utf-8").splitlines() if ln.strip() and not ln.startswith("#")]
    if not lines:
        return 0.0, ""
    d, _, rest = lines[-1].partition(" ")
    try:
        return datetime.strptime(d, "%Y-%m-%dT%H:%M").timestamp(), rest
    except ValueError:
        return 0.0, rest


async def critic_recall() -> None:
    """拿真的大模型跑统一自检：回归集里每一条“该认出”的，自检认出没有；“不该命中”的有没有误伤。"""
    import yaml
    sys.path.insert(0, str(ROOT))
    from aigroup.chat.checks import critic_prompt, critic_verdict
    from aigroup.core.config import load_config
    from aigroup.llm.client import LLM, parse_json
    cases = yaml.safe_load((ROOT / "tests" / "incidents.yaml").read_text(encoding="utf-8"))
    cfg = load_config(ROOT / "config.yaml")
    llm = LLM(cfg)
    hit = miss = fp = 0
    by_rule = 0
    gaps = []                               # 31.1 规则和自检都认不出的（真正漏掉的）
    for c in cases:
        ctx = "\n".join(f"{w}：{t}" for w, t, _ in c.get("msgs") or [])
        mine = "\n".join(f"- {t}" for w, t, _ in c.get("msgs") or [] if w == "她")        # 27.1 线上自检也看她今天说过的
        prompt = critic_prompt(ctx, c["draft"], str(c.get("web") or ""), mine=mine)          # 26.0 和线上同一份提示词
        try:
            out = await llm.chat([{"role": "user", "content": prompt}], role="gate", temperature=0.0, max_tokens=200)
        except Exception as e:  # noqa: BLE001
            print(f"  ? {c['id']} 没问成：{e}")
            continue
        bad = bool(critic_verdict(parse_json(out, {}) or {}))
        rule = c.get("expect") not in ("critic", "none")      # 有规则检查管（test_incidents 已经保证规则认得出）
        if c.get("expect") == "none":
            fp += bad
            print(f"  {'✗ 误伤' if bad else '✓'} {c['id']}")
        else:
            hit += bad
            miss += not bad
            by_rule += rule and not bad
            if not bad and not rule:
                gaps.append(c["id"])
            tag = "✓" if bad else (f"· 自检没认出，规则 {c['expect']} 认出" if rule else "✗ 都没认出")
            print(f"  {tag} {c['id']}（{c.get('what', '')}）")
    print(f"统一自检：认出 {hit}/{hit + miss}，误伤 {fp}")
    print(f"合起来（规则 + 自检）：认出 {hit + by_rule}/{hit + miss}，都没认出 {len(gaps)}" + (f"：{'、'.join(gaps)}" if gaps else ""))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--llm", action="store_true")
    ap.add_argument("--record", default="")
    ap.add_argument("--hotfix", action="store_true")
    a = ap.parse_args()
    py = sys.executable
    ok = all([
        step("ruff", [py, "-m", "ruff", "check", "."]) if subprocess.run([py, "-m", "ruff", "--version"], capture_output=True).returncode == 0
        else (print("· 没装 ruff，跳过") or True),
        step("隐私扫描", [py, "scripts/privacy_scan.py"]),
        step("问题回归集", [py, "-m", "pytest", "-q", "tests/test_incidents.py"]),
        step("测试", [py, "-m", "pytest", "-q", "tests"]),
    ])
    ts, what = last_release()
    days = (time.time() - ts) / 86400 if ts else 99
    if days < RELEASE_DAYS and not a.hotfix:
        print(f"✗ 发版间隔：上次是 {days:.1f} 天前（{what}），平时一周一次；急修加 --hotfix")
        ok = False
    else:
        print(f"✓ 发版间隔：上次 {days:.1f} 天前{'（急修）' if a.hotfix else ''}")
    if a.llm:
        asyncio.run(critic_recall())
    if ok and a.record:
        head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%Y-%m-%dT%H:%M} {a.record} {head}{' hotfix' if a.hotfix else ''}\n")
        print(f"记下了：{a.record} {head}")
    print("可以上线" if ok else "先别上线")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
