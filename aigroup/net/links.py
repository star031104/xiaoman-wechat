"""群友分享的链接：点开看看是什么，告诉她标题、简介和正文开头，她就能接着聊，不用假装看过（从 ai-life 搬过来的）。

- B 站（含 b23.tv 短链、直接发 BV 号）：走公开接口拿标题、UP 主、时长、简介；
- YouTube：oEmbed 拿标题和作者；
- 其他网页（公众号、新闻、博客……）：读 og 标题 / 简介 和正文开头；图片链接交给看图模型；
- 打不开的（要登录、被墙、超时）就告诉她“打不开，只看到是哪个网站”。
安全：只访问公网地址（不碰 127.0.0.1、局域网、路由器后台这类），每一跳跳转都检查；最多读 2MB。
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import socket
import time
from html.parser import HTMLParser
from urllib.parse import quote, urljoin, urlparse

import httpx

log = logging.getLogger("aigroup.links")

URL_RE = re.compile(r"https?://[^\s<>\"'，。！？、；（）()【】\[\]{}|\\^`]+", re.I)
BV_RE = re.compile(r"(?<![0-9A-Za-z])(BV[0-9A-Za-z]{10})(?![0-9A-Za-z])")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0 Safari/537.36")
MAX_BYTES = 2 * 1024 * 1024
SITE_NAMES = {"bilibili.com": "B站", "b23.tv": "B站", "zhihu.com": "知乎", "weixin.qq.com": "公众号",
              "xiaohongshu.com": "小红书", "xhslink.com": "小红书", "douyin.com": "抖音", "weibo.com": "微博",
              "weibo.cn": "微博", "youtube.com": "YouTube", "youtu.be": "YouTube", "github.com": "GitHub",
              "music.163.com": "网易云音乐", "douban.com": "豆瓣", "x.com": "X（推特）", "twitter.com": "X（推特）"}


def find_links(text: str, limit: int = 2) -> list[str]:
    out = []
    for m in URL_RE.finditer(text or ""):
        u = m.group(0).rstrip(".,!?:;~")
        if u not in out:
            out.append(u)
    for m in BV_RE.finditer(text or ""):
        u = f"https://www.bilibili.com/video/{m.group(1)}"
        if not any(m.group(1) in x for x in out):
            out.append(u)
    return out[:limit]


def site_name(host: str) -> str:
    host = (host or "").lower()
    for k, v in SITE_NAMES.items():
        if host == k or host.endswith("." + k):
            return v
    return host.removeprefix("www.")


FAKE_IP = ipaddress.ip_network("198.18.0.0/15")        # Clash / 代理软件 fake-ip、TUN 模式下所有域名都解析到这里
NAT64 = ipaddress.ip_network("64:ff9b::/96")


def ip_ok(addr: str, fake_ok: bool = False) -> bool:
    """这个 IP 能不能访问：只要公网地址。"""
    try:
        ip = ipaddress.ip_address(addr.split("%")[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        inner = ip.ipv4_mapped or ip.sixtofour or (ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF) if ip in NAT64 else None)
        if inner is not None:
            return ip_ok(str(inner), fake_ok)
    if fake_ok and ip in FAKE_IP:
        return True
    return ip.is_global and not ip.is_multicast


async def is_public(url: str, proxy: str | None = None) -> bool:
    """只允许公网 http(s)：拒绝本机、局域网、保留地址（防止被一个链接骗去访问路由器后台之类）。"""
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        return False
    host = u.hostname
    try:
        ipaddress.ip_address(host.split("%")[0])
        return ip_ok(host)                 # 直接写 IP 的：fake-ip 段也不行
    except ValueError:
        pass
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, u.port or (443 if u.scheme == "https" else 80),
                                                             type=socket.SOCK_STREAM)
    except OSError:
        return bool(proxy)               # 本机解析不了（被墙的域名）：有代理就交给代理试，没代理就算了
    return bool(infos) and all(ip_ok(info[4][0], fake_ok=True) for info in infos)


class _Text(HTMLParser):
    """从网页里抠出标题、og 信息和正文段落。"""
    SKIP = {"script", "style", "noscript", "nav", "footer", "header", "aside", "form", "svg", "button"}
    BLOCK = {"p", "h1", "h2", "h3", "li", "blockquote", "section", "article", "br", "div"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}
        self.title = ""
        self._in_title = False
        self._skip = 0
        self.parts: list[str] = []
        self._buf: list[str] = []
        self._in_wx = 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "meta":
            k = (a.get("property") or a.get("name") or "").lower()
            if k in ("og:title", "og:description", "og:site_name", "description", "og:type", "twitter:title",
                     "twitter:description", "author"):
                self.meta.setdefault(k, (a.get("content") or "").strip())
        elif tag == "title":
            self._in_title = True
        elif tag in self.SKIP:
            self._skip += 1
        elif tag in self.BLOCK:
            self._flush()
        if a.get("id") == "js_content":          # 公众号正文
            self._in_wx = 1

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        elif tag in self.SKIP and self._skip:
            self._skip -= 1
        elif tag in self.BLOCK:
            self._flush()

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self._skip:
            self._buf.append(data)

    def _flush(self):
        t = re.sub(r"\s+", " ", "".join(self._buf)).strip()
        self._buf = []
        if len(t) >= 12:
            self.parts.append(t)

    def body(self, n: int) -> str:
        self._flush()
        seen, out = set(), []
        for p in self.parts:
            # 正文段落一般带标点、比较长；导航栏、按钮文字跳过
            if p in seen or len(p) < 12 or (len(p) < 40 and not re.search(r"[。！？!?；]|[，,].*[，,]", p)):
                continue
            seen.add(p)
            out.append(p)
            if sum(len(x) for x in out) > n:
                break
        return " ".join(out)[:n]


def visible_text(html: str) -> str:
    """页面上看得见的字（去掉脚本、样式、导航、页脚），一块一行。"""
    t = re.sub(r"(?is)<(script|style|noscript|svg|template|iframe)[^>]*>.*?</\1>", " ", html or "")
    t = re.sub(r"(?is)<(nav|footer)[^>]*>.*?</\1>", " ", t)
    t = re.sub(r"(?i)<br\s*/?>|</(p|div|li|tr|h\d|table|section|article)>", "\n", t)
    t = re.sub(r"(?i)</t[dh]>", " | ", t)
    t = re.sub(r"(?s)<[^>]+>", " ", t)
    import html as _h
    t = _h.unescape(t)
    t = "\n".join(re.sub(r"[ \t\r\f\v]+", " ", ln).strip(" |") for ln in t.split("\n"))
    return re.sub(r"\n{2,}", "\n", t).strip()


def page_text(html: str) -> dict:
    """网页 → {title, date, text}。装了 trafilatura 就用它（带表格），没装就把看得见的字都留下（数据站的表格也在里面）。"""
    title, date, text = "", "", ""
    try:
        import trafilatura  # type: ignore
        meta = trafilatura.extract_metadata(html)
        if meta is not None:
            title, date = str(meta.title or ""), str(meta.date or "")
        text = trafilatura.extract(html, include_comments=False, include_tables=True, favor_recall=True) or ""
    except Exception:  # noqa: BLE001
        text = ""
    if len(text.strip()) < 1500:                 # 正文太短（数据站、列表页）：把看得见的字都留下，取长的那个
        t = visible_text(html)
        if len(t) > len(text):
            text = t
        if not title:
            m = re.search(r"(?is)<title[^>]*>(.*?)</title>", html)
            title = re.sub(r"\s+", " ", m.group(1)).strip() if m else ""
    if not date:
        m = re.search(r'(?i)(?:article:published_time|datePublished|pubdate|publishdate)["\']?\s*(?:content=|:)\s*["\']([0-9]{4}-[0-9]{2}-[0-9]{2})', html)
        date = m.group(1) if m else ""
    return {"title": title[:120], "date": date[:10], "text": re.sub(r"[ \t]+", " ", text).strip()}


def _env_proxy(url: str) -> bool:
    """httpx 会自动用系统 / 环境变量里的代理；那种情况下连上的是代理，不检查对端地址。"""
    import urllib.request
    try:
        ps = urllib.request.getproxies()
        host = urlparse(url).hostname or ""
        return bool(ps.get(urlparse(url).scheme) or ps.get("all")) and not urllib.request.proxy_bypass(host)
    except Exception:  # noqa: BLE001
        return False


class LinkReader:
    def __init__(self, cfg, brain=None):
        c = dict((cfg.get("net") or {}).get("links") or {})
        self.enabled = bool(c.get("enabled", True))
        self.max_chars = int(c.get("max_chars", 500))
        self.proxy = cfg.get("proxy") or None
        # 要登录才能看的网站：用你自己浏览器里的 cookie（config.yaml 的 net.cookies，比如 {"zhihu.com": "z_c0=…"}）
        self.cookies = {str(k).lower().lstrip("."): str(v) for k, v in
                        ((cfg.get("net") or {}).get("cookies") or {}).items() if v}
        self.brain = brain
        self._cache: dict[str, tuple[float, dict]] = {}
        self._videos: list[dict] = []                    # 刚读到的 B 站视频（等着“点开看”：读字幕、弹幕、热评）

    async def fetch_bytes(self, url: str, headers: dict | None = None) -> bytes:
        """给 B 站“看视频”用：一样只访问公网、带 cookie，最多 24MB（弹幕 XML、视频的音频）。"""
        _, buf, _ = await self._get(url, headers=headers, max_bytes=24 * 1024 * 1024)
        return buf

    def take_videos(self) -> list[dict]:
        """拿走刚读到的 B 站视频（每个只给一次）。"""
        out, self._videos = self._videos, []
        return out

    async def _get(self, url: str, headers: dict | None = None, max_bytes: int = MAX_BYTES,
                   proxy_first: bool = False) -> tuple[httpx.Response, bytes, str]:
        """先直连再走代理（国外网站反过来）；自己处理跳转，每一跳都检查是不是公网。"""
        routes = [None, self.proxy] if self.proxy else [None]
        if proxy_first and self.proxy:
            routes.reverse()
        last = None
        for proxy in routes:
            try:
                async with httpx.AsyncClient(proxy=proxy, timeout=httpx.Timeout(8, read=10),
                                             headers={"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9",
                                                      **(headers or {})}) as c:
                    cur = url
                    for _ in range(5):
                        if not await is_public(cur, self.proxy):
                            raise RuntimeError("不是公网地址")
                        host = (urlparse(cur).hostname or "").lower()
                        ck = next((v for d, v in self.cookies.items() if host == d or host.endswith("." + d)), "")
                        async with c.stream("GET", cur, headers={"Cookie": ck} if ck else None) as r:
                            if proxy is None and not _env_proxy(cur):   # 防 DNS 重绑定：真正直连的那个地址也要是公网
                                try:
                                    peer = r.extensions["network_stream"].get_extra_info("server_addr")
                                except Exception:  # noqa: BLE001
                                    peer = None
                                if peer and not ip_ok(str(peer[0]), fake_ok=True):
                                    raise RuntimeError("不是公网地址")
                            if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
                                cur = urljoin(cur, r.headers["location"])
                                continue
                            r.raise_for_status()
                            buf = b""
                            async for ch in r.aiter_bytes():
                                buf += ch
                                if len(buf) >= max_bytes:
                                    r.extensions = {**r.extensions, "truncated": True}
                                    break
                            return r, buf, cur
                    raise RuntimeError("跳转太多")
            except Exception as e:  # noqa: BLE001
                last = e
        raise RuntimeError(str(last)[:120])

    async def read(self, url: str) -> dict:
        hit = self._cache.get(url)
        if hit and time.time() - hit[0] < 6 * 3600:
            return hit[1]
        host = (urlparse(url).hostname or "").lower()
        info = {"url": url, "site": site_name(host), "ok": False}
        try:
            if host.endswith("b23.tv") or "bilibili.com" in host:
                info.update(await self._bilibili(url))
            elif host.endswith(("youtube.com", "youtu.be")):
                info.update(await self._youtube(url))
            else:
                info.update(await self._page(url))
        except Exception as e:  # noqa: BLE001
            log.info("链接没打开（%s）：%s", url[:60], e)
            info["error"] = str(e)[:80]
        self._cache[url] = (time.time(), info)
        if len(self._cache) > 100:
            self._cache.pop(next(iter(self._cache)))
        return info

    # ---------------- B 站 ----------------
    async def _bilibili(self, url: str) -> dict:
        m = BV_RE.search(url)
        final = url
        if not m:                                       # b23.tv 短链：先跳转拿到真实地址
            _, _, final = await self._get(url, max_bytes=4096)
            m = BV_RE.search(final)
            if not m:
                return await self._page(final)
        bv = m.group(1)
        pm = re.search(r"[?&]p=(\d{1,3})", str(final))      # 15.0 多 P 视频：链接里的 ?p=3
        part = int(pm.group(1)) if pm else 1
        import json
        import uuid
        d = {}
        try:
            r, buf, _ = await self._get(f"https://api.bilibili.com/x/web-interface/view?bvid={bv}",
                                        headers={"Referer": "https://www.bilibili.com/",
                                                 "Cookie": f"buvid3={str(uuid.uuid4()).upper()}infoc"},
                                        max_bytes=512 * 1024)
            d = (json.loads(buf.decode("utf-8", "ignore")) or {}).get("data") or {}
        except Exception as e:  # noqa: BLE001
            log.info("B站接口没返回（%s），改读视频页面", e)
        if not d:                                       # 接口被风控时：读视频页面上的标题和简介
            page = await self._page(f"https://www.bilibili.com/video/{bv}")
            page["kind"] = "B站视频"
            page["title"] = re.sub(r"_哔哩哔哩_bilibili$", "", page.get("title", "")).strip()
            page.pop("text", None)
            if "视频去哪了" in page["title"] or "{$" in page.get("desc", ""):
                raise RuntimeError("B站这会儿不让看（风控）")
            page["desc"] = re.sub(r",\s*视频播放量.*$", "", page.get("desc", "")).strip()
            page["bv"] = bv
            page["p"] = part
            return page
        dur = int(d.get("duration") or 0)
        stat = d.get("stat") or {}
        return {"ok": True, "kind": "B站视频", "bv": bv, "p": part, "title": d.get("title", ""), "author": (d.get("owner") or {}).get("name", ""),
                "duration": f"{dur // 60}:{dur % 60:02d}" if dur else "", "category": d.get("tname", ""),
                "desc": (d.get("desc") or "").strip()[:300],
                "views": stat.get("view")}

    # ---------------- YouTube ----------------
    async def _youtube(self, url: str) -> dict:
        r, buf, _ = await self._get(f"https://www.youtube.com/oembed?format=json&url={quote(url, safe='')}",
                                    max_bytes=64 * 1024, proxy_first=True)
        import json
        d = json.loads(buf.decode("utf-8", "ignore"))
        return {"ok": True, "kind": "YouTube 视频", "title": d.get("title", ""), "author": d.get("author_name", "")}

    # ---------------- 一般网页 ----------------
    async def _page(self, url: str) -> dict:
        r, buf, final = await self._get(url)
        ctype = (r.headers.get("content-type") or "").lower()
        if ctype.startswith("image/"):
            desc = ""
            if self.brain is not None and getattr(self.brain.llm, "vision", False) \
                    and not r.extensions.get("truncated"):          # 太大被截断的图片是坏的，不送去看
                import base64
                try:
                    desc = await self.brain.llm.describe_image(base64.b64encode(buf).decode(), "")
                except Exception:  # noqa: BLE001
                    desc = ""
            return {"ok": bool(desc), "kind": "图片", "title": "", "desc": desc}
        if "pdf" in ctype:
            return {"ok": True, "kind": "PDF 文件", "title": final.rsplit("/", 1)[-1][:60], "desc": ""}
        if "html" not in ctype and "xml" not in ctype and ctype:
            return {"ok": False, "kind": ctype.split(";")[0], "title": ""}
        charset = "utf-8"
        m = re.search(r"charset=([\w-]+)", ctype) or re.search(rb"<meta[^>]+charset=[\"']?([\w-]+)", buf[:4096], re.I)
        if m:
            charset = m.group(1).decode() if isinstance(m.group(1), bytes) else m.group(1)
        try:
            html = buf.decode(charset, errors="ignore")
        except LookupError:
            html = buf.decode("utf-8", errors="ignore")
        p = _Text()
        try:
            p.feed(html)
        except Exception:  # noqa: BLE001
            pass
        title = (p.meta.get("og:title") or p.meta.get("twitter:title") or p.title or "").strip()
        desc = (p.meta.get("og:description") or p.meta.get("description") or p.meta.get("twitter:description") or "").strip()
        body = p.body(self.max_chars)
        try:                                   # 13.0 装了 trafilatura 就用它抠正文（广告、导航去得更干净），没装用自己的
            import trafilatura  # type: ignore
            tb = trafilatura.extract(html, include_comments=False, include_tables=False, favor_precision=True) or ""
            if len(tb.strip()) >= 40:
                body = re.sub(r"\s+", " ", tb).strip()[:self.max_chars]
        except Exception:  # noqa: BLE001
            pass
        if not title and not body:
            raise RuntimeError("页面里没读到内容（可能要登录）")
        if re.search(r"安全验证|人机验证|验证码|captcha|just a moment|attention required|请登录|登录后", title, re.I):
            raise RuntimeError("网站要验证或登录，没看到内容")
        site = p.meta.get("og:site_name") or site_name(urlparse(final).hostname or "")
        return {"ok": True, "kind": "网页", "title": re.sub(r"\s+", " ", title)[:100], "desc": desc[:200],
                "text": body, "site": site}

    # ---------------- 19.0 查资料：读一个网页的正文（长一点、能翻页、表格里的数字也要） ----------------
    async def read_text(self, url: str, offset: int = 0, n: int = 6000) -> dict:
        """返回 {ok, title, site, date, text, total, more}；读不到时 {ok: False, error}。只访问公网地址。"""
        url = (url or "").strip()
        if not re.match(r"^https?://", url):
            return {"ok": False, "error": "不是网址（要以 http:// 或 https:// 开头）"}
        key = "full|" + url
        hit = self._cache.get(key)
        if hit and time.time() - hit[0] < 6 * 3600:
            info = hit[1]
        else:
            info = {"ok": False}
            try:
                r, buf, final = await self._get(url)
                ctype = (r.headers.get("content-type") or "").lower()
                if "html" not in ctype and "xml" not in ctype and "text" not in ctype and ctype:
                    raise RuntimeError(f"不是网页（{ctype.split(';')[0]}）")
                charset = "utf-8"
                m = re.search(r"charset=([\w-]+)", ctype) or re.search(rb"<meta[^>]+charset=[\"']?([\w-]+)", buf[:4096], re.I)
                if m:
                    charset = m.group(1).decode() if isinstance(m.group(1), bytes) else m.group(1)
                try:
                    page = buf.decode(charset, errors="ignore")
                except LookupError:
                    page = buf.decode("utf-8", errors="ignore")
                info = {"ok": True, **page_text(page), "site": site_name(urlparse(final).hostname or "")}
                if not info["text"]:
                    raise RuntimeError("页面里没读到正文（可能要登录，或者内容是脚本加载的）")
                if re.search(r"安全验证|人机验证|验证码|captcha|just a moment|attention required", info.get("title", ""), re.I):
                    raise RuntimeError("网站要人机验证，看不到内容")
            except Exception as e:  # noqa: BLE001
                info = {"ok": False, "error": str(e)[:100]}
            self._cache[key] = (time.time(), info)
            if len(self._cache) > 100:
                self._cache.pop(next(iter(self._cache)))
        if not info.get("ok"):
            return info
        text = info["text"]
        offset = max(0, int(offset or 0))
        part = text[offset:offset + n]
        return {**{k: v for k, v in info.items() if k != "text"}, "text": part, "total": len(text),
                "more": offset + n < len(text)}

    # ---------------- 给她看的一段话 ----------------
    @staticmethod
    def describe(info: dict) -> str:
        site = info.get("site") or "网页"
        if not info.get("ok"):
            return f"[链接：{site}的，你点开没打开（可能要登录或网络不通）]"
        k = info.get("kind")
        if k in ("B站视频", "YouTube 视频"):
            bits = [f"《{info.get('title', '')}》"]
            meta = "，".join(x for x in (f"UP主 {info['author']}" if info.get("author") and k == "B站视频" else
                                         (f"作者 {info['author']}" if info.get("author") else ""),
                                         info.get("duration") and f"时长 {info['duration']}",
                                         info.get("category") and f"分区 {info['category']}") if x)
            if meta:
                bits.append(f"（{meta}）")
            if info.get("desc") and info["desc"] not in ("-", "无"):
                bits.append(f"，简介：{info['desc'][:200]}")
            more = "，你正在点开看" if info.get("bv") else "。你只看到了标题和简介，没看过视频本身"
            return f"[链接：{k}" + "".join(bits) + more + "]"
        if k == "图片":
            return f"[链接：一张图片，{info.get('desc', '')}]"
        if k == "PDF 文件":
            return f"[链接：PDF 文件「{info.get('title', '')}」，你没打开看]"
        parts = [f"「{info['title']}」" if info.get("title") else ""]
        if info.get("desc"):
            parts.append(f"简介：{info['desc']}")
        if info.get("text"):
            parts.append(f"正文开头：{info['text']}")
        return f"[链接：{site}的网页" + "；".join(x for x in parts if x) + "]"

    async def enrich(self, text: str, extra: list[str] | None = None) -> str:
        """把一段话里的链接（加上分享卡片里的链接 extra）都读一遍，返回追加给她看的说明（没有链接返回空）。"""
        if not self.enabled:
            return ""
        urls = list(dict.fromkeys([u for u in (extra or []) if u.startswith("http")] + find_links(text)))[:2]
        if not urls:
            return ""
        async def one(u):
            try:
                return await asyncio.wait_for(self.read(u), 12)       # 慢吞吞的网站不能让她一直“点开看”
            except asyncio.TimeoutError:
                host = (urlparse(u).hostname or "").lower()
                return {"url": u, "site": site_name(host), "ok": False, "error": "超时"}
        infos = await asyncio.gather(*(one(u) for u in urls))
        self._videos += [i for i in infos if i.get("ok") and i.get("bv")]
        return "\n".join(self.describe(i) for i in infos)
