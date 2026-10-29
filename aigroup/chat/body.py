"""31.0 困不困用公式算（思路来自陪伴插件的睡眠压力，不调模型）。

以前只按钟点：23 点以后一律“有点困”。可她昨晚要是聊到两点、今天八点就醒，下午就该犯困；
昨晚早睡、今天睡到自然醒，十一点也还精神。

    睡眠压力 = 醒了几个小时 × 4 + 昨晚少睡的分钟数 × 0.14
    < 45 精神；45~66 有点困；≥ 66 很困（醒了 16 个半小时左右）

醒 / 睡的时间看她自己真的什么时候在说话：
- 昨晚睡：默认 bed_time（00:30）；昨晚要是 bed_time 以后还在群里说话，就按最后一句算
- 今天起：默认 wake_time（08:00）；今天要是更早就开口了，就按第一句算
- 一天从早上 5 点算（5 点前还算“昨天没睡”）
"""
from __future__ import annotations

from datetime import datetime, timedelta

FULL_SLEEP_MIN = 480
SLEEPY, VERY_SLEEPY = 45.0, 66.0


def sleep_pressure(awake_hours: float, debt_min: float) -> float:
    return max(0.0, awake_hours) * 4 + max(0.0, debt_min) * 0.14


def _at(day: datetime, hhmm: str) -> datetime:
    try:
        h, m = [int(x) for x in str(hhmm).split(":")[:2]]
    except (ValueError, TypeError):
        h, m = 8, 0
    return day.replace(hour=h % 24, minute=m % 60, second=0, microsecond=0)


def sleep_cycle(now: datetime, her_ts: list[float], wake_time: str = "08:00", bed_time: str = "00:30") -> dict:
    """her_ts：她最近两天说话的时间戳。返回 {"wake", "bed", "slept_min", "awake_h", "pressure"}。"""
    tz = now.tzinfo
    day0 = (now - timedelta(hours=5)).replace(hour=5, minute=0, second=0, microsecond=0)   # 这一“天”从早上 5 点开始
    bed_default = _at(day0, bed_time)
    if bed_default.hour >= 12:                                    # 23:30 这种：是前一天晚上
        bed_default -= timedelta(days=1)
    night = [datetime.fromtimestamp(t, tz) for t in her_ts if day0 - timedelta(hours=11) <= datetime.fromtimestamp(t, tz) < day0]
    bed = max([bed_default] + [x for x in night if x > bed_default])
    wake = _at(day0, wake_time)
    morning = [datetime.fromtimestamp(t, tz) for t in her_ts if day0 <= datetime.fromtimestamp(t, tz) <= now]
    if morning and min(morning) < wake:
        wake = min(morning)
    if now < wake:                                                # 被叫醒了（还没到平时起床的点）
        wake = now
    slept = max(0.0, (wake - bed).total_seconds() / 60)
    awake_h = (now - wake).total_seconds() / 3600
    return {"wake": wake, "bed": bed, "slept_min": round(slept), "awake_h": round(awake_h, 2),
            "pressure": round(sleep_pressure(awake_h, FULL_SLEEP_MIN - slept), 1)}


def body_line(now: datetime, cyc: dict) -> str:
    """给提示词的一句（不给数字）：困不困 + 饭点饿不饿。"""
    p, awake = float(cyc.get("pressure") or 0), float(cyc.get("awake_h") or 0)
    bits = []
    if awake < 1.0:
        bits.append("刚起床，还有点懵")
    elif p >= VERY_SLEEPY:
        late = cyc.get("bed") and cyc["bed"].hour < 5 and cyc["bed"].hour >= 1
        bits.append("困得不行，眼皮在打架" + ("（昨晚睡得晚）" if late else "") + "：回得很短、懒得展开，想早点睡")
    elif p >= SLEEPY:
        bits.append("有点困了，回得懒一点、短一点")
    if now.hour in (11, 12, 17, 18) and awake >= 1.0:
        bits.append("有点饿，惦记着吃什么")
    return "；".join(bits) or "平常的状态，心情还不错"
