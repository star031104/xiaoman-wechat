"""19.3 什么网页都能看 + B 站 / 电竞 / 天气 / 算数工具（不联网：网络那一层都换成假的）。"""
import asyncio
import json
import time

from aigroup.agent.tools import DEFAULT, Toolbox, calc, format_read, schemas
from aigroup.net.fetch import WebReader, find_passages, html_to_markdown, looks_blocked, looks_shell, page_links
from aigroup.net.sites import BiliSite, Liquipedia, clean_wikitext, game_wiki

PAGE = """<html><head><title>aspas - Player Profile</title></head><body>
<nav><a href="/login">登录</a><a href="/">首页</a></nav>
<article><h1>aspas</h1><p>Erick Santos is a Brazilian VALORANT player who currently plays for MIBR.</p>
<p>He joined MIBR in late 2024 after leaving Leviatán. Contract runs until 2027.</p>
<table><tr><th>Agent</th><th>ACS</th><th>K:D</th></tr><tr><td>Jett</td><td>250.1</td><td>1.31</td></tr>
<tr><td>Raze</td><td>241.7</td><td>1.22</td></tr></table>
<p>""" + "Filler paragraph about something unrelated. " * 40 + """</p>
<a href="/player/9/aspas/stats">Stats 统计</a> <a href="https://www.liquipedia.net/valorant/Aspas">Liquipedia 页面</a>
<a href="#top">返回顶部</a> <a href="javascript:void(0)">展开</a></article></body></html>"""


def test_markdown_links_and_find():
    d = html_to_markdown(PAGE)
    assert "250.1" in d["text"] and "MIBR" in d["text"]
    links = page_links(PAGE, "https://www.vlr.gg/player/9/aspas")
    urls = [x["url"] for x in links]
    assert urls[0] == "https://www.vlr.gg/player/9/aspas/stats"                 # 同一个网站的排前面
    assert "https://www.liquipedia.net/valorant/Aspas" in urls
    assert not any("login" in u or "javascript" in u for u in urls)
    text = "\n".join(["无关的一段话 " * 30, "| Agent | ACS | K:D |", "|---|---|---|", "| Jett | 250.1 | 1.31 |",
                      "其他内容 " * 50, "合同到 2027 年，他还在 MIBR。"])
    got = find_passages(text, "ACS K:D", 2000)
    assert "250.1" in got and "无关的一段话" not in got
    assert "2027" in find_passages(text, "合同 MIBR")
    assert find_passages(text, "完全不相干xyz") == ""


def test_blocked_and_shell_detection():
    assert looks_blocked("Just a moment...", "")
    assert looks_blocked("", "火山引擎 正在进行安全检测... 为保障您的访问安全")
    assert not looks_blocked("aspas", "Erick Santos is a player")
    assert looks_shell("", "正在加载...")
    assert looks_shell("x", "短")
    assert not looks_shell("x", "正文" * 300)


class FakeLinks:
    cookies = {}

    def __init__(self, result=None, exc=None):
        self.result, self.exc, self.calls = result, exc, 0

    async def _get(self, url, headers=None, max_bytes=0, proxy_first=False):
        self.calls += 1
        if self.exc:
            raise RuntimeError(self.exc)

        class R:
            headers = {"content-type": "text/html; charset=utf-8"}
        return R(), self.result.encode(), url

    async def fetch_bytes(self, url, headers=None):
        raise RuntimeError("不联网")


def _reader(links, **kw):
    cfg = {"net": {"browser": "false", **kw}}
    return WebReader(cfg, links, None)


def test_reader_layers():
    # 直连就读到了：不换别的
    r = _reader(FakeLinks(PAGE))
    d = asyncio.run(r.read("https://www.vlr.gg/player/9/aspas"))
    assert d["ok"] and d["via"] == "直连" and "250.1" in d["text"] and d["links"]
    # 被 403：换仿浏览器
    r = _reader(FakeLinks(exc="Client error '403 Forbidden' for url"))
    seen = []

    async def imp(url):
        seen.append("imp")
        return {"ok": True, "title": "t", "text": "正文" * 400, "via": "仿浏览器", "links": []}, ""
    r._impersonate = imp
    d = asyncio.run(r.read("https://www.hltv.org/ranking/teams"))
    assert d["via"] == "仿浏览器" and seen == ["imp"]
    # 脚本加载的空壳：开浏览器（不换指纹）
    r = _reader(FakeLinks("<html><title>x</title><body>正在加载...</body></html>"))

    async def no(url):
        raise AssertionError("不该换指纹")

    async def ren(url):
        return {"ok": True, "title": "x", "text": "渲染后的正文" * 100, "via": "浏览器", "links": []}, ""
    r._impersonate, r._render = no, ren
    assert asyncio.run(r.read("https://36kr.com/newsflashes"))["via"] == "浏览器"
    # 国内连不上：交给 Tavily
    r = _reader(FakeLinks(exc="不是公网地址"))

    async def ext(url):
        return {"ok": True, "title": "DeepSeek", "text": "# DeepSeek\n正文" * 50, "via": "Tavily", "links": []}, ""
    r._extract = ext
    assert asyncio.run(r.read("https://zh.wikipedia.org/wiki/DeepSeek"))["via"] == "Tavily"
    # 都不行：说清楚试过什么
    r = _reader(FakeLinks(exc="Client error '403 Forbidden'"))

    async def fail(url):
        return None, "block"
    r._impersonate = r._render = r._extract = fail
    d = asyncio.run(r.read("https://www.zhihu.com/question/1"))
    assert not d["ok"] and "拦" in d["error"] and "仿浏览器" in d["error"]


