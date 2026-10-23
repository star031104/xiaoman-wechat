"""微信版 2.1：复查修的问题（不丢消息、确认晚到、同名群、口令派生、分块读、副本目录、补消息按 id、拍一拍的人）。"""
import asyncio
import hashlib
import os
import threading
import time

import pytest

from aigroup.chat.group import GroupChat
from aigroup.wechat import dbview
from aigroup.wechat import keys as K
from aigroup.wechat.dbview import DBView

from conftest import FakeLLM
from test_wechat import FRIEND_W, OWNER_W, ROOM, FakeSender, _account, _bot
from wxfake import FakeAccount, FakeWeChatDB


def _connect(b):
    b._init_blocking()
    b.connected.set()


def test_poll_error_does_not_lose_messages(tmp_path, monkeypatch):
    acc = _account(tmp_path)
    b = _bot(tmp_path, acc, monkeypatch)
    _connect(b)
    acc.add_msg(ROOM, FRIEND_W, "这句不能丢")
    real = b.to_event
    boom = {"n": 1}

    def flaky(m):
        if boom["n"]:
            boom["n"] -= 1
            raise RuntimeError("半路出错")
        return real(m)
    b.to_event = flaky
    with pytest.raises(RuntimeError):
        b._poll_locked()
    b.data.views["message/message_0.db"].dirty = True       # 下一轮照样会再读（库没变也读）
    evs = b._poll_locked()
    assert [e["message"][0]["data"]["text"] for e in evs] == ["这句不能丢"]


class LateSender(FakeSender):
    """粘贴完过了好一会儿才在库里出现（比 confirm_seconds 还久）。"""

    def paste_send(self, name, text=None, files=None, allow_search=True, title=None):
        self.sent.append((name, text, files))
        threading.Timer(1.0, lambda: self.acc.add_msg(ROOM, FakeAccount.SELF, text)).start()
        return "window"


def test_late_confirmation_becomes_own_message(tmp_path, monkeypatch):
    acc = _account(tmp_path)
    b = _bot(tmp_path, acc, monkeypatch, confirm_seconds=0.5)
    b.sender = LateSender(acc)
    own = []

    async def on_own(ev):
        own.append(ev)
    b.on_self_message.append(on_own)

    async def go():
        b.start()
        for _ in range(100):
            await asyncio.sleep(0.05)
            if b.ready:
                break
        with pytest.raises(Exception, match="没看到"):
            await b.send_group(b.gid(ROOM), [{"type": "text", "data": {"text": "迟到的一句"}}])
        for _ in range(60):
            await asyncio.sleep(0.05)
            if own:
                break
        await b.stop()
    asyncio.run(go())
    assert own and own[0]["message"][0]["data"]["text"] == "迟到的一句"


def test_refuse_same_name_or_unnamed_group(tmp_path, monkeypatch):
    acc = _account(tmp_path)
    acc.add_contact("wxid_twin", "画画群")                     # 一个好友的昵称正好也叫“画画群”
    acc.add_room("777@chatroom", "", {OWNER_W: "阿远", FakeAccount.SELF: "小满"})
    acc.publish()
    b = _bot(tmp_path, acc, monkeypatch)
    b.groups.append("777@chatroom")

    async def go():
        b.start()
        for _ in range(100):
            await asyncio.sleep(0.05)
            if b.ready:
                break
        with pytest.raises(Exception, match="同名"):
            await b.send_group(b.gid(ROOM), [{"type": "text", "data": {"text": "x"}}])
        with pytest.raises(Exception, match="没有群名"):
            await b.send_group(b.gid("777@chatroom"), [{"type": "text", "data": {"text": "x"}}])
        b.sender.titles[ROOM] = "画画群"                         # 写的标题和同名的一样：还是分不清，不发
        with pytest.raises(Exception, match="同名"):
            await b.send_group(b.gid(ROOM), [{"type": "text", "data": {"text": "x"}}])
        b.sender.titles[ROOM] = "画画群（工作室）"                # 按群 id 写了一个不同的窗口标题：照它
        assert await b.send_group(b.gid(ROOM), [{"type": "text", "data": {"text": "能发"}}])
        await b.stop()
    asyncio.run(go())
    assert b.sender.sent == [("画画群", "能发", None)]


def test_passphrase_derivation(tmp_path):
    phrase = os.urandom(32)
    salt = os.urandom(16)
    key = hashlib.pbkdf2_hmac("sha512", phrase.hex().encode(), salt, 256000, dklen=32)
    db = FakeWeChatDB(tmp_path / "p.db", tmp_path / "acc" / "db_storage" / "message" / "message_0.db", key, salt=salt)
    db.executescript("CREATE TABLE t(a);")
    db.checkpoint()
    db.publish()
    chk = K.KeyChecker(K.db_files(tmp_path / "acc"))
    keys = {}
    assert chk.check(phrase, keys, phrase=True) == 0 and chk.phrases == [phrase]
    assert K.strategy_passphrase(chk, keys) == 1 and keys["message/message_0.db"] == key


