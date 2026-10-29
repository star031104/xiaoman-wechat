"""判断和解析的纯函数（不碰数据库、不调大模型，好测）：

- 要不要开口：batch_features（一批消息的特征）→ decide（冷却、上限、免打扰、阈值）
- 大模型回复怎么用：lenient_json（宽松解析 JSON）、parse_group_reply（纯文字回复拆成几条）、to_segments（@名字 → QQ 的 at 段）
- 几个文字规则：BUSY_RE（她在忙）、QUESTION_RE（在问大家）、NAME_TAIL（“小满足”不算叫她）……
"""
from __future__ import annotations

import json
import math
import re

from aigroup.onebot import at_seg, text_seg


def bigram_overlap(a: str, b: str) -> float:
    """两句话的字二元组重合度（粗略判断是不是在接同一个话题）。"""
    def grams(t):
        t = re.sub(r"\[[^\]]*\]|@\S+|[\s，。！？!?,.~～…]", "", t or "")
        return {t[i:i + 2] for i in range(len(t) - 1)}
    ga, gb = grams(a), grams(b)
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / min(len(ga), len(gb))


BUSY_RE = re.compile(r"课|上班|开会|考试|实验|兼职|值班|练琴|排练|睡|午休|洗澡|通勤|地铁|面试|答辩|自习|图书馆")


QUESTION_RE = re.compile(r"有没有人|有人|大家|谁(知道|会|有|去)|求推荐|推荐一下|怎么办|[?？]\s*$")


REACT_RE = re.compile(r"[\[【]\s*回应\s*[:：]\s*([^\]】]{1,8})[\]】]")


EMOJI_ONLY_RE = re.compile(r"^(\s|\[(图片|表情|表情包|动画表情)[^\]]*\]|[\U0001F000-\U0001FAFF\u2600-\u27BF])*$")


PHOTO_RE = re.compile(r"\[图片")


# 名字后面紧跟这些字，多半是别的词（小满足、小满意、小满月……），不算叫她
NAME_TAIL = "足意月载贯盈"


# 人设里的性格词、说话方式，不算“感兴趣的话题”
TRAIT_WORDS = {"吐槽", "毒舌", "揣测", "反问", "装蒜", "说话碎", "话多", "自信", "笑场", "可爱", "心软", "嘴硬", "得意",
               "欠揍", "损人", "互怼", "直球", "颜值", "傲娇", "温柔", "开朗", "腼腆", "话痨", "调皮", "撒娇", "立flag", "拖延"}


def decide(*, mentioned: bool, now: float, last_reply_ts: float, replies_10min: int, quiet: bool, g: dict,
           p: float, thr: float, backoff_wait: float = 0.0, pending: int = 1, tie: float = 1.0,
           rand: float = 0.0, waited: bool = False, burst: int = 0, hush: str = "", corrected: bool = False,
           asked: bool = False) -> tuple[bool, str]:
    """这一批消息之后她要不要开口。p：gate 算的概率；thr：按发言配额算的阈值。返回 (要不要, 原因)。
    23.1 burst：她最近 5 分钟发了几条（不算被叫到才回的）；hush：群里刚有人嫌她 / 她自己说了“闭嘴”，到点前只回叫到她的。"""
    cap = int(g["max_per_10min"])
    if mentioned:
        if replies_10min >= cap * 2:
            return False, "被叫到了，但这 10 分钟说得太多了"
        return True, "被叫到"
    if quiet:
        return False, "免打扰时段，只回叫到她的"
    if corrected:
        # 23.3 群友刚指出她说错了：认一句不受刷屏刹车 / 安静 / 冷却限制（10-24 13:24 刹车把她的认错也拦了，像没听见）
        return True, "被纠正，认一句"
    if asked and not hush and replies_10min < cap * 2:
        # 30.1 没 @ 但明显在问她（接她的话、问她看法）：和被叫到一样，不受刷屏刹车 / 冷却拦（10-27 21:05 被“5 分钟 5 条”拦了，17 分钟后被 @ 才答）
        return True, "在问她"
    if replies_10min >= cap:
        return False, "10 分钟上限"
    if hush:
        return False, f"先安静会儿（{hush}）"
    hard = int(g.get("burst_max", 5) or 0)
    if hard and burst >= hard:
        return False, f"这 5 分钟已经说了 {burst} 条，先歇会儿"
    soft = int(g.get("burst_soft", 3) or 0)
    if soft and burst >= soft and g.get("burst_soft_hard", True):
        # 31.2 软刹车改硬：最近 5 分钟已经说了 soft 条、又没人叫她，就不说（以前重新过门槛就放行，10-28 / 10-29 天天 3 分钟 ≥4 条）
        return False, f"这 5 分钟已经说了 {burst} 条，没人叫就不说"
    if waited and not (soft and burst >= soft):
        return True, "等的人开口了"
    if waited:
        waited = False                      # 23.1 一来一回说多了：“接她的话”也要过冷却和门槛
    if last_reply_ts and now - last_reply_ts < float(g["cooldown_seconds"]):
        return False, "冷却中"
    if backoff_wait > 0 and pending < 6:
        return False, f"刚连着不想说，再等 {backoff_wait:.0f} 秒"
    from aigroup.chat.gate import passes
    if not passes(p, thr, tie, rand):
        return False, f"不太想插话（{p:.2f} < 阈值 {thr:.2f}）"
    return True, f"插话（{p:.2f} ≥ 阈值 {thr:.2f}）"


