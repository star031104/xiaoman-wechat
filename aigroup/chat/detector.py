"""AI 腔小分类器（3.0）：分辨“像群友打的字”还是“像大模型写的”。

依据：
- Computational Turing Test（2025）：用分类器在几条候选里挑“最不像 AI 的”收益最大
- 中文 AI 文本检测（2025-09）：短文本上 FastText 这种字 n-gram 线性模型（83.5%）比 BERT、RoBERTa 还准
- C-ReD（2026）：现成检测器对 DeepSeek 的输出接近随机，必须用自己的数据训

这里不装 fasttext，用纯 Python 的字 1~3 元组多项式朴素贝叶斯（同类模型，几千条几百毫秒就训完）：
- 负例（人）：这个群群友自己打的字
- 正例（AI）：她（大模型）发出去的话、被质检拦下的候选
每晚攒够各 150 条就重训一次，模型存在 kv 里；quality.check 里概率 > 0.7 才扣分。
"""
from __future__ import annotations

import json
import math
import re
from collections import Counter

MIN_EACH = 150
MAX_VOCAB = 20000


def grams(text: str) -> list[str]:
    t = re.sub(r"\[[^\]]{0,40}\]|@\S+|https?://\S+", " ", str(text or "")).strip()
    t = re.sub(r"\s+", " ", t)
    out = []
    for n in (1, 2, 3):
        out += [t[i:i + n] for i in range(len(t) - n + 1)]
    out.append(f"<len{min(6, len(t) // 10)}>")                      # 长度也是很强的信号：群友的话大多很短
    return out


class Detector:
    def __init__(self, logp: dict, prior: float, default: tuple[float, float], n: tuple[int, int]):
        self.logp, self.prior, self.default, self.n = logp, prior, default, n

    def prob(self, text: str) -> float:
        """像 AI 的概率（0~1）。"""
        z = self.prior
        for g in grams(text):
            a, h = self.logp.get(g, self.default)
            z += a - h
        z = max(-30.0, min(30.0, z))
        return 1 / (1 + math.exp(-z))

    @classmethod
    def train(cls, ai: list[str], human: list[str], alpha: float = 0.5) -> "Detector":
        ca, ch = Counter(), Counter()
        for t in ai:
            ca.update(grams(t))
        for t in human:
            ch.update(grams(t))
        vocab = [g for g, _ in (ca + ch).most_common(MAX_VOCAB)]
        ta = sum(ca[g] for g in vocab) + alpha * (len(vocab) + 1)
        th = sum(ch[g] for g in vocab) + alpha * (len(vocab) + 1)
        logp = {g: (round(math.log((ca[g] + alpha) / ta), 4), round(math.log((ch[g] + alpha) / th), 4)) for g in vocab}
        default = (math.log(alpha / ta), math.log(alpha / th))
        return cls(logp, 0.0, default, (len(ai), len(human)))      # 先验取 0：两边一样多（不让“她说得少”影响判断）

    def to_json(self) -> dict:
        return {"logp": self.logp, "prior": self.prior, "default": list(self.default), "n": list(self.n)}

    @classmethod
    def from_json(cls, d: dict) -> "Detector":
        return cls({k: tuple(v) for k, v in d["logp"].items()}, float(d["prior"]), tuple(d["default"]), tuple(d["n"]))

    @classmethod
    def load(cls, db) -> "Detector | None":
        d = db.get("ai_detector")
        if not isinstance(d, dict) or not d.get("logp"):
            return None
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


def train_from_db(db) -> "Detector | None":
    ai, human = training_data(db)
    if len(ai) < MIN_EACH or len(human) < MIN_EACH:
        return None
    # 留 20% 看准不准：准确率 < 0.65 就不用（数据还太少或太像）
    k_ai, k_h = len(ai) // 5, len(human) // 5
    det = Detector.train(ai[k_ai:], human[k_h:])
    ok = sum(det.prob(t) > 0.5 for t in ai[:k_ai]) + sum(det.prob(t) <= 0.5 for t in human[:k_h])
    acc = ok / max(1, k_ai + k_h)
    full = Detector.train(ai, human)
    db.set("ai_detector_info", {"acc": round(acc, 3), "ai": len(ai), "human": len(human), "used": acc >= 0.65})
    if acc < 0.65:
        db.set("ai_detector", None)
        return None
    db.set("ai_detector", full.to_json())
    return full
