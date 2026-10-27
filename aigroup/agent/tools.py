"""19.3 研究员的工具箱：群里的研究员（AgentMixin）用的工具。

工具宁少勿多（Anthropic《为 agent 写工具》）：一共 7 个，每个都把多步操作包好、返回人读得懂的字段、默认截断并告诉它怎么接着看。
    web_search  搜索（可以只搜某个网站）
    read_url    读任何网页：带表格的正文；find 只看和问题有关的段落；links 列出页面上的链接接着点；offset 翻页
    recall      翻她自己的记忆（调用方传进来）
    bili        B 站：搜视频 / 搜 UP 主 / 热门 / 看一个视频（字幕、弹幕、热评，能看某个时间点）
    esports     电竞数据：Liquipedia（选手、战队、赛事），无畏契约 / 英雄联盟 / CS / DOTA2 / 王者荣耀……
    weather     某个城市现在和这两天的天气（Open-Meteo）
    calc        算数（换算、比例、百分比；只认数字和 + - * / ** % 和几个数学函数）
返回的外部内容都包在 <资料> 里；报错写成人话、告诉它下一步怎么办。
"""
from __future__ import annotations

import ast
import logging
import math
import operator
import re
from urllib.parse import urlparse

log = logging.getLogger("aigroup.agent")

PAGE = 6000                                  # read_url 一次给多少字

SCHEMAS = {
    "web_search": {
        "description": "搜索引擎。返回 6 条左右：标题、站点、发布日期（有的没有）、摘要、链接。先用短而宽的词，再收窄；"
                       "知道去哪个网站找最准（比如 vlr.gg、hltv.org、gol.gg、bilibili.com、douban.com）就填 site。"
                       "不知道数据在哪就先搜“对象 + 数据 / stats / 技术统计 / 参数”找数据站。",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "搜索词，像在搜索框里打的（中文或英文）"},
            "days": {"type": "integer", "description": "只要最近几天的新闻（7~30）；0 = 不限（查资料、百科、数据页用 0）"},
            "site": {"type": "string", "description": "只搜这个网站（域名，比如 vlr.gg）；不限就不填"},
        }, "required": ["query"]}},
    "read_url": {
        "description": "打开任何网页读正文（表格也在，Markdown）。被拦、要脚本加载的会自动换办法（仿浏览器、真浏览器、国外的经 Tavily 读）。"
                       "页面长时用 find 只看和问题有关的段落（最省）；links=true 会附上页面上的链接（编号 + 文字 + 网址），可以接着 read_url 点进去；"
                       "没读完会告诉你下一页的 offset。",
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string", "description": "完整网址，从搜索结果或上一页的链接里拿"},
            "find": {"type": "string", "description": "只要和这些词有关的段落，比如“ACS K/D 最近 30 天”；要看全文就不填"},
            "links": {"type": "boolean", "description": "要不要附上页面上的链接（找下一页、找详情页时用）"},
            "offset": {"type": "integer", "description": "从第几个字开始读，默认 0"},
        }, "required": ["url"]}},
    "recall": {
        "description": "翻她自己的记忆：以前聊过的、她记下的事、以前查到过的知识。问到以前的事时先用这个。",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "关键词"},
        }, "required": ["query"]}},
    "bili": {
        "description": "B 站。action=search：搜视频（order 可选 totalrank 综合 / click 播放多 / pubdate 最新）或搜 UP 主（kind=user）；"
                       "action=hot：现在的热门；action=video：看一个视频（标题、简介、字幕原话、弹幕最热闹的几段、热评），"
                       "问到某个时间点就填 at（比如 3:20、第三分钟）。",
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["search", "hot", "video"]},
            "query": {"type": "string", "description": "search 用：搜什么"},
            "kind": {"type": "string", "enum": ["video", "user"], "description": "search 用：搜视频还是搜 UP 主，默认视频"},
            "order": {"type": "string", "enum": ["totalrank", "click", "pubdate"], "description": "search 视频的排序"},
            "bv": {"type": "string", "description": "video 用：BV 号"},
            "at": {"type": "string", "description": "video 用：想看哪个时间点附近（可选）"},
        }, "required": ["action"]}},
    "esports": {
        "description": "电竞数据（Liquipedia）：选手、战队、赛事、阵容、近期成绩、奖金。game 写哪个游戏（无畏契约 / 英雄联盟 / CS / DOTA2 / 王者荣耀 / 守望先锋……），"
                       "query 写选手 / 战队 / 赛事名（英文名最准，比如 aspas、Sentinels、VCT 2026 Champions）。"
                       "页面长，用 find 只看相关的（比如“Results 2026”“Roster”）。每局个人数据（KDA、伤害、经济）Liquipedia 一般没有，"
                       "用 read_url 去数据站看（英雄联盟 gol.gg / lol.fandom.com，无畏契约 vlr.gg，CS hltv.org，DOTA2 opendota.com）。",
        "parameters": {"type": "object", "properties": {
            "game": {"type": "string", "description": "哪个游戏"},
            "query": {"type": "string", "description": "选手 / 战队 / 赛事名"},
            "find": {"type": "string", "description": "只要和这些词有关的段落（可选）"},
        }, "required": ["game", "query"]}},
    "weather": {
        "description": "某个城市现在和今明两天的天气（温度、天气、降水概率、风）。",
        "parameters": {"type": "object", "properties": {
            "city": {"type": "string", "description": "城市名，比如 珠海、Tokyo"},
        }, "required": ["city"]}},
    "calc": {
        "description": "算数：只认数字、+ - * / // % **、括号和 sqrt / log / round / abs / min / max。换算汇率、算比例、算差多少用这个，别心算。",
        "parameters": {"type": "object", "properties": {
            "expr": {"type": "string", "description": "算式，比如 (9999-8999)/8999*100"},
        }, "required": ["expr"]}},
}

