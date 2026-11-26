"""38.3 多写几句、挑最好的一句（取代“拦下来改写一句”）。

以前：写 2 句 → 挑一句 → 发前检查 → 有毛病就改写（被叫到）或者不说。改写出来的常常比原句还差（体检 11-09：拦错的 73%），
而且一条毛病一条规则，规则越加越多、互相打架。

现在：
1. 写：一次写 3 句，风格不一样（接内容 / 接梗或情绪 / 最短的）。
2. 每句过一遍规则：
   - 底线（直接不能用）：隐私和安全（hard_issues）、quality 里的硬规则（自称 AI、链接、思考外泄……）、
     编具体事实（price_src：价格、比分、日期没出处；看不清的图认牌子；引了没对上的 OCR 字）、把别人的话安错人（misattrib）。
     安静期、管理员叫停在 speak 的 preflight 里，不在这里。
   - 其余规则全部只扣分：quality 的软规则照原分（AI 腔、八股这些 1~5 分）；发前检查每命中一条扣 6 分（= 不说的分），
     “这句根本不该说”（〔不说〕那几条）扣 8 分；
     影子（shadow）的只记录不扣分。
3. 一次便宜模型的“挑选”：拿出过的毛病清单（FAILURE_MODES）和 4 条通用原则，给每句打 1~5 分、标毛病，再说哪句最好、或者不说更好。
   4 条原则只写在这里（不再一条毛病一条规则）：接话题不评人；像群友不像助手；长短跟群友差不多；场面读对。
4. 总分 = 规则扣分 + 模型的扣分 + 太长的扣分，挑最低的。
   - 被叫到 / 小屋手动：照发总分最低的；全撞底线就重新写一批（只一次），还不行就回一句最短的。
   - 没被叫到：最低的也 ≥ 6 分、或者挑选说“不说更好”，就不说。
每次的几句、各自的扣分、选了哪句、为什么，都记进 trace（quality 一栏），18 点复核时抽查挑得对不对。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re

from aigroup.chat.checks import CRITIC_GUIDE, DROP, FAILURE_MODES, JOKE_OK, STYLE_IDX, critic_index
from aigroup.chat.quality import HARD, check_bubbles
from aigroup.chat.rulebook import hard_checks

log = logging.getLogger("aigroup.group")
tlog = logging.getLogger("aigroup.text.group")

SKIP_AT = 6.0                    # 没被叫到时，最好的一句也扣到这么多就不说（和以前的 SOFT_BLOCK 一样）
HARD_CHECKS = hard_checks()                     # 底线（rulebook 里层是“底线”的）：编具体事实、把别人的话安错人
HARD_QUALITY = ("说了幕后的事", "碰了雷区")       # quality 里不到 HARD 分、但算底线的（rulebook.QUALITY_EXTRA 层是“底线”）
RULE_PEN = 6.0                   # 发前规则命中一条 = 和不说一样差（规则都来自确认过的事故）：没被叫到就不会发
DROP_PEN = 8.0                   # “这句根本不该说”（〔不说〕那几条）

PRINCIPLES = [
    "接话题本身：接他说的那件事（问一个点、补一个点、吐槽这件事），不评价说话的人（“讲得挺顺”“你今天是主讲”“难得没跑题”）",
    "像群友，不像助手：问意见就直接给一个答案；不主动揽活（“要不要我帮你列 / 整理”）、不交报告（“先纠个前提”、一大段资料）、不说教；得意、嘴硬一句可以，连着来就烦",
    "长短跟群友差不多：群友一条一般 {L} 个字；被认真问到可以长一点，但一条只说一件事",
    "场面读对：谁说的、在问谁、在聊什么、刚发生了什么，都对得上",
]


def principles_text(group_len: int = 10) -> str:
    return "\n".join(f"{i + 1}. {p.format(L=group_len or 10)}" for i, p in enumerate(PRINCIPLES))


def judge_prompt(ctx: str, cands: list[str], *, brief: str = "", read: str = "", mine: str = "", sore: str = "",
                 mentioned: bool = False, group_len: int = 10) -> str:
    """挑选：每句标毛病（清单）+ 按 4 条原则打 1~5 分 + 选一句（或者不说）。"""
    lines = "\n".join(f"{k + 1}. {c}" for k, c in enumerate(cands))
    return (
        "一个人在朋友群里聊天，下面是她想发的几种说法。帮她挑一句最好的。\n\n"
        "# 先看每句有没有犯这些毛病（只看这些；没犯就是没犯，别挑剔说法，别嫌不够稳妥）\n"
        + "\n".join(f"{i + 1}. {m}" for i, m in enumerate(FAILURE_MODES))
        + "\n\n" + CRITIC_GUIDE + "\n"
        "（上面是给一句话找毛病的尺度；下面每句都照这个尺度单独看一遍。）\n\n"
        "# 再按这 4 条给每句打 1~5 分（5 = 像个会聊天的群友，1 = 一看就不对）\n"
        + principles_text(group_len) + "\n\n"
        + f"# 最近的群聊（最后几条最新）\n{ctx}\n\n"
        + (f"# 最近几句群友话的意思（读过的）\n{read}\n\n" if read else "")
        + (f"{sore}\n（第 24 条对着这里看）\n\n" if sore else "")
        + (f"# 她今天早些时候说过的（看第 6、9、14 条：同一个意思、同一个梗、同一个句架子）\n{mine}\n\n" if mine else "")
        + (f"# 她这次刚查到的资料（第 5 条只看这里和群聊里有没有；这里写着的数字、比分、判断照说不算毛病）\n{brief}\n\n"
           if brief else "# 她这次没查资料\n\n")
        + ("# 有人在叫她，这次一定要回一句\n\n" if mentioned else
           "# 没人叫她：都不好、或者这时候插话不如不说，best 就写 0\n\n")
        + f"# 她想发的几种说法\n{lines}\n\n"
        "只输出 JSON：{\"cands\": [{\"k\": 序号, \"kind\": \"事实/看法/玩笑/关心\", \"bad\": 毛病序号（没有写 0）, "
        "\"why\": \"一句话\", \"score\": 1到5}], \"best\": 最好那句的序号（没人叫她、不说更好写 0）}"
    )


def parse_judge(raw: str, n: int) -> dict:
    """挑选的结果 → {"cands": {k: {"bad", "why", "score", "kind"}}, "best": k 或 0 或 None}。认不出就返回空。"""
    try:
        d = json.loads(re.search(r"\{.*\}", raw or "", re.S).group(0))
    except Exception:  # noqa: BLE001
        return {}
    out: dict = {"cands": {}, "best": None}
    for c in d.get("cands") or [] if isinstance(d.get("cands"), list) else []:
        if not isinstance(c, dict):
            continue
        try:
            k = int(c.get("k"))
        except (TypeError, ValueError):
            continue
        if not 1 <= k <= n:
            continue
        try:
            sc = max(1.0, min(5.0, float(c.get("score"))))
        except (TypeError, ValueError):
            sc = 3.0
        bad = critic_index(c.get("bad")) if c.get("bad") not in (None, False, 0, "0", "") else 0
        kind = str(c.get("kind") or "")
        if bad and "玩笑" in kind and bad not in JOKE_OK:      # 玩笑只认那几条
            bad = 0
        if bad and "看法" in kind and bad in (3, 8):
            bad = 0
        if bad == 13:                    # 接错人交给规则（misattrib）：模型分不清谁说的哪句
            bad = 0
        out["cands"][k] = {"bad": bad, "why": str(c.get("why") or "")[:80], "score": sc, "kind": kind[:4]}
    try:
        b = int(d.get("best"))
        out["best"] = b if 0 <= b <= n else None
    except (TypeError, ValueError):
        out["best"] = None
    return out


def judge_penalty(j: dict | None) -> tuple[float, list[str]]:
    """挑选给这一句的扣分：分数每差 1 分扣 0.8；犯了清单里的毛病扣 4（风格类的扣 1.5）。"""
    if not j:
        return 0.0, []
    pen = round((5.0 - float(j.get("score", 3.0))) * 0.8, 2)
    hits = []
    bad = int(j.get("bad") or 0)
    if bad:
        style = bad in STYLE_IDX
        pen += 1.5 if style else 4.0
        mode = FAILURE_MODES[bad - 1].split("（")[0] if 0 < bad <= len(FAILURE_MODES) else "毛病"
        hits.append(f"挑选：{mode}——{j.get('why', '')[:40]}")
    if float(j.get("score", 3.0)) <= 2:
        hits.append(f"挑选打 {j['score']:.0f} 分：{j.get('why', '')[:40]}")
    return pen, hits


def length_penalty(n_chars: int, group_len: int, mentioned: bool, ratio: float = 3.0) -> float:
    """比群友长太多扣分：没被叫到超过 ratio/2 倍（默认 1.5）开始扣，被叫到超过 ratio 倍（默认 3）开始扣；最多扣 3。
    ratio 是小屋里的 length_max_ratio。"""
    if not group_len or n_chars <= 0:
        return 0.0
    r = n_chars / group_len
    start = ratio if mentioned else ratio / 2
    return round(min(3.0, max(0.0, (r - start) * 1.2)), 2)


WENYAN_PROMPT = """下面是{name}想在朋友群里说的一句话，先用文言写下了意思。把它翻成她会随手打出来的大白话。

