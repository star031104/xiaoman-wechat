"""AI 腔小分类器（3.0）：分辨“像群友打的字”还是“像大模型写的”。

依据：
- Computational Turing Test（2025）：用分类器在几条候选里挑“最不像 AI 的”收益最大
- 中文 AI 文本检测（2025-09）：短文本上 FastText 这种字 n-gram 线性模型（83.5%）比 BERT、RoBERTa 还准
- C-ReD（2026）：现成检测器对 DeepSeek 的输出接近随机，必须用自己的数据训

这里不装 fasttext，用纯 Python 的字 1~3 元组多项式朴素贝叶斯（同类模型，几千条几百毫秒就训完）：
- 负例（人）：这个群群友自己打的字
- 正例（AI）：她（大模型）发出去的话、被质检拦下的候选
每晚攒够各 150 条就重训一次，模型存在 kv 里；quality.check 里概率 > 0.8 才扣分。

22.3 修：以前对她的每句话都打 1.00——正例是“她说的话”、负例是“群友说的话”，模型学会的是“谁在说话”
（群友的名字、她的口头禅、她爱聊的话题），不是“像不像 AI”；朴素贝叶斯把几十个字组的对数似然比直接加起来，
概率又一下子就到头。改成：
- 先把群友的名字 / 外号、她的口头禅、数字、英文词、链接换成占位符，只留“怎么说”的信号
- 分数用每个字组的平均似然比（不随句子变长无限变大），再在留出集上做 Platt 校准（概率是真的“几成像”）
- 留出集 AUC ≥ 0.75，而且拿你打过分的她的话验证：它说越像 AI 的，你打的分要越低（Spearman ≤ -0.2，≥ 30 条时才判），
  两条都过才用。10-23 用线上数据验：旧模型对她每句都是 1.00、和你打的分相关 0.06；新模型去掉“谁在说话”以后相关 0.09，
  也就是“像不像群友打的字”和你觉得好不好基本无关——过不了线就不用，不再给每句话扣“像 AI”的分
"""
from __future__ import annotations

import json
import math
import re
from collections import Counter

MIN_EACH = 150
MAX_VOCAB = 20000
MIN_AUC = 0.75
_TAG = re.compile(r"\[[^\]]{0,200}\]|@\S+|https?://\S+")


def normalize(text: str, mask: list[str] | None = None) -> str:
    t = _TAG.sub(" ", str(text or ""))
    for w in sorted(mask or [], key=len, reverse=True):
        if w and len(w) >= 2:
            t = t.replace(w, "Ⓝ")
    t = re.sub(r"[A-Za-z][A-Za-z0-9_]*", "ａ", t)
    t = re.sub(r"\d+", "０", t)
    return re.sub(r"\s+", " ", t).strip()


def grams(text: str, mask: list[str] | None = None) -> list[str]:
    t = normalize(text, mask)
    out = []
    for n in (1, 2, 3):
        out += [t[i:i + n] for i in range(len(t) - n + 1)]
    out.append(f"<len{min(6, len(t) // 10)}>")                      # 长度也是很强的信号：群友的话大多很短
    return out


def _sig(z: float) -> float:
    z = max(-30.0, min(30.0, z))
    return 1 / (1 + math.exp(-z))


def auc(pos: list[float], neg: list[float]) -> float | None:
    if not pos or not neg:
        return None
    allv = sorted([(v, 1) for v in pos] + [(v, 0) for v in neg])
    rank_sum, i = 0.0, 0
    while i < len(allv):
        j = i
        while j + 1 < len(allv) and allv[j + 1][0] == allv[i][0]:
            j += 1
        r = (i + j) / 2 + 1
        rank_sum += sum(r for k in range(i, j + 1) if allv[k][1])
        i = j + 1
    n1, n0 = len(pos), len(neg)
    return round((rank_sum - n1 * (n1 + 1) / 2) / (n1 * n0), 3)


