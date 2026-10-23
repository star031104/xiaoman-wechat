"""4.4 B 站视频“点开看了”：不止标题和简介——读字幕（有的话）、弹幕（哪几段最热闹、大家在刷什么）、热评，
让后台模型整理成“讲了什么、几个要点（带时间）、高能的地方、评论区在说什么”，她就能聊具体内容
（“第 3 分钟那段笑死”），不会假装看过画面。

- 字幕：B 站的字幕接口（要 wbi 签名；很多视频的 AI 字幕要登录才给——config 的 net.cookies 里填了 bilibili.com 的 cookie 就能拿到）
- 没字幕：把音频拿下来（公开的 playurl 接口，最低音质），切成一分钟一段，用语音识别转成文字（默认硅基流动的
  FunAudioLLM/SenseVoiceSmall，Key 用你在硅基流动的那个；没装 ffmpeg 就整段转、不带时间）；只转前 20 分钟
- 弹幕：公开的弹幕 XML，按 20 秒一段数，挑最密的几段和那里刷得最多的词
- 热评：公开的评论接口，按赞数挑几条
- 画面（15.0）：拿最低清晰度的视频流，用 ffmpeg 在几个时间点各截一张（均匀撒点 + 弹幕最热闹的几段），连同封面一起交给看图模型，
  一次调用写出“每个时间点画面上是什么”；没装 ffmpeg / 没配看图模型就跳过
- 多 P：链接里带 ?p=3 就看第 3 P
- 都拿不到就只用标题、简介、标签，并且说清楚“没看到内容”
这个文件 QQ 群版、微信版、ai-life 一样（只依赖 fetch 和 llm 两个参数）。
"""
from __future__ import annotations

import asyncio
import hashlib
import html
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from collections import Counter
from urllib.parse import urlencode

log = logging.getLogger(__name__.split(".")[0] + ".bili")

MIXIN = [46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38,
         41, 13, 37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36,
         20, 34, 44, 52]

SUMMARY_PROMPT = """你帮一个人“看”一个 B 站视频：下面是视频的标题、简介、字幕或语音转写（如果有）、几个时间点的画面（如果有）、
弹幕最热闹的几段、热评。把声音和画面对上（比如“05:20 画面上是……，这时候他在说……”），整理成她能拿来聊天的要点。
只根据给的材料写：画面只知道截到的那几帧，别的时间的画面不要编；没有字幕也没有画面就只说能从简介、弹幕、评论看出来的。
只输出 JSON：
{"gist": "这个视频讲了什么（一两句话，不超过 60 字）",
 "points": ["带时间的要点，比如 03:12 博主吐槽了……、05:20 画面上……（最多 6 条，没有内容可以不写）"],
 "visual": "画面大概是什么样（真人出镜 / 游戏录屏 / 动画 / 幻灯片 / 实拍风景……，有什么让人记住的画面），没有画面材料写空字符串",
 "highlights": "最好笑 / 最高能 / 大家刷屏的地方，说清楚在第几分钟、大家在刷什么（不超过 60 字）",
 "vibe": "评论区大家怎么说（不超过 50 字）",
 "keys": ["以后聊到时会出现的关键词（2~5 个）"],
 "seen": "full（有字幕或语音转写，内容基本知道）或 partial（只有简介、弹幕、评论、画面截图）"}

"""

FRAMES_PROMPT = """这是 B 站视频《{title}》（UP 主：{author}）的封面和几个时间点的截图，按顺序是：{labels}。
逐张用中文写画面上是什么（人物在做什么、场景、画面上的字幕或大字、是游戏 / 动画 / 实拍 / PPT……），每张 40 字以内；
看不清就写“看不清”。只输出 JSON：{{"frames": [{{"t": "封面 或 mm:ss", "see": "……"}}]}}"""


