"""调度用的小工具：时间段、安静时段、主动概率、节日。"""
from __future__ import annotations

import math
from datetime import datetime, timedelta  # noqa: F401


def parse_range(s: str | None, day: datetime) -> tuple[datetime, datetime] | None:
    """'08:00-09:30' -> (当天 08:00, 当天 09:30)；跨午夜的结束时间顺延到第二天。"""
    if not s or "-" not in s:
        return None
    a, b = [x.strip() for x in s.split("-", 1)]
    ha, ma = map(int, a.split(":"))
    hb, mb = map(int, b.split(":"))
    start = day.replace(hour=ha, minute=ma, second=0, microsecond=0)
    end = day.replace(hour=hb, minute=mb, second=0, microsecond=0)
    if end <= start:
        end += timedelta(days=1)
    return start, end


def in_quiet(now: datetime, quiet: str | None) -> bool:
    if not quiet:
        return False
    for base in (now, now - timedelta(days=1)):
        r = parse_range(quiet, base)
        if r and r[0] <= now < r[1]:
            return True
    return False


def fire_probability(cfg_p, hours_since_user: float, minutes_step: float, factor: float = 1.0) -> float:
    """这一轮检查里随机主动的概率：基础频率 + 越久没聊越想；factor＝想念值 × 钟点习惯。"""
    base_rate = float(cfg_p.base_per_day) / 16.0          # 按清醒 16 小时摊开，每小时期望次数
    longing = float(cfg_p.longing_per_hour) * max(0.0, hours_since_user - 2)
    rate = (base_rate + longing) * max(0.0, factor)       # 每小时期望次数
    return 1 - math.exp(-rate * minutes_step / 60.0)


def hour_factor(proactive_rows, user_times: list[float], hour: int, tz) -> float:
    """他在这个钟点有多容易回：①她主动找他时 30 分钟内回复的比例 ②他自己主动来找她的次数。
    数据少时接近 1，不会乱跳。"""
    n = [0] * 24
    r = [0] * 24
    for row in proactive_rows:
        h = datetime.fromtimestamp(row["ts"], tz).hour
        n[h] += 1
        if row["replied_ts"] and row["replied_ts"] - row["ts"] <= 1800:
            r[h] += 1
    g = (sum(r) + 1) / (sum(n) + 2)
    f1 = ((r[hour] + 2 * g) / (n[hour] + 2)) / g
    starts = [0] * 24
    prev = None
    for t in user_times:
        if prev is None or t - prev > 1800:
            starts[datetime.fromtimestamp(t, tz).hour] += 1
        prev = t
    mean = sum(starts) / 24
    f2 = (starts[hour] + 1) / (mean + 1)
    return max(0.4, min(1.8, math.sqrt(f1 * f2)))


GENERIC_REASONS = ["你突然想到他了", "你在忙自己的事，看到个东西想分享给他",
                   "想问问他今天过得怎么样", "你有点无聊，想找他说说话"]


def festival_today(day) -> str | None:
    """今天的节日 / 节气（农历节日需要 lunar_python，没装就只认公历节日）。"""
    fixed = {"01-01": "元旦", "02-14": "情人节", "03-14": "白色情人节", "04-01": "愚人节", "05-20": "520",
             "06-01": "儿童节", "10-01": "国庆节", "10-31": "万圣节", "11-11": "双十一", "12-24": "平安夜",
             "12-25": "圣诞节", "12-31": "跨年夜"}
    names = []
    if f"{day:%m-%d}" in fixed:
        names.append(fixed[f"{day:%m-%d}"])
    try:
        from lunar_python import Solar
        lu = Solar.fromYmd(day.year, day.month, day.day).getLunar()
        names += [f for f in lu.getFestivals() if f]
        if lu.getJieQi():
            names.append(lu.getJieQi())
        if lu.getMonth() == 12 and lu.getDay() >= 29:
            nxt = Solar.fromYmd(day.year, day.month, day.day).next(1).getLunar()
            if nxt.getMonth() == 1 and nxt.getDay() == 1:
                names.append("除夕")
    except Exception:  # noqa: BLE001
        pass
    return "、".join(dict.fromkeys(names)) or None
