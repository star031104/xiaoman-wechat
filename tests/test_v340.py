"""34.0 / 34.1：赛事数据接口、搜索时效硬过滤、表情包按意思挑、截图 OCR、“上周 / 前几天”、事实被谁取代。"""
import asyncio
import json
import time
from datetime import date, datetime, timedelta

from conftest import FRIEND, GROUP, _gc

from aigroup.memory.autobio import range_refs
from aigroup.net.search import fresh_filter
from aigroup.net.sports import TZ8, SportsHub, is_sports, match_line, tokens

Z8 = ('<li label="电竞,游戏,英雄联盟,LOL-IG,TES,S16,S赛淘汰赛1/4决赛" id="saishi9" data-time="{t}" data-type="game"><time>06:00</time>'
      '<b><span class="_league">S16淘汰赛1/4决赛</span><span class="_teams"> IG <img src="x.png"/><span> - </span><img src="y.png"/> TES</span></b></li>'
      '<li label="综合,乒乓球,王楚钦,其他,林德,WTT法兰克福冠军赛" id="saishi8" data-time="{t}" data-type="tabletennis"><time>20:45</time>'
      '<b><span class="_league">WTT法兰克福冠军赛男单第1轮</span><span class="_teams"> 林德 <img/><span> - </span><img/> 王楚钦</span></b></li>')
LP = ('<div data-toggle-area-content="1"><div class="match-info"><span class="timer-object" data-timestamp="{ts}">x</span>'
      '<span class="name" style=""><a href="/cs/NAVI" title="Natus Vincere">NAVI</a></span>'
      '<span class="match-info-header-scoreholder-scorewrapper"><span class="match-info-header-scoreholder-upper">'
      '<span class="match-info-header-scoreholder-score">1</span><span class="match-info-header-scoreholder-score">0</span></span>'
      '<span class="match-info-header-scoreholder-lower">(Bo3)</span></span>'
      '<span class="name" style=""><a href="/cs/F" title="Team Falcons">Falcons</a></span>'
      '<span class="match-info-tournament-name"><a href="#"><span>IEM Beijing 2026</span></a></span></div></div>'
      '<div data-toggle-area-content="2"><div class="match-info"><span class="timer-object" data-timestamp="{ts2}" data-finished="finished">x</span>'
      '<span class="name"><a title="FaZe Clan">FaZe</a></span><span class="match-info-header-scoreholder-score match-info-header-winner">2</span>'
      '<span class="match-info-header-scoreholder-score">0</span><span class="name"><a title="FURIA (page does not exist)">FURIA</a></span>'
      '<span class="match-info-tournament-name"><a><span>IEM Beijing 2026</span></a></span></div></div>')


def _hub(pages: dict):
    h = SportsHub()

    async def get(url, ttl, **kw):
        for k, v in pages.items():
            if k in url:
                return v
        raise RuntimeError("no page")
    h._get = get
    return h


def test_tokens_and_topic():
    assert tokens("今晚有没有羽毛球比赛") == ["羽毛球"]
    assert tokens("IG和TES几点打") == ["IG", "TES"]
    assert tokens("dota 最近有啥比赛") == ["dota"]
    assert is_sports("CS2 Major") and is_sports("国乒今天打谁") and not is_sports("今晚吃啥")


def test_zhibo8_and_filter():
    t = (datetime.now(TZ8) + timedelta(hours=5)).strftime("%Y-%m-%d %H:%M")
    h = _hub({"zhibo8.com": Z8.format(t=t), "list.htm": json.dumps({"list": []})})
    out = asyncio.run(h.brief("王楚钦 乒乓球 什么时候"))
    assert "林德 vs 王楚钦" in out and "IG" not in out
    none = asyncio.run(h.brief("今晚有没有羽毛球比赛"))
    assert "没找到" in none and "羽毛球" in none


