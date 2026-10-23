"""4.0 联网：读群友发的链接、不知道的先搜一搜、把查到的记下来（都用假的网络，不联网）。"""
import asyncio
import json

from conftest import FRIEND, GROUP, _gc, _msg, _run


def test_knowledge_store_and_recall(cfg, brain):
    from aigroup.net.knowledge import Knowledge
    k = Knowledge(brain.db)
    kid = k.add(GROUP, "禁忌岛", ["七日世界", "禁忌岛"], "七日世界的新剧本，10月22日上线，海岛 PVE")
    assert kid and k.add(GROUP, "禁忌岛", ["禁忌岛"], "更新过的说法") == kid          # 同一话题覆盖，不重复
    assert "更新过的说法" in k.prompt_block(GROUP, "周四一起玩禁忌岛吗")
    assert k.prompt_block(GROUP, "今天吃什么") == ""
    assert "禁忌岛" in k.interests_block(GROUP)
    assert k.entries(GROUP)[0]["hits"] == 1


class FakeWeb:
    enabled = True

    def __init__(self):
        self.queries = []

    async def search(self, q, n=5):
        self.queries.append(q)
        return [{"title": "禁忌岛10月22日上线", "url": "https://x", "snippet": "七日世界全新生存剧本，海岛 PVE"}]

    block = staticmethod(__import__("aigroup.net.search", fromlist=["WebSearch"]).WebSearch.block)


def test_search_when_asked_and_remember(cfg, brain):
    gc, ob = _gc(cfg, brain, reply="22号上线，海岛那个")
    gc.web = FakeWeb()
    orig = brain.llm.chat

    async def chat(messages, helper=False, **kw):
        last = messages[-1]["content"]
        if "需要先上网查一下" in last:
            return json.dumps({"need": True, "query": "七日世界 禁忌岛 上线时间"}, ensure_ascii=False)
        if "整理成一条" in last:
            return json.dumps({"topic": "禁忌岛", "keys": ["禁忌岛", "七日世界"],
                               "summary": "七日世界的新剧本，10月22日上线"}, ensure_ascii=False)
        return await orig(messages, helper, **kw)
    brain.llm.chat = chat
    _run(gc, _msg(FRIEND, "小王", "小满 七日世界禁忌岛什么时候上线啊", 1, at=True))
    assert gc.web.queries == ["七日世界 禁忌岛 上线时间"]
    sent_prompt = brain.llm.group_calls[-1][-1]["content"]
    assert "你刚在手机上搜了一下" in sent_prompt and "海岛 PVE" in sent_prompt
    assert ob.sent
    assert gc.knowledge.entries(GROUP)[0]["topic"] == "禁忌岛"
    # 闲聊不查
    gc.web.queries.clear()
    _run(gc, _msg(FRIEND, "小王", "小满 晚上吃啥", 2, at=True))
    assert gc.web.queries == []


def test_no_search_when_model_says_no(cfg, brain):
    gc, ob = _gc(cfg, brain)
    gc.web = FakeWeb()
    orig = brain.llm.chat

    async def chat(messages, helper=False, **kw):
        if "需要先上网查一下" in messages[-1]["content"]:
            return '{"need": false, "query": ""}'
        return await orig(messages, helper, **kw)
    brain.llm.chat = chat
    _run(gc, _msg(FRIEND, "小王", "小满 你觉得我这身衣服怎么样", 1, at=True))
    assert gc.web.queries == []


def test_link_read_into_message(cfg, brain):
    gc, ob = _gc(cfg, brain)

    class FakeLinks:
        async def enrich(self, text, extra=None):
            return "[链接：B站视频《猫猫合集》（UP主 某某）。你只看到了标题和简介，没看过视频本身]"
    gc.links = FakeLinks()
    _run(gc, _msg(FRIEND, "小王", "你们看这个 https://b23.tv/abc", 1))
    row = brain.db.conn.execute("SELECT content FROM qq_group_messages WHERE qq_msg_id=1").fetchone()
    assert "猫猫合集" in row["content"]
    assert gc.knowledge.entries(GROUP)[0]["topic"] == "猫猫合集"


def test_clean_query():
    from aigroup.net.knowledge import clean_query
    assert clean_query("@小满 你知道三体讲了什么吗？", ["小满"]) == "三体讲了什么"


def test_web_search_real_class_offline(cfg):
    from aigroup.net.search import WebSearch
    w = WebSearch({**cfg, "net": {"search": False}})
    assert asyncio.run(w.search("三体")) == []


def test_parse_generic_and_relevant():
    from aigroup.net.search import parse_generic, relevant
    page = ('<script>var a="<h3>x</h3>"</script><h3><a href="u">其他人还搜了</a></h3><p>一些词</p>'
            '<h3 class="t"><a href="https://a">《小王子》内容概括</a></h3><div>讲述了一段关于爱和孤独的故事"}],"clamp":2}</div>'
            '<h3>VCT 冠军 - 百度图片</h3>变清晰 变清晰')
    r = parse_generic(page)
    assert r == [{"title": "《小王子》内容概括", "url": "https://a", "snippet": "讲述了一段关于爱和孤独的故事"}]
    assert relevant("小王子 讲了什么", r) and not relevant("三体", r)


def test_weekly_reflect(cfg, brain):
    gc, ob = _gc(cfg, brain)
    import time as _t
    for i in range(4):
        gc._record(GROUP, 10001, "小满", f"我最近在玩七日世界 {i}", 900 + i, role="assistant")
    gc.knowledge.add(GROUP, "禁忌岛", ["禁忌岛"], "七日世界的新剧本")
    orig = brain.llm.chat

    async def chat(messages, helper=False, **kw):
        if "这一周的我" in messages[-1]["content"]:
            return "- 这周迷上了七日世界\n- 和小王更熟了\n随便一句"
        return await orig(messages, helper, **kw)
    brain.llm.chat = chat
    assert asyncio.run(gc.weekly_reflect()) == 1
    assert brain.db.get(f"qq_reflect_{GROUP}") == "- 这周迷上了七日世界\n- 和小王更熟了"
    assert asyncio.run(gc.weekly_reflect()) == 0                      # 同一周只想一次
    msgs = asyncio.run(gc.build_messages(GROUP, False, False, "小王"))
    assert "这一周的你" in msgs[0]["content"] and _t.time()