DEFAULT = ["web_search", "read_url", "recall", "bili", "esports", "weather", "calc"]

# 20.1 一次研究里搜过的词（tool_loop 开始时换一份新的；同一轮并行的几个调用共用）
import contextvars  # noqa: E402

SEEN: contextvars.ContextVar = contextvars.ContextVar("agent_seen_queries", default=None)
QUESTION: contextvars.ContextVar = contextvars.ContextVar("agent_question", default="")
DEPTH: contextvars.ContextVar = contextvars.ContextVar("agent_depth", default="")   # 21.0 快查用 DeepSeek（快），深查先用 Tavily 免费额度


def _qnorm(q: str) -> str:
    import re as _re
    q = _re.sub(r"\b(site|after|before|lang):\S+", "", (q or "").lower())
    return _re.sub(r"[\s\"'“”，,。.!?！？、:：]+", "", q)


def seen_before(q: str, site: str = "") -> str:
    """和这次研究里已经搜过的几乎一样（字二元组 Jaccard > 0.8，且网站一样）→ 返回那个旧的查询；第一次搜返回空并记下。"""
    seen = SEEN.get()
    if seen is None:
        return ""
    n = _qnorm(q)
    b = {n[i:i + 2] for i in range(len(n) - 1)} or {n}
    for old, osite, ob in seen:
        if osite == site and len(b | ob) and len(b & ob) / len(b | ob) > 0.8:
            return old
    seen.append((q, site, b))
    return ""