# 31.2 只是回答她的问题（“没有”“嗯”“对”），不是在问她（10-29 15:33 群友回“没有”被当成在问她，绕过了刷屏刹车）
BARE_ANSWER = re.compile(r"^\s*(@\S+\s*)?(没有?|有|嗯+|对+|是+|不是|好+|行+|可以|没呢|还没|没看|看了|没追|知道了?|不知道|"
                         r"哈+|ok|OK|好的|收到|算了)[。！!~～，,]*\s*$")


def burst_room(her_3min: int, n: int, mentioned: bool, cap: int = 3) -> int:
    """31.2 发之前再数一遍：她最近 3 分钟已经发了 her_3min 条，这次要发 n 条，合起来别超过 cap 条（3 分钟 ≥4 条算刷屏）。
    决定开口时数的是那一刻的，从想到发要几十秒，中间她可能已经发了别的（10-29 15:33 回放必拦、线上却发了）。
    被叫到至少留 1 条。返回这次最多还能发几条。"""
    room = max(0, cap - max(0, int(her_3min)))
    return min(n, max(room, 1 if mentioned else 0))


def batch_features(texts: list, *, now: float, last_reply_ts: float, prev_msg_ts: float, recent: list,
                   owner_spoke: bool, interest: bool, busy: bool, attention: float, energy: float,
                   cold_minutes: float, ignored: int = 0, waited: bool = False, turns: float = 0.0,
                   mood: float = 0.0) -> dict:
    """一批消息 → gate 的特征。recent：最近 5 分钟群里的 [(ts, role)]。"""
    joined = " ".join(t[2] for t in texts)
    metas = [t[3] if len(t) > 3 else {} for t in texts]
    n5 = len(recent)
    her5 = sum(1 for _, r in recent if r == "assistant")
    gap = now - last_reply_ts if last_reply_ts else 86400.0
    return {
        "at_other": 1.0 if any(m.get("at_other") for m in metas) else 0.0,
        "reply_other": 1.0 if any(m.get("reply_other") for m in metas) else 0.0,
        "question": 1.0 if QUESTION_RE.search(joined) or "?" in joined or "？" in joined else 0.0,
        # 23.0 只有表情 / 表情包才算；晒照片、截图（[图片…]）是在分享东西，不当成“没内容”
        "emoji_only": 1.0 if texts and all(EMOJI_ONLY_RE.match(t[2] or "") and not PHOTO_RE.search(t[2] or "")
                                           for t in texts) else 0.0,
        "self_share": her5 / n5 if n5 else 0.0,
        "n5m": float(n5),
        "ln_gap": math.log1p(max(0.0, gap)),
        "batch": float(len(texts)),
        "owner": 1.0 if owner_spoke else 0.0,
        "interest": 1.0 if interest else 0.0,
        "busy": 1.0 if busy else 0.0,
        "attention": float(attention),
        "tired": 1.0 - float(energy),
        "cold": 1.0 if prev_msg_ts and now - prev_msg_ts > cold_minutes * 60 else 0.0,
        "ignored": min(1.0, int(ignored) / 3),
        "waited": 1.0 if waited else 0.0,
        "turns": float(turns),
        "mood": float(mood),
    }


AT_RE = re.compile(r"\[@([^\]\n]{1,30})\]")


TAG_RE = re.compile(r"\[[^\]\n]{0,40}\]")
KEEP_TAGS: set[str] = set()      # 这些 [xx] 留着不删（微信版放微信小表情的名字：[捂脸] 粘进去就是表情）