风格：{style}
{target}文言：{wy}

要求：
- 只翻意思，不加文言里没有的事；人名、数字、比分、专有名词照原样
- 像真人打字：口语、可以省主语、不用书面语和成语堆砌
- 不要“我认”“我记下了”“稳”“你掂量”“我不拦”这种江湖腔，不要“要不要我帮你”
- 一两句；想分两条发就用 ||| 隔开
只输出翻好的话。"""


def wenyan_prompt(name: str, wy: str, style: str, target: str = "") -> str:
    """38.4 文言 → 白话（南竹《用文言文打败鱼八股》）：文言里没有那股腔，只带几个字的风格提示翻回来，
    比“白话润色白话”更不容易绕回原来的腔调。"""
    tgt = f"她要接的是这条：{target[:80]}\n" if target else ""
    return WENYAN_PROMPT.format(name=name, style=style or "朋友群里随手打字，短", target=tgt, wy=wy[:60])


class ChooseMixin:
    async def wenyan_cand(self, gid: int, wy: str, target: str = "") -> str:
        """文言那句翻回白话（一次便宜模型调用，6 秒超时）；不成返回空串，这一轮就少一句候选，别的照常。"""
        wy = (wy or "").strip()
        if not wy or not getattr(self, "brain", None):
            return ""
        prompt = wenyan_prompt(self.brain.persona.name, wy, str(self.gk(gid).get("wenyan_style") or ""), target)
        try:
            out = await asyncio.wait_for(self.brain.llm.chat([{"role": "user", "content": prompt}], role="gate",
                                                             temperature=0.8, max_tokens=80), 6)
        except Exception as e:  # noqa: BLE001
            log.info("群 %s：文言转白话没成：%s", gid, e)
            return ""
        out = re.sub(r"^(白话|翻译|译文)[:：]\s*", "", str(out or "").strip().strip("“”\"'"))
        return out[:120]

    async def judge_cands(self, gid: int, cands: list[list[str]], *, mentioned: bool, web: str = "",
                          target_uid=None) -> dict:
        """一次便宜模型调用，给几句一起打分。每小时有上限（和以前的自检共用 critic_per_hour）；失败返回空（只靠规则挑）。"""
        if (self.check_mode(gid, "critic") == "off" or not getattr(self, "brain", None) or not hasattr(self, "_critic_ctx")
                or len(cands) < 1):
            return {}
        if not self._allow(f"critic_{gid}", int(self.gk(gid).get("critic_per_hour", 30) or 0)):
            return {}
        texts = [" / ".join(c) for c in cands]
        ctx, brief, read, mine, sore = self._critic_ctx(gid, " ".join(texts), web, target_uid)
        L = self.group_len(gid) if hasattr(self, "group_len") else 10
        prompt = judge_prompt(ctx, texts, brief=brief, read=read, mine=mine, sore=sore, mentioned=mentioned, group_len=L)
        try:
            out = await asyncio.wait_for(self.brain.llm.chat([{"role": "user", "content": prompt}], role="gate",
                                                             temperature=0.0, max_tokens=120 + 90 * len(texts)), 10)
        except Exception as e:  # noqa: BLE001
            log.info("群 %s：挑选没成（只按规则挑）：%s", gid, e)
            return {}
        return parse_judge(out, len(texts))

    async def choose(self, gid: int, cands: list[list[str]], *, recent: list[str], sim: float, qargs: dict, focus=None,
                     target_uid=None, target_row: dict | None = None, mentioned: bool = False, web: str = "") -> dict:
        """几句 → {"i": 选中的下标（-1 = 一句都不能用 / 不说）, "score", "hits", "skip", "hard_all", "hard_reasons",
        "table": [{k, text, rule, judge, len, total, hard, hits}], "judge"}。"""
        rows = []
        for k, c in enumerate(cands):
            q, qh = check_bubbles(c, recent, sim, **qargs)
            hard = [h for h in qh] if q >= HARD else [h for h in qh if h in HARD_QUALITY]
            if hasattr(self, "hard_issues"):
                hard += list(self.hard_issues(gid, c, focus) or [])
            pen, hits, shadow = (q if q < HARD and not hard else 0.0), list(qh), []
            findings = self.rule_findings(gid, c, target_uid=target_uid, target_row=target_row, mentioned=mentioned,
                                          web=web) if hasattr(self, "rule_findings") else []
            for name, mode, why in findings:
                w = why[len(DROP):] if why.startswith(DROP) else why
                if mode != "on":
                    shadow.append((name, w))
                    continue
                if name in HARD_CHECKS:
                    hard.append(w)
                    continue
                pen += DROP_PEN if why.startswith(DROP) else RULE_PEN
                hits.append(f"{name}：{w[:50]}")
            rows.append({"k": k, "text": " / ".join(c), "rule": round(pen, 2), "hard": hard, "hits": hits,
                         "findings": findings, "shadow": shadow})
        ok = [r for r in rows if not r["hard"]]
        j = await self.judge_cands(gid, [cands[r["k"]] for r in ok], mentioned=mentioned, web=web,
                                   target_uid=target_uid) if ok else {}
        L = self.group_len(gid) if hasattr(self, "group_len") else 0
        for n, r in enumerate(ok, 1):
            jp, jh = judge_penalty((j.get("cands") or {}).get(n))
            lp = (length_penalty(len(r["text"].replace(" / ", "")), L, mentioned,
                                 float(self.gk(gid).get("length_max_ratio", 3) or 3))
                  if self.gk(gid).get("length_match", True) else 0.0)
            r["judge"], r["len"] = jp, lp
            r["hits"] += jh + ([f"太长（群友一般 {L} 字）"] if lp >= 1 else [])
            r["total"] = round(r["rule"] + jp + lp, 2)
        for r in rows:
            r.setdefault("total", None)
        res = {"table": [{x: r.get(x) for x in ("k", "text", "rule", "judge", "len", "total", "hard", "hits")} for r in rows],
               "judge": j, "hard_all": not ok, "hard_reasons": list(dict.fromkeys(h for r in rows for h in r["hard"]))[:4],
               "rows": rows}
        if not ok:
            res.update(i=-1, score=HARD, hits=res["hard_reasons"], skip=True)
            return res
        best = min(ok, key=lambda r: (r["total"], r["k"]))
        silence = j.get("best") == 0 and not mentioned
        res.update(i=best["k"], best=best["k"], score=best["total"], hits=best["hits"],
                   skip=bool(best["total"] >= SKIP_AT or silence), silence=silence)
        return res

    def choose_log(self, gid: int, res: dict, gate_id=None, chosen: int = -1, outcome: str = ""):
        """选中那句命中的规则记进 qq_check_log（和以前一样能统计）；没选上的不记。规则登记（rules.note）一并记。"""
        rows = res.get("rows") or []
        k = chosen if chosen >= 0 else res.get("best", 0 if rows else -1)     # 没发的也按最好那句记（统计规则命中）
        r = next((x for x in rows if x["k"] == k), None)
        if r is not None:
            for name, mode, why in r.get("findings") or []:
                self.check_log(gid, name, mode, r["text"], why, gate_id)
            j = (res.get("judge") or {}).get("cands") or {}
            ok = [x for x in rows if not x["hard"]]
            pos = next((n for n, x in enumerate(ok, 1) if x["k"] == k), 0)
            jj = j.get(pos) or {}
            if jj.get("bad"):
                self.check_log(gid, "critic", "on", r["text"], f"{jj['bad']}：{jj.get('why', '')}", gate_id)
        try:
            from aigroup.chat.rules import note as _rnote
            hits = (r or {}).get("hits") or res.get("hits") or []
            if hits:
                _rnote(self.db, gid, hits, outcome or ("拦下" if chosen < 0 else "罚分"),
                       (rows[0]["text"] if rows else ""))
        except Exception as e:  # noqa: BLE001
            log.debug("记规则命中失败：%s", e)

    @staticmethod
    def choose_summary(res: dict, chosen: int) -> str:
        """trace 里 quality 一栏：几句、各自扣分、选了哪句。"""
        cands = [{"k": c.get("k"), "text": str(c.get("text") or "")[:80], "rule": c.get("rule"), "judge": c.get("judge"),
                  "len": c.get("len"), "total": c.get("total"), "hard": [str(h)[:40] for h in (c.get("hard") or [])[:2]],
                  "hits": [str(h)[:50] for h in (c.get("hits") or [])[:4]]} for c in (res.get("table") or [])[:4]]
        d = {"chosen": chosen, "skip": bool(res.get("skip")), "silence": bool(res.get("silence")), "cands": cands}
        if res.get("wy_k") is not None:                    # 38.4 第几句是文言转白话来的（统计它被挑中的比例）
            d["wy_k"], d["wy"] = res["wy_k"], str(res.get("wy") or "")[:60]
        return json.dumps(d, ensure_ascii=False)
