"""19.0 她回话之前先“想一想要不要办点事”：判断层（要不要查、要不要设提醒）→ 研究层（不带人设，用工具查、交一份简报）
→ 简报交给原来的说话层，用她的口吻说。

挂在 GroupChat 上（AgentMixin），用到：self.brain.llm（chat_tools）、self.web（搜索）、self.links（读网页）、
self.memory / self.knowledge（她的记忆）、grow.py 里的提醒（remind_set / remind_list / remind_cancel）。
开关：群设置 agent（默认关，微信群在 config 里打开）。失败时返回 None，调用方退回老的“搜一次”。
"""
from __future__ import annotations

import asyncio
import json
import re
import logging
import random
import time
from datetime import datetime
from urllib.parse import urlparse

from aigroup.agent import prompts as P
from aigroup.agent.loop import tool_loop

log = logging.getLogger("aigroup.agent")
tlog = logging.getLogger("aigroup.trace")


def _date_key(r: dict) -> str:
    return str(r.get("date") or "")


def _bigr(t: str) -> set:
    t = re.sub(r"\s+", "", t or "")
    return {t[i:i + 2] for i in range(len(t) - 1)}


def same_source(rows: list[dict], th: float = 0.6) -> set[int]:
    """20.1 几条结果的摘要几乎一样（互相转载同一段话）：都标“同源”，别当成几个独立来源互相印证。"""
    bs = [_bigr(str(r.get("snippet") or "")) for r in rows]
    out = set()
    for i in range(len(bs)):
        for j in range(i + 1, len(bs)):
            a, b = bs[i], bs[j]
            if len(a) >= 20 and len(b) >= 20 and len(a & b) / max(1, len(a | b)) > th:
                out |= {i, j}
    return out


def format_results(results: list[dict], engine: str) -> str:
    """搜索结果 → 给研究员看的一段：有日期的按日期从旧到新排（最新的放最后），每条包在 <资料> 里。"""
    if not results:
        return "（没搜到东西：换个说法、换中英文、或者放宽 days 再试）"
    from aigroup.net.links import site_name
    rows = sorted(results, key=lambda r: (bool(_date_key(r)), _date_key(r)))
    same = same_source(rows)
    out = []
    for i, r in enumerate(rows):
        host = (urlparse(r.get("url") or "").hostname or "").lower()
        site = r.get("site") or (site_name(host) if host else "")
        auth = r.get("authority")
        # DeepSeek 的 authority 是按整个网站打的（同站每页一样、软文站也可能高），只当“这个网站一般靠不靠谱”的参考
        cred = f' 网站权威="{["很低", "低", "一般", "高", "很高"][auth]}"' if isinstance(auth, int) and 0 <= auth <= 4 else ""
        dup = ' 同源="是"' if i in same else ""
        out.append(f'<资料 来源="{site or host or "未知"}" 日期="{_date_key(r) or "无"}"{cred}{dup}>\n'
                   f'{r.get("title", "")}\n{r.get("snippet", "")}\n链接：{r.get("url", "")}\n</资料>')
    tail = "" if engine in ("Tavily", "DeepSeek", "缓存") else (
        f"\n（这次用的是{engine}，结果没有发布日期：别把它们当成“最近”的事，需要日期就 read_url 打开看）")
    return "\n".join(out) + tail


def format_page(info: dict, url: str, offset: int) -> str:
    if not info.get("ok"):
        return f"（{url} 没打开：{info.get('error') or '读不到'}。换一个来源，或者用搜索摘要）"
    more = (f"\n（没读完：全文 {info['total']} 字，下一页用 offset={offset + len(info['text'])}）"
            if info.get("more") else "")
    return (f'<资料 来源="{info.get("site") or ""}" 日期="{info.get("date") or "无"}">\n'
            f'{info.get("title", "")}\n{info.get("text", "")}\n</资料>{more}')


def research_budget(g: dict, depth: str, qtype: str) -> tuple[int, float, str]:
    """21.0 力度：按问题类型给轮数 / 秒数 / 快深（prompts.TYPES）；没给类型就按判断层的 quick / deep 和老设置。"""
    t = P.TYPES.get(qtype or "")
    if t:
        _, _, _, _, dep, r, sec = t
        if depth == "deep" and dep == "quick" and qtype not in ("fact", "calc"):
            dep, r, sec = "deep", max(r, 3), max(sec, 30)          # 判断层觉得要深查的，就深一点
        scale = float(g.get("agent_quick_seconds", 15)) / 15 if dep == "quick" else float(g.get("agent_deep_seconds", 40)) / 40
        return r, round(sec * scale, 1), dep
    deep = depth == "deep"
    return (int(g.get("agent_deep_rounds", 4) if deep else g.get("agent_quick_rounds", 2)),
            float(g.get("agent_deep_seconds", 40) if deep else g.get("agent_quick_seconds", 15)),
            "deep" if deep else "quick")


def time_window(after, before) -> tuple[str, str] | None:
    """判断层给的 after / before（YYYY-MM-DD）→ (起, 止)；格式不对的丢掉。"""
    def ok(x):
        x = str(x or "").strip()[:10]
        return x if re.fullmatch(r"20\d\d-\d\d-\d\d", x) else ""
    a, b = ok(after), ok(before)
    return (a, b) if a or b else None


def window_text(w: tuple[str, str] | None) -> str:
    if not w:
        return ""
    a, b = w
    return f"（时间范围：{a or '不限'} 到 {b or '现在'}；搜的时候按这个填 days，旧的不能当成这段时间的事）"


