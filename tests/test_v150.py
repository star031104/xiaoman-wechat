"""15.0：B 站视频看画面、追问视频细节；知识边界；想法池；在聊的人今天的状态；事实的来源和有效期；签名效果统计。"""
import asyncio
import time

from conftest import FRIEND, GROUP, _gc

from aigroup.net.bili import Bili, ask_time, line_time, pick_times
from aigroup.traits import parse_traits


def test_time_helpers():
    assert line_time("[03:12] 你好") == 192 and line_time("[1:02:03] x") == 3723 and line_time("没有") is None
    assert ask_time("3:12 那里笑死") == 192 and ask_time("第三分钟那段") == 180 and ask_time("5分多钟") == 330
    assert ask_time("开头说了啥") == 0 and ask_time("最后怎么了", "[00:10] a\n[09:40] b") == 580
    assert ask_time("十二分钟的地方") == 720 and ask_time("今天天气") is None
    ts = pick_times(600, [{"t": "05:00"}], 6)
    assert ts and all(0 <= t < 600 for t in ts) and 305 in ts
    assert all(abs(a - b) >= 15 for i, a in enumerate(ts) for b in ts[i + 1:])
    assert pick_times(0, [], 6) == []


class VisionLLM:
    vision = True

    def __init__(self, raw):
        self.raw, self.calls = raw, []

    async def chat(self, messages, **kw):
        self.calls.append(messages)
        return self.raw

    async def describe_image(self, b64, hint="", prompt=""):
        return "一个人在打游戏"


def test_see_frames_one_call_and_fallback():
    b = Bili(None, VisionLLM('{"frames": [{"t": "封面", "see": "封面大字：通宵"}, {"t": "01:00", "see": "游戏录屏"}]}'))
    fr = asyncio.run(b.see({"title": "t", "author": "a"}, [(60, b"\xff\xd8x")], b"\xff\xd8c"))
    assert fr == [{"t": "封面", "see": "封面大字：通宵"}, {"t": "01:00", "see": "游戏录屏"}]
    assert len(b.llm.calls) == 1 and sum(1 for c in b.llm.calls[0][0]["content"] if c["type"] == "image_url") == 2
    b2 = Bili(None, VisionLLM("不是 JSON"))
    fr2 = asyncio.run(b2.see({"title": "t"}, [(60, b"\xff\xd8x")]))
    assert fr2 == [{"t": "01:00", "see": "一个人在打游戏"}]
    assert asyncio.run(Bili(None, None).see({}, [(1, b"x")])) == []


def test_watch_with_frames_and_describe(monkeypatch):
    class Fake(Bili):
        async def view(self, bv):
            return {"aid": 1, "cid": 2, "duration": 300, "title": "测试视频", "owner": {"name": "UP"}, "pic": "",
                    "pages": [{"cid": 2, "duration": 300, "part": "P1"}, {"cid": 3, "duration": 120, "part": "第二集"}]}

        async def subtitles(self, bv, aid, cid):
            return "[00:10] 大家好\n[01:00] 开始打了"

        async def danmaku(self, cid):
            return []

        async def comments(self, aid, bv, k=6):
            return []

        async def frames(self, bv, cid, times):
            self.got = (cid, times)
            return [(t, b"\xff\xd8") for t in times[:2]]

        async def see(self, w, shots, cover=b""):
            return [{"t": "00:40", "see": "游戏画面"}]
    b = Fake(None, None)
    w = asyncio.run(b.watch("BV1xx411c7mD", 2, 4))
    assert b.got[0] == 3 and w["title"].endswith("（P2 第二集）") and w["frames"] == [{"t": "00:40", "see": "游戏画面"}]
    w0 = asyncio.run(b.watch("BV1xx411c7mD", 1, 0))
    assert w0["frames"] == []
    d = Bili.describe(w, {"seen": "full", "gist": "打游戏", "visual": "游戏录屏"})
    assert "画面：游戏录屏" in d and "画面看了几个片段" in d


def test_video_detail_answers_followups(cfg, brain):
    gc, ob = _gc(cfg, brain)
    gc.db.set("qq_video_BV1", {"title": "猫猫视频", "block": "[你点开《猫猫视频》看完了]", "keys": ["猫猫"],
                               "lines": "[00:05] 开头打招呼\n[03:10] 猫跳上桌子\n[03:30] 打翻了杯子\n[09:00] 最后睡着了",
                               "frames": [{"t": "03:20", "see": "橘猫站在桌上"}]})
    gc.db.set(f"qq_videos_{GROUP}", [{"ts": time.time(), "bv": "BV1", "key": "qq_video_BV1", "title": "猫猫视频",
                                      "block": "[你点开《猫猫视频》看完了]"}])
    b = gc.videos_block(GROUP, query="第三分钟那段怎么回事")
    assert "03:10" in b and "打翻了杯子" in b and "橘猫站在桌上" in b and "开头打招呼" not in b
    b2 = gc.videos_block(GROUP, query="那个视频讲了啥")
    assert "最后睡着了" in b2 and "你看到的画面" in b2
    assert "里说的话" not in gc.videos_block(GROUP, query="今天吃什么")