# 25.1 常去的数据站：判断题 / 选购题要拿原始数据，研究员不知道某个领域的数据在哪（10-27 只会搜“评分 / 复盘”）。
# 按问题里的词挑几行给它；不在表里的领域靠 JUDGE_HOW 里“先搜‘对象 + 数据 / stats’找数据站”。部署者可以在 net.source_hints 里加。
SOURCE_HINTS: list[tuple[str, str]] = [
    (r"英雄联盟|LOL|LPL|LCK|LEC|LCS|世界赛|S1\d|MSI|德杯|季中",
     "英雄联盟：gol.gg（每局 KDA / 伤害 / 经济，tournament → game → stats）、lol.fandom.com（Leaguepedia 赛事页的 Scoreboard）、"
     "dpm.lol / op.gg esports；中文看玩加电竞、虎扑赛后帖（评价参考）"),
    (r"无畏契约|瓦洛兰特|valorant|VCT", "无畏契约：vlr.gg（每图 ACS / KD / ADR）、rib.gg"),
    (r"\bCS\b|CS2|CSGO|反恐精英|Major", "CS：hltv.org（每图 rating / ADR / KD）"),
    (r"DOTA|刀塔", "DOTA2：opendota.com、dotabuff.com、datdota.com"),
    (r"王者荣耀|KPL", "王者荣耀：KPL 官网 / 王者营地的赛后数据、玩加电竞"),
    (r"羽毛球|BWF|汤杯|尤杯|苏杯|全英赛|石宇奇|安洗莹|林诗栋|郑思维|陈雨菲",
     "羽毛球：bwfbadminton.com（赛程比分、每局比分、交手记录 H2H）、bwf.tournamentsoftware.com（全部比赛、各局比分）、"
     "badmintonranks / 世界排名；技术统计少，就看各局比分走势、关键分、交手记录、近期战绩"),
    (r"乒乓|WTT|樊振东|王楚钦|孙颖莎|马龙", "乒乓：worldtabletennis.com（WTT 赛果、各局比分）、ittf.com 排名、results.ittf.link"),
    (r"网球|ATP|WTA|大满贯|温网|法网|美网|澳网|郑钦文|德约|辛纳|阿尔卡拉斯",
     "网球：atptour.com / wtatennis.com（比赛统计：一发率、破发点、制胜分）、tennisabstract.com（交手和赛季数据）"),
    (r"足球|英超|西甲|意甲|德甲|法甲|欧冠|中超|世界杯|球员|进球",
     "足球：fbref.com（球员 / 球队赛季数据）、sofascore.com / fotmob.com（单场评分、xG、技术统计）、whoscored.com、transfermarkt（身价、伤病）；中文：懂球帝、直播吧"),
    (r"篮球|NBA|CBA|湖人|勇士|詹姆斯|库里", "篮球：basketball-reference.com、nba.com/stats（单场 box score、正负值）、CBA 官网、虎扑数据"),
    (r"F1|一级方程式|排位赛", "F1：formula1.com（成绩、圈速）、statsf1.com"),
    (r"手机|耳机|笔记本|电脑|显卡|CPU|相机|平板|显示器|键盘|鼠标|路由器|手表|电视|值不值|性价比|买哪|推荐.*买|预算",
     "数码选购：官网参数页、京东 / 天猫商品页（价格写日期）、什么值得买（价格史、晒单）、中关村在线 / 极客湾 / 爱否（评测）、"
     "notebookcheck.net（笔记本 / 手机实测）、rtings.com（耳机 / 显示器 / 电视实测）、dxomark（拍照）、B 站评测视频（bili 工具）"),
    (r"电影|电视剧|番|动画|小说|书|专辑|游戏(?!.*(比赛|战队))", "作品：豆瓣（评分、短评）、IMDb、Bangumi（番剧）、Metacritic / OpenCritic（游戏）、Steam 评测"),
    (r"股票|基金|A股|港股|美股|汇率|金价|币", "行情：东方财富、雪球、新浪财经、investing.com（价格写时间，只说事实不荐股）"),
]


def source_hints(text: str, extra: dict | None = None, k: int = 3) -> str:
    """25.1 问题里提到的领域 → 那几行“常去的数据站”（最多 k 行）；extra = 配置里加的 {正则: 说明}。"""
    rows = list(SOURCE_HINTS) + [(str(a), str(b)) for a, b in (extra or {}).items()]
    out = []
    for rx, line in rows:
        try:
            if re.search(rx, text or "", re.I) and line not in out:
                out.append(line)
        except re.error:
            continue
    return "\n".join(f"- {x}" for x in out[:k])


