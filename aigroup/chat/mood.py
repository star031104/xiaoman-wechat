"""持续的心情（2.2）：参考 Sentipolis（2026）的 PAD 情绪状态和 ai-life 的 emotion.py。

每个群一份（她在不同群的处境不一样）：
- valence 心情好坏（-1~1）、arousal 兴奋 / 有精神（-1~1）
- 每次读的时候按半衰期 120 分钟往基线衰减（基线：valence 0.2，arousal 由钟点决定，深夜低）
- 事件（纯规则，不调大模型）：被夸 +v；被怼 / 被骂 -v、+a；她说完没人接 -v；群里很热闹 +a；有人直接找她 +v（小）
- 注入时只给一句话（“刚被阿杰怼了一句，有点不服”），不给数字——Sentipolis 发现直接喂数字没用；
  最近一个显著事件会记下“因为什么”，免得“被怼完几分钟就忘了”
"""
from __future__ import annotations

import math
import re
import time
from datetime import datetime

HALF_MIN = 120.0
PRAISE = re.compile(r"可爱|厉害|好看|牛[逼啤批]?|真棒|好强|太强|喜欢你|爱了|笑死我了|哈哈哈哈|绝了|天才|好家伙.{0,4}(可以|厉害)|谢谢小满|谢了")
# 只算冲着她说的（“你好菜”“小满你是笨蛋”），不按单字（“你吃什么菜”不算被怼）
TEASE = re.compile(r"(你|小满)(真|太|好|也太|是个|就是个|这个|这么|个)?(菜|笨|蠢|傻|丑|垃圾|废物)(?![单谱市品刀])|菜鸡|笨蛋|傻[子瓜逼]|闭嘴|滚(蛋|开|吧)?$|"
                   r"别说话|就你话多|你懂个|一边去|烦死(你|了你)")


def clamp(x: float) -> float:
    return max(-1.0, min(1.0, x))


class Mood:
    def __init__(self, db, tz=None):
        self.db, self.tz = db, tz

    def _base(self, now: float) -> tuple[float, float]:
        h = datetime.fromtimestamp(now, self.tz).hour
        a = -0.5 if h < 7 or h >= 24 else -0.2 if h < 9 or h >= 23 else 0.1 if 11 <= h <= 13 or 19 <= h <= 22 else 0.0
        return 0.2, a

    def get(self, gid: int, now: float | None = None) -> dict:
        now = now or time.time()
        st = self.db.get(f"qq_mood_{gid}") or {}
        bv, ba = self._base(now)
        v, a, ts = float(st.get("v", bv)), float(st.get("a", ba)), float(st.get("ts", now))
        k = 0.5 ** (max(0.0, now - ts) / 60 / HALF_MIN)
        return {"v": bv + (v - bv) * k, "a": ba + (a - ba) * k, "ts": now,
                "why": st.get("why", "") if now - float(st.get("why_ts", 0)) < 90 * 60 else "", "why_ts": st.get("why_ts", 0)}

    def bump(self, gid: int, dv: float = 0.0, da: float = 0.0, why: str = "", now: float | None = None):
        now = now or time.time()
        m = self.get(gid, now)
        m["v"], m["a"] = clamp(m["v"] + dv), clamp(m["a"] + da)
        if why and (abs(dv) >= 0.15 or abs(da) >= 0.2):
            m["why"], m["why_ts"] = why[:30], now
        self.db.set(f"qq_mood_{gid}", {k: (round(v, 3) if isinstance(v, float) else v) for k, v in m.items()})

    def on_message(self, gid: int, name: str, text: str, to_her: bool):
        """群友说了一句话：直接冲着她的才影响心情（夸 / 怼）。"""
        if not to_her:
            return
        if TEASE.search(text or ""):
            self.bump(gid, -0.2, +0.25, f"刚被{name}怼了一句")
        elif PRAISE.search(text or ""):
            self.bump(gid, +0.25, +0.1, f"刚被{name}夸了")
        else:
            self.bump(gid, +0.03, +0.05)

    def on_ignored(self, gid: int):
        self.bump(gid, -0.15, -0.1, "刚才说话没人理")

    def on_lively(self, gid: int, n5m: int):
        if n5m >= 15:
            self.bump(gid, 0.0, +0.05)

    def describe(self, gid: int, now: float | None = None) -> str:
        m = self.get(gid, now)
        v, a = m["v"], m["a"]
        if v >= 0.5:
            s = "心情挺好，有点小得意" if a >= 0.2 else "心情不错，懒洋洋的"
        elif v >= 0.05:
            s = "兴致勃勃，想说话" if a >= 0.3 else "平常的心情" if a > -0.3 else "有点困，懒得多说"
        elif v > -0.35:
            s = "有点不服气，想怼回去" if a >= 0.2 else "有点没劲"
        else:
            s = "有点不高兴，想说话也带点情绪" if a >= 0.1 else "有点低落，不太想说话"
        out = f"你的心情：{s}"
        if m["why"]:
            out += f"（{m['why']}）"
        return out + "。别直接说出自己的心情，体现在语气里就行。"

    @staticmethod
    def feature(m: dict) -> float:
        """给门控用：心情好、有精神时更想说（-1~1）。"""
        return round(0.5 * m["v"] + 0.5 * m["a"], 3)


def turn_memory(delta_turns: int) -> float:
    """Time-Varying Turn-Taking（2025）：w(δ)=0.95·sigmoid(10−δ/2)，δ＝她上次说话以后群友又说了几条。
    刚说过话时她更可能接着说（正在聊），隔得越久越接近“没在聊”；比指数衰减慢。"""
    return round(0.95 / (1 + math.exp(-(10 - delta_turns / 2))), 4)
