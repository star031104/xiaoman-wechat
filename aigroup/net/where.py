"""4.4 群友在哪、那边天气：有人说“在珠海出差”“到成都了”“回老家了”，她记下这个人现在在哪个城市（多久），
提示词里写上那边的天气；他在外地、那边要下大雨 / 下雪 / 太热太冷 / 刮大风，她会像朋友那样提醒一句。

- 记位置：整理印象（digest）时顺带写“在|编号|城市|到哪天|出差/旅游/回家/住”（不多花钱）；
  群友自己说“我在 XX 出差 / 到 XX 了”这种也马上记（城市名先查一遍坐标，查不到就不记）
- 两种：住的地方（base：住、上学、工作的城市，一直记着）和这几天在的地方（trip：出差、旅游、回老家，到日子就忘）
- 天气：Open-Meteo（不用 Key），每个城市一小时查一次
- 小屋“她的日子”里能看到、能改、能删
"""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime

import httpx

log = logging.getLogger("aigroup.where")

SCHEMA = """
CREATE TABLE IF NOT EXISTS qq_where (
    user_id INTEGER NOT NULL,
    kind TEXT NOT NULL,              -- base 住的地方 / trip 这几天在的地方
    city TEXT NOT NULL,
    lat REAL, lon REAL,
    why TEXT NOT NULL DEFAULT '',    -- 出差 / 旅游 / 回老家 / 住 / 上学……
    since REAL NOT NULL,
    until REAL NOT NULL DEFAULT 0,   -- trip 到这个时间就不算了；0 = 一直
    src TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (user_id, kind)
);
"""

WMO = {0: "晴", 1: "晴间多云", 2: "多云", 3: "阴", 45: "有雾", 48: "有雾", 51: "毛毛雨", 53: "毛毛雨", 55: "毛毛雨",
       56: "冻雨", 57: "冻雨", 61: "小雨", 63: "中雨", 65: "大雨", 66: "冻雨", 67: "冻雨", 71: "小雪", 73: "中雪", 75: "大雪",
       77: "雪粒", 80: "阵雨", 81: "阵雨", 82: "暴雨", 85: "阵雪", 86: "大阵雪", 95: "雷雨", 96: "雷雨伴冰雹", 99: "强雷雨伴冰雹"}
BAD = {65: "大雨", 67: "冻雨", 75: "大雪", 82: "暴雨", 86: "大雪", 95: "雷雨", 96: "雷雨伴冰雹", 99: "强雷雨伴冰雹"}
BASE_WHY = ("住", "上学", "读书", "实习", "工作", "上班", "定居", "老家")

# 群友自己说的“我在 XX 出差 / 到 XX 了 / 来 XX 玩”
SAY_RE = re.compile(r"(?:我|人)?(?:现在|今天|明天|这周|下周)?(?:在|到|来|去|飞|回)(?:了)?([一-龥]{2,5}?)(?:市)?"
                    r"(出差|旅游|旅行|玩|开会|培训|了|啦|啊|呢|这边|这里|实习|上学|读书|定居)")
NOT_CITY = {"家", "公司", "学校", "宿舍", "路上", "床上", "外面", "楼下", "楼上", "这边", "那边", "门口", "车上", "地铁", "机场",
            "高铁", "火车", "食堂", "图书馆", "教室", "办公室", "医院", "外地", "老家", "南方", "北方", "国外", "这里", "那里", "现场",
            "单位", "工地", "店里", "超市", "商场", "附近", "海边", "山上", "群里", "游戏", "线上", "里面", "外边", "上班", "下班"}


