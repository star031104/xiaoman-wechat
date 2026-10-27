"""30.2：前提只在真错了时纠；只有一家来源的说法标出来；“纠一下”一天一次；“原文在这”却没收到东西要直说。"""
import time

from conftest import FRIEND, GROUP, _gc

from aigroup.agent.mixin import brief_block
from aigroup.chat.checks import FAILURE_MODES, PHRASE_FAMILIES


def test_single_source_marked():
    d = {"claims": [{"text": "图是节目组配的", "confidence": "high", "sources": [{"site": "bbc.com"}]},
                    {"text": "三语道歉", "confidence": "high", "sources": [{"site": "a.com"}, {"site": "b.com"}]}]}
    out = brief_block("x", d)
    assert "图是节目组配的（只有一家这么说）" in out and "三语道歉（只有一家" not in out


def test_correct_family_and_mode():
    rx = PHRASE_FAMILIES["纠一下"]
    assert rx.search("先纠一下，不是一场") and rx.search("但得纠一下") and rx.search("先纠个时间")
    assert not rx.search("我还在纠结")
    assert "说教" in FAILURE_MODES[18]


def test_pointer_missing(cfg, brain):
    gc, _ = _gc(cfg, brain)
    now = time.time()
    assert gc.pointer_missing(GROUP, [(1, FRIEND, "@你 原文在这")], now)
    gc._record(GROUP, FRIEND, "小王", "[图片：截图，一份声明]", 1)
    assert not gc.pointer_missing(GROUP, [(1, FRIEND, "@你 原文在这")], now)
    assert not gc.pointer_missing(GROUP, [(1, FRIEND, "我在这呢")], now)


def test_seen_missing_rule(cfg, brain):
    gc, _ = _gc(cfg, brain)
    now = time.time()
    gc._record(GROUP, FRIEND, "小王", "@你 原文在这", 1)
    assert "没收到" in gc._seen_missing(GROUP, "原文我看了，是主席个人号发的", now)
    assert gc._seen_missing(GROUP, "没刷出来，你再发一下？", now) == ""
    gc._record(GROUP, FRIEND, "小王", "[图片：截图，一份声明]", 2)
    assert gc._seen_missing(GROUP, "原文我看了", now) == ""


def test_sleep_nag_family():
    rx = PHRASE_FAMILIES["催睡"]
    assert rx.search("大半夜的你先睡") and rx.search("别再刷了，去睡") and not rx.search("小王睡醒没")
