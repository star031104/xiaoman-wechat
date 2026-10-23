"""19.0 “先想一想”的评测：用 tests/agent_cases.yaml 里的题，测判断层选得对不对；加 --research 再真去查、让模型给简报打分。

用法（要真的大模型和网络；在服务器上跑，--home 是用哪个“她的家”的 config.yaml、.env 和 Key）：
    python scripts/agent_eval.py --home home/wechat
    python scripts/agent_eval.py --home home/wechat --research --only premise-retired,data-player
上线标准（方案 19.0 第 10 节）：通过率 ≥ 85%，“不该查的”误查率 ≤ 5%，快查 P50 ≤ 12 秒。结果另存一份 JSON（--out）。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

JUDGE = """你是评测员。现在是 {now}。下面是一个群聊问题、研究员上网查完交的简报（要点、前提、来源、把握）。
注意：简报里很多是你训练之后才发生的事，你不知道不等于是编的——只有和来源对不上、自相矛盾、明显不合常理，才算“编造”。
按这几项打分（每项 0~1）：
事实正确（有没有自相矛盾、和来源对不上的）、日期正确（近况有没有拿旧消息充数）、来源质量（官方 / 数据站 / 专业媒体优先）、
该停就停（够回答就交卷，没有一大堆无关的）、回答了问题（要点是不是在答问的东西；前提不成立时，指出前提就算回答了）。
{premise}
只输出 JSON：{{"scores": {{"事实": 0.0, "日期": 0.0, "来源": 0.0, "停": 0.0, "回答": 0.0}}, "score": 0.0, "pass": true, "why": "一句话"}}
score 是五项的平均；score ≥ 0.7 且没有编造就 pass。

