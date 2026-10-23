"""19.3 几个常用网站的“专用通道”：比通用读网页更准、更省（直接拿结构化数据）。

- B 站：搜视频 / 搜 UP 主（wbi 签名 + 游客 cookie）、热门、看一个视频（简介、字幕或你们之前看过记下的原话、弹幕最热闹的几段、热评；
  问到某个时间点就给那附近的原话）。看视频的重活（听写、截画面）还是群里有人发视频时由 knowledge.py 做，这里只读现成的
- 电竞：Liquipedia（无畏契约、英雄联盟、CS、DOTA2、王者荣耀、守望先锋……都是同一套 MediaWiki）：先搜条目再读页面
  （选手 / 战队 / 赛事页的信息框、近期成绩、阵容）。按它的 API 规矩来：带项目名和联系方式的 User-Agent、gzip、
  所有请求 2 秒一次、解析页面 30 秒一次、结果缓存 6 小时；内容是 CC-BY-SA 3.0，引用时注明 Liquipedia
"""
from __future__ import annotations

import asyncio
import html as _html
import json
import logging
import re
import time
from datetime import datetime
from urllib.parse import quote, urlencode

import httpx

log = logging.getLogger(__name__.split(".")[0] + ".sites")

# ================= 电竞：Liquipedia =================
LP_UA = "xiaoman-wechat/21.1 (https://github.com/star031104/xiaoman-wechat; low-volume group-chat assistant)"
GAMES = {
    "valorant": ("无畏契约", "瓦罗兰特", "瓦", "valorant", "val"),
    "leagueoflegends": ("英雄联盟", "lol", "撸啊撸", "league of legends"),
    "counterstrike": ("cs", "cs2", "csgo", "反恐精英", "counter-strike", "counter strike"),
    "dota2": ("dota", "dota2", "刀塔"),
    "honorofkings": ("王者荣耀", "王者", "honor of kings", "kog"),
    "overwatch": ("守望先锋", "ow", "overwatch"),
    "pubg": ("绝地求生", "吃鸡", "pubg"),
    "apexlegends": ("apex", "apex英雄", "apex legends"),
    "rainbowsix": ("彩虹六号", "r6", "rainbow six"),
    "mobilelegends": ("无尽对决", "mlbb", "mobile legends"),
    "starcraft2": ("星际争霸2", "星际2", "sc2", "starcraft"),
    "rocketleague": ("火箭联盟", "rocket league"),
    "fighters": ("格斗", "街霸", "street fighter", "tekken", "铁拳"),
    "deltaforce": ("三角洲", "三角洲行动", "delta force"),
}


def game_wiki(game: str) -> str:
    g = re.sub(r"\s+", " ", (game or "").strip().lower())
    if g in GAMES:
        return g
    for wiki, names in GAMES.items():
        if g in names or any(n in g for n in names if len(n) >= 3):
            return wiki
    return "valorant" if not g else re.sub(r"[^a-z0-9]", "", g) or "valorant"


class Liquipedia:
    def __init__(self, timeout: float = 15.0):
        self.timeout = timeout
        self._lock = asyncio.Lock()
        self._last = 0.0
        self._last_parse = 0.0
        self._cache: dict[str, tuple[float, object]] = {}
        self._client: httpx.AsyncClient | None = None

    def _c(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=self.timeout, follow_redirects=True,
                                             headers={"User-Agent": LP_UA, "Accept-Encoding": "gzip"})
        return self._client

    async def _api(self, wiki: str, params: dict, parse: bool = False, max_wait: float = 12.0) -> dict:
        ck = wiki + "|" + json.dumps(params, sort_keys=True, ensure_ascii=False)
        hit = self._cache.get(ck)
        if hit and time.time() - hit[0] < 6 * 3600:
            return hit[1]                                         # type: ignore[return-value]
        async with self._lock:
            wait = max(0.0, self._last + 2.0 - time.time())
            if parse:
                wait = max(wait, self._last_parse + 30.0 - time.time())
            if wait > max_wait:
                raise TimeoutError(f"Liquipedia 要等 {wait:.0f} 秒才能再读")
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                r = await self._c().get(f"https://liquipedia.net/{wiki}/api.php", params={**params, "format": "json"})
            finally:
                self._last = time.time()
                if parse:
                    self._last_parse = time.time()
            r.raise_for_status()
            d = r.json()
        self._cache[ck] = (time.time(), d)
        if len(self._cache) > 200:
            self._cache.pop(next(iter(self._cache)))
        return d

    async def search(self, wiki: str, q: str, n: int = 5) -> list[dict]:
        d = await self._api(wiki, {"action": "query", "list": "search", "srsearch": q, "srlimit": n})
        return [{"title": x.get("title", ""), "snippet": re.sub(r"<[^>]+>", "", _html.unescape(x.get("snippet", "")))}
                for x in ((d.get("query") or {}).get("search") or [])]

    async def page(self, wiki: str, title: str) -> dict:
        """一个条目 → {title, url, text}。先解析成网页读（带表格）；30 秒内刚解析过别的，就退回读原始 wikitext（信息框照样看得懂）。"""
        url = f"https://liquipedia.net/{wiki}/{quote(title.replace(' ', '_'))}"
        try:
            d = await self._api(wiki, {"action": "parse", "page": title, "prop": "text", "redirects": 1,
                                       "disabletoc": 1}, parse=True)
            if d.get("error"):
                raise RuntimeError(str(d["error"].get("info") or d["error"])[:80])
            page = ((d.get("parse") or {}).get("text") or {}).get("*") or ""
            from aigroup.net.fetch import html_to_markdown
            md = html_to_markdown(page)
            return {"title": (d.get("parse") or {}).get("title") or title, "url": url, "text": md["text"],
                    "full": md.get("full", "")}
        except TimeoutError:
            pass
        d = await self._api(wiki, {"action": "query", "titles": title, "prop": "revisions", "rvprop": "content",
                                   "rvslots": "main", "redirects": 1})
        pages = list(((d.get("query") or {}).get("pages") or {}).values())
        if not pages or "missing" in pages[0]:
            raise RuntimeError(f"Liquipedia 上没有「{title}」这个条目")
        wt = (((pages[0].get("revisions") or [{}])[0].get("slots") or {}).get("main") or {}).get("*") or ""
        return {"title": pages[0].get("title") or title, "url": url, "text": clean_wikitext(wt)}


