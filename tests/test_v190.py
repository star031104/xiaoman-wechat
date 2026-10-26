"""19.0：工具调用（判断层 + 研究层）、查资料的简报、当面要的提醒（到点就发、发出去才算完成、重复、取消）。"""
import asyncio
import json
import time

from conftest import FRIEND, GROUP, FakeLLM, _gc, _msg, _run

from aigroup.agent.loop import tool_loop
from aigroup.agent.mixin import brief_block, format_results, parse_brief
from aigroup.chat.grow import next_due
from aigroup.net.links import page_text


def call(name, **args):
    return {"id": f"c{name}{len(args)}", "type": "function",
            "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}


class ToolLLM(FakeLLM):
    """chat_tools 按顺序吐出剧本里的回复；剧本空了就交卷。"""

    def __init__(self, script=None, reply="哈哈哈真的假的"):
        super().__init__(reply)
        self.script = list(script or [])
        self.tool_calls_log = []

    def tool_profiles(self, role="helper"):
        return ["deepseek"]

    async def chat_tools(self, messages, tools, role="helper", thinking=False, tool_choice=None, max_tokens=4000,
                         effort=None):
        self.tool_calls_log.append({"messages": [dict(m) for m in messages], "tools": [t["function"]["name"] for t in tools or []],
                                    "thinking": thinking, "tool_choice": tool_choice})
        if self.script:
            nxt = self.script.pop(0)
            if isinstance(nxt, Exception):
                raise nxt
            return nxt
        return {"role": "assistant", "content": "无", "_finish": "stop"}


def msg(content="", calls=None):
    d = {"role": "assistant", "content": content, "_finish": "tool_calls" if calls else "stop"}
    if calls:
        d["tool_calls"] = calls
    return d


BRIEF = json.dumps({"answer_points": ["aspas 还在 MIBR，合同到 2027", "Stage 2 被 LOUD 淘汰，2022 年以来第一次没进 Champions"],
                    "premise": "", "as_of": "2026-10-21", "sources": [{"title": "x", "site": "Sheep Esports", "date": "2026-09-21"}],
                    "confidence": "high", "unknown": ""}, ensure_ascii=False)


# ---------------- 循环本身 ----------------
def test_tool_loop_runs_tools_and_finishes():
    llm = ToolLLM([msg(calls=[call("web_search", query="a"), call("read_url", url="https://x.com")]), msg(BRIEF)])
    seen = []

    async def ex(name, args):
        seen.append(name)
        return f"<资料>{name}</资料>"
    msgs = [{"role": "user", "content": "查"}]
    r = asyncio.run(tool_loop(llm, msgs, [], ex, max_rounds=3))
    assert sorted(seen) == ["read_url", "web_search"] and r.rounds == 2 and not r.forced
    assert parse_brief(r.text)["answer_points"][0].startswith("aspas")
    assert [m["role"] for m in msgs] == ["user", "assistant", "tool", "tool", "assistant"]
    assert all(not k.startswith("_") for m in msgs for k in m)            # _finish 不回传给接口


def test_tool_loop_forces_answer_when_out_of_rounds():
    llm = ToolLLM([msg(calls=[call("web_search", query="a")])] * 3 + [msg(BRIEF)])

    async def ex(name, args):
        return "结果"
    msgs = [{"role": "user", "content": "查"}]
    r = asyncio.run(tool_loop(llm, msgs, [], ex, max_rounds=2, finish_prompt="交卷"))
    assert r.forced and r.rounds == 4                               # 叫它交卷还想查：去掉那条、不带工具再要一次
    assert llm.tool_calls_log[-2]["tool_choice"] == "none" and llm.tool_calls_log[-1]["tools"] == []
    assert msgs[-2]["content"] == "交卷" and msgs[-1]["content"] == BRIEF
    assert "aspas" in r.text


def test_tool_loop_tool_error_is_text():
    llm = ToolLLM([msg(calls=[call("web_search", query="a")]), msg(BRIEF)])

    async def ex(name, args):
        raise RuntimeError("网断了")
    msgs = [{"role": "user", "content": "查"}]
    asyncio.run(tool_loop(llm, msgs, [], ex, max_rounds=2))
    assert "出错了" in msgs[2]["content"]


# ---------------- 简报和搜索结果的格式 ----------------
def test_format_results_sorted_and_free_engine_note():
    res = [{"title": "新", "url": "https://a.com/1", "snippet": "s1", "date": "2026-10-20"},
           {"title": "无日期", "url": "https://b.com/2", "snippet": "s2", "date": ""},
           {"title": "旧", "url": "https://c.com/3", "snippet": "s3", "date": "2026-09-01"}]
    t = format_results(res, "Tavily")
    assert t.index("无日期") < t.index("旧") < t.index("新") and "<资料" in t and "没有发布日期" not in t
    assert "没有发布日期" in format_results(res[:1], "百度")
    assert "没搜到" in format_results([], "")


def test_parse_brief_and_block():
    d = parse_brief(BRIEF)
    b = brief_block("aspas 近况", d)
    assert "LOUD" in b and "2026-10-21" in b and "Sheep Esports" in b and "别贴链接" in b
    d2 = parse_brief("就是一句话没按格式")
    assert d2["answer_points"] == ["就是一句话没按格式"] and d2["confidence"] == "low"
    d3 = parse_brief(json.dumps({"answer_points": [], "premise": "他早就退役了"}))
    assert d3["answer_points"] == [] and d3["premise"] == "他早就退役了"
    assert "什么都没查到" not in brief_block("x", d3)
    assert "前提：TenZ 已退役" in brief_block("x", {"answer_points": ["a"], "premise": "TenZ 已退役"})


def test_page_text_keeps_tables():
    html = ("<html><head><title>aspas | VLR</title><script>var x=1</script></head><body><nav>Matches Events</nav>"
            "<table><tr><td>ACS</td><td>K:D</td></tr><tr><td>250.1</td><td>1.12</td></tr></table></body></html>")
    d = page_text(html)
    assert "250.1" in d["text"] and "1.12" in d["text"] and "var x" not in d["text"] and "aspas" in d["title"]


def test_next_due():
    t = time.mktime((2026, 10, 23, 8, 0, 0, 0, 0, -1))           # 周五
    assert next_due(t, "daily") - t == 86400
    assert time.localtime(next_due(t, "weekday")).tm_wday == 0     # 周五之后的工作日是周一
    assert next_due(t, "weekly") - t == 7 * 86400 and next_due(t, "") is None


# ---------------- 判断层：设提醒 ----------------
def _later(minutes):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(time.time() + minutes * 60))


