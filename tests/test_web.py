"""群聊小屋（网页）：接口能用、改了马上生效、闭麦的群不说话。"""
import asyncio
import json

from aiohttp.test_utils import TestClient, TestServer

from aigroup.chat.group import GroupChat
from aigroup.web.server import GroupHome, clean_knob

from conftest import (
    FRIEND,
    FRIEND2,
    GROUP,
    GROUP2,
    OB,
    OWNER,
    LearnLLM,
    ProfileLLM,
    _gc,
    _msg,
    _run,
)


def _setup(cfg, brain):
    brain.llm = ProfileLLM()
    ob = OB()
    gc = GroupChat(cfg, brain, ob, OWNER, [GROUP], ["满满"])
    return gc, ob


def _client(cfg, brain, gc, ob):
    return TestClient(TestServer(GroupHome(cfg, brain, ob, gc).app()))


def test_clean_knob():
    assert clean_knob("talkativeness", "1.9") == 1.0               # 超过上限
    assert clean_knob("interject_chance", 0.3) is None            # 1.4 起没有这个开关了
    assert clean_knob("max_bubbles", 2.6) == 3
    assert clean_knob("see_images", "true") is True
    assert clean_knob("nope", 1) is None and clean_knob("pace", "abc") is None


def test_state_people_and_edits(cfg, brain):
    gc, ob = _setup(cfg, brain)
    brain.llm.reply = "笑死，就这？"
    _run(gc, _msg(FRIEND, "小王", "满满在吗", 1, at=True), _msg(OWNER, "我", "今天好热", 2))

    async def go():
        async with _client(cfg, brain, gc, ob) as c:
            r = await c.get("/")
            assert r.status == 200 and "群聊小屋" in await r.text()
            s = await (await c.get("/api/state")).json()
            g = s["groups"][0]
            assert g["id"] == GROUP and g["today"] >= 3 and g["her_today"] >= 1
            assert s["kpi"]["mentions_today"] >= 1 and s["persona"]["name"] == "小满"
            assert sum(s["hours"]["her"]) == g["her_today"] and len(s["days"]) == 14

            m = await (await c.get(f"/api/messages?gid={GROUP}")).json()
            assert [x["role"] for x in m["messages"]][:2] == ["user", "user"]
            m = await (await c.get(f"/api/messages?gid={GROUP}&q=好热")).json()
            assert len(m["messages"]) == 1
            assert (await c.get("/api/messages?gid=1")).status == 400        # 不是她的群

            p = await (await c.get("/api/people")).json()
            assert {x["user_id"] for x in p["people"]} == {FRIEND, OWNER}
            r = await (await c.post("/api/person/refresh", json={"uid": FRIEND})).json()
            assert r["ok"] and r["person"]["profile"]["称呼"] == "王哥"
            r = await (await c.post("/api/person/update", json={"uid": FRIEND, "profile": {
                "印象": "改过的印象", "记得的事": "养猫\n\n周六答辩"}})).json()
            assert r["ok"]
            x = await (await c.get(f"/api/person?uid={FRIEND}")).json()
            assert x["profile"] == {"印象": "改过的印象", "记得的事": ["养猫", "周六答辩"]} and x["recent"]
            assert gc.person(FRIEND)["note"] == "改过的印象"
            await c.post("/api/person/forget", json={"uid": FRIEND})
            assert gc.person(FRIEND)["profile"] == {}

            await c.post("/api/her/interests", json={"words": "画画，猫、奶茶\n一"})
            assert gc.interests() == ["画画", "奶茶"]          # 一个字的容易误触发，不要
            await c.post("/api/group/notes", json={"gid": GROUP, "text": "- 开摆是口头禅"})
            assert brain.db.get(f"qq_group_notes_{GROUP}") == "- 开摆是口头禅"
            h = await (await c.get("/api/her")).json()
            assert h["persona"]["catchphrases"] and "小满" in h["persona"]["aliases"]

            r = await (await c.post("/api/group/say", json={"gid": GROUP, "text": "我来啦"})).json()
            assert r["ok"] and ob.sent[-1][1][0]["data"]["text"] == "我来啦"
            last = brain.db.conn.execute("SELECT role, content FROM qq_group_messages ORDER BY id DESC LIMIT 1").fetchone()
            assert (last["role"], last["content"]) == ("assistant", "我来啦")
            brain.llm.reply = "周末有人去看展吗"
            r = await (await c.post("/api/group/nudge", json={"gid": GROUP, "topic": "周末"})).json()
            assert r["ok"]
            lg = await (await c.get("/api/logs")).json()
            assert lg["lines"] == []
    asyncio.run(go())


