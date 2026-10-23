"""每个群的运行时状态（只在内存里，重启就没了；要持久的都在数据库）。"""
from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field

from aigroup.chat.gate import Attention, Backoff, Energy


@dataclass
class GroupState:
    last_msg_ts: float = 0.0       # 这一批之前群里最后一条消息的时间
    last_reply_ts: float = 0.0     # 她上一次在这个群说话
    replies: deque = field(default_factory=lambda: deque(maxlen=50))
    pending: asyncio.Task | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    batch_mentioned: bool = False
    batch_owner: bool = False
    batch_trigger: int | None = None      # 被叫到的那条（回复时引用它）
    batch_who: str = ""
    batch_callers: list = field(default_factory=list)   # 5.0 这一批里叫她的人：[(名字, 他说的)]
    batch_guess: str = ""
    about_ts: float = 0.0          # 5.0 上次因为“有人在背后说她”而开口的时间（10 分钟最多一次）          # 5.0 这一批里有没 @ 她、但多半是冲她说的（说话人的名字）
    batch_start_prev_ts: float = 0.0
    batch_texts: list = field(default_factory=list)   # [(消息 id, QQ 号, 文字, {at_other, reply_other, sticker})]
    name: str = ""
    last_repeat: str = ""          # 她上次跟着复读的那句（同一轮复读只跟一次）
    mentioned_since: float = 0.0   # 被叫到、还没回的时候从什么时候开始等
    mention_due: float = 0.0       # 18.1 被叫到后定下的回复时刻：后来的新消息只合进这一批，不再往后推
    speaking: bool = False         # 正在“说”（问大模型、打字、发）：这时候来的新消息不打断她
    to_her_ts: float = 0.0         # 最近一次有人直接找她的时间（说到一半有人找她，剩下的就不发了）
    wait: dict | None = None       # 她在等谁开口：{"uid", "until"}
    last_wait: float = 0.0          # 21.1 这次回话前等了几秒（看到消息的延迟）
    talk_note: str = ""             # 21.1 有人让她少说点 / 多说点：下一次回话时给她的一句提示
    pitch: dict | None = None      # 21.0 她想说、但觉得“再等等”的一句：{"say", "ts", "last_id", "think", "why"}
    attention: Attention = field(default_factory=Attention)
    energy: Energy = field(default_factory=Energy)
    backoff: Backoff = field(default_factory=Backoff)
