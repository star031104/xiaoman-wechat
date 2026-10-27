"""25.1：通用的“下判断 / 选购”研究——判断层的 judge / buy 类型、研究员先定标准拿原始数据、常去的数据站、
交卷带 verdict、引用核对也查判断的依据、简报里给她看法、躲判断的检查。"""
import json

from conftest import GROUP, _gc


def test_router_has_judge_and_buy():
    from aigroup.agent import prompts as P
    enum = P.ROUTER_TOOLS[0]["function"]["parameters"]["properties"]["type"]["enum"]
    assert "judge" in enum and "buy" in enum
    assert "锐评" in P.ROUTER_SYSTEM and "type=judge" in P.ROUTER_SYSTEM
    assert "judge" in P.TYPES and "buy" in P.TYPES
    note = P.type_note("judge")
    assert "标准" in note and "原始数据" in note and "verdict" in note
    assert "需求" in P.type_note("buy") and "assume" in P.type_note("buy")
    assert P.type_note("fact").count("verdict") == 0
    assert '"verdict"' in P.RESEARCH_SYSTEM.format(now="x", name="小满", rounds="")


def test_source_hints_cover_domains():
    from aigroup.agent.tools import source_hints
    assert "gol.gg" in source_hints("判断 英雄联盟 世界赛 AL 0-2 TL 谁最差")
    assert "bwf" in source_hints("这场羽毛球石宇奇输在哪").lower()
    assert "rtings" in source_hints("预算 3000 买什么耳机")
    assert "fbref" in source_hints("这个赛季英超哈兰德怎么样")
    assert source_hints("今天天气怎么样") == ""
    assert "x.com" in source_hints("测试一下", {"测试": "测试：x.com"})


def test_parse_verdict_and_dodge_filter():
    from aigroup.agent.mixin import parse_brief
    raw = json.dumps({
        "answer_short": "AL 0-2 TL 出局", "claims": [{"text": "第二局 47 分钟", "sources": [{"site": "hupu"}], "confidence": "高"}],
        "counter": ["查不到任何把败因归到某一个选手身上的权威复盘，不支持点名某人为最差"],
        "verdict": {"call": "下路 Hope 最难受", "why": [{"point": "死得最多", "data": "两局共 9 次死亡", "src": "gol.gg"}],
                    "against": "被针对得最狠", "confidence": "中", "change_if": "看到对线经济差", "assume": ""}}, ensure_ascii=False)
    d = parse_brief(raw)
    assert d["verdict"]["call"] == "下路 Hope 最难受" and d["verdict"]["confidence"] == "medium"
    assert d["counter"] == []                                   # “没有权威复盘”不是辟谣
    d2 = parse_brief(json.dumps({"answer_short": "x", "verdict": {"call": "无法判断谁最差"}}, ensure_ascii=False))
    assert d2["verdict"] == {}
    d3 = parse_brief(json.dumps({"verdict": {"call": "买 A", "why": ["便宜"]}}, ensure_ascii=False))
    assert d3["answer_points"] == ["买 A"] and d3["answerable"]


def test_verify_verdict_drops_made_up_numbers():
    from aigroup.agent.mixin import parse_brief, verify_verdict
    d = parse_brief(json.dumps({"answer_short": "x", "verdict": {"call": "Hope 最差", "why": [
        {"point": "死得最多", "data": "9 次死亡"}, {"point": "伤害最低", "data": "伤害占比 13.5%"}, {"point": "被针对", "data": ""}]}},
        ensure_ascii=False))
    n = verify_verdict(d, "<资料> Hope 0/9/3 ... </资料>")
    assert n == 1 and [w["point"] for w in d["verdict"]["why"]] == ["死得最多", "被针对"]
    d = parse_brief(json.dumps({"answer_short": "x", "verdict": {"call": "Hope 最差", "why": [{"point": "a", "data": "伤害占比 13.5%"}]}}))
    verify_verdict(d, "没有这个数")
    assert d["verdict"]["why"] == [] and d["verdict"]["confidence"] == "low"


def test_brief_block_shows_verdict_and_how_to_say():
    from aigroup.agent.mixin import brief_block, parse_brief
    d = parse_brief(json.dumps({"answer_short": "AL 0-2 出局", "unknowns": ["没有专业评分"], "confidence": "低",
                                "verdict": {"call": "下路 Hope 最难受", "why": [{"point": "死得最多", "data": "9 次"}],
                                            "against": "被针对", "confidence": "中"}}, ensure_ascii=False))
    b = brief_block("判断谁最差", d)
    assert "你看了数据以后的判断：下路 Hope 最难受（大概）" in b and "依据：死得最多（9 次）" in b and "另一种看法：被针对" in b
    assert "别说“没人评过所以不敢说”" in b
    assert "带个“好像”" not in b                                   # 有判断就别再叫她整体含糊
    plain = brief_block("赛程", parse_brief(json.dumps({"answer_short": "01:00 开打"}, ensure_ascii=False)))
    assert "你看了数据以后的判断" not in plain


def test_dodge_check(cfg, brain):
    gc, _ = _gc(cfg, brain)
    web = "# 你刚在手机上查了一下：x\n你看了数据以后的判断：下路 Hope 最难受（大概）"
    assert gc._dodge("谁最差我真不敢点", web)
    assert gc._dodge("是我表达含糊，我只知道 Hope 被针对", web)
    assert not gc._dodge("我看是 Hope，死太多了", web)
    assert not gc._dodge("谁最差我真不敢点", "")                 # 没判断（真没数据）时不逼她


def test_judge_cap_when_mentioned(cfg, brain):
    gc, _ = _gc(cfg, brain)
    assert float(gc.gk(GROUP).get("agent_mention_judge_cap")) >= 40
    note = gc._source_note(GROUP, "判断 英雄联盟 AL TL 谁最差", "judge")
    assert "gol.gg" in note
    assert gc._source_note(GROUP, "判断 英雄联盟 AL TL 谁最差", "fact") == ""


def test_truncated_brief_is_salvaged():
    from aigroup.agent.mixin import close_json, parse_brief
    full = json.dumps({"answer_short": "石宇奇 0-2 输给俞泰彬", "claims": [
        {"text": "17-21、23-25", "sources": [{"site": "bwfbadminton.com", "quote": "Yoo \"created\" the sensation"}], "confidence": "高"},
        {"text": "第二局 17-11 领先被追", "sources": [], "confidence": "中"}],
        "verdict": {"call": "输在第二局局末", "why": [{"point": "丢了 3 个局点", "data": "23-25"}]}}, ensure_ascii=False)
    cut = full[:full.index("第二局 17-11") + 5]                      # 截在第二条说法中间
    d = parse_brief(cut)
    assert d["answer_short"] == "石宇奇 0-2 输给俞泰彬" and d["claims"][0]["text"] == "17-21、23-25"
    assert json.loads(close_json(full))["verdict"]["call"] == "输在第二局局末"
    d = parse_brief('{"answer_short": "只救回这句", "claims": [{"text": "坏"引号"')
    assert d["answer_short"] == "只救回这句"