def platt(scores: list[float], labels: list[int], iters: int = 300) -> tuple[float, float]:
    """一维逻辑回归：p = sigmoid(a·s + b)。"""
    a, b = 1.0, 0.0
    lr = 0.5
    n = max(1, len(scores))
    for _ in range(iters):
        ga = gb = 0.0
        for s, y in zip(scores, labels):
            p = _sig(a * s + b)
            ga += (p - y) * s
            gb += p - y
        a -= lr * ga / n
        b -= lr * gb / n
    return round(a, 4), round(b, 4)


class Detector:
    def __init__(self, logp: dict, prior: float, default: tuple[float, float], n: tuple[int, int],
                 mask: list[str] | None = None, cal: tuple[float, float] = (1.0, 0.0)):
        self.logp, self.prior, self.default, self.n = logp, prior, default, n
        self.mask, self.cal = list(mask or []), tuple(cal)

    def score(self, text: str) -> float:
        """每个字组平均的 log(像 AI / 像人)。"""
        gs = grams(text, self.mask)
        if not gs:
            return 0.0
        z = 0.0
        for g in gs:
            a, h = self.logp.get(g, self.default)
            z += a - h
        return z / len(gs)

    def prob(self, text: str) -> float:
        """像 AI 的概率（0~1，校准过）。"""
        a, b = self.cal
        return _sig(a * self.score(text) + b)

    @classmethod
    def train(cls, ai: list[str], human: list[str], alpha: float = 0.5, mask: list[str] | None = None) -> "Detector":
        ca, ch = Counter(), Counter()
        for t in ai:
            ca.update(grams(t, mask))
        for t in human:
            ch.update(grams(t, mask))
        vocab = [g for g, _ in (ca + ch).most_common(MAX_VOCAB)]
        ta = sum(ca[g] for g in vocab) + alpha * (len(vocab) + 1)
        th = sum(ch[g] for g in vocab) + alpha * (len(vocab) + 1)
        logp = {g: (round(math.log((ca[g] + alpha) / ta), 4), round(math.log((ch[g] + alpha) / th), 4)) for g in vocab}
        default = (math.log(alpha / ta), math.log(alpha / th))
        return cls(logp, 0.0, default, (len(ai), len(human)), mask)

    def to_json(self) -> dict:
        return {"v": 2, "logp": self.logp, "prior": self.prior, "default": list(self.default), "n": list(self.n),
                "mask": self.mask, "cal": list(self.cal)}

    @classmethod
    def from_json(cls, d: dict) -> "Detector":
        return cls({k: tuple(v) for k, v in d["logp"].items()}, float(d["prior"]), tuple(d["default"]), tuple(d["n"]),
                   d.get("mask") or [], tuple(d.get("cal") or (1.0, 0.0)))

    @classmethod
    def load(cls, db) -> "Detector | None":
        d = db.get("ai_detector")
        if not isinstance(d, dict) or not d.get("logp") or int(d.get("v") or 1) < 2:
            return None                       # 22.3 以前的版本学的是“谁在说话”，不要了，等重训
        try:
            return cls.from_json(d)
        except Exception:  # noqa: BLE001
            return None