def pick_times(duration: int, hot: list[dict], n: int = 8) -> list[int]:
    """截哪几秒：均匀撒点（避开开头 3 秒和片尾），再加上弹幕最热闹的几段；挨得太近（15 秒内）的只留一个。"""
    if duration <= 0 or n <= 0:
        return []
    end = max(1, duration - 3)
    even = [int(3 + (end - 3) * (i + 0.5) / n) for i in range(n)] if end > 3 else [0]
    hots = []
    for m in hot or []:
        try:
            mm, ss = str(m.get("t", "")).split(":")[-2:]
            hots.append(int(mm) * 60 + int(ss) + 5)                # 热闹的那 20 秒里偏前一点
        except (ValueError, AttributeError):
            continue
    out: list[int] = []
    for t in sorted(set(hots[:3] + even)):
        if 0 <= t < duration and all(abs(t - x) >= 15 for x in out):
            out.append(t)
    return out[:n + 2]


CN_NUM = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def line_time(line: str) -> int | None:
    """“[03:12] ……” → 192 秒。"""
    m = re.match(r"\s*\[(?:(\d+):)?(\d{1,2}):(\d{2})\]", line or "")
    if not m:
        return None
    return int(m.group(1) or 0) * 3600 + int(m.group(2)) * 60 + int(m.group(3))


def ask_time(q: str, lines: str = "") -> int | None:
    """15.0 群友问的是视频里哪个时间：“3:12”“第三分钟”“5 分多钟”“开头”“最后 / 结尾”。认不出返回 None。"""
    q = q or ""
    m = re.search(r"(\d{1,2})\s*[:：]\s*(\d{2})", q)
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))
    m = re.search(r"第?\s*(\d{1,3}|[一二两三四五六七八九十]{1,3})\s*分(?:多|半)?钟?", q)
    if m:
        g = m.group(1)
        if g.isdigit():
            n = int(g)
        elif "十" in g:                                          # 十 / 十二 / 二十 / 二十五
            a, _, b = g.partition("十")
            n = (CN_NUM.get(a, 1) if a else 1) * 10 + (CN_NUM.get(b, 0) if b else 0)
        else:
            n = CN_NUM.get(g, 0)
        return n * 60 + (30 if re.search(r"多|半", m.group(0)) else 0)
    if re.search(r"开头|一开始|刚开始", q):
        return 0
    if re.search(r"最后|结尾|末尾", q):
        ts = [t for t in (line_time(x) for x in (lines or "").splitlines()) if t is not None]
        return max(ts) if ts else None
    return None


def mixin_key(img_key: str, sub_key: str) -> str:
    raw = img_key + sub_key
    return "".join(raw[i] for i in MIXIN if i < len(raw))[:32]


def wbi_sign(params: dict, img_key: str, sub_key: str, ts: int | None = None) -> dict:
    """B 站 wbi 签名：参数排序、去掉 !'()* 、拼上 mixin key 求 md5。"""
    p = dict(params, wts=int(ts or time.time()))
    p = {k: re.sub(r"[!'()*]", "", str(v)) for k, v in sorted(p.items())}
    q = urlencode(p)
    p["w_rid"] = hashlib.md5((q + mixin_key(img_key, sub_key)).encode()).hexdigest()
    return p


def mmss(t: float) -> str:
    t = int(max(0, t))
    return f"{t // 3600}:{t % 3600 // 60:02d}:{t % 60:02d}" if t >= 3600 else f"{t // 60:02d}:{t % 60:02d}"


def parse_danmaku(xml: str) -> list[tuple[float, str]]:
    out = []
    for m in re.finditer(r'<d p="([\d.]+),[^"]*">(.*?)</d>', xml or "", re.S):
        txt = html.unescape(m.group(2)).strip()
        if txt and not _noise(txt):                     # “刚刚”“三小时前”“前排”这类打卡弹幕、高级弹幕的代码不算
            out.append((float(m.group(1)), txt))
    return out


NOISE_RE = re.compile(r"^(刚刚|刚才|\S{0,3}(秒|分钟|小时|天)前|前排|第一|第\d+|打卡|来了|签到|\d+|[?？!！.。，,~～]+|[?？]+.?|1+|草+|啊+)$")


def _noise(t: str) -> bool:
    t = t.strip()
    return bool(NOISE_RE.match(t)) or (t.startswith("[") and '"' in t) or len(t) > 40