def test_liquipedia_parse_scores_and_names():
    now = time.time()
    h = _hub({"liquipedia.net": json.dumps({"parse": {"text": LP.format(ts=int(now - 1800), ts2=int(now - 86400))}}),
              "zhibo8.com": "", "list.htm": "{}"})
    h._lp_last = 0
    ms = asyncio.run(h.liquipedia("counterstrike"))
    live = next(m for m in ms if m["a"] == "Natus Vincere")
    assert (live["sa"], live["sb"], live["status"], live["bo"]) == ("1", "0", "进行中", "Bo3")
    done = next(m for m in ms if m["a"] == "FaZe Clan")
    assert done["b"] == "FURIA" and done["status"] == "已结束" and done["sa"] == "2"
    line = match_line(done, datetime.now(TZ8))
    assert "FaZe Clan 2:0 FURIA" in line


def test_fresh_filter_drops_old_and_undated():
    today = date(2026, 11, 3)
    res = [{"date": "2026-11-02"}, {"date": "2026-10-20"}, {"date": ""}, {"date": "2026-11-01 08:00"}]
    assert [r["date"][:10] for r in fresh_filter(res, 3, today)] == ["2026-11-02", "2026-11-01"]
    assert fresh_filter(res, 0, today) == res


def test_range_refs():
    now = datetime(2026, 11, 3, 10)
    a, b = range_refs("上周那个事", now)
    assert a == datetime(2026, 10, 26) and b == datetime(2026, 11, 2)
    assert range_refs("前几天你说的", now)[0] == datetime(2026, 10, 30)
    assert range_refs("上周五", now) is None and range_refs("今天", now) is None


def test_sticker_match_by_meaning(cfg, brain):
    gc, _ = _gc(cfg, brain)
    st = gc.stickers
    st.db.conn.execute("INSERT INTO qq_stickers(key, path, groups, users, seen, contexts, descr, emotion, usage, meaning, created) "
                       "VALUES ('k1','a.png',?, '[]', 3, ?, '斜眼', '无语', '对离谱发言', '无语到想笑', 0)",
                       (json.dumps([GROUP]), json.dumps([["阿王：这也行？"]], ensure_ascii=False)))
    st.db.conn.commit()
    assert st.match(GROUP, "我真的无语到想笑", 0.2)["key"] == "k1"
    assert st.match(GROUP, "明天考试加油", 0.35) is None
    assert st.match(GROUP + 1, "无语到想笑", 0.2) is None          # 别的群没见过


def test_sticker_steal_first_sight(cfg, brain):
    gc, _ = _gc(cfg, brain)
    st = gc.stickers
    st.steal_prob = 1.0
    r = st.seen(GROUP, FRIEND, "abc123", "http://x/abc123.gif", ["阿王：哈哈"], small=True)
    assert r["need_fetch"]
    st.steal_prob = 0.0
    r2 = st.seen(GROUP, FRIEND, "def456", "http://x/def456.gif", ["阿王：哈哈"], small=True)
    assert not r2["need_fetch"]
    r3 = st.seen(GROUP, FRIEND, "def456", "http://x/def456.gif", ["阿王：哈哈"], small=True)
    assert r3["need_fetch"]                                     # 小群第二次见到就收


def test_fact_replaced_by(cfg, brain):
    gc, _ = _gc(cfg, brain)
    m = gc.memory
    old = m.add_fact(GROUP, "阿王在深圳实习", speaker=FRIEND, subject=FRIEND, kind="fact", source="self")
    new = m.add_fact(GROUP, "阿王换到广州实习了", speaker=FRIEND, subject=FRIEND, kind="fact", source="self", replaces=[old])
    r = m.db.conn.execute("SELECT replaced_by, t_invalid FROM qq_facts WHERE id=?", (old,)).fetchone()
    assert r["replaced_by"] == new and r["t_invalid"]
    assert m.before_text({"id": new}) == "阿王在深圳实习"


def test_ocr_graceful():
    from aigroup.net.ocr import ocr_text
    assert ocr_text(b"") == "" and ocr_text(b"not an image") == ""
