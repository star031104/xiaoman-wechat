"""5.0：真实微信群里测出来的几件事——追问没 @ 也接、口头禅不重复、大多数时候一条、刚认识的人别叫真名说私事、
不叫名字的群、做不到的线下的事别硬圆、别把问题推回去、别说幕后的事、重启前没回的 @ 重启后补回。"""
import asyncio
import json
import time

from aigroup.chat.address import her_phrases, likely_to_her, shape_bubbles
from aigroup.chat.quality import check, pick

from conftest import FRIEND, FRIEND2, GROUP, OWNER, J, _gc, _msg, _run


# ---------------- 纯函数 ----------------
def test_likely_to_her():
    hers = ["这辈分已记入小本本，改天连本带利还你", "叫爷爷？行啊，那红包先准备好"]
    kw = dict(now=100.0, her_last_ts=40.0, her_recent=hers)
    assert likely_to_her("你小本本上都记啥了", attention=0.0, **kw)            # 接了她刚说的词
    assert likely_to_her("小本本记了啥？", attention=0.0, **kw)                # 没“你”，但接她的词、在问
    assert likely_to_her("你快说点什么", attention=0.5, **kw)                  # 正跟她聊着
    assert not likely_to_her("你快说点什么", attention=0.1, **kw)              # 没在跟她聊、也没接她的词
    assert not likely_to_her("你们有点变态了", attention=0.9, **kw)            # 只有“你们”
    assert not likely_to_her("你小本本上都记啥了", attention=0.9, at_other=True, **kw)
    # 22.0：十分钟内用“你”问她刚说的事（“你还有什么稿子”），也算冲她；再久就不算了
    assert likely_to_her("你小本本上都记啥了", attention=0.9, now=500.0, her_last_ts=40.0, her_recent=hers)
    assert not likely_to_her("你小本本上都记啥了", attention=0.9, now=900.0, her_last_ts=40.0, her_recent=hers)
    assert not likely_to_her("小王你小本本呢", attention=0.9, other_names=["小王"], **kw)
    assert likely_to_her("感觉它不聪明", attention=0.0, now=400.0, her_last_ts=40.0, her_recent=hers) == "about"
    assert likely_to_her("感觉它不聪明", attention=0.0, now=900.0, her_last_ts=40.0, her_recent=hers) == ""
    assert likely_to_her("你小本本上都记啥了", attention=0.0, **kw) == "to"


def test_her_phrases_and_shape():
    day = ["老李你这一大早的是在验货吗", "验货也得等货到啊", "昨晚被王总拉进来测试的，你也是来验货的吗",
           "这辈分已记入小本本，改天连本带利还你", "张总早，我是小满"]
    out = her_phrases(day[:3], day, ["还在跑测试"], ["已记入小本本", "我宣布"], names=["小满", "王总", "老李", "张总"])
    assert "验货" in out and "我宣布" not in out and "小满" not in out and "王总" not in out
    assert shape_bubbles(["起了起了", "这不刚爬起来嘛"], [1], 0.1) == ["起了起了，这不刚爬起来嘛"]
    assert shape_bubbles(["起了起了", "这不刚爬起来嘛"], [1], 0.9) == ["起了起了", "这不刚爬起来嘛"]
    assert len(shape_bubbles(["一句挺长的话说了好多好多字啊啊啊啊", "第二句也挺长的再多说一点点内容吧"], [2, 2, 2], 0.9)) == 1
    assert shape_bubbles(["好的", "*得"], [2, 2, 2], 0.0) == ["好的", "*得"]          # 改错字不合
    assert shape_bubbles(["a", "b"], [], 0.0, long=True) == ["a", "b"]


def test_quality_v50():
    assert "假装在场" in check("人是真坐在课室了，就是替你答到的时候别让我出声")[1]
    assert "推回去" in check("你们学校附近有啥好吃的，我帮你挑")[1]
    assert "说了幕后的事" in check("昨晚被王总拉进来测试的那个")[1]
    assert "说了幕后的事" not in check("昨晚被王总拉进来测试的那个", ai_open=True)[1]
    assert check("中午吃猪脚饭吧，便宜管饱")[0] == 0
    assert any(h.startswith("刚用过") for h in check("验货也得等货到啊", stale=["验货"])[1])
    assert any(h.startswith("叫了名字") for h in check("阿花早", no_names=["阿花"])[1])
    i, _, _ = pick([["你决定吧"], ["吃猪脚饭，便宜管饱"]])
    assert i == 1
    i, _, _ = pick([["好的", "我也想去"], ["好的我也想去"]])                      # 一样好时挑条数少的
    assert i == 1


# ---------------- 串起来 ----------------
def test_follow_up_without_at_is_answered(cfg, brain):
    gc, ob = _gc(cfg, brain, cooldown_seconds=600)
    brain.llm.reply = J(act="say", kind="I1", to=1, len="short", say=["已记入小本本，改天连本带利还你"])
    _run(gc, _msg(FRIEND, "小王", "叫我一声爷爷", 1, at=True))
    assert len(ob.sent) == 1
    brain.llm.reply = J(act="say", kind="I1", to=2, len="short", say=["记了你欠我三顿饭"])
    _run(gc, _msg(FRIEND, "小王", "你小本本上都记啥了", 2))                    # 没 @，冷却 600 秒也照样接
    assert len(ob.sent) == 2
    user = brain.llm.group_calls[-1][1]["content"]
    assert "多半是冲你说的" in user or "在接你之前对他说的话" in user      # 19.1 先认成“在接她的话”