def test_router_sets_reminder_and_tells_her(cfg, brain):
    gc, ob = _gc(cfg, brain, agent=True)
    brain.llm = ToolLLM([msg(calls=[call("reminder_set", when=_later(60), what="交作业", for_who="我")])])
    _run(gc, _msg(FRIEND, "小王", "随便聊聊", 1))
    block = asyncio.run(gc.agent_think(GROUP, True, "小王", None))
    assert "记下了提醒" in block and "交作业" in block
    ps = gc.plans(GROUP)
    assert len(ps) == 1 and ps[0]["source"] == "tool" and ps[0]["user_id"] == FRIEND and ps[0]["who"] == "小王"
    brain.llm.script = [msg(calls=[call("reminder_set", when=_later(61), what="交作业", for_who="我")])]
    assert "已经记过" in asyncio.run(gc.agent_think(GROUP, True, "小王", None))       # 不重复记
    assert "过去了" in gc.remind_set(GROUP, {"when": _later(-30), "what": "x", "for_who": "我"}, FRIEND, "小王")
    assert "没看懂" in gc.remind_set(GROUP, {"when": "明天早上", "what": "x"}, FRIEND, "小王")
    assert "交作业" in gc.remind_list(GROUP)
    assert "取消了" in gc.remind_cancel(GROUP, {"what": "作业"}) and gc.plans(GROUP) == []


def test_router_nothing_to_do(cfg, brain):
    gc, ob = _gc(cfg, brain, agent=True)
    brain.llm = ToolLLM([])
    _run(gc, _msg(FRIEND, "小王", "今天天气不错哈哈", 1))
    assert asyncio.run(gc.agent_think(GROUP, False, "小王", None)) == ""
    assert brain.llm.tool_calls_log[0]["thinking"] is False


