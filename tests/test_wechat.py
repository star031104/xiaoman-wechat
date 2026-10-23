"""微信接入：解密 / 增量同步、拿密钥、解析消息、收发闭环。（不需要真微信：自己造一份同格式的加密库）"""
import asyncio
import os
import struct
import time

import pytest
import zstandard

from aigroup.wechat import keys as K
from aigroup.wechat.crypto import PAGE, verify_key
from aigroup.wechat.dbview import DBView
from aigroup.wechat.wxdb import WeChatData, parse_row, room_cards, unpack_text

from conftest import FakeLLM
from wxfake import FakeAccount, FakeWeChatDB, ext_buffer

ROOM = "12345678@chatroom"
OWNER_W, FRIEND_W, NEW_W = "wxid_owner", "wxid_friend", "wxid_newbie"


# ---------------- 解密 / 同步 ----------------
def test_dbview_incremental_wal_and_checkpoint(tmp_path):
    key = os.urandom(32)
    f = FakeWeChatDB(tmp_path / "p.db", tmp_path / "enc" / "m.db", key)
    f.executescript("CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT);")
    for i in range(200):
        f.execute("INSERT INTO t(v) VALUES (?)", ("x" * 60 + str(i),))
    f.checkpoint()
    f.execute("INSERT INTO t(v) VALUES ('in-wal')")
    f.publish()
    assert verify_key(key, (tmp_path / "enc" / "m.db").read_bytes()[:PAGE])
    assert not verify_key(os.urandom(32), (tmp_path / "enc" / "m.db").read_bytes()[:PAGE])
    v = DBView(tmp_path / "enc" / "m.db", key, tmp_path / "w")
    assert v.refresh() and v.check_ck and v.overlay

    def q(sql):
        c = v.connect()
        try:
            return c.execute(sql).fetchone()
        finally:
            c.close()
    assert q("SELECT COUNT(*) FROM t")[0] == 201 and q("PRAGMA quick_check")[0] == b"ok"
    assert not v.refresh()                                   # 没变就不动
    f.execute("INSERT INTO t(v) VALUES ('second')")
    f.publish()
    assert v.refresh() and q("SELECT COUNT(*) FROM t")[0] == 202
    f.checkpoint()                                           # wal 合进主库、重置
    f.execute("INSERT INTO t(v) VALUES ('new-gen')")
    f.publish()
    assert v.refresh()
    assert q("SELECT COUNT(*), MAX(id) FROM t")[0] == 203 and q("PRAGMA quick_check")[0] == b"ok"
    f.checkpoint()
    f.publish()                                              # wal 清空
    assert v.refresh() and q("SELECT COUNT(*) FROM t")[0] == 203 and not v.overlay


def test_dbview_ignores_torn_and_uncommitted_frames(tmp_path):
    key = os.urandom(32)
    f = FakeWeChatDB(tmp_path / "p.db", tmp_path / "enc" / "m.db", key)
    f.executescript("CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT);")
    f.checkpoint()
    f.execute("INSERT INTO t(v) VALUES ('a')")
    f.execute("INSERT INTO t(v) VALUES ('b')")
    f.publish()
    wal = tmp_path / "enc" / "m.db-wal"
    raw = bytearray(wal.read_bytes())
    nframes = (len(raw) - 32) // (24 + PAGE)
    # 最后一帧被写坏一半：最后一个事务不算，前面的照样能读
    last = 32 + (nframes - 1) * (24 + PAGE) + 24 + 100
    raw[last:last + 8] = b"\x00" * 8
    wal.write_bytes(bytes(raw))
    v = DBView(tmp_path / "enc" / "m.db", key, tmp_path / "w")
    v.refresh()
    c = v.connect()
    assert c.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 1 and v.dirty
    c.close()
    f.publish()                                              # 写完整了
    assert v.refresh()
    c = v.connect()
    assert [r[0] for r in c.execute("SELECT v FROM t ORDER BY id")] == [b"a", b"b"]
    c.close()


