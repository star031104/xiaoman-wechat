"""23.2（10-23 日报的问题）：替别人答直接不说、视频还没看完别空说“去看”、讲她的视频看完主动说、看错图把握高也拦、
同一个梗换说法也算、“发过了”关账、聊别的时别追问旧事、被嫌的说法补全。"""
import asyncio
import time

from conftest import FRIEND, FRIEND2, GROUP, J, _gc

from aigroup.chat.checks import CLOSE_RX, DROP


def test_steal_answer_drops_instead_of_rewrite(cfg, brain):
    gc, ob = _gc(cfg, brain, cooldown_seconds=0, self_score=False)
    gc._record(GROUP, FRIEND, "小王", "随便聊聊", 1)
    gc._record(GROUP, FRIEND2, "老张", "小王我学会了能当自己的项目吗", 2)
    w = gc._addressee(GROUP, "学会了也没空写，我赌五毛", FRIEND2, {"content": "小王我学会了能当自己的项目吗"})
    assert w.startswith(DROP) and "别抢着答" in w

    called = []

    async def rw(messages, raw, hits, gid):
        called.append(hits)
        return [["改了也还是替人答"]]
    gc._rewrite = rw
    brain.llm.reply = J(act="say", kind="I2", to=2, len="short", say=["学会了也没空写，我赌五毛"], move="接梗")
    ok = asyncio.run(gc.speak(GROUP, mentioned=False, owner_spoke=False, trigger=None, who=""))
    assert not ok and not ob.sent and not called                         # 不重写、不发
    r = gc.db.conn.execute("SELECT detail FROM qq_check_log WHERE name='addressee'").fetchone()
    assert "别抢着答" in r["detail"]


def test_video_pending_and_about_her(cfg, brain):
    gc, _ = _gc(cfg, brain)
    gc._record(GROUP, FRIEND, "小王", "【我做了个 AI 群友】 https://b23.tv/x [链接：B站视频《我做了个 AI 群友》"
                                      "（UP主 小王，时长 1:43），你正在点开看]", 1)
    now = time.time()
    w = gc._video_pending(GROUP, "成片终于发了，这就去看", now)
    assert w.startswith(DROP) and "还在看" in w
    assert gc._video_pending(GROUP, "晚上吃啥", now) == ""                  # 说别的不管
    gc.db.set(f"qq_videos_{GROUP}", [{"ts": now + 1, "bv": "BVx", "title": "x", "block": "x"}])
    assert gc._video_pending(GROUP, "这就去看", now + 2) == ""              # 看完了：可以聊
    assert gc.video_about_her(GROUP, {"title": "我做了个 AI 群友", "gist": "", "keys": []})
    assert gc.video_about_her(GROUP, {"title": "满满的一天", "gist": "", "keys": []})          # 她的名字
    assert not gc.video_about_her(GROUP, {"title": "羽毛球教学", "gist": "正手发球", "keys": ["发球"]})


def test_media_claim_promise_words(cfg, brain):
    gc, _ = _gc(cfg, brain)
    gc._record(GROUP, FRIEND, "小王", "[视频]", 1)
    for t in ("成片来了，我看看", "这就去看", "行，等你调完看成片"):
        assert "打不开" in gc._media_claim(GROUP, t, time.time()), t


def test_image_assert_even_when_sure(cfg, brain):
    gc, _ = _gc(cfg, brain)
    gc._record(GROUP, FRIEND2, "老张", "周末打球不", 1)
    gc._record(GROUP, FRIEND, "小王", "[图片：照片，拍的是一家店的羽毛球價目表，關鍵字「羽毛球價目表」「RSL」「VICTOR」，"
                                      "圖中日期「2025年8月」]", 2)
    w = gc._image_guess(GROUP, "小王这是在看8号那场的场地啊", image={"top": "高"})
    assert "图里写着是什么就是什么" in w
    assert gc._image_guess(GROUP, "小王这是在挑球啊", image={"top": "高"}) == ""      # 图里有球：顺着说
    assert gc._image_guess(GROUP, "这是要买球？", image={"top": "高"}) == ""


def test_catchphrase_family(cfg, brain):
    gc, _ = _gc(cfg, brain)
    gc._record(GROUP, FRIEND, "小王", "晚上吃啥", 1)
    gc._record(GROUP, 10001, "小满", "行吧，那我小本本记岔了", 2, role="assistant")
    w = gc._catchphrase(GROUP, "三连到位，那我刚才那句就当你没听见")
    assert "同一个梗" in w
    assert gc._catchphrase(GROUP, "我不记得了") == ""                      # “不记得”不算
    gc._record(GROUP, FRIEND, "小王", "你小本本里记了啥", 3)
    assert gc._catchphrase(GROUP, "这条不记了") == ""                      # 人家在说这个：接着说不算


def test_close_and_stale_ask(cfg, brain):
    assert CLOSE_RX.search("之前发过了") and CLOSE_RX.search("早发了")
    gc, _ = _gc(cfg, brain)
    now = time.time()
    brain.db.conn.execute("INSERT INTO qq_group_messages(ts, group_id, user_id, name, role, content) VALUES (?,?,?,?,?,?)",
                          (now - 3 * 3600, GROUP, FRIEND, "小王", "user", "开发文档我晚点发"))
    brain.db.conn.commit()
    for i, t in enumerate(["高校薪酬表", "这个表挺全", "博士后也有"]):
        gc._record(GROUP, FRIEND2, "老张", t, 10 + i)
    w = gc._stale_ask(GROUP, "工资这块我不懂，但那开发文档发了没", False, time.time())
    assert "先接现在的话题" in w
    assert gc._stale_ask(GROUP, "博士后一个月多少？", False, time.time()) == ""     # 问的是现在聊的
    assert gc._stale_ask(GROUP, "那开发文档发了没", True, time.time()) == ""         # 被叫到不管


def test_hush_words(cfg, brain):
    for t, n in (("这是场地吗我问你", 2), ("一天问两三次这你受得了吗", 2), ("？何意味", 2)):
        gc, _ = _gc(cfg, brain)
        gc._st(GROUP).last_reply_ts = time.time() - 10
        assert not gc.huh_signal(GROUP, FRIEND, t)                         # 一次：记着
        gc._st(GROUP).last_reply_ts = time.time() - 10
        assert gc.huh_signal(GROUP, FRIEND2, t), t                          # 第二次：安静
    gc, _ = _gc(cfg, brain)
    gc._st(GROUP).last_reply_ts = time.time() - 10
    assert gc.huh_signal(GROUP, FRIEND, "这个话题跳过了哥") and gc.hushed(GROUP)     # 一句就够（也是“少说点”）
