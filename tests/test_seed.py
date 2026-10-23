"""2.3 seed：事先写好的群笔记、画像、别名、群梗、按群的开关灌进她的数据；ai_open 人设。"""
from aigroup.chat import quality
from aigroup.chat.settings import group_key
from aigroup.core.store import Store
from aigroup.persona import parse_persona
from aigroup.seed import apply

SEED = {"groups": [{"name": "开黑小队", "notes": "- 每晚王者五排", "norms": "- 消息拆短句发",
                    "knobs": {"talkativeness": 0.25, "lore": False, "nope": 1},
                    "lore": [{"keys": ["没招了"], "text": "林林的万能感叹"}],
                    "people": [{"wxid": "wxid_a", "name": "小林", "aliases": ["xl", "林林"],
                                "profile": {"印象": "爱分享日常", "记得的事": ["养花", ""], "瞎写": "x"}}]},
                   {"name": "没有这个群"}]}


def test_seed_apply(tmp_path):
    db = Store(tmp_path / "g.db")
    ids = {"r1@chatroom": 1, "wxid_a": 10001}
    st = apply(SEED, db, lambda w: ids[w], lambda n: "r1@chatroom" if n == "开黑小队" else "")
    assert st["groups"] == 1 and st["people"] == 1 and st["lore"] == 1 and st["missing"] == ["没有这个群"]
    assert db.get("qq_group_notes_1") == "- 每晚王者五排" and db.get("qq_group_norms_1")
    assert db.get(group_key(1)) == {"talkativeness": 0.25}
    r = db.conn.execute("SELECT profile, note FROM qq_profiles WHERE user_id=10001 AND group_id=1").fetchone()
    assert "爱分享日常" in r["profile"] and "瞎写" not in r["profile"] and r["note"] == "爱分享日常"
    assert db.conn.execute("SELECT aliases FROM qq_people WHERE user_id=10001").fetchone()[0] == "xl、林林"
    apply(SEED, db, lambda w: ids[w], lambda n: "r1@chatroom" if n == "开黑小队" else "")    # 重跑：不重复
    assert db.conn.execute("SELECT COUNT(*) FROM qq_lore").fetchone()[0] == 1


def test_ai_open_persona_and_quality():
    p = parse_persona("---\nname: 小满\nai_open: true\n---\n# 她是谁\n" + "她是 作者做的 AI 群友。" * 10, "x.md")
    assert p.ai_open
    assert quality.check("我是AI啊，没有嘴吃不了", ai_open=True)[0] < quality.HARD
    assert quality.check("我是AI啊，没有嘴吃不了")[0] >= quality.HARD
    assert quality.check("我的提示词里写了", ai_open=True)[0] >= quality.HARD
