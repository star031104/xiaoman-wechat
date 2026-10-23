"""5.0 群聊里几件“像不像真人”的小判断（纯函数，不调大模型；chat/ingest.py、speak.py、prompt.py 用）。

- likely_to_her：没 @ 她，但这句多半是冲她说的（她刚说完话，同一个人接着说“你……”，或者接了她刚说的词）
- her_phrases：她最近几个小时已经用过的口头禅、自己的招牌说法 → 先别再用（提示词里说一声，质检也看）
- shape_bubbles：大多数时候只发一条；两条都很短、或者最近几次都是分两条发的，就合成一条
- 真实群聊里测出来的问题（2026-10-20 的微信群）：被 @ 才回、不接“你小本本上都记啥了”这种没 @ 的追问；
  同一个梗（验货、小本本、我宣布）一个早上说三遍；几乎每次都拆两条发
"""
from __future__ import annotations

import re

CJK = re.compile(r"[一-鿿]+")
TAG = re.compile(r"\[[^\]]{0,40}\]")
# 太常见、不算谁的“招牌说法”的两字词
COMMON2 = set("""这个 那个 什么 怎么 我们 你们 他们 她们 就是 还是 可以 不是 没有 知道 觉得 一下 一个 现在 今天 明天 昨天 时候 真的 已经
自己 因为 所以 但是 不过 然后 感觉 还有 这么 那么 好像 应该 可能 起来 出来 一点 东西 事情 有点 怎么 为什么 这样 那样 哈哈 哈哈哈
来了 去了 好的 不要 不会 不行 要是 如果 而且 一起 大家 别人 一直 刚才 刚刚 马上 等下 一样 看看 说说 问问 吃饭 上课 睡觉 开始""".split())


def _plain(t: str) -> str:
    return TAG.sub("", t or "")


def grams(text: str, n: int) -> set[str]:
    out = set()
    for run in CJK.findall(_plain(text)):
        for i in range(len(run) - n + 1):
            out.add(run[i:i + n])
    return out


def shared_terms(text: str, hers: list[str], n: int = 3) -> set[str]:
    """这句话和她最近说的话有没有共同的词（三个字的片段，比如“小本本”）。"""
    mine = grams(text, n)
    if not mine:
        return set()
    theirs = set()
    for h in hers:
        theirs |= grams(h, n)
    return mine & theirs


ABOUT_HER = re.compile(r"它|这个?(机器人|bot|AI|人机)|(机器人|人机|bot)(怎么|好|真|还|又|不)", re.I)


def likely_to_her(text: str, *, now: float, her_last_ts: float, attention: float, her_recent: list[str],
                  at_other: bool = False, reply_other: bool = False, other_names: list[str] | None = None,
                  window: float = 180.0, about_window: float = 600.0) -> str:
    """没 @ 她、没叫她名字，但多半是冲她说的 → "to"；在背后说她（“它不聪明”“这机器人还挺会”）→ "about"；都不是 → ""。

    "to"：她刚说完（window 秒内），这句不是 @ 别人、回复别人，开头也没叫别人的名字；并且：
      这句里有“你”（不只是“你们”），说话的人正跟她聊着（注意力 ≥ 0.3）或接了她刚说的词；
      或者没有“你”，但接了她刚说的词、还是个问句（“小本本记了啥？”）。
    "about"：她这 10 分钟里说过话，有人用“它 / 机器人 / 人机”说她（不是 @ 别人、回复别人的）。"""
    if at_other or reply_other or not her_last_ts:
        return ""
    t = _plain(text).strip()
    if not t:
        return ""
    if now - her_last_ts <= window:
        head = t[:8]
        if not any(n and len(n) >= 2 and n in head for n in (other_names or [])):
            shared = shared_terms(t, her_recent[:3])
            if "你" in t.replace("你们", ""):
                if attention >= 0.3 or shared:
                    return "to"
            elif shared and re.search(r"[?？吗呢啥]", t):
                return "to"
    if (now - her_last_ts <= about_window and "你" in t.replace("你们", "")
            and not any(n and len(n) >= 2 and n in t[:8] for n in (other_names or [])) and re.search(r"[?？吗呢啥哪谁咋几]", t)
            and her_recent and _topic_overlap(t, her_recent[:3])):
        return "to"                         # 22.0 “你还有什么稿子”“哪个老师叫你去的”：问她刚说的事，十分钟内都算冲她
    if now - her_last_ts <= about_window and ABOUT_HER.search(t):
        return "about"
    return ""


def _topic_overlap(text: str, hers: list[str]) -> bool:
    """两个字的、不太常见的词有没有重合（“稿子”“采访”）。"""
    def g2(x):
        return {w for w in grams(x, 2) if w not in COMMON2 and not w.startswith("你") and not w.startswith("我")}
    mine = g2(text)
    return bool(mine and any(mine & g2(h) for h in hers))


def _blocks(a: str, b: str, min_len: int = 2) -> list[str]:
    """两句话里共同的、连续的中文片段（最长的那几段）。"""
    from difflib import SequenceMatcher
    out = []
    for m in SequenceMatcher(None, a, b, autojunk=False).get_matching_blocks():
        seg = a[m.a:m.a + m.size]
        for run in CJK.findall(seg):
            if len(run) >= min_len:
                out.append(run)
    return out


