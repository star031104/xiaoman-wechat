import asyncio
import json
import shutil
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["AIGROUP_CHANNEL"] = "wechat"           # 这套测试测的是微信群

from aigroup.chat.group import GroupChat  # noqa: E402

SELF, OWNER, FRIEND, FRIEND2, GROUP = 10001, 55555, 88888, 66666, 777777


def as_plan(reply: str) -> str:
    """老测试写的是纯文字回复：提示词要 JSON 时，像一个听话的模型那样包成 JSON。"""
    import json
    import re
    r = (reply or "").strip()
    if r.startswith("{"):
        return r
    m = re.search(r"\[回应[:：]([^\]]+)\]", r)
    if m and len(re.sub(r"\[[^\]]*\]", "", r).strip()) < 2:
        return json.dumps({"act": "react", "react": m.group(1)}, ensure_ascii=False)
    if "[跳过]" in r:
        return json.dumps({"act": "skip"}, ensure_ascii=False)
    return json.dumps({"act": "say", "len": "long", "say": [x for x in re.split(r"\s*\|{3}\s*", r) if x]},
                      ensure_ascii=False)


class FakeLLM:
    vision = False

    def __init__(self, reply="哈哈哈真的假的"):
        self.reply = reply
        self.raw = False                   # True：原样返回 reply（测解析失败）
        self.calls = []
        self.group_calls = []

    async def chat(self, messages, helper=False, **kw):
        self.calls.append(messages)
        last = messages[-1]["content"]
        if "最近的群聊记录" in last:
            self.group_calls.append(messages)
            if "只输出 JSON" in last and not self.raw:
                return as_plan(self.reply)
            return self.reply
        if "关键词" in last:
            return "画画、水彩、猫、奶茶"
        if "更新你对每个群友的印象" in last:
            return f"{FRIEND}|爱吐槽，最近在赶论文\n群|周六约好一起去看展\n群|“开摆”是群里的口头禅\n我|我养了一只叫芝麻的猫"
        if "今天大概的安排" in last:
            return "09:40-11:50 素描课\n14:00-16:00 图书馆赶作业\n20:00-22:00 追番"
        return "好"


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    from aigroup.core import config as C
    root = Path(C.ROOT)
    (tmp_path / "persona").mkdir()
    shutil.copy(root / "persona" / "template.md", tmp_path / "persona" / "xiaoman.md")
    c = C.load_config(tmp_path / "nope.yaml")
    c["root"] = tmp_path
    c["persona_file"] = "persona/xiaoman.md"
    c["quiet_hours"] = ""
    c["wechat"] = {**c["wechat"], "groups": [GROUP], "owner_id": OWNER,
               "group": {**c["wechat"]["group"], "pace": 0, "merge_wait_seconds": 0, "merge_bubbles": False, "active_hours": "00:00-23:59",
                                     "talk_curve": {}, "typo_rate": 0, "explore_rate": 0, "ablate_rate": 0, "feed": False, "listener_mode": "off", "share_target": 0, "length_match": False, "appraise_enforce": True,
                                     "poke_back_chance": 0}}
    return c


@pytest.fixture
def brain(cfg):
    from aigroup.brain import GroupBrain
    b = GroupBrain(cfg)
    b.llm = FakeLLM()
    return b


class ProfileLLM(FakeLLM):
    async def json(self, messages, default=None, role="helper"):
        self.calls.append(messages)
        return {str(FRIEND): {"印象": "嘴硬心软的论文战士", "称呼": "王哥", "性格": "爱吐槽、靠谱",
                              "说话风格": "句尾爱加“捏”，爱发表情包", "兴趣": "原神、猫", "近况": "在赶毕业论文",
                              "和你": "常互损", "别碰": "他的发际线", "记得的事": ["养了一只橘猫叫团子", "周六要答辩"]}}


class LearnLLM(FakeLLM):
    def __init__(self):
        super().__init__()
        self.jcalls = []

    async def json(self, messages, default=None, role="helper"):
        c = messages[-1]["content"]
        self.jcalls.append(c)
        if "styles" in c:
            return {"styles": [{"situation": "对某件事很惊讶", "style": "我嘞个豆"}, {"situation": "x", "style": "短"}],
                    "terms": ["yyds", "开摆", "不存在的词"]}
        if "不看任何上下文" in c:
            return {"开摆": "开始摆放东西", "yyds": "永远的神"}
        if "differs" in c:
            return {"开摆": {"meaning": "躺平不干了", "differs": True}, "yyds": {"meaning": "永远的神", "differs": False}}
        return default


# ---------------- 测试共用：假的 NapCat、建 GroupChat、造消息、跑完后台任务 ----------------
GROUP2 = 888888


def J(**kw):
    return json.dumps(kw, ensure_ascii=False)


class OB:
    self_id = SELF
    ready = True

    def __init__(self):
        self.sent, self.calls = [], []

    async def send_group(self, gid, segs):
        self.sent.append((gid, segs))
        return 900 + len(self.sent)

    async def call(self, action, **kw):
        self.calls.append((action, kw))
        if action == "get_group_member_info":
            return {"card": "新来的"}
        return {"group_name": "画画群"}


def _gc(cfg, brain, reply="哈哈哈真的假的", **g):
    brain.llm.reply = reply
    cfg["wechat"]["group"].update(g)
    ob = OB()
    return GroupChat(cfg, brain, ob, OWNER, [GROUP], ["满满"]), ob


def _msg(uid, name, text, mid, at=False):
    segs = ([{"type": "at", "data": {"qq": str(SELF)}}] if at else []) + [{"type": "text", "data": {"text": text}}]
    return {"post_type": "message", "message_type": "group", "group_id": GROUP, "user_id": uid, "message_id": mid,
            "sender": {"card": name}, "message": segs}


def _run(gc, *evs):
    async def go():
        for e in evs:
            await gc.on_event(e)
        for _ in range(50):
            await asyncio.sleep(0.01)
            if not gc._bg:
                break
        await asyncio.gather(*list(gc._bg), return_exceptions=True)
    asyncio.run(go())