# ---------------- 研究层 ----------------
def test_research_quick_makes_brief_and_reuses(cfg, brain, monkeypatch):
    gc, ob = _gc(cfg, brain, agent=True)
    brain.llm = ToolLLM([msg(calls=[call("research", goal="aspas 近况", depth="quick")]),
                         msg(calls=[call("web_search", query="aspas", days=30)]),
                         msg(BRIEF)])
    searched = []

    async def fake_search(q, n=6, days=0, **kw):
        searched.append((q, days))
        return [{"title": "Aspas to miss Champions", "url": "https://www.sheepesports.com/a", "snippet": "...",
                 "date": "2026-09-21"}], "Tavily"
    monkeypatch.setattr(gc.web, "agent_search", fake_search)
    _run(gc, _msg(FRIEND, "小王", "aspas最近状态咋样", 1))
    block = asyncio.run(gc.agent_think(GROUP, True, "小王", None))
    assert searched == [("aspas", 30)]
    assert "LOUD" in block and "你刚在手机上查了一下" in block
    research_call = brain.llm.tool_calls_log[1]
    assert research_call["thinking"] is False and "web_search" in research_call["tools"]
    assert "sheepesports" in brain.llm.tool_calls_log[2]["messages"][-1]["content"]
    assert ob.sent == []                                          # 快查不说“等我看看”
    assert gc.db.get("agent_log")[0]["goal"] == "aspas 近况"
    # 接着追问、判断层说不用查：带上刚才的简报
    brain.llm.script = []
    again = asyncio.run(gc.agent_think(GROUP, True, "小王", None))
    assert "你刚才查过的" in again and "LOUD" in again


def test_research_deep_says_filler_and_thinks(cfg, brain, monkeypatch):
    gc, ob = _gc(cfg, brain, agent=True, agent_filler_after=0)       # 23.3 默认查了 10 秒还没好才说；这里马上说

    async def fake_search(q, n=6, days=0, **kw):
        return [], ""
    monkeypatch.setattr(gc.web, "agent_search", fake_search)
    _run(gc, _msg(FRIEND, "小王", "TenZ最近数据怎么样", 1, at=True))
    ob.sent.clear()
    brain.llm = ToolLLM([msg(calls=[call("research", goal="TenZ 最近数据", depth="deep")]), msg(BRIEF)])
    asyncio.run(gc.agent_think(GROUP, True, "小王", None))
    assert len(ob.sent) == 1                                      # 先说了一句“等我看看”
    assert brain.llm.tool_calls_log[1]["thinking"] is True
    ob.sent.clear()
    brain.llm.script = [msg(calls=[call("research", goal="再查", depth="deep")]), msg(BRIEF)]
    asyncio.run(gc.agent_think(GROUP, True, "小王", None))
    assert ob.sent == []                                          # 10 分钟内不再说


def test_research_limits_fall_back(cfg, brain):
    gc, ob = _gc(cfg, brain, agent=True, agent_per_day=0)
    brain.llm = ToolLLM([msg(calls=[call("research", goal="x", depth="quick")])])
    _run(gc, _msg(FRIEND, "小王", "随便聊聊", 1))
    assert asyncio.run(gc._agent_research(GROUP, "x", "quick", True)) is None


def test_speak_uses_agent_and_falls_back(cfg, brain):
    gc, ob = _gc(cfg, brain, reply="记下啦 明天提醒你", agent=True)
    brain.llm = ToolLLM([msg(calls=[call("reminder_set", when=_later(600), what="交作业", for_who="我")])],
                        reply="记下啦 明天提醒你")
    _run(gc, _msg(FRIEND, "小王", "满满 明天提醒我交作业", 1, at=True))
    assert any("你刚办的事" in m[-1]["content"] for m in brain.llm.group_calls)
    assert gc.plans(GROUP)[0]["what"] == "交作业"
    brain.llm.script = [RuntimeError("接口挂了")]
    brain.llm.group_calls.clear()
    _run(gc, _msg(FRIEND, "小王", "满满 在吗", 2, at=True))
    assert brain.llm.group_calls                                     # 想一想失败也照常回话


def test_agent_off_by_default(cfg, brain):
    gc, ob = _gc(cfg, brain)
    brain.llm = ToolLLM([])
    assert gc.agent_on(GROUP) is False
    _run(gc, _msg(FRIEND, "小王", "满满 在吗", 1, at=True))
    assert brain.llm.tool_calls_log == []


