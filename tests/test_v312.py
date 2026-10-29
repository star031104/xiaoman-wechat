"""31.2 按 10-27～10-29 日报：刷屏刹车改硬 + 发前再数、查资料先回一句挪到 60 秒后、掉线回来旧话只看不回、
引着原话质疑也算纠正、没打开的链接别说看了、刚说过的别再说、她当天说过的判断给自检和研究员、几处小规则。"""
import asyncio
import time
from datetime import datetime

from conftest import FRIEND, FRIEND2, GROUP, SELF, _gc

from aigroup.chat.plan import BARE_ANSWER, burst_room, decide

G = {"max_per_10min": 30, "cooldown_seconds": 10, "burst_max": 5, "burst_soft": 3}


def _d(**kw):
    base = dict(mentioned=False, now=1000.0, last_reply_ts=0.0, replies_10min=0, quiet=False, g=dict(G), p=0.9, thr=0.1)
    base.update(kw)
    return decide(**base)


def test_soft_brake_is_hard_now():
    ok, why = _d(burst=3, waited=True)
    assert not ok and "没人叫" in why
    ok, _ = _d(burst=2, waited=True)
    assert ok
    ok, _ = _d(burst=4, mentioned=True)                        # 被叫到不受影响
    assert ok
    ok, _ = _d(burst=3, corrected=True)                        # 被纠正认一句不受影响
    assert ok
    ok, _ = _d(burst=3, g={**G, "burst_soft_hard": False}, waited=True)
    assert ok                                                  # 关掉就是老样子


def test_burst_room():
    assert burst_room(0, 2, False) == 2
    assert burst_room(2, 2, False) == 1                        # 3 分钟合起来最多 3 条
    assert burst_room(3, 2, False) == 0
    assert burst_room(5, 2, True) == 1                         # 被叫到至少留 1 条
    assert burst_room(1, 3, True) == 2


def test_bare_answer_is_not_asking_her():
    for t in ("没有", "嗯嗯", "对", "还没", "没追", "好的"):
        assert BARE_ANSWER.match(t), t
    for t in ("你追了没", "没有你说的那么夸张吧？", "你觉得呢"):
        assert not BARE_ANSWER.match(t), t


def test_quoted_challenge(cfg, brain):
    gc, _ = _gc(cfg, brain)
    from aigroup.chat.checks import ChecksMixin
    qc = ChecksMixin.quoted_challenge
    assert "节目组" in qc([(1, FRIEND, "[回复你的「他原话没点名，把脸P上去是节目组干的」] ？")])
    assert qc([(1, FRIEND, "[回复你的「那场是26号踢的」] ？"), (2, FRIEND, "你再看一下呢")])
    assert not qc([(1, FRIEND, "[回复你的「今晚吃火锅」] 好啊我也想吃")])
    assert not qc([(1, FRIEND, "你再看一下呢")])               # 前面没引她的话
    # 隔了几个小时也算纠正（以前要她 3 分钟内说过话）
    gc._record(GROUP, SELF, "小满", "那张图是节目组配的", 1, role="assistant", ts=time.time() - 5 * 3600)
    assert gc.corrected_now(GROUP, [(2, FRIEND, "[回复你的「那张图是节目组配的」] ？")], time.time())
    assert not gc.corrected_now(GROUP, [(3, FRIEND, "[回复你的「那张图是节目组配的」] 哈哈哈")], time.time())


def test_standalone_not_x_is_correction():
    from aigroup.chat.checks import CORRECT_RX
    assert CORRECT_RX.search("不是客户经理")
    assert not CORRECT_RX.search("不是吧")
    assert not CORRECT_RX.search("不是我说你，你这也太能吃了")


def test_link_unopened(cfg, brain):
    gc, _ = _gc(cfg, brain)
    gc._record(GROUP, FRIEND, "小王", "[分享：一票直达冰岛！] [链接：B站的，你点开没打开（可能要登录或网络不通）]", 1)
    now = time.time()
    assert "没打开" in gc._link_unopened(GROUP, "我看了下，这是新开的航线", now)
    assert gc._link_unopened(GROUP, "直达冰岛？坐到腿麻", now) == ""
    gc._record(GROUP, FRIEND2, "老张", "[分享：长文] [链接：某站的网页「长文」；正文开头：今年……]", 2)
    assert gc._link_unopened(GROUP, "这篇写得挺好", now) == ""          # 最近那条打开了


def test_self_echo(cfg, brain):
    gc, _ = _gc(cfg, brain)
    gc._record(GROUP, SELF, "小满", "知道，以下克上那个手势", 1, role="assistant", ts=time.time() - 10)
    w = gc._self_echo(GROUP, "答过了，以下克上那个，还想验证我", True, time.time())
    assert "刚说过" in w and not w.startswith("〔")             # 被叫到：重写，不是直接不说
    assert gc._self_echo(GROUP, "答过了，以下克上那个，还想验证我", False, time.time()).startswith("〔不说〕")
    assert gc._self_echo(GROUP, "最早是下路选手比出来的", True, time.time()) == ""
    assert gc._self_echo(GROUP, "答过了，以下克上那个", True, time.time() + 300) == ""   # 过了窗口


def test_old_backlog(cfg, brain):
    gc, _ = _gc(cfg, brain)
    now = time.time()
    gc.db.set(f"qq_back_{GROUP}", {"ts": now - 60, "cut": now - 1860})
    old = {"role": "user", "content": "[回复你的「押KC」] 牛逼牛逼", "ts": now - 4 * 3600}
    assert gc._old_backlog(GROUP, old, False, now).startswith("〔不说〕")
    assert gc._old_backlog(GROUP, {**old, "content": "凌晨那场你看了没"}, False, now) == ""    # 在问她
    assert gc._old_backlog(GROUP, {**old, "ts": now - 120}, False, now) == ""                   # 新的
    assert gc._old_backlog(GROUP, old, True, now) == ""                                       # 被叫到
    gc.db.set(f"qq_back_{GROUP}", {"ts": now - 7200, "cut": now - 9000})
    assert gc._old_backlog(GROUP, old, False, now) == ""                                      # 回来很久了


