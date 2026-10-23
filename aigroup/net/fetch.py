"""19.3 什么网页都能看：一层不行换下一层，读出来的是带表格的正文（Markdown）、能只挑和问题有关的段落、能列出页面上的链接接着点。

借鉴的做法（都只取思路，没有整包装进来）：
- Scrapling 的 Fetcher / StealthyFetcher：先发普通请求，被拦（403、人机验证）就换成模仿真浏览器 TLS 指纹的请求（curl_cffi），
  还不行再开无头浏览器真渲染一遍
- Crawl4AI 的 fit_markdown + BM25 过滤：正文转成 Markdown（标题、列表、表格都在），再按问题只留相关的段落，省 token
- Playwright MCP：浏览器只当兜底、按需启动，图片 / 字体 / 视频不加载，每个请求都检查是不是公网地址
- Firecrawl / Tavily Extract：国内连不上的网站（维基、GitHub、被 DNS 污染的）交给 Tavily 的服务器去读（一次算一个额度）

四层（都可选，没装就跳过）：
    1 直连（httpx，LinkReader._get：只访问公网、每一跳都检查、带 net.cookies）
    2 仿浏览器指纹（pip install curl_cffi）：1 被 403 / 412 / 429 / 503 / 人机验证拦下时
    3 无头浏览器（pip install playwright && python -m playwright install chromium-headless-shell）：
      页面是脚本加载的（正文太短、“正在加载”“请开启 JavaScript”“安全检测”）时；一次只开一个页面，闲 2 分钟自动关
    4 Tavily Extract（配了 net.tavily_key）：连不上（超时、DNS 被污染）或前面都被拦时
配置 net.browser：auto（装了就用，默认）/ false（不用）；net.tavily_extract：true（默认）/ false。
"""
from __future__ import annotations

import asyncio
import html as _html
import ipaddress
import logging
import re
import time
from urllib.parse import urljoin, urlparse

log = logging.getLogger(__name__.split(".")[0] + ".fetch")

BLOCK_STATUS = (401, 403, 405, 412, 418, 429, 451, 503)
CHALLENGE_RE = re.compile(r"just a moment|attention required|are you human|verify you are human|enable javascript|"
                          r"请开启\s*javascript|请启用\s*javascript|安全验证|人机验证|安全检测|正在检测当前网络环境|"
                          r"sina visitor system|请求存在异常|访问异常|captcha|cf-chl|滑动验证", re.I)
SHELL_RE = re.compile(r"正在加载|加载中|loading\.\.\.|您使用的ie浏览器版本过低|you need to enable javascript", re.I)
LOGIN_RE = re.compile(r"登录后(查看|推荐|可以)|扫码登录|请先登录|sign in to (continue|view)|log in to (continue|see)", re.I)


def looks_blocked(title: str, text: str) -> bool:
    """是人机验证 / 拦截页：开头有那些字，而且正文不长（长文章里提到“验证码”不算）。"""
    if len((text or "").strip()) >= 1500:
        return False
    return bool(CHALLENGE_RE.search(f"{title}\n{(text or '')[:600]}"))


def looks_shell(title: str, text: str) -> bool:
    """像是脚本加载的空壳：正文很短，或者开头就是“正在加载”。"""
    t = (text or "").strip()
    return len(t) < 300 or (len(t) < 1500 and bool(SHELL_RE.search(t[:300])))


def html_to_markdown(page: str) -> dict:
    """网页 → {title, date, text}：trafilatura 转 Markdown（标题、列表、表格都留着）；正文太短（数据站、列表页）就把看得见的字都留下。"""
    from aigroup.net.links import page_text
    base = page_text(page)
    try:
        import trafilatura  # type: ignore
        md = trafilatura.extract(page, output_format="markdown", include_tables=True, include_links=False,
                                 include_comments=False, favor_recall=True) or ""
    except Exception:  # noqa: BLE001
        md = ""
    md = re.sub(r"\n{3,}", "\n\n", md).strip()
    if len(md) >= 1500 or len(md) >= len(base["text"]) * 0.8:
        base["text"] = md
    from aigroup.net.links import visible_text
    full = visible_text(page)
    base["full"] = full if len(full) > len(base["text"]) * 1.2 else ""     # 正文抽取丢掉的（阵容表、侧栏数据），find 时再翻一遍
    return base


A_RE = re.compile(r"(?is)<a\b[^>]*?href\s*=\s*([\"'])(.*?)\1[^>]*>(.*?)</a>")


