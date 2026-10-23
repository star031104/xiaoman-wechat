"""14.0 结构化人格：人格文件解析、放进人设最前面、按情境挑签名和对话示例、她提的性格调整建议、每周人格一致性检查。"""
import asyncio
import shutil
import time
from pathlib import Path

from conftest import FRIEND, GROUP, _gc

from aigroup.persona import load_persona
from aigroup.traits import detect_flags, load_traits, parse_traits

ROOT = Path(__file__).resolve().parents[1]


def _with_traits(cfg, brain):
    pdir = Path(cfg["root"]) / "persona"
    shutil.copy(ROOT / "persona" / "traits.example.yaml", pdir / "t.yaml")
    src = (pdir / "xiaoman.md").read_text(encoding="utf-8")
    (pdir / "xiaoman.md").write_text(src.replace("---\n", "---\ntraits: t.yaml\n", 1), encoding="utf-8")
    brain.persona = load_persona(pdir / "xiaoman.md")
    return brain.persona


def test_parse_and_render():
    t = load_traits(ROOT / "persona" / "traits.example.yaml")
    assert not t.warnings and len(t.signatures) == 6 and t.facets["A4"]["level"] == 2
    core = t.render_core()
    assert core.startswith("# 你的人格") and "顺从偏低" in core and "攻击 0%" in core and "1. 不让任何人" in core
    bad = parse_traits({"facets": {"Z9": 3, "A4": {"level": "x"}}, "humor": {"mix": {"亲和": 90}},
                        "signatures": [{"when": "x"}, {"when": "y", "do": "z", "flags": ["nope"]}]})
    assert len(bad.warnings) == 5
    assert parse_traits({"core": "你是\n  小满"}).core == "你是小满"           # 折行的中文不留空格


def test_flags_and_pick():
    t = load_traits(ROOT / "persona" / "traits.example.yaml")
    f = detect_flags("@满满 你好菜啊像人机", mentioned=True)
    assert {"mentioned", "teased"} <= f
    assert [s.id for s in t.pick("你好菜啊像人机", f)] == ["teased"]
    assert [s.id for s in t.pick("又连跪了", set())] == ["lost_game"]
    assert t.pick("今天天气不错", set()) == []
    assert "late_night" in detect_flags("", hour=2) and "idle" in detect_flags("", why="群里已经安静了 3 个多小时")


def test_persona_body_starts_with_core_and_prompt_has_situation(cfg, brain):
    p = _with_traits(cfg, brain)
    assert p.traits is not None and p.body.startswith("# 你的人格")
    gc, ob = _gc(cfg, brain)
    gc.brain.persona = p
    gc._record(GROUP, FRIEND, "小王", "又连跪了，好难受", 1)
    msgs = asyncio.run(gc.build_messages(GROUP, False, False, "", None, query="又连跪了，好难受"))
    sysm, user = msgs[0]["content"], msgs[1]["content"]
    assert "# 你的人格" in sysm
    assert "# 这种时候的你" in user and "先站他这边怪队友" in user and "立刻不贫了" in user
    assert user.index("# 这种时候的你") > user.index("# 此刻")                       # 离生成最近
    gc.g["signatures"] = False
    msgs = asyncio.run(gc.build_messages(GROUP, False, False, "", None, query="又连跪了"))
    assert "# 这种时候的你" not in msgs[1]["content"]


def test_examples_picked_by_keywords(cfg, brain):
    gc, ob = _gc(cfg, brain)
    p = gc.brain.persona
    assert p.examples, "示例人设要有对话示例"
    ex = p.examples[0]
    kw = next(k for k in ex.keywords if k)
    gc._record(GROUP, FRIEND, "小王", kw, 1)
    msgs = asyncio.run(gc.build_messages(GROUP, False, False, "", None, query=kw))
    assert "# 你平时这种时候会这样说" in msgs[1]["content"]
    assert ex.turns[-1][1].split("|||")[0] in msgs[1]["content"]


def test_pestered_flag_passes_through(cfg, brain):
    _with_traits(cfg, brain)
    gc, ob = _gc(cfg, brain)
    sb = gc.situation_block(GROUP, "随便说说", mentioned=True, ign=0, why="", flags={"pestered"},
                            now=gc.brain.now())
    assert sb == "" or "# 这种时候的你" in sb


def test_proposals(cfg, brain):
    _with_traits(cfg, brain)
    gc, ob = _gc(cfg, brain)
    n = gc.note_proposals(GROUP, [{"facet": "A4", "from": 2, "to": 3, "why": "最近更会让着人", "evidence": "10-20 认错"},
                                  {"facet": "A4", "to": 2},                    # 和现在一样：不记
                                  {"facet": "ZZ", "to": 3}, "x"])
    assert n == 1
    pr = gc.db.get("persona_proposals")
    assert pr[0]["facet"] == "A4" and pr[0]["from"] == 2 and pr[0]["status"] == "pending"
    assert gc.life_snapshot(GROUP)["proposals"][0]["name"] == "顺从"


def test_trait_check(cfg, brain):
    from aigroup.eval.traits import drift, trait_check
    p = _with_traits(cfg, brain)
    gc, ob = _gc(cfg, brain)
    now = time.time()
    for i in range(20):
        gc._record(GROUP, 10001, "满满", f"哈哈第{i}句", 500 + i, role="assistant", ts=now - 100 + i)

    class J:
        async def json(self, messages, default=None, role="helper"):
            assert "不看任何人设" in messages[0]["content"] and "A4 顺从" in messages[0]["content"]
            return {"scores": {"A4": 5, "A2": 4, "E2": 4, "A6": 4, "N4": 2}, "unsure": ["N4"],
                    "humor": {"亲和": 3, "调侃": 2, "自嘲": 1, "攻击": 1}, "attack": ["你真是废物"]}
    res = asyncio.run(trait_check(gc.db, J(), p))
    assert res["off"] == [["A4", 3.0]] and res["humor"]["攻击"] == 1 and res["dist"] == 0.75
    assert drift({"A1": 3}, {}, [])["dist"] is None


def test_profile_has_new_fields():
    from aigroup.memory.people import PROFILE_KEYS, profile_line
    assert "他的反应" in PROFILE_KEYS and "在意" in PROFILE_KEYS
    line = profile_line("小王", {"他的反应": "被损时会回嘴", "在意": "有人接梗"}, "挺熟", "")
    assert "他的反应：被损时会回嘴" in line and "在意：有人接梗" in line
