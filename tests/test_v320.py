"""32.0 三个 B 站视频（自主意图 / 遗忘曲线 / 给大肥鱼一部手机）里挑的四件：
“群里的人”有硬预算、规则体检（自己推自己踩 / 前后打架 / 写了两遍）、管理员 /stop 叫停、小屋记忆体温。"""
import asyncio
import time

from conftest import FRIEND, GROUP, OWNER, _gc, _msg, _run

from aigroup.chat.outbox import parse_control
from aigroup.chat.prompt import cap_roster
from aigroup.chat.rule_audit import audit, conflicts, duplicates, rule_lines, seeds, split_sections
from aigroup.memory.facts import temp_label


# ---------------- 1. “群里的人”的预算 ----------------
def _people(n=3, each=1300, others=5):
    det = [f"- 人{i}：" + "；".join(f"字段{k}是一段描述" * 3 for k in range(each // 30)) for i in range(n)]
    oth = [f"- 路人{i}：不太熟（刚认识）" for i in range(others)]
    return ["（熟悉度怎么相处：……）", *det, "（其他人）", *oth]


def test_cap_roster_drops_others_only():
    lines = _people(each=1300, others=8)
    total = sum(len(x) + 1 for x in lines[:4]) + 10        # 只够详细画像
    out, cut = cap_roster(lines, total)
    assert out == lines[:4] and cut == sum(len(x) + 1 for x in lines[4:])
    half, _ = cap_roster(lines, total + 60)                # 够放几个“其他人”：从后往前去
    assert half[:5] == lines[:5] and 4 < len(half) < len(lines)
    small, _ = cap_roster(lines, 500)                      # 只剩详细画像也超：详细画像不截（截了会截掉“别碰”）
    assert small == lines[:4]


def test_cap_roster_noop_and_deterministic():
    lines = _people(each=300)
    assert cap_roster(lines, 0) == (lines, 0)
    assert cap_roster(lines, 99999) == (lines, 0)
    assert cap_roster(_people(others=9), 3000) == cap_roster(_people(others=9), 3000)


def test_roster_capped_in_prompt_and_size_noted(cfg, brain):
    gc, _ = _gc(cfg, brain, roster_chars=400)
    gc._record(GROUP, FRIEND, "阿王", "在吗", 1)
    gc._set_profile(FRIEND, GROUP, {"印象": "很长", "性格": "；".join(["很长的一段性格描述"] * 80)})
    for i in range(6):                                     # 一句话带过的“其他人”
        gc.db.conn.execute("INSERT OR IGNORE INTO qq_people(user_id, name) VALUES (?, ?)", (70000 + i, f"路人{i}"))
        gc._set_profile(70000 + i, GROUP, {"印象": f"路人{i}，不太熟"})
    gc.__dict__.pop("_roster_cache", None)
    msgs = asyncio.run(gc.build_messages(GROUP, False, False, "", None))
    sysm = msgs[0]["content"]
    roster = sysm[sysm.find("# 群里的人"):]
    assert "很长的一段性格描述" in roster                 # 详细画像不截
    d = gc.db.get(f"prompt_size_{GROUP}")
    assert d and d["system"] == len(sysm) and d["user"] == len(msgs[-1]["content"]) and d["roster_cap"] == 400
    assert d["roster"] == len(roster)


# ---------------- 2. 规则体检 ----------------
FAM = {"小本本": __import__("re").compile(r"小本本|记仇"), "催睡": __import__("re").compile(r"(早点|先|去)睡")}


def test_seeds_only_unlimited_clauses():
    rules = rule_lines({"a": "- 有人追问就接住，比如你刚说“记小本本”\n- 小本本这种一天最多一次\n- 不追问记仇的事\n- not: 先去睡"})
    s = seeds(rules, FAM)
    assert [d["word"] for d in s] == ["小本本"] and "比如" in s[0]["clause"]


def test_conflicts_and_duplicates():
    rules = rule_lines({"规则": "- 别主动开玩笑，等别人先开口\n- 别人难过时多陪陪他",
                        "人设": "- 熟了以后可以主动开玩笑\n- 话少一点也行"})
    c = conflicts(rules)
    assert len(c) == 1 and c[0]["word"].startswith("主动") and c[0]["yes"]["source"] == "人设"
    d = duplicates(rule_lines({"a": "- 一句话说过就别再换个说法说一遍", "b": "- 一句话说过就别再换个说法说第二遍"}))
    assert d and d[0]["score"] >= 0.55


def test_code_prompt_has_no_seeds(cfg, brain):
    """代码里写的规则（“在群里怎么相处”）和她一天安排的提示词，不能拿检查里限着的说法当例子（以前写着“记小本本”“我去看看”“整理小本本”）。"""
    gc, _ = _gc(cfg, brain)
    msgs = asyncio.run(gc.build_messages(GROUP, False, False, "", None))
    r = audit(split_sections(msgs[0]["content"]))
    assert not [d for d in r["seeds"] if d["source"].startswith("在群里怎么相处")], r["seeds"]
    assert not [d for d in r["duplicates"] if d["a"]["source"] == d["b"]["source"] == "在群里怎么相处"], r["duplicates"]
    from aigroup.brain import GroupBrain
    life = GroupBrain._life_ask(type("P", (), {"ai_open": True})())
    assert not audit({"安排": life})["seeds"]


# ---------------- 3. 叫停 ----------------
def test_parse_control():
    assert parse_control("/stop") == ("stop", None)
    assert parse_control("/stop 30") == ("stop", 30)
    assert parse_control("／停 2小时") == ("stop", 120)
    assert parse_control("@满满 /stop 15分钟") == ("stop", 15)
    assert parse_control("/go") == ("go", None) and parse_control("/继续") == ("go", None)
    assert parse_control("/状态") == ("status", None)
    for t in ("stop", "别 /stop 了", "/stopwatch", "http://a.com/stop", "停一下"):
        assert parse_control(t) is None, t


def test_owner_stop_then_go(cfg, brain):
    gc, ob = _gc(cfg, brain, pace=0)
    _run(gc, _msg(OWNER, "老板", "/stop 30", 11))
    assert gc.stopped(GROUP) and gc.muted(GROUP)
    assert 1700 < gc.stopped(GROUP)["until"] - time.time() <= 1800
    assert ob.sent and "前不说话" in ob.sent[-1][1][0]["data"]["text"]       # 确认这句不受叫停影响
    n = len(ob.sent)
    _run(gc, _msg(FRIEND, "阿王", "满满你在吗", 12, at=True))                  # 叫她也不回
    assert len(ob.sent) == n
    kinds = [r["kind"] for r in gc.db.conn.execute("SELECT kind FROM qq_actions WHERE group_id=?", (GROUP,))]
    assert "control" in kinds
    _run(gc, _msg(OWNER, "老板", "/go", 13))
    assert not gc.stopped(GROUP) and not gc.muted(GROUP)
    assert "回来了" in ob.sent[-1][1][0]["data"]["text"]
    _run(gc, _msg(OWNER, "老板", "/状态", 14))
    assert "正常" in ob.sent[-1][1][0]["data"]["text"]


def test_non_admin_stop_is_just_chat(cfg, brain):
    gc, ob = _gc(cfg, brain, pace=0)
    _run(gc, _msg(FRIEND, "阿王", "/stop", 21))
    assert not gc.stopped(GROUP)
    gc2, _ = _gc(cfg, brain, pace=0, admins=[FRIEND])
    _run(gc2, _msg(FRIEND, "阿王", "/stop 5", 22))
    assert gc2.stopped(GROUP)


def test_gate_blocks_while_stopped(cfg, brain):
    gc, ob = _gc(cfg, brain)
    gc.stop_now(GROUP, 10, by="测试")

    async def go():
        try:
            await gc.ob.send_group(GROUP, [{"type": "text", "data": {"text": "冒个泡"}}])
        except RuntimeError as e:
            return str(e)
    assert "叫停" in asyncio.run(go())
    assert not ob.sent
    why = [r["why"] for r in gc.db.conn.execute("SELECT why FROM qq_actions WHERE group_id=? AND ok=0", (GROUP,))]
    assert "叫停" in why
    assert "叫停中" in gc.stop_status(GROUP)


def test_stop_off_switch(cfg, brain):
    gc, _ = _gc(cfg, brain, pace=0, stop_cmd=False)
    _run(gc, _msg(OWNER, "老板", "/stop", 31))
    assert not gc.stopped(GROUP)


# ---------------- 4. 记忆体温 ----------------
def test_temp_label_bands():
    assert temp_label(1.0) == "清楚" and temp_label(0.7) == "清楚"
    assert temp_label(0.5) == "有点淡"
    assert temp_label(0.34) == "快忘了"                    # 0.35 以下提示词里会带“记得不太清了”
    assert temp_label(0.05) == "几乎想不起"


def test_temp_follows_strength(cfg, brain):
    gc, _ = _gc(cfg, brain)
    m = gc.memory
    now = time.time()
    assert temp_label(m.strength(now, now=now)) == "清楚"
    assert temp_label(m.strength(now - 45 * 86400, now=now)) in ("快忘了", "有点淡")
    assert temp_label(m.strength(now - 400 * 86400, now=now)) == "几乎想不起"
    assert temp_label(m.strength(now - 400 * 86400, now=now, evidence=3)) != "清楚"