# ---------------- 到点提醒 ----------------
def test_tool_reminder_fires_even_in_quiet_hours(cfg, brain):
    cfg["quiet_hours"] = "00:00-23:59"
    gc, ob = _gc(cfg, brain, reply="小王 交作业啦", agent=True, active_hours="03:00-03:01", plans_per_day=0)
    pid = gc.remind_set(GROUP, {"when": _later(1), "what": "交作业", "for_who": "我", "repeat": "daily"}, FRIEND, "小王")
    assert "记下了" in pid
    gc.db.conn.execute("UPDATE qq_plans SET due=?", (time.time() - 5,))
    gc._record(GROUP, FRIEND, "小王", "随便聊聊", 1)
    asyncio.run(gc.idle_tick())
    assert ob.sent and "到点了" in brain.llm.group_calls[-1][-1]["content"]
    done = gc.plans(GROUP, "done")
    assert len(done) == 1
    nxt = gc.plans(GROUP, "open")                                 # 每天的：排好了下一次
    assert len(nxt) == 1 and nxt[0]["due"] > time.time() + 23 * 3600 and nxt[0]["repeat"] == "daily"
    ob.sent.clear()
    asyncio.run(gc.idle_tick())
    assert ob.sent == []                                          # 发过就不再发


def test_tool_reminder_retries_then_sends_plain(cfg, brain):
    gc, ob = _gc(cfg, brain, reply="[跳过]", agent=True)
    gc.remind_set(GROUP, {"when": _later(1), "what": "开会", "for_who": "大家"}, FRIEND, "小王")
    gc.db.conn.execute("UPDATE qq_plans SET due=?", (time.time() - 5,))
    gc._record(GROUP, FRIEND, "小王", "随便聊聊", 1)
    for i in range(3):
        asyncio.run(gc.tool_reminders_tick())
        gc.db.conn.execute("UPDATE qq_plans SET next_try=0")
    p = gc.db.conn.execute("SELECT * FROM qq_plans").fetchone()
    assert p["status"] == "done" and p["tries"] == 2
    assert "到点了：开会" in json.dumps(ob.sent[-1], ensure_ascii=False)


def test_tool_reminder_missed_after_long_downtime(cfg, brain):
    gc, ob = _gc(cfg, brain, agent=True)
    gc.remind_set(GROUP, {"when": _later(1), "what": "开会", "for_who": "大家"}, FRIEND, "小王")
    gc.db.conn.execute("UPDATE qq_plans SET due=?", (time.time() - 13 * 3600,))
    asyncio.run(gc.tool_reminders_tick())
    assert gc.db.conn.execute("SELECT status FROM qq_plans").fetchone()["status"] == "missed" and ob.sent == []


def test_old_plans_tick_ignores_tool_reminders(cfg, brain):
    gc, ob = _gc(cfg, brain, agent=True)
    gc.remind_set(GROUP, {"when": _later(20), "what": "开黑", "for_who": "大家"}, FRIEND, "小王")
    _run(gc, _msg(FRIEND, "小王", "随便聊聊", 1))
    assert asyncio.run(gc.plans_tick(GROUP, gc._st(GROUP))) is False


def test_tavily_out_flag(cfg, brain):
    gc, ob = _gc(cfg, brain)
    assert gc.web.tavily_out() is False
    gc.db.set("tavily_out", time.strftime("%Y-%m"))
    assert gc.web.tavily_out() is True
    gc.db.set("tavily_out", "2000-01")
    assert gc.web.tavily_out() is False


def test_digest_plan_postponed_not_expired(cfg, brain):
    """19.0 整理出来的约定错过了窗口不再悄悄作废：晚了 2 小时还会提醒（说一句晚了），晚了 6 小时以上改成过后问。"""
    gc, ob = _gc(cfg, brain, reply="几点了人呢")
    soon = time.strftime("%Y-%m-%d %H:%M", time.localtime(time.time() + 20 * 60))
    pid = gc.add_plan(GROUP, 0, "大家", "开黑", soon, "remind")
    gc.db.conn.execute("UPDATE qq_plans SET due=? WHERE id=?", (time.time() - 2 * 3600, pid))
    _run(gc, _msg(FRIEND, "小王", "随便聊聊", 1))
    assert asyncio.run(gc.plans_tick(GROUP, gc._st(GROUP))) is True
    assert "顺口说一句晚了" in brain.llm.group_calls[-1][-1]["content"]
    pid2 = gc.add_plan(GROUP, FRIEND, "小王", "去面试", soon, "remind")
    gc.db.conn.execute("UPDATE qq_plans SET due=? WHERE id=?", (time.time() - 8 * 3600, pid2))
    gc.db.set(f"qq_plans_last_{GROUP}", 0)
    brain.llm.reply = "面试咋样了，过没过"
    assert asyncio.run(gc.plans_tick(GROUP, gc._st(GROUP))) is True
    row = gc.db.conn.execute("SELECT kind, status FROM qq_plans WHERE id=?", (pid2,)).fetchone()
    assert row["kind"] == "ask" and row["status"] == "done"
    assert "怎么样了" in brain.llm.group_calls[-1][-1]["content"]


