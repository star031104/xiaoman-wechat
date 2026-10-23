"""什么时候开口（1.4）：特征 → 概率 → 按“1 小时发言配额”定阈值。另有按人的注意力、精力、空闲退避。

做法参考 MaiBot（src/maisaka/turn_trigger/ 的 reply_likelihood / dynamic_gate / idle_backoff）和
AstrBot 的 group_chat_plus（注意力半衰期）、Heartflow（精力值）。都是纯计算，不调大模型。

- 概率：logit = 偏置 + Σ 权重 × 特征。前面一组权重是 MaiBot 用它自己的运行日志拟合出来的，后面几项是这里加的；
  每次判断都写进 qq_gate_log（特征、概率、阈值、说没说、说完有没有人接），攒够了可以用自己的数据重新拟合。
- 阈值：最近 1 小时里这个群每一批消息的概率从高到低累加，加到“话痨度 × 总和”那一刻的概率就是阈值
  （话痨度 0.3 ≈ 把最想说的那 30% 的概率留给她）。样本少于 20 条时查表。
- 反馈：这 1 小时里她主动说的次数超过目标（话痨度 × 概率总和）就按比例收紧；只收紧、不放宽，免得一波消息过后说个不停。
- 注意力：她回了谁，谁 +0.4、群里其他人 −0.1，半衰期 5 分钟；正在和她聊的人再开口，她更想接。
- 精力：主动说一次 −0.1（最低 0.1），每 5 分钟回 0.02；说多了自然变懒。
- 空闲退避：大模型连续选择“不说”n 次（n≥2），下一次判断要等 15 秒 × 2^(n−2)，最多 300 秒；攒了 6 条以上新消息就不等。

2.0：
- 样本少时查表值和分位数按 n/20 混合（不再在第 20 条时跳变）；概率全都一样时按比例随机放行（以前会全放行）
- 按时段调话量（talk_curve）
- 每晚用自己的数据（门控日志 + 接话效果）重新拟合权重，攒够 500 条带标签的判断才启用
"""
from __future__ import annotations

import json
import math
import time

from aigroup.core.store import ensure_columns

WEIGHTS = {
    # —— MaiBot 拟合的 ——
    "bias": -1.2005,
    "name": 1.5852,          # 叫了她的名字（被叫到时本来就必回，这里只是记下来）
    "at_other": -1.1157,     # @了别人
    "question": 0.4807,      # 有问号 / 在问大家
    "emoji_only": -1.0281,   # 只有表情、图片
    "self_share": 2.6525,    # 最近 5 分钟里她说的占比（正在聊）
    "n5m": -0.0367,          # 最近 5 分钟的消息数（群里太热闹时别抢）
    "ln_gap": 0.0571,        # ln(1 + 距她上次说话的秒数)
    "batch": 0.1012,         # 这一批几条
    # —— 这里加的 ——
    "reply_other": -0.8,     # 在回复别人的消息
    "owner": 0.6,            # 他（主人）在说话
    "interest": 0.7,         # 聊到她感兴趣的
    "busy": -1.2,            # 她正在上课 / 上班
    "attention": 1.5,        # 说话的人正是她刚在聊的（0~1）
    "tired": -1.0,           # 1 − 精力
    "cold": -0.5,            # 群里冷了很久后的第一句
    "ignored": -0.6,         # 她最近主动说的几句都没人理（被冷落了，0~1）
    "waited": 2.0,           # 她刚才说过“等某人开口再说”，那个人开口了
    "turns": 0.4,            # 2.2：w(δ)，δ＝她上次说话以后群友又说了几条（刚说过更可能接着说）
    "mood": 0.5,             # 2.2：心情好、有精神时更想说（-1~1）
}
# 样本不够时：话痨度 → 阈值（MaiBot 的离线统计表）
COLD_TABLE = [(0.0, 0.95), (0.1, 0.605), (0.3, 0.43), (0.5, 0.336), (0.8, 0.25), (1.0, 0.0)]
WINDOW = 3600
MIN_SAMPLES = 20

