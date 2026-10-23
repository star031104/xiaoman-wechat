"""2.2 两个群：每个群单独调开关、小屋按群看画像 / 对比 / 自己的事。"""
import asyncio

from aiohttp.test_utils import TestClient, TestServer

from aigroup.chat.group import GroupChat
from aigroup.chat.settings import group_key
from aigroup.web.server import GroupHome

from conftest import FRIEND, FRIEND2, GROUP, GROUP2, OB, OWNER, ProfileLLM, _msg, _run


def _two(cfg, brain):
    brain.llm = ProfileLLM()
    brain.llm.reply = "笑死"
    ob = OB()
    return GroupChat(cfg, brain, ob, OWNER, [GROUP, GROUP2], ["满满"]), ob


def _in(gid, ev):
    ev["group_id"] = gid
    return ev


def test_group_knobs_override_and_persist(cfg, brain):
    gc, _ = _two(cfg, brain)
    base = gc.g["talkativeness"]
    gc.gover[GROUP2] = {"talkativeness": 0.9}
    assert gc.gk(GROUP)["talkativeness"] == base and gc.gk(GROUP2)["talkativeness"] == 0.9
    assert gc.gk(GROUP2)["max_bubbles"] == gc.g["max_bubbles"]          # 没单独调的跟着全局
    assert gc.gk(None) is gc.g
    brain.db.set(group_key(GROUP2), {"talkativeness": 0.8, "mood": False, "nope": 1})
    gc2 = GroupChat(cfg, brain, OB(), OWNER, [GROUP, GROUP2], [])
    assert gc2.gover[GROUP2] == {"talkativeness": 0.8}                  # 只能全局调的、不认识的都不要


def test_web_two_groups(cfg, brain):
    gc, ob = _two(cfg, brain)
    _run(gc, _msg(FRIEND, "小王", "满满在吗", 1, at=True), _msg(OWNER, "我", "今天好热", 2),
         _in(GROUP2, _msg(FRIEND, "小王", "B 群这边也来了", 3)), _in(GROUP2, _msg(FRIEND2, "小李", "欢迎欢迎", 4)))

    async def go():
        async with TestClient(TestServer(GroupHome(cfg, brain, ob, gc).app())) as c:
            s = await (await c.get("/api/state")).json()
            a, b = {g["id"]: g for g in s["groups"]}[GROUP], {g["id"]: g for g in s["groups"]}[GROUP2]
            assert a["mentions_today"] >= 1 and b["mentions_today"] == 0
            assert a["active_today"] == 2 and b["active_today"] == 2 and s["kpi"]["both_groups"] == 1
            assert sum(s["hours"]["by"][str(GROUP2)]["all"]) == b["today"]
            assert s["days"][-1]["by"][str(GROUP2)][0] == b["today"]
            assert "effect_rate" in a and a["own_knobs"] == []

            # 每个群单独调开关
            await c.post("/api/settings", json={"gid": GROUP2, "values": {"talkativeness": 0.95, "lore": False}})
            assert gc.gk(GROUP2)["talkativeness"] == 0.95 and "lore" not in gc.gover[GROUP2]
            assert gc.gk(GROUP)["talkativeness"] == gc.g["talkativeness"] != 0.95
            assert brain.db.get(group_key(GROUP2)) == {"talkativeness": 0.95}
            k = await (await c.get(f"/api/settings?gid={GROUP2}")).json()
            it = {i["key"]: i for i in k["items"]}
            assert it["talkativeness"]["changed"] and it["talkativeness"]["value"] == 0.95 and it["lore"]["global_only"]
            k = await (await c.get("/api/settings")).json()
            assert {i["key"]: i for i in k["items"]}["talkativeness"]["groups"] == [GROUP2]
            g = await (await c.get(f"/api/gate?gid={GROUP2}")).json()
            assert g["talkativeness"] == 0.95
            await c.post("/api/settings", json={"gid": GROUP2, "reset": True})
            assert GROUP2 not in gc.gover and brain.db.get(group_key(GROUP2)) is None
            assert (await c.get("/api/settings?gid=1")).status == 400

            # 群友：按群看
            p = await (await c.get("/api/people")).json()
            w = {x["user_id"]: x for x in p["people"]}[FRIEND]
            assert sorted(w["groups"]) == sorted([GROUP, GROUP2]) and set(w["per"]) == {str(GROUP), str(GROUP2)}
            x = await (await c.get(f"/api/person?uid={FRIEND}")).json()
            per = {g["gid"]: g for g in x["per"]}
            assert per[GROUP]["found_her"] >= 1 and per[GROUP2]["found_her"] == 0 and sum(per[GROUP2]["hours"]) == 1
            assert any(q["name"] == "小李" for q in per[GROUP2]["partners"]) or per[GROUP2]["partners"] == []

            # 只改 B 群的画像：A 群的不动
            await c.post("/api/person/update", json={"uid": FRIEND, "gid": GROUP2, "profile": {"印象": "B 群的他"}})
            assert gc.person(FRIEND, GROUP2)["profile"] == {"印象": "B 群的他"}
            assert gc.person(FRIEND, GROUP)["profile"] != {"印象": "B 群的他"}
            await c.post("/api/person/update", json={"uid": FRIEND, "gid": "all", "profile": {"印象": "都一样"}})
            assert gc.person(FRIEND, GROUP)["profile"] == gc.person(FRIEND, GROUP2)["profile"] == {"印象": "都一样"}
            await c.post("/api/person/update", json={"uid": FRIEND2, "gid": "all", "profile": {"印象": "只在 B 群"}})
            assert gc.person(FRIEND2, GROUP)["profile"] == {}                 # 他不在 A 群：不写进去
            await c.post("/api/person/forget", json={"uid": FRIEND, "gid": GROUP})
            assert gc.person(FRIEND, GROUP)["profile"] == {} and gc.person(FRIEND, GROUP2)["profile"] == {"印象": "都一样"}

            brain.db.set(f"qq_self_notes_{GROUP2}", "- B 群说过要去看展")
            h = await (await c.get("/api/her")).json()
            assert {x["gid"]: x["text"] for x in h["self_notes_by"]}[GROUP2] == "- B 群说过要去看展"
    asyncio.run(go())


def test_idle_uses_group_active_hours(cfg, brain):
    gc, _ = _two(cfg, brain)
    gc.gover[GROUP] = {"active_hours": "00:00-00:01", "idle_chance": 0.0}
    assert gc.gk(GROUP)["active_hours"] == "00:00-00:01" and gc.gk(GROUP2)["active_hours"] == gc.g["active_hours"]
    assert gc.secs("read_delay", GROUP) >= 0
