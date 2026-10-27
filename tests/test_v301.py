"""30.1：认真回答时不按条数挑短的；问她的话不受刷屏刹车拦；原图没下完不拿缩略图凑；自检多两条（空评语、把证实的事说成传闻）。"""
from aigroup.chat.checks import FAILURE_MODES, critic_verdict
from aigroup.chat.plan import decide
from aigroup.chat.quality import pick

G = {"max_per_10min": 8, "burst_max": 5, "burst_soft": 3, "cooldown_seconds": 30}


def test_pick_keeps_full_answer_when_serious():
    full = [["不算有诚意", "通篇只说近期言论，还强调个人表达不代表联盟", "停播、解约以后才发的"]]
    short = [["形式合格实质回避，不算诚意道歉"]]
    assert pick(full + short)[0] == 1                          # 闲聊：一样好挑条数少的
    assert pick(full + short, count_penalty=False)[0] == 0      # 认真回答：用她最想说的那个


def test_asked_bypasses_burst():
    kw = dict(mentioned=False, now=1000.0, last_reply_ts=990.0, replies_10min=5, quiet=False, g=G, p=0.9, thr=0.5, burst=6)
    assert decide(**kw)[0] is False
    ok, why = decide(**kw, asked=True)
    assert ok and why == "在问她"
    assert decide(**kw, asked=True, hush="被嫌话多")[0] is False
    assert decide(**{**kw, "replies_10min": 16}, asked=True)[0] is False


def test_failure_modes_new():
    assert len(FAILURE_MODES) >= 18
    assert critic_verdict({"bad": True, "which": 17, "kind": "看法", "why": "只有评语"})
    assert critic_verdict({"bad": True, "which": 18, "kind": "事实", "why": "说成传闻"})


def test_image_waits_for_full(tmp_path, monkeypatch):
    import asyncio
    import io

    from PIL import Image

    from aigroup.wechat import wximg

    def png(color):
        b = io.BytesIO()
        Image.new("RGB", (200, 200), color).save(b, "PNG")
        return b.getvalue()
    full, thumb = tmp_path / "a.dat", tmp_path / "a_t.dat"
    full.write_bytes(png((255, 255, 255)))                     # 原图没下完：一片白
    thumb.write_bytes(png((200, 0, 0)))
    st = wximg.ImageStore.__new__(wximg.ImageStore)
    st.paths_for = lambda c, n, t: [full, thumb]
    st._find_key = lambda: None
    monkeypatch.setattr(wximg, "decrypt", lambda d, a, b: d)
    assert st.load("c", "n", 0, thumb=False) is None and st.partial
    assert st.load("c", "n", 0) == thumb.read_bytes() and st.thumb
    full.write_bytes(png((0, 120, 0)))                         # 下完了
    assert st.load("c", "n", 0, thumb=False) == full.read_bytes() and not st.thumb

    class Bot:
        opts, data = {}, type("D", (), {"account_dir": tmp_path})()
    from aigroup.wechat.bot import WeChatBot
    b = Bot()
    b._images = st
    st.acc = tmp_path
    out = asyncio.run(WeChatBot.fetch_image(b, "wximg:c:n:0", wait=0.1, wait_full=0.1))
    assert out == full.read_bytes() and b.last_was_thumb is False


def test_corrected_by_echo_and_evidence(cfg, brain):
    import time

    from conftest import FRIEND, GROUP, _gc
    gc, _ = _gc(cfg, brain)
    gc._record(GROUP, 10001, "小满", "那个传闻我没查到原文，先别当真", 1, role="assistant")
    now = time.time()
    assert gc.corrected_now(GROUP, [(1, FRIEND, "别当真？？")], now)
    assert gc.corrected_now(GROUP, [(1, FRIEND, "官方的道歉声明都出来了")], now)
    assert not gc.corrected_now(GROUP, [(1, FRIEND, "今天吃啥？？")], now)
    assert not gc.corrected_now(GROUP, [(1, FRIEND, "声明都发了吗")], now)


def test_two_research_in_parallel(cfg, brain):
    import asyncio
    import json

    from conftest import FRIEND, GROUP, _gc
    gc, _ = _gc(cfg, brain)
    gc.agent_on = lambda gid: True
    gc._record(GROUP, FRIEND, "小王", "@你 这两份声明是真的吗，有诚意吗", 1)
    calls = [{"function": {"name": "research", "arguments": json.dumps({"goal": g, "depth": "quick", "type": t})}}
             for g, t in (("核实声明真假", "source"), ("判断有没有诚意", "judge"))]

    async def chat_tools(msgs, tools, **kw):
        return {"tool_calls": calls}
    gc.brain.llm.chat_tools = chat_tools
    seen = []

    async def research(gid, goal, depth, mentioned, question="", window=None, qtype="", filler=True):
        seen.append((goal, filler))
        await asyncio.sleep(0.05)
        return f"# 查了：{goal}"
    gc._agent_research = research
    out = asyncio.run(gc.agent_think(GROUP, True))
    assert "核实声明真假" in out and "判断有没有诚意" in out
    assert [f for _, f in seen] == [True, False]