SCHEMA = """
CREATE TABLE IF NOT EXISTS qq_gate_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    group_id INTEGER NOT NULL,
    mentioned INTEGER NOT NULL DEFAULT 0,
    p REAL NOT NULL,
    threshold REAL NOT NULL,
    passed INTEGER NOT NULL DEFAULT 0,      -- 过了门槛（会去问大模型）
    spoke INTEGER NOT NULL DEFAULT 0,       -- 真的说了
    llm_skip INTEGER NOT NULL DEFAULT 0,    -- 大模型自己选了不说
    feats TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_qq_gate_log ON qq_gate_log(group_id, ts);
"""


def sigmoid(x: float) -> float:
    if x < -40:
        return 0.0
    if x > 40:
        return 1.0
    return 1.0 / (1.0 + math.exp(-x))


def likelihood(feats: dict, weights: dict | None = None) -> float:
    w = weights or WEIGHTS
    z = w.get("bias", 0.0) + sum(float(w.get(k, 0.0)) * float(v) for k, v in feats.items() if k != "bias")
    return sigmoid(z)


def cold_threshold(talk: float) -> float:
    talk = max(0.0, min(1.0, float(talk)))
    for (a, ta), (b, tb) in zip(COLD_TABLE, COLD_TABLE[1:]):
        if a <= talk <= b:
            return ta + (tb - ta) * (talk - a) / (b - a)
    return COLD_TABLE[-1][1]


def threshold(probs: list[float], talk: float, spoke: int) -> tuple[float, float]:
    """probs：最近 1 小时没被叫到的那些批次的概率；spoke：其中她主动说了几次。返回 (阈值, 实际用的保留比例)。

    样本少（< 20）时不再一刀切查表：按 n/20 把查表值和按样本算的分位数混起来，免得第 20 条时阈值突然跳一大截。"""
    thr, keep, _ = threshold_ex(probs, talk, spoke)
    return thr, keep


def threshold_ex(probs: list[float], talk: float, spoke: int) -> tuple[float, float, float]:
    """同 threshold，多返回一个 tie：概率正好等于阈值的那些批次，有多大比例该放行（全都一样时用得上）。"""
    talk = max(0.0, min(1.0, float(talk)))
    if talk <= 0:
        return 1.01, 0.0, 0.0
    cold = cold_threshold(talk)
    if not probs:
        return cold, talk, 1.0
    total = sum(probs)
    target = talk * total
    keep = talk
    if spoke > target > 0:                      # 说多了：按比例收紧（只收紧）
        keep = talk * target / spoke
    need = keep * total
    acc, thr = 0.0, 0.0
    srt = sorted(probs, reverse=True)
    for p in srt:
        acc += p
        if acc >= need:
            thr = p
            break
    above = sum(p for p in srt if p > thr + 1e-9)
    ties = [p for p in srt if abs(p - thr) <= 1e-9]
    tie = max(0.0, min(1.0, (need - above) / (sum(ties) or 1))) if ties else 1.0
    if len(probs) < MIN_SAMPLES:
        w = len(probs) / MIN_SAMPLES
        return (1 - w) * cold + w * thr, keep, 1.0
    return thr, keep, tie


def passes(p: float, thr: float, tie: float, rand: float) -> bool:
    if p > thr + 1e-9:
        return True
    if abs(p - thr) <= 1e-9:
        return rand < tie
    return False


def talk_factor(rules, hour_min: str) -> float:
    """按时段调话量（MaiBot 的 talk_value_rules）：rules 形如 {"09:00-11:59": 0.8, "18:00-23:59": 1.2}。"""
    if not rules:
        return 1.0
    hm = int(hour_min[:2]) * 60 + int(hour_min[3:5])
    for k, v in dict(rules).items():
        try:
            a, b = str(k).split("-")
            ta = int(a[:2]) * 60 + int(a[3:5])
            tb = int(b[:2]) * 60 + int(b[3:5])
        except (ValueError, IndexError):
            continue
        if (ta <= hm <= tb) if ta <= tb else (hm >= ta or hm <= tb):
            try:
                return max(0.0, float(v))
            except (TypeError, ValueError):
                return 1.0
    return 1.0


# ---------------- 用自己的数据重新拟合（2.0） ----------------
REFIT_MIN = 500


