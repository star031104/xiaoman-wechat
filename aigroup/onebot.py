"""QQ 接入：通过 OneBot v11（NapCat）收发消息。

    NapCat 登录一个 QQ 小号，开一个“正向 WebSocket 服务器”（这里默认 ws://127.0.0.1:3002），
    这里连上去：收到的群消息交给 chat/ingest.py，发消息走 call()。

    安全上的几条硬规矩（照搬 Estrella Bot 踩过的坑）：
    - 发出去的消息一律用“消息段数组”，不拼 CQ 码字符串：模型就算写出 [CQ:at,qq=all] 也只是普通文字
    - 自己发的消息（user_id == self_id）直接丢掉，免得自己回自己
    - 断线指数退避重连（1 秒 → 30 秒），断线时所有等待中的请求立刻失败，不会挂住
"""
from __future__ import annotations

import asyncio
import base64
import itertools
import json
import logging
import re
import time
from dataclasses import dataclass, field

log = logging.getLogger("aigroup.qq")


class OneBotError(RuntimeError):
    pass


# ---------------- QQ 自带的小黄脸 ----------------
# 只收常用的经典表情（id 多年没变过）；不认识的 id 当“[表情]”
FACES = {
    0: "惊讶", 1: "撇嘴", 2: "色", 3: "发呆", 4: "得意", 5: "流泪", 6: "害羞", 7: "闭嘴", 8: "睡", 9: "大哭",
    10: "尴尬", 11: "发怒", 12: "调皮", 13: "呲牙", 14: "微笑", 15: "难过", 16: "酷", 18: "抓狂", 19: "吐",
    20: "偷笑", 21: "可爱", 22: "白眼", 23: "傲慢", 24: "饥饿", 25: "困", 26: "惊恐", 27: "流汗", 28: "憨笑",
    29: "悠闲", 30: "奋斗", 31: "咒骂", 32: "疑问", 33: "嘘", 34: "晕", 38: "敲打", 39: "再见", 41: "发抖",
    42: "爱情", 49: "拥抱", 53: "蛋糕", 63: "玫瑰", 66: "爱心", 74: "太阳", 75: "月亮", 76: "赞",
    96: "冷汗", 97: "擦汗", 98: "抠鼻", 99: "鼓掌", 100: "糗大了", 101: "坏笑", 104: "哈欠", 106: "委屈",
    107: "快哭了", 109: "亲亲", 111: "可怜", 178: "斜眼笑", 179: "doge", 182: "笑哭", 212: "托腮",
    264: "捂脸", 265: "辣眼睛", 271: "吃瓜", 277: "汪汪",
}
# 她的情绪 → 发哪个小黄脸（没有学到他的表情包时用）
EMOTION_FACE = {
    "开心": 13, "大笑": 182, "喜欢": 66, "害羞": 6, "难过": 15, "委屈": 106, "生气": 11, "哼": 23,
    "惊讶": 0, "无语": 22, "困": 25, "抱抱": 49, "想你": 66, "得意": 4, "哭": 9, "调皮": 12, "疑问": 32,
}


def face_for(emotion: str) -> int | None:
    e = (emotion or "").strip()
    if e in EMOTION_FACE:
        return EMOTION_FACE[e]
    for k, v in EMOTION_FACE.items():
        if k in e or e in k:
            return v
    return None


# ---------------- 消息段 ----------------
def text_seg(t: str) -> dict:
    return {"type": "text", "data": {"text": str(t)}}


def at_seg(qq: int | str) -> dict:
    return {"type": "at", "data": {"qq": str(qq)}}


def reply_seg(mid: int | str) -> dict:
    return {"type": "reply", "data": {"id": str(mid)}}


def image_seg(data: bytes, sticker: bool = False) -> dict:
    d = {"file": "base64://" + base64.b64encode(data).decode()}
    if sticker:                       # 当表情包发（聊天里显示成小尺寸的动画表情）
        d.update(sub_type=1, summary="[动画表情]")
    return {"type": "image", "data": d}