def test_no_names_group_and_just_met(cfg, brain):
    gc, ob = _gc(cfg, brain, call_names=False)
    brain.db.conn.execute("INSERT INTO qq_profiles(user_id, group_id, msgs, since_update, last_seen, profile) "
                          "VALUES (?,?,?,?,?,?) ON CONFLICT(user_id, group_id) DO UPDATE SET profile=excluded.profile",
                          (FRIEND, GROUP, 30, 0, time.time(),
                           json.dumps({"称呼": "阿花", "近况": "今天最后一节课"}, ensure_ascii=False)))
    brain.db.conn.commit()
    brain.db.set(f"qq_seeded_{GROUP}", [FRIEND])                 # 种子里事先写了他
    brain.llm.reply = J(act="say", kind="I1", to=1, len="short", say=["阿花早"], alt=[["[@小王] 早，起这么早"]])
    _run(gc, _msg(FRIEND, "小王", "hello", 1, at=True))
    sent = "".join(s["data"].get("text", "") for _, segs in ob.sent for s in segs)
    assert "阿花" not in sent and "起这么早" in sent
    assert not any(seg["type"] == "at" for _, segs in ob.sent for seg in segs)      # 也不 @ 人
    sys = brain.llm.group_calls[-1][0]["content"]
    assert "这个群大家平时不叫名字" in sys and "刚认识" in sys and "事先跟你讲的" in sys
    assert "最后一节课" not in sys                                # 刚认识：近况先不给她


def test_bad_reply_when_mentioned_is_rewritten(cfg, brain):
    gc, ob = _gc(cfg, brain)
    replies = [J(act="say", kind="I1", to=1, len="short", say=["昨晚被拉进来测试的那个，来验货吗"]),
               J(act="say", kind="I1", to=1, len="short", say=["新来的，多指教"])]

    async def chat(messages, **kw):
        brain.llm.calls.append(messages)
        if "最近的群聊记录" in messages[-1]["content"] or "重写" in messages[-1]["content"]:
            brain.llm.group_calls.append(messages)
            return replies.pop(0) if replies else J(act="skip")
        return "好"
    brain.llm.chat = chat
    _run(gc, _msg(FRIEND, "小王", "你是谁", 1, at=True))
    sent = "".join(s["data"].get("text", "") for _, segs in ob.sent for s in segs)
    assert "多指教" in sent and "测试" not in sent


def test_unanswered_mention_before_restart(cfg, brain):
    gc, ob = _gc(cfg, brain)
    now = time.time()
    gc._record(GROUP, FRIEND2, "小李", "@你 你怎么知道我是谁", 51, to_her=True, ts=now - 120)
    gc._boot_ts = now - 60                                       # 这条是重启前收到的
    out = gc._pending_mentions(GROUP, set(), now)
    assert [x["who"] for x in out] == ["小李"] and out[0]["kind"] == "pending"
    assert gc._pending_mentions(GROUP, set(), now) == []         # 只补一次
    gc._record(GROUP, FRIEND2, "小李", "@你 还有一句", 52, to_her=True, ts=now - 100)
    gc._record(GROUP, 0, "小满", "回过了", 53, role="assistant", ts=now - 90)
    assert gc._pending_mentions(GROUP, set(), now) == []         # 她后来说过话了：不补
    brain.db.set(f"qq_night_{GROUP}", [{"who": "小李", "mid": 51, "ts": now - 120, "kind": "pending"}])
    brain.llm.reply = J(act="say", kind="I1", to=1, len="short", say=["听说的呗"])
    asyncio.run(gc._night_catch_up(GROUP, gc._st(GROUP), now))
    user = brain.llm.group_calls[-1][1]["content"]
    assert "还没回" in user and "才看到" in user                  # 提示里明确说“别说才看到”
    assert ob.sent
    _ = OWNER


def test_names_off_follows_group_habit(cfg, brain):
    gc, ob = _gc(cfg, brain)
    now = time.time()
    for i in range(160):
        gc._record(GROUP, FRIEND if i % 2 else FRIEND2, "小王" if i % 2 else "小李", f"今天吃了{i}碗饭", 1000 + i, ts=now - 1000 + i)
    assert gc.names_off(GROUP)                                      # 群友之间几乎不叫名字
    gc._names_habit.clear()
    for i in range(30):
        gc._record(GROUP, FRIEND, "小王", f"小李你来不来{i}", 2000 + i, ts=now - 100 + i)
    assert not gc.names_off(GROUP)                                  # 经常叫名字
    gc.gover[GROUP] = {"call_names": False}
    assert gc.names_off(GROUP)                                      # 小屋里明确设了：听设的
