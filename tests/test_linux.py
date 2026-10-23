"""Linux 版微信（服务器 / Docker）：读进程内存、认窗口、xdotool 发消息。不需要真的微信。"""
import os
import sys

import pytest

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="只在 Linux 上测")


def test_procmem_reads_own_memory():
    from aigroup.wechat import linux
    secret = bytearray(os.urandom(32))
    import ctypes
    addr = ctypes.addressof((ctypes.c_char * 32).from_buffer(secret))
    m = linux.ProcMem(os.getpid())
    try:
        assert m.read(addr, 32) == bytes(secret)
        regs = list(m.regions(private_only=True))
        assert regs and any(a <= addr < a + n for a, n in regs)      # 堆 / 匿名内存都在
    finally:
        m.close()


def test_wanted_and_enable(monkeypatch):
    from aigroup.wechat import keys, linux, wxui
    assert not linux.wanted({})
    assert linux.wanted({"container": "wechat"}) and linux.wanted({"linux": True})
    monkeypatch.setattr(keys, "ProcMem", keys.ProcMem)
    monkeypatch.setattr(keys, "weixin_procs", keys.weixin_procs)
    monkeypatch.setattr(keys, "file_users", keys.file_users)
    for n in ("windows", "idle_seconds", "screen_locked", "Sender", "UIError"):
        monkeypatch.setattr(wxui, n, getattr(wxui, n))
    monkeypatch.setattr(linux, "ENABLED", False)
    linux.enable({"container": "wechat"})
    assert linux.active() and keys.ProcMem is linux.ProcMem and wxui.Sender is linux.XSender
    assert wxui.idle_seconds() > 1000 and not wxui.screen_locked()


class FakeX:
    def __init__(self, wins):
        self.wins, self.calls, self.active = wins, [], None
        self.clip = None

    def __call__(self, opts, args, data=None, timeout=10.0):
        self.calls.append((args, data))
        if args[0] == "sh":
            return "".join(f"{w}\t0\t0\t800\t600\t{t}\n" for w, t in self.wins.items())
        if args[:2] == ["xdotool", "windowactivate"]:
            self.active = args[-1]
        if args[:2] == ["xdotool", "getactivewindow"]:
            return f"{self.active}\n"
        if args[0] == "xclip":
            self.clip = (args, data)
        return ""


def test_xsender_pastes_into_group_window(monkeypatch):
    from aigroup.wechat import linux
    fx = FakeX({11: "微信", 22: "周末K歌群", 33: "小窗"})
    monkeypatch.setattr(linux, "xrun", fx)
    s = linux.XSender({"container": "wechat"})
    assert s.paste_send("周末K歌群", "大家好") == "window"
    keys = [a for a, _ in fx.calls if a[:2] == ["xdotool", "key"]]
    assert keys[0][-2:] == ["ctrl+a", "ctrl+v"] and keys[-1][-1] == "Return"
    assert fx.clip[1] == "大家好".encode() and fx.active == "22"
    with pytest.raises(linux.XError, match="单独窗口"):
        s.paste_send("没这个群", "x")                                  # 默认不用搜索方式


def test_xsender_files_path_map(monkeypatch):
    from aigroup.wechat import linux
    fx = FakeX({22: "群A"})
    monkeypatch.setattr(linux, "xrun", fx)
    s = linux.XSender({"path_map": {"/root/xiaoman/wechat/data": "/root/xwechat_files"}})
    s.paste_send("群A", files=["/root/xiaoman/wechat/data/_out/a.png"])
    args, data = fx.clip
    assert "text/uri-list" in args and data.decode().startswith("file:///root/xwechat_files/_out/a.png")


def test_prefix():
    from aigroup.wechat.linux import _prefix
    assert _prefix({}) == []
    assert _prefix({"container": "wechat"})[:3] == ["docker", "exec", "-i"] and _prefix({"container": "wechat"})[-1] == "wechat"
    assert _prefix({"x_exec": "ssh box"}) == ["ssh", "box"]


def test_file_users_matches_by_account_tail(tmp_path, monkeypatch):
    from aigroup.wechat import linux
    f = tmp_path / "acc_1" / "db_storage" / "contact" / "contact.db"
    f.parent.mkdir(parents=True)
    f.write_bytes(b"x")
    fh = open(f, "rb")
    try:
        me = os.getpid()
        monkeypatch.setattr(linux, "weixin_procs", lambda names=None: [(me, 1, 3)])
        assert linux.file_users([tmp_path / "acc_1" / "db_storage" / "contact" / "contact.db"]) == [me]
        assert linux.file_users([tmp_path / "acc_2" / "db_storage" / "contact" / "contact.db"]) == []
    finally:
        fh.close()


def test_dbview_skips_preallocated_zero_pages(tmp_path):
    """Linux 版微信一建库就把文件占到 25MB（后面全是 0）：空页不验、不重读，不能一页一页慢慢等。"""
    import time as _t

    from aigroup.wechat.crypto import PAGE
    from aigroup.wechat.dbview import DBView

    from wxfake import FakeWeChatDB
    key = os.urandom(32)
    f = FakeWeChatDB(tmp_path / "p.db", tmp_path / "enc" / "m.db", key)
    f.executescript("CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT);")
    for i in range(50):
        f.execute("INSERT INTO t(v) VALUES (?)", ("x" * 60 + str(i),))
    f.checkpoint()
    f.publish()
    enc = tmp_path / "enc" / "m.db"
    with open(enc, "ab") as fh:
        fh.write(b"\0" * PAGE * 400)
    v = DBView(enc, key, tmp_path / "w")
    t0 = _t.time()
    v.refresh()
    assert _t.time() - t0 < 5
    c = v.connect()
    try:
        assert c.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 50
    finally:
        c.close()