def page_links(page: str, base_url: str, limit: int = 40) -> list[dict]:
    """页面上的链接：[{text, url}]，同一个网站的排前面；去掉登录、下载 App、锚点这些。"""
    host = (urlparse(base_url).hostname or "").lower()
    seen, same, other = set(), [], []
    for _, href, inner in A_RE.findall(page or ""):
        href = _html.unescape(href.strip())
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        url = urljoin(base_url, href).split("#", 1)[0]
        if not url.startswith(("http://", "https://")) or url in seen or url.rstrip("/") == base_url.rstrip("/"):
            continue
        text = re.sub(r"\s+", " ", _html.unescape(re.sub(r"(?s)<[^>]+>", " ", inner))).strip()
        if len(text) < 2 or len(text) > 80 or re.fullmatch(r"(登录|注册|下载.*app|首页|更多|返回顶部|上一页|下一页|log ?in|sign ?up|home)",
                                                          text, re.I):
            continue
        seen.add(url)
        h = (urlparse(url).hostname or "").lower()
        (same if h == host or h.endswith("." + host) or host.endswith("." + h) else other).append({"text": text, "url": url})
    return (same + other)[:limit]


def _terms(q: str) -> list[str]:
    """问题 → 关键词：英文 / 数字按词，中文按两个字一组（再加上整段）。"""
    q = (q or "").lower()
    out = [w for w in re.findall(r"[a-z0-9][a-z0-9.+#-]*", q) if len(w) >= 2]
    for seg in re.findall(r"[一-鿿]+", q):
        if len(seg) <= 4:
            out.append(seg)
        out += [seg[i:i + 2] for i in range(len(seg) - 1)]
    stop = {"什么", "怎么", "多少", "最近", "现在", "一下", "这个", "那个", "哪些", "有没", "没有", "是不", "不是"}
    return list(dict.fromkeys(w for w in out if w not in stop))


def find_passages(text: str, query: str, n: int = 4000, window: int = 500) -> str:
    """只留和问题有关的段落（像 Crawl4AI 的 BM25 过滤）：按段切块、按关键词命中打分，挑分高的、按原来的顺序拼起来。
    表格行连在一起算一块，免得把一行数字切开。一个都没命中就返回空字符串。"""
    terms = _terms(query)
    if not terms or not text:
        return ""
    # 先切成段（空行、表格和正文交界处都切），再把相邻的短段合到 window 字左右；表格和正文不混，表格前面的短标题跟着表格走
    paras, cur, cur_tab = [], [], False
    for ln in text.split("\n"):
        tab = ln.lstrip().startswith("|")
        if cur and (not ln.strip() or tab != cur_tab or (not tab and sum(map(len, cur)) > window)):
            paras.append(("\n".join(cur), cur_tab))
            cur = []
        if ln.strip():
            cur.append(ln)
            cur_tab = tab
    if cur:
        paras.append(("\n".join(cur), cur_tab))
    blocks, buf, buf_tab = [], "", False
    for para, tab in paras:
        if buf and (tab != buf_tab or len(buf) + len(para) > window):
            if tab and not buf_tab and len(buf) < 80:
                buf, buf_tab = buf + para + "\n", True
                continue
            blocks.append(buf)
            buf = ""
        buf += para + "\n"
        buf_tab = tab
    if buf.strip():
        blocks.append(buf)
    scored = []
    for i, b in enumerate(blocks):
        low = b.lower()
        s = sum(min(3, low.count(t)) * (2 if len(t) > 2 else 1) for t in terms)
        if s:
            scored.append((s, i))
    if not scored:
        return ""
    scored.sort(key=lambda x: (-x[0], x[1]))
    keep, total = set(), 0
    for _, i in scored:
        if total >= n:
            break
        keep.add(i)
        total += len(blocks[i])
    out, last = [], -2
    for i in sorted(keep):
        if i != last + 1 and out:
            out.append("……")
        out.append(blocks[i].strip())
        last = i
    return "\n".join(out)[:n]


