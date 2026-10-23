"""上网查一下：她不知道的东西（这是什么、谁、哪本书讲了什么、最近的新闻比分……）先搜一搜再说。

按顺序试，哪个先给出“对得上”的结果就用哪个：
- 20.0 DeepSeek 搜索（/v1/tools/search，和聊天用同一个 DeepSeek Key；¥5 / 千次成功请求，空结果不收钱；
  带发布日期、权威度 authority 0~4、综合分 rerank_score 0~3；net.deepseek_search: false 关掉）
- Tavily（config.yaml 的 net.tavily_key 填了才用；每月 1000 次免费；13.0 起填了 Key 就先用它，net.tavily_first: false 改回兜底）
- 百度、360、搜狗（都不用 Key；像浏览器一样先打开首页拿 cookie 再搜，国内服务器直连就能用）
只拿标题和摘要，最多几条；不点开结果页（点开交给 links.py，只读群友发的链接）。
结果里一个查询词都没出现（被风控时会给一堆不相干的）就当没搜到，换下一个。
"""
from __future__ import annotations

import html
import logging
import re
import time

import httpx

log = logging.getLogger("aigroup.search")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0 Safari/537.36")
TAG = re.compile(r"<[^>]+>")


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(TAG.sub("", s or ""))).strip()


ENGINES = (  # 名字, 首页, 搜索地址, 参数名
    ("百度", "https://www.baidu.com/", "https://www.baidu.com/s", "wd"),
    ("360", "https://www.so.com/", "https://www.so.com/s", "q"),
    ("搜狗", "https://www.sogou.com/", "https://www.sogou.com/web", "query"),
)
DROP = re.compile(r"<(script|style|noscript)[^>]*>.*?</\1>", re.S | re.I)


def parse_generic(page: str, n: int = 5) -> list[dict]:
    """通用的结果页解析：每个 <h3> 是一条结果的标题，它后面到下一个 <h3> 之间的文字当摘要（百度、360、搜狗都是这样排的）。"""
    page = DROP.sub(" ", page or "")
    parts = re.split(r"<h3[\s>]", page)[1:]
    out = []
    for part in parts:
        m = re.match(r"[^>]*>(.*?)</h3>(.*)", part, re.S) or re.match(r"(.*?)</h3>(.*)", part, re.S)
        if not m:
            continue
        title = _clean(m.group(1))
        href = re.search(r'href="([^"]+)"', part)
        snippet = _clean(m.group(2))[:400]
        snippet = re.sub(r"^(\d{4}年\d{1,2}月\d{1,2}日|\d+\s*(天|小时|分钟)前)\s*[·\-]?\s*", "", snippet)
        snippet = re.split(r'["\']?[}\]]|\{"|变清晰', snippet)[0].strip()    # 页面里混进来的 JSON、图片栏
        if (len(title) < 4 or re.match(r"(相关搜索|大家还在搜|其他人还|为您推荐|猜你想搜)", title)
                or re.search(r"百度图片|百度视频|- 视频$|_图片$", title) or len(snippet) < 8):
            continue
        out.append({"title": title[:80], "url": html.unescape(href.group(1)) if href else "", "snippet": snippet[:220]})
        if len(out) >= n:
            break
    return out


def relevant(query: str, results: list[dict]) -> bool:
    """结果里至少出现一个查询词（2 个字以上的片段）才算搜到了。"""
    words = [w for w in re.split(r"[\s,，、]+", query) if len(w) >= 2]
    if not words:
        words = [query[i:i + 2] for i in range(0, max(1, len(query) - 1), 2)]
    text = " ".join(r["title"] + r["snippet"] for r in results)
    return any(w in text for w in words)


def pub_date(v) -> str:
    """Tavily 的 published_date（RFC 2822 或 ISO）→ YYYY-MM-DD；认不出返回空。"""
    s = str(v or "").strip()
    if not s:
        return ""
    from datetime import datetime
    from email.utils import parsedate_to_datetime
    for f in (lambda: parsedate_to_datetime(s), lambda: datetime.fromisoformat(s.replace("Z", "+00:00"))):
        try:
            return f().strftime("%Y-%m-%d")
        except Exception:  # noqa: BLE001
            continue
    m = re.search(r"(20\d\d)-(\d\d)-(\d\d)", s)
    return m.group(0) if m else ""


