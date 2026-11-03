"""35.2：11-03 日报 P1～P8——安静期所有主动路都不说、拦过的意思换个说法也不说、群里别叫昵称、表情包算一条、
写稿时又来了话、@ 别人的不接、价格要有出处、占位话、5xx 重试、主动额度按时段摊。"""
import asyncio
import time

from aigroup.chat.settings import DEFAULTS
from aigroup.chat.social import SocialMixin
from aigroup.chat.v352 import cn_values, is_filler, price_mentions, unsourced_prices

from conftest import FRIEND, FRIEND2, GROUP, OWNER, _gc


def _rec(gc, uid, name, text, ts, role="user"):
    cur = gc.db.conn.execute("INSERT INTO qq_group_messages(ts, group_id, user_id, name, role, content) VALUES(?,?,?,?,?,?)",
                             (ts, GROUP, uid, name, role, text))
    gc.db.conn.commit()
    return cur.lastrowid


def _speak(gc, **kw):
    args = dict(mentioned=False, owner_spoke=False, trigger=None, who="", why="到点了，提醒一下")
    args.update(kw)
    return asyncio.run(gc.speak(GROUP, **args))


def test_defaults():
    assert DEFAULTS["burst_max"] == 4
    for k in ("check_price_src", "check_ask_told", "check_filler", "check_ask_humans"):
        assert DEFAULTS[k] == "on"


def test_hush_words():
    for t in ("你 小嘴巴 闭起来", "嘴闭上"):
        assert SocialMixin.QUIET.search(t)
    for t in ("闭起来眼睛", "把眼睛闭上"):
        assert not SocialMixin.QUIET.search(t)
    for t in ("神了……", "还发表情包", "部署啥"):
        assert SocialMixin.HUH.search(t)
    for t in ("神了这个", "吃啥好呢今天"):
        assert not SocialMixin.HUH.search(t)


def test_hush_blocks_must_paths(cfg, brain):
    gc, ob = _gc(cfg, brain, reply="几点了，说好的部署呢", quality_check=False)
    gc.hush(GROUP, "她自己说了先不说")
    assert _speak(gc, must=True) is False                   # 提醒、跟进、想念这些 must 的也不说
    assert not ob.sent
    assert _speak(gc, must=True, manual=True) is True       # 小屋手动点的照说
    assert ob.sent


def test_hush_still_answers_mention(cfg, brain):
    gc, ob = _gc(cfg, brain, reply="在呢", quality_check=False)
    gc.hush(GROUP, "有人嫌话多")
    assert _speak(gc, mentioned=True, why=None, who="阿王") is True


def test_blocked_topic_again(cfg, brain):
    gc, ob = _gc(cfg, brain, reply="老张，几点了，说好的部署呢", quality_check=False)
    gc.note_blocked(GROUP, "几点了，说好的部署呢")
    assert gc.blocked_again(GROUP, ["老张，几点了，说好的部署呢"])
    assert not gc.blocked_again(GROUP, ["今天中午吃啥"])
    assert _speak(gc, must=True) is False
    assert not ob.sent


def test_public_names(cfg, brain):
    gc, _ = _gc(cfg, brain)
    gc.owner_id = OWNER
    pet = gc.fav_name(GROUP)
    assert gc.public_names(GROUP, [f"行啊{pet}，这回真做出来了"], must=False) == [f"行啊{pet}，这回真做出来了"]   # 只有他俩
    _rec(gc, FRIEND, "阿王", "哈哈", time.time() - 30)
    out = gc.public_names(GROUP, [f"行啊{pet}，这回真做出来了"], must=False)
    assert pet not in out[0]
    assert gc.public_names(GROUP, [f"{pet}人呢"], must=True) == [f"{pet}人呢"]           # 想念 @ 照叫
    gc.g["fav_name_public"] = True
    assert gc.public_names(GROUP, [f"{pet}人呢"], must=False) == [f"{pet}人呢"]