def training_data(db, days: float = 60) -> tuple[list[str], list[str]]:
    import time
    since = time.time() - days * 86400
    human = [r["content"] for r in db.conn.execute(
        "SELECT content FROM qq_group_messages WHERE role='user' AND ts>? AND length(content)>=2 ORDER BY id DESC LIMIT 6000",
        (since,)).fetchall() if not r["content"].startswith("[")]
    ai = [r["content"] for r in db.conn.execute(
        "SELECT content FROM qq_group_messages WHERE role='assistant' AND ts>? AND length(content)>=2 ORDER BY id DESC "
        "LIMIT 6000", (since,)).fetchall() if not r["content"].startswith("[")]
    try:                                                              # 22.3 你打过分的她的话留着验证，不进训练
        scored = set()
        for r in db.conn.execute("SELECT said FROM qq_human_score"):
            scored.update(x.strip() for x in str(r["said"] or "").split(" / ") if x.strip())
        ai = [x for x in ai if x not in scored]
    except Exception:  # noqa: BLE001
        pass
    try:                                                              # 你在小屋里二选一时没选的、或者说“都不好”的
        for r in db.conn.execute("SELECT a, b, choice FROM qq_pairs"):
            a, b = json.loads(r["a"]), json.loads(r["b"])
            lose = {0: b, 1: a, -1: a + b}.get(r["choice"], [])
            ai += [x for x in lose if isinstance(x, str)]
    except Exception:  # noqa: BLE001
        pass
    try:                                                              # 被质检拦下的候选：更典型的 AI 腔
        for r in db.conn.execute("SELECT plan FROM qq_trace WHERE outcome='blocked' AND plan!='' AND ts>?", (since,)):
            for c in (json.loads(r["plan"]).get("cands") or []):
                ai += [x for x in c if isinstance(x, str)]
    except Exception:  # noqa: BLE001
        pass
    return ai, human


MAX_RHO = -0.2


def human_check(db, det: "Detector") -> tuple[float | None, int]:
    """拿你打过分的她的话验证：像 AI 的概率和你打的分的 Spearman（应该是负的）。"""
    from aigroup.eval.calib import spearman
    try:
        rows = db.conn.execute("SELECT said, score FROM qq_human_score").fetchall()
    except Exception:  # noqa: BLE001
        return None, 0
    ps, hs = [], []
    for r in rows:
        parts = [x for x in str(r["said"] or "").split(" / ") if x.strip()]
        if parts:
            ps.append(max(det.prob(x) for x in parts))
            hs.append(float(r["score"]))
    return (spearman(ps, hs) if len(ps) >= 3 else None), len(ps)


def train_from_db(db, mask: list[str] | None = None) -> "Detector | None":
    """mask：要换成占位符的词（群友的名字 / 外号、她的名字和口头禅）。"""
    import random
    ai, human = training_data(db)
    if len(ai) < MIN_EACH or len(human) < MIN_EACH:
        db.set("ai_detector_info", {"ai": len(ai), "human": len(human), "used": False, "why": f"两边各要 {MIN_EACH} 条"})
        return None
    rnd = random.Random(7)
    ai, human = list(ai), list(human)
    rnd.shuffle(ai)
    rnd.shuffle(human)
    k_ai, k_h = len(ai) // 5, len(human) // 5
    det = Detector.train(ai[k_ai:], human[k_h:], mask=mask)
    sa = [det.score(t) for t in ai[:k_ai]]
    sh = [det.score(t) for t in human[:k_h]]
    a_u = auc(sa, sh)
    cal = platt(sa + sh, [1] * len(sa) + [0] * len(sh))
    full = Detector.train(ai, human, mask=mask)
    full.cal = cal
    ps = sorted(full.prob(t) for t in ai[:200])
    acc = (sum(_sig(cal[0] * s + cal[1]) > 0.5 for s in sa) + sum(_sig(cal[0] * s + cal[1]) <= 0.5 for s in sh)) / max(1, k_ai + k_h)
    rho, n_s = human_check(db, full)
    used = a_u is not None and a_u >= MIN_AUC and (n_s < 30 or (rho is not None and rho <= MAX_RHO))
    db.set("ai_detector_info", {"v": 2, "auc": a_u, "acc": round(acc, 3), "ai": len(ai), "human": len(human), "used": used,
                                "rho_vs_you": rho, "n_scored": n_s,
                                "her_p": [round(ps[int(q * (len(ps) - 1))], 2) for q in (0.1, 0.5, 0.9)] if ps else [],
                                "why": "" if used else (f"留出 AUC {a_u} < {MIN_AUC}" if not a_u or a_u < MIN_AUC else
                                                        f"和你打的分相关 {rho}（要 ≤ {MAX_RHO}）：它判的“像 AI”和你觉得好不好对不上")})
    if not used:
        db.set("ai_detector", None)
        return None
    db.set("ai_detector", full.to_json())
    return full