# ---------------- 拿密钥 ----------------
class FakeMem:
    """假进程内存：{地址: bytes}。"""

    def __init__(self, blocks):
        self.blocks = blocks

    def regions(self, private_only=False, max_size=0):
        return [(a, len(b)) for a, b in sorted(self.blocks.items())]

    def read(self, addr, n):
        for a, b in self.blocks.items():
            if a <= addr < a + len(b):
                return b[addr - a: addr - a + n]
        return None


def test_cipher_config_strategy_and_cache(tmp_path):
    acc = FakeAccount(tmp_path)
    files = K.db_files(acc.acc)
    assert set(files) == {"contact/contact.db", "message/message_0.db"}
    chk = K.KeyChecker(files)
    # 按微信 4.1 的样子摆内存：名字串、引用它的 {ptr,len}、配置对象、异或过的 x'key+salt'
    name_at, ref_at, cfg_at, blob_at = 0x100000, 0x200000, 0x300000, 0x400000
    q = lambda v: struct.pack("<Q", v)  # noqa: E731
    k_msg = acc.keys["message/message_0.db"]
    blob = K._xor(f"x'{k_msg.hex()}{os.urandom(16).hex()}'".encode())
    ref = b"\x00" * 0x10 + q(name_at) + q(len(K.CIPHER_NAME)) + b"\x00" * 0x10 + q(cfg_at) + b"\x00" * 0x60
    cfg = b"\x11" * 0xA0 + q(blob_at) + q(len(blob)) + b"\x00" * 0x40           # 偏移和 replica 不一样也能找到
    mem = FakeMem({name_at: K.CIPHER_NAME + b"\x00" * 16, ref_at: ref, cfg_at: cfg, blob_at: blob})
    keys = {}
    assert K.strategy_cipher_config(mem, chk, keys) == 1 and keys["message/message_0.db"] == k_msg
    # 明文 x'…' 的老办法
    k_c = acc.keys["contact/contact.db"]
    mem2 = FakeMem({0x500000: b"junk x'" + k_c.hex().encode() + b"' more"})
    assert K.strategy_plain_hex(mem2, chk, keys) == 1 and keys["contact/contact.db"] == k_c
    # 缓存：只认验得过的
    cache = tmp_path / "keys.json"
    K.save_cache(cache, acc.acc.name, {**keys, "message/message_9.db": os.urandom(32)})
    got, notes = K.get_keys(acc.acc, acc.acc.name, cache)
    assert got == keys and "缓存" in notes[0]


def test_deep_filter_finds_key_in_noise(tmp_path, monkeypatch):
    np = pytest.importorskip("numpy")  # noqa: F841
    acc = FakeAccount(tmp_path)
    k = acc.keys["message/message_0.db"]
    noise = bytearray(b"\x00\x01" * 4096 + os.urandom(64) + b"hello world " * 100)
    off = 8 * 700
    noise[off:off + 32] = k
    p1 = (acc.acc / "db_storage" / "message" / "message_0.db").read_bytes()[:PAGE]

    class M(FakeMem):
        def __init__(self, *a):
            super().__init__({0x10000: bytes(noise)})

        def close(self):
            pass
    monkeypatch.setattr(K, "ProcMem", M)
    out = K._deep_worker((1, [(0x10000, len(noise))], {"message/message_0.db": (p1[:16], p1)}, 8))
    assert out == {"message/message_0.db": k.hex()}


