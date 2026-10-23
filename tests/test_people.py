"""群友：画像（按群分开、证据、每天仔细回想）、别名、老数据升级。"""
import asyncio
import json
import time

from conftest import (
    FRIEND,
    FRIEND2,
    GROUP,
    GROUP2,
    OB,
    OWNER,
    J,
    _gc,
    _msg,
    _run,
)


def test_people_profiles(cfg, brain):
    from aigroup.memory.people import active_time, familiarity

    from conftest import ProfileLLM
    brain.llm = ProfileLLM()
    gc, ob = _gc(cfg, brain, profile_every=3)
    gc.g["talkativeness"] = 0
    _run(gc, _msg(FRIEND, "小王", "论文又被导师打回来了捏", 1), _msg(FRIEND, "小王", "团子今天又拆家", 2),
         _msg(FRIEND, "小王", "小满你周六来看我答辩不", 3))
    p = gc.person(FRIEND)
    assert p["mentions"] == 1 and p["msgs"] == 3
    assert [r["user_id"] for r in gc.needs_profile()] == [FRIEND]
    assert asyncio.run(gc.profile_people()) == 1
    p = gc.person(FRIEND)
    assert p["profile"]["称呼"] == "王哥" and p["profile"]["记得的事"] == ["养了一只橘猫叫团子", "周六要答辩"]
    assert p["note"] == "嘴硬心软的论文战士" and not gc.needs_profile()
    prompt = brain.llm.calls[-1][0]["content"]
    assert "团子今天又拆家" in prompt and "原来的画像" in prompt
    roster = gc.roster(GROUP)
    assert "王哥" in roster and "别碰：他的发际线" in roster and "记得：养了一只橘猫叫团子" in roster
    assert familiarity(3, 1) == "刚认识" and familiarity(200, 10) == "很熟" and familiarity(60, 3) == "挺熟"
    assert active_time([10] + [0] * 22 + [10]) == "常在深夜出现" and active_time([1] * 24) == ""


def test_old_db_gets_profile_columns(cfg, brain):
    brain.db.conn.execute("DROP TABLE IF EXISTS qq_people")
    brain.db.conn.execute("CREATE TABLE qq_people (user_id INTEGER PRIMARY KEY, name TEXT NOT NULL DEFAULT '', "
                          "note TEXT NOT NULL DEFAULT '', msgs INTEGER NOT NULL DEFAULT 0, last_seen REAL)")
    brain.db.conn.execute("INSERT INTO qq_people(user_id, name, note, msgs) VALUES (?,?,?,?)", (FRIEND, "小王", "老朋友", 9))
    gc, ob = _gc(cfg, brain)
    p = gc.person(FRIEND)
    assert p["note"] == "老朋友" and p["mentions"] == 0 and p["profile"] == {}


def test_profiles_are_per_group(cfg, brain):
    from conftest import ProfileLLM
    brain.llm = ProfileLLM()
    cfg["wechat"]["group"]["profile_every"] = 1
    gc = __import__("aigroup.chat.group", fromlist=["GroupChat"]).GroupChat(cfg, brain, OB(), OWNER, [GROUP, GROUP2], ["满满"])
    gc._record(GROUP, FRIEND, "小王", "我周六要答辩", 1)
    gc._record(GROUP2, FRIEND, "小王", "这个群我只聊游戏", 2)
    rows = [r for r in gc.needs_profile() if r["group_id"] == GROUP]
    assert asyncio.run(gc.profile_people(rows)) == 1
    prompt = brain.llm.calls[-1][0]["content"]
    assert "答辩" in prompt and "只聊游戏" not in prompt                         # 画像只看这个群里说的话
    assert gc.person(FRIEND, GROUP)["profile"]["近况"] == "在赶毕业论文"
    assert gc.person(FRIEND, GROUP2)["profile"] == {}                            # 在 B 群不知道
    assert "王哥" in gc.roster(GROUP) and "王哥" not in gc.roster(GROUP2)


def test_migration_and_interests_cleanup(cfg, brain):
    db = brain.db
    gc, ob = _gc(cfg, brain)
    db.set("migr_20", None)
    db.conn.execute("INSERT INTO qq_people(user_id, name, msgs, profile, note) VALUES (?,?,?,?,?)",
                    (FRIEND, "小王", 3, json.dumps({"印象": "老朋友"}, ensure_ascii=False), "老朋友"))
    db.conn.execute("INSERT INTO qq_group_messages(ts, group_id, user_id, name, role, content) VALUES (?,?,?,?,?,?)",
                    (time.time(), GROUP, FRIEND, "小王", "user", "hi"))
    db.conn.commit()
    db.set("qq_self_notes", "- 我养了猫")
    db.set("qq_interests", ["画画", "吐槽", "本小满", "游戏"])
    db.set("web_overrides", {"interject_chance": 0.3, "talkativeness": 0.5})
    gc, ob = _gc(cfg, brain)
    assert gc.person(FRIEND, GROUP)["note"] == "老朋友"
    assert db.get(f"qq_self_notes_{GROUP}") == "- 我养了猫"
    assert db.get("qq_interests") == ["画画", "游戏"]
    assert db.get("web_overrides") == {"talkativeness": 0.5} and gc.g["talkativeness"] == 0.5


