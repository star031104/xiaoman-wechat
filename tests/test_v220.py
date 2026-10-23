"""22.0：亲历闸门、问过账、跨天套路、叫错人、时间感、损人风险、回应性、热聊别插、自述账本 / 立场 / 时间线、
群梗只由群友触发、账本别名、远距离点名、追问她的话一定接、每天的指标。"""
import asyncio
import time

from conftest import FRIEND, FRIEND2, GROUP, J, _gc


def test_self_claims_and_grounding():
    from aigroup.chat.checks import grounded, self_claims
    assert self_claims("我这边稿子还没交呢，先担心担心我") == ["我这边稿子还没交呢"]
    assert self_claims("采访稿啊，老师让我们去采访学校的食堂阿姨")
    assert self_claims("我这周蹲了三个食堂，阿姨都认识我了[捂脸]") == ["我这周蹲了三个食堂"]
    for t in ("我刚看了一下，是两点", "我觉得生椰拿铁不错", "我记下来了，七天以后再问", "我今天在群里说太多了"):
        assert not self_claims(t), t
    assert not grounded("我这边稿子还没交呢", "小满，喜欢打游戏、看 B 站")
    assert grounded("我昨天又熬夜打排位了", "她晚上爱打排位，经常熬夜")


def _say(gc, brain, text, **kw):
    brain.llm.reply = J(act="say", kind="I2", to=1, len="short", say=[text], move="接住")
    return asyncio.run(gc.speak(GROUP, mentioned=False, owner_spoke=False, trigger=None, who="", **kw))


def test_source_gate_rewrites_invented_life(cfg, brain):
    gc, ob = _gc(cfg, brain, cooldown_seconds=0)
    gc._record(GROUP, FRIEND, "小王", "ai 要来抢我们工作了", 1)
    calls = []
    orig = gc._rewrite

    async def rw(messages, raw, hits, gid):
        calls.append(hits)
        return [["那你们得先教会它摸鱼"]]
    gc._rewrite = rw
    assert _say(gc, brain, "我这边稿子还没交呢，先担心担心我")
    assert any("编了你自己没有的经历" in h for hs in calls for h in hs)
    assert "摸鱼" in str(ob.sent[-1][1])
    n = gc.db.conn.execute("SELECT COUNT(*) c FROM qq_check_log WHERE name='source_gate'").fetchone()["c"]
    assert n == 1
    gc._rewrite = orig


def test_shadow_mode_only_logs(cfg, brain):
    gc, ob = _gc(cfg, brain, cooldown_seconds=0, check_source_gate="shadow")
    gc._record(GROUP, FRIEND, "小王", "ai 要来抢我们工作了", 1)
    assert _say(gc, brain, "我这边稿子还没交呢")
    assert "稿子" in str(ob.sent[-1][1])
    r = gc.db.conn.execute("SELECT mode FROM qq_check_log WHERE name='source_gate'").fetchone()
    assert r["mode"] == "shadow"


def test_ask_ledger_repeat_and_closed(cfg, brain):
    gc, _ = _gc(cfg, brain)
    now = time.time()
    gc.note_asks(GROUP, ["AG 今天赢了没"], FRIEND, now - 3600)
    assert "已经问过" in gc._ask_repeat(GROUP, "AG 那场比分出来没？", FRIEND, now)
    assert gc._ask_repeat(GROUP, "晚上吃啥？", FRIEND, now) == ""
    gc.db.conn.execute("DELETE FROM qq_asks")
    gc.note_asks(GROUP, ["AG 今天赢了没"], FRIEND, now - 30 * 3600)
    assert gc._ask_repeat(GROUP, "AG 比分咋样？", FRIEND, now) == ""                 # 过了 24 小时：可以再问
    gc._record(GROUP, FRIEND, "小王", "能别鞭尸了吗", 5)
    gc.db.conn.execute("UPDATE qq_group_messages SET ts=? WHERE qq_msg_id=5", (now - 30 * 3600 + 60,))
    assert "别再问了" in gc._ask_repeat(GROUP, "AG 比分咋样？", FRIEND, now)          # 对方说过别问：一周都别提
    assert "别再问了" in gc.asks_block(GROUP)


def test_templates_across_days(cfg, brain):
    gc, _ = _gc(cfg, brain)
    now = time.time()
    for i, t in enumerate(["行行行，改成不催版", "行行行，改成不记仇版", "行行行，你说得对", "那我开启安静模式"]):
        gc._record(GROUP, 10001, "小满", t, 100 + i, role="assistant")
        gc.db.conn.execute("UPDATE qq_group_messages SET ts=? WHERE qq_msg_id=?", (now - (i % 2) * 86400 - 60, 100 + i))
    gc.db.conn.commit()
    tpl = gc.her_templates(GROUP, now=now)
    assert "行行行" in tpl and "改成…版 / …模式" in tpl
    assert "行行行" in gc._template_hit(GROUP, "行行行，听你的", now)
    assert "套路" in gc._template_hit(GROUP, "好，改成乖巧版", now)


