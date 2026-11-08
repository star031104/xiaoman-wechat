"""35.0：攒一批再回、说多了提醒多听、带出处的摘要、群友查 / 删自己的记录、导出、视频、小屋显示 v2 人设、分层。"""
import asyncio
import json
import time
from collections import defaultdict

from aiohttp.test_utils import TestClient, TestServer

from aigroup.chat.group import GroupChat
from aigroup.memory.episodes import parse_lines
from aigroup.web.server import GroupHome

from conftest import FRIEND, FRIEND2, GROUP, OB, OWNER, _gc, _msg, _run


def _rec(gc, uid, name, text, ts, role="user"):
    gc.db.conn.execute("INSERT INTO qq_group_messages(ts, group_id, user_id, name, role, content) VALUES(?,?,?,?,?,?)",
                       (ts, GROUP, uid, name, role, text))
    gc.db.conn.commit()


def test_layers_no_name_clash():
    own = defaultdict(list)
    for b in GroupChat.__mro__:
        if b.__name__.endswith("Mixin"):
            for k in vars(b):
                if not k.startswith("__"):
                    own[k].append(b.__name__)
    assert len([b for b in GroupChat.__mro__ if b.__name__.endswith("Mixin")]) == 27
    assert {k: v for k, v in own.items() if len(v) > 1} == {}


def test_merge_wait(cfg, brain):
    gc, _ = _gc(cfg, brain, merge_wait_seconds=2)
    st = gc._st(GROUP)
    st.batch_first_ts = time.time()
    assert gc.merge_wait(GROUP) == 2
    st.batch_mentioned = True
    assert gc.merge_wait(GROUP) == 1
    st.batch_mentioned = False
    now = time.time()
    for i in range(5):
        _rec(gc, FRIEND, "阿王", f"第{i}句", now - 3)
    assert gc.merge_wait(GROUP) == 5                                # 热闹：多等等
    st.batch_first_ts = now - 7.5
    assert gc.merge_wait(GROUP) <= 0.6                              # 一批最多 8 秒
    gc.g["merge_adaptive"] = False
    assert gc.merge_wait(GROUP) == 2
    gc.g["merge_adaptive"], gc.g["merge_wait_seconds"] = True, 0
    assert gc.merge_wait(GROUP) == 0


def test_share_hint(cfg, brain):
    gc, _ = _gc(cfg, brain)
    now = time.time()
    for i in range(6):
        _rec(gc, FRIEND, "阿王", f"x{i}", now - 100)
    assert gc.v35_state(GROUP, mentioned=False) == []
    for i in range(4):
        _rec(gc, 0, "小满", f"y{i}", now - 50, role="assistant")
    out = gc.v35_state(GROUP, mentioned=False)
    assert out and "多听少说" in out[0] and "40%" in out[0]
    assert "短一点" in gc.v35_state(GROUP, mentioned=True)[0]


def test_episode_parse_and_block(cfg, brain):
    assert parse_lines("阿王周六去深圳看 KPL 总决赛|#12,#13\n- 无关|#99\n乱写的一行", {12, 13}) == \
        [{"t": "阿王周六去深圳看 KPL 总决赛", "ids": [12, 13]}]
    gc, _ = _gc(cfg, brain)
    t0 = time.time() - 3 * 86400
    for i in range(12):
        _rec(gc, FRIEND, "阿王", "我周六要去深圳看KPL总决赛" if i == 3 else f"闲聊{i}", t0 + i)
    rows = gc.episodes_due(GROUP)
    assert len(rows) == 12                                          # 不到 40 条，但已经停了半小时以上
    ids = [r["id"] for r in rows]

    async def fake_chat(messages, **kw):
        assert f"#{ids[3]} 阿王：我周六要去深圳看KPL总决赛" in messages[-1]["content"]
        return f"阿王说周六去深圳看 KPL 总决赛|#{ids[3]}"
    gc.brain.llm.chat = fake_chat
    assert asyncio.run(gc.episodes_tick(GROUP)) == 1
    assert gc.episodes_due(GROUP) == []
    blk = gc.episodes_block(GROUP, "KPL总决赛阿王去深圳了吗")
    assert "看 KPL 总决赛" in blk and "原话：阿王“我周六要去深圳看KPL总决赛”" in blk
    assert gc.episodes_block(GROUP, "今天吃啥") == ""
    assert gc.episodes_block(GROUP, "KPL总决赛阿王去深圳了吗", before_id=ids[0]) == ""   # 还在聊天记录里的那段不重复给