class Browser:
    """按需开的无头浏览器：一个进程里共用一个，一次只开一个页面，闲 120 秒自动关（服务器内存小）。"""

    def __init__(self, timeout: float = 20.0):
        self.timeout = timeout
        self._pw = None
        self._b = None
        self._sem = asyncio.Semaphore(1)
        self._last = 0.0
        self._closer: asyncio.Task | None = None
        self._hosts: dict[str, tuple[float, bool]] = {}

    @staticmethod
    def available() -> bool:
        try:
            import playwright  # type: ignore  # noqa: F401
            return True
        except Exception:  # noqa: BLE001
            return False

    async def _ensure(self):
        if self._b is not None:
            return self._b
        from playwright.async_api import async_playwright  # type: ignore
        self._pw = await async_playwright().start()
        try:
            self._b = await self._pw.chromium.launch(headless=True, args=["--disable-dev-shm-usage", "--no-sandbox"])
        except Exception:
            await self._pw.stop()
            self._pw = None
            raise
        if self._closer is None or self._closer.done():
            self._closer = asyncio.create_task(self._idle_close())
        return self._b

    async def _idle_close(self):
        while self._b is not None:
            await asyncio.sleep(30)
            if time.time() - self._last > 120 and not self._sem.locked():
                await self.close()

    async def close(self):
        b, pw = self._b, self._pw
        self._b = self._pw = None
        try:
            if b is not None:
                await b.close()
            if pw is not None:
                await pw.stop()
        except Exception:  # noqa: BLE001
            pass

    async def _public(self, url: str) -> bool:
        from aigroup.net.links import is_public
        host = (urlparse(url).hostname or "").lower()
        hit = self._hosts.get(host)
        if hit and time.time() - hit[0] < 600:
            return hit[1]
        ok = await is_public(url)
        self._hosts[host] = (time.time(), ok)
        return ok

    async def render(self, url: str, cookies: str = "") -> tuple[str, str]:
        """打开网页、等脚本跑完、往下滚两屏，返回（最终网址，HTML）。只放行公网地址；图片、字体、视频不加载。"""
        async with self._sem:
            self._last = time.time()
            b = await self._ensure()
            from aigroup.net.links import UA
            ctx = await b.new_context(locale="zh-CN", user_agent=UA, viewport={"width": 1280, "height": 1600},
                                      java_script_enabled=True)
            try:
                if cookies:
                    host = urlparse(url).hostname or ""
                    jar = []
                    for kv in cookies.split(";"):
                        k, _, v = kv.strip().partition("=")
                        if k and v:
                            jar.append({"name": k, "value": v, "domain": host, "path": "/"})
                    if jar:
                        await ctx.add_cookies(jar)

                async def guard(route):
                    req = route.request
                    if req.resource_type in ("image", "media", "font") or not await self._public(req.url):
                        await route.abort()
                    else:
                        await route.continue_()
                await ctx.route("**/*", guard)
                page = await ctx.new_page()
                await page.goto(url, wait_until="domcontentloaded", timeout=self.timeout * 1000)
                try:
                    await page.wait_for_load_state("networkidle", timeout=6000)
                except Exception:  # noqa: BLE001
                    pass
                for _ in range(2):
                    try:
                        await page.mouse.wheel(0, 2400)
                        await page.wait_for_timeout(700)
                    except Exception:  # noqa: BLE001
                        break
                return page.url, await page.content()
            finally:
                self._last = time.time()
                try:
                    await ctx.close()
                except Exception:  # noqa: BLE001
                    pass


