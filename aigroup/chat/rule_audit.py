"""32.0 规则体检（B 站“人设提示词像一道数学题：条件缺了、互相冲突，推出来就错”）。

不改任何东西，只把提示词里的规则摆出来查三件事：
1. 自己推自己踩：检查里限着“一天最多一次”的说法（checks.PHRASE_FAMILIES），提示词 / 人设 / 人格文件却拿它当例子、
   当口头禅写着（10-26 体检：人格文件三处写“小本本 / 记仇”，检查又限一天一次）；
2. 前后打架：一处说“别 X”，另一处说“可以 / 要 / 多 X”；
3. 重复：同一条规矩在两处各写一遍（改一处忘了另一处，就成了 2）。
另外数一下每一段有多少条“别 / 不要 / 禁止”，补丁越堆越多的段落一眼就能看出来。

scripts/rule_audit.py 拿线上最新的 system 提示词和人格文件跑；tests 里拿代码里的提示词跑（应该是 0）。
"""
from __future__ import annotations

import re

CLAUSE = re.compile(r"[，,。；;！!？?\n]")
LIMIT = re.compile(r"别|不要|不用|不能|不再|最多|一天|少用|少说|换个说法|禁止|不许|上限|超过|一次|限|拦|算一个|也算|"
                   r"(^|[：:\s])不|not\s*:")                 # “不追问……”“not: 说……”也是在限制
NEG = re.compile(r"(?:别|不要|禁止|不许|不能|不再|少)(?:再|总|老|太|乱|硬|每次都|一上来就)?([一-龥]{2,6})")
FILLER = {"一点", "一下", "一些", "一句", "一个", "一次", "一会", "几句", "两句"}
ENC = re.compile(r"(?:可以|要|多|记得|就该|应该|常|爱|喜欢|偶尔)(?:再|多)?([一-龥]{2,6})")
NEG_BEFORE = re.compile(r"(别|不要|禁止|不许|不能|不再|不|没|少)[^，,。；;]{0,3}$")


def rule_lines(sources: dict[str, str]) -> list[tuple[str, str]]:
    """{来源: 文本} → [(来源, 一条规则)]。按行切，太短的、没有中文的不算。"""
    out = []
    for src, text in sources.items():
        for ln in str(text or "").splitlines():
            ln = ln.strip().lstrip("-*0123456789.、 ").strip()
            if len(ln) >= 6 and re.search(r"[一-龥]", ln):
                out.append((src, ln))
    return out


def clauses(line: str) -> list[str]:
    return [c.strip() for c in CLAUSE.split(line) if c.strip()]


def seeds(rules: list[tuple[str, str]], families: dict) -> list[dict]:
    """检查里限着的说法，在规则里被当成例子 / 口头禅写着（那一小句里没有“别 / 最多 / 一天”这类限制词）。"""
    out = []
    for src, ln in rules:
        for c in clauses(ln):
            if LIMIT.search(c):
                continue
            for fam, rx in families.items():
                m = rx.search(c)
                if m:
                    out.append({"source": src, "family": fam, "word": m.group(0), "clause": c, "line": ln[:160]})
    return out


def _ngrams(t: str) -> set:
    t = re.sub(r"[^一-龥a-zA-Z0-9]", "", t)
    return {t[i:i + 2] for i in range(len(t) - 1)}


def conflicts(rules: list[tuple[str, str]]) -> list[dict]:
    """一处“别 X”，另一处“可以 / 要 / 多 X”（X 至少两个字，前面没有否定词）。"""
    negs = []
    for src, ln in rules:
        for c in clauses(ln):
            for m in NEG.finditer(c):
                x = m.group(1)[:4]
                if len(x) >= 2 and x[:2] not in FILLER:
                    negs.append((src, ln, x))
    out, seen = [], set()
    for src, ln in rules:
        for c in clauses(ln):
            for m in ENC.finditer(c):
                y = m.group(1)
                for nsrc, nln, x in negs:
                    if nln == ln or x not in y[:len(x) + 1]:
                        continue
                    pre = c[:c.find(x)] if x in c else ""
                    if NEG_BEFORE.search(pre):
                        continue
                    key = (x, nln[:40], ln[:40])
                    if key in seen:
                        continue
                    seen.add(key)
                    out.append({"word": x, "no": {"source": nsrc, "line": nln[:160]}, "yes": {"source": src, "line": ln[:160]}})
    return out


