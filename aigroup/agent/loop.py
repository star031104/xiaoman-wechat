"""19.0 工具调用循环：问模型 → 它要用工具就并行执行 → 把结果给它 → 再问……到它交卷或者轮数 / 时间用完（强制交卷）。

和具体业务无关：工具怎么执行由调用方传进来（execute(name, args) -> str）。
DeepSeek 的思考模式要求把每条 assistant 消息（含 reasoning_content）原样带回去，所以一次循环里思考模式不能中途切换。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field

log = logging.getLogger("aigroup.agent")


@dataclass
class LoopResult:
    text: str = ""                                   # 最后一条回复的正文（交卷的 JSON 或者“无”）
    calls: list[dict] = field(default_factory=list)  # 第一轮（判断层）的工具调用：[{name, args}]
    steps: list[str] = field(default_factory=list)   # 每一步干了什么（给日志和小屋看）
    rounds: int = 0
    forced: bool = False                             # 是不是到点被叫停、强制交卷的
    seconds: float = 0.0
    need: list[str] = field(default_factory=list)    # 21.1 研究员写的“要证实”的说法
    covered: list[bool] = field(default_factory=list)
    stop: str = ""
    tool_text: list[str] = field(default_factory=list)   # 21.1 工具原始结果（压缩之前的），给引用核对用                                   # covered（都证实了）/ model（自己交卷）/ deadline / rounds


def parse_args(raw: str) -> dict:
    try:
        d = json.loads(raw or "{}")
        return d if isinstance(d, dict) else {}
    except (json.JSONDecodeError, TypeError):
        from aigroup.chat.plan import lenient_json
        d = lenient_json(raw or "")
        return d if isinstance(d, dict) else {}


def _clean_msg(m: dict) -> dict:
    return {k: v for k, v in m.items() if not k.startswith("_") and v is not None}


def compress_old_tools(messages: list[dict], keep_rounds: int = 2) -> int:
    """20.1 深查第 3 轮起：更早的工具结果换成一行摘要（MiroThinker 只留最近几次工具结果不掉点；IterResearch 每轮只带结论）。
    只改 role=tool 的内容；assistant 消息（DeepSeek 思考模式的 reasoning_content）原样保留。返回压缩了几条。"""
    idx = [i for i, m in enumerate(messages) if m.get("role") == "assistant" and m.get("tool_calls")]
    if len(idx) <= keep_rounds:
        return 0
    cut = idx[-keep_rounds]
    n = 0
    for m in messages[:cut]:
        if m.get("role") == "tool" and not str(m.get("content") or "").startswith("（早先的结果，已压缩）"):
            m["content"] = "（早先的结果，已压缩）" + summarize_tool(str(m.get("content") or ""))
            n += 1
    return n


def summarize_tool(text: str, k: int = 4) -> str:
    """一段工具结果 → 一行：每条资料取标题和前 40 字。"""
    import re as _re
    items = _re.findall(r"<资料([^>]*)>\s*(.*?)</资料>", text, _re.S)
    if not items:
        return text[:160].replace("\n", " ")
    out = []
    for attrs, body in items[:k]:
        lines = [x.strip() for x in body.strip().splitlines() if x.strip()]
        date = _re.search(r'日期="([^"]*)"', attrs)
        out.append(f"{lines[0][:30] if lines else ''}"
                   f"（{date.group(1) if date else ''}）：{(lines[1] if len(lines) > 1 else '')[:40]}")
    return "；".join(out)


async def run_tools(calls: list[dict], execute, timeout: float = 20.0) -> list[tuple[dict, dict, str]]:
    """并行执行一轮里的所有工具调用。返回 [(调用, 参数, 结果文字)]，顺序和调用一致。"""
    async def one(tc):
        name = tc["function"]["name"]
        args = parse_args(tc["function"].get("arguments"))
        try:
            res = await asyncio.wait_for(execute(name, args), timeout)
        except asyncio.TimeoutError:
            res = f"（{name} 超时了，没拿到结果；换个办法或者用已有的资料）"
        except Exception as e:  # noqa: BLE001
            log.info("工具 %s 出错：%s", name, e)
            res = f"（{name} 出错了：{str(e)[:80]}）"
        return tc, args, str(res)
    return list(await asyncio.gather(*(one(tc) for tc in calls)))


NEED_RE = __import__("re").compile(r"要证实[:：]\s*(.+)")
PROG_RE = __import__("re").compile(r"进度[:：]\s*([^\n]+)")


def parse_need(text: str) -> list[str]:
    """「要证实：1. A 2. B」→ [A, B]。"""
    import re as _re
    m = NEED_RE.search(text or "")
    if not m:
        return []
    parts = [x.strip(" ；;，,。") for x in _re.split(r"\s*\d+[.、．)]\s*", m.group(1)) if x.strip(" ；;，,。")]
    return parts[:4]


def parse_progress(text: str) -> list[bool]:
    """「进度：1✓ 2✗ 3✓」→ [True, False, True]；没写返回 []。"""
    import re as _re
    m = PROG_RE.search(text or "")
    if not m:
        return []
    return [mk in ("✓", "√", "✔", "对") for mk in _re.findall(r"\d+\s*([✓√✔✗×xX对错])", m.group(1))]


async def tool_loop(llm, messages: list[dict], tools: list[dict], execute, *, max_rounds: int = 4,
                    thinking: bool = False, deadline_s: float = 45.0, finish_prompt: str = "",
                    role: str = "helper", max_tokens: int = 4000, compress: bool = True,
                    covered_prompt: str = "") -> LoopResult:
    """跑一次完整的工具循环。messages 会被就地追加（方便调用方存档）。"""
    t0 = time.time()
    res = LoopResult()
    from aigroup.agent.tools import SEEN
    SEEN.set([])                                     # 20.1 这次研究搜过的词：重复的不再真去搜
    covered = False
    for rnd in range(max_rounds + 1):
        if rnd >= 3 and compress:
            compress_old_tools(messages, keep_rounds=2)
        last = covered or rnd == max_rounds or (time.time() - t0) > deadline_s
        if last and rnd > 0 and covered and covered_prompt:
            messages.append({"role": "user", "content": covered_prompt})
            res.stop = "covered"
        elif last and rnd > 0 and finish_prompt:
            messages.append({"role": "user", "content": finish_prompt})
            res.forced = True
            res.stop = "rounds" if rnd == max_rounds else "deadline"
        m = await llm.chat_tools(messages, tools, role=role, thinking=thinking, max_tokens=max_tokens,
                                 tool_choice="none" if (last and rnd > 0) else None)
        res.rounds = rnd + 1
        messages.append(_clean_msg(m))
        calls = m.get("tool_calls") or []
        if last and rnd > 0 and calls and not (m.get("content") or "").strip():
            # 叫它交卷了它还想查（不该发生）：去掉这条，不带工具再要一次
            messages.pop()
            m = await llm.chat_tools(messages, None, role=role, thinking=thinking, max_tokens=max_tokens)
            res.rounds += 1
            messages.append(_clean_msg(m))
            calls = []
        if not calls or (last and rnd > 0):
            res.text = m.get("content") or ""
            res.stop = res.stop or "model"
            break
        body = str(m.get("content") or "") + "\n" + str(m.get("reasoning_content") or "")
        if not res.need:
            res.need = parse_need(body)
        prog = parse_progress(body)
        if prog:
            res.covered = prog
        if covered_prompt and rnd > 0 and prog and all(prog) and len(prog) >= max(1, len(res.need)):
            # 21.1（HALT）：要证实的都有资料了还想再查——不执行，下一轮直接交卷
            for tc in calls:
                messages.append({"role": "tool", "tool_call_id": tc["id"], "content": "（要证实的都有资料了，这次没去查）"})
            res.steps.append("都证实了，停")
            covered = True
            continue
        done = await run_tools(calls, execute)
        for tc, args, out in done:
            res.steps.append(f"{tc['function']['name']} {json.dumps(args, ensure_ascii=False)[:100]}")
            messages.append({"role": "tool", "tool_call_id": tc["id"], "content": out})
            res.tool_text.append(out)
        if rnd == 0:
            res.calls = [{"name": tc["function"]["name"], "args": args} for tc, args, _ in done]
    res.seconds = time.time() - t0
    return res