def record_seg(data: bytes) -> dict:
    """13.0 发语音（wav / mp3 的字节；NapCat 自己转成 QQ 的格式）。"""
    return {"type": "record", "data": {"file": "base64://" + base64.b64encode(data).decode()}}


def face_seg(fid: int) -> dict:
    return {"type": "face", "data": {"id": str(fid)}}


@dataclass
class Flat:
    """把一条 QQ 消息拆成大脑看得懂的样子。"""
    text: str = ""                       # 纯文字（@、表情、图片都换成了占位）
    mentioned: bool = False              # 有没有 @ 我
    reply_id: int | None = None          # 引用了哪条消息
    images: list[str] = field(default_factory=list)      # 图片 URL
    record: str | None = None            # 语音（NapCat 给的 file 名）
    record_url: str | None = None        # 13.0 语音的下载地址（有的话）
    ats: list[str] = field(default_factory=list)         # @ 了谁（QQ 号字符串）
    stickers: list[str] = field(default_factory=list)    # 表情包图片的 URL（会被她“偷”来用）
    sticker_files: list[str] = field(default_factory=list)  # 表情包的文件名（多半是 md5，用来认出“同一张”）
    links: list[str] = field(default_factory=list)       # 分享卡片里的链接
    forward_id: str | None = None                        # 合并转发
    poke: bool = False


def flatten(message, self_id: int | None, names: dict[str, str] | None = None) -> Flat:
    """OneBot 的 message（段数组）→ Flat。names: QQ 号 → 显示名，用来把 @某人 写成名字。"""
    f = Flat()
    if isinstance(message, str):          # NapCat 配成了 string 格式：只当纯文字，不解析 CQ 码
        f.text = message
        return f
    parts: list[str] = []
    for seg in message or []:
        t = seg.get("type")
        d = seg.get("data") or {}
        if t == "text":
            parts.append(d.get("text", ""))
        elif t == "at":
            qq = str(d.get("qq", ""))
            if self_id and qq == str(self_id):
                f.mentioned = True
                continue                  # @我 本身不进正文
            f.ats.append(qq)
            parts.append("@全体成员 " if qq == "all" else f"@{(names or {}).get(qq) or d.get('name') or qq} ")
        elif t == "reply":
            try:
                f.reply_id = int(d.get("id"))
            except (TypeError, ValueError):
                pass
        elif t == "image":
            url = d.get("url") or d.get("file") or ""
            sub = str(d.get("summary") or "")
            is_sticker = str(d.get("sub_type", "0")) == "1" or "表情" in sub
            if is_sticker:
                if url:
                    f.stickers.append(url)
                    f.sticker_files.append(str(d.get("file") or ""))
                parts.append("[表情包]")
            else:
                if url:
                    f.images.append(url)
                parts.append("[图片]")
        elif t == "face":
            try:
                name = FACES.get(int(d.get("id")))
            except (TypeError, ValueError):
                name = None
            parts.append(f"[表情:{name}]" if name else "[表情]")
        elif t == "mface":
            if d.get("url"):
                f.stickers.append(str(d["url"]))
                f.sticker_files.append(str(d.get("emoji_id") or d.get("key") or ""))
            parts.append(f"[表情包{(':' + d['summary'].strip('[]')) if d.get('summary') else ''}]")
        elif t == "record":
            f.record = d.get("file") or d.get("path") or d.get("url")
            f.record_url = d.get("url") if str(d.get("url") or "").startswith("http") else None
            parts.append("[语音]")
        elif t == "video":
            parts.append("[视频]")
        elif t == "file":
            parts.append(f"[文件:{d.get('name') or d.get('file') or ''}]")
        elif t == "forward":
            f.forward_id = str(d.get("id") or "") or None
            parts.append("[合并转发]")
        elif t == "json":
            title, desc, url = card_info(d.get("data"))
            if url:
                f.links.append(url)
            parts.append(f"[分享：{title}{(' — ' + desc) if desc and desc != title else ''}]" if title else "[卡片消息]")
            if url:
                parts.append(f" {url}")
        elif t == "poke":
            f.poke = True
            parts.append("[戳一戳]")
        elif t in ("dice", "rps"):
            parts.append({"poke": "[戳一戳]", "dice": "[骰子]", "rps": "[猜拳]"}[t])
        elif t == "markdown":
            parts.append(str(d.get("content") or ""))
    f.text = re.sub(r"[ \t]+", " ", "".join(parts)).strip()
    return f


