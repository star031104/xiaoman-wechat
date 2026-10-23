"""空闲和她自己的生活：冒泡、早上补回、戳回去、此刻在干嘛、兴趣、每天的安排。"""
import asyncio
import time

from conftest import (
    FRIEND,
    FRIEND2,
    GROUP,
    OWNER,
    FakeLLM,
    _gc,
    _msg,
    _run,
)


def test_idle_bubble(cfg, brain, monkeypatch):
    gc, ob = _gc(cfg, brain, share_per_day=0)        # 10~23 点会先走“分享”：这里只测冒泡
    old = time.time() - 5 * 3600
    for uid, name, text in ((OWNER, "阿杰", "周末去不去看展"), (FRIEND, "小王", "去！")):
        brain.db.conn.execute("INSERT INTO qq_group_messages(ts, group_id, user_id, name, role, content) "
                              "VALUES (?,?,?,?,?,?)", (old, GROUP, uid, name, "user", text))
    brain.db.conn.commit()
    import aigroup.chat.group as gm
    monkeypatch.setattr(gm.random, "random", lambda: 0.0)
    asyncio.run(gc.idle_tick(60))
    assert ob.sent and "安静了" in brain.llm.group_calls[-1][1]["content"]
    n = len(ob.sent)
    asyncio.run(gc.idle_tick(60))
    assert len(ob.sent) == n


def test_night_catch_up(cfg, brain):
    gc, ob = _gc(cfg, brain)
    cfg["quiet_hours"] = "00:00-23:59"
    _run(gc, _msg(FRIEND, "小王", "小满在吗", 8))
    _run(gc, _msg(FRIEND2, "阿杰", "小满明天去不去", 9))
    assert not ob.sent and [x["who"] for x in brain.db.get(f"qq_night_{GROUP}")] == ["小王", "阿杰"]
    cfg["quiet_hours"] = ""
    asyncio.run(gc.idle_tick(1))
    assert len(ob.sent) == 1 and "才看到" in brain.llm.group_calls[-1][1]["content"]
    assert "小王、阿杰" in brain.llm.group_calls[-1][1]["content"] and not brain.db.get(f"qq_night_{GROUP}")


def test_poke_back(cfg, brain):
    gc, ob = _gc(cfg, brain, poke_back_chance=1.0, read_delay=[0, 0])
    _run(gc, {"post_type": "notice", "notice_type": "notify", "sub_type": "poke", "group_id": GROUP,
              "user_id": FRIEND, "target_id": 10001})
    assert any(a == "group_poke" for a, _ in ob.calls)


def test_her_activity_and_busy(cfg, brain):
    gc, ob = _gc(cfg, brain)
    now = brain.now().replace(hour=10, minute=0)
    asyncio.run(brain.plan_life(now))
    act = gc.her_activity(now)
    assert "素描课" in act and gc.busy(act)
    assert gc.her_activity(now.replace(hour=12, minute=30)) == ""


def test_interests_notes_and_self(cfg, brain):
    gc, ob = _gc(cfg, brain)
    asyncio.run(gc.learn_interests())
    assert "水彩" in gc.interests()
    brain.db.conn.execute("INSERT INTO qq_group_messages(ts, group_id, user_id, name, role, content) VALUES (?,?,?,?,?,?)",
                          (time.time(), GROUP, FRIEND, "小王", "user", "周六去看展"))
    brain.db.conn.execute("INSERT INTO qq_people(user_id, name, msgs) VALUES (?,?,1)", (FRIEND, "小王"))
    brain.db.conn.commit()
    asyncio.run(gc.digest())
    assert "看展" in brain.db.get(f"qq_group_notes_{GROUP}") and "芝麻" in brain.db.get(f"qq_self_notes_{GROUP}")
    note = brain.db.conn.execute("SELECT note FROM qq_people WHERE user_id=?", (FRIEND,)).fetchone()["note"]
    assert "赶论文" in note
    sys = asyncio.run(gc.build_messages(GROUP, False, False, ""))[0]["content"]
    assert "开摆" in sys and "芝麻" in sys


def test_plan_life_rejects_garbage(cfg, brain):
    class LifeLLM(FakeLLM):
        n = 0

        async def chat(self, messages, **kw):
            self.n += 1
            if self.n == 1:
                return "09:30-11:00 高数课\n中午跟室友拼饭+v我吉首的米酒\n下午画海报"
            return "09:30-11:00 高数课，坐第三排\n12:00-13:00 和室友拼饭\n14:00-17:00 图书馆画海报"
    brain.llm = LifeLLM()
    day = brain.now()
    brain.db.set(f"life_{day:%Y-%m-%d}", "09:30-11:00 高数课\n中午乱写的")         # 缓存里是坏的：重排
    text = asyncio.run(brain.plan_life(day))
    assert brain.llm.n == 2 and "图书馆" in text and brain.valid_life(text)


def test_plan_life_gives_up_for_the_day(cfg, brain):
    class Bad(FakeLLM):
        n = 0

        async def chat(self, messages, **kw):
            self.n += 1
            return "随便写写"
    brain.llm = Bad()
    day = brain.now()
    asyncio.run(brain.plan_life(day))
    asyncio.run(brain.plan_life(day))
    assert brain.llm.n == 3                                         # 第二次不再重试