def test_addressee_time_tease_responsive_room(cfg, brain):
    gc, _ = _gc(cfg, brain)
    gc._record(GROUP, FRIEND, "小王", "今天好累", 1)
    gc._record(GROUP, FRIEND2, "老张", "嗯嗯", 2)
    w = gc._addressee(GROUP, "老张你也太拼了", FRIEND, None)
    assert "叫错人" in w
    assert gc._addressee(GROUP, "小王你也太拼了", FRIEND, None) == ""
    assert "那句是" in gc._addressee(GROUP, "我觉得是周五", FRIEND, {"content": "老张你周几回来？"})
    old = {"ts": time.time() - 9 * 3600, "content": "我看完了", "role": "user"}
    assert "小时以前" in gc._time_sense("你刚说看完了？", old, time.time())
    assert gc._time_sense("看完感觉咋样", old, time.time()) == ""
    assert "好事" in gc._responsive("恭喜！", {"role": "user", "content": "我面试过了！"})
    assert gc._responsive("几面啊？", {"role": "user", "content": "我面试过了！"}) == ""
    gc._record(GROUP, FRIEND, "小王", "别损了我很烦", 3)
    assert "别损" in gc._tease_risk(GROUP, "笑死，你也有今天", FRIEND)
    now = time.time()
    for i in range(6):
        gc._record(GROUP, FRIEND if i % 2 == 0 else FRIEND2, "x", f"第{i}句", 10 + i)
        gc.db.conn.execute("UPDATE qq_group_messages SET ts=? WHERE qq_msg_id=?", (now - 60 + i * 10, 10 + i))
    gc.db.conn.commit()
    assert "一来一回" in gc.room_hot(GROUP, now)


def test_autobio_apply_and_blocks(cfg, brain):
    gc, _ = _gc(cfg, brain)
    lines = ["我|我养了一只叫芝麻的猫", "态|AG|我站 AG，输了也站", f"时|{brain.now():%m-%d}|小王面试过了", "时|13-40|坏日期"]
    ns, nt, ne = gc.autobio_apply(GROUP, lines, ["我养了一只叫芝麻的猫"])
    assert (ns, nt, ne) == (1, 1, 1)
    blk = gc.autobio_block(GROUP, "芝麻最近咋样，AG 今天打谁")
    assert "芝麻" in blk and "我站 AG" in blk
    assert "小王面试过了" in gc.timeline_block(GROUP)


def test_lore_only_human_triggered_and_stale(cfg, brain):
    gc, _ = _gc(cfg, brain)
    lid = gc.lore.add(GROUP, ["验货"], "刚进群那晚大家说的“验货”")
    gc.db.conn.execute("UPDATE qq_lore SET created=? WHERE id=?", (time.time() - 40 * 86400, lid))
    gc.db.conn.commit()
    assert lid in gc.lore.human_stale(GROUP, 30)                                   # 40 天里群友没人打过
    gc._record(GROUP, 10001, "小满", "你这是在验货吗", 1, role="assistant")
    assert "验货" not in gc._recent_text(GROUP, users_only=True)                   # 她自己说的不触发
    gc.lore._stale_cache = {}
    gc._record(GROUP, FRIEND, "小王", "验货验货", 2)
    assert lid not in gc.lore.human_stale(GROUP, 30)


def test_ledger_alias(brain):
    from aigroup.memory.ledger import Ledger
    led = Ledger(brain.db)
    led.put(GROUP, "成都AG超玩会（AG）", "最近一场", "3:1 赢了 狼队", "比分")
    assert led.valid(GROUP, "ag 昨天打得咋样")
    assert not led.valid(GROUP, "这个 agent 好用吗")


def test_far_name_when_no_quote(cfg, brain):
    gc, ob = _gc(cfg, brain, cooldown_seconds=0)
    ob.can_quote = False
    gc._record(GROUP, FRIEND, "小王", "我面试过了", 1)
    for i in range(3):
        gc._record(GROUP, FRIEND2, "老张", f"别的事{i}", 2 + i)
    gc.call_name = lambda gid, uid: "王哥"
    brain.llm.reply = J(act="say", kind="I2", to=1, len="short", say=["几面啊"], move="追问")
    assert asyncio.run(gc.speak(GROUP, mentioned=False, owner_spoke=False, trigger=None, who=""))
    assert "王哥，几面啊" in str(ob.sent[-1][1])


def test_daily_metrics_and_flavor(cfg, brain):
    from aigroup.eval.daily import flavor_alerts, jsd, lsm, run_daily
    gc, _ = _gc(cfg, brain)
    gc._record(GROUP, 10001, "小满", "我这边稿子还没交呢", 1, role="assistant")
    gc._record(GROUP, FRIEND, "小王", "啥稿子", 2)
    out = run_daily(gc, brain.now())
    assert out and out[0]["self_claims"] == 1 and out[0]["ungrounded"] == 1
    assert jsd([3] * 10, [3] * 10) == 0.0 and jsd([3] * 10, [50] * 10) > 0.5
    assert lsm(["你在干嘛呢"] * 5, ["你在干嘛呢"] * 5) == 1.0
    assert flavor_alerts({"调侃": 20.0, "自嘲": 1.0}, {"调侃": 5.0, "自嘲": 0.0}) == ["调侃从每百句 20.0 掉到 5.0"]