def test_digest_plan_done_only_when_said(cfg, brain):
    gc, ob = _gc(cfg, brain)
    soon = time.strftime("%Y-%m-%d %H:%M", time.localtime(time.time() + 20 * 60))
    pid = gc.add_plan(GROUP, 0, "大家", "开黑", soon, "remind")
    _run(gc, _msg(FRIEND, "小王", "随便聊聊", 1))

    async def mute(*a, **k):
        return False
    gc.speak = mute
    assert asyncio.run(gc.plans_tick(GROUP, gc._st(GROUP))) is False
    row = gc.db.conn.execute("SELECT * FROM qq_plans WHERE id=?", (pid,)).fetchone()
    assert row["status"] == "open" and row["tries"] == 1 and row["next_try"] > time.time()
    assert asyncio.run(gc.plans_tick(GROUP, gc._st(GROUP))) is False          # 没到重试时间
    assert gc.db.conn.execute("SELECT tries FROM qq_plans WHERE id=?", (pid,)).fetchone()["tries"] == 1


def test_home_agent_view_and_plan_edit(cfg, brain):
    """19.0 小屋：她查过什么（轮数、秒数、简报）；约定能改时间，历史里看得到错过的 / 取消的。"""
    from aiohttp.test_utils import TestClient, TestServer

    from aigroup.web.server import GroupHome
    gc, ob = _gc(cfg, brain, agent=True)
    gc._agent_log({"ts": time.time(), "gid": GROUP, "goal": "aspas 近况", "depth": "deep", "thinking": True,
                   "rounds": 4, "seconds": 31.2, "forced": True, "steps": ["web_search {\"query\": \"aspas\"}"],
                   "brief": {"answer_points": ["还在 MIBR"], "confidence": "high", "sources": [{"site": "vlr.gg"}]}})
    gc._agent_log({"ts": time.time(), "gid": GROUP + 1, "goal": "别的群", "depth": "quick", "rounds": 1, "seconds": 3})
    soon = time.strftime("%Y-%m-%d %H:%M", time.localtime(time.time() + 3600))
    pid = gc.add_plan(GROUP, 0, "大家", "开黑", soon, "remind")
    gone = gc.add_plan(GROUP, 0, "大家", "看比赛", time.strftime("%Y-%m-%d %H:%M", time.localtime(time.time() + 9 * 3600)), "remind")
    gc.set_plan(gone, "missed")
    later = time.strftime("%Y-%m-%d %H:%M", time.localtime(time.time() + 5 * 3600))

    async def go():
        async with TestClient(TestServer(GroupHome(cfg, brain, ob, gc).app())) as c:
            a = await (await c.get(f"/api/agent?gid={GROUP}")).json()
            assert a["enabled"] and [x["goal"] for x in a["logs"]] == ["aspas 近况"]
            assert a["reader"][0] == "直连" and "esports" in a["tools"] and "bili" in a["tools"]
            assert a["logs"][0]["rounds"] == 4 and a["logs"][0]["brief"]["answer_points"] == ["还在 MIBR"]
            r = await (await c.post("/api/life/plan", json={"id": pid, "when": later})).json()
            assert r["ok"], r
            r = await (await c.post("/api/life/plan", json={"id": pid, "when": "2000-01-01 08:00"})).json()
            assert not r["ok"] and "过去" in r["error"]
            L = await (await c.get(f"/api/life?gid={GROUP}")).json()
            assert any(p["status"] == "missed" for p in L["plans_done"])
    asyncio.run(go())
    due = gc.db.conn.execute("SELECT due FROM qq_plans WHERE id=?", (pid,)).fetchone()["due"]
    assert abs(due - time.mktime(time.strptime(later, "%Y-%m-%d %H:%M"))) < 61
    from pathlib import Path
    page = (Path(__file__).resolve().parents[1] / "aigroup" / "web" / "page.html").read_text(encoding="utf-8")
    assert 'id="agentList"' in page and "api/agent?gid=" in page and "data-pwhen" in page
