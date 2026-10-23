"""4.4 群友在哪、那边天气（假的坐标和天气接口）。"""
import asyncio
import time

from conftest import FRIEND, GROUP, _gc, _msg, _run


def _fake_get(code_tomorrow=65):
    async def get(self, url, params):
        if "geocoding" in url:
            n = params["name"]
            if n == "珠海":
                return {"results": [{"latitude": 35.8, "longitude": 119.9, "feature_code": "PPL", "admin1": "山东"}]}
            if n == "珠海市":
                return {"results": [{"latitude": 22.27, "longitude": 113.57, "feature_code": "PPLA2", "population": 2207090,
                                     "admin1": "广东"}]}
            return {"results": []}
        return {"current": {"temperature_2m": 26, "weather_code": 2},
                "daily": {"weather_code": [2, code_tomorrow], "temperature_2m_max": [28, 25], "temperature_2m_min": [22, 21],
                          "precipitation_probability_max": [10, 90], "wind_speed_10m_max": [10, 20]}}
    return get


def test_geocode_picks_the_city(cfg, brain, monkeypatch):
    from aigroup.net.where import Where
    monkeypatch.setattr(Where, "_get", _fake_get())
    gc, ob = _gc(cfg, brain)
    g = asyncio.run(gc.where.geocode("珠海"))
    assert g["admin"] == "广东" and round(g["lat"]) == 22                    # 不是山东那个同名的村
    assert asyncio.run(gc.where.geocode("公司")) is None


def test_said_where_weather_note_and_care(cfg, brain, monkeypatch):
    from aigroup.net.where import Where
    monkeypatch.setattr(Where, "_get", _fake_get())
    gc, ob = _gc(cfg, brain, reply="珠海明天大雨，记得带伞")
    _run(gc, _msg(FRIEND, "小王", "我在珠海出差，酒店好贵", 1))
    p = gc.where.get(FRIEND)
    assert p and p["city"] == "珠海" and p["kind"] == "trip" and p["why"] == "出差"
    msgs = asyncio.run(gc.build_messages(GROUP, False, False, "小王", focus=[FRIEND]))
    assert "这几天在珠海出差" in msgs[0]["content"] and "明天大雨" in msgs[0]["content"]
    monkeypatch.setattr(brain, "now", lambda: brain.__class__.now(brain).replace(hour=20))
    st = gc._st(GROUP)
    assert asyncio.run(gc.weather_tick(GROUP, st)) is True
    assert "那边明天大雨" in brain.llm.group_calls[-1][-1]["content"]
    gc.db.set(f"qq_wxcheck_{GROUP}", 0)
    assert asyncio.run(gc.weather_tick(GROUP, st)) is False                  # 一天一次
    assert gc.where_list()[0]["city"] == "珠海"


def test_where_from_digest_and_base(cfg, brain, monkeypatch):
    from aigroup.net.where import Where
    monkeypatch.setattr(Where, "_get", _fake_get(code_tomorrow=2))
    gc, ob = _gc(cfg, brain)
    _run(gc, _msg(FRIEND, "小王", "hi", 1))

    async def go():
        gc.digest_extra_apply(GROUP, [f"在|{FRIEND}|珠海|-|住"], [FRIEND])
        for _ in range(20):
            await asyncio.sleep(0.01)
    asyncio.run(go())
    p = gc.where.get(FRIEND)
    assert p and p["kind"] == "base" and p["until"] == 0
    asyncio.run(gc.where.set(FRIEND, "珠海", "旅游", time.time() - 10))       # 过期的旅行不算
    assert gc.where.get(FRIEND)["kind"] == "base"
    gc.where.delete(FRIEND)
    assert gc.where.get(FRIEND) is None