def stale_check(d: dict, w: tuple[str, str] | None) -> dict:
    """20.1 简报的“信息截至”比要的时间段还早：标出来，让她说“这是 x 号的信息”。"""
    if not w or not w[0]:
        return d
    asof = str(d.get("as_of") or "")[:10]
    if re.fullmatch(r"20\d\d-\d\d-\d\d", asof) and asof < w[0]:
        extra = f"查到的信息只到 {asof}，比要问的 {w[0]} 之后还早：说的时候讲明是 {asof} 的情况，之后的不知道"
        d["unknown"] = (str(d.get("unknown") or "") + "；" + extra).strip("；")
        if d.get("confidence") == "high":
            d["confidence"] = "medium"
    return d


def _point_text(x) -> str:
    if isinstance(x, dict):
        return str(x.get("text") or "").strip()
    return str(x or "").strip()


CONF = {"高": "high", "中": "medium", "低": "low", "high": "high", "medium": "medium", "low": "low"}
MAJOR = re.compile(r"退役|转会|离队|加盟|去世|逝世|离世|塌房|被捕|官宣|解散|停更|封号|跑路|倒闭|涨价|降价|暴涨|暴跌|夺冠")


def close_json(text: str) -> str:
    """25.1 交卷的 JSON 被截断（深查带思考，max_tokens 被思考吃掉一大半；10-27 羽毛球那题整份简报只剩 400 字原文）：
    退回到最后一个完整的值，把没关的引号、括号补上。"""
    i = text.find("{")
    if i < 0:
        return ""
    s = text[i:]
    stack, in_str, esc, cut = [], False, False, 0
    for k, ch in enumerate(s):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]":
            if stack:
                stack.pop()
            if not stack:
                return s[:k + 1]
            cut = k + 1
        elif ch == ",":
            cut = k
    if not stack:
        return s
    body = s[:cut].rstrip().rstrip(",")
    # 补括号：重新数一遍到 body 结尾还开着的
    stack, in_str, esc = [], False, False
    for ch in body:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]" and stack:
            stack.pop()
    return body + "".join(reversed(stack))


def parse_brief(text: str) -> dict:
    """研究员交的 JSON → 简报。21.0 第 3 版：一句结论 + 一条条说法（各带来源和置信度）+ 冲突 / 辟谣 / 没查到 / 假设 / 可补充；
    20.1 第 2 版（answer_points + conflict + answerable）和更老的也认。返回里总有 answer_points（字符串列表）给老代码和小屋用。"""
    from aigroup.chat.plan import lenient_json
    d = lenient_json(text or "")
    keys = ("answer_points", "answer_short", "claims", "premise", "answerable", "verdict")
    if not isinstance(d, dict) or not any(k in d for k in keys):
        try:
            d2 = json.loads(close_json(re.sub(r"```(?:json)?", "", text or "")))
            if isinstance(d2, dict):
                d = d2
        except ValueError:
            m = re.search(r'"answer_short"\s*:\s*"((?:[^"\\]|\\.)*)"', text or "")
            if m:
                d = {"answer_short": json.loads(f'"{m.group(1)}"'), "confidence": "low"}
    if not isinstance(d, dict) or not any(k in d for k in keys):
        t = (text or "").strip()
        return {"answer_points": [t[:400]] if t else [], "premise": "", "as_of": "", "sources": [],
                "confidence": "low", "unknown": "" if t else "没查到", "conflict": [], "counter": [], "facts": [],
                "answerable": bool(t), "points": [], "claims": [], "answer_short": "", "followups": [], "assumptions": []}
    claims = []
    for c in (d.get("claims") or []) if isinstance(d.get("claims"), list) else []:
        if isinstance(c, dict) and str(c.get("text") or "").strip():
            srcs = [x for x in (c.get("sources") or []) if isinstance(x, dict)][:4]
            claims.append({"text": str(c["text"]).strip()[:200], "sources": srcs,
                           "confidence": CONF.get(str(c.get("confidence") or "").strip(), "medium")})
    d["claims"] = claims[:6]
    if claims:
        d["points"] = [{"text": c["text"], "conf": c["confidence"], "n_src": len(c["sources"])} for c in claims]
        d["answer_points"] = [c["text"] for c in claims]
        if not d.get("sources"):
            seen, srcs = set(), []
            for c in claims:
                for x in c["sources"]:
                    k = (x.get("site") or "", x.get("date") or "")
                    if k not in seen:
                        seen.add(k)
                        srcs.append({"site": x.get("site") or "", "date": x.get("date") or "", "title": ""})
            d["sources"] = srcs[:6]
        if not d.get("confidence"):
            order = ["low", "medium", "high"]
            d["confidence"] = order[max(order.index(c["confidence"]) for c in claims[:2])]
    else:
        pts = d.get("answer_points") or []
        pts = pts if isinstance(pts, list) else [pts]
        d["points"] = [p if isinstance(p, dict) else {"text": str(p)} for p in pts if _point_text(p)][:6]
        d["answer_points"] = [_point_text(p) for p in d["points"]]
    d["answer_short"] = str(d.get("answer_short") or "").strip()[:160]
    if d["answer_short"] and not d["answer_points"]:
        d["answer_points"] = [d["answer_short"]]
    d["confidence"] = CONF.get(str(d.get("confidence") or "").strip(), d.get("confidence") or "medium")
    conf = d.get("conflicts") if isinstance(d.get("conflicts"), list) else d.get("conflict")
    d["conflict"] = [c for c in (conf or []) if isinstance(c, dict) and c.get("sides")][:3]
    d["counter"] = [str(x).strip() for x in (d.get("counter") or []) if str(x).strip()][:3] \
        if isinstance(d.get("counter"), list) else ([str(d["counter"])] if d.get("counter") else [])
    d["counter"] = [x for x in d["counter"] if not DODGE_CALL.search(x)]     # 25.1 “没有权威复盘、不支持点名”不是辟谣
    unk = d.get("unknowns")
    if isinstance(unk, list) and unk:
        d["unknown"] = "；".join(str(x) for x in unk if str(x).strip())[:200]
    for k in ("followups", "assumptions"):
        d[k] = [str(x).strip()[:80] for x in (d.get(k) or []) if str(x).strip()][:4] if isinstance(d.get(k), list) else []
    d["facts"] = [f for f in (d.get("facts") or []) if isinstance(f, dict) and f.get("entity") and f.get("value")][:5] \
        if isinstance(d.get("facts"), list) else []
    d["verdict"] = parse_verdict(d.get("verdict"))
    if d["verdict"] and not d["answer_points"]:
        d["answer_points"] = [d["verdict"]["call"]]
    ans = d.get("answerable")
    d["answerable"] = not (ans is False or str(ans).lower() in ("false", "no", "0")) or bool(d.get("premise"))
    if not d["answer_points"] and not d.get("premise"):
        d["answerable"] = False
    return d