def schemas(names: list[str] | None = None) -> list[dict]:
    return [{"type": "function", "function": {"name": n, **SCHEMAS[n]}} for n in (names or DEFAULT) if n in SCHEMAS]


# ---------------- calc：只算数，不执行别的 ----------------
_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod, ast.Pow: operator.pow, ast.USub: operator.neg,
        ast.UAdd: operator.pos}
_FN = {"sqrt": math.sqrt, "log": math.log, "log10": math.log10, "round": round, "abs": abs, "min": min, "max": max,
       "pi": math.pi, "e": math.e}


def calc(expr: str) -> str:
    e = (expr or "").replace("×", "*").replace("÷", "/").replace("（", "(").replace("）", ")").replace("，", ",")
    e = re.sub(r"(\d+(?:\.\d+)?)\s*%(?!\s*[\d(.])", r"(\1/100)", e)[:200]        # 10% → 0.1；10 % 3 还是取余

    def ev(n):
        if isinstance(n, ast.Expression):
            return ev(n.body)
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)):
            return n.value
        if isinstance(n, ast.BinOp) and type(n.op) in _OPS:
            a, b = ev(n.left), ev(n.right)
            if isinstance(n.op, ast.Pow) and (abs(b) > 100 or abs(a) > 1e6):
                raise ValueError("指数太大")
            return _OPS[type(n.op)](a, b)
        if isinstance(n, ast.UnaryOp) and type(n.op) in _OPS:
            return _OPS[type(n.op)](ev(n.operand))
        if isinstance(n, ast.Name) and n.id in ("pi", "e"):
            return _FN[n.id]
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in _FN and not n.keywords:
            return _FN[n.func.id](*[ev(a) for a in n.args])
        raise ValueError("只能算数字")
    try:
        v = ev(ast.parse(e, mode="eval"))
    except ZeroDivisionError:
        return "（除数是 0）"
    except Exception as ex:  # noqa: BLE001
        return f"（算不了：{ex}；只写数字和 + - * / ** % 括号）"
    if isinstance(v, float):
        v = round(v, 6)
    return f"{expr} = {v}"