def test_backlog_worth(cfg, brain):
    gc, _ = _gc(cfg, brain)
    now = time.time()
    assert gc._backlog_worth(GROUP, "牛逼牛逼", now - 600, now)            # 半小时内的都补
    assert not gc._backlog_worth(GROUP, "无敌了小满", now - 4 * 3600, now)
    assert gc._backlog_worth(GROUP, "@你 明天那场谁能赢", now - 4 * 3600, now)
    assert gc._backlog_worth(GROUP, "[回复你的「节目组干的」] ？", now - 4 * 3600, now)


def test_backlog_prompt_note(cfg, brain):
    gc, _ = _gc(cfg, brain)
    gc._record(GROUP, FRIEND, "小王", "hi", 1)
    now = time.time()
    gc.db.set(f"qq_back_{GROUP}", {"ts": now - 60, "cut": now - 1860})
    msgs = asyncio.run(gc.build_messages(GROUP, False, False, "", None))
    assert "才重新上线" in msgs[-1]["content"] or "才重新上线" in msgs[0]["content"]


def test_ask_repeat_ignores_names(cfg, brain):
    gc, _ = _gc(cfg, brain)
    names = gc._name_terms(GROUP)
    gc.note_asks(GROUP, ["小王凌晨的球赛你追了没"], FRIEND, now=time.time() - 3 * 3600)
    if "小王" in names:
        assert gc._ask_repeat(GROUP, "小王改寄账单地址？", FRIEND, time.time()) == ""
    assert "问过" in gc._ask_repeat(GROUP, "凌晨的球赛你追了没", FRIEND, time.time())


def test_ask_repeat_lets_them_raise_it(cfg, brain):
    gc, _ = _gc(cfg, brain)
    now = time.time()
    gc._record(GROUP, FRIEND, "小王", "别提面试了好吗压力很大的", 1, ts=now - 3600)
    if hasattr(gc, "note_closed"):
        pass
    w0 = gc._ask_repeat(GROUP, "面试是哪天", FRIEND, now)
    gc._record(GROUP, FRIEND, "小王", "我可能有面试又要回广州", 2, ts=now - 30)
    assert gc._ask_repeat(GROUP, "面试是哪天", FRIEND, now) == ""                # 他自己刚提起
    assert isinstance(w0, str)


def test_hush_words_and_echo(cfg, brain):
    gc, _ = _gc(cfg, brain)
    assert gc.HUH.search("催命鬼来了") and gc.HUH.search("听君一席话如听一席话")
    assert gc.QUIET.search("别提面试了好吗压力很大的")
    now = time.time()
    gc._record(GROUP, SELF, "小满", "这桌看着真不错，我饿了", 1, role="assistant", ts=now - 6)
    gc._st(GROUP).last_reply_ts = now - 6
    assert gc.echoes_her(GROUP, "这桌看着真不错，我饿了", now)
    assert gc.huh_signal(GROUP, FRIEND, "这桌看着真不错，我饿了", now)
    assert gc.hushed(GROUP, now)
    assert not gc.echoes_her(GROUP, "这桌看着不错", now)


def test_meta_version_talk():
    from aigroup.chat.social import META
    assert META.search("两个月，够我更新好几版了")
    assert not META.search("这游戏更新了新版本")


def test_claims_today_in_critic_and_research(cfg, brain):
    gc, _ = _gc(cfg, brain)
    gc._record(GROUP, SELF, "小满", "UR碰TEC被2-0扫了", 1, role="assistant")
    gc._record(GROUP, SELF, "小满", "今天天气不错", 2, role="assistant")
    c = gc.claims_today(GROUP)
    assert any("TEC" in x for x in c) and not any("天气" in x for x in c)
    note = gc._said_note(GROUP, "TEC 打 WSIG 谁能赢")
    assert "TEC" in note and "以查到的为准" in note
    assert gc._said_note(GROUP, "明天北京天气") == ""
    from aigroup.chat.checks import FAILURE_MODES
    assert "今天早些时候说过" in FAILURE_MODES[9]


def test_filler_waits_a_minute():
    from aigroup.chat.settings import DEFAULTS
    assert DEFAULTS["agent_filler_after"] >= 60


def test_note_deploy(tmp_path):
    import subprocess
    from aigroup.core.releases import note_deploy
    root = tmp_path
    subprocess.run(["git", "init", "-q"], cwd=root)
    (root / "CHANGELOG.md").write_text("# 更新记录\n\n## 31.2 — x\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=root)
    subprocess.run(["git", "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-qm", "x"], cwd=root)
    assert note_deploy(root) == ""                               # 没有 releases.log 的机器不动
    (root / "deploy").mkdir()
    (root / "deploy" / "releases.log").write_text("2026-10-28T11:16 30.3 82c94c8 hotfix\n", encoding="utf-8")
    line = note_deploy(root, datetime(2026, 10, 30, 3, 0))
    assert line.startswith("2026-10-30T03:00 31.2 ") and line.strip().endswith("上线")
    assert note_deploy(root) == ""                               # 已经记了这个提交


def test_raised_ignores_their_close(cfg, brain):
    gc, _ = _gc(cfg, brain)
    now = time.time()
    gc._record(GROUP, FRIEND, "小王", "别提面试了好吗压力很大的", 1, ts=now - 20)
    assert not gc._raised_by_them(GROUP, {"面试"}, now, FRIEND)