# ---------------- 解析 ----------------
def test_parse_types_and_cards():
    assert room_cards(ext_buffer({"wxid_a": "阿A", "wxid_b": "小b"})) == {"wxid_a": "阿A", "wxid_b": "小b"}
    z = zstandard.ZstdCompressor().compress("长消息".encode() * 50)
    assert unpack_text(z) == "长消息" * 50

    def row(content, t=1, sender=3, source=None):
        return {"local_type": t, "message_content": content, "compress_content": None, "real_sender_id": sender,
                "local_id": 1, "server_id": 9, "sort_seq": 1, "create_time": 1, "source": source}

    class R(dict):
        def keys(self):
            return list(super().keys())
    n2i = {3: "wxid_a"}
    m = parse_row(ROOM, R(row("wxid_a:\n@小满\u2005你好", source="<msgsource><atuserlist><![CDATA[wxid_her]]></atuserlist></msgsource>")),
                  "wxid_her", n2i)
    assert m.sender == "wxid_a" and m.text == "@小满\u2005你好" and m.ats == ["wxid_her"]
    q = ("wxid_a:\n<msg><appmsg><title>真的吗</title><type>57</type><refermsg><type>1</type><svrid>777</svrid>"
         "<chatusr>wxid_her</chatusr><content>我今天画完了</content></refermsg></appmsg></msg>")
    m = parse_row(ROOM, R(row(zstandard.ZstdCompressor().compress(q.encode()), t=(57 << 32) | 49)), "wxid_her", n2i)
    assert m.quote_id == 777 and m.text == "真的吗" and m.quote_sender == "wxid_her"
    m = parse_row(ROOM, R(row('wxid_a:\n<msg><emoji md5="abc" cdnurl="http://x/e?a=1&amp;b=2"/></msg>', t=47)),
                  "wxid_her", n2i)
    assert m.text == "[表情包]" and m.emoji_md5 == "abc" and m.emoji_url == "http://x/e?a=1&b=2"
    m = parse_row(ROOM, R(row('<sysmsg type="pat"><pat><fromusername>wxid_a</fromusername>'
                              '<pattedusername>wxid_her</pattedusername></pat></sysmsg>', t=10000, sender=0)),
                  "wxid_her", n2i)
    assert m.pat_target == "wxid_her" and m.sender == "wxid_a"
    m = parse_row(ROOM, R(row('"阿A"邀请"小c"加入了群聊', t=10000, sender=0)), "wxid_her", n2i)
    assert m.text == ""


def _account(tmp_path):
    acc = FakeAccount(tmp_path)
    acc.add_room(ROOM, "画画群", {OWNER_W: "阿远", FRIEND_W: "王哥", FakeAccount.SELF: "小满"},
                 cards={FRIEND_W: "论文战士", FakeAccount.SELF: "满满"})
    acc.add_room("999@chatroom", "别的群", {OWNER_W: "阿远"})
    acc.add_msg(ROOM, OWNER_W, "老消息")
    acc.publish()
    return acc


def test_wechat_data_reads_groups_and_messages(tmp_path):
    acc = _account(tmp_path)
    d = WeChatData(acc.acc, acc.keys, tmp_path / "work")
    assert d.self_wxid == "wxid_her" and not d.missing_keys()
    d.refresh_all()
    d.load_contacts(force=True)
    gs = {g["id"]: g for g in d.groups()}
    assert gs[ROOM]["name"] == "画画群" and gs[ROOM]["members"] == 3 and gs[ROOM]["has_msgs"]
    assert d.display(FRIEND_W, ROOM) == "论文战士" and d.display(OWNER_W, ROOM) == "阿远"
    assert d.self_names(ROOM) == ["满满", "小满"]
    seq = d.max_seq(ROOM)
    acc.add_msg(ROOM, FRIEND_W, "新的一句")
    assert d.refresh_all()
    ms = d.new_messages(ROOM, seq)
    assert [(m.sender, m.text) for m in ms] == [(FRIEND_W, "新的一句")]


# ---------------- 收发闭环（接 GroupChat） ----------------
class FakeSender:
    """装成往微信里粘贴：直接往假库里写一条“她发的”消息。"""
    search_fallback = True

    def __init__(self, acc, fail=False):
        self.acc, self.fail, self.sent = acc, fail, []
        self.titles: dict = {}

    def paste_send(self, name, text=None, files=None, allow_search=True, title=None):
        self.sent.append((name, text, files))
        self.allow_search = allow_search
        if not self.fail:
            if files:
                self.acc.add_msg(ROOM, FakeAccount.SELF, '<msg><img md5="x"/></msg>', mtype=3)
            else:
                self.acc.add_msg(ROOM, FakeAccount.SELF, text)
        return "window"


