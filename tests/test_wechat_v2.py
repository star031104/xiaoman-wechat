"""微信版 2.0：从本地库补消息、成员列表、离线消息不乱接、微信关了又开、发送限速、微信小表情、点不了表情。"""
import asyncio
import json
import time

import pytest

from aigroup.chat import plan
from aigroup.chat.group import GroupChat
from aigroup.wechat.wxdb import WeChatData

from conftest import OB, FakeLLM, _run
from test_wechat import FRIEND_W, OWNER_W, ROOM, FakeSender, _account, _bot
from wxfake import FakeAccount


def _connect(b):
    b._init_blocking()
    b.connected.set()


def test_catch_up_from_local_db_and_members(tmp_path, monkeypatch, cfg):
    """她没开的时候群里的话库里都有：接上后补进记录；期间 @ 她的，记下来等会儿补一句“才看到”。"""
    from aigroup.brain import GroupBrain
    acc = _account(tmp_path)
    now = int(time.time())
    acc.add_msg(ROOM, FRIEND_W, "昨天的画交了吗", ts=now - 3000)
    acc.add_msg(ROOM, FakeAccount.SELF, "交啦", ts=now - 2900)                         # 她的号在手机上说的
    acc.add_msg(ROOM, OWNER_W, "@满满 晚上一起开黑", ts=now - 2800,
                source="<msgsource><atuserlist>wxid_her</atuserlist></msgsource>")
    b = _bot(tmp_path, acc, monkeypatch)
    _connect(b)
    gid = b.gid(ROOM)
    brain = GroupBrain(cfg)
    brain.llm = FakeLLM()
    gc = GroupChat(cfg, brain, b, b.ids.id(OWNER_W), [gid], [])

    async def go():
        ms = await b.call("get_group_member_list", group_id=gid)
        assert {m["card"] for m in ms} >= {"论文战士", "满满"}
        h = await b.call("get_group_msg_history", group_id=gid, count=200)
        assert [m["user_id"] for m in h["messages"]][-3:] == [b.ids.id(FRIEND_W), b.self_id, b.ids.id(OWNER_W)]
        await gc.refresh_members()
        await gc.catch_up()
    asyncio.run(go())
    rows = [tuple(r) for r in brain.db.conn.execute(
        "SELECT role, content FROM qq_group_messages WHERE group_id=? ORDER BY ts", (gid,)).fetchall()]
    assert ("assistant", "交啦") in rows and ("user", "@你 晚上一起开黑") in rows and ("user", "昨天的画交了吗") in rows
    night = brain.db.get(f"qq_night_{gid}")
    assert night and night[-1]["kind"] == "offline"
    assert brain.db.get(f"qq_members_{gid}")[str(b.ids.id(FRIEND_W))] == "论文战士"


def test_stale_messages_go_to_backlog_not_live(tmp_path, monkeypatch):
    acc = _account(tmp_path)
    b = _bot(tmp_path, acc, monkeypatch)
    _connect(b)
    acc.add_msg(ROOM, FRIEND_W, "一小时前的话（微信刚开才同步下来）", ts=int(time.time()) - 3600)
    acc.add_msg(ROOM, FRIEND_W, "刚说的")
    evs = b._poll_locked()
    assert [e["message"][0]["data"]["text"] for e in evs] == ["刚说的"] and b._backlog


def test_wechat_closed_then_reopened(tmp_path, monkeypatch):
    acc = _account(tmp_path)
    b = _bot(tmp_path, acc, monkeypatch)
    _connect(b)
    calls = []

    async def on_conn():
        calls.append(1)
    b.on_connect.append(on_conn)

    async def go():
        b.running_check = lambda: False
        await b._check_alive()
        assert not b.ready and b.alive is False
        with pytest.raises(Exception, match="没在运行"):
            await b.send_group(b.gid(ROOM), [{"type": "text", "data": {"text": "x"}}])
        b._last_alive = 0
        b.running_check = lambda: True
        await b._check_alive()
        for _ in range(20):
            await asyncio.sleep(0.02)
        assert b.ready and calls == [1] and b.stats["reconnects"] == 1
    asyncio.run(go())


def test_send_gap_hour_cap_and_faces(tmp_path, monkeypatch):
    acc = _account(tmp_path)
    b = _bot(tmp_path, acc, monkeypatch, max_sends_per_hour=2, send_gap_seconds=0.3)
    gid = b.gid(ROOM)

    async def go():
        b.start()
        for _ in range(100):
            await asyncio.sleep(0.05)
            if b.ready:
                break
        t0 = time.time()
        await b.send_group(gid, [{"type": "text", "data": {"text": "第一句"}}, {"type": "face", "data": {"id": "182"}}])
        await b.send_group(gid, [{"type": "text", "data": {"text": "第二句"}}])
        assert time.time() - t0 >= 0.3                                   # 两条之间隔开
        assert b.sender.sent[0][1] == "第一句[破涕为笑]"
        with pytest.raises(Exception, match="这一小时"):
            await b.send_group(gid, [{"type": "text", "data": {"text": "第三句"}}])
        assert b.status()["hour_sends"] == 2
        await b.stop()
    asyncio.run(go())