class Toolbox:
    """run(name, args) -> 给研究员看的一段文字。需要的东西都由调用方传进来，没有的工具就不开（schemas 里也不给）。"""

    def __init__(self, cfg, links, web, *, recall=None, where=None, tz=None, video_cache=None):
        from aigroup.net.fetch import WebReader
        self.cfg, self.links, self.web = cfg, links, web
        self.reader = WebReader(cfg, links, web)
        self.recall_fn = recall                    # async (query) -> str
        self.where = where
        self.tz = tz
        self._bili = None
        self._lp = None
        self.video_cache = video_cache

    def names(self, recall: bool | None = None) -> list[str]:
        out = list(DEFAULT)
        if not (recall if recall is not None else self.recall_fn is not None):
            out.remove("recall")
        return out

    def schemas(self, recall: bool | None = None) -> list[dict]:
        return schemas(self.names(recall))

    def bili(self):
        if self._bili is None:
            from aigroup.net.sites import BiliSite
            self._bili = BiliSite(self.links.fetch_bytes, self.tz, self.video_cache)
        return self._bili

    def lp(self):
        if self._lp is None:
            from aigroup.net.sites import Liquipedia
            self._lp = Liquipedia()
        return self._lp

    async def run(self, name: str, args: dict, recall=None) -> str:
        """recall：这一次用的“翻记忆”（群里每个群不一样，所以每次传进来）。"""
        if name == "recall":
            fn = recall or self.recall_fn
            return await fn(str((args or {}).get("query") or "")) if fn else "（这里没有她的记忆）"
        fn = getattr(self, "t_" + name, None)
        if fn is None or name not in SCHEMAS:
            return f"（没有 {name} 这个工具）"
        return await fn(args or {})

    # ---------------- 每个工具 ----------------
    async def t_web_search(self, a: dict) -> str:
        from aigroup.agent.mixin import format_results
        try:
            days = max(0, min(365, int(a.get("days") or 0)))
        except (TypeError, ValueError):
            days = 0
        q, site = str(a.get("query") or ""), str(a.get("site") or "").strip()
        dup = seen_before(q, site)
        if dup:                     # 20.1 换个说法搜同一件事（真实日志里 32% 的改写是这样）：不真去搜，提醒它该交卷了
            return (f"（和刚才搜过的「{dup}」几乎一样，不会有新结果。够回答就交卷；不够就换个角度"
                    "（换语言、换实体、加 site: 去数据站），或者 read_url 打开已有的结果）")
        kw = {"site": site} if site else {}
        if QUESTION.get():
            kw["question"] = QUESTION.get()
        if DEPTH.get() == "quick" and getattr(self.web, "ds_key", ""):
            kw["prefer"] = "deepseek"
        res, eng = await self.web.agent_search(q, 6, days, **kw)
        return format_results(res, eng)

    async def t_read_url(self, a: dict) -> str:
        url = str(a.get("url") or "").strip()
        try:
            off = max(0, int(a.get("offset") or 0))
        except (TypeError, ValueError):
            off = 0
        if re.search(r"BV[0-9A-Za-z]{10}", url) and ("bilibili.com" in url or "b23.tv" in url):
            return await self.t_bili({"action": "video", "bv": url, "at": a.get("find") or ""})
        m = re.match(r"https?://(?:www\.)?liquipedia\.net/([a-z0-9]+)/([^?#]+)", url)
        if m and m.group(2) not in ("api.php",):                  # Liquipedia 网页不让直接抓：走它的 API
            from urllib.parse import unquote
            return await self.t_esports({"game": m.group(1), "query": unquote(m.group(2)).replace("_", " "),
                                         "find": a.get("find") or "", "exact": True})
        info = await self.reader.read(url)
        return format_read(info, url, off, str(a.get("find") or ""), bool(a.get("links")))

    async def t_bili(self, a: dict) -> str:
        act = str(a.get("action") or "search")
        b = self.bili()
        try:
            if act == "hot":
                return await b.hot()
            if act == "video":
                return await b.video(str(a.get("bv") or a.get("query") or ""), str(a.get("at") or ""))
            q = str(a.get("query") or "").strip()
            if not q:
                return "（search 要写 query）"
            return await b.search(q, str(a.get("order") or "totalrank"), str(a.get("kind") or "video"))
        except Exception as e:  # noqa: BLE001
            return f"（B 站没拿到：{str(e)[:80]}；可以改用 web_search 加 site=bilibili.com）"

    async def t_esports(self, a: dict) -> str:
        from aigroup.net.fetch import find_passages
        from aigroup.net.sites import game_wiki
        wiki = game_wiki(str(a.get("game") or ""))
        q = str(a.get("query") or "").strip()
        if not q:
            return "（要写 query：选手 / 战队 / 赛事名）"
        lp = self.lp()
        try:
            hits = [{"title": q, "snippet": ""}] if a.get("exact") else await lp.search(wiki, q, 5)
        except Exception as e:  # noqa: BLE001
            return f"（Liquipedia 没连上：{str(e)[:60]}；改用 web_search，或者 read_url 读 vlr.gg / hltv.org）"
        if not hits:
            return f"（Liquipedia 的 {wiki} 里没搜到「{q}」：换英文名、换游戏，或者用 web_search）"
        top = next((h for h in hits if h["title"].lower() == q.lower()), hits[0])
        if a.get("exact"):
            top = {"title": q}
        try:
            p = await lp.page(wiki, top["title"])
        except Exception as e:  # noqa: BLE001
            return f"（「{top['title']}」没读到：{str(e)[:60]}）"
        text = p["text"]
        find = str(a.get("find") or "")
        body = (find_passages(text, find, PAGE) or find_passages(p.get("full") or "", find, PAGE) if find else "") or text[:PAGE]
        more = "" if len(text) <= PAGE or find else f"\n（页面很长，全文 {len(text)} 字：用 find 只看相关的，比如“Results”“Roster”）"
        others = "；".join(h["title"] for h in hits if h is not top)[:200]
        if "/" not in p["title"]:
            more += (f"\n（近期比赛、成绩在子页面：read_url {p['url']}/Results 或 {p['url']}/Matches；"
                     f"实时比分、数据看 vlr.gg / hltv.org）")
        return (f'<资料 来源="Liquipedia（CC-BY-SA）" 日期="无">\n{p["title"]}（{p["url"]}）\n{body}\n</资料>{more}'
                + (f"\n（别的条目：{others}）" if others else ""))

    async def t_weather(self, a: dict) -> str:
        w = self.where
        if w is None:
            from aigroup.net.where import Where

            class _W(Where):                       # 只要查坐标、查天气（不用数据库）
                def __init__(self):
                    self._geo, self._wx = {}, {}
            w = self.where = _W()
        city = str(a.get("city") or "").strip()
        g = await w.geocode(city)
        if not g:
            return f"（没找到「{city}」这个城市：写城市名，比如 珠海、成都、Tokyo）"
        wx = await w.weather(g["lat"], g["lon"])
        if not wx:
            return "（天气没查到，过会儿再试）"
        t, m = wx["today"], wx["tomorrow"]
        return (f'<资料 来源="Open-Meteo" 日期="今天">\n{city}（{g.get("admin", "")}）：现在 {wx["now"]}；'
                f"今天 {t['text']} {t['min']}~{t['max']}°C，降水概率 {t['rain']}%，风 {t['wind']} km/h；"
                f"明天 {m['text']} {m['min']}~{m['max']}°C，降水概率 {m['rain']}%\n</资料>")

    async def t_calc(self, a: dict) -> str:
        return calc(str(a.get("expr") or ""))