def _bot(tmp_path, acc, monkeypatch, **opts):
    from aigroup.wechat import bot as B
    monkeypatch.setattr(B, "open_data", lambda o, root, **k: (WeChatData(acc.acc, acc.keys, tmp_path / "work"), []))
    b = B.WeChatBot({"groups": [ROOM], "poll_seconds": 0.05, "confirm_seconds": 1.5, **opts}, tmp_path)
    b.sender = FakeSender(acc)
    return b


def test_bot_events_mentions_poke_and_join(tmp_path, monkeypatch):
    acc = _account(tmp_path)
    b = _bot(tmp_path, acc, monkeypatch)
    b._init_blocking()
    b.connected.set()
    assert b.ready and b.self_id == b.ids.id("wxid_her") and b.gid(ROOM) > 10000
    assert b._poll_locked() == []                                   # 启动前的老消息不补
    acc.add_msg(ROOM, FRIEND_W, "@满满\u2005今晚画什么", source="<msgsource><atuserlist>wxid_her</atuserlist></msgsource>")
    acc.add_msg(ROOM, OWNER_W, "@论文战士 你论文呢", source="<msgsource><atuserlist>wxid_friend</atuserlist></msgsource>")
    acc.add_msg(ROOM, FakeAccount.SELF, "我自己说的")
    acc.add_msg(ROOM, FRIEND_W, '<sysmsg type="pat"><pat><fromusername>wxid_friend</fromusername>'
                '<pattedusername>wxid_her</pattedusername></pat></sysmsg>', mtype=10000, prefix=False)
    acc.add_msg("999@chatroom", OWNER_W, "别的群的话")
    evs = b._poll_locked()
    from aigroup.onebot import flatten
    own = [e for e in evs if e["post_type"] == "message_sent"]          # 不是程序发的：当成她在手机上说的
    evs = [e for e in evs if e["post_type"] != "message_sent"]
    assert len(evs) == 3 and len(own) == 1 and own[0]["user_id"] == b.self_id
    f = flatten(evs[0]["message"], b.self_id)
    assert f.mentioned and f.text == "今晚画什么" and evs[0]["sender"]["card"] == "论文战士"
    f = flatten(evs[1]["message"], b.self_id, {str(b.ids.id(FRIEND_W)): "论文战士"})
    assert not f.mentioned and f.ats == [str(b.ids.id(FRIEND_W))] and f.text == "@论文战士 你论文呢"
    assert evs[2]["notice_type"] == "notify" and evs[2]["target_id"] == b.self_id
    assert [m.text for m in b._self_seen] == ["我自己说的"]
    # 新人进群：群成员表变了
    acc.add_room(ROOM, "画画群", {OWNER_W: "阿远", FRIEND_W: "王哥", FakeAccount.SELF: "小满", NEW_W: "小新"})
    acc.publish()
    b._last_contacts = 0
    evs = b._poll_locked()
    assert evs == [{"post_type": "notice", "notice_type": "group_increase", "group_id": b.gid(ROOM),
                    "user_id": b.ids.id(NEW_W)}]


