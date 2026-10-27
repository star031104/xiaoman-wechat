"""27.0 / 27.1 / 28.0：有毛病先换她自己写的另一个说法、没被叫到不重写；有人说“别提了”重新想；
提示词里的毛病只写一份；检查只留 8 个；记忆合成几块带上限；画像每人有上限、别碰一定留着。"""
import asyncio
import json

from conftest import FRIEND, GROUP, J, _gc


def _say(gc, brain, say, alt=None, **kw):
    brain.llm.reply = J(act="say", kind="I2", to=1, len="short", say=say, alt=alt or [], move="接梗")
    return asyncio.run(gc.speak(GROUP, mentioned=False, owner_spoke=False, trigger=None, who="", **kw))


def test_pick_clean_uses_other_candidate(cfg, brain):
    gc, ob = _gc(cfg, brain, cooldown_seconds=0, check_critic="off")
    gc._record(GROUP, FRIEND, "小王", "今天好冷", 1)
    for i, t in enumerate(["我小本本记岔了", "这条记进小本本"]):
        gc._record(GROUP, 10001, "小满", t, 10 + i, role="assistant")
    assert _say(gc, brain, ["行，小本本上再记一笔"], alt=[["冷就多穿点，别硬扛"]])
    sent = "".join(s["data"].get("text", "") for _, segs in ob.sent for s in segs)
    assert "多穿点" in sent and "小本本" not in sent
    assert not any("重写" in json.dumps(m, ensure_ascii=False)[-200:] for m in brain.llm.calls)


def test_no_rewrite_when_unprompted_and_all_bad(cfg, brain):
    gc, ob = _gc(cfg, brain, cooldown_seconds=0, check_critic="off")
    gc._record(GROUP, FRIEND, "小王", "今天好冷", 1)
    for i, t in enumerate(["我小本本记岔了", "这条记进小本本"]):
        gc._record(GROUP, 10001, "小满", t, 10 + i, role="assistant")
    n = len(brain.llm.group_calls)
    assert not _say(gc, brain, ["行，小本本上再记一笔"], alt=[["小本本又多一页"]]) and not ob.sent
    assert len(brain.llm.group_calls) == n + 1                 # 只有主回复一次，没有重写


def test_stop_signal_regex():
    from aigroup.chat.checks import CLOSE_RX
    from aigroup.chat.speak import STOP_RX
    for t in ("别提面试了好吗压力很大的", "别说了", "换个话题吧", "压力好大"):
        assert STOP_RX.search(t), t
    assert CLOSE_RX.search("别提面试了") and not STOP_RX.search("面试加油")


def test_prompt_failure_list_single_source(cfg, brain):
    from aigroup.chat.checks import FAILURE_MODES
    gc, _ = _gc(cfg, brain)
    gc._record(GROUP, FRIEND, "小王", "hi", 1)
    sysp = asyncio.run(gc.build_messages(GROUP, True, False, "小王", None))[0]["content"]
    rules = sysp[sysp.index("# 在群里怎么相处"):]
    assert "下面这些毛病以前真出过" in rules
    assert all(m.split("（")[0] in rules for m in FAILURE_MODES)
    import re
    assert not re.search(r"（2\d\.\d）", rules)                  # 不再有一条条带版本号的补丁


def test_checks_trimmed():
    from aigroup.chat.checks import CHECKS
    from aigroup.chat.settings import DEAD_KNOBS, DEFAULTS
    assert len(CHECKS) == 11 and "critic" in CHECKS and "pushback" not in CHECKS
    assert "check_pushback" in DEAD_KNOBS and "check_pushback" not in DEFAULTS


def test_critic_sees_her_earlier_lines(cfg, brain):
    gc, _ = _gc(cfg, brain)
    seen = {}

    async def chat(messages, **kw):
        seen["p"] = messages[-1]["content"]
        return json.dumps({"bad": False})
    gc.brain.llm.chat = chat
    gc._record(GROUP, 10001, "小满", "叫吴总听着像财务部来查账的", 1, role="assistant")
    for i in range(12):
        gc._record(GROUP, FRIEND, "小王", f"第{i}句", 10 + i)
    asyncio.run(gc._critic(GROUP, "叫老板听着像来查账的", False, []))
    assert "她今天早些时候说过的" in seen["p"] and "财务部来查账" in seen["p"]


def test_merge_blocks_budget_and_stable():
    from aigroup.chat.prompt import merge_blocks
    a = "# 你自己说过的事\n" + "\n".join(f"- 第{i}件事" * 3 for i in range(50))
    b = "# 这一周的你\n最近有点忙"
    out = merge_blocks("关于你自己", [a, "", b], 300)
    assert out.startswith("# 关于你自己\n## 你自己说过的事") and "## 这一周的你\n最近有点忙" in out
    assert len(out) < 360 and merge_blocks("x", [a, b], 300) == merge_blocks("x", [a, b], 300)
    assert merge_blocks("x", ["", None], 300) == ""


def test_memory_merge_in_prompt(cfg, brain):
    gc, _ = _gc(cfg, brain)
    gc.db.set(f"qq_selfcore_{GROUP}", "嘴硬心软")
    gc.db.set(f"qq_reflect_{GROUP}", "这周话多了点")
    gc._record(GROUP, FRIEND, "小王", "hi", 1)
    sysp = asyncio.run(gc.build_messages(GROUP, True, False, "小王", None))[0]["content"]
    assert "# 关于你自己（和这些保持一致）\n## 你是怎样的人" in sysp and "这周话多了点" in sysp
    gc.g["memory_merge"] = False
    sysp = asyncio.run(gc.build_messages(GROUP, True, False, "小王", None))[0]["content"]
    assert "# 你是怎样的人（这几个月慢慢长出来的）" in sysp and "# 关于你自己" not in sysp


def test_profile_line_caps_keep_must():
    from aigroup.memory.people import profile_line
    prof = {"印象": "爱吐槽", "称呼": "王哥", "口头禅": "、".join(["笑死"] * 200), "兴趣": "x" * 900,
            "记得的事": ["养了一只猫" * 20] * 8, "别碰": "体重", "近况": "在赶论文"}
    line = profile_line("小王", prof, "很熟", "", cap=600)
    assert len(line) < 700 and "别碰：体重" in line and "熟悉度：很熟" in line and "称呼：王哥" in line
    assert "…" in line


def test_main_knobs_listed():
    from aigroup.chat.settings import KNOBS, MAIN_KNOBS
    assert 20 <= len(MAIN_KNOBS) <= 32 and all(k in KNOBS for k in MAIN_KNOBS) and "talkativeness" in MAIN_KNOBS