def her_phrases(her_recent: list[str], her_day: list[str], others: list[str], catchphrases: list[str],
                limit: int = 8, names: list[str] | None = None) -> list[str]:
    """她最近（几个小时内）用过、先别再用的说法：
    1) 人设里的口头禅，最近说过的；
    2) 她这一天里在两句以上不同的话里都说过、群友最近没说过的片段（她的“招牌梗”，比如“验货”“替你答到”），
       而且最近几小时里又用过。"""
    out: list[str] = []
    recent_txt = " ".join(_plain(x) for x in her_recent)
    for c in catchphrases or []:
        c = str(c).strip()
        if c and c in recent_txt and c not in out:
            out.append(c)
    others_txt = " ".join(_plain(x) for x in others)
    day = [_plain(x) for x in her_day if x and not x.startswith("[")][:60]
    counts: dict[str, int] = {}
    for i in range(len(day)):
        for j in range(i + 1, len(day)):
            for b in set(_blocks(day[i], day[j])):
                counts[b] = counts.get(b, 0) + 1
    cand = [g for g in counts if g in recent_txt and g not in others_txt and not (len(g) == 2 and g in COMMON2)
            and not (len(g) == 2 and counts[g] < 3)            # 两个字的：至少三句里都说过才算她的“招牌”
            and not all(ch in "了的吗呢啊吧嘛呀哦哈么" for ch in g)
            and not (len(g) <= 4 and g[0] in "你我他她这那就也都还")
            and not any(n and (g in n or n in g and len(g) - len(n) <= 1) for n in (names or []))]
    cand.sort(key=lambda g: (-counts[g], -len(g)))
    for g in cand:
        if any(g in x or x in g for x in out):          # 已经有包含它的（“已记入小本本”里有“小本本”）
            continue
        out.append(g[:12])
        if len(out) >= limit:
            break
    return out[:limit]


FILLER = set("我你他她它的了是在有就也都还这那么吗呢啊吧呀哦嘛哈个一不")


def _meaningful(b: str, names=()) -> bool:
    """一个共同片段算不算“说过的东西”：不是一串虚词、不是群友名字（叫同一个人很正常）、不是常用词。"""
    core = b.strip("".join(FILLER))
    for w in sorted(COMMON2, key=len, reverse=True):          # “今天准备”去掉常用词只剩“准备”：不算
        core = core.replace(w, "")
    core = "".join(ch for ch in core if ch not in FILLER)
    if len(core) < 3:
        return False
    for n in names or ():
        if n and len(n) >= 2 and n in b and len(b) - len(n) <= 2:
            return False
    return True


def echo_today(text: str, lines: list[str], names=(), min_len: int = 5) -> str:
    """19.1 这句和她今天说过的某句意思撞了：有 ≥ min_len 个字的共同片段（“纪录片看哭”）→ 返回那个片段，没有返回 ""。
    真实群聊（10-21）：早上 6:40 “别到时候又看纪录片看哭”，10:01 又 @ 他“AG 赢了别又看纪录片看哭”。"""
    t = _plain(text)
    if len(t) < min_len:
        return ""
    for ln in lines or []:
        ln = _plain(ln)
        if not ln or ln.startswith("["):
            continue
        for b in _blocks(t, ln, min_len):
            if _meaningful(b, names):
                return b
    return ""


def plan_terms(plan_text: str, her_lines: list[str], names=(), min_len: int = 3, limit: int = 8) -> list[str]:
    """19.1 她今天安排里的东西，她今天已经主动说过的（“桑拿鸡”）：同一天别再主动提第二次。"""
    plan = _plain(plan_text)
    if not plan:
        return []
    out: list[str] = []
    for ln in her_lines or []:
        for b in _blocks(plan, _plain(ln), min_len):
            if _meaningful(b, names) and b[0] not in "你我他她这那就也都还的了在" and not any(b in x for x in out):
                out = [x for x in out if x not in b] + [b[:10]]
    return out[:limit]


def shape_bubbles(bubbles: list[str], recent_sizes: list[int], rand: float, long: bool = False) -> list[str]:
    """大多数时候一条：两条都短（合起来 ≤ 28 个字）时一半概率合成一条；最近 3 次都是分好几条发的，就一定合。
    长回复（len=long）、带 @ 的、第二条是“*改错字”的不动。"""
    bs = [b for b in bubbles if b and b.strip()]
    if len(bs) < 2 or long or any(b.startswith("*") for b in bs[1:]) or any("[@" in b for b in bs[1:]):
        return bs
    streak = len(recent_sizes) >= 3 and all(n >= 2 for n in recent_sizes[:3])
    total = sum(len(b) for b in bs)
    if streak and total <= 50 or total <= 28 and rand < 0.5:
        joined = bs[0].rstrip()
        for b in bs[1:]:
            sep = "" if re.search(r"[，。！？!?,~…、]$", joined) else "，"
            joined += sep + b.strip()
        return [joined]
    return bs
