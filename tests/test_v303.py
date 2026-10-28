"""30.3：每天的安排别天天一样（前几天写过的不再写，群里真定下的事照着写、不猜）；同一件事三天内问过就别再记约定。"""
import asyncio
import time
from datetime import timedelta

from conftest import FRIEND, GROUP, _gc


def test_life_repeats_and_banned(cfg, brain):
    prev = ["09:00-10:30 翻了那家烤鸭的评价，排队不短，圣诞去之前得提前排号"]
    rep = brain.life_repeats("12:00-13:00 又翻了那家烤鸭的评价，排队不短\n15:00-16:00 研究了一道新菜", prev)
    assert len(rep) == 1 and "烤鸭" in rep[0]
    assert brain.hits_banned("12:00-13:00 去看那家烤鸭", ["那家烤鸭排队"])
    assert brain.hits_banned("10:00-11:00 又看了 11-08 的赛程", ["11-08 撞时间"])
    assert not brain.hits_banned("10:00-11:00 刷了会儿新番", ["那家烤鸭排队"])


def test_plan_life_avoids_repeats(cfg, brain):
    day = brain.now()
    old = "\n".join(["09:00-10:30 翻了那家烤鸭的评价，排队不短", "10:30-12:00 琢磨 11-08 单打混双会不会撞",
                     "15:00-16:30 某某那句快好了还挂着"])
    for i in (1, 2):
        brain.db.set(f"life_{(day - timedelta(days=i)):%Y-%m-%d}", old)
    outs = [old, "\n".join(["09:00-10:30 刷了会儿新番第三集", "13:00-14:00 研究了一家粤菜馆的菜单", "21:00-22:30 看了场无畏契约"])]
    seen = []

    async def chat(msgs, **kw):
        seen.append(msgs[-1]["content"])
        return outs[min(len(seen) - 1, 1)]

    async def js(msgs, default=None, role="helper"):
        return {"items": ["那家烤鸭排队", "11-08 撞时间"]}
    brain.llm.chat, brain.llm.json = chat, js
    brain.db.set(f"life_{day:%Y-%m-%d}", None)
    text = asyncio.run(brain.plan_life(day))
    assert "烤鸭" not in text and "新番" in text
    assert "今天不许出现" in seen[0] and "前几天你已经写过的" in seen[0] and "烤鸭" in seen[1]


def test_known_events_and_plan_dedupe(cfg, brain):
    gc, _ = _gc(cfg, brain)
    when = (brain.now() + timedelta(days=2)).strftime("%Y-%m-%d") + " 12:00"
    assert gc.add_plan(GROUP, FRIEND, "小王", "打球，单打十二点、双打六点", when)
    assert "单打十二点" in brain.known_events(brain.now())
    pid = gc.add_plan(GROUP, FRIEND, "小王", "面试", brain.now().strftime("%Y-%m-%d") + " 23:59")
    assert pid
    gc.db.conn.execute("UPDATE qq_plans SET status='done', fired=? WHERE id=?", (time.time(), pid))
    tomorrow = (brain.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    assert gc.add_plan(GROUP, FRIEND, "小王", "面试", tomorrow) == 0          # 刚问过：不再记一遍


def test_plan_skips_closed_topic(cfg, brain):
    gc, _ = _gc(cfg, brain)
    gc._record(GROUP, FRIEND, "小王", "别提面试了好吗", 1)
    tomorrow = (brain.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    assert gc.add_plan(GROUP, FRIEND, "小王", "面试结果", tomorrow) == 0