def card_info(raw) -> tuple[str, str, str]:
    """QQ 分享卡片（B 站、小程序、链接分享……）→ (标题, 简介, 链接)。认不出的返回空。"""
    try:
        j = json.loads(raw) if isinstance(raw, str) else (raw or {})
    except ValueError:
        return "", "", ""
    meta = j.get("meta") or {}
    title = desc = url = ""
    for v in meta.values():
        if not isinstance(v, dict):
            continue
        title = title or str(v.get("title") or "")
        desc = desc or str(v.get("desc") or "")
        url = url or str(v.get("qqdocurl") or v.get("jumpUrl") or v.get("url") or "")
    title = title or str(j.get("prompt") or "").strip("[]")
    if url and not url.startswith(("http://", "https://")):
        url = ""
    return title[:60], desc[:80], url


# ---------------- 客户端 ----------------
class OneBot:
    """OneBot v11 正向 WebSocket 客户端。"""

    def __init__(self, url: str, token: str = "", timeout: float = 20.0):
        self.url, self.token, self.timeout = url, token or "", timeout
        self.self_id: int | None = None
        self.handlers: list = []           # async def h(event: dict)
        self.on_connect: list = []         # async def h()
        self._ws = None
        self._pending: dict[str, asyncio.Future] = {}
        self._seq = itertools.count(1)
        self._stop = False
        self._task: asyncio.Task | None = None
        self._bg: set[asyncio.Task] = set()
        self.connected = asyncio.Event()

    @property
    def ready(self) -> bool:
        return self._ws is not None and not self._ws.closed and self.connected.is_set()

    def start(self):
        self._task = asyncio.create_task(self._run())

    async def stop(self):
        self._stop = True
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:  # noqa: BLE001
                pass
        if self._task:
            self._task.cancel()

    async def _run(self):
        import aiohttp
        backoff = 1.0
        warned = False
        while not self._stop:
            try:
                headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
                async with aiohttp.ClientSession() as s:
                    async with s.ws_connect(self.url, headers=headers, heartbeat=30, max_msg_size=0) as ws:
                        self._ws = ws
                        self.connected.set()
                        backoff, warned = 1.0, False
                        log.info("QQ 已连上 NapCat（%s）", self.url)
                        self._spawn(self._after_connect())
                        async for msg in ws:
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                self._dispatch(msg.data)
                            elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED):
                                break
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                if not warned:
                    log.warning("连不上 NapCat（%s）：%s —— 先把 NapCat 打开、确认开了 WebSocket 服务器；会一直自动重试",
                                self.url, e)
                    warned = True
            finally:
                self.connected.clear()
                self._ws = None
                for fut in self._pending.values():
                    if not fut.done():
                        fut.set_exception(OneBotError("和 NapCat 的连接断了"))
                self._pending.clear()
            if self._stop:
                break
            await asyncio.sleep(backoff)
            backoff = min(30.0, backoff * 2)

    async def _after_connect(self):
        try:
            info = await self.call("get_login_info")
            self.self_id = int(info.get("user_id") or 0) or self.self_id
            log.info("QQ 账号：%s（%s）", info.get("nickname"), self.self_id)
        except Exception as e:  # noqa: BLE001
            log.warning("取 QQ 登录信息失败：%s", e)
        for h in self.on_connect:
            try:
                await h()
            except Exception as e:  # noqa: BLE001
                log.warning("QQ 连接后的初始化失败：%s", e)

    def _spawn(self, coro):
        t = asyncio.create_task(coro)
        self._bg.add(t)
        t.add_done_callback(self._bg.discard)

    def _dispatch(self, raw: str):
        try:
            data = json.loads(raw)
        except ValueError:
            return
        echo = data.get("echo")
        if echo is not None and str(echo) in self._pending:
            fut = self._pending.pop(str(echo))
            if not fut.done():
                fut.set_result(data)
            return
        if "post_type" not in data:
            return
        if data.get("self_id"):
            self.self_id = int(data["self_id"])
        if data.get("post_type") == "meta_event":
            return
        # 自己发的消息（message_sent 或者 user_id 就是自己）一律不处理
        if data.get("post_type") == "message_sent":
            return
        if data.get("post_type") == "message" and self.self_id and int(data.get("user_id") or 0) == self.self_id:
            return
        for h in self.handlers:
            self._spawn(self._safe(h, data))

    @staticmethod
    async def _safe(h, data):
        try:
            await h(data)
        except Exception as e:  # noqa: BLE001
            log.exception("处理 QQ 事件失败：%s", e)

    async def call(self, action: str, **params) -> dict:
        if not self.ready:
            raise OneBotError("还没连上 NapCat")
        echo = f"aigroup-{next(self._seq)}"
        fut = asyncio.get_running_loop().create_future()
        self._pending[echo] = fut
        try:
            await self._ws.send_str(json.dumps({"action": action, "params": params, "echo": echo}, ensure_ascii=False))
            data = await asyncio.wait_for(fut, self.timeout)
        finally:
            self._pending.pop(echo, None)
        if data.get("status") not in ("ok", "async") or int(data.get("retcode") or 0) != 0:
            raise OneBotError(f"{action} 失败：retcode={data.get('retcode')} {data.get('message') or data.get('wording') or ''}")
        return data.get("data") or {}

    # 常用动作
    async def send_private(self, user_id: int, segs: list[dict]) -> int | None:
        d = await self.call("send_private_msg", user_id=int(user_id), message=segs)
        return _mid(d)

    async def send_group(self, group_id: int, segs: list[dict]) -> int | None:
        d = await self.call("send_group_msg", group_id=int(group_id), message=segs)
        return _mid(d)