DODGE_CALL = re.compile(r"无法判断|不(敢|好|能)(判断|下结论|点名|说)|没有(权威|专业|现成)|查不到.{0,6}(评分|复盘)|不支持点名")


def parse_verdict(v) -> dict:
    """25.1 判断题 / 选购题的“她自己的判断”：{call, why[{point, data, src}], against, confidence, change_if, assume}；
    没有、或者 call 本身就是在躲（“无法判断”）→ {}。"""
    if not isinstance(v, dict):
        return {}
    call = str(v.get("call") or "").strip()[:120]
    if not call or DODGE_CALL.search(call):
        return {}
    why = []
    for w in v.get("why") or [] if isinstance(v.get("why"), list) else []:
        if isinstance(w, dict) and str(w.get("point") or w.get("data") or "").strip():
            why.append({"point": str(w.get("point") or "").strip()[:80], "data": str(w.get("data") or "").strip()[:100],
                        "src": str(w.get("src") or "").strip()[:40]})
        elif isinstance(w, str) and w.strip():
            why.append({"point": w.strip()[:80], "data": "", "src": ""})
    return {"call": call, "why": why[:3], "against": str(v.get("against") or "").strip()[:100],
            "confidence": CONF.get(str(v.get("confidence") or "").strip(), "medium"),
            "change_if": str(v.get("change_if") or "").strip()[:80], "assume": str(v.get("assume") or "").strip()[:80]}


NUM = re.compile(r"\d+(?:\.\d+)?")


def verify_verdict(d: dict, tool_text: str) -> int:
    """25.1 判断的依据得是真拿到的：why 里的数字在这次工具结果里都找不到的那条删掉（编的数据比不判断更糟）；
    一条有数字的依据都不剩、又没有别的依据 → 把握降到“低”。返回删了几条。"""
    v = d.get("verdict") or {}
    if not v or not tool_text:
        return 0
    keep, n = [], 0
    for w in v.get("why") or []:
        nums = [x for x in NUM.findall(w.get("data") or "") if len(x.replace(".", "")) >= 1 and x not in ("0", "1", "2")]
        if nums and not any(x in tool_text for x in nums):
            n += 1
            continue
        keep.append(w)
    v["why"] = keep
    if not keep:
        v["confidence"] = "low"
    return n


def single_source_major(d: dict) -> list[str]:
    """21.0 重大消息（退役、转会、去世……）只有一个来源的那几条说法。依据：Is Deep Research Reliable?（2026-07）：
    一篇误导文档就让深度研究 54.7% 采信错误结论 → 这种说法要降置信度、要反搜辟谣。"""
    out = []
    for c in d.get("claims") or []:
        if MAJOR.search(c.get("text") or "") and len({(x.get("site") or "") for x in c.get("sources") or []}) < 2:
            out.append(c["text"])
    return out


def verify_citations(d: dict, tool_text: str) -> int:
    """21.1 引用核对：说法后面的来源，网站名或原文摘要得在这次真搜到 / 读到的结果里出现过；对不上的删掉，
    一条说法的来源全删光了就降到“低”。返回删了几个。（深度研究评测：编造引用、计划漂移是最常见的失败）"""
    if not tool_text:
        return 0
    low = tool_text.lower()
    n = 0
    for c in d.get("claims") or []:
        keep = []
        for x in c.get("sources") or []:
            site = str(x.get("site") or "").strip().lower()
            quote = re.sub(r"\s+", "", str(x.get("quote") or ""))
            ok = (site and (site in low or site.replace("www.", "") in low)) or (len(quote) >= 6 and quote[:8] in re.sub(r"\s+", "", tool_text))
            if ok or (not site and not quote):
                keep.append(x)
            else:
                n += 1
        if c.get("sources") and not keep:
            c["confidence"] = "low"
        c["sources"] = keep
    if n and d.get("claims"):
        d["points"] = [{"text": c["text"], "conf": c["confidence"], "n_src": len(c["sources"])} for c in d["claims"]]
    return n