def format_read(info: dict, url: str, offset: int = 0, find: str = "", links: bool = False) -> str:
    """WebReader 的结果 → 给研究员看的一段：<资料> 里放正文（或只放相关段落），后面附翻页提示、链接列表。"""
    from aigroup.net.fetch import find_passages
    if not info.get("ok"):
        return f"（{url} 没打开：{info.get('error') or '读不到'}。换一个来源，或者用搜索摘要）"
    text = info.get("text") or ""
    tail = ""
    if find:
        body = find_passages(text, find, PAGE)
        if not body and info.get("full"):
            body = find_passages(info["full"], find, PAGE)             # 正文里没有：在整页看得见的字里再找
        if not body:
            body = text[offset:offset + 1500]
            tail = f"\n（页面里没找到“{find}”相关的，上面是开头一段；全文 {len(text)} 字，可以换个词再 find，或者 offset 翻页）"
        else:
            tail = f"\n（只给了和“{find}”有关的段落；全文 {len(text)} 字）"
    else:
        body = text[offset:offset + PAGE]
        if offset + PAGE < len(text):
            tail = f"\n（没读完：全文 {len(text)} 字，下一页用 offset={offset + PAGE}，或者用 find 只看相关的）"
    if info.get("note"):
        tail += f"\n{info['note']}"
    host = (urlparse(info.get("url") or url).hostname or "").lower()
    head = f'<资料 来源="{info.get("site") or host}" 日期="{info.get("date") or "无"}" 读法="{info.get("via", "")}">\n'
    out = head + f"{info.get('title', '')}\n{body}\n</资料>{tail}"
    if links and info.get("links"):
        out += "\n页面上的链接：\n" + "\n".join(f"[{i}] {x['text']} → {x['url']}" for i, x in enumerate(info["links"][:30], 1))
    return out