def test_own_phone_messages_are_recorded(tmp_path, monkeypatch, cfg):
    from aigroup.app import record_own
    from aigroup.brain import GroupBrain
    acc = _account(tmp_path)
    b = _bot(tmp_path, acc, monkeypatch)
    _connect(b)
    brain = GroupBrain(cfg)
    brain.llm = FakeLLM()
    gc = GroupChat(cfg, brain, b, 0, [b.gid(ROOM)], [])
    acc.add_msg(ROOM, FakeAccount.SELF, "我在手机上说一句")
    evs = b._poll_locked()
    assert evs[0]["post_type"] == "message_sent"
    asyncio.run(record_own(gc, evs[0]))
    r = brain.db.conn.execute("SELECT role, content FROM qq_group_messages").fetchall()
    assert [tuple(x) for x in r] == [("assistant", "我在手机上说一句")]


class NoReactOB(OB):
    can_react = False
    prompt_hint = "\n- 微信小表情测试提示"


def test_no_react_on_wechat_and_keep_wx_faces(cfg, brain):
    """微信点不了表情：提示词里不给“react”这个选项；模型还是选了 react 但写了话，就把话说出来。"""
    cfg["wechat"]["group"].update({"see_images": False})
    ob = NoReactOB()
    gc = GroupChat(cfg, brain, ob, 55555, [777777], ["满满"])
    brain.llm.reply = json.dumps({"act": "react", "react": "开心", "say": ["哈哈[捂脸]"]}, ensure_ascii=False)
    old = set(plan.KEEP_TAGS)
    plan.KEEP_TAGS |= {"捂脸"}
    try:
        _run(gc, {"post_type": "message", "message_type": "group", "group_id": 777777, "user_id": 88888,
                  "message_id": 1, "sender": {"card": "小王"},
                  "message": [{"type": "at", "data": {"qq": "10001"}}, {"type": "text", "data": {"text": " 在吗"}}]})
    finally:
        plan.KEEP_TAGS.clear()
        plan.KEEP_TAGS |= old
    sys_prompt = "\n".join(m["content"] for m in brain.llm.group_calls[-1] if m["role"] == "system")
    assert '"react"' not in sys_prompt and "微信小表情测试提示" in sys_prompt
    sent = ["".join(s["data"].get("text", "") for s in segs if s["type"] == "text") for _, segs in ob.sent]
    assert sent and "哈哈[捂脸]" in sent[0]
    assert not [c for c in ob.calls if c[0] == "set_msg_emoji_like"]


def test_app_end_to_end(tmp_path, monkeypatch, cfg):
    """整个程序跑起来：接上（假）微信 → 补消息 → 有人 @ 她 → 她回 → 托盘点退出。"""
    import socket

    from aigroup import app
    from aigroup import brain as B
    from aigroup.wechat import bot as WB
    acc = _account(tmp_path)
    acc.add_msg(ROOM, FRIEND_W, "她没开的时候说的", ts=int(time.time()) - 1000)
    real_check = app.check
    monkeypatch.setattr(app, "check", lambda c: [p for p in real_check(c) if "Windows" not in p])
    monkeypatch.setattr(WB, "open_data", lambda o, root, **k: (WeChatData(acc.acc, acc.keys, tmp_path / "work"), []))
    monkeypatch.setattr(WB, "_running", lambda: True)

    class Brain(B.GroupBrain):
        def __init__(self, c):
            super().__init__(c)
            self.llm = FakeLLM("来了来了")
    monkeypatch.setattr(B, "GroupBrain", Brain)
    real_init = WB.WeChatBot.__init__

    def init(self, opts, root):
        real_init(self, {**opts, "poll_seconds": 0.05, "confirm_seconds": 2}, root)
        self.sender = FakeSender(acc)
        bots.append(self)
    bots = []
    monkeypatch.setattr(WB.WeChatBot, "__init__", init)
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    cfg["web"] = {**(cfg.get("web") or {}), "port": port}
    cfg["wechat"] = {**cfg["wechat"], "groups": [ROOM], "owner": OWNER_W}
    cfg["llm"] = {**cfg["llm"], "profiles": {"m": {"base_url": "https://x/v1", "model": "m", "api_key": "k"}},
                  "routes": {"chat": ["m"]}}

    async def go():
        task = asyncio.create_task(app.run(cfg))
        for _ in range(200):
            await asyncio.sleep(0.05)
            if bots and bots[0].ready:
                break
        b = bots[0]
        await asyncio.sleep(0.5)
        acc.add_msg(ROOM, OWNER_W, "@满满 在吗", source="<msgsource><atuserlist>wxid_her</atuserlist></msgsource>")
        for _ in range(200):
            await asyncio.sleep(0.05)
            if any(t == "来了来了" for _, t, _ in b.sender.sent):
                break
        (tmp_path / "data" / "tray_cmd").write_text("stop", encoding="utf-8")
        return await asyncio.wait_for(task, 10), b
    res, b = asyncio.run(go())
    assert res == "stop"
    assert any(t == "来了来了" for _, t, _ in b.sender.sent)
    import sqlite3
    db = sqlite3.connect(tmp_path / "data" / "group.db")
    texts = [r[0] for r in db.execute("SELECT content FROM qq_group_messages ORDER BY ts")]
    assert "她没开的时候说的" in texts and "@你 在吗" in texts and "来了来了" in texts