def clean_wikitext(wt: str) -> str:
    """wikitext 粗略整理：去掉注释、引用、图片，[[a|b]] → b，信息框的 |key=value 一行一个。"""
    t = re.sub(r"(?s)<!--.*?-->", "", wt or "")
    t = re.sub(r"(?s)<ref[^>]*?/>|<ref[^>]*>.*?</ref>", "", t)
    t = re.sub(r"\[\[(?:File|Image):[^\]]*\]\]", "", t)
    t = re.sub(r"\[\[([^\]|]*\|)?([^\]]*)\]\]", r"\2", t)
    t = re.sub(r"'{2,}", "", t)
    t = re.sub(r"\n\s*\|", "\n|", t)
    return re.sub(r"\n{3,}", "\n\n", t).strip()


# ================= B 站 =================
def _strip(s: str) -> str:
    return re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^>]+>", "", str(s or "")))).strip()


def _num(n) -> str:
    try:
        n = int(n)
    except (TypeError, ValueError):
        return str(n or "")
    return f"{n / 10000:.1f}万" if n >= 10000 else str(n)


class BiliSite:
    """fetch(url, headers) -> bytes：用 LinkReader.fetch_bytes（只访问公网；配了 net.cookies 的 B 站 cookie 就用登录的）。"""

    def __init__(self, fetch, tz=None, video_cache=None):
        self.fetch, self.tz = fetch, tz
        self.video_cache = video_cache          # 可选：bv -> 你们之前看过记下的 {title, lines, frames, ...}
        self._ck = ("", 0.0)
        from aigroup.net.bili import Bili
        self.bili = Bili(fetch)

    async def _cookie(self) -> str:
        ck, ts = self._ck
        if ck and time.time() - ts < 6 * 3600:
            return ck
        try:
            d = json.loads((await self.fetch("https://api.bilibili.com/x/frontend/finger/spi",
                                             {"Referer": "https://www.bilibili.com/"})).decode("utf-8", "ignore"))
            b3, b4 = (d.get("data") or {}).get("b_3", ""), (d.get("data") or {}).get("b_4", "")
            ck = f"buvid3={b3}; buvid4={quote(b4)}; b_nut={int(time.time())}" if b3 else self.bili.buvid
        except Exception as e:  # noqa: BLE001
            log.info("B 站游客 cookie 没拿到：%s", e)
            ck = self.bili.buvid
        self._ck = (ck, time.time())
        self.bili.buvid = ck
        return ck

    async def _json(self, url: str) -> dict:
        await self._cookie()
        return await self.bili._json(url)

    def _day(self, ts) -> str:
        try:
            return datetime.fromtimestamp(int(ts), self.tz).strftime("%Y-%m-%d")
        except (TypeError, ValueError, OSError):
            return ""

    async def search(self, keyword: str, order: str = "totalrank", kind: str = "video", n: int = 8) -> str:
        await self._cookie()
        a, b = await self.bili._keys()
        from aigroup.net.bili import wbi_sign
        st = "bili_user" if kind in ("user", "up", "bili_user") else "video"
        params = {"search_type": st, "keyword": keyword[:40], "page": 1}
        if st == "video":
            params["order"] = order if order in ("totalrank", "click", "pubdate", "dm", "stow") else "totalrank"
        d = await self._json("https://api.bilibili.com/x/web-interface/wbi/search/type?" + urlencode(wbi_sign(params, a, b)))
        if d.get("code"):
            raise RuntimeError(f"B 站搜索没成：{d.get('message')}")
        res = (d.get("data") or {}).get("result") or []
        if not res:
            return f"（B 站上没搜到「{keyword}」）"
        rows = []
        for x in res[:n]:
            if st == "video":
                rows.append(f"- {_strip(x.get('title'))}｜UP：{x.get('author', '')}｜{_num(x.get('play'))}播放｜"
                            f"{x.get('duration', '')}｜{self._day(x.get('pubdate'))}｜{x.get('bvid', '')}"
                            f"\n  {_strip(x.get('description'))[:80]}")
            else:
                rows.append(f"- {_strip(x.get('uname'))}（{_num(x.get('fans'))}粉丝，{x.get('videos', 0)}个视频）"
                            f"｜{_strip(x.get('usign'))[:60]}｜mid={x.get('mid')}")
        what = {"click": "按播放量", "pubdate": "按最新", "dm": "按弹幕", "stow": "按收藏"}.get(order, "综合排序")
        return (f'<资料 来源="B站搜索" 日期="{datetime.now(self.tz):%Y-%m-%d}">\nB 站搜「{keyword}」'
                f"（{'UP 主' if st != 'video' else '视频，' + what}）：\n" + "\n".join(rows) + "\n</资料>")

    async def hot(self, n: int = 10) -> str:
        d = await self._json(f"https://api.bilibili.com/x/web-interface/popular?ps={n}&pn=1")
        rows = [f"- {_strip(x.get('title'))}｜UP：{(x.get('owner') or {}).get('name', '')}｜"
                f"{_num((x.get('stat') or {}).get('view'))}播放｜{x.get('bvid', '')}"
                for x in ((d.get("data") or {}).get("list") or [])[:n]]
        return (f'<资料 来源="B站热门" 日期="{datetime.now(self.tz):%Y-%m-%d}">\n' + "\n".join(rows) + "\n</资料>") if rows \
            else "（B 站热门没拿到）"

    async def video(self, bv: str, at: str = "") -> str:
        m = re.search(r"BV[0-9A-Za-z]{10}", bv or "")
        if not m:
            return "（要 BV 号，比如 BV1xx411c7mD；不知道就先 action=search）"
        bv = m.group(0)
        cached = self.video_cache(bv) if callable(self.video_cache) else None
        v = await self.bili.view(bv)
        aid, cid = int(v.get("aid") or 0), int(v.get("cid") or 0)
        st = v.get("stat") or {}
        lines = str((cached or {}).get("lines") or "")
        if not lines and aid and cid:
            lines = await self.bili.subtitles(bv, aid, cid)
        hot = []
        try:
            from aigroup.net.bili import hot_moments
            hot = hot_moments(await self.bili.danmaku(cid)) if cid else []
        except Exception:  # noqa: BLE001
            hot = []
        try:
            cm = await self.bili.comments(aid, bv, 5) if aid else []
        except Exception:  # noqa: BLE001
            cm = []
        out = [f"《{v.get('title', '')}》｜UP：{(v.get('owner') or {}).get('name', '')}｜{self._day(v.get('pubdate'))}｜"
               f"时长 {int(v.get('duration') or 0) // 60} 分 {int(v.get('duration') or 0) % 60} 秒｜{_num(st.get('view'))}播放 "
               f"{_num(st.get('like'))}赞 {_num(st.get('danmaku'))}弹幕",
               f"简介：{(v.get('desc') or '').strip()[:300] or '（没有）'}"]
        if lines:
            from aigroup.net.bili import ask_time, line_time
            t = ask_time(at, lines) if at else None
            ls = [ln for ln in lines.splitlines() if ln.strip()]
            if t is not None:
                near = [ln for ln in ls if (lt := line_time(ln)) is not None and abs(lt - t) <= 60]
                out.append(f"{t // 60:02d}:{t % 60:02d} 附近说的：\n" + ("\n".join(near[:12]) or "（那附近没有字幕）"))
            else:
                body = "\n".join(ls)
                out.append("字幕 / 原话（带时间）：\n" + (body[:2500] + ("\n……" if len(body) > 2500 else "")))
        else:
            out.append("（这个视频没有字幕，内容只能看简介、弹幕和评论）")
        if (cached or {}).get("frames"):
            out.append("画面：" + "；".join(f"{f.get('t')} {f.get('see')}" for f in cached["frames"][:6]))
        if hot:
            out.append("弹幕最热闹的几段：" + "；".join(f"{h['t']}（{'、'.join(h['top'])}）" for h in hot))
        if cm:
            out.append("热评：\n" + "\n".join(f"- {c}" for c in cm))
        return (f'<资料 来源="B站" 日期="{self._day(v.get("pubdate")) or "无"}">\n' + "\n".join(out) + "\n</资料>")