def labeled_rows(db, since: float = 0.0) -> list[tuple[dict, float, float]]:
    """(特征, 标签, 权重)：她主动说了的 → 接话效果 ≥0.6 记 1、否则 0；大模型自己选了不说的 → 0（权重 0.5）。
    质检拦下、解析失败、发送失败的不算（那不是“该不该开口”的问题）。"""
    out = []
    try:
        rows = db.conn.execute(
            "SELECT g.feats, g.spoke, g.llm_skip, e.score, t.outcome FROM qq_gate_log g "
            "LEFT JOIN qq_reply_effect e ON e.gate_id=g.id AND e.done=1 "
            "LEFT JOIN qq_trace t ON t.gate_id=g.id "
            "WHERE g.mentioned=0 AND g.passed=1 AND g.ts>?", (since,)).fetchall()
    except Exception:  # noqa: BLE001        # 还没有 qq_trace 表
        rows = db.conn.execute(
            "SELECT g.feats, g.spoke, g.llm_skip, e.score, NULL outcome FROM qq_gate_log g "
            "LEFT JOIN qq_reply_effect e ON e.gate_id=g.id AND e.done=1 "
            "WHERE g.mentioned=0 AND g.passed=1 AND g.ts>?", (since,)).fetchall()
    for r in rows:
        try:
            f = json.loads(r["feats"] or "{}")
        except ValueError:
            continue
        if r["spoke"] and r["score"] is not None:
            out.append((f, 1.0 if r["score"] >= 0.6 else 0.0, 1.0))
        elif r["llm_skip"] and r["outcome"] in (None, "skip"):
            out.append((f, 0.0, 0.5))
    # 13.0：没过门槛的也能有标签——你在小屋里标的（最准，权重 2）；拿不准时问出来的“不 / 等”（弱标签，权重 0.3）
    try:
        extra = db.conn.execute("SELECT id, feats, human, judge FROM qq_gate_log WHERE mentioned=0 AND ts>? AND "
                                "((human IN (0, 1) AND NOT (human=0 AND IFNULL(human_why,'')='说的不对')) "
                                "OR (passed=0 AND judge IN ('skip', 'wait')))", (since,)).fetchall()
    except Exception:  # noqa: BLE001
        extra = []
    for r in extra:
        try:
            f = json.loads(r["feats"] or "{}")
        except ValueError:
            continue
        if r["human"] in (0, 1):
            out.append((f, float(r["human"]), 2.0))
        else:
            out.append((f, 0.0, 0.3))
    return out


def logloss(rows, w) -> float:
    tot, n = 0.0, 0.0
    for f, y, wt in rows:
        p = min(1 - 1e-6, max(1e-6, likelihood(f, w)))
        tot -= wt * (y * math.log(p) + (1 - y) * math.log(1 - p))
        n += wt
    return tot / (n or 1)


def fit(rows: list[tuple[dict, float, float]], prior: dict | None = None, l2: float = 0.05, steps: int = 600,
        lr: float = 0.3) -> dict:
    """带 L2（往原来的权重拉）的逻辑回归，纯 Python 批量梯度下降。

    2.0.1：先把特征标准化再训练（n5m 能到几十、ln_gap 到 11，不标准化时梯度来回震荡、权重发散），
    训练完再换算回原来的尺度；学习率随步数衰减。"""
    prior = dict(prior or WEIGHTS)
    keys = [k for k in prior if k != "bias"]
    n = sum(wt for _, _, wt in rows) or 1.0
    mu = {k: sum(wt * float(f.get(k, 0.0)) for f, _, wt in rows) / n for k in keys}
    sd = {k: max(1e-3, math.sqrt(sum(wt * (float(f.get(k, 0.0)) - mu[k]) ** 2 for f, _, wt in rows) / n)) for k in keys}
    # 标准化空间里的先验：w' = w·sd，b' = b + Σ w·mu
    pw = {k: prior[k] * sd[k] for k in keys}
    pb = prior.get("bias", 0.0) + sum(prior[k] * mu[k] for k in keys)
    w, b = dict(pw), pb
    xs = [([(float(f.get(k, 0.0)) - mu[k]) / sd[k] for k in keys], y, wt) for f, y, wt in rows]
    for step in range(steps):
        a = lr / (1 + step / 200)
        gb, gw = 0.0, [0.0] * len(keys)
        for x, y, wt in xs:
            z = b + sum(w[k] * xi for k, xi in zip(keys, x))
            g = (sigmoid(z) - y) * wt
            gb += g
            for i, xi in enumerate(x):
                gw[i] += g * xi
        b -= a * (gb / n + l2 * (b - pb))
        for i, k in enumerate(keys):
            w[k] -= a * (gw[i] / n + l2 * (w[k] - pw[k]))
    out = {k: w[k] / sd[k] for k in keys}
    out["bias"] = b - sum(out[k] * mu[k] for k in keys)
    return {k: round(v, 4) for k, v in out.items()}