def test_send_confirms_in_db_and_group_chat_roundtrip(tmp_path, monkeypatch, cfg):
    from aigroup.brain import GroupBrain
    from aigroup.chat.group import GroupChat
    acc = _account(tmp_path)
    b = _bot(tmp_path, acc, monkeypatch)

    async def go():
        b.start()
        for _ in range(100):
            await asyncio.sleep(0.05)
            if b.ready:
                break
        assert b.ready
        gid = b.gid(ROOM)
        # 发：@ 写成名字，库里核对到了才算
        mid = await b.send_group(gid, [{"type": "at", "data": {"qq": str(b.ids.id(FRIEND_W))}},
                                       {"type": "text", "data": {"text": " 快写论文"}}])
        assert b.sender.sent[-1] == ("画画群", "@论文战士 快写论文", None) and mid and mid > 0
        assert b.sender.allow_search and b.data.ambiguous(ROOM) == []
        assert (await b.call("get_group_info", group_id=gid))["group_name"] == "画画群"
        gl = await b.call("get_group_list")
        assert {g["group_name"] for g in gl} == {"画画群", "别的群"}
        # 没在库里看到：算失败
        b.sender.fail = True
        with pytest.raises(Exception, match="没看到"):
            await b.send_group(gid, [{"type": "text", "data": {"text": "这句不会出现"}}])
        b.sender.fail = False
        # 接上 GroupChat：被 @ → 她回一句 → 真的“发”进了库
        cfg["wechat"]["groups"] = [ROOM]
        brain = GroupBrain(cfg)
        brain.llm = FakeLLM("在呢在呢")
        gc = GroupChat(cfg, brain, b, b.ids.id(OWNER_W), [gid], [])
        b.handlers.append(gc.on_event)
        n = len(b.sender.sent)
        acc.add_msg(ROOM, OWNER_W, "@满满\u2005在吗", source="<msgsource><atuserlist>wxid_her</atuserlist></msgsource>")
        for _ in range(200):
            await asyncio.sleep(0.05)
            if len(b.sender.sent) > n:
                break
        await asyncio.sleep(0.3)
        assert b.sender.sent[n][1] == "在呢在呢"
        rows = brain.db.conn.execute("SELECT role, content FROM qq_group_messages ORDER BY id").fetchall()
        assert [tuple(r) for r in rows][-2:] == [("user", "@你 在吗"), ("assistant", "在呢在呢")]
        await b.stop()
    asyncio.run(go())


def test_wait_mark_excludes_old_self_messages(tmp_path, monkeypatch):
    """同一句话之前发过一次，第二次发失败时不能拿旧的那条冒充成功。"""
    acc = _account(tmp_path)
    b = _bot(tmp_path, acc, monkeypatch)

    async def go():
        b.start()
        for _ in range(100):
            await asyncio.sleep(0.05)
            if b.ready:
                break
        gid = b.gid(ROOM)
        await b.send_group(gid, [{"type": "text", "data": {"text": "哈哈"}}])
        b.sender.fail = True
        t0 = time.time()
        with pytest.raises(Exception, match="没看到"):
            await b.send_group(gid, [{"type": "text", "data": {"text": "哈哈"}}])
        assert time.time() - t0 >= b.confirm_timeout
        await b.stop()
    asyncio.run(go())


def test_image_send_and_ambiguous_names(tmp_path, monkeypatch):
    import base64
    acc = _account(tmp_path)
    b = _bot(tmp_path, acc, monkeypatch)

    async def go():
        b.start()
        for _ in range(100):
            await asyncio.sleep(0.05)
            if b.ready:
                break
        gif = b"GIF89a" + os.urandom(40)
        mid = await b.send_group(b.gid(ROOM), [{"type": "image", "data": {"file": "base64://" + base64.b64encode(gif).decode()}}])
        name, text, files = b.sender.sent[-1]
        assert text is None and files[0].endswith(".gif") and mid
        assert not os.path.exists(files[0])                      # 临时文件发完删掉
        await b.stop()
    asyncio.run(go())
    acc.add_contact("wxid_x", "画画群的朋友")
    acc.publish()
    d = WeChatData(acc.acc, acc.keys, tmp_path / "w2")
    d.load_contacts(force=True)
    assert d.ambiguous(ROOM) == ["画画群的朋友"]


def test_dbview_wal_with_nonstandard_checksums(tmp_path):
    """wal 帧的校验和不是标准算法时，退回只靠 salt + 每页 HMAC，照样能读。"""
    import shutil

    from wxfake import encrypt_wal
    key = os.urandom(32)
    f = FakeWeChatDB(tmp_path / "p.db", tmp_path / "enc" / "m.db", key)
    f.executescript("CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT);")
    f.checkpoint()
    f.execute("INSERT INTO t(v) VALUES ('x')")
    f.publish()
    shutil.copyfile(str(tmp_path / "p.db") + "-wal", tmp_path / "w.tmp")
    encrypt_wal(tmp_path / "w.tmp", str(tmp_path / "enc" / "m.db") + "-wal", key, f.salt, recompute_ck=False)
    v = DBView(tmp_path / "enc" / "m.db", key, tmp_path / "w")
    v.refresh()
    c = v.connect()
    assert c.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 1 and not v.check_ck
    c.close()
