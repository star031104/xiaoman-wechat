"""2.2.1 一台电脑开着两个微信（你的大号 + 她的小号）：认出哪个是她的，只用她那个微信的窗口。"""
import pytest

from aigroup.wechat import keys, wxui

# 两个微信：大号 100（子进程 101、102），小号 200（子进程 201）；小号是从大号里启动的（父进程是 100）
PROCS = [(100, 1, 90), (101, 100, 10), (102, 100, 8), (200, 100, 80), (201, 200, 9)]


@pytest.fixture
def two(tmp_path, monkeypatch):
    big, small = tmp_path / "wxid_big_aaaa", tmp_path / "wxid_small_bbbb"
    for a in (big, small):
        (a / "db_storage" / "contact").mkdir(parents=True)
        (a / "db_storage" / "contact" / "contact.db").write_bytes(b"x")
    holders = {str(big): [100], str(small): [200]}

    def users(paths):
        return [p for x in paths for k, v in holders.items() if str(x).startswith(k) for p in v]
    monkeypatch.setattr(keys, "file_users", users)
    monkeypatch.setattr(keys, "weixin_procs", lambda: PROCS)
    return big, small, holders


def test_owner_pids_split_two_instances(two):
    big, small, holders = two
    assert keys.owner_pids(small, PROCS) == [200, 201]          # 父进程 100 开着大号的库：不算她的
    assert keys.owner_pids(big, PROCS) == [100, 101, 102]
    holders[str(small)] = []
    assert keys.owner_pids(small, PROCS) == []                  # 小号没登录
    holders[str(small)] = [201]                                  # 库在子进程里：往上找到 200（没开着别的号）
    assert keys.owner_pids(small, PROCS) == [200, 201]


def test_owner_pids_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr(keys, "file_users", lambda paths: None)
    assert keys.owner_pids(tmp_path, PROCS) is None


def test_instances():
    assert keys.instances([(1, 0, 50), (2, 1, 3), (9, 0, 40)]) == [[1, 2], [9]]


def _win(h, title, pid):
    return {"hwnd": h, "title": title, "cls": "x", "pid": pid, "rect": (0, 0, 800, 600), "visible": True, "iconic": False}


def test_sender_only_uses_her_wechat(monkeypatch):
    ws = [_win(1, "微信", 100), _win(2, "摸鱼群", 100), _win(3, "微信", 200), _win(4, "摸鱼群", 200)]
    monkeypatch.setattr(wxui, "windows", lambda pids=None: [w for w in ws if pids is None or w["pid"] in pids])
    s = wxui.Sender({})
    s.own = lambda: [200, 201]
    w, how = s.target("摸鱼群")
    assert w["hwnd"] == 4 and how == "window"                    # 小号那个微信的窗口，不是大号的
    ws.remove(ws[3])
    w, how = s.target("摸鱼群")
    assert how in ("", "search") and (w is None or w["pid"] == 200)   # 大号开着这个群也不用它
    s.own = lambda: []
    with pytest.raises(wxui.UIError):
        s.target("摸鱼群")                                      # 小号没登录：不发
    ws.append(_win(4, "摸鱼群", 200))
    s.own = lambda: None                                         # 认不出：两个同名窗口就拒发
    with pytest.raises(wxui.UIError):
        s.target("摸鱼群")
    ws.pop()
    w, how = s.target("摸鱼群")
    assert w["hwnd"] == 2 and how == "window"
    ws.remove(ws[1])
    assert s.target("没有的群") == (None, "")                    # 两个主窗口又认不出：不用搜索