def _mid(d: dict) -> int | None:
    try:
        return int(d.get("message_id"))
    except (TypeError, ValueError):
        return None


async def send_once(url: str, token: str, action: str, params: dict, timeout: float = 4.0) -> dict:
    """单独开一条连接发一个请求就关（程序退出时道别用：主连接可能已经跟着事件循环停了）。"""
    import aiohttp
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as s:
        async with s.ws_connect(url, headers=headers) as ws:
            await ws.send_str(json.dumps({"action": action, "params": params, "echo": "once"}, ensure_ascii=False))
            async for msg in ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    break
                d = json.loads(msg.data)
                if d.get("echo") == "once":
                    if d.get("status") != "ok":
                        raise OneBotError(f"{action} 失败：{d.get('message') or d.get('retcode')}")
                    return d.get("data") or {}
    raise OneBotError("连接断了")


def send_once_sync(url: str, token: str, action: str, params: dict, timeout: float = 4.0) -> dict:
    """同步版：在新线程里跑（调用方可能正处在一个事件循环里，也可能循环已经停了）。"""
    import threading
    box: dict = {}

    def run():
        try:
            box["ok"] = asyncio.run(send_once(url, token, action, params, timeout))
        except Exception as e:  # noqa: BLE001
            box["err"] = e
    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout + 1)
    if "ok" in box:
        return box["ok"]
    raise box.get("err") or OneBotError("超时")


async def fetch_bytes(url: str, limit: int = 8 * 1024 * 1024, timeout: float = 20) -> bytes | None:
    """下载 QQ 图片（腾讯的图床地址，国内直连，不走代理）。"""
    if not url.startswith(("http://", "https://")):
        return None
    import aiohttp
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as s:
            async with s.get(url) as r:
                if r.status != 200:
                    return None
                # 17.1 content.read(n) 只给“已经到了的”那一块，大一点的图会被截断（存下来的表情包只有开头 3KB）：读到头为止
                buf = bytearray()
                async for chunk in r.content.iter_chunked(64 * 1024):
                    buf += chunk
                    if len(buf) > limit:
                        return None
                want = r.content_length
                if want is not None and len(buf) != want:
                    return None
                return bytes(buf)
    except Exception as e:  # noqa: BLE001
        log.info("下载 QQ 图片失败：%s", e)
        return None


def now_ts() -> float:
    return time.time()