def brief_block(goal: str, d: dict, fresh: bool = True) -> str:
    """简报 → 放进她提示词里的一段。21.0：先给一句结论，再给一条条说法（标把握），再冲突 / 辟谣 / 没查到，最后告诉她这次怎么说。"""
    head = (f"# 你刚在手机上查了一下：{goal}" if fresh else f"# 你刚才查过的（还记得）：{goal}")
    if d.get("as_of"):
        head += f"（信息截至 {d['as_of']}）"
    lines = [head]
    if d.get("premise"):
        lines.append(f"前提：{d['premise']}")
    if d.get("answer_short"):
        lines.append(f"结论：{d['answer_short']}")
    word = {"high": "", "medium": "（大概）", "low": "（不太确定）"}
    if d.get("claims"):
        lines += [f"- {c['text']}{word.get(c['confidence'], '')}" for c in d["claims"]]
    else:
        lines += [f"- {p}" for p in d.get("answer_points") or []]
    if not d.get("answer_points") and not d.get("premise"):
        lines.append("- （什么都没查到）")
    for c in d.get("conflict") or []:
        sides = "；".join(str(x.get("say") or "") for x in c.get("sides") or [] if isinstance(x, dict))
        if sides:
            lines.append(f"说法不一样（{c.get('type') or '口径不同'}）：{sides}")
    for x in d.get("counter") or []:
        lines.append(f"辟谣 / 反证：{x}")
    if d.get("unknown"):
        lines.append(f"没查到 / 不确定：{d['unknown']}")
    if d.get("followups"):
        lines.append("被追问时可以补：" + "；".join(d["followups"]))
    conf = {"high": "比较有把握", "medium": "大概对", "low": "把握不大"}.get(str(d.get("confidence") or ""), "")
    if conf:
        lines.append(f"把握：{conf}")
    v = d.get("verdict") or {}
    if v:
        lines.append(f"你看了数据以后的判断：{v['call']}{word.get(v.get('confidence'), '')}")
        lines += [f"  依据：{w['point']}" + (f"（{w['data']}）" if w.get("data") else "") for w in v.get("why") or []]
        if v.get("against"):
            lines.append(f"  另一种看法：{v['against']}")
        if v.get("assume"):
            lines.append(f"  你是按这个挑的：{v['assume']}")
        if v.get("change_if"):
            lines.append(f"  看到这些会改判：{v['change_if']}")
    srcs = [s for s in (d.get("sources") or []) if isinstance(s, dict)][:4]
    if srcs:
        lines.append("来源：" + "；".join(f"{s.get('site') or s.get('title', '')[:20]}"
                                         f"{'（' + str(s.get('date')) + '）' if s.get('date') else ''}" for s in srcs))
    say = []
    if d.get("premise"):
        say.append(P.BRIEF_SAY["premise"])
    if v:
        say.append(P.BRIEF_SAY["verdict"])
        if v.get("confidence") == "low":
            say.append(P.BRIEF_SAY["verdict_low"])
    if d.get("answerable") is False and not d.get("premise") and not v:
        say.append(P.BRIEF_SAY["unanswerable"])
    for c in d.get("conflict") or []:
        t = str(c.get("type") or "口径不同")
        if t in P.BRIEF_SAY and P.BRIEF_SAY[t] not in say:
            say.append(P.BRIEF_SAY[t])
    if d.get("counter"):
        say.append(P.BRIEF_SAY["counter"])
    if d.get("single_major"):
        say.append(P.BRIEF_SAY["single"])
    if (str(d.get("confidence") or "") == "low" or (d.get("unknown") and d.get("answerable") is not False)) and not v:
        say.append(P.BRIEF_SAY["low"])
    if d.get("answer_short") or d.get("followups"):
        say.append(P.BRIEF_SAY["followups"])
    if say:
        lines.append("这次怎么说：" + "；".join(say) + "。")
    lines.append(P.BRIEF_HOWTO)
    return "\n".join(lines)