def duplicates(rules: list[tuple[str, str]], threshold: float = 0.55) -> list[dict]:
    """两处写的同一条规矩（字二元组重合 ≥ threshold，长度 ≥ 12）。同一来源里的也算。"""
    grams = [(src, ln, _ngrams(ln)) for src, ln in rules if len(ln) >= 12]
    out = []
    for i in range(len(grams)):
        for j in range(i + 1, len(grams)):
            a, b = grams[i][2], grams[j][2]
            if not a or not b:
                continue
            jac = len(a & b) / len(a | b)
            if jac >= threshold and grams[i][1] != grams[j][1]:
                out.append({"score": round(jac, 2), "a": {"source": grams[i][0], "line": grams[i][1][:160]},
                            "b": {"source": grams[j][0], "line": grams[j][1][:160]}})
    return sorted(out, key=lambda d: -d["score"])


def prohibitions(rules: list[tuple[str, str]]) -> dict[str, int]:
    """每个来源有多少条带“别 / 不要 / 禁止 / 不许 / 最多”的规则。"""
    out: dict[str, int] = {}
    for src, ln in rules:
        if re.search(r"别|不要|禁止|不许|最多", ln):
            out[src] = out.get(src, 0) + 1
    return out


def split_sections(system: str) -> dict[str, str]:
    """system 提示词按“# 标题”切成几段，标题当来源。"""
    out: dict[str, str] = {}
    head = "开头"
    for ln in str(system or "").splitlines():
        if ln.startswith("# "):
            head = ln[2:].strip()[:24]
            continue
        out[head] = out.get(head, "") + ln + "\n"
    return out


def audit(sources: dict[str, str], families: dict | None = None) -> dict:
    if families is None:
        from aigroup.chat.checks import PHRASE_FAMILIES as families
    rules = rule_lines(sources)
    # 人格文件本身会渲染进 system 的“你的人格”：拿它和提示词比“打架 / 两遍”就是自己和自己比，只查“自己推自己踩”
    cmp = [(src, ln) for src, ln in rules if not src.startswith("人格文件")]
    return {"rules": len(rules), "prohibitions": prohibitions(rules), "seeds": seeds(rules, families),
            "conflicts": conflicts(cmp), "duplicates": duplicates(cmp)}


def report(r: dict, top: int = 15) -> str:
    out = [f"# 规则体检：{r['rules']} 条规则",
           "## 每段有几条“别 / 不要 / 禁止 / 最多”",
           *[f"- {k}：{v}" for k, v in sorted(r["prohibitions"].items(), key=lambda kv: -kv[1])],
           f"## 1. 自己推自己踩（{len(r['seeds'])}）：检查里一天限一次的说法，规则里当例子写着"]
    out += [f"- 【{d['source']}】“{d['word']}”（{d['family']}）：{d['clause']}" for d in r["seeds"][:top]]
    out.append(f"## 2. 前后打架（{len(r['conflicts'])}）：一处“别 X”，一处“要 / 可以 X”（机器找的，要人看一眼）")
    out += [f"- “{d['word']}”：【{d['no']['source']}】{d['no']['line']}  ⟷  【{d['yes']['source']}】{d['yes']['line']}"
            for d in r["conflicts"][:top]]
    out.append(f"## 3. 写了两遍（{len(r['duplicates'])}）")
    out += [f"- {d['score']}：【{d['a']['source']}】{d['a']['line']}  ⟷  【{d['b']['source']}】{d['b']['line']}"
            for d in r["duplicates"][:top]]
    return "\n".join(out)