def parse_group_reply(raw: str, name: str, max_bubbles: int) -> list[str] | None:
    """模型输出 → 要发的几条。None ＝ 跳过。保留 [@名字]，别的标签去掉（[表情:x] 换成 emoji）。"""
    from aigroup.core.util import emotion_to_emojis
    raw = (raw or "").strip()
    if not raw or re.search(r"\[(跳过|SKIP|skip|沉默)\]", raw) and len(TAG_RE.sub("", raw).strip()) < 2:
        return None
    raw = re.sub(rf"^\s*{re.escape(name)}\s*[:：]\s*", "", raw)
    chunks = re.split(r"\s*\|{2,}\s*", raw) if re.search(r"\|{2,}", raw) else re.split(r"\n+", raw)
    out: list[str] = []
    for c in chunks:
        def emo(m):
            e = emotion_to_emojis(m.group(1))
            return e[0] if e else ""
        c = re.sub(r"\[表情[:：]([^\]]+)\]", emo, c)
        c = TAG_RE.sub(lambda m: m.group(0) if AT_RE.fullmatch(m.group(0)) or m.group(0)[1:-1] in KEEP_TAGS else "", c)
        c = re.sub(rf"^\s*{re.escape(name)}\s*[:：]\s*", "", c).strip().strip("“”\"")
        if c:
            out.append(c)
    if not out:
        return None
    if len(out) > max_bubbles:
        out = out[:max_bubbles - 1] + [" ".join(out[max_bubbles - 1:])]
    return out


def to_segments(text: str, name_to_qq: dict[str, int]) -> list[dict]:
    """把 [@名字] 换成真正的 @，其余都是纯文字段（不解析 CQ 码）。@ 不到的名字就留成文字“@名字”。"""
    segs: list[dict] = []
    pos = 0
    # 模型常写成“@小王 ”而不是“[@小王]”：名字认识的也变成真正的 @（长名字先换，免得“小王”吃掉“小王子”）
    for nm in sorted((n for n in name_to_qq if n), key=len, reverse=True):
        text = re.sub(rf"(?<!\[)@{re.escape(nm)}(?=[\s，,：:！!？?。]|$)", lambda _m, n=nm: f"[@{n}]", text)
    for m in AT_RE.finditer(text):
        if m.start() > pos:
            segs.append(text_seg(text[pos:m.start()]))
        nm = m.group(1).strip().lstrip("@")
        qq = name_to_qq.get(nm)
        if qq and nm not in ("全体成员", "all"):
            segs.append(at_seg(qq))
            segs.append(text_seg(" "))
        else:
            segs.append(text_seg(f"@{nm} "))
        pos = m.end()
        while pos < len(text) and text[pos] == " ":            # @ 后面已经补了空格
            pos += 1
    if pos < len(text):
        segs.append(text_seg(text[pos:]))
    return [s for s in segs if s["type"] != "text" or s["data"]["text"]]


def lenient_json(text: str):
    """容错地从大模型输出里抠出 JSON 对象：去掉 <think>、代码块，补尾随逗号、中文引号，截断时尽量救回 say。"""
    from aigroup.llm.client import strip_think
    t = strip_think(text or "")
    m = re.search(r"```(?:json)?\s*(.*?)```", t, flags=re.S)
    if m:
        t = m.group(1)
    i = t.find("{")
    if i < 0:
        return None
    body = t[i:]
    j = body.rfind("}")
    cands = [body[:j + 1]] if j > 0 else []
    for c in list(cands):
        c2 = re.sub(r",\s*([}\]])", r"\1", c)
        c2 = c2.replace("“", '"').replace("”", '"') if c2.count('"') < 4 else c2
        cands.append(c2)
    for c in cands:
        try:
            d = json.loads(c)
            if isinstance(d, dict):
                return d
        except ValueError:
            continue
    # 被截断了：至少把 act 和 say 里完整的字符串救回来
    act = re.search(r'"act(?:ion)?"\s*:\s*"(\w+)"', body)
    say = re.search(r'"say"\s*:\s*\[(.*)', body, flags=re.S)
    if act:
        items = re.findall(r'"((?:[^"\\]|\\.)*)"\s*[,\]]', say.group(1)) if say else []
        return {"act": act.group(1), "say": [json.loads(f'"{x}"') for x in items], "_partial": True}
    return None


KINDS = ("I1", "I2", "S1", "S2")