class AgentMixin:
    # ---------------- 开关和限额 ----------------
    def agent_on(self, gid: int) -> bool:
        llm = self.brain.llm
        return bool(self.gk(gid).get("agent", False)) and hasattr(llm, "tool_profiles") and bool(llm.tool_profiles())

    async def topic_watch(self, gid: int, force: bool = False) -> list[str]:
        """30.0 群里这几天一直在聊、还会有新进展的事（一项正在进行的比赛、在找的工作、在追的番、在等的发布……）：
        每天看一次是哪一两件，各查一次最新进展，记进账本（之后被问到不用现查，也不会把旧消息当新的说）。
        不写死领域：从她这一周查过的问题和群里三天的聊天里让模型挑。每天最多 topic_watch_max 件。"""
        key = f"topic_watch_{gid}_{self.brain.now():%Y-%m-%d}"
        if (not force and self.db.get(key)) or not self.gk(gid).get("topic_watch", True) or not self.agent_on(gid):
            return []
        self.db.set(key, True)
        week = time.time() - 7 * 86400
        goals = [str(x.get("goal") or "")[:80] for x in (self.db.get("agent_log") or [])
                 if isinstance(x, dict) and int(x.get("gid") or 0) == gid and float(x.get("ts") or 0) > week][-20:]
        rows = self.db.conn.execute("SELECT content FROM qq_group_messages WHERE group_id=? AND role='user' AND ts>? "
                                    "AND content NOT LIKE '[%' ORDER BY id DESC LIMIT 80", (gid, time.time() - 3 * 86400)).fetchall()
        chat = " / ".join(str(r["content"] or "")[:40] for r in reversed(rows))[:2400]
        if len(goals) + len(rows) < 8:
            return []
        n = int(self.gk(gid).get("topic_watch_max", 2) or 0)
        prompt = (f"这是一个朋友群。下面是群里这几天的聊天（节选）和群里的 AI 群友这一周查过的问题。挑出群里这几天一直在聊、"
                  f"接下来还会有新进展、值得每天看一眼的事（最多 {n} 件）：比如一项正在进行的比赛 / 赛季、正在追的连载、"
                  "一件还没结果的事（在等的发布、在投的岗位方向）。只是一次性聊过、问过的不算；没有就返回空列表。\n"
                  "每件写：topic（几个字）、query（今天去搜什么，具体到能搜到最新进展，比如“S16 世界赛 今天 赛果 下一轮对阵”）。\n\n"
                  "# 查过的问题\n" + "\n".join(f"- {g}" for g in goals) + f"\n\n# 聊天节选（从早到晚）\n{chat}\n\n"
                  '只输出 JSON：{"topics": [{"topic": "...", "query": "..."}]}')
        d = await self.brain.llm.json([{"role": "user", "content": prompt}], default={}, role="helper")
        picked = []
        for t in ((d.get("topics") if isinstance(d, dict) else None) or [])[:n]:
            if not isinstance(t, dict) or not str(t.get("query") or "").strip():
                continue
            q, topic = str(t["query"]).strip()[:80], str(t.get("topic") or "").strip()[:20]
            try:
                await self._agent_research(gid, q, "quick", False, question=topic or q, qtype="recent")
                picked.append(topic or q)
            except Exception as e:  # noqa: BLE001
                log.info("群 %s 跟进「%s」出错：%s", gid, topic, e)
        if picked:
            log.info("群 %s 每天跟进：%s", gid, "、".join(picked))
            self.db.set(f"topic_watch_last_{gid}", {"ts": time.time(), "topics": picked})
        return picked

    def _agent_day_key(self, gid: int) -> str:
        return f"agent_n_{gid}_{self.brain.now():%Y-%m-%d}"

    def _agent_log(self, rec: dict):
        logs = self.db.get("agent_log") or []
        logs = ([rec] + [x for x in logs if isinstance(x, dict)])[:60]
        self.db.set("agent_log", logs)

    def _briefs(self, gid: int) -> list[dict]:
        bs = self.db.get(f"agent_briefs_{gid}") or []
        return [b for b in bs if isinstance(b, dict) and time.time() - float(b.get("ts", 0)) < 6 * 3600]

    def _similar_brief(self, gid: int, goal: str, minutes: float = 30) -> dict | None:
        """24.0 半小时内查过、问题差不多（字二元组重合 ≥ 0.5，或者说的是同样几个实体）的简报。"""
        from aigroup.chat.plan import bigram_overlap
        from aigroup.chat.checks import content_terms
        g = content_terms(goal)
        for b in self._briefs(gid):
            if time.time() - float(b.get("ts") or 0) > minutes * 60 or not (b.get("brief") or {}).get("answer_points"):
                continue
            o = content_terms(str(b.get("goal") or ""))
            if bigram_overlap(goal, str(b.get("goal") or "")) >= 0.5 or (g and o and len(g & o) / max(1, min(len(g), len(o))) >= 0.6):
                return b
        return None

    def _save_brief(self, gid: int, goal: str, d: dict):
        bs = [{"goal": goal, "ts": time.time(), "brief": d}] + self._briefs(gid)
        self.db.set(f"agent_briefs_{gid}", bs[:5])

    def ledger(self):
        lg = getattr(self, "_ledger", None)
        if lg is None:
            from aigroup.memory.ledger import Ledger
            lg = self._ledger = Ledger(self.db, (self.cfg.get("net") or {}).get("ledger_ttl"))
        return lg

    def ledger_write(self, gid: int, d: dict) -> int:
        if not self.gk(gid).get("ledger", True):
            return 0
        try:
            return self.ledger().write(gid, d)
        except Exception as e:  # noqa: BLE001
            log.info("群 %s：记账本出错：%s", gid, e)
            return 0

    def ledger_block(self, gid: int, text: str) -> str:
        if not self.gk(gid).get("ledger", True):
            return ""
        try:
            return self.ledger().block(gid, text, self.cfg.tz)
        except Exception as e:  # noqa: BLE001
            log.info("群 %s：翻账本出错：%s", gid, e)
            return ""

    def _now_text(self) -> str:
        now = self.brain.now()
        return f"{now:%Y-%m-%d %H:%M} 星期{P.WEEK[now.weekday()]}"

    # 24.0 “想一想”之前先用规则筛：线上 146 次里只有 29 次真的要查 / 设提醒，其余每次白花 1~2 秒和一次调用
    NEED_RX = re.compile(r"[？?]|吗|多少|几[点号个天岁]|啥|什么|怎么|咋|哪|谁|是不是|有没有|能不能|会不会|查|搜|提醒|叫我|记得叫|闹钟|"
                         r"最新|新闻|比赛|赛程|比分|战绩|排名|夺冠|赢|输|首发|转会|退役|价格|多少钱|天气|下雨|温度|上线|发布|更新|版本|"
                         r"明天|后天|今晚|周[一二三四五六日天末]|下周|\d|BV|bv|b23|http|链接|视频|UP主|up主|"
                         r"评价|押|预测|分析|说说|讲讲|出处|哪来的|真的假的|对位|赛|局|战队|选手|英雄联盟|LPL|LCK|KPL|瓦|无畏|活动|截图", re.I)

    def agent_needed(self, gid: int, texts: list[str]) -> bool:
        """这批新消息里有没有可能要查资料 / 设提醒的信号（问句、时间、数字、比赛、价格、链接……）。没有就不调“想一想”。"""
        if not self.gk(gid).get("agent_prefilter", True):
            return True
        joined = " ".join(str(t or "") for t in texts)
        return bool(self.NEED_RX.search(joined))

    def agent_needed_ctx(self, gid: int, texts: list[str], mentioned: bool) -> bool:
        """被叫到时多看几句上下文（“@你 评价一下”本身没信号，前面在聊比赛）。"""
        if self.agent_needed(gid, texts):
            return True
        if not mentioned:
            return False
        ctx = [str(r["content"] or "") for r in self._rows(gid, 8) if r["role"] == "user"]
        return self.agent_needed(gid, ctx)

    def agent_skip_block(self, gid: int) -> str:
        """没调“想一想”时：半小时内刚查过的简报和账本里还有效的照样给她（和模型说“不用办事”时一样）。"""
        users = [r for r in self._rows(gid, 12) if r["role"] == "user"]
        recent_text = " ".join(str(r["content"] or "") for r in users[-4:])
        led = self.ledger_block(gid, recent_text)
        recent = self._briefs(gid)
        if recent and time.time() - recent[0]["ts"] < 30 * 60:
            return brief_block(recent[0]["goal"], recent[0]["brief"], fresh=False) + ("\n\n" + led if led else "")
        return led

    # ---------------- 入口：说话前先想一想 ----------------
    async def agent_think(self, gid: int, mentioned: bool, who: str = "", trigger=None) -> str | None:
        """返回放进提示词的一段（可能是空字符串：什么都不用办）；出错返回 None（调用方退回老办法）。"""
        rows = list(self._rows(gid, 12))
        users = [r for r in rows if r["role"] == "user"]
        if not users:
            return ""
        asker = users[-1]
        if trigger:
            hit = next((r for r in reversed(users) if r["qq_msg_id"] == trigger), None)
            asker = hit or asker
        recent = self._briefs(gid)
        rec_txt = "\n".join(f"- {b['goal']}（{int((time.time() - b['ts']) / 60)} 分钟前）" for b in recent[:3]) or "（没有）"
        recent_text = " ".join(str(r["content"] or "") for r in users[-4:])
        led = self.ledger_block(gid, recent_text)          # 20.2 账本里还有效的：够答就不用再查
        if led:
            rec_txt += "\n" + led
        focus = (f"{'有人在叫她，' if mentioned else '她想接一句，'}最新一条是 {asker['name']} 说的。"
                 f"说这句话的人（“我”）是：{asker['name']}")
        try:
            gname = await self.group_name(gid)
        except Exception:  # noqa: BLE001
            gname = "群"
        # 24.0 system 里不放时间（每分钟都变，整段缓存都命不中）：时间挪到最后
        msgs = [{"role": "system", "content": P.ROUTER_SYSTEM.format(name=self.brain.persona.name, group=gname)},
                {"role": "user", "content": P.ROUTER_USER.format(transcript=self.transcript(gid, 12), recent=rec_txt,
                                                                 focus=focus, now=self._now_text())}]
        t0 = time.time()
        try:
            m = await self.brain.llm.chat_tools(msgs, P.ROUTER_TOOLS, role="helper", thinking=False, max_tokens=500)
        except Exception as e:  # noqa: BLE001
            log.info("群 %s：判断要不要办事失败：%s", gid, e)
            return None
        calls = m.get("tool_calls") or []
        if not calls:
            tlog.info("群 %s 想一想：不用办事（%.1fs）", gid, time.time() - t0)
            if recent and time.time() - recent[0]["ts"] < 30 * 60:
                return brief_block(recent[0]["goal"], recent[0]["brief"], fresh=False) + ("\n\n" + led if led else "")
            return led
        from aigroup.agent.loop import parse_args
        acts, research = [], None
        for tc in calls:
            name, args = tc["function"]["name"], parse_args(tc["function"].get("arguments"))
            tlog.info("群 %s 想一想：%s %s", gid, name, json.dumps(args, ensure_ascii=False)[:120])
            if name == "research" and research is None and str(args.get("goal") or "").strip():
                research = args
                same = self._similar_brief(gid, str(args.get("goal") or ""))
                if same is not None:                      # 24.0 半小时内刚查过同一件事：直接用那份简报，不再查一遍
                    log.info("群 %s：「%s」%d 分钟前刚查过，直接用", gid, same["goal"][:30], (time.time() - same["ts"]) / 60)
                    research = None
                    self.__dict__.setdefault("_reused_brief", {})[gid] = brief_block(same["goal"], same["brief"], fresh=False)
            elif name == "reminder_set":
                acts.append(self.remind_set(gid, args, int(asker["user_id"]), asker["name"]))
            elif name == "reminder_list":
                acts.append(self.remind_list(gid))
            elif name == "reminder_cancel":
                acts.append(self.remind_cancel(gid, args))
        parts = []
        reused = (self.__dict__.get("_reused_brief") or {}).pop(gid, "")
        if reused:
            parts.append(reused)
        if acts:
            parts.append("# 你刚办的事（回话时顺口确认一下，时间说清楚；没办成的就说没办成）\n" + "\n".join(f"- {a}" for a in acts))
        if research:
            block = await self._agent_research(gid, str(research["goal"]).strip()[:120],
                                               "deep" if research.get("depth") == "deep" else "quick", mentioned,
                                               question=str(asker["content"] or "")[:128],
                                               window=time_window(research.get("after"), research.get("before")),
                                               qtype=str(research.get("type") or ""))
            if block is None:
                old = await self.web_block(gid, [r["content"] for r in users[-4:]], mentioned)
                if old:
                    parts.append(old)
            elif block:
                parts.append(block)
        return "\n\n".join(parts)

    # ---------------- 研究层 ----------------
    def _asker_note(self, gid: int, qtype: str) -> str:
        """26.0 “该不该买 / 适不适合我”：把问的人画像里能用的几项给研究员（预算、用途、在意什么），按他来挑。"""
        if qtype != "buy" or not self.gk(gid).get("agent_buy_profile", True) or not hasattr(self, "person"):
            return ""
        row = next((r for r in reversed(list(self._rows(gid, 8))) if r["role"] == "user"), None)
        if not row:
            return ""
        try:
            prof = (self.person(int(row["user_id"]), gid) or {}).get("profile") or {}
        except Exception:  # noqa: BLE001
            return ""
        keep = {k: v for k, v in prof.items() if v and not str(k).startswith("别碰")}
        if not keep:
            return ""
        txt = "；".join(f"{k}：{str(v)[:60]}" for k, v in list(keep.items())[:8])[:400]
        return (f"\n\n（问的人是 {row['name']}，你对他的了解：{txt}。“适不适合他”按这些挑；"
                "聊天里他自己说的预算、用途优先；对他的了解里没有的写进 verdict.assume。）")

    async def _agent_research(self, gid: int, goal: str, depth: str, mentioned: bool, question: str = "",
                              window: tuple[str, str] | None = None, qtype: str = "") -> str | None:
        g = self.gk(gid)
        day_key = self._agent_day_key(gid)
        if int(self.db.get(day_key, 0) or 0) >= int(g.get("agent_per_day", 60)):
            log.info("群 %s：今天查得够多了，退回只搜一次", gid)
            return None
        if not self._allow("research", int(g.get("search_per_hour", 12))):
            log.info("群 %s：这小时查得够多了，退回只搜一次", gid)
            return None
        self.db.set(day_key, int(self.db.get(day_key, 0) or 0) + 1)
        rounds, deadline, depth = research_budget(g, depth, qtype)       # 21.0 按问题类型配力度
        deep = depth == "deep"
        if mentioned:                                    # 21.1 被叫到：群里等不了太久，到点就交卷（没覆盖的写进“没查到”）
            cap = ("agent_mention_judge_cap", 40) if qtype in ("judge", "buy") else \
                ("agent_mention_deep_cap", 25) if deep else ("agent_mention_quick_cap", 12)
            deadline = min(deadline, float(g.get(cap[0], cap[1])))
        filler = None
        if mentioned and g.get("agent_filler", True) and deep:
            # 21.1 先用她自己的口气短回一句，查完再补。23.3 查得快就不发这句（10-24“我去手机查一下哈”+ 答案连着 5 条）：
            # 过 agent_filler_after 秒还没查完才发
            after = float(g.get("agent_filler_after", 10) or 0)
            if after <= 0:
                await self._agent_filler(gid, goal)
            else:
                async def _late_filler():
                    await asyncio.sleep(after)
                    await self._agent_filler(gid, goal)
                filler = asyncio.ensure_future(_late_filler())
        thinking = deep and bool(g.get("agent_deep_thinking", True))
        msgs = [{"role": "system", "content": P.RESEARCH_SYSTEM.format(
                    now=self._now_text(), name=self.brain.persona.name,
                    rounds=f"这次最多查 {rounds} 轮，一般 {max(1, rounds - 1)} 轮就该交卷。")},
                {"role": "user", "content": P.RESEARCH_USER.format(goal=goal + window_text(window) + P.type_note(qtype)
                                                                   + self._source_note(gid, goal + " " + question, qtype)
                                                                   + self._asker_note(gid, qtype),
                                                                   context=self.transcript(gid, 6))}]
        from aigroup.agent.tools import DEPTH, QUESTION
        QUESTION.set(question or goal)
        DEPTH.set(depth)
        try:
            tb = self.toolbox()
            res = await tool_loop(self.brain.llm, msgs, tb.schemas(recall=True),
                                  lambda n, a: tb.run(n, a, recall=lambda q: self._agent_recall(gid, q)),
                                  max_rounds=rounds, thinking=thinking, deadline_s=deadline, finish_prompt=P.FINISH,
                                  max_tokens=int(g.get("agent_max_tokens", 8000 if thinking else 4000)),
                                  covered_prompt=P.FINISH_COVERED if g.get("agent_stop_on_covered", True) else "")
        except Exception as e:  # noqa: BLE001
            log.info("群 %s：查资料失败：%s", gid, e)
            return None
        finally:
            if filler is not None and not filler.done():
                filler.cancel()
        d = parse_brief(res.text)
        dropped = verify_citations(d, "\n".join(res.tool_text)) if g.get("agent_verify_cites", True) else 0
        if g.get("agent_verify_cites", True):
            dropped += verify_verdict(d, "\n".join(res.tool_text))
        log.info("群 %s：查了「%s」（%s，%d 轮，%.0f 秒，%s%s）", gid, goal, depth, res.rounds, res.seconds,
                 {"covered": "都证实了就停", "model": "自己交卷", "deadline": "到点交卷", "rounds": "轮数用完"}.get(res.stop, res.stop),
                 f"，删了 {dropped} 个对不上的来源" if dropped else "")
        for s in res.steps:
            tlog.info("群 %s 查资料：%s", gid, s)
        self._agent_log({"ts": time.time(), "gid": gid, "goal": goal, "depth": depth, "thinking": thinking,
                         "rounds": res.rounds, "seconds": round(res.seconds, 1), "forced": res.forced,
                         "stop": res.stop, "need": res.need[:4], "covered": res.covered[:4], "bad_cites": dropped,
                         "steps": res.steps[:20], "brief": d})
        d = stale_check(d, window)
        if not d.get("answer_points") and not d.get("premise"):
            return brief_block(goal, {"answer_points": [], "unknown": d.get("unknown") or "没查到", "answerable": False})
        d = await self._recheck_major(d, res.steps)
        self._save_brief(gid, goal, d)
        self.ledger_write(gid, d)
        if g.get("remember_web", True) and d.get("confidence") != "low" and d.get("claims"):   # 25.1 只记查到的事实，不记她的判断
            self._spawn(self.learn(gid, goal, [{"title": goal, "snippet": "；".join(d["answer_points"][:3])}]))
        return brief_block(goal, d)

    async def _recheck_major(self, d: dict, steps: list[str]) -> dict:
        """21.0 重大消息只有一个来源：降到“中”；研究员没搜过辟谣的，补搜一次“xx 辟谣 / 澄清”，搜到就记进 counter。"""
        single = single_source_major(d)
        if not single:
            return d
        d["single_major"] = single[:2]
        for c in d.get("claims") or []:
            if c["text"] in single and c["confidence"] == "high":
                c["confidence"] = "medium"
        if d.get("confidence") == "high":
            d["confidence"] = "medium"
        if any(re.search(r"辟谣|澄清|不实|谣言|假的", s) for s in steps):
            return d
        try:
            from aigroup.agent.tools import DEPTH
            DEPTH.set("quick")
            q = re.sub(r"[，,。].*$", "", single[0])[:30] + " 辟谣 澄清"
            res, _ = await self.web.agent_search(q, 5, prefer="deepseek")
        except Exception as e:  # noqa: BLE001
            log.info("补搜辟谣出错：%s", e)
            return d
        hit = [r for r in res if re.search(r"辟谣|不实|谣言|澄清|假消息|并未|否认", (r.get("title") or "") + (r.get("snippet") or ""))]
        if hit:
            d["counter"] = (d.get("counter") or []) + [f"{hit[0].get('site') or ''}：{hit[0].get('title', '')[:40]}"]
            d["confidence"] = "low"
        return d

    async def _agent_filler(self, gid: int, goal: str = ""):
        """要查一会儿：先短回一句（每个群 10 分钟最多一次）。21.1：用她自己的口气现写一句，不是固定那几句；写不出来再用固定的。"""
        k = f"agent_filler_{gid}"
        if time.time() - float(self.db.get(k, 0) or 0) < 600:
            return
        if hasattr(self, "open_promises") and self.open_promises(gid):
            return                                   # 25.0 刚答应过还没给：别再来一句“我去看看”
        self.db.set(k, time.time())
        text = ""
        if self.gk(gid).get("agent_filler_gen", True) and goal:
            used = list(self.db.get("agent_fillers_used") or [])[-6:]
            p = self.brain.persona
            prompt = (f"你是群聊里的{p.name}（{(p.identity or '')[:40]}）。有人问你一件事，你要去手机上查一下：{goal[:60]}。\n"
                      f"最近的聊天：\n{self.transcript(gid, 4)}\n\n"
                      "先在群里回他一句很短的话（4~12 个字），用你平时的口气，表示你去查 / 去看看；不要说结论、不要问问题、不要引号。"
                      + (f"别和这些一样：{'、'.join(used)}。" if used else "") + "只输出这一句。")
            try:
                out = await asyncio.wait_for(self.brain.llm.chat([{"role": "user", "content": prompt}], role="gate",
                                                                 temperature=0.9, max_tokens=30), 6)
                out = re.sub(r"^[“\"']|[”\"']$", "", (out or "").strip().splitlines()[0] if (out or "").strip() else "")
                if 2 <= len(out) <= 20 and not re.search(r"[？?]|作为|AI|助手", out):
                    text = out
            except Exception as e:  # noqa: BLE001
                log.info("先回一句没写出来：%s", e)
            if text:
                self.db.set("agent_fillers_used", (used + [text])[-6:])
        text = text or random.choice(P.FILLERS)
        try:
            from aigroup.onebot import text_seg
            mid = await self.ob.send_group(gid, [text_seg(text)])
            self._record(gid, self.ob.self_id or 0, self.brain.persona.name, text, mid, role="assistant")
            if hasattr(self, "note_sent"):               # 25.0 先回的这句也是答应：记一笔，查完给了结果就销账
                self.note_sent(gid, [text], question=goal)
        except Exception as e:  # noqa: BLE001
            log.info("群 %s：“等我看看”没发出去：%s", gid, e)

    def _source_note(self, gid: int, text: str, qtype: str) -> str:
        """25.1 判断 / 选购 / 比较 / 近况题：按问题里的领域给研究员几行“常去的数据站”。"""
        if qtype not in ("judge", "buy", "compare", "recent") or not self.gk(gid).get("agent_source_hints", True):
            return ""
        from aigroup.agent.tools import source_hints
        try:
            extra = (self.cfg.get("net") or {}).get("source_hints")
        except Exception:  # noqa: BLE001
            extra = None
        h = source_hints(text, extra if isinstance(extra, dict) else None)
        return f"\n\n常去的数据站（参考，打不开就换下一个）：\n{h}" if h else ""

    def toolbox(self):
        """19.3 研究员的工具箱（aigroup/agent/tools.py）：搜索、读任何网页、翻记忆、B 站、电竞、天气、算数。"""
        tb = getattr(self, "_toolbox", None)
        if tb is None:
            from aigroup.agent.tools import Toolbox
            tb = self._toolbox = Toolbox(self.cfg, self.links, self.web, where=getattr(self, "where", None),
                                         tz=self.cfg.tz, video_cache=lambda bv: self.db.get(f"qq_video_{bv}"))
        return tb

    async def _agent_exec(self, gid: int, name: str, args: dict) -> str:
        return await self.toolbox().run(name, args, recall=lambda q: self._agent_recall(gid, q))

    async def _agent_recall(self, gid: int, q: str) -> str:
        out = []
        try:
            found = self.memory.recall(gid, q, k_msgs=6, k_facts=6)
            blk = self.memory.prompt_block(found, self.names(gid))
            if blk:
                out.append(blk)
        except Exception as e:  # noqa: BLE001
            log.info("翻记忆出错：%s", e)
        led = self.ledger_block(gid, q)
        if led:
            out.append(led)
        kn = getattr(self, "knowledge", None)
        if kn is not None:
            es = kn.recall(gid, q, k=4)
            if es:
                out.append("以前查到过的：\n" + "\n".join(f"- {e['topic']}：{e['summary']}"
                                                     f"（{datetime.fromtimestamp(e['ts'], self.cfg.tz):%Y-%m-%d}）"
                                                     for e in es))
        return "\n".join(out) or "（没想起来相关的）"