def active_weights(db) -> tuple[dict, str]:
    """现在用哪套权重：攒够 REFIT_MIN 条带标签的判断、晚上重新拟合过的就用拟合的。"""
    w = db.get("gate_weights")
    if isinstance(w, dict) and w.get("weights") and int(w.get("n", 0)) >= REFIT_MIN:
        return {**WEIGHTS, **w["weights"]}, f"用自己的数据拟合的（{w['n']} 条，{w.get('day', '')}）"
    return WEIGHTS, "MaiBot 的默认权重"


def refit(db, day: str = "", rand=None) -> dict | None:
    """够 REFIT_MIN 条就重新拟合；留 20% 做验证，新权重的 logloss 比现在用的低才启用。"""
    import random as _r
    rows = labeled_rows(db)
    if len(rows) < REFIT_MIN:
        return None
    rows = list(rows)
    (rand or _r.Random(7)).shuffle(rows)
    k = max(1, len(rows) // 5)
    hold, train = rows[:k], rows[k:]
    cur, _ = active_weights(db)
    w = fit(train, prior=cur)
    new_loss, old_loss = logloss(hold, w), logloss(hold, cur)
    info = {"n": len(rows), "day": day, "holdout": len(hold), "new_loss": round(new_loss, 4), "old_loss": round(old_loss, 4)}
    if new_loss >= old_loss:
        db.set("gate_refit_last", {**info, "used": False})
        return None
    db.set("gate_weights", {"weights": fit(rows, prior=cur), **info})
    db.set("gate_refit_last", {**info, "used": True})
    return w


class Attention:
    """每个群里，她对每个人的注意力（0~1，半衰期 half 秒）。"""

    def __init__(self, half: float = 300.0):
        self.half = half
        self.v: dict[int, tuple[float, float]] = {}

    def get(self, uid: int, now: float | None = None) -> float:
        now = now or time.time()
        val, ts = self.v.get(int(uid), (0.0, now))
        return val * 0.5 ** (max(0.0, now - ts) / self.half)

    def talked_to(self, uid: int | None, everyone: list[int], now: float | None = None):
        now = now or time.time()
        for u in set(int(x) for x in everyone or []):
            if uid is None or u != int(uid):
                self.v[u] = (max(0.0, self.get(u, now) - 0.1), now)
        if uid:
            self.v[int(uid)] = (min(1.0, self.get(uid, now) + 0.4), now)

    def top(self, now: float | None = None) -> list[tuple[int, float]]:
        now = now or time.time()
        out = [(u, self.get(u, now)) for u in self.v]
        return sorted([x for x in out if x[1] >= 0.05], key=lambda x: -x[1])


class Energy:
    def __init__(self, cost: float = 0.1, regen_per_5min: float = 0.02):
        self.value, self.ts = 1.0, time.time()
        self.cost, self.regen = cost, regen_per_5min

    def get(self, now: float | None = None) -> float:
        now = now or time.time()
        return min(1.0, self.value + self.regen * max(0.0, now - self.ts) / 300)

    def spend(self, now: float | None = None):
        now = now or time.time()
        self.value, self.ts = max(0.1, self.get(now) - self.cost), now


class Backoff:
    def __init__(self, base: float = 15.0, cap: float = 300.0):
        self.n, self.until, self.base, self.cap = 0, 0.0, base, cap

    def skipped(self, now: float | None = None):
        now = now or time.time()
        self.n += 1
        if self.n >= 2:
            self.until = now + min(self.cap, self.base * 2 ** (self.n - 2))

    def spoke(self):
        self.n, self.until = 0, 0.0

    def waiting(self, now: float | None = None) -> float:
        return max(0.0, self.until - (now or time.time()))


class GateLog:
    """qq_gate_log 的读写。"""

    def __init__(self, db):
        self.db = db
        db.conn.executescript(SCHEMA)
        ensure_columns(db.conn, "qq_gate_log", {
            "kind": "TEXT NOT NULL DEFAULT ''",          # 2.0：大模型判断的类别 I1 / I2 / S1 / S2
            "explore": "INTEGER NOT NULL DEFAULT 0",     # 2.0.1：本该拦下、随机放行的一小部分（重新拟合时纠正选样偏差）
            "judge": "TEXT NOT NULL DEFAULT ''",         # 13.0 拿不准时问的那一个字：say / wait / skip
            "human": "INTEGER",                          # 13.0 你在小屋“标注”里判的：1 该说、0 不该说（最准的标签）
            # 20.0：2 可说可不说（不当标签用）；-1 看不出来；weak：没人标时程序自己推的弱标签（0 / 1），见 label.weak_labels
            "act": "TEXT NOT NULL DEFAULT ''",           # 20.0 拿不准时问出来的动作（接住 / 回答 / 追问 / 纠错 ……）
            "why": "TEXT NOT NULL DEFAULT ''",           # 20.0 这一刻为什么说 / 没说（人话，小屋里给你看）
            "weak": "INTEGER",
            "human_act": "TEXT NOT NULL DEFAULT ''",     # 20.0 你判“该接”时顺手选的：该怎么接
            "ablated": "TEXT NOT NULL DEFAULT ''",       # 20.1 这次随机去掉了提示词里的哪一段（模块消融）
            "drift": "TEXT NOT NULL DEFAULT ''",         # 20.1 这一轮是不是容易漂走的场景（加了锚点）
            "appraise": "TEXT NOT NULL DEFAULT ''",      # 21.0 她开口前的九问自评（JSON）：拿你的标注对每一问
            "human_why": "TEXT NOT NULL DEFAULT ''",     # 21.0 你判“不该插”时选的：时机不对 / 说的不对
            "listener": "TEXT NOT NULL DEFAULT ''",      # 21.1 听者的判断（JSON：window / issues / enforced）
        })
        db.conn.commit()

    def prune(self, days: float = 90):
        self.db.conn.execute("DELETE FROM qq_gate_log WHERE ts<?", (time.time() - days * 86400,))
        self.db.conn.commit()

    def window(self, gid: int, now: float | None = None) -> tuple[list[float], int]:
        now = now or time.time()
        rows = self.db.conn.execute(
            "SELECT p, spoke FROM qq_gate_log WHERE group_id=? AND ts>? AND mentioned=0", (gid, now - WINDOW)).fetchall()
        return [float(r["p"]) for r in rows], sum(int(r["spoke"]) for r in rows)

    def add(self, gid: int, mentioned: bool, p: float, thr: float, passed: bool, feats: dict, explore: bool = False,
            judge: str = "", act: str = "", why: str = "") -> int:
        cur = self.db.conn.execute(
            "INSERT INTO qq_gate_log(ts, group_id, mentioned, p, threshold, passed, feats, explore, judge, act, why) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (time.time(), gid, int(mentioned), round(p, 4), round(thr, 4), int(passed),
             json.dumps({k: round(float(v), 4) for k, v in feats.items()}), int(explore), judge or "", act or "",
             (why or "")[:120]))
        self.db.conn.commit()
        return int(cur.lastrowid)

    def drop(self, gate_id: int | None):
        """这一批没判断完（等着的时候又来了新消息，会和新消息一起重新判断）：删掉，免得重复计数。"""
        if gate_id:
            self.db.conn.execute("DELETE FROM qq_gate_log WHERE id=?", (gate_id,))
            self.db.conn.commit()

    def mark(self, gate_id: int | None, spoke: bool = False, llm_skip: bool = False, kind: str = ""):
        if not gate_id:
            return
        self.db.conn.execute("UPDATE qq_gate_log SET spoke=?, llm_skip=?, kind=CASE WHEN ?!='' THEN ? ELSE kind END "
                             "WHERE id=?", (int(spoke), int(llm_skip), kind, kind, gate_id))
        self.db.conn.commit()
