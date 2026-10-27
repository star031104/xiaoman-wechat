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


def test_shadow_mode_only_logs(cfg, brain):
    gc, ob = _gc(cfg, brain, cooldown_seconds=0, check_catchphrase="shadow", check_critic="off")
    gc._record(GROUP, FRIEND, "小王", "ai 要来抢我们工作了", 1)
    for i, t in enumerate(["我小本本记岔了", "这条记进小本本"]):
        gc._record(GROUP, 10001, "小满", t, 10 + i, role="assistant")
    assert _say(gc, brain, "行，小本本上再记一笔")
    assert "小本本" in str(ob.sent[-1][1])
    r = gc.db.conn.execute("SELECT mode FROM qq_check_log WHERE name='catchphrase'").fetchone()
    assert r["mode"] == "shadow"


def test_ask_ledger_repeat_and_closed(cfg, brain):
    gc, _ = _gc(cfg, brain)
    now = time.time()
    gc.note_asks(GROUP, ["AG 今天赢了没"], FRIEND, now - 3600)
    assert "已经问过" in gc._ask_repeat(GROUP, "AG 那场比分出来没？", FRIEND, now)
    assert gc._ask_repeat(GROUP, "晚上吃啥？", FRIEND, now) == ""
    gc.db.conn.execute("DELETE FROM qq_asks")
    gc.note_asks(GROUP, ["AG 今天赢了没"], FRIEND, now - 80 * 3600)
    assert gc._ask_repeat(GROUP, "AG 比分咋样？", FRIEND, now) == ""                 # 23.1 过了 72 小时：可以再问
    gc._record(GROUP, FRIEND, "小王", "能别鞭尸了吗", 5)
    gc.db.conn.execute("UPDATE qq_group_messages SET ts=? WHERE qq_msg_id=5", (now - 80 * 3600 + 60,))
    assert "别再问了" in gc._ask_repeat(GROUP, "AG 比分咋样？", FRIEND, now)          # 对方说过别问：一周都别提
    assert "别再问了" in gc.asks_block(GROUP)


def test_addressee_time_tease_responsive_room(cfg, brain):
    gc, _ = _gc(cfg, brain)
    gc._record(GROUP, FRIEND, "小王", "今天好累", 1)
    gc._record(GROUP, FRIEND2, "老张", "嗯嗯", 2)
    w = gc._addressee(GROUP, "老张你也太拼了", FRIEND, None)
    assert "叫错人" in w
    assert gc._addressee(GROUP, "小王你也太拼了", FRIEND, None) == ""
    assert "那句是" in gc._addressee(GROUP, "我觉得是周五", FRIEND, {"content": "老张你周几回来？"})


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


# ---------------- 22.1 编别人的事 / 被质疑还嘴硬 ----------------
def test_kripp_alpha():
    from aigroup.eval.calib import kripp_alpha, to_human_scale
    h = [1, 2, 3, 4, 5, 3, 2, 4]
    assert kripp_alpha(h, h) == 1.0
    assert kripp_alpha(h, to_human_scale([10, 30, 50, 70, 90, 55, 25, 75], h)) == 1.0     # 名次一样 → 对到你的分布后完全一致
    assert kripp_alpha(h, [6 - x for x in h]) < 0


# ---------------- 22.2 看图 ----------------
def test_image_only_is_not_a_bid_and_no_guessing(cfg, brain):
    gc, _ = _gc(cfg, brain)
    gc._record(GROUP, 10001, "小满", "我宣布我已经学成了", 1, role="assistant")
    assert gc.bid_from(GROUP, FRIEND, "[图片]") == ""
    gc._record(GROUP, FRIEND, "小王", "[图片：照片，店里贴的羽毛球价目表，「羽毛球價目表」「RSL」]", 2)


def test_trim_desc():
    from aigroup.chat.ingest import trim_desc
    d = trim_desc("照片，羽毛球价目表。\n2) 图里的字：「羽毛球價目表」「RSL」，電話「54781148", 40)
    assert "\n" not in d and d.count("「") == d.count("」") and "2)" not in d
    assert len(trim_desc("一" * 300, 180)) <= 180


# ---------------- 22.3 AI 腔检测器 ----------------
def test_detector_masks_names_and_needs_to_agree_with_you(cfg, brain):
    from aigroup.chat.detector import Detector, normalize, train_from_db
    assert normalize("小王这是在看8号那场 ok", ["小王"]) == "Ⓝ这是在看０号那场 ａ"
    gc, _ = _gc(cfg, brain)
    now = time.time()
    human = ["笑死", "我也是哈哈", "今晚吃啥", "冲冲冲", "绝了", "6", "啊这", "谁懂啊", "我来了", "好耶"]
    ai = ["这个问题很有意思，我们可以从几个方面来看", "希望对你有帮助！", "总的来说，这是一个不错的选择",
          "我理解你的感受，不妨试试以下方法", "此外，保持良好的作息也至关重要"]
    rows = [(now, GROUP, FRIEND, "x", "user", f"{h}{i}") for i in range(20) for h in human]
    rows += [(now, GROUP, 10001, "小满", "assistant", f"{a}{i}") for i in range(40) for a in ai]
    brain.db.conn.executemany("INSERT INTO qq_group_messages(ts, group_id, user_id, name, role, content) VALUES (?,?,?,?,?,?)", rows)
    from aigroup.eval.calib import ensure
    ensure(brain.db)
    for i in range(30):                    # 你打的分和它反着来：它说像 AI 的你反而打高分 → 不用
        brain.db.conn.execute("INSERT INTO qq_human_score(trace, group_id, said, context, score, ts) VALUES (?,?,?,?,?,?)",
                              (f"t{i}", GROUP, ai[i % 5] if i % 2 else human[i % 10], "", 5 if i % 2 else 1, now))
    brain.db.conn.commit()
    assert train_from_db(brain.db) is None
    info = brain.db.get("ai_detector_info")
    assert not info["used"] and info["rho_vs_you"] > 0 and "对不上" in info["why"]
    brain.db.set("ai_detector", {"logp": {"a": [0, 0]}, "prior": 0, "default": [0, 0], "n": [1, 1]})
    assert Detector.load(brain.db) is None                                   # 旧版本的模型不再用


# ---------------- 22.4 看图三步 ----------------
def test_image_block_and_intent(cfg, brain):
    from aigroup.chat.prompt import PromptMixin
    gc, _ = _gc(cfg, brain)
    gc._record(GROUP, 10001, "小满", "那我先去看比赛了", 1, role="assistant")
    gc._record(GROUP, FRIEND, "小王", "[图片：照片，香港一家羽毛球用品店贴的羽毛球价目表，RSL 每筒 120~245 港币]", 2)
    gc._record(GROUP, FRIEND, "小王", "好贵", 3)
    b = gc.image_block(GROUP)
    assert "羽毛球价目表" in b and "好贵" in b and '"image"' in b
    p = PromptMixin.parse_plan_text('{"act": "say", "say": ["这是要囤球？"], "image": {"is": "价目表", '
                                    '"why": [{"guess": "觉得贵吐槽", "conf": "中"}, {"guess": "想买球", "conf": "low"}], "how": "问一句"}}')
    assert p["image"]["top"] == "中" and p["image"]["why"][1]["conf"] == "低"
    gc._record(GROUP, FRIEND, "小王", "[图片：照片，价目表]", 4)
    gc._record(GROUP, 10001, "小满", "嗯", 5, role="assistant")
    assert gc.image_block(GROUP) == ""                                             # 她说过话以后的才算