def test_at_other(cfg, brain):
    gc, _ = _gc(cfg, brain)
    assert gc.to_others(GROUP, [(1, FRIEND, "@阿张 蹲到告诉我", {"at_other": True})])
    assert not gc.to_others(GROUP, [(1, FRIEND, "@阿张 蹲到告诉我", {"at_other": True}), (2, FRIEND2, "好", {})])
    gc.note_at(GROUP, FRIEND, [str(FRIEND2)])
    assert gc.answers_at(GROUP, FRIEND2, "好")
    assert not gc.answers_at(GROUP, FRIEND2, "这个牌子我用过，还行吧挺润的")
    assert not gc.answers_at(GROUP, FRIEND, "好")


def test_budget_window(cfg, brain):
    gc, _ = _gc(cfg, brain, unprompted_per_day=30)
    now = time.time()
    assert gc.budget_left(GROUP, now) == ""
    for i in range(11):
        gc.db.conn.execute("INSERT INTO qq_reply_effect(ts, group_id, mentioned, text) VALUES (?,?,0,?)", (now - 600 - i, GROUP, "x"))
    gc.db.conn.commit()
    assert "4 小时主动额度用完" in gc.budget_left(GROUP, now)
    gc.g["unprompted_window_hours"] = 0
    assert gc.budget_left(GROUP, now) == ""
    gc.g["unprompted_per_day"] = 10
    assert "当天主动额度用完" in gc.budget_left(GROUP, now)


def test_prices():
    assert cn_values("八十") == {80} and cn_values("六七十") == {60, 70} and cn_values("两三百") == {200, 300}
    assert cn_values("二十五") == {25} and cn_values("120") == {120}
    ctx = "[图片：5%咖啡因眼部精华液 双11 页面]"
    assert unsourced_prices("选图二，三十毫升八十上下", ctx, ctx) == ["八十上下"]
    assert unsourced_prices("到手 79 块", "到手价 79元") == []
    assert unsourced_prices("大概八十块", "到手价 79元") == []
    assert price_mentions("我们有三百个人", "双11") == []
    assert price_mentions("跑了八十上下公里吧") == []


def test_filler():
    for t in ("我查查手机哈", "我找找看，等我一下", "等我一下", "稍等"):
        assert is_filler(t), t
    for t in ("我看是第二个", "我查了，是第二个", "看看这个价"):
        assert not is_filler(t), t


def test_filler_dropped_next_to_content(cfg, brain):
    gc, ob = _gc(cfg, brain, reply="我找找看，等我一下|||选第二个，咖啡因那款", quality_check=False, merge_bubbles=False,
                 max_bubbles=3)
    assert _speak(gc, mentioned=True, why=None, who="阿王") is True
    texts = [s["data"]["text"] for _, segs in ob.sent for s in segs if s["type"] == "text"]
    assert not any("等我一下" in t for t in texts)


def test_llm_retry_5xx(cfg):
    from aigroup.llm.client import LLM

    class Boom(Exception):
        pass
    llm = LLM.__new__(LLM)
    assert hasattr(LLM, "_chat")
    calls = []

    class C:
        class chat:
            class completions:
                @staticmethod
                async def create(**kw):
                    calls.append(1)
                    if len(calls) == 1:
                        raise Boom("Error code: 503 - Service Unavailable")

                    class R:
                        usage = None

                        class _m:
                            content = "好"
                        choices = [type("c", (), {"message": _m})]
                    return R()

    class P:
        name, model, temperature, max_tokens, extra_body, json_mode, price, tools = "p", "m", None, None, None, False, None, False
        client = C

        def available(self):
            return True

        def cool(self, e):
            pass
    llm.routes, llm.profiles, llm.retry_5xx = {"chat": ["p"]}, {"p": P()}, True
    llm.temperature, llm.max_tokens = 0.7, 100
    llm._with_note = lambda m: m
    llm._record = lambda *a, **k: None
    import aigroup.llm.client as cl
    orig = cl.asyncio.sleep

    async def fast(s):
        return None
    cl.asyncio.sleep = fast
    try:
        assert asyncio.run(llm._chat([{"role": "user", "content": "x"}], "chat", None, None)) == "好"
    finally:
        cl.asyncio.sleep = orig
    assert len(calls) == 2
