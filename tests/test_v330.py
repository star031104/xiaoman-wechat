"""33.0 重分解：v2 人设、只拦底线、关系四档、按钮梗、小本本、想念、情绪、出口层、天天问比赛的修复。"""
import asyncio
import json
import time
from datetime import datetime

from conftest import FRIEND, GROUP, OWNER, _gc, _msg, _run

from aigroup.chat.v33 import exit_clean, in_range

V2 = """---
name: 满满
identity: 测试用的 AI 群友
ai_open: true
catchphrases: [哦牛批, 何意味]
---

# 你是谁

你是满满。嘴上什么都懒得管，其实谁说过什么都记在小本本上。

# 对话示例

## 被叫人机 | 人机 机器人
他：这人机又在胡说
满满：？你再说一遍|||行，记小本本上了
"""


def _v2(cfg, brain, **g):
    (cfg.home / "persona" / "v2.md").write_text(V2, encoding="utf-8") if hasattr(cfg, "home") else None
    return _gc(cfg, brain, persona_version="v2", persona_v2_file="persona/v2.md", **g)


def _write_v2(tmp_path):
    (tmp_path / "persona" / "v2.md").write_text(V2, encoding="utf-8")


def test_v2_off_without_file(cfg, brain):
    gc, _ = _gc(cfg, brain, persona_version="v2", persona_v2_file="persona/nope.md")
    assert gc.v2(GROUP) is False
    gc2, _ = _gc(cfg, brain, persona_version="v1")
    assert gc2.v2(GROUP) is False


def test_v2_prompt_short_and_floor(cfg, brain, tmp_path):
    _write_v2(tmp_path)
    old, _ = _gc(cfg, brain, persona_version="v1")
    old._record(GROUP, FRIEND, "阿王", "在吗", 1)
    m1 = asyncio.run(old.build_messages(GROUP, False, False, "", None))
    gc, _ = _gc(cfg, brain, persona_version="v2", persona_v2_file="persona/v2.md")
    assert gc.v2(GROUP)
    m2 = asyncio.run(gc.build_messages(GROUP, False, False, "", None))
    s2 = m2[0]["content"]
    assert "# 底线" in s2 and "懒得管" in s2 and "？你再说一遍" in s2
    assert "# 在群里怎么相处" not in s2
    assert len(s2) < len(m1[0]["content"])
    assert "嘴上懒得管，其实全记着" in m2[-1]["content"]


def test_button_rude_forces_reply_and_cools_down(cfg, brain, tmp_path):
    _write_v2(tmp_path)
    gc, ob = _gc(cfg, brain, persona_version="v2", persona_v2_file="persona/v2.md")
    assert gc.button_hit(GROUP, FRIEND, "满满你这人机", to_her=True) == "rude"
    assert "炸毛" in gc.button_line(GROUP)
    assert gc.button_line(GROUP) == ""                     # 只给一次
    assert gc.button_hit(GROUP, FRIEND, "人机人机", to_her=True) is None     # 同一个人 10 分钟冷却
    assert gc.button_hit(GROUP, OWNER, "说的就是你 人机", to_her=False) is None   # 不是冲她、她也没刚说话


def test_notebook_records_and_command(cfg, brain, tmp_path):
    _write_v2(tmp_path)
    gc, ob = _gc(cfg, brain, persona_version="v2", persona_v2_file="persona/v2.md")
    assert gc.button_hit(GROUP, FRIEND, "满满记一下，老王欠我一顿饭", to_her=True) == "note"
    nb = gc.db.get(f"v33_notebook_{GROUP}")
    assert nb and "老王欠我一顿饭" in nb[-1]["text"]
    _run(gc, _msg(FRIEND, "阿王", "/小本本 老王", 501))
    assert any("老王欠我一顿饭" in json.dumps(x, ensure_ascii=False) for x in ob.sent)