def _norm(s: str) -> str:
    s = re.sub(r"(哈){3,}", "哈哈哈", s)
    s = re.sub(r"(.)\1{3,}", r"\1\1\1", s)
    return s.strip()[:16]


def hot_moments(dm: list[tuple[float, str]], k: int = 3, bucket: int = 20) -> list[dict]:
    """弹幕最密的几段（每段 bucket 秒，挑出来的几段至少隔开 1 分钟），每段刷得最多的几句。"""
    if len(dm) < 8:
        return []
    cnt = Counter(int(t // bucket) for t, _ in dm)
    picked: list[int] = []
    for b, _n in cnt.most_common():
        if all(abs(b - x) * bucket >= 60 for x in picked):
            picked.append(b)
        if len(picked) >= k:
            break
    out = []
    for b in sorted(picked):
        words = Counter(_norm(t) for s, t in dm if int(s // bucket) == b)
        out.append({"t": mmss(b * bucket), "n": cnt[b], "top": [w for w, _ in words.most_common(4) if w]})
    return out


def common_danmaku(dm: list[tuple[float, str]], k: int = 6) -> list[str]:
    words = Counter(_norm(t) for _, t in dm)
    return [w for w, n in words.most_common(k) if n >= 2]


def subtitle_lines(body: list[dict], max_chars: int = 5000, every: float = 20.0) -> str:
    """字幕合并成每 20 秒一行“[mm:ss] ……”，太长就均匀抽着留。"""
    rows, cur, start = [], [], None
    for x in body or []:
        t, c = float(x.get("from") or 0), str(x.get("content") or "").strip()
        if not c:
            continue
        if start is None:
            start = t
        if t - start >= every and cur:
            rows.append(f"[{mmss(start)}] {''.join(cur)}")
            cur, start = [], t
        cur.append(c + "，")
    if cur:
        rows.append(f"[{mmss(start or 0)}] {''.join(cur)}")
    text = "\n".join(rows)
    if len(text) <= max_chars:
        return text
    step = len(text) / max_chars
    keep = [r for i, r in enumerate(rows) if int(i % max(1, round(step))) == 0]
    return "\n".join(keep)[:max_chars]


EMOJI_RE = re.compile("[\U0001F000-\U0001FFFF\u2600-\u27BF]")


class ASR:
    """语音转文字：OpenAI 兼容的 /audio/transcriptions。net.asr 里写了用那个；没写就找一个硅基流动的接口借它的 Key。"""

    def __init__(self, cfg):
        c = dict(((cfg.get("net") or {}).get("asr")) or {})
        key, base = str(c.get("api_key") or ""), str(c.get("base_url") or "")
        if not key:
            for p in ((cfg.get("llm") or {}).get("profiles") or {}).values():
                if "siliconflow" in str(p.get("base_url", "")) and str(p.get("api_key") or "").strip():
                    key, base = str(p["api_key"]), str(p["base_url"])
                    break
        self.key = key.strip()
        self.base = (base or "https://api.siliconflow.cn/v1").rstrip("/")
        self.model = str(c.get("model") or "FunAudioLLM/SenseVoiceSmall")
        self.enabled = bool(c.get("enabled", True)) and bool(self.key)
        self.max_minutes = int(c.get("max_minutes", 20))

    async def one_raw(self, data: bytes, name: str = "a.mp3", mime: str = "audio/mpeg") -> str:
        """原样返回（SenseVoice 会带表示情绪、笑声的 emoji，13.0 的语音消息要用）。"""
        import httpx
        async with httpx.AsyncClient(timeout=60) as cli:
            r = await cli.post(f"{self.base}/audio/transcriptions", headers={"Authorization": f"Bearer {self.key}"},
                               files={"file": (name, data, mime)}, data={"model": self.model})
            r.raise_for_status()
            return str(r.json().get("text") or "")

    async def one(self, data: bytes, name: str = "a.mp3", mime: str = "audio/mpeg") -> str:
        return EMOJI_RE.sub("", await self.one_raw(data, name, mime)).strip()

    async def transcribe(self, audio: bytes, seconds: int) -> str:
        """一分钟一段，带时间（要 ffmpeg）；没有 ffmpeg 就整段转（只转 10 分钟以内的）。"""
        limit = self.max_minutes * 60
        if not shutil.which("ffmpeg"):
            if seconds > 600:
                return ""
            txt = await self.one(audio, "a.m4a", "audio/mp4")
            return f"[全程] {txt}" if txt else ""
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "in.m4a")
            with open(src, "wb") as f:
                f.write(audio)
            cmd = ["ffmpeg", "-v", "error", "-i", src, "-t", str(limit), "-ac", "1", "-ar", "16000", "-b:a", "32k",
                   "-f", "segment", "-segment_time", "60", os.path.join(td, "s%03d.mp3")]
            await asyncio.to_thread(subprocess.run, cmd, capture_output=True, timeout=120)
            parts = sorted(x for x in os.listdir(td) if x.startswith("s") and x.endswith(".mp3"))
            sem = asyncio.Semaphore(4)

            async def go(i, fn):
                async with sem:
                    with open(os.path.join(td, fn), "rb") as f:
                        data = f.read()
                    try:
                        return i, await self.one(data, fn)
                    except Exception as e:  # noqa: BLE001
                        log.info("第 %d 分钟没转成文字：%s", i, e)
                        return i, ""
            res = await asyncio.gather(*(go(i, fn) for i, fn in enumerate(parts)))
        lines = [f"[{i:02d}:00] {t}" for i, t in sorted(res) if t]
        more = "（后面没转）" if seconds > limit else ""
        return "\n".join(lines) + more


class Bili:
    """fetch(url, headers) -> bytes（用 LinkReader 的：只访问公网、带上 net.cookies 里 B 站的 cookie）。"""

    def __init__(self, fetch, llm=None, asr: ASR | None = None):
        self.fetch, self.llm, self.asr = fetch, llm, asr
        self._wbi: tuple[float, str, str] = (0.0, "", "")
        self.buvid = f"buvid3={str(uuid.uuid4()).upper()}infoc"

    def _h(self, bv: str = "") -> dict:
        return {"Referer": f"https://www.bilibili.com/video/{bv}" if bv else "https://www.bilibili.com/",
                "Origin": "https://www.bilibili.com", "Cookie": self.buvid}

    async def _json(self, url: str, bv: str = "") -> dict:
        buf = await self.fetch(url, self._h(bv))
        d = json.loads(buf.decode("utf-8", "ignore") or "{}")
        return d if isinstance(d, dict) else {}

    async def _keys(self) -> tuple[str, str]:
        t, a, b = self._wbi
        if a and time.time() - t < 6 * 3600:
            return a, b
        d = (await self._json("https://api.bilibili.com/x/web-interface/nav")).get("data") or {}
        img = (d.get("wbi_img") or {}).get("img_url", "")
        sub = (d.get("wbi_img") or {}).get("sub_url", "")
        a, b = img.rsplit("/", 1)[-1].split(".")[0], sub.rsplit("/", 1)[-1].split(".")[0]
        self._wbi = (time.time(), a, b)
        return a, b

    async def view(self, bv: str) -> dict:
        d = (await self._json(f"https://api.bilibili.com/x/web-interface/view?bvid={bv}", bv)).get("data") or {}
        if not d:
            raise RuntimeError("拿不到视频信息")
        return d

    async def subtitles(self, bv: str, aid: int, cid: int) -> str:
        try:
            a, b = await self._keys()
            q = urlencode(wbi_sign({"aid": aid, "cid": cid}, a, b))
            d = (await self._json(f"https://api.bilibili.com/x/player/wbi/v2?{q}", bv)).get("data") or {}
            subs = (d.get("subtitle") or {}).get("subtitles") or []
        except Exception as e:  # noqa: BLE001
            log.info("字幕列表没拿到：%s", e)
            return ""
        if not subs:
            return ""
        subs.sort(key=lambda s: (0 if str(s.get("lan", "")).startswith("zh") else 1, 0 if "ai" not in str(s.get("lan")) else 1))
        url = str(subs[0].get("subtitle_url") or "")
        if not url:
            return ""
        if url.startswith("//"):
            url = "https:" + url
        try:
            body = json.loads((await self.fetch(url, self._h(bv))).decode("utf-8", "ignore")).get("body") or []
        except Exception as e:  # noqa: BLE001
            log.info("字幕没下载下来：%s", e)
            return ""
        return subtitle_lines(body)

    async def audio(self, bv: str, cid: int) -> bytes:
        """最低音质的音频（公开的 playurl 接口，不用登录）。"""
        d = (await self._json(f"https://api.bilibili.com/x/player/playurl?bvid={bv}&cid={cid}&fnval=16&fnver=0&fourk=0",
                              bv)).get("data") or {}
        tracks = sorted((d.get("dash") or {}).get("audio") or [], key=lambda a: int(a.get("bandwidth") or 0))
        if not tracks:
            return b""
        t = tracks[0]
        for url in [t.get("baseUrl") or t.get("base_url"), *(t.get("backupUrl") or t.get("backup_url") or [])]:
            if url:
                try:
                    return await self.fetch(url, {"Referer": "https://www.bilibili.com/"})
                except Exception as e:  # noqa: BLE001
                    log.info("音频没下载下来：%s", e)
        return b""

    async def video_urls(self, bv: str, cid: int) -> list[str]:
        """15.0 最低清晰度的视频流（优先 AVC 编码，ffmpeg 都能解）。"""
        d = (await self._json(f"https://api.bilibili.com/x/player/playurl?bvid={bv}&cid={cid}&fnval=16&fnver=0&fourk=0",
                              bv)).get("data") or {}
        vs = (d.get("dash") or {}).get("video") or []
        vs.sort(key=lambda v: (0 if int(v.get("codecid") or 0) == 7 else 1, int(v.get("bandwidth") or 0)))
        out = []
        for v in vs[:2]:
            for url in [v.get("baseUrl") or v.get("base_url"), *(v.get("backupUrl") or v.get("backup_url") or [])]:
                if url and url not in out:
                    out.append(url)
        return out

    async def grab(self, url: str, t: int, timeout: float = 25.0) -> bytes:
        """用 ffmpeg 从视频流的第 t 秒截一张 480 宽的 JPEG（只按需读那一小段，不下整个视频）。"""
        ff = shutil.which("ffmpeg")
        if not ff:
            return b""
        hdr = "Referer: https://www.bilibili.com/\r\nUser-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36\r\n"
        cmd = [ff, "-nostdin", "-loglevel", "error", "-headers", hdr, "-ss", str(int(t)), "-i", url, "-frames:v", "1",
               "-vf", "scale=480:-2", "-q:v", "6", "-f", "image2", "-c:v", "mjpeg", "pipe:1"]
        try:
            p = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            out, _ = await asyncio.wait_for(p.communicate(), timeout)
        except Exception as e:  # noqa: BLE001
            log.info("第 %s 秒没截到：%s", t, e)
            try:
                p.kill()
            except Exception:  # noqa: BLE001
                pass
            return b""
        return out if out[:2] == b"\xff\xd8" else b""

    async def frames(self, bv: str, cid: int, times: list[int]) -> list[tuple[int, bytes]]:
        if not times or not shutil.which("ffmpeg"):
            return []
        try:
            urls = await self.video_urls(bv, cid)
        except Exception as e:  # noqa: BLE001
            log.info("视频流没拿到：%s", e)
            return []
        if not urls:
            return []
        sem = asyncio.Semaphore(3)

        async def one(t):
            async with sem:
                for u in urls[:2]:
                    b = await self.grab(u, t)
                    if b:
                        return t, b
                return t, b""
        got = await asyncio.gather(*(one(t) for t in times))
        return [(t, b) for t, b in got if b]

    async def see(self, w: dict, shots: list[tuple[int, bytes]], cover: bytes = b"") -> list[dict]:
        """15.0 封面 + 截图一次交给看图模型；它不收多张图就一张一张看（最多 6 张）。"""
        if self.llm is None or not getattr(self.llm, "vision", False) or (not shots and not cover):
            return []
        import base64
        imgs = ([("封面", cover)] if cover else []) + [(mmss(t), b) for t, b in shots]
        prompt = FRAMES_PROMPT.format(title=w.get("title", ""), author=w.get("author", ""),
                                      labels="、".join(lab for lab, _ in imgs))
        content = [{"type": "text", "text": prompt}] + [
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(b).decode()}}
            for _, b in imgs]
        try:
            raw = await self.llm.chat([{"role": "user", "content": content}], role="vision", temperature=0.2,
                                      max_tokens=900, background=True)
            from aigroup.llm.client import parse_json
            d = parse_json(raw, {}) or {}
            fr = [{"t": str(x.get("t") or ""), "see": str(x.get("see") or "")[:80]} for x in (d.get("frames") or [])
                  if isinstance(x, dict) and x.get("see")]
            if fr:
                return fr
        except Exception as e:  # noqa: BLE001
            log.info("一次看多张没成（%s），改成一张一张看", e)
        out = []
        for lab, b in imgs[:6]:
            try:
                txt = await self.llm.describe_image(base64.b64encode(b).decode(),
                                                    prompt=f"这是 B 站视频《{w.get('title', '')}》{lab}的截图。用中文一句话说画面上是什么，40 字以内。")
                if txt:
                    out.append({"t": lab, "see": txt.strip()[:80]})
            except Exception as e:  # noqa: BLE001
                log.info("截图没看成：%s", e)
                break
        return out

    async def speech(self, bv: str, cid: int, seconds: int) -> str:
        if self.asr is None or not self.asr.enabled or seconds > 90 * 60:
            return ""
        try:
            data = await self.audio(bv, cid)
            if len(data) < 2000:
                return ""
            return await self.asr.transcribe(data, seconds)
        except Exception as e:  # noqa: BLE001
            log.info("视频的声音没转成文字：%s", e)
            return ""

    async def danmaku(self, cid: int) -> list[tuple[float, str]]:
        try:
            buf = await self.fetch(f"https://api.bilibili.com/x/v1/dm/list.so?oid={cid}", self._h())
            return parse_danmaku(buf.decode("utf-8", "ignore"))
        except Exception as e:  # noqa: BLE001
            log.info("弹幕没拿到：%s", e)
            return []

    async def comments(self, aid: int, bv: str, k: int = 6) -> list[str]:
        try:
            d = (await self._json(f"https://api.bilibili.com/x/v2/reply?type=1&oid={aid}&sort=1&ps=20&pn=1", bv)).get("data") or {}
        except Exception as e:  # noqa: BLE001
            log.info("评论没拿到：%s", e)
            return []
        rs = list((d.get("hots") or []) + (d.get("replies") or []))
        rs.sort(key=lambda r: -int(r.get("like") or 0))
        out, seen = [], set()
        for r in rs:
            msg = re.sub(r"\[[^\]]{1,8}\]", "", str((r.get("content") or {}).get("message") or "")).strip()
            if msg and msg not in seen and len(msg) <= 120:
                seen.add(msg)
                out.append(f"{msg}（赞 {r.get('like', 0)}）")
            if len(out) >= k:
                break
        return out

    async def watch(self, bv: str, part: int = 1, n_frames: int = 8) -> dict:
        """把能拿到的都拿来：视频信息、字幕（或语音转写）、画面截图、弹幕、热评。"""
        v = await self.view(bv)
        aid, cid = int(v.get("aid") or 0), int(v.get("cid") or 0)
        dur = int(v.get("duration") or 0)
        pages = v.get("pages") or []
        ptitle = ""
        if part > 1 and len(pages) >= part:                     # 15.0 多 P：看指定的那一 P
            pg = pages[part - 1]
            cid, dur, ptitle = int(pg.get("cid") or cid), int(pg.get("duration") or dur), str(pg.get("part") or "")
        sub = await self.subtitles(bv, aid, cid) if aid and cid else ""
        heard = "" if sub or not cid else await self.speech(bv, cid, dur)      # 没字幕：听一遍（语音转文字）
        dm = await self.danmaku(cid) if cid else []
        cm = await self.comments(aid, bv) if aid else []
        moments = hot_moments(dm)
        title = v.get("title", "") + (f"（P{part} {ptitle}）" if ptitle else "")
        frames: list[dict] = []
        if n_frames > 0 and cid and dur:
            shots = await self.frames(bv, cid, pick_times(dur, moments, n_frames))
            cover = b""
            if v.get("pic") and part <= 1:
                try:
                    cover = await self.fetch(str(v["pic"]).replace("http://", "https://") + "@480w.jpg", self._h(bv))
                    cover = cover if cover[:2] == b"\xff\xd8" else b""
                except Exception:  # noqa: BLE001
                    cover = b""
            frames = await self.see({"title": title, "author": (v.get("owner") or {}).get("name", "")}, shots, cover)
        return {"bv": bv, "p": part, "title": title, "author": (v.get("owner") or {}).get("name", ""),
                "desc": (v.get("desc") or "").strip()[:500], "duration": mmss(dur) if dur else "",
                "category": v.get("tname", ""), "subtitle": sub, "heard": heard, "moments": moments, "frames": frames,
                "danmaku": common_danmaku(dm), "n_danmaku": len(dm), "comments": cm,
                "views": (v.get("stat") or {}).get("view")}

    async def summarize(self, w: dict) -> dict:
        mat = [f"标题：{w['title']}", f"UP 主：{w['author']}", f"时长：{w['duration']}", f"分区：{w['category']}",
               f"简介：{w['desc'] or '（没有）'}"]
        if w.get("subtitle"):
            mat.append("字幕：\n" + w["subtitle"])
        elif w.get("heard"):
            mat.append("视频里说的话（语音转文字，可能有错字；没有画面信息）：\n" + w["heard"][:6000])
        else:
            mat.append("字幕：（没拿到）")
        if w.get("frames"):
            mat.append("画面（封面和几个时间点的截图，看图模型描述的）：\n" + "\n".join(f"- {f['t']}：{f['see']}" for f in w["frames"]))
        if w.get("moments"):
            mat.append("弹幕最热闹的几段：" + "；".join(f"{m['t']}（{m['n']} 条，刷的是：{'、'.join(m['top'])}）"
                                                for m in w["moments"]))
        if w.get("danmaku"):
            mat.append("刷得最多的弹幕：" + "、".join(w["danmaku"]))
        if w.get("comments"):
            mat.append("热评：\n" + "\n".join(f"- {c}" for c in w["comments"]))
        d = await self.llm.json([{"role": "user", "content": SUMMARY_PROMPT + "\n".join(mat)[:11000]}], default={},
                                role="helper")
        return d if isinstance(d, dict) else {}

    @staticmethod
    def describe(w: dict, s: dict) -> str:
        """给她看的一段（放进聊天记录 / 提示词里）。"""
        full = s.get("seen") == "full" and (w.get("subtitle") or w.get("heard"))
        eyes = bool(w.get("frames"))
        seen = ("看完了" + ("（画面看了几个片段）" if eyes else "（只听了声音，没看画面）")) if full else \
            ("大概看了看（看了几张画面截图和弹幕评论，没听到说了什么）" if eyes else "大概看了看（没听到内容，只看了简介、弹幕和评论）")
        bits = [f"你点开《{w['title']}》{seen}"]
        if s.get("gist"):
            bits.append(f"讲的是：{s['gist']}")
        if s.get("visual"):
            bits.append(f"画面：{s['visual']}")
        pts = [str(x) for x in (s.get("points") or []) if x][:5]
        if pts:
            bits.append("要点：" + "；".join(pts))
        if s.get("highlights"):
            bits.append(f"最热闹的地方：{s['highlights']}")
        if s.get("vibe"):
            bits.append(f"评论区：{s['vibe']}")
        bits = [b.rstrip("。.；; ") for b in bits]
        return "[" + "。".join(bits) + "。聊的时候像自己刚看过一样随口说，没看到的部分别编]"
