"""5.0 进群前的准备：导出的聊天记录 → 大模型整理成种子草稿 → 灌进去以后算“事先听说的”。"""
import asyncio
import time

from aigroup.onboard import build_seed, names_habit, parse_export
from aigroup.seed import apply

EXPORT = """# 画画群
导出时间 2026-10-20 10:00，共 4 条

## 成员（“我”= 导出用的这个号）
- 我｜微信昵称 阿杰｜备注 ｜wxid_owner｜说了 1 条
- 小王｜微信昵称 王王｜备注 王哥｜wxid_wang｜说了 6 条
- 小满｜微信昵称 小满｜备注 ｜wxid_her｜说了 9 条

## 聊天记录
""" + "\n".join(f"2026-10-1{i % 9} 2{i % 4}:0{i % 9} 小王：今天又画了一张猫 {i}" for i in range(6)) + """
2026-10-19 22:00 我：[图片]
2026-10-19 22:01 我：王哥画得不错
"""


class LLM:
    def __init__(self):
        self.calls = []

    async def json(self, messages, default=None, role="helper"):
        c = messages[-1]["content"]
        self.calls.append(c)
        if "提前了解这个群" in c:
            return {"notes": ["最近大家在画猫"], "norms": ["深夜最活跃"], "lore": [{"keys": ["开摆"], "text": "不干了"}]}
        return {"aliases": ["王哥"], "印象": "爱画猫的", "称呼": "王哥", "近况": "在赶稿", "别碰": "", "记得的事": ["养了一只橘猫"]}


def test_parse_and_build(tmp_path, cfg):
    f = tmp_path / "画画群.txt"
    f.write_text(EXPORT, encoding="utf-8")
    ex = parse_export(f)
    assert ex["name"] == "画画群" and [m["id"] for m in ex["members"]] == ["wxid_owner", "wxid_wang", "wxid_her"]
    assert len(ex["lines"]) == 8 and ex["lines"][0][1] == "小王"
    llm = LLM()
    seed = asyncio.run(build_seed(cfg, [ex], llm, "小满", progress=lambda *_: None))
    g = seed["groups"][0]
    assert g["name"] == "画画群" and "画猫" in g["notes"] and g["lore"][0]["keys"] == ["开摆"]
    p = g["people"][0]
    assert p["wxid"] == "wxid_wang" and p["aliases"] == ["王哥"] and "别碰" not in p["profile"]   # 空字段不写
    assert len(llm.calls) == 2                                    # 一个群 + 一个常说话的人（“我”只说了一句）
    assert all("[图片]" not in c for c in llm.calls)              # 图片这种没内容的不喂给模型

    from aigroup.core.store import Store
    db = Store(tmp_path / "g.db")
    st = apply(seed, db, lambda x: {"wxid_wang": 7}.get(x, 9), lambda name: "chat1")
    assert st["people"] == 1 and db.get(f"qq_seeded_{9}") == [7]


def test_names_habit():
    lines = [(time.time(), "a", "今天吃啥") for _ in range(150)]
    assert names_habit(lines, ["小王", "老李"]) < 0.04
    lines += [(time.time(), "a", "小王你来不来") for _ in range(20)]
    assert names_habit(lines, ["小王", "老李"]) > 0.04
    assert names_habit(lines[:50], ["小王"]) == 1.0                # 太少：不判断