class OB2(OB):
    async def send_group(self, gid, segs, **kw):
        self.sent.append((gid, segs))
        return len(self.sent)


def test_records_cmd_and_erase(cfg, brain):
    gc = GroupChat(cfg, brain, OB2(), OWNER, [GROUP], ["满满"])
    ob = gc.ob
    _run(gc, _msg(FRIEND, "阿王", "我在准备考研", 1), _msg(FRIEND2, "小周", "加油", 2))
    gc.memory.add_fact(GROUP, "阿王在准备考研", speaker=FRIEND, subject=FRIEND, kind="fact", source="self")
    _run(gc, _msg(FRIEND, "阿王", "/我的记录", 3))
    said = ob.sent[-1][1][0]["data"]["text"]
    assert "2 条聊天" in said and "1 件关于你的事" in said and "/删除我的记录 确认" in said
    _run(gc, _msg(FRIEND, "阿王", "/删除我的记录", 4))
    assert "确认" in ob.sent[-1][1][0]["data"]["text"]
    assert gc.member_counts(FRIEND)["messages"] >= 2                # 没确认：不删
    _run(gc, _msg(FRIEND, "阿王", "/删除我的记录 确认", 5))
    assert "删好了" in ob.sent[-1][1][0]["data"]["text"]
    assert gc.member_counts(FRIEND) == {"messages": 0, "facts": 0, "profile": False, "plans": 0}
    assert gc.member_counts(FRIEND2)["messages"] == 1               # 别人的不动


def test_export_erase_api_and_persona(cfg, brain, tmp_path):
    from conftest import ProfileLLM
    brain.llm = ProfileLLM()
    v2 = tmp_path / "persona" / "v2.md"
    v2.write_text((tmp_path / "persona" / "xiaoman.md").read_text(encoding="utf-8").replace("口头禅", "口头禅", 1),
                  encoding="utf-8")
    cfg["wechat"]["group"].update(persona_version="v2", persona_v2_file="persona/v2.md")
    gc = GroupChat(cfg, brain, OB(), OWNER, [GROUP], ["满满"])
    home = GroupHome(cfg, brain, gc.ob, gc)
    _rec(gc, FRIEND, "阿王", "我在准备考研", time.time())

    async def go():
        async with TestClient(TestServer(home.app())) as c:
            r = await c.get(f"/api/person/export?uid={FRIEND}")
            assert r.status == 200 and "attachment" in r.headers.get("Content-Disposition", "")
            d = json.loads(await r.text())
            assert d["messages"][0]["content"] == "我在准备考研"
            assert not (await (await c.post("/api/person/erase", json={"uid": FRIEND})).json())["ok"]
            assert (await (await c.post("/api/person/erase", json={"uid": FRIEND, "confirm": True})).json())["ok"]
            assert gc.member_counts(FRIEND)["messages"] == 0
            her = await (await c.get("/api/her")).json()
            assert her["persona"]["file"] == "persona/v2.md"
            st = await (await c.get("/api/state")).json()
            assert st["persona"]["file"] == "persona/v2.md"
    asyncio.run(go())


def test_video_thumb(cfg, brain):
    import io

    from PIL import Image
    b = io.BytesIO()
    Image.new("RGB", (64, 36), "red").save(b, "JPEG")

    class OBV(OB):
        async def fetch_video(self, url, wait=6.0):
            return b.getvalue(), ""
    gc = GroupChat(cfg, brain, OBV(), OWNER, [GROUP], ["满满"])
    seen = []

    async def describe(img, hint="", prompt=""):
        seen.append(prompt)
        return "一个人在打羽毛球。"
    gc.brain.llm.describe_image = describe
    gc.brain.llm.vision = True
    out = asyncio.run(gc._look_video("wxvid:x@chatroom:" + "a" * 32 + ":1", "阿王", GROUP))
    assert out == "一个人在打羽毛球（只看了封面）" and "封面" in seen[0]
    assert asyncio.run(gc._look_video("wxvid:x@chatroom:" + "a" * 32 + ":1", "阿王", GROUP)) == out   # 看过的不再看
    from aigroup.onebot import flatten
    f = flatten([{"type": "text", "data": {"text": "[视频]"}}, {"type": "video", "data": {"url": "wxvid:a:b:1"}}], 1)
    assert f.text == "[视频]" and f.videos == ["wxvid:a:b:1"]
