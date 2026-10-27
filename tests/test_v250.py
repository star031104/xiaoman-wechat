"""25.0：承诺对账、说查到要有记录、像自己以前、照抄、接错人、聊天记录分块滑窗、聊天记录写称呼。"""
import asyncio
import time

from conftest import FRIEND, FRIEND2, GROUP, SELF, _gc


def test_promise_words():
    from aigroup.chat.promise import DONE_RX, is_bare_promise, is_promise
    for t in ["我这就去看看啊", "还没核完，我翻翻再给你准数", "我瞅眼赛程去", "我去翻翻赛程", "我戳下赛程看看", "等我查一下"]:
        assert is_promise(t) and is_bare_promise(t), t
    for t in ["你去看看医生", "看看再说吧", "TES这场我没底，别问我站谁", "哪四场，报个名字我照着核"]:
        assert not is_promise(t), t
    assert DONE_RX.search("核好了，四场都在今晚") and not DONE_RX.search("看完了，结尾不错")


def test_ledger_records_and_fulfils(cfg, brain):
    gc, _ = _gc(cfg, brain)
    now = time.time()
    gc._record(GROUP, FRIEND, "小王", "@你 明天凌晨四场再预测一下", 1, to_her=True, ts=now - 30)
    gc.note_sent(GROUP, ["我这就去看看啊"], now=now - 20)
    op = gc.open_promises(GROUP)
    assert len(op) == 1 and op[0]["target_uid"] == FRIEND and "预测" in op[0]["question"]
    gc.note_sent(GROUP, ["还没核完，我翻翻再给你准数", "别急，这回不瞎押了"], now=now - 10)
    assert len(gc.open_promises(GROUP)) == 1                       # 又说一遍“我翻翻”：不算兑现，也不重复记
    gc.note_sent(GROUP, ["T1和BLG我跟着热门走，AL我赌它续命"], now=now)
    assert gc.open_promises(GROUP) == []                            # 给了结果：销账


def test_promise_tick_follows_up_once(cfg, brain):
    gc, ob = _gc(cfg, brain, reply="刚才说去翻，结果没翻到靠谱的，这四场我不瞎押了")
    now = time.time()
    gc._record(GROUP, FRIEND, "小王", "@你 明天凌晨四场再预测一下", 1, to_her=True, ts=now - 900)
    gc.note_sent(GROUP, ["我这就去看看啊"], now=now - 890)
    st = gc._st(GROUP)
    assert asyncio.run(gc.promise_tick(GROUP, st, now)) is True
    assert ob.sent, "到点没给结果：要补一句"
    p = gc.db.conn.execute("SELECT state FROM qq_promises").fetchone()
    assert p["state"] == "followed"
    assert asyncio.run(gc.promise_tick(GROUP, st, now + 60)) is False      # 补过一次就不再补


def test_promise_expires(cfg, brain):
    gc, ob = _gc(cfg, brain)
    now = time.time()
    gc._record(GROUP, FRIEND, "小王", "@你 帮我查下", 1, to_her=True, ts=now - 3 * 3600 + 60)
    gc.note_sent(GROUP, ["我去查一下"], now=now - 2 * 3600)
    assert asyncio.run(gc.promise_tick(GROUP, gc._st(GROUP), now)) is False
    assert gc.db.conn.execute("SELECT state FROM qq_promises").fetchone()["state"] == "expired"


def test_claim_checked_needs_source(cfg, brain):
    gc, _ = _gc(cfg, brain)
    now = time.time()
    assert gc._promise_check(GROUP, "核好了，01:00 CFO打T1", "", now)
    assert gc._promise_check(GROUP, "核好了，01:00 CFO打T1", "# 你刚在手机上查了一下：赛程\n- （什么都没查到）", now)
    assert not gc._promise_check(GROUP, "核好了，01:00 CFO打T1", "# 你刚在手机上查了一下：赛程\n- 01:00 CFO 对 T1", now)


def test_copy_check(cfg, brain):
    gc, _ = _gc(cfg, brain)
    gc.note_shown(GROUP, ["这周末去爬山的话记得带够水和吃的", "笑死我了"])
    assert gc._copied(GROUP, "周末去爬山的话记得带够水")
    assert not gc._copied(GROUP, "带点水吧，山上买很贵")
    assert not gc._copied(GROUP, "他原话是“这周末去爬山的话记得带够水和吃的”")     # 引号里是在引用


def test_chunked_transcript_keeps_prefix(cfg, brain):
    gc, _ = _gc(cfg, brain)
    gc.g["context_chunk"] = 15
    now = time.time()
    for i in range(50):
        gc._record(GROUP, FRIEND, "小王", f"第{i}条消息内容", 100 + i, ts=now - 3000 + i * 10)
    a = gc.transcript(GROUP)
    gc._record(GROUP, FRIEND, "小王", "第50条消息内容", 200, ts=now - 2400)
    b = gc.transcript(GROUP)
    assert b.startswith(a.split("\n")[0]) and b.split("\n")[0] == a.split("\n")[0]
    n = len([x for x in b.splitlines() if "消息内容" in x])
    assert 30 <= n < 45
    gc.g["context_chunk"] = 0
    assert len([x for x in gc.transcript(GROUP).splitlines() if "消息内容" in x]) == 30


def test_speaker_label_uses_call_name(cfg, brain):
    gc, _ = _gc(cfg, brain)
    gc._record(GROUP, FRIEND, "小王🥝", "我今天发烧了", 1)
    gc.call_name = lambda gid, uid: "王哥" if uid == FRIEND else ""
    gc.names_off = lambda gid: False
    assert "王哥（小王" in gc.transcript(GROUP)


def test_self_pity_family():
    from aigroup.chat.checks import PHRASE_FAMILIES
    rx = PHRASE_FAMILIES["自怜"]
    for t in ["你们仨都高级，就我一个打工的", "就我光秃秃一个小满", "我一个小AI哪敢跟你老板说话", "那我这妹妹也失宠了呗"]:
        assert rx.search(t), t
    assert SELF  # conftest 里的她


def test_misattrib(cfg, brain):
    gc, _ = _gc(cfg, brain)
    now = time.time()
    gc._record(GROUP, FRIEND, "小王", "37.3都快正常体温了", 1, ts=now - 100)
    gc._record(GROUP, FRIEND2, "老张", "37.3那是低烧啊", 2, ts=now - 20)
    assert "小王" in gc._misattrib(GROUP, "37.3还是有点烧，老张你别拿它当正常体温", now)
    assert not gc._misattrib(GROUP, "小王你别拿它当正常体温", now)