def test_knowledge_boundary_render():
    t = parse_traits({"knowledge": {"knows": ["瓦"], "vague": ["比分"], "unknown": ["手感"], "not_into": "股票"}})
    c = t.render_core()
    assert "你熟的：瓦" in c and "只知道个大概的：比分" in c and "不懂的：手感" in c and "不太感兴趣的：股票" in c


def test_thought_pool(cfg, brain):
    gc, ob = _gc(cfg, brain)
    now = time.time()
    gc.hold_thought(GROUP, "周末那场羽毛球赛我想押小王赢", now=now)
    gc.hold_thought(GROUP, "周末那场羽毛球赛我想押小王赢！", now=now)            # 差不多的不重复存
    assert len(gc.db.get(f"qq_thoughts_{GROUP}")) == 1
    assert gc.held_thoughts(GROUP, "今天吃什么", now=now) == []
    assert gc.held_thoughts(GROUP, "周末羽毛球赛几点", now=now)
    assert gc.held_thoughts(GROUP, "随便", idle=True, now=now)
    assert gc.held_thoughts(GROUP, "周末羽毛球赛几点", now=now + 7 * 3600) == []      # 6 小时过期
    gc.use_thoughts(GROUP, "我押小王赢，周末羽毛球赛稳了")
    assert gc.db.get(f"qq_thoughts_{GROUP}") == []
    from aigroup.chat.group import GroupChat
    p = GroupChat.parse_plan_text('{"act": "skip", "say": [], "hold": "等会儿再说这个梗"}')
    assert p["hold"] == "等会儿再说这个梗"
    assert GroupChat.parse_plan_text('{"act": "skip", "say": [], "hold": null}')["hold"] == ""


def test_thought_pool_and_people_today_in_prompt(cfg, brain):
    gc, ob = _gc(cfg, brain)
    gc._record(GROUP, FRIEND, "小王", "今天加班到十点，好累", 1)
    gc.hold_thought(GROUP, "加班的时候记得吃晚饭")
    msgs = asyncio.run(gc.build_messages(GROUP, False, False, "", None, query="加班好累", focus=[FRIEND]))
    u = msgs[1]["content"]
    assert "小王（" in u and "状态不太好" in u and "在忙" in u      # 21.0 人卡
    assert "还没说出口的" in u and "加班的时候记得吃晚饭" in u
    assert '"hold"' in msgs[0]["content"]


def test_hold_saved_from_plan(cfg, brain):
    gc, ob = _gc(cfg, brain)
    gc._record(GROUP, FRIEND, "小王", "哈哈", 1)
    brain.llm.reply = '{"act": "skip", "say": [], "hold": "这个梗我等会儿接"}'
    asyncio.run(gc.speak(GROUP, mentioned=False, owner_spoke=False, trigger=None, who=""))
    assert gc.db.get(f"qq_thoughts_{GROUP}")[0]["text"] == "这个梗我等会儿接"


def test_fact_source_and_until(cfg, brain):
    gc, ob = _gc(cfg, brain)
    m = gc.memory
    now = time.time()
    a = m.add_fact(GROUP, "这周在赶毕业论文", speaker=FRIEND, subject=FRIEND, source="self", until=now + 3600)
    b = m.add_fact(GROUP, "听说他要换工作了", subject=FRIEND, source="other")
    c = m.add_fact(GROUP, "上周在出差", subject=FRIEND, source="self", until=now - 10)        # 已经过期的不当有效
    fs = {f["id"]: f for f in m.facts(GROUP)}
    assert a in fs and b in fs and fs[a]["source"] == "self"
    assert c in fs                                                     # until 已过：存的时候当作长期（t_invalid 不写过去的时间）
    blk = m.prompt_block({"facts": [fs[a], fs[b]]}, {str(FRIEND): "小王"})
    assert "（他自己说的）" in blk and "（听别人说的，未必准）" in blk and "为止" in blk
    gc.db.conn.execute("UPDATE qq_facts SET t_invalid=? WHERE id=?", (now - 5, a))
    assert a not in {f["id"] for f in m.facts(GROUP)}


def test_sig_stats(cfg, brain):
    gc, ob = _gc(cfg, brain)
    now = time.time()
    ids = []
    for sc in (1.0, 0.0, 0.6):
        cur = gc.db.conn.execute("INSERT INTO qq_reply_effect(ts, group_id, done, score) VALUES (?,?,1,?)", (now, GROUP, sc))
        ids.append(cur.lastrowid)
    gc.db.set("sig_log", [{"id": ids[0], "sigs": ["teased"], "ts": now}, {"id": ids[1], "sigs": ["teased", "sad"], "ts": now},
                          {"id": ids[2], "sigs": ["sad"], "ts": now}])
    st = {r["id"]: r for r in gc.sig_stats()}
    assert st["teased"]["n"] == 2 and st["teased"]["avg"] == 0.5 and st["teased"]["cold"] == 0.5
    assert st["sad"]["avg"] == 0.3
    assert "sigs" in gc.eval_snapshot()
