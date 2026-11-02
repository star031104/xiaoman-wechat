"""34.1 赛程 / 比分的结构化数据源：比分、开赛时间只认这里，搜索引擎只管新闻和背景。

来源（都是公开、不用注册的接口，非官方，可能会变；每个都带缓存，挂了就跳过）：
- 直播吧首页 + 比分 list.htm：几乎所有体育（足球、篮球、羽毛球、乒乓球、网球、斯诺克、排球、F1……）和一部分电竞，北京时间
- 腾讯英雄联盟赛事接口（LPL / 世界赛 / MSI / 德杯）：赛程、比分，北京时间
- 腾讯 TGA 王者荣耀接口（KPL 春夏季赛、年度总决赛、KCC）：赛程、比分，北京时间
- vlr.gg（无畏契约）：赛程和赛果网页，时间用页面上的“还有多久 / 多久前”换算
- Liquipedia（CS2、Dota2、无畏契约、英雄联盟、王者荣耀、彩虹六号、守望先锋、PUBG Mobile、APEX、星际 2……）：
  Liquipedia:Matches 页（action=parse），时间戳是 UTC；它的 API 规定 parse 30 秒一次，这里每个游戏缓存 15 分钟
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone

log = logging.getLogger("aigroup.sports")
TZ8 = timezone(timedelta(hours=8))
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
LP_UA = "XiaomanBot/1.0 (small private group-chat bot; +https://github.com/star031104/xiaoman-wechat)"

# 话题 → 用哪些来源。顺序就是优先级；zhibo8 是兜底（体育和一部分电竞都有）
LOL_RX = re.compile(r"英雄联盟|\bLOL\b|\blol\b|撸啊撸|LPL|LCK|\bS1\d\b|\bS\d{1,2}赛|S赛|[Ww]orlds|世界赛|全球总决赛|\bMSI\b|德杯|T1|BLG|TES|JDG|\bIG\b|\biG\b|WBG|LNG|NIP|\bAL\b|Gen\.?G|HLE|\bDK\b|\bKT\b|\bG2\b|FNC|faker|Faker")
KOG_RX = re.compile(r"王者荣耀|王者|KPL|kpl|KCC|挑战者杯|AG超玩会|成都AG|狼队|TTG|eStar|EDG\.M|北京WB|\bWB\b|DYG|RNG\.M|KSG|DRG|济南RW|LGD\.NBW|南京Hero|Hero久竞|厦门VG|西安WE|TES\.A|情久")
VAL_RX = re.compile(r"无畏契约|瓦洛兰特|瓦罗兰特|valorant|Valorant|VCT|\b瓦\b|瓦的|看瓦|瓦比赛|PRX|Paper Rex|Sentinels|FNATIC|EDG瓦|DRX|Team Heretics")
LP_GAMES = [   # (Liquipedia wiki, 叫法)
    ("counterstrike", re.compile(r"CS2|CSGO|CS:GO|\bCS\b|反恐精英|\bMajor\b|IEM|BLAST|ESL Pro|PGL|NAVI|Navi|Vitality|FaZe|小蜜蜂|TYLOO|MOUZ|Spirit|MongolZ|\bG2\b")),
    ("dota2", re.compile(r"DOTA|Dota|dota|刀塔|\bTI\d*\b|国际邀请赛|Tundra|Team Liquid")),
    ("valorant", VAL_RX),
    ("leagueoflegends", LOL_RX),
    ("honorofkings", KOG_RX),
    ("rainbowsix", re.compile(r"彩虹六号|R6|Rainbow ?Six")),
    ("overwatch", re.compile(r"守望先锋|Overwatch|OWCS")),
    ("pubgmobile", re.compile(r"PUBG ?Mobile|PMGC|PMWC")),
    ("pubg", re.compile(r"\bPUBG\b|绝地求生|吃鸡")),
    ("apexlegends", re.compile(r"APEX|Apex")),
    ("starcraft2", re.compile(r"星际争霸|星际2|SC2|StarCraft")),
    ("mobilelegends", re.compile(r"Mobile Legends|MLBB|无尽对决")),
    ("rocketleague", re.compile(r"火箭联盟|Rocket League|RLCS")),
    ("fighters", re.compile(r"格斗游戏|街霸|Street Fighter|铁拳|Tekken|EVO")),
]
SPORT_RX = re.compile(r"羽毛球|国羽|BWF|乒乓|国乒|WTT|网球|ATP|WTA|大满贯|斯诺克|台球|足球|国足|女足|英超|西甲|意甲|德甲|法甲|欧冠|欧联|中超|亚冠|世界杯|"
                      r"篮球|NBA|CBA|WCBA|NCAA|排球|女排|田径|游泳|跳水|体操|举重|拳击|UFC|F1|赛车|马拉松|高尔夫|冰球|NFL|棒球|MLB|"
                      r"世锦赛|奥运|亚运|全运会|苏迪曼杯|汤尤杯|汤姆斯杯|尤伯杯|和平精英|电竞")
ASK_RX = re.compile(r"比分|赛程|几点|几号|什么时候|啥时候|今[天晚]|明[天晚]|昨[天晚]|谁赢|赢了|输了|晋级|淘汰|对阵|打谁|跟谁打|比赛|开打|结果|决赛|半决赛|小组赛")
STOP = {"比赛", "比分", "赛程", "今天", "今晚", "明天", "明晚", "昨天", "昨晚", "几点", "什么时候", "啥时候", "赢了", "输了", "结果", "谁赢",
        "对阵", "开打", "一下", "知道", "请问", "现在", "这场", "那场", "还有", "多少", "怎么", "是不是", "有没有", "最近", "有啥",
        "有什么", "什么", "哪些", "哪个", "怎么样", "如何", "能不能", "会不会", "还能", "今年", "夺冠", "冠军", "打得", "看看", "这周",
        "下周", "上周", "周末", "这几天", "打谁", "跟谁", "对手", "谁打", "晋级", "淘汰", "决赛", "半决赛", "小组赛", "直播", "比赛吗",
        "几号", "比多少", "赢没", "打完", "开始", "结束", "情况", "消息", "战绩", "排名", "一场", "那个", "这个", "我们", "你们", "他们"}
# 赛事的俗称 → 数据里的叫法（问“世界赛”“S赛”，接口里写的是“全球总决赛”）。只是赛事名的词找不到也不算“没找到”
EVENT_SYN = {"世界赛": ["全球总决赛", "worlds", "world championship"], "s赛": ["全球总决赛", "worlds"],
             "worlds": ["全球总决赛", "worlds"], "全球总决赛": ["全球总决赛", "worlds"], "季中赛": ["msi", "季中"],
             "msi": ["msi", "季中"], "major": ["major"], "总决赛": ["总决赛", "final"], "冠军赛": ["champions", "冠军赛"],
             "冠军杯": ["champions", "冠军杯"], "大师赛": ["masters", "大师赛"], "国际邀请赛": ["the international", "国际邀请赛"],
             "ti": ["the international"], "季后赛": ["playoff", "季后赛"], "常规赛": ["regular", "常规赛"]}
NICK_SYN = {"国乒": ["乒乓", "wtt", "ittf"], "国羽": ["羽毛球", "bwf"], "乒乓": ["乒乓", "wtt", "ittf"],
            "羽毛球": ["羽毛球", "bwf"], "国足": ["国足", "中国"], "网球": ["网球", "atp", "wta"]}
FILL = re.compile(r"[的了吗呢吧啊和跟在是有啥么呀哦嘛也都还就又与及或要能会去看打谁赢输场次个]")


def is_sports(text: str) -> bool:
    t = str(text or "")
    return bool(LOL_RX.search(t) or KOG_RX.search(t) or VAL_RX.search(t) or SPORT_RX.search(t)
                or any(rx.search(t) for _, rx in LP_GAMES))


def tokens(text: str) -> list[str]:
    t = str(text or "")
    out = [w for w in re.findall(r"[A-Za-z][A-Za-z0-9.\-]{1,}|\d+[A-Za-z]+[A-Za-z0-9]*", t) if w.lower() not in ("vs", "bo3", "bo5", "bo1")]
    out += [w for w in ("世界赛", "全球总决赛", "季中赛", "国际邀请赛", "总决赛", "冠军赛", "大师赛", "季后赛", "常规赛") if w in t]
    if re.search(r"(?<![A-Za-z])[Ss]赛", t):
        out.append("S赛")
    for seg in re.findall(r"[\u4e00-\u9fff]{2,}", t):
        seg = re.sub(r"英雄联盟|王者荣耀|无畏契约|瓦洛兰特|瓦罗兰特|反恐精英|刀塔|世界赛|全球总决赛|季中赛|国际邀请赛|总决赛|冠军赛|大师赛|季后赛|常规赛", " ", seg)
        seg = re.sub("|".join(sorted(STOP, key=len, reverse=True)), " ", seg)
        seg = FILL.sub(" ", seg)
        out += [x for x in seg.split() if len(x) >= 2 and x not in STOP]
    return list(dict.fromkeys(out))


def _status(code, done=(), live=(), todo=()) -> str:
    c = str(code)
    return "已结束" if c in done else "进行中" if c in live else "未开始" if c in todo else "状态不明"


def _jsonp(text: str):
    a, b = text.find("{"), text.rfind("}")
    return json.loads(text[a:b + 1]) if a >= 0 and b > a else {}


def _txt(s: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", " ", s or "")).replace("\xa0", " ").strip()


def _eta(s: str) -> timedelta:
    d = h = m = 0
    for n, u in re.findall(r"(\d+)\s*([dhm])", s or ""):
        d, h, m = (d + int(n), h, m) if u == "d" else (d, h + int(n), m) if u == "h" else (d, h, m + int(n))
    return timedelta(days=d, hours=h, minutes=m)


def match_line(m: dict, now: datetime) -> str:
    t = m["time"].astimezone(TZ8)
    hm = f"约{t:%H}点" if m.get("approx") else f"{t:%H:%M}"
    day = "今天" if t.date() == now.date() else "明天" if t.date() == (now + timedelta(days=1)).date() else \
        "昨天" if t.date() == (now - timedelta(days=1)).date() else f"{t:%m-%d}"
    score = f" {m['sa']}:{m['sb']} " if m.get("sa") not in (None, "") and m["status"] != "未开始" else " vs "
    extra = "，".join(x for x in (m.get("league"), m.get("bo"), m["status"]) if x)
    if not str(m.get("b") or "").strip():
        return f"- {day} {hm} {m['a']}（{extra}）"
    return f"- {day} {hm} {m['a']}{score}{m['b']}（{extra}）"


class SportsHub:
    def __init__(self, timeout: float = 8.0):
        self.timeout = timeout
        self._cache: dict[str, tuple[float, object]] = {}
        self._lp_last = 0.0
        self._lock = asyncio.Lock()

    async def _get(self, url: str, ttl: float, *, headers: dict | None = None, data: dict | None = None) -> str:
        hit = self._cache.get(url)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        import httpx
        h = {"User-Agent": UA, "Accept-Encoding": "gzip"}
        h.update(headers or {})
        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as c:
            r = await (c.post(url, headers=h, data=data) if data else c.get(url, headers=h))
        r.raise_for_status()
        self._cache[url] = (time.time(), r.text)
        if len(self._cache) > 120:
            self._cache.pop(next(iter(self._cache)))
        return r.text

    # ---------------- 直播吧 ----------------
    async def zhibo8(self) -> list[dict]:
        page = await self._get("https://www.zhibo8.com/", 600)
        try:
            live = {str(x.get("id")): x for x in json.loads(await self._get("https://bifen4m.qiumibao.com/json/list.htm", 120)).get("list") or []}
        except Exception:  # noqa: BLE001
            live = {}
        out = []
        for li in re.findall(r"<li[^>]*label=\"[^\"]*\"[^>]*>.*?</li>", page, re.S):
            label = re.search(r"label=\"([^\"]*)\"", li).group(1)
            mid = (re.search(r"id=\"saishi(\d+)\"", li) or [None, ""])[1]
            tm = re.search(r"data-time=\"(\d{4}-\d\d-\d\d \d\d:\d\d)\"", li)
            if not tm:
                continue
            league = _txt((re.search(r"class=\"_league\">(.*?)</span>", li, re.S) or [None, ""])[1])
            teams = _txt((re.search(r"class=\"_teams\">(.*?)</span>\s*</b>", li, re.S) or [None, ""])[1])
            a, _, b = teams.partition(" - ")
            x = live.get(mid) or {}
            st = str(x.get("period_cn") or "")
            status = "已结束" if re.search(r"完|结束", st) else "未开始" if (not st or "未" in st) else "进行中"
            out.append({"src": "直播吧", "label": label, "league": league, "a": (x.get("home_team") or a).strip() or league,
                        "b": (x.get("visit_team") or b).strip(), "sa": x.get("home_score") if x else None,
                        "sb": x.get("visit_score") if x else None, "status": status if x else "状态不明（直播吧只有赛程）",
                        "time": datetime.strptime(tm.group(1), "%Y-%m-%d %H:%M").replace(tzinfo=TZ8), "bo": st if "′" in st else ""})
        return out

    # ---------------- 腾讯英雄联盟 ----------------
    async def tencent_lol(self) -> list[dict]:
        base = "https://apps.game.qq.com/lol/match/apis/"
        out = []
        for bgid, name in ((1, "世界赛"), (5, "LPL"), (8, "MSI"), (4, "德杯")):
            try:
                gl = _jsonp(await self._get(f"{base}searchMatchGameInfo.php?r1=gamelist&p1={bgid}", 86400)).get("msg") or []
                if not gl:
                    continue
                g = max(gl, key=lambda x: int(x.get("GameId") or 0))
                end = str(g.get("eDate") or "")[:10]
                if end and end < (datetime.now(TZ8) - timedelta(days=4)).strftime("%Y-%m-%d"):
                    continue                                  # 这个赛事最近一届早结束了
                gid = g["GameId"]
                teams = {str(t.get("TeamId")): t.get("TeamName") for t in
                         (_jsonp(await self._get(f"{base}searchTeamList.php?r1=theTeamlist&p1={gid}", 86400)).get("msg") or [])}
                d = _jsonp(await self._get(f"{base}searchBMatchInfo_bak.php?p8=1&p1={gid}&p6=2&page=1&pagesize=200&r1=retObj", 300))
                for x in ((d.get("msg") or {}).get("result") or []):
                    try:
                        t = datetime.strptime(str(x.get("MatchDate"))[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ8)
                    except ValueError:
                        continue
                    nm = str(x.get("bMatchName") or "")
                    a, _, b = nm.partition(" vs ")
                    gm = str(x.get("GameMode") or "")
                    out.append({"src": "腾讯英雄联盟", "league": f"{g.get('GameName') or name} {x.get('GameProcName') or x.get('GameTypeName') or ''}".strip(),
                                "a": teams.get(str(x.get("TeamA"))) or a, "b": teams.get(str(x.get("TeamB"))) or b,
                                "sa": x.get("ScoreA"), "sb": x.get("ScoreB"), "time": t,
                                "status": _status(x.get("MatchStatus"), done=("3",), live=("2",), todo=("1",)),
                                "bo": {"1": "Bo1", "3": "Bo3", "4": "Bo5", "5": "Bo5"}.get(gm, ""), "game": "lol"})
            except Exception as e:  # noqa: BLE001
                log.info("腾讯英雄联盟 %s 没拿到：%s", name, e)
        return out

    # ---------------- 腾讯王者荣耀 ----------------
    async def tencent_kpl(self) -> list[dict]:
        y = datetime.now(TZ8).year
        out = []
        for sid in (f"KPL{y}S3", f"KPL{y}S2", f"KPL{y}S1", f"KCC{y}", f"KPL{y + 1}S1"):
            try:
                d = json.loads(await self._get(f"https://tga-openapi.tga.qq.com/web/tgabank/getSchedules?seasonid={sid}", 300))
            except Exception:  # noqa: BLE001
                continue
            for x in d.get("data") or []:
                try:
                    t = datetime.fromtimestamp(int(x.get("match_timestamp")), TZ8)
                except (TypeError, ValueError):
                    continue
                out.append({"src": "腾讯王者荣耀", "league": f"{x.get('season') or sid} {x.get('stage_name') or ''}".strip(),
                            "a": x.get("hname") or "待定", "b": x.get("gname") or "待定",
                            "sa": x.get("host_score"), "sb": x.get("guest_score"), "time": t,
                            "status": _status(x.get("match_state"), done=("4",), live=("2", "3"), todo=("1",)),
                            "bo": f"Bo{x.get('bo_total')}" if x.get("bo_total") else "", "game": "kog"})
        return out

    # ---------------- vlr.gg ----------------
    async def vlr(self) -> list[dict]:
        now = datetime.now(TZ8)
        out = []
        for url, done in (("https://www.vlr.gg/matches", False), ("https://www.vlr.gg/matches/results", True)):
            try:
                page = await self._get(url, 300)
            except Exception as e:  # noqa: BLE001
                log.info("vlr.gg 没拿到：%s", e)
                continue
            for blk in page.split("class=\"wf-module-item match-item")[1:60]:
                names = [_txt(x) for x in re.findall(r"match-item-vs-team-name\">\s*<div class=\"text-of\">(.*?)</div>", blk, re.S)]
                scores = [_txt(x) for x in re.findall(r"match-item-vs-team-score[^\"]*\">(.*?)</div>", blk, re.S)]
                st = _txt((re.search(r"ml-status\">(.*?)</div>", blk, re.S) or [None, ""])[1])
                eta = _txt((re.search(r"ml-eta[^\"]*\">(.*?)</div>", blk, re.S) or [None, ""])[1])
                ev = _txt((re.search(r"match-item-event text-of\">(.*?)</div>\s*<div class=\"match-item-icon", blk, re.S) or [None, ""])[1])
                if len(names) < 2:
                    continue
                t = now - _eta(eta) if done else now + _eta(eta)
                status = "已结束" if done else "进行中" if st.upper() == "LIVE" else "未开始"
                out.append({"src": "vlr.gg", "league": re.sub(r"\s+", " ", ev), "a": names[0], "b": names[1],
                            "sa": scores[0] if scores and scores[0] not in ("–", "-", "") else None,
                            "sb": scores[1] if len(scores) > 1 and scores[1] not in ("–", "-", "") else None,
                            "time": t, "status": status, "bo": "", "game": "valorant", "approx": True})
        return out

    # ---------------- Liquipedia ----------------
    async def liquipedia(self, wiki: str) -> list[dict]:
        url = f"https://liquipedia.net/{wiki}/api.php?action=parse&page=Liquipedia:Matches&format=json&prop=text&formatversion=2"
        hit = self._cache.get(url)
        if hit and time.time() - hit[0] < 900:
            raw = hit[1]
        else:
            async with self._lock:                         # Liquipedia：parse 30 秒一次
                wait = 31 - (time.time() - self._lp_last)
                if wait > 0:
                    await asyncio.sleep(wait)                # 刚查过别的游戏：等到 30 秒
                self._lp_last = time.time()
                raw = await self._get(url, 900, headers={"User-Agent": LP_UA})
        text = json.loads(raw).get("parse", {}).get("text", "")
        done_at = text.find("data-toggle-area-content=\"2\"")
        out = []
        for part, done in ((text[:done_at] if done_at > 0 else text, False), (text[done_at:] if done_at > 0 else "", True)):
            for blk in part.split("<div class=\"match-info\">")[1:80]:
                ts = re.search(r"data-timestamp=\"(\d+)\"", blk)
                names = [re.sub(r"\s*\(page does not exist\)", "", html.unescape(n or s_)) for n, s_ in re.findall(r"<span class=\"name\"[^>]*>(?:<a [^>]*title=\"([^\"]*)\"[^>]*>)?([^<]*)", blk)]
                sc = [_txt(x) for x in re.findall(r"match-info-header-scoreholder-score(?: [^\"]*)?\">([^<]*)<", blk)]
                bo = _txt((re.search(r"scoreholder-lower\">(.*?)</span>", blk) or [None, ""])[1]).strip("()")
                tour = _txt((re.search(r"match-info-tournament-name\">.*?<span>(.*?)</span>", blk, re.S) or [None, ""])[1])
                if not ts or len(names) < 2:
                    continue
                t = datetime.fromtimestamp(int(ts.group(1)), timezone.utc)
                fin = done or "data-finished=\"finished\"" in blk
                past = datetime.now(timezone.utc) - t
                status = ("已结束" if fin else "未开始" if past.total_seconds() < 0 else
                          "进行中" if past < timedelta(hours=5) else "状态不明（可能已结束，没拿到赛果）")
                out.append({"src": "Liquipedia", "league": tour, "a": names[0], "b": names[1],
                            "sa": sc[0] if len(sc) > 1 else None, "sb": sc[1] if len(sc) > 1 else None,
                            "time": t, "status": status, "bo": bo, "game": wiki})
        return out

    # ---------------- 汇总 ----------------
    def plan(self, text: str) -> list[str]:
        t = str(text or "")
        srcs = []
        if LOL_RX.search(t):
            srcs += ["tencent_lol"]
        if KOG_RX.search(t):
            srcs += ["tencent_kpl"]
        if VAL_RX.search(t):
            srcs += ["vlr"]
        srcs += [f"lp:{w}" for w, rx in LP_GAMES if rx.search(t) and w not in ("valorant", "leagueoflegends", "honorofkings")]
        if not srcs and (SPORT_RX.search(t) or ASK_RX.search(t)):
            srcs = ["zhibo8"]
        elif srcs:
            srcs += ["zhibo8"]
        return list(dict.fromkeys(srcs))

    async def gather(self, text: str) -> tuple[list[dict], list[str]]:
        srcs = self.plan(text)
        jobs = []
        for s in srcs:
            if s.startswith("lp:"):
                jobs.append(self.liquipedia(s[3:]))
            else:
                jobs.append(getattr(self, s)())
        res = await asyncio.gather(*jobs, return_exceptions=True)
        out, used = [], []
        for s, r in zip(srcs, res):
            if isinstance(r, Exception):
                log.info("赛事数据 %s 出错：%s", s, r)
                continue
            if r:
                used.append(s)
                out += r
        return out, used

    async def brief(self, text: str, *, back_days: float = 3, ahead_days: float = 7, k: int = 12) -> str:
        """和这句话有关的比赛（前 3 天到后 7 天）。没有就返回空字符串。"""
        if not is_sports(text) and not ASK_RX.search(str(text or "")):
            return ""
        try:
            ms, used = await asyncio.wait_for(self.gather(text), 40 if any(x.startswith("lp:") for x in self.plan(text)) else 15)
        except Exception as e:  # noqa: BLE001
            log.info("赛事数据没拿到：%s", e)
            return ""
        if not ms:
            return ""
        now = datetime.now(TZ8)
        lo, hi = now - timedelta(days=back_days), now + timedelta(days=ahead_days)
        ms = [m for m in ms if lo <= m["time"] <= hi]
        toks = [x.lower() for x in tokens(text)]

        def hay(m):
            return " ".join(str(m.get(k_) or "") for k_ in ("a", "b", "league", "label")).lower()
        esports = any(s_ != "zhibo8" for s_ in used)
        if esports:                                         # 问的是电竞：直播吧只留电竞那几行
            ms = [m for m in ms if m["src"] != "直播吧" or "电竞" in str(m.get("label") or "")]
        game_words = {"英雄联盟", "王者荣耀", "王者", "无畏契约", "瓦洛兰特", "电竞", "lol", "kpl", "dota", "cs2", "csgo", "cs",
                      "valorant", "vct", "刀塔", "反恐精英"}
        toks = [x for x in toks if not (esports and x in game_words)]
        def has(x, h):
            return any(a in h for a in EVENT_SYN.get(x) or NICK_SYN.get(x) or [x])
        hit = [m for m in ms if any(has(x, hay(m)) for x in toks)]
        if toks and not hit and all(x in EVENT_SYN for x in toks):   # 只说了“世界赛”“major”这类赛事名：按游戏给最近的
            toks = []
        if toks and not hit:
            return (f"# 赛程比分（赛事数据接口：{'、'.join(used) or '直播吧'}；{now:%m-%d %H:%M} 拉取）\n"
                    f"- 前 {back_days:g} 天到后 {ahead_days:g} 天里没找到和「{'、'.join(toks[:4])}」有关的比赛（直播吧只列有直播的场次）")
        pool = hit or ms
        if not pool:
            return ""
        seen, uniq = set(), []                                # 同一场两个来源都有：留结构化的（排在前面的来源）
        big = re.compile(r"VCT|Champions|Masters|China|Major|IEM|BLAST|ESL Pro|PGL|The International|Riyadh|世界赛|全球总决赛|总决赛")
        for m in sorted(pool, key=lambda m: (m["src"] == "直播吧", m["status"] != "已结束" and m["time"] < now,
                                              not big.search(str(m.get("league") or "")), abs((m["time"] - now).total_seconds()))):
            key = (m["time"].astimezone(TZ8).strftime("%m%d%H"), frozenset({str(m["a"]).lower()[:3], str(m["b"]).lower()[:3]}))
            if key in seen:
                continue
            seen.add(key)
            uniq.append(m)
        uniq = sorted(uniq[:k], key=lambda m: m["time"])
        names = {"tencent_lol": "腾讯英雄联盟", "tencent_kpl": "腾讯王者荣耀", "vlr": "vlr.gg", "zhibo8": "直播吧"}
        src = "、".join(names.get(s, "Liquipedia " + s[3:] if s.startswith("lp:") else s) for s in used)
        approx = any(m.get("approx") for m in uniq)
        return (f"# 赛程比分（赛事数据接口：{src}；{now:%m-%d %H:%M} 拉取，北京时间；比分、时间、对阵以这里为准"
                + ("；vlr.gg 的时间是估算的，精确到小时" if approx else "") + "）\n"
                + "\n".join(match_line(m, now) for m in uniq))