问题：{q}
简报：{brief}"""


def fill_when(s: str, now: datetime) -> str:
    """{today} {tomorrow} {nextweek:N}（下周星期 N）{nextwork}（下一个工作日）→ 日期。"""
    if not s:
        return ""
    out = s.replace("{today}", f"{now:%Y-%m-%d}").replace("{tomorrow}", f"{now + timedelta(days=1):%Y-%m-%d}")
    if "{nextwork}" in out:
        d = now + timedelta(days=1)
        while d.weekday() >= 5:
            d += timedelta(days=1)
        out = out.replace("{nextwork}", f"{d:%Y-%m-%d}")
    for n in range(1, 8):
        k = "{nextweek:%d}" % n                       # 下周 N（下一个自然周的星期 N）
        if k in out:
            d = now - timedelta(days=now.weekday()) + timedelta(days=7 + n - 1)
            out = out.replace(k, f"{d:%Y-%m-%d}")
    return out


def transcript(lines: list[str], now: datetime) -> str:
    out = []
    for i, ln in enumerate(lines):
        who, _, text = ln.partition("：")
        t = now - timedelta(minutes=len(lines) - i)
        who = "你" if who in ("小满", "她") else who
        out.append(f"[{t:%H:%M}] {who}：{text if who == '你' else '‹' + text + '›'}")
    return "\n".join(out)


async def route(llm, P, case: dict, now: datetime, name: str) -> dict:
    last = case["chat"][-1].partition("：")[0]
    msgs = [{"role": "system", "content": P.ROUTER_SYSTEM.format(
                now=f"{now:%Y-%m-%d %H:%M} 星期{P.WEEK[now.weekday()]}", name=name, group="测试群")},
            {"role": "user", "content": P.ROUTER_USER.format(
                transcript=transcript(case["chat"], now), recent="（没有）",
                focus=f"有人在叫她，最新一条是 {last} 说的。说这句话的人（“我”）是：{last}")}]
    t0 = time.time()
    m = await llm.chat_tools(msgs, P.ROUTER_TOOLS, role="helper", thinking=False, max_tokens=500)
    from aigroup.agent.loop import parse_args
    calls = [{"name": c["function"]["name"], "args": parse_args(c["function"].get("arguments"))}
             for c in m.get("tool_calls") or []]
    return {"calls": calls, "seconds": round(time.time() - t0, 1)}


def check_route(case: dict, r: dict, now: datetime) -> tuple[bool, str]:
    exp = case["expect"] if isinstance(case["expect"], list) else [case["expect"]]
    names = [c["name"] for c in r["calls"]]
    got = names[0] if names else "none"
    if got not in exp:
        return False, f"该 {'/'.join(exp)}，实际 {got}"
    c = r["calls"][0] if r["calls"] else {}
    if got == "research" and case.get("depth") and c["args"].get("depth") != case["depth"]:
        return True, f"力度该 {case['depth']}，实际 {c['args'].get('depth')}（不算错）"
    if got == "research" and case.get("window") and not (c["args"].get("after") or c["args"].get("before")):
        return False, "问题里有相对时间，该换成具体日期填 after / before"
    if got == "reminder_set" and case.get("when"):
        want = fill_when(case["when"], now)
        if str(c["args"].get("when") or "").strip() != want:
            return False, f"时间该 {want}，实际 {c['args'].get('when')}"
    return True, ""


async def research(llm, P, web, links, goal: str, depth: str, now: datetime, name: str, ctx: str, cfg=None) -> dict:
    from aigroup.agent.loop import tool_loop
    from aigroup.agent.mixin import parse_brief
    from aigroup.agent.tools import Toolbox
    tb = Toolbox(cfg, links, web, tz=cfg.tz)

    async def ex(n, a):
        return await tb.run(n, a)
    deep = depth == "deep"
    rounds = 4 if deep else 2
    msgs = [{"role": "system", "content": P.RESEARCH_SYSTEM.format(
                now=f"{now:%Y-%m-%d %H:%M} 星期{P.WEEK[now.weekday()]}", name=name,
                rounds=f"这次最多查 {rounds} 轮，一般 {rounds - 1} 轮就该交卷。")},
            {"role": "user", "content": P.RESEARCH_USER.format(goal=goal, context=ctx)}]
    res = await tool_loop(llm, msgs, tb.schemas(), ex, max_rounds=rounds, thinking=deep,
                          deadline_s=40 if deep else 15, finish_prompt=P.FINISH, covered_prompt=P.FINISH_COVERED)
    from aigroup.agent.mixin import verify_citations
    brief = parse_brief(res.text)
    bad = verify_citations(brief, "\n".join(res.tool_text))
    return {"brief": brief, "rounds": res.rounds, "seconds": round(res.seconds, 1), "forced": res.forced,
            "steps": res.steps, "stop": res.stop, "need": res.need, "bad_cites": bad}


def log_cases(home: str, n: int) -> list[dict]:
    """21.1 回归集：从她线上真查过的问题里取最近 n 个（不重复），直接研究、不走判断层。"""
    import sqlite3
    db = Path(home).expanduser() / "data" / "group.db"
    c = sqlite3.connect(db)
    row = c.execute("SELECT v FROM kv WHERE k='agent_log'").fetchone()
    log_ = json.loads(row[0]) if row and row[0] else []
    seen, out = set(), []
    for x in reversed(log_):
        g = str(x.get("goal") or "").strip()
        if g and g not in seen:
            seen.add(g)
            out.append({"id": f"log-{len(out) + 1}", "expect": "research", "direct": True, "goal": g,
                        "depth": x.get("depth") or "quick", "chat": [f"群友：{g}"]})
        if len(out) >= n:
            break
    return out


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--home", default="", help="她的家（home/wechat、home/qq）；不写就用 AIGROUP_HOME 或仓库目录")
    ap.add_argument("--cases", default=str(ROOT / "tests" / "agent_cases.yaml"))
    ap.add_argument("--research", action="store_true", help="判断层选了 research 的题真去查，并打分")
    ap.add_argument("--only", default="", help="只跑这几题（id，用逗号隔开）")
    ap.add_argument("--out", default="", help="结果另存成 JSON")
    ap.add_argument("--from-log", type=int, default=0, help="21.1 回归集：用她线上真查过的最近 N 个问题（直接研究、分五项打分）")
    ap.add_argument("--engine", default="", help="20.1 只用哪个搜索：deepseek / tavily（比较两个时各跑一次）")
    a = ap.parse_args()
    if a.home:
        os.environ["AIGROUP_HOME"] = str(Path(a.home).expanduser().resolve())   # 要在 import config 之前设
    import yaml

    from aigroup.agent import prompts as P
    from aigroup.core.config import load_config
    from aigroup.llm.client import LLM, parse_json
    cfg = load_config()
    llm = LLM(cfg)
    if not llm.tool_profiles():
        print("这个 config 里没有能做工具调用的模型（要 DeepSeek 官方接口，或 profile 里写 tools: true）")
        return 2
    cases = (yaml.safe_load(open(a.cases, encoding="utf-8")) or {}).get("cases") or []
    if a.from_log:
        if not a.home:
            print("--from-log 要和 --home 一起用")
            return 2
        cases, a.research = log_cases(a.home, a.from_log), True
    if a.only:
        keep = set(a.only.split(","))
        cases = [c for c in cases if c["id"] in keep]
    now = datetime.now(cfg.tz)
    name = "小满"
    web = links = None
    if a.research:
        from aigroup.net.links import LinkReader
        from aigroup.net.search import WebSearch
        if a.engine == "tavily":
            cfg["net"] = {**dict(cfg.get("net") or {}), "deepseek_search": False}
        elif a.engine == "deepseek":
            cfg["net"] = {**dict(cfg.get("net") or {}), "tavily_key": "", "search_first": "deepseek"}
        web, links = WebSearch(cfg), LinkReader(cfg)
        lk = getattr(links, "web", None)
        if lk is not None:
            links.web = web
    rows, quick_s = [], []
    for c in cases:
        try:
            if c.get("direct"):
                r, ok, why = {"calls": [{"name": "research", "args": {"goal": c["goal"], "depth": c["depth"]}}],
                              "seconds": 0}, True, ""
            else:
                r = await route(llm, P, c, now, name)
                ok, why = check_route(c, r, now)
        except Exception as e:  # noqa: BLE001
            r, ok, why = {"calls": [], "seconds": 0}, False, f"出错：{e}"
        row = {"id": c["id"], "expect": c["expect"], "calls": r["calls"], "route_s": r["seconds"], "ok": ok, "why": why}
        call = r["calls"][0] if r["calls"] else None
        if a.research and ok and call and call["name"] == "research":
            depth = "deep" if call["args"].get("depth") == "deep" else "quick"
            try:
                from aigroup.agent.mixin import time_window, window_text
                from aigroup.agent.tools import QUESTION
                QUESTION.set(c["chat"][-1].partition("：")[2][:128])
                goal = str(call["args"].get("goal") or "") + window_text(time_window(call["args"].get("after"),
                                                                                     call["args"].get("before")))
                rr = await research(llm, P, web, links, goal, depth, now, name,
                                    transcript(c["chat"], now), cfg)
                if depth == "quick":
                    quick_s.append(r["seconds"] + rr["seconds"])
                prem = ("这道题的前提本身有问题（比如人已经退役 / 东西还没出），简报（premise 或要点里）没指出来就不能通过。"
                        if c.get("premise") else "")
                if c.get("answerable") is False:
                    prem += "这道题网上基本查不到：简报 answerable=false 或者 unknown 里老实写了查不到就算对；拿不相干的资料凑答案不能通过。"
                if c.get("conflict"):
                    prem += "这道题网上说法不一：简报的 conflict 写了不同说法（或者要点里讲清了新旧说法）才能通过。"
                b = rr["brief"]
                if c.get("answerable") is False and b.get("answerable") is False:
                    row_abst = True
                else:
                    row_abst = False
                j = parse_json(await llm.chat([{"role": "user", "content": JUDGE.format(
                    now=f"{now:%Y-%m-%d}", premise=prem, q=c["chat"][-1], brief=json.dumps(rr["brief"], ensure_ascii=False))}],
                    role="helper", temperature=0.1, max_tokens=400), {}) or {}
                row.update(research=rr, judge=j, abstained=row_abst)
                if not j.get("pass", False):
                    row["ok"], row["why"] = False, f"简报没过：{j.get('why', '')}"
            except Exception as e:  # noqa: BLE001
                row["ok"], row["why"] = False, f"查资料出错：{e}"
        rows.append(row)
        mark = "✓" if row["ok"] else "✗"
        got = ",".join(f"{x['name']}{json.dumps(x['args'], ensure_ascii=False)}" for x in r["calls"]) or "无"
        print(f"{mark} {c['id']:<20} {r['seconds']:>4}s  {got[:90]}  {row['why']}")
    n = len(rows)
    passed = sum(1 for x in rows if x["ok"])
    nones = [x for x in rows if x["expect"] == "none"]
    false_search = sum(1 for x in nones if any(c["name"] == "research" for c in x["calls"]))
    print(f"\n通过 {passed}/{n}（{passed / max(1, n):.0%}，目标 ≥ 85%）；不该查的误查 {false_search}/{len(nones)}"
          f"（{false_search / max(1, len(nones)):.0%}，目标 ≤ 5%）"
          + (f"；快查 P50 {statistics.median(quick_s):.1f} 秒（目标 ≤ 12）" if quick_s else ""))
    ab = [x for x in rows if x.get("research") and next((c for c in cases if c["id"] == x["id"]), {}).get("answerable") is False]
    if ab:
        print(f"该说查不到的 {len(ab)} 题里，简报老实写了 answerable=false 的 {sum(1 for x in ab if x.get('abstained'))} 题")
    rs = [x["research"] for x in rows if x.get("research")]
    if rs:
        stops: dict = {}
        for x in rs:
            stops[x.get("stop") or "?"] = stops.get(x.get("stop") or "?", 0) + 1
        print(f"研究怎么停的：{stops}（目标：自己停 = covered + model ≥ 70%）；对不上的来源一共 {sum(x.get('bad_cites', 0) for x in rs)} 个；"
              + "五项平均：" + "，".join(f"{k} {statistics.mean([float((x.get('judge') or {}).get('scores', {}).get(k, 0) or 0) for x in rows if x.get('research')]):.2f}"
                                        for k in ("事实", "日期", "来源", "停", "回答")))
    if web is not None:
        print(f"搜索引擎：{a.engine or '默认顺序'}；DeepSeek 用了 {web.ds_used() if web.ds_key else '（没开）'}，"
              f"Tavily 这个月 {web.tavily_used()} 次")
    if a.out:
        Path(a.out).write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0 if passed / max(1, n) >= 0.85 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