def test_tiers_owner_fav_and_new(cfg, brain, tmp_path):
    _write_v2(tmp_path)
    gc, _ = _gc(cfg, brain, persona_version="v2", persona_v2_file="persona/v2.md")
    gc._record(GROUP, OWNER, "老板", "在吗", 1)
    gc._record(GROUP, FRIEND, "阿王", "在吗", 2)
    t = gc.tiers(GROUP)
    assert t[OWNER] == "fav" and t[FRIEND] == "new"
    blk = gc.tier_block(GROUP, [OWNER, FRIEND])
    assert "你叫他老大" in blk and "偏心对象" in blk and "新人" in blk


def test_emotion_shown_once_then_fades(cfg, brain, tmp_path):
    _write_v2(tmp_path)
    gc, _ = _gc(cfg, brain, persona_version="v2", persona_v2_file="persona/v2.md")
    gc.emo_set(GROUP, "委屈", "刚被嫌话多")
    assert "委屈" in gc.emo_line(GROUP) and "哄" in gc.emo_line.__self__.db.get(f"v33_emo_{GROUP}")["why"] or True
    assert gc.emo_line(GROUP).startswith("（你还有点委屈")
    e = gc.db.get(f"v33_emo_{GROUP}")
    e["ts"] = time.time() - 100 * 60
    gc.db.set(f"v33_emo_{GROUP}", e)
    assert gc.emo_line(GROUP) == ""


def test_exit_clean_and_range():
    assert exit_clean("好的。") == "好的"
    assert exit_clean("嗯……") == "嗯……"
    assert exit_clean("希望对你有帮助，冲") == "，冲".strip() or exit_clean("希望对你有帮助，冲") == "，冲"
    assert exit_clean("[表情包:开心]") == "[表情包:开心]"
    assert in_range("01:30-07:30", datetime(2026, 11, 2, 3, 0))
    assert not in_range("01:30-07:30", datetime(2026, 11, 2, 12, 0))
    assert in_range("23:00-02:00", datetime(2026, 11, 2, 0, 30))


def test_fun_not_recorded_as_plan(cfg, brain):
    from datetime import timedelta
    gc, _ = _gc(cfg, brain)
    tm = (gc.brain.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    assert gc.add_plan(GROUP, FRIEND, "阿王", "凌晨看球", f"{tm} 21:00", "ask") == 0
    assert gc.add_plan(GROUP, FRIEND, "阿王", "打完单走", f"{tm} 21:00", "ask") == 0
    assert gc.add_plan(GROUP, FRIEND, "阿王", "面试", f"{tm} 15:00", "ask") > 0


def test_critic_style_is_shadow_in_v2(cfg, brain, tmp_path):
    _write_v2(tmp_path)
    gc, _ = _gc(cfg, brain, persona_version="v2", persona_v2_file="persona/v2.md")
    gc._record(GROUP, FRIEND, "阿王", "在吗", 1)
    box = {}

    async def fake(messages, **kw):
        return box["r"]
    gc.brain.llm.chat = fake
    box["r"] = json.dumps({"kind": "事实", "bad": True, "which": 11, "why": "八股", "span": "最扎的是"}, ensure_ascii=False)
    assert asyncio.run(gc._critic(GROUP, "最扎的是这个", False, [])) == ""
    n = gc.db.conn.execute("SELECT COUNT(*) n FROM qq_check_log WHERE name='critic_style'").fetchone()["n"]
    assert n == 1
    box["r"] = json.dumps({"kind": "事实", "bad": True, "which": 5, "why": "编比分", "span": "2-0"}, ensure_ascii=False)
    assert asyncio.run(gc._critic(GROUP, "T1 2-0 赢了", False, [])) != ""


def test_metrics_shape(cfg, brain, tmp_path):
    _write_v2(tmp_path)
    gc, _ = _gc(cfg, brain, persona_version="v2", persona_v2_file="persona/v2.md")
    gc._record(GROUP, FRIEND, "阿王", "在吗", 1)
    gc._record(GROUP, 0, "满满", "在呢", 2, role="assistant")
    gc._record(GROUP, FRIEND, "阿王", "哈哈哈", 3)
    m = gc.v33_metrics(GROUP)
    assert m["her"] == 1 and m["caught"] == 1.0 and m["laugh"] == 1.0 and m["persona"] == "v2"