class WebReader:
    """read(url) → {ok, title, site, date, text, links, via, error}。text 是整页正文（Markdown），调用方自己翻页 / 过滤。"""

    def __init__(self, cfg, links, web=None):
        net = dict(cfg.get("net") or {})
        self.links = links                     # LinkReader：第 1 层（只访问公网、带 cookie）
        self.web = web                         # WebSearch：第 4 层（Tavily Extract）
        mode = str(net.get("browser", "auto")).lower()
        self.browser = Browser(float(net.get("render_timeout", 20))) if mode not in ("false", "0", "off", "no") \
            and Browser.available() else None
        self.tavily_extract = bool(net.get("tavily_extract", True))
        self._browser_off = 0.0
        if net.get("browser_path"):                               # 浏览器装在别的用户目录下（服务用 root 跑时）
            import os
            os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(net["browser_path"]))
        self._cache: dict[str, tuple[float, dict]] = {}

    def layers(self) -> list[str]:
        """现在能用的几层（小屋里显示）。"""
        out = ["直连"]
        try:
            import curl_cffi  # type: ignore  # noqa: F401
            out.append("仿浏览器")
        except Exception:  # noqa: BLE001
            pass
        if self.browser is not None and time.time() >= self._browser_off:
            out.append("无头浏览器")
        w = self.web
        rd = []
        if self.tavily_extract and w is not None and getattr(w, "tavily_key", "") and not w.tavily_out():
            rd.append("Tavily 代读")
        if w is not None and getattr(w, "ds_key", "") and not w._ds_down():
            rd.append("DeepSeek 代读")
        out += list(reversed(rd)) if getattr(w, "ds_first", False) else rd
        return out

    def _cookie(self, url: str) -> str:
        host = (urlparse(url).hostname or "").lower()
        return next((v for d, v in (getattr(self.links, "cookies", {}) or {}).items()
                     if host == d or host.endswith("." + d)), "")

    @staticmethod
    def _decode(buf: bytes, ctype: str) -> str:
        charset = "utf-8"
        m = re.search(r"charset=([\w-]+)", ctype or "") or re.search(rb"<meta[^>]+charset=[\"']?([\w-]+)", buf[:4096], re.I)
        if m:
            charset = m.group(1).decode() if isinstance(m.group(1), bytes) else m.group(1)
        try:
            return buf.decode(charset, errors="ignore")
        except LookupError:
            return buf.decode("utf-8", errors="ignore")

    def _pack(self, page: str, url: str, via: str) -> dict:
        from aigroup.net.links import site_name
        d = html_to_markdown(page)
        return {"ok": True, **d, "site": site_name(urlparse(url).hostname or ""), "url": url, "via": via,
                "links": page_links(page, url)}

    # ---------------- 每一层 ----------------
    async def _direct(self, url: str) -> tuple[dict | None, str]:
        """返回（结果或 None，为什么没成：block / shell / down / 别的）。"""
        try:
            r, buf, final = await self.links._get(url)
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            m = re.search(r"\b(\d{3})\b", msg)
            if m and (int(m.group(1)) in BLOCK_STATUS or int(m.group(1)) == 400):
                return None, "block"
            if m and 400 <= int(m.group(1)) < 500:
                return {"ok": False, "error": f"网页打不开（{m.group(1)}）"}, "type"     # 404 这些：换办法也没用
            return None, "down"                                  # 超时、连不上、DNS 被污染、别的网络错：交给后面的办法
        ctype = (r.headers.get("content-type") or "").lower()
        if ctype and not any(x in ctype for x in ("html", "xml", "text", "json")):
            return {"ok": False, "error": f"不是网页（{ctype.split(';')[0]}）"}, "type"
        page = self._decode(buf, ctype)
        d = self._pack(page, final, "直连")
        if looks_blocked(d.get("title", ""), d.get("text", "")):
            return d, "block"
        if looks_shell(d.get("title", ""), d.get("text", "")):
            return d, "shell"
        return d, ""

    async def _impersonate(self, url: str) -> tuple[dict | None, str]:
        try:
            from curl_cffi.requests import AsyncSession  # type: ignore
        except Exception:  # noqa: BLE001
            return None, "no"
        from aigroup.net.links import is_public
        cur = url
        try:
            async with AsyncSession(impersonate="chrome", timeout=12) as s:
                for _ in range(5):
                    if not await is_public(cur):
                        return None, "down"
                    ck = self._cookie(cur)
                    r = await s.get(cur, allow_redirects=False, headers={"Accept-Language": "zh-CN,zh;q=0.9",
                                                                           **({"Cookie": ck} if ck else {})})
                    if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
                        cur = urljoin(cur, r.headers["location"])
                        continue
                    if r.status_code in BLOCK_STATUS:
                        return None, "block"
                    if r.status_code >= 400:
                        return None, f"HTTP {r.status_code}"
                    page = r.text[:3_000_000]
                    d = self._pack(page, cur, "仿浏览器")
                    if looks_blocked(d.get("title", ""), d.get("text", "")):
                        return d, "block"
                    return d, "shell" if looks_shell(d.get("title", ""), d.get("text", "")) else ""
        except Exception as e:  # noqa: BLE001
            return None, "down" if re.search(r"timed? ?out|resolve|connect", str(e), re.I) else str(e)[:80]
        return None, "跳转太多"

    async def _render(self, url: str) -> tuple[dict | None, str]:
        if self.browser is None or time.time() < self._browser_off:
            return None, "no"
        try:
            final, page = await asyncio.wait_for(self.browser.render(url, self._cookie(url)), self.browser.timeout + 15)
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            if re.search(r"Executable doesn't exist|playwright install|missing dependencies|Host system is missing", msg, re.I):
                self._browser_off = time.time() + 3600        # 浏览器没装好：一小时内别再试
                log.warning("无头浏览器没装好（python -m playwright install chromium-headless-shell）：%s", msg[:160])
                return None, "no"
            log.info("浏览器没打开 %s：%s", url, msg[:100])
            return None, msg[:80]
        d = self._pack(page, final, "浏览器")
        if looks_blocked(d.get("title", ""), d.get("text", "")):
            return d, "block"
        return d, "shell" if looks_shell(d.get("title", ""), d.get("text", "")) else ""

    async def _extract(self, url: str) -> tuple[dict | None, str]:
        w = self.web
        ds = bool(w is not None and getattr(w, "ds_key", "") and not w._ds_down())
        tav = bool(self.tavily_extract and w is not None and getattr(w, "tavily_key", "") and not w.tavily_out())
        if not (ds or tav):
            return None, "no"
        host = (urlparse(url).hostname or "").lower()
        try:
            if host in ("localhost",) or host.endswith((".local", ".lan", ".internal")) or not ipaddress.ip_address(host).is_global:
                return None, "down"
        except ValueError:
            pass
        got, err, via = {}, "", ""
        # 20.4.3 先用哪个跟搜索一样（默认 Tavily 免费额度在前，用完再 DeepSeek）
        order = (["DeepSeek", "Tavily"] if getattr(w, "ds_first", False) else ["Tavily", "DeepSeek"])
        for name in order:
            if got and got.get("text"):
                break
            if name == "DeepSeek" and ds:
                try:
                    got, via = await w.ds_fetch(url), "DeepSeek"
                except Exception as e:  # noqa: BLE001
                    err = str(e)[:80]
            elif name == "Tavily" and tav:
                try:
                    got, via = await w.extract(url), "Tavily"
                except Exception as e:  # noqa: BLE001
                    err = str(e)[:80]
        if err and not got:
            return None, err
        if not got or not got.get("text"):
            return None, "block"
        from aigroup.net.links import site_name
        raw = got["text"]
        links = [{"text": t, "url": u} for t, u in re.findall(r"(?<!!)\[([^\]\n]{2,60})\]\((https?://[^)\s]+)\)", raw)][:40]
        text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", raw)                      # 图片（还有 base64 的）去掉
        text = re.sub(r"\[([^\]\n]*)\]\((?:[^()\s]|\([^)]*\))*\)", r"\1", text)      # [文字](链接) → 文字（链接另外列）
        text = re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]+\n", "\n", text)).strip()
        title = got.get("title") or (re.search(r"^#\s+(.+)$", text, re.M).group(1) if re.search(r"^#\s+(.+)$", text, re.M) else "")
        if looks_blocked(title, text):
            return None, "block"
        return {"ok": True, "title": title[:120], "date": "", "text": text, "site": site_name(host), "url": url,
                "via": via, "links": links}, ""

    # ---------------- 入口 ----------------
    async def read(self, url: str) -> dict:
        url = (url or "").strip()
        if not re.match(r"^https?://", url):
            return {"ok": False, "error": "不是网址（要以 http:// 或 https:// 开头）"}
        hit = self._cache.get(url)
        if hit and time.time() - hit[0] < 3 * 3600:
            return hit[1]
        tried, best = [], None

        def take(d):
            nonlocal best
            if d is not None and d.get("ok") and (best is None or len(d.get("text", "")) > len(best.get("text", ""))):
                best = d
        d, why = await self._direct(url)
        tried.append("直连")
        if why == "type":
            return d
        take(d)
        steps = ((("block",), "仿浏览器", self._impersonate), (("block", "shell"), "浏览器", self._render),
                 (("block", "shell", "down"), "代读", self._extract))
        for need, label, fn in steps:
            if why not in need:
                continue
            d, w2 = await fn(url)
            if w2 == "no":
                continue                                         # 没装 / 没配：跳过这一层
            tried.append(label)
            take(d)
            if not w2 and d is not None:
                why = ""
                break
            if w2 in ("block", "shell", "down"):
                why = w2                                         # 别的错（这一层自己坏了）：保留原来的原因，接着试下一层
        if best is not None and best.get("ok") and looks_blocked(best.get("title", ""), best.get("text", "")):
            best = None                                          # 读到的只是人机验证页：不算读到
            why = "block"
        if best is None or not best.get("ok"):
            reason = {"block": "网站拦下了（要人机验证或登录）", "down": "连不上（国内打不开或者超时）",
                      "shell": "页面是脚本加载的，没读到内容"}.get(why, why or "读不到")
            out = best if (best and best.get("error")) else {"ok": False, "error": f"{reason}；试过：{'、'.join(tried) or '直连'}"}
        else:
            out = best
            if why in ("block", "shell"):
                note = "（可能要登录才能看全）" if LOGIN_RE.search(best.get("text", "")[:2000]) else "（可能没读全）"
                out = {**best, "note": note}
            log.info("读了 %s（%s，%d 字）", url, best.get("via"), len(best.get("text", "")))
        self._cache[url] = (time.time(), out)
        if len(self._cache) > 80:
            self._cache.pop(next(iter(self._cache)))
        return out