def test_wizard_pick_account(monkeypatch, tmp_path):
    from aigroup import wizard
    from aigroup.wechat import bot

    class D:
        def __init__(self, wxid, nick):
            self.self_wxid, self.contacts, self.closed = wxid, {wxid: {"nick": nick}}, False
            self.account_dir = tmp_path / wxid

        def refresh_all(self):
            pass

        def load_contacts(self, force=False):
            pass

        def close(self):
            self.closed = True
    ds = {"big_aaaa": D("big", "阿远"), "small_bbbb": D("small", "小满")}
    monkeypatch.setattr(bot, "accounts_here", lambda o: [
        {"account": "big_aaaa", "wxid": "big", "dir": tmp_path, "logged": True},
        {"account": "small_bbbb", "wxid": "small", "dir": tmp_path, "logged": True}])
    monkeypatch.setattr(bot, "open_data", lambda o, root: (ds[o["account"]], []))
    gone = []
    monkeypatch.setattr(bot, "forget_account", lambda root, acc: gone.append(acc))
    monkeypatch.setattr(wizard, "ask", lambda q, d="": "2")
    others = []
    d = wizard._pick_account({}, "", others)
    assert d is ds["small_bbbb"] and others == ["big"] and gone == ["big_aaaa"] and ds["big_aaaa"].closed
    # 只开着一个：直接用它
    monkeypatch.setattr(bot, "accounts_here", lambda o: [
        {"account": "big_aaaa", "wxid": "big", "dir": tmp_path, "logged": False},
        {"account": "small_bbbb", "wxid": "small", "dir": tmp_path, "logged": True}])
    o = {}
    assert wizard._pick_account(o, "", []) is None and o["account"] == "small_bbbb"


def test_forget_others(monkeypatch, tmp_path):
    import json

    from aigroup import wizard
    monkeypatch.setattr(wizard, "ROOT", tmp_path)
    w = tmp_path / "data" / "wechat"
    for a in ("big_aaaa", "small_bbbb"):
        (w / "cache" / a / "p1").mkdir(parents=True)
    (w / "keys.json").write_text(json.dumps({"big_aaaa": {"x": "00"}, "small_bbbb": {"x": "11"}}), encoding="utf-8")
    wizard._forget_others("small_bbbb")
    assert json.loads((w / "keys.json").read_text(encoding="utf-8")) == {"small_bbbb": {"x": "11"}}
    assert not (w / "cache" / "big_aaaa").exists() and (w / "cache" / "small_bbbb").exists()


def test_wxexport(monkeypatch, tmp_path):
    from types import SimpleNamespace as NS

    from aigroup.core import util
    from aigroup.wechat import bot, diag
    msgs = [NS(sender="me", ts=1700000000, text="在吗", raw=""), NS(sender="a", ts=1700000060, text="在\n干嘛", raw="")]

    class D:
        self_wxid = "me"
        contacts = {"a": {"nick": "阿明", "remark": "小明"}}
        closed = False

        def refresh_all(self):
            pass

        def load_contacts(self, force=False):
            pass

        def groups(self):
            return [{"id": "1@chatroom", "name": "测试群", "members": 2, "has_msgs": True},
                    {"id": "2@chatroom", "name": "大群", "members": 400, "has_msgs": True}]

        def members(self, g):
            return ["me", "a"]

        def display(self, u, g=""):
            return {"a": "张小明"}.get(u, u)

        def new_messages(self, g, since, limit=0):
            return msgs

        def close(self):
            D.closed = True
    monkeypatch.setattr(diag, "ROOT", tmp_path)
    monkeypatch.setattr(diag, "_wcfg", lambda: {"account": "small_bbbb"})
    monkeypatch.setattr(bot, "accounts_here", lambda w: [{"account": "big_aaaa", "wxid": "big", "logged": True},
                                                        {"account": "small_bbbb", "wxid": "small", "logged": True}])
    monkeypatch.setattr(bot, "open_data", lambda o, root, progress=None: (D(), []))
    gone = []
    monkeypatch.setattr(bot, "forget_account", lambda root, a: gone.append(a))
    answers = iter(["", "1"])                                     # 默认选大号；导第 1 个群
    monkeypatch.setattr(util, "ask", lambda q, d="": next(answers) or d)
    assert diag.export_cmd([]) == 0
    f = next((tmp_path / "data" / "export").rglob("*.txt"))
    t = f.read_text(encoding="utf-8")
    assert "# 测试群" in t and "我：在吗" in t and "张小明：在 ⏎ 干嘛" in t and "备注 小明" in t and "大群" not in t
    assert gone == ["big_aaaa"] and D.closed