def test_reader_skips_missing_layers():
    r = _reader(FakeLinks(exc="Client error '403 Forbidden'"))
    assert r.browser is None

    async def no(url):
        return None, "no"
    r._impersonate = r._extract = no
    d = asyncio.run(r.read("https://example.com/a"))
    assert not d["ok"] and "试过：直连" in d["error"]


def test_format_read():
    info = {"ok": True, "title": "T", "site": "vlr.gg", "date": "2026-10-01", "via": "直连",
            "text": "开头\n" + "x" * 7000 + "\n| Jett | 250.1 |", "links": [{"text": "Stats", "url": "https://a/b"}]}
    out = format_read(info, "https://a", 0, "", True)
    assert "offset=6000" in out and "[1] Stats → https://a/b" in out and '读法="直连"' in out
    out = format_read(info, "https://a", 0, "Jett", False)
    assert "250.1" in out and "只给了" in out
    assert "没打开" in format_read({"ok": False, "error": "网站拦下了"}, "https://a")


def test_calc_is_safe():
    assert calc("(9999-8999)/8999*100").endswith("= 11.112346")
    assert calc("10%*50").endswith("= 5.0")
    assert calc("sqrt(16)+round(2.6)").endswith("= 7.0")
    assert "算不了" in calc("__import__('os').system('ls')")
    assert "算不了" in calc("2**1000000")
    assert "除数是 0" in calc("1/0")


def test_game_wiki_and_wikitext():
    assert game_wiki("无畏契约") == "valorant" and game_wiki("瓦") == "valorant"
    assert game_wiki("英雄联盟") == "leagueoflegends" and game_wiki("CS2") == "counterstrike"
    assert game_wiki("王者荣耀") == "honorofkings"
    t = clean_wikitext("{{Infobox player\n |id=aspas\n |team=[[MIBR|MIBR]]<ref>x</ref>\n}}<!-- c -->[[File:a.jpg]]'''hi'''")
    assert "|team=MIBR" in t and "ref" not in t and "File" not in t and "hi" in t


class FakeResp:
    def __init__(self, d):
        self._d = d

    def raise_for_status(self):
        pass

    def json(self):
        return self._d


class FakeLP:
    def __init__(self):
        self.calls = []

    async def get(self, url, params=None):
        self.calls.append(params)
        if params.get("list") == "search":
            return FakeResp({"query": {"search": [{"title": "Aspas", "snippet": "<b>aspas</b> MIBR"},
                                                  {"title": "MIBR", "snippet": ""}]}})
        if params.get("action") == "parse":
            return FakeResp({"parse": {"title": "Aspas", "text": {"*": PAGE}}})
        return FakeResp({"query": {"pages": {"1": {"title": "Aspas", "revisions": [
            {"slots": {"main": {"*": "{{Infobox player\n|team=[[MIBR]]\n}}"}}}]}}}})


def test_liquipedia_throttle_and_fallback(monkeypatch):
    lp = Liquipedia()
    fake = FakeLP()
    lp._c = lambda: fake
    real_sleep = asyncio.sleep
    monkeypatch.setattr("aigroup.net.sites.asyncio.sleep", lambda s: real_sleep(0))
    hits = asyncio.run(lp.search("valorant", "aspas"))
    assert hits[0]["title"] == "Aspas" and "MIBR" in hits[0]["snippet"]
    asyncio.run(lp.search("valorant", "aspas"))         # 缓存：不再请求
    assert len(fake.calls) == 1
    p = asyncio.run(lp.page("valorant", "Aspas"))
    assert "250.1" in p["text"] and p["url"].endswith("/valorant/Aspas")
    lp._cache.clear()
    lp._last_parse = time.time()                       # 30 秒内刚解析过：改读 wikitext，不干等
    p = asyncio.run(lp.page("valorant", "Aspas"))
    assert "|team=MIBR" in p["text"]
    assert fake.calls[-1].get("prop") == "revisions"