class Where:
    def __init__(self, db, tz):
        self.db, self.tz = db, tz
        db.conn.executescript(SCHEMA)
        db.conn.commit()
        self._geo: dict[str, tuple[float, dict | None]] = {}
        self._wx: dict[str, tuple[float, dict | None]] = {}

    # ---------------- 坐标、天气 ----------------
    async def _get(self, url: str, params: dict) -> dict:
        async with httpx.AsyncClient(timeout=10, headers={"User-Agent": "xiaoman-wechat (friend bot)"}) as c:
            r = await c.get(url, params=params)
            r.raise_for_status()
            return r.json()

    async def geocode(self, city: str) -> dict | None:
        """城市名 → {name, lat, lon, admin}。中文名会同时查“XX”和“XX市”，挑人口最多的那个（避免查到同名的村子）。"""
        city = re.sub(r"\s+", "", city or "")[:20]
        if len(city) < 2 or city in NOT_CITY:
            return None
        hit = self._geo.get(city)
        if hit and time.time() - hit[0] < 7 * 86400:
            return hit[1]
        names = [city]
        if re.search(r"[一-龥]$", city) and not city.endswith(("市", "县", "州", "区", "盟")):
            names.append(city + "市")
        best = None
        for n in names:
            try:
                d = await self._get("https://geocoding-api.open-meteo.com/v1/search",
                                    {"name": n, "count": 10, "language": "zh" if re.search(r"[一-龥]", n) else "en"})
            except Exception as e:  # noqa: BLE001
                log.info("查城市坐标失败（%s）：%s", n, e)
                continue
            for r in d.get("results") or []:
                fc = str(r.get("feature_code") or "")
                pop = int(r.get("population") or 0)
                if not (fc.startswith(("PPLA", "PPLC", "PCL")) or pop >= 100000):
                    continue
                if best is None or pop > best["pop"]:
                    best = {"name": city, "lat": r["latitude"], "lon": r["longitude"], "pop": pop,
                            "admin": r.get("admin1") or r.get("country") or ""}
        self._geo[city] = (time.time(), best)
        return best

    async def weather(self, lat: float, lon: float) -> dict | None:
        key = f"{lat:.2f},{lon:.2f}"
        hit = self._wx.get(key)
        if hit and time.time() - hit[0] < 3600:
            return hit[1]
        try:
            d = await self._get("https://api.open-meteo.com/v1/forecast", {
                "latitude": lat, "longitude": lon, "timezone": "auto", "forecast_days": 2,
                "current": "temperature_2m,weather_code",
                "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,wind_speed_10m_max"})
        except Exception as e:  # noqa: BLE001
            log.info("查天气失败：%s", e)
            self._wx[key] = (time.time() - 3000, None)       # 10 分钟后再试
            return None
        cur, dl = d.get("current") or {}, d.get("daily") or {}

        def day(i):
            code = int((dl.get("weather_code") or [0, 0])[i] or 0)
            return {"code": code, "text": WMO.get(code, "多云"), "max": round((dl.get("temperature_2m_max") or [0, 0])[i]),
                    "min": round((dl.get("temperature_2m_min") or [0, 0])[i]),
                    "rain": int((dl.get("precipitation_probability_max") or [0, 0])[i] or 0),
                    "wind": round((dl.get("wind_speed_10m_max") or [0, 0])[i] or 0)}
        w = {"now": f"{WMO.get(int(cur.get('weather_code') or 0), '多云')} {round(cur.get('temperature_2m') or 0)}°C",
             "today": day(0), "tomorrow": day(1), "ts": time.time()}
        self._wx[key] = (time.time(), w)
        return w

    @staticmethod
    def alert(d: dict) -> str:
        """这一天值不值得提醒一句：大雨暴雨、雷雨、大雪、冻雨、36 度以上、零下 5 度以下、大风。"""
        if not d:
            return ""
        if d["code"] in BAD:
            return BAD[d["code"]]
        if d["code"] in (61, 63, 80, 81) and d["rain"] >= 80:
            return "雨（降水概率 %d%%）" % d["rain"]
        if d["max"] >= 36:
            return f"高温 {d['max']}°C"
        if d["min"] <= -5:
            return f"很冷（最低 {d['min']}°C）"
        if d["wind"] >= 50:
            return f"大风（{d['wind']} km/h）"
        return ""

    @staticmethod
    def brief(w: dict | None) -> str:
        if not w:
            return ""
        t, m = w["today"], w["tomorrow"]
        return f"现在{w['now']}；今天{t['text']} {t['min']}~{t['max']}°C；明天{m['text']} {m['min']}~{m['max']}°C"

    # ---------------- 记位置 ----------------
    async def set(self, uid: int, city: str, why: str = "", until: str | float = "", src: str = "") -> dict | None:
        g = await self.geocode(city)
        if not g:
            return None
        base = any(w in (why or "") for w in BASE_WHY) and "回" not in (why or "")
        kind = "base" if base else "trip"
        now = time.time()
        if isinstance(until, (int, float)) and until:
            end = float(until)
        else:
            end = 0.0
            m = re.match(r"(\d{4}-\d{1,2}-\d{1,2})", str(until or ""))
            if m:
                try:
                    end = datetime.strptime(m.group(1), "%Y-%m-%d").replace(tzinfo=self.tz).timestamp() + 86400
                except ValueError:
                    end = 0.0
            if kind == "trip" and not end:
                end = now + 5 * 86400                     # 出差旅游没说多久：记 5 天
        if kind == "base":
            end = 0.0
        self.db.conn.execute(
            "INSERT INTO qq_where(user_id, kind, city, lat, lon, why, since, until, src) VALUES (?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(user_id, kind) DO UPDATE SET city=excluded.city, lat=excluded.lat, lon=excluded.lon, "
            "why=excluded.why, since=excluded.since, until=excluded.until, src=excluded.src",
            (int(uid), kind, g["name"], g["lat"], g["lon"], (why or "")[:8], now, end, src))
        self.db.conn.commit()
        log.info("记下了 %s 在 %s（%s）", uid, g["name"], why or kind)
        return {"kind": kind, **g}

    def delete(self, uid: int, kind: str | None = None):
        if kind:
            self.db.conn.execute("DELETE FROM qq_where WHERE user_id=? AND kind=?", (int(uid), kind))
        else:
            self.db.conn.execute("DELETE FROM qq_where WHERE user_id=?", (int(uid),))
        self.db.conn.commit()

    def get(self, uid: int) -> dict | None:
        """他现在在哪：在外地（trip，还没到日子）优先，否则住的地方。"""
        now = time.time()
        rows = {r["kind"]: dict(r) for r in self.db.conn.execute(
            "SELECT * FROM qq_where WHERE user_id=?", (int(uid),)).fetchall()}
        t = rows.get("trip")
        if t and (not t["until"] or t["until"] > now):
            return {**t, "base": rows.get("base")}
        return rows.get("base")

    def all(self) -> list[dict]:
        now = time.time()
        return [dict(r) for r in self.db.conn.execute(
            "SELECT * FROM qq_where WHERE kind='base' OR until=0 OR until>? ORDER BY since DESC", (now,)).fetchall()]

    def note(self, uid: int, he: str = "他") -> str:
        """写进“群里的人”那一行的：在哪、那边天气（只用缓存，不现查）。"""
        p = self.get(uid)
        if not p:
            return ""
        w = self._wx.get(f"{p['lat']:.2f},{p['lon']:.2f}")
        wx = self.brief(w[1]) if w and w[1] else ""
        where = (f"{he}这几天在{p['city']}{p['why'] or ''}" if p["kind"] == "trip" else f"{he}在{p['city']}{p['why'] or '住'}")
        return f"（{where}" + (f"，那边{wx}" if wx else "") + "）"

    async def prefetch(self, uids: list[int]):
        import asyncio
        spots = {(p["lat"], p["lon"]) for p in (self.get(u) for u in uids) if p and p.get("lat") is not None}
        await asyncio.gather(*(self.weather(a, b) for a, b in spots), return_exceptions=True)   # 29.0 同时查，同一个地方只查一次


def said_where(text: str) -> tuple[str, str] | None:
    """群友自己说“我在珠海出差”“到成都了”“回老家了”：返回（城市, 为什么）。"""
    m = SAY_RE.search(text or "")
    if not m:
        return None
    city, why = m.group(1), m.group(2)
    if city in NOT_CITY or any(x in city for x in ("这", "那", "哪", "什么", "一下", "家里", "公司")):
        return None
    if why in ("了", "啦", "啊", "呢", "这边", "这里"):
        why = "回家" if "回" in text[m.start():m.start() + 3] else ""
    return city, why