def test_people_aliases(cfg, brain):
    """3.0.3：群里叫真名 / 外号，她知道说的是谁；成员列表补名字、按配置填别名。"""
    gc, ob = _gc(cfg, brain, reply=J(act="say", say=["他又咋了"]))
    gc.g["people_aliases"] = {"阿杰的名片": "王小杰、jj"}

    async def call(action, **kw):
        ob.calls.append((action, kw))
        if action == "get_group_member_list":
            return [{"user_id": FRIEND, "card": "小王", "nickname": "w"},
                    {"user_id": FRIEND2, "card": "阿杰的名片", "nickname": "jie"},
                    {"user_id": 99887766, "card": "", "nickname": "潜水的"}]
        return {"group_name": "画画群"}
    ob.call = call
    asyncio.run(gc.refresh_members())
    assert gc.aliases_of(FRIEND2) == ["王小杰", "jj"]
    assert gc.names(GROUP, members=True)["99887766"] == "潜水的"      # 没说过话的人 @ 他也能显示名字
    assert gc.alias_hits("王小杰是路边一条") == [FRIEND2]
    assert gc.alias_hits("JJ 来了") == [FRIEND2] and gc.alias_hits("jjjj") == []
    gc.set_aliases(FRIEND, "小王同学，小满")                          # 她自己的名字不能当别人的别名
    assert gc.alias_hits("小满你好") == [] and gc.aliases_of(FRIEND) == ["小王同学", "小满"]
    _run(gc, _msg(FRIEND, "小王", "王小杰今天又迟到了", 5))
    ros = gc.roster(GROUP, focus=[FRIEND, *gc.alias_hits("王小杰今天又迟到了")])   # 没说话的阿杰也想起来了
    assert "阿杰的名片（大家也叫：王小杰、jj）" in ros and "说到这些名字" in ros


def test_alias_seed_added_later(cfg, brain):
    """配置里后来又给同一个人加了别名：启动时补上；配置没变就不动（小屋里删掉的不回来）。"""
    gc, ob = _gc(cfg, brain)

    async def call(action, **kw):
        if action == "get_group_member_list":
            return [{"user_id": FRIEND, "card": "小王", "nickname": "w"}]
        return {}
    ob.call = call
    gc.g["people_aliases"] = {str(FRIEND): "王小明"}
    asyncio.run(gc.refresh_members())
    gc.g["people_aliases"] = {str(FRIEND): "王小明、wxm"}
    asyncio.run(gc.refresh_members())
    assert gc.aliases_of(FRIEND) == ["王小明", "wxm"]
    gc.set_aliases(FRIEND, "wxm")                                    # 在小屋里删掉了一个
    asyncio.run(gc.refresh_members())
    assert gc.aliases_of(FRIEND) == ["wxm"]


def test_profile_evidence_and_deep(cfg, brain):
    """3.1 画像：证据里带上他接的是谁的话、别人怎么说他；每天的仔细回想。"""
    from conftest import ProfileLLM
    brain.llm = ProfileLLM()
    gc, ob = _gc(cfg, brain, profile_every=3)
    gc.g["talkativeness"] = 0
    gc.set_aliases(FRIEND, "王小明")
    _run(gc, _msg(FRIEND2, "阿杰", "今晚谁打游戏", 1), _msg(FRIEND, "小王", "我我我", 2),
         _msg(FRIEND2, "阿杰", "王小明又鸽了", 3))
    for i in range(4, 12):
        _run(gc, _msg(FRIEND, "小王", f"论文第{i}章写完了捏", i))
    asyncio.run(gc.profile_people())
    prompt = brain.llm.calls[-1][0]["content"]
    assert "接着阿杰的‹今晚谁打游戏›" in prompt and "王小明又鸽了" in prompt and "大家也叫他：王小明" in prompt
    assert "口头禅" in prompt and "不要当成他的性格或事实" in prompt
    n0 = len(brain.llm.calls)
    assert asyncio.run(gc.deep_profiles()) == 1                     # 小王这周说了 9 句，阿杰只有 2 句
    assert "仔细回想" in brain.llm.calls[-1][0]["content"] and len(brain.llm.calls) == n0 + 1
    from aigroup.memory.people import profile_line
    line = profile_line("小王", {"口头禅": "捏/绝了", "群里关系": "和阿杰常互损", "怎么聊": "夸他论文"}, "挺熟", "")
    assert "口头禅：捏/绝了" in line and "群里关系：和阿杰常互损" in line and "怎么聊：夸他论文" in line