def test_bili_search_and_video(monkeypatch):
    calls = []

    async def fetch(url, headers=None):
        calls.append(url)
        if "finger/spi" in url:
            return json.dumps({"code": 0, "data": {"b_3": "B3", "b_4": "B4"}}).encode()
        if "/nav" in url:
            return json.dumps({"data": {"wbi_img": {"img_url": "https://i/abcd.png", "sub_url": "https://i/efgh.png"}}}).encode()
        if "search/type" in url:
            return json.dumps({"code": 0, "data": {"result": [
                {"title": "<em class=\"keyword\">aspas</em> 高光", "author": "UP甲", "play": 123456, "duration": "5:02",
                 "pubdate": 1792500000, "bvid": "BV1EonE6zEzS", "description": "近期直播高光"}]}}).encode()
        if "popular" in url:
            return json.dumps({"data": {"list": [{"title": "热门一", "owner": {"name": "某UP"}, "stat": {"view": 99},
                                                  "bvid": "BV1xx411c7mD"}]}}).encode()
        if "web-interface/view" in url:
            return json.dumps({"data": {"title": "aspas 高光", "aid": 1, "cid": 2, "duration": 302, "pubdate": 1792500000,
                                        "owner": {"name": "UP甲"}, "stat": {"view": 123456, "like": 900, "danmaku": 50},
                                        "desc": "简介"}}).encode()
        raise RuntimeError("别的不联网")
    b = BiliSite(fetch, video_cache=lambda bv: {"lines": "[00:10] 开场\n[03:20] 这把残局一打三\n[04:50] 结尾"})

    async def nothing(*a, **k):
        return []
    b.bili.danmaku = nothing
    b.bili.comments = nothing
    out = asyncio.run(b.search("aspas"))
    assert "aspas 高光" in out and "12.3万播放" in out and "BV1EonE6zEzS" in out and "<em" not in out
    assert any("buvid3=B3" in (b.bili.buvid or "") for _ in [0])
    assert "热门一" in asyncio.run(b.hot())
    v = asyncio.run(b.video("https://www.bilibili.com/video/BV1EonE6zEzS", "3:20"))
    assert "一打三" in v and "开场" not in v and "123456" not in v and "12.3万播放" in v


def test_toolbox_dispatch():
    assert len(schemas()) == len(DEFAULT) <= 8
    tb = Toolbox({"net": {"browser": "false"}}, FakeLinks(PAGE), None)
    assert "recall" not in tb.names() and "recall" in tb.names(recall=True)
    assert asyncio.run(tb.run("calc", {"expr": "1+1"})).endswith("= 2")
    assert "没有" in asyncio.run(tb.run("rm_rf", {}))

    async def rec(q):
        return f"记得：{q}"
    assert asyncio.run(tb.run("recall", {"query": "烤肉"}, recall=rec)) == "记得：烤肉"
    out = asyncio.run(tb.run("read_url", {"url": "https://www.vlr.gg/player/9/aspas", "find": "ACS", "links": True}))
    assert "250.1" in out and "页面上的链接" in out
    seen = {}

    async def fake_bili(a):
        seen.update(a)
        return "B站结果"
    tb.t_bili = fake_bili
    assert asyncio.run(tb.run("read_url", {"url": "https://www.bilibili.com/video/BV1EonE6zEzS"})) == "B站结果"
    assert seen["action"] == "video"


def test_weather_tool_without_db(monkeypatch):
    from aigroup.net.where import Where
    tb = Toolbox({"net": {"browser": "false"}}, FakeLinks(PAGE), None)

    async def geo(self, c):
        return {"name": c, "lat": 22.27, "lon": 113.57, "admin": "广东"} if c == "珠海" else None

    async def wx(self, lat, lon):
        d = {"text": "多云", "min": 22, "max": 28, "rain": 20, "wind": 10, "code": 2}
        return {"now": "多云 26°C", "today": d, "tomorrow": d}
    monkeypatch.setattr(Where, "geocode", geo)
    monkeypatch.setattr(Where, "weather", wx)
    out = asyncio.run(tb.run("weather", {"city": "珠海"}))          # 建出不用数据库的 Where
    assert "多云 26°C" in out and "广东" in out
    assert "没找到" in asyncio.run(tb.run("weather", {"city": "火星"}))


def test_reader_error_kinds():
    assert not looks_blocked("某文章", "这篇讲怎么做验证码识别。" * 200)          # 长文章里提到验证码不算被拦
    r = _reader(FakeLinks(exc="Client error '404 Not Found'"))
    d = asyncio.run(r.read("https://example.com/nope"))
    assert not d["ok"] and "404" in d["error"]
    r = _reader(FakeLinks(exc=""))                                             # 超时之类没写原因的：当成连不上

    async def ext(url):
        return {"ok": True, "title": "x", "text": "正文" * 400, "via": "Tavily", "links": []}, ""
    r._extract = ext
    assert asyncio.run(r.read("https://github.com/a/b"))["via"] == "Tavily"