def test_dbview_reads_in_chunks(tmp_path, monkeypatch):
    monkeypatch.setattr(dbview, "CHUNK", 3)
    key = os.urandom(32)
    f = FakeWeChatDB(tmp_path / "p.db", tmp_path / "e" / "m.db", key)
    f.executescript("CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT);")
    for i in range(120):
        f.execute("INSERT INTO t(v) VALUES (?)", ("y" * 80 + str(i),))
    f.checkpoint()
    f.publish()
    v = DBView(tmp_path / "e" / "m.db", key, tmp_path / "w")
    v.refresh()
    c = v.connect()
    assert c.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 120 and c.execute("PRAGMA quick_check").fetchone()[0] == b"ok"
    c.close()
    assert v.main_pages > 3


def test_work_dir_per_process(tmp_path):
    from aigroup.wechat.bot import _try_lock, work_dir
    base = tmp_path / "data" / "wechat" / "cache" / "acc"
    stale = base / "p999999"
    busy = base / "p888888"
    for d in (stale, busy):
        d.mkdir(parents=True)
        (d / "x.plain.db").write_bytes(b"1")
    held = _try_lock(busy / "lock")                          # 另一个进程正拿着
    (base / "old.plain.db").write_bytes(b"1")                # 1.x 留下的
    mine = work_dir(tmp_path, "acc")
    assert mine.name == f"p{os.getpid()}" and mine.exists()
    assert not stale.exists() and not (base / "old.plain.db").exists()
    if os.name != "nt":                                      # flock 是按打开的文件算的：同一进程里也拦得住
        assert busy.exists()
    held.close()
    assert work_dir(tmp_path, "acc") == mine


def test_catch_up_records_late_old_messages_by_id(tmp_path, monkeypatch, cfg):
    """先实时收到了一条新的，之后才从库里补到更早的几条：按 id 去重，旧的照样补进去。"""
    from aigroup.brain import GroupBrain
    acc = _account(tmp_path)
    b = _bot(tmp_path, acc, monkeypatch)
    _connect(b)
    gid = b.gid(ROOM)
    brain = GroupBrain(cfg)
    brain.llm = FakeLLM()
    gc = GroupChat(cfg, brain, b, b.ids.id(OWNER_W), [gid], [])
    gc._record(gid, b.ids.id(FRIEND_W), "王哥", "刚刚实时收到的", 1)
    acc.add_msg(ROOM, OWNER_W, "一小时前的话", ts=int(time.time()) - 3600)
    assert b._poll_locked() == [] and b._backlog
    asyncio.run(gc.catch_up())
    texts = [r[0] for r in brain.db.conn.execute("SELECT content FROM qq_group_messages WHERE group_id=?", (gid,))]
    assert "一小时前的话" in texts and texts.count("刚刚实时收到的") == 1
    asyncio.run(gc.catch_up())                               # 再补一次也不重复
    texts2 = [r[0] for r in brain.db.conn.execute("SELECT content FROM qq_group_messages WHERE group_id=?", (gid,))]
    assert len(texts2) == len(texts)


def test_pat_sender_from_xml(tmp_path, monkeypatch):
    acc = _account(tmp_path)
    b = _bot(tmp_path, acc, monkeypatch)
    _connect(b)
    acc.add_msg(ROOM, ROOM, '<sysmsg type="pat"><pat><fromusername>wxid_friend</fromusername>'
                '<pattedusername>wxid_her</pattedusername></pat></sysmsg>', mtype=10000, prefix=False)
    evs = b._poll_locked()
    assert evs[0]["sub_type"] == "poke" and evs[0]["user_id"] == b.ids.id(FRIEND_W)


def test_rebuilt_shard_drops_key(tmp_path, monkeypatch):
    """微信重建了消息库（新 salt、新密钥）：认出来、丢掉旧密钥，交给“补拿密钥”，而不是一直空转。"""
    acc = _account(tmp_path)
    b = _bot(tmp_path, acc, monkeypatch)
    _connect(b)
    d = b.data
    m = acc.msg
    m.key, m.salt = os.urandom(32), os.urandom(16)                # 同一个文件，换了密钥重新写
    acc.add_msg(ROOM, FRIEND_W, "重建以后的话")
    d.refresh_all()
    assert "message/message_0.db" in d.missing_keys()
    d.keys["message/message_0.db"] = m.key                         # 补拿到了新密钥
    d.refresh_all()
    assert any(x.text == "重建以后的话" for x in d.new_messages(ROOM, 0))


def test_clock_skew_and_reconnect_after_unknown(tmp_path, monkeypatch):
    acc = _account(tmp_path)
    b = _bot(tmp_path, acc, monkeypatch)
    _connect(b)
    b._skews.extend([900, 905, 898])                               # 电脑时钟快了 15 分钟（她自己发的消息量出来的）
    acc.add_msg(ROOM, FRIEND_W, "刚说的", ts=int(time.time()) - 900)
    evs = b._poll_locked()
    assert evs and not b._backlog                                  # 校正以后不算旧消息

    async def go():
        for state in (False, None, True):                          # 关掉 → 查不到 → 开回来
            b.running_check = lambda s=state: s
            b._last_alive = -1e9
            await b._check_alive()
        for _ in range(10):
            await asyncio.sleep(0.02)
        assert b.ready
    asyncio.run(go())