def test_settings_live_and_persist(cfg, brain):
    gc, ob = _setup(cfg, brain)

    async def go():
        async with _client(cfg, brain, gc, ob) as c:
            s = await (await c.get("/api/settings")).json()
            assert any(i["key"] == "talkativeness" for i in s["items"])
            await c.post("/api/settings", json={"values": {"talkativeness": 0.3, "see_images": True, "bad": 1}})
            assert gc.g["talkativeness"] == 0.3 and gc.g["see_images"] is True
            assert brain.db.get("web_overrides") == {"talkativeness": 0.3, "see_images": True}
    asyncio.run(go())
    gc2 = GroupChat(cfg, brain, OB(), OWNER, [GROUP], [])       # 重启以后还在
    assert gc2.g["talkativeness"] == 0.3

    async def go2():
        async with _client(cfg, brain, gc2, ob) as c:
            await c.post("/api/settings", json={"reset": True})
    asyncio.run(go2())
    assert gc2.g["talkativeness"] == cfg["wechat"]["group"]["talkativeness"] and not brain.db.get("web_overrides")


def test_muted_group_stays_quiet(cfg, brain):
    gc, ob = _setup(cfg, brain)

    async def go():
        async with _client(cfg, brain, gc, ob) as c:
            r = await (await c.post("/api/group/mute", json={"gid": GROUP, "muted": True})).json()
            assert r["muted"]
    asyncio.run(go())
    _run(gc, _msg(FRIEND, "小王", "满满在吗", 1, at=True))
    assert not ob.sent                                              # 被 @ 也不说
    assert gc.person(FRIEND)["msgs"] == 1                           # 但消息照样记
    brain.db.set(f"qq_mute_{GROUP}", None)
    _run(gc, _msg(FRIEND, "小王", "满满？", 2, at=True))
    assert ob.sent


def test_only_local(cfg, brain):
    gc, ob = _setup(cfg, brain)

    async def go():
        async with _client(cfg, brain, gc, ob) as c:
            r = await c.get("/api/state", headers={"Host": "evil.com"})
            assert r.status == 403
            r = await c.post("/api/settings", json={}, headers={"Origin": "https://evil.com"})
            assert r.status == 403
    asyncio.run(go())


def test_learned_web(cfg, brain):
    from aiohttp.test_utils import TestClient, TestServer

    from aigroup.web.server import GroupHome
    brain.llm = LearnLLM()
    gc, ob = _gc(cfg, brain)
    db = brain.db.conn
    db.execute("INSERT INTO qq_expressions(group_id, situation, style, count, users) VALUES (?,?,?,?,?)",
               (GROUP, "催人", "速速", 3, json.dumps([FRIEND, FRIEND2])))
    db.execute("INSERT INTO qq_jargon(group_id, term, count, meaning, is_jargon, users) VALUES (?,?,?,?,?,?)",
               (GROUP, "开摆", 9, "躺平", 1, json.dumps([FRIEND, FRIEND2])))
    db.commit()

    async def go():
        async with TestClient(TestServer(GroupHome(cfg, brain, ob, gc).app())) as c:
            r = await (await c.get(f"/api/learned?gid={GROUP}")).json()
            assert r["expressions"][0]["used"] and r["jargon"][0]["used"]
            ex_id = r["expressions"][0]["id"]
            x = await (await c.post("/api/learned/set", json={"kind": "expression", "id": ex_id, "status": "no"})).json()
            assert x["ok"]
            r = await (await c.get(f"/api/learned?gid={GROUP}")).json()
            assert not r["expressions"][0]["used"]
            await c.post("/api/learned/set", json={"kind": "jargon", "id": r["jargon"][0]["id"], "meaning": "不干了"})
            assert db.execute("SELECT meaning FROM qq_jargon").fetchone()[0] == "不干了"
            await c.post("/api/learned/set", json={"kind": "jargon", "id": r["jargon"][0]["id"], "delete": True})
            assert not db.execute("SELECT 1 FROM qq_jargon").fetchone()
            assert (await c.post("/api/learned/set", json={"kind": "bad", "id": 1})).status == 400
            g = await (await c.get(f"/api/gate?gid={GROUP}")).json()
            assert g["threshold"] == 0.43 and g["energy"] == 1.0
            s = await (await c.get("/api/state")).json()
            assert "effect_rate" in s["kpi"]
    asyncio.run(go())


def test_web_learned_extras(cfg, brain):
    from aiohttp.test_utils import TestClient, TestServer

    from aigroup.web.server import GroupHome
    gc, ob = _gc(cfg, brain)
    fid = gc.memory.add_fact(GROUP, "小王周六要答辩", subject=FRIEND)

    async def go():
        async with TestClient(TestServer(GroupHome(cfg, brain, ob, gc).app())) as c:
            r = await (await c.get(f"/api/learned?gid={GROUP}")).json()
            assert r["facts"][0]["text"] == "小王周六要答辩" and r["stickers"] == [] and "daily" in r
            await c.post("/api/learned/fact", json={"id": fid, "expire": True})
            assert not gc.memory.facts(GROUP)
            await c.post("/api/group/norms", json={"gid": GROUP, "text": "- 不刷屏"})
            assert brain.db.get(f"qq_group_norms_{GROUP}") == "- 不刷屏"
            g = await (await c.get(f"/api/gate?gid={GROUP}")).json()
            assert "weights" in g and "trace" in g and g["refit_min"] == 500
    asyncio.run(go())