def text_dates(text: str, today) -> list[str]:
    """从标题 / 摘要里认出写着的日期（2026年10月19日、10月19日、2026-10-19、10/19），
    没写年份的按今天往前推（12 月的新闻在 1 月看到算去年）。返回 YYYY-MM-DD 列表。"""
    out = []
    for m in re.finditer(r"(?:(20\d\d)\s*[年\-/.]\s*)?(\d{1,2})\s*[月\-/.]\s*(\d{1,2})\s*[日号]?", text or ""):
        y, mo, d = m.group(1), int(m.group(2)), int(m.group(3))
        if not (1 <= mo <= 12 and 1 <= d <= 31):
            continue
        if not y and not re.search(r"[月]", m.group(0)):       # 光是“3.5”“10/19”这种不带“月”的，容易是比分、版本号
            continue
        yy = int(y) if y else today.year - (1 if (mo, d) > (today.month, today.day) else 0)
        out.append(f"{yy:04d}-{mo:02d}-{d:02d}")
    return out


class Bucket:
    """20.1 每个搜索引擎一个令牌桶：一轮并行好几个查询时排队取令牌，拿不到就等一下；超限（429）时整个引擎歇一会儿。"""

    def __init__(self, rate: float, burst: int | None = None):
        self.rate = max(0.1, float(rate))
        self.cap = float(burst or max(1, int(rate)))
        self.tokens, self.ts, self.pause_until = self.cap, time.monotonic(), 0.0

    async def take(self, max_wait: float = 1.5) -> bool:
        import asyncio
        end = time.monotonic() + max_wait
        while True:
            now = time.monotonic()
            if now < self.pause_until:
                if self.pause_until > end:
                    return False
                await asyncio.sleep(self.pause_until - now)
                continue
            self.tokens = min(self.cap, self.tokens + (now - self.ts) * self.rate)
            self.ts = now
            if self.tokens >= 1:
                self.tokens -= 1
                return True
            wait = (1 - self.tokens) / self.rate
            if now + wait > end:
                return False
            await asyncio.sleep(wait)

    def pause(self, seconds: float):
        self.pause_until = max(self.pause_until, time.monotonic() + seconds)


def deepseek_key(cfg) -> str:
    """20.0 net.deepseek_key 没填时，用聊天接口里官方 DeepSeek（api.deepseek.com）的 Key。"""
    c = dict(cfg.get("net") or {})
    k = str(c.get("deepseek_key") or "").strip()
    if k and "${" not in k:
        return k
    for p in dict((cfg.get("llm") or {}).get("profiles") or {}).values():
        if isinstance(p, dict) and "api.deepseek.com" in str(p.get("base_url") or "").lower():
            k = str(p.get("api_key") or "").strip()
            if k and k != "none" and "${" not in k:
                return k
    return ""


