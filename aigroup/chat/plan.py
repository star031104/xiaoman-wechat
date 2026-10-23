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


# 名字后面紧跟这些字，多半是别的词（小满足、小满意、小满月……），不算叫她
NAME_TAIL = "足意月载贯盈"


# 人设里的性格词、说话方式，不算“感兴趣的话题”
TRAIT_WORDS = {"吐槽", "毒舌", "揣测", "反问", "装蒜", "说话碎", "话多", "自信", "笑场", "可爱", "心软", "嘴硬", "得意",
               "欠揍", "损人", "互怼", "直球", "颜值", "傲娇", "温柔", "开朗", "腼腆", "话痨", "调皮", "撒娇", "立flag", "拖延"}


def decide(*, mentioned: bool, now: float, last_reply_ts: float, replies_10min: int, quiet: bool, g: dict,
           p: float, thr: float, backoff_wait: float = 0.0, pending: int = 1, tie: float = 1.0,
           rand: float = 0.0, waited: bool = False) -> tuple[bool, str]:
    """这一批消息之后她要不要开口。p：gate 算的概率；thr：按发言配额算的阈值。返回 (要不要, 原因)。"""
    cap = int(g["max_per_10min"])
    if mentioned:
        if replies_10min >= cap * 2:
            return False, "被叫到了，但这 10 分钟说得太多了"
        return True, "被叫到"
    if quiet:
        return False, "免打扰时段，只回叫到她的"
    if replies_10min >= cap:
        return False, "10 分钟上限"
    if waited:
        return True, "等的人开口了"
    if last_reply_ts and now - last_reply_ts < float(g["cooldown_seconds"]):
        return False, "冷却中"
    if backoff_wait > 0 and pending < 6:
        return False, f"刚连着不想说，再等 {backoff_wait:.0f} 秒"
    from aigroup.chat.gate import passes
    if not passes(p, thr, tie, rand):
        return False, f"不太想插话（{p:.2f} < 阈值 {thr:.2f}）"
    return True, f"插话（{p:.2f} ≥ 阈值 {thr:.2f}）"


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
        "emoji_only": 1.0 if texts and all(EMOJI_ONLY_RE.match(t[2] or "") for t in texts) else 0.0,
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