def test_web_self_notes_per_group(cfg, brain):
    from aiohttp.test_utils import TestClient, TestServer

    from aigroup.chat.group import GroupChat
    from aigroup.web.server import GroupHome
    gc = GroupChat(cfg, brain, OB(), OWNER, [GROUP, GROUP2], [])
    brain.db.set(f"qq_self_notes_{GROUP2}", "- B 群的事")

    async def go():
        async with TestClient(TestServer(GroupHome(cfg, brain, OB(), gc).app())) as c:
            await c.post("/api/her/self_notes", json={"gid": GROUP, "text": "- A 群的事"})
    asyncio.run(go())
    assert brain.db.get(f"qq_self_notes_{GROUP}") == "- A 群的事" and brain.db.get(f"qq_self_notes_{GROUP2}") == "- B 群的事"


def test_life_page_api(cfg, brain):
    """4.2 她的日子：看、改喜好、记约定、教她一件事、改知识。"""
    import time as _t
    gc, ob = _setup(cfg, brain)
    _run(gc, _msg(FRIEND, "小王", "hi", 1))

    async def go():
        async with _client(cfg, brain, gc, ob) as c:
            r = await (await c.post("/api/life/pref", json={"topic": "香菜", "v": "讨厌"})).json()
            assert r["ok"]
            when = _t.strftime("%Y-%m-%d %H:%M", _t.localtime(_t.time() + 86400))
            r = await (await c.post("/api/life/plan", json={"gid": GROUP, "what": "开黑", "when": when, "kind": "remind"})).json()
            assert r["ok"]
            r = await (await c.post("/api/knowledge", json={"add": True, "gid": GROUP, "topic": "禁忌岛", "keys": "七日世界",
                                                           "summary": "新剧本"})).json()
            assert r["ok"]
            L = await (await c.get(f"/api/life?gid={GROUP}")).json()
            assert L["prefs"][0]["topic"] == "香菜" and L["plans"][0]["what"] == "开黑"
            kid = L["knowledge"][0]["id"]
            await c.post("/api/knowledge", json={"id": kid, "summary": "10月22日上线的新剧本"})
            L = await (await c.get(f"/api/life?gid={GROUP}")).json()
            assert L["knowledge"][0]["summary"].startswith("10月22日") and L["knowledge"][0]["source"] == "manual"
            await c.post("/api/life/plan", json={"id": L["plans"][0]["id"], "status": "skip"})
            await c.post("/api/life/pref", json={"topic": "香菜", "delete": True})
            L = await (await c.get(f"/api/life?gid={GROUP}")).json()
            assert not L["plans"] and not L["prefs"]
            page = await (await c.get("/")).text()
            assert 'data-p="life"' in page and 'class="adm" data-p="knobs"' in page
    asyncio.run(go())


def test_eval_and_birthday_api(cfg, brain):
    gc, ob = _setup(cfg, brain)
    _run(gc, _msg(FRIEND, "小王", "hi", 1))

    async def go():
        async with _client(cfg, brain, gc, ob) as c:
            E = await (await c.get("/api/eval")).json()
            assert E["hist"] == [] and E["current"]
            r = await (await c.post("/api/life/birthday", json={"uid": FRIEND, "day": "05-21"})).json()
            assert r["ok"]
            r = await (await c.post("/api/life/birthday", json={"uid": FRIEND, "day": "5月99"})).json()
            assert not r["ok"]
            L = await (await c.get(f"/api/life?gid={GROUP}")).json()
            assert any(b["day"] == "05-21" for b in L["birthdays"])
            page = await (await c.get("/")).text()
            assert 'id="evRun"' in page and 'id="lfBd"' in page
    asyncio.run(go())


def test_where_api(cfg, brain, monkeypatch):
    from aigroup.net.where import Where

    async def get(self, url, params):
        if "geocoding" in url:
            return {"results": [{"latitude": 30.6, "longitude": 104.1, "feature_code": "PPLA", "population": 9,
                                 "admin1": "四川"}]}
        return {"current": {"temperature_2m": 20, "weather_code": 0},
                "daily": {"weather_code": [0, 0], "temperature_2m_max": [25, 25], "temperature_2m_min": [15, 15],
                          "precipitation_probability_max": [0, 0], "wind_speed_10m_max": [5, 5]}}
    monkeypatch.setattr(Where, "_get", get)
    gc, ob = _setup(cfg, brain)
    _run(gc, _msg(FRIEND, "小王", "hi", 1))

    async def go():
        async with _client(cfg, brain, gc, ob) as c:
            r = await (await c.post("/api/life/where", json={"uid": FRIEND, "city": "成都", "why": "旅游"})).json()
            assert r["ok"] and "四川" in r["message"]
            L = await (await c.get(f"/api/life?gid={GROUP}")).json()
            assert L["where"][0]["city"] == "成都" and "晴" in L["where"][0]["weather"]
            r = await (await c.post("/api/life/where", json={"uid": FRIEND, "kind": "trip", "delete": True})).json()
            L = await (await c.get(f"/api/life?gid={GROUP}")).json()
            assert not L["where"]
    asyncio.run(go())