class WebSearch:
    def __init__(self, cfg):
        c = dict(cfg.get("net") or {})
        self.enabled = bool(c.get("search", True))
        self.tavily_key = str(c.get("tavily_key") or "").strip()
        self.tavily_first = bool(c.get("tavily_first", True))      # 13.0 配了 Key 就先用 Tavily（结果干净，服务器上不会被风控）
        self.proxy = cfg.get("proxy") or None
        self.timeout = float(c.get("search_timeout", 12))
        self._cache: dict[str, list[dict]] = {}
        self._clients: dict[str, httpx.AsyncClient] = {}
        self.kv = None                    # 19.0 GroupChat 挂上数据库：记 Tavily 这个月用了几次、免费额度用完没
        self.ds_key = deepseek_key(cfg) if c.get("deepseek_search", True) else ""
        # 20.4.3 默认先用 Tavily 的每月免费额度，用完（或出错）再用 DeepSeek；search_first: deepseek 改回 DeepSeek 在前
        self.ds_first = str(c.get("search_first") or "tavily").lower() == "deepseek"
        self.ds_ip = str(c.get("deepseek_ip") or "").strip()      # 20.1 固定按哪个 IP 出结果（不同 IP 前 10 只重合 0.58）
        self.buckets = {"DeepSeek": Bucket(float(c.get("deepseek_qps", 5))), "Tavily": Bucket(float(c.get("tavily_qps", 2)))}
        self.last_engine = ""

    async def _client(self, name: str, home: str) -> httpx.AsyncClient:
        c = self._clients.get(name)
        if c is None or c.is_closed:
            c = httpx.AsyncClient(timeout=self.timeout, follow_redirects=True, proxy=None,
                                  headers={"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9",
                                           "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"})
            try:
                await c.get(home)                         # 先打开首页拿 cookie，像个正常浏览器
            except Exception:  # noqa: BLE001
                pass
            self._clients[name] = c
        return c

    async def _engine(self, name: str, home: str, url: str, key: str, q: str, n: int) -> list[dict]:
        c = await self._client(name, home)
        r = await c.get(url, params={key: q})
        r.raise_for_status()
        if re.search(r"安全验证|验证码|captcha", r.text[:20000], re.I) and "<h3" not in r.text:
            self._clients.pop(name, None)
            raise RuntimeError("要验证")
        return parse_generic(r.text, n)

    async def search(self, query: str, n: int = 5, fresh_days: int = 0) -> list[dict]:
        """fresh_days > 0：只要最近这几天的新闻（13.2 分享用）。Tavily 走新闻模式、带发布日期；
        别的搜索引擎给不出日期，结果照旧返回，由调用方看日期再决定。"""
        q = re.sub(r"\s+", " ", query or "").strip()[:60]
        if not self.enabled or len(q) < 2:
            return []
        ck = f"{q}|{fresh_days}" if fresh_days else q
        if ck in self._cache:
            return self._cache[ck]
        res: list[dict] = []
        tries = [(e[0], (lambda e=e: self._engine(*e, q, n))) for e in ENGINES]
        if self.tavily_key and not self.tavily_out():
            tav = ("Tavily", lambda: self._tavily(q, n, fresh_days))
            tries = [tav] + tries if (self.tavily_first or fresh_days) else tries + [tav]
        if self.ds_key and not self._ds_down():
            ds = ("DeepSeek", lambda: self._deepseek(q, n, fresh_days))
            tries = [ds] + tries if self.ds_first else tries[:1] + [ds] + tries[1:]
        for name, fn in tries:
            try:
                got = await fn()
            except Exception as e:  # noqa: BLE001
                log.info("%s搜索失败（%s）：%s", name, q, e)
                continue
            if got and relevant(q, got):
                res = got
                self.last_engine = name
                log.info("搜了「%s」（%s）：%d 条", q, name, len(res))
                break
            log.info("%s搜「%s」没搜到对得上的", name, q)
        self._cache[ck] = res
        if len(self._cache) > 200:
            self._cache.pop(next(iter(self._cache)))
        return res

    # ---------------- 19.0 Tavily 免费额度：用完了这个月就只用免费的引擎 ----------------
    def _month(self) -> str:
        return time.strftime("%Y-%m")

    def tavily_out(self) -> bool:
        return bool(self.kv is not None and self.kv.get("tavily_out") == self._month())

    def tavily_used(self) -> int:
        return int((self.kv.get(f"tavily_n_{self._month()}") if self.kv is not None else 0) or 0)

    async def _tavily(self, q: str, n: int, fresh_days: int = 0, snippet: int = 220, site: str = "") -> list[dict]:
        body = {"query": q, "max_results": n, "search_depth": "basic"}
        if site:
            body["include_domains"] = [site]
        if fresh_days:
            body.update(topic="news", days=int(fresh_days))
        async with httpx.AsyncClient(timeout=self.timeout) as c:
            r = await c.post("https://api.tavily.com/search", headers={"Authorization": f"Bearer {self.tavily_key}"},
                             json=body)
            if r.status_code in (432, 433) or (r.status_code in (401, 403) and re.search(
                    r"limit|quota|credit|exceed|plan", r.text or "", re.I)):
                if self.kv is not None:
                    self.kv.set("tavily_out", self._month())
                log.warning("Tavily 这个月的免费额度用完了（%s），这个月改用免费的搜索", r.status_code)
                raise RuntimeError("Tavily 额度用完")
            r.raise_for_status()
            d = r.json()
        if self.kv is not None:
            k = f"tavily_n_{self._month()}"
            self.kv.set(k, int(self.kv.get(k) or 0) + 1)
        return [{"title": _clean(x.get("title", ""))[:80], "url": x.get("url", ""),
                 "snippet": _clean(x.get("content", ""))[:snippet], "date": pub_date(x.get("published_date"))}
                for x in (d.get("results") or [])[:n]]

    # ---------------- 20.0 DeepSeek 搜索 / 读网页 ----------------
    def _ds_down(self) -> bool:
        return bool(self.kv is not None and float(self.kv.get("ds_search_down") or 0) > time.time())

    def _ds_count(self, kind: str):
        if self.kv is not None:
            k = f"ds_{kind}_n_{self._month()}"
            self.kv.set(k, int(self.kv.get(k) or 0) + 1)

    def ds_used(self) -> dict:
        m = self._month()
        g = (lambda k: int((self.kv.get(k) if self.kv is not None else 0) or 0))
        return {"search": g(f"ds_search_n_{m}"), "fetch": g(f"ds_fetch_n_{m}")}

    async def _ds_post(self, path: str, body: dict, timeout: float) -> dict:
        bk = self.buckets.get("DeepSeek")
        if bk is not None and not await bk.take():
            raise RuntimeError("DeepSeek 这会儿太忙（限速）")
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.post(f"https://api.deepseek.com/v1/tools/{path}", json=body,
                             headers={"Authorization": f"Bearer {self.ds_key}"})
        if r.status_code in (401, 402):           # Key 不对 / 余额不够：一小时内别再试
            if self.kv is not None:
                self.kv.set("ds_search_down", time.time() + 3600)
            log.warning("DeepSeek %s 用不了（%s）：%s", path, r.status_code, r.text[:120])
            raise RuntimeError(f"DeepSeek {r.status_code}")
        if r.status_code == 429:
            if bk is not None:
                bk.pause(10)
            raise RuntimeError("DeepSeek 限速了（429）")
        if r.status_code == 404 and path == "fetch":
            return {}
        r.raise_for_status()
        return r.json()

    async def _deepseek(self, q: str, n: int, days: int = 0, snippet: int = 220, site: str = "",
                        question: str = "") -> list[dict]:
        """DeepSeek 搜索：没有单独的时间、网站参数，用查询里的 after: / site: 写。summary 是给模型看的长摘要
        （实测中位数三千多字，截短了再用）。20.1：带日期过滤会把没日期的结果全丢掉（实测约 11%），
        结果少于 3 条时不带日期再搜一次补上，没日期的从摘要里抠日期。ori_question 带上原问题帮它重排。"""
        from datetime import datetime, timedelta
        tail = (f" site:{site}" if site else "") + (
            f" after:{(datetime.now() - timedelta(days=int(days))):%Y-%m-%d}" if days else "")
        qq = q[:max(8, 128 - len(tail))] + tail              # 整个 query 最多 128 字：先截问题，别把 site: / after: 截掉
        body = {"query": qq, "count": max(1, min(50, n + 2))}
        if question and question.strip() and question.strip() != q.strip():
            body["ori_question"] = question.strip()[:128]
        if self.ds_ip:
            body["localization"] = self.ds_ip
        d = await self._ds_post("search", body, self.timeout)
        res = list(d.get("results") or [])
        if res:
            self._ds_count("search")             # 只有有结果才收钱
        if days and len(res) < 3:
            b2 = dict(body, query=(q[:max(8, 128 - len(f" site:{site}" if site else ""))] + (f" site:{site}" if site else "")))
            try:
                d2 = await self._ds_post("search", b2, self.timeout)
            except Exception:  # noqa: BLE001
                d2 = {}
            more = [x for x in (d2.get("results") or []) if not x.get("date")]
            if more:
                self._ds_count("search")
                urls = {x.get("url") for x in res}
                res += [x for x in more if x.get("url") not in urls]
        res = sorted(res, key=lambda x: -float(x.get("rerank_score") or 0))[:n]
        today = datetime.now().date()
        out = []
        for x in res:
            summ = _clean(x.get("summary") or x.get("snippet") or "")
            date = pub_date(x.get("date"))
            if not date:
                ds = text_dates(summ[:400], today)
                date = ds[0] if ds else ""
            out.append({"title": _clean(x.get("title", ""))[:80], "url": x.get("url", ""), "snippet": summ[:snippet],
                        "date": date, "site": str(x.get("site_name") or ""),
                        "authority": int(x.get("authority") if x.get("authority") is not None else -1),
                        "score": round(float(x.get("rerank_score") or 0), 2)})
        return out

    async def ds_fetch(self, url: str) -> dict:
        """DeepSeek 读网页：{title, text}（Markdown）；读不到返回 {}。比 Tavily Extract 便宜，不占 Tavily 额度。"""
        if not self.ds_key or self._ds_down():
            return {}
        d = await self._ds_post("fetch", {"url": url[:8192]}, 30)
        if d.get("content"):
            self._ds_count("fetch")
        return {"title": str(d.get("title") or ""), "text": str(d.get("content") or "")}

    async def extract(self, url: str) -> dict:
        """19.3 让 Tavily 的服务器去读一个网页（国内打不开的、被拦的）：{title, text}（Markdown）。5 个网址算 1 个额度，这里一次一个。"""
        if not self.tavily_key or self.tavily_out():
            return {}
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post("https://api.tavily.com/extract", headers={"Authorization": f"Bearer {self.tavily_key}"},
                             json={"urls": [url], "format": "markdown", "extract_depth": "basic"})
            if r.status_code in (432, 433):
                if self.kv is not None:
                    self.kv.set("tavily_out", self._month())
                raise RuntimeError("Tavily 额度用完")
            r.raise_for_status()
            d = r.json()
        if self.kv is not None:
            k = f"tavily_n_{self._month()}"
            self.kv.set(k, int(self.kv.get(k) or 0) + 1)
        res = (d.get("results") or [{}])[0] if d.get("results") else {}
        return {"title": str(res.get("title") or ""), "text": str(res.get("raw_content") or "")}

    async def agent_search(self, query: str, n: int = 6, days: int = 0, site: str = "",
                           question: str = "", prefer: str = "") -> tuple[list[dict], str]:
        """给“查资料”用：DeepSeek / Tavily 优先（带日期、摘要长一点），都不行就换百度、360（没有日期）。
        返回（结果, 用的哪个引擎）。"""
        q = re.sub(r"\s+", " ", query or "").strip()[:80]
        site = re.sub(r"^https?://|/.*$", "", (site or "").strip().lower())[:60]
        if not self.enabled or len(q) < 2:
            return [], ""
        ck = f"agent|{q}|{days}|{site}"            # 缓存不分引擎
        hit = self._cache.get(ck)
        if hit:
            return hit, "缓存"
        tries = []
        if self.tavily_key and not self.tavily_out():
            tries.append(("Tavily", lambda: self._tavily(q, n, int(days or 0), snippet=320, site=site)))
        if self.ds_key and not self._ds_down():
            ds = ("DeepSeek", lambda: self._deepseek(q, n, int(days or 0), snippet=320, site=site, question=question))
            # 实测（deepseek-search-bench 2026-10）：公众号、小红书完全搜不到，加 site: 也不行 → 这类放到最后
            weak = bool(re.search(r"小红书|公众号|xiaohongshu|weixin\.qq", q + site, re.I))
            first = (prefer == "deepseek") or (self.ds_first and prefer != "tavily")
            tries = tries + [ds] if (weak or not first) else [ds] + tries
        qq = f"site:{site} {q}" if site else q                   # 19.3 只搜某个网站（百度 / 360 认 site:）
        tries += [(e[0], (lambda e=e: self._engine(*e, qq, n))) for e in ENGINES]
        for name, fn in tries:
            try:
                got = await fn()
            except Exception as e:  # noqa: BLE001
                log.info("%s搜索失败（%s）：%s", name, q, e)
                continue
            if got:
                self._cache[ck] = got
                if len(self._cache) > 200:
                    self._cache.pop(next(iter(self._cache)))
                log.info("查资料：搜了「%s」（%s，%d 条）", q, name, len(got))
                return got, name
        return [], ""

    @staticmethod
    def block(query: str, results: list[dict], k: int = 4) -> str:
        if not results:
            return ""
        lines = [f"- {r['title']}：{r['snippet']}" for r in results[:k] if r.get("snippet") or r.get("title")]
        return (f"# 你刚在手机上搜了一下「{query}」，看到这些（自然地用，像你自己知道 / 刚看到的；别说“根据搜索结果”，"
                f"别贴链接，别像百科一样整段念；搜到的对不上就别硬说）\n" + "\n".join(lines))
