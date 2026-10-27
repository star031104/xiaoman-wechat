"""微信接入：对上层（chat/）装成和 NapCat 一样的 OneBot 客户端，对下读本地库收消息、用窗口操作发消息。

    - 收：每秒看一眼消息库变没变，变了就把白名单群里的新消息转成 OneBot 格式的事件交给 handlers
    - 补：get_group_msg_history 直接从本地库里取（她没开的时候群里说的话库里都有），chat/ingest.py 的 catch_up 用它
    - 发：send_group 把消息段拼成文字（@ 写成“@名字”，小黄脸写成微信的 [捂脸] 这种），交给 wxui 粘贴发送，
      然后在库里找到自己刚发的那条才算成功；两次发送之间至少隔 send_gap_seconds，每小时最多 max_sends_per_hour 条
    - 自己的号在手机上说的话（不是她发的）：交给 on_self_message，记成“她说的”
    - 微信关了 / 重启 / 更新：connected 先放下，微信回来后重新接上，再跑一遍 on_connect（拉成员、补消息）
    - 微信号、群号是字符串，上层全是整数：data/wechat/ids.db 里给每个 wxid 分配一个固定的数字编号
"""
from __future__ import annotations

import asyncio
import base64
import collections
import logging
import os
import re
import sqlite3
import tempfile
import threading
import time
from difflib import SequenceMatcher
from pathlib import Path

from aigroup.wechat.wxdb import WeChatData, WxMsg, find_data_root, list_accounts

log = logging.getLogger("aigroup.wechat")

# QQ 小黄脸的名字 → 微信表情的写法（粘进输入框会变成表情）。没有对应的就不发
WX_FACES = set("微笑 撇嘴 色 发呆 得意 流泪 害羞 闭嘴 睡 大哭 尴尬 发怒 调皮 呲牙 惊讶 难过 囧 抓狂 吐 偷笑 愉快 白眼 傲慢 "
               "困 惊恐 憨笑 悠闲 咒骂 疑问 嘘 晕 衰 骷髅 敲打 再见 擦汗 抠鼻 鼓掌 坏笑 右哼哼 鄙视 委屈 快哭了 阴险 亲亲 "
               "可怜 笑脸 生病 脸红 破涕为笑 恐惧 失望 无语 嘿哈 捂脸 奸笑 机智 皱眉 耶 吃瓜 加油 汗 天啊 Emm 社会社会 "
               "旺柴 好的 打脸 哇 翻白眼 666 让我看看 叹气 苦涩 裂开 嘴唇 爱心 心碎 拥抱 强 弱 握手 胜利 抱拳 勾引 拳头 OK "
               "合十 啤酒 咖啡 蛋糕 玫瑰 凋谢 菜刀 炸弹 便便 月亮 太阳 庆祝 礼物 红包 發 福 烟花 爆竹 猪头 跳跳 发抖 转圈".split())
FACE_ALIAS = {"可爱": "愉快", "笑哭": "破涕为笑", "斜眼笑": "奸笑", "doge": "旺柴", "赞": "强", "汪汪": "旺柴",
              "托腮": "皱眉", "辣眼睛": "翻白眼", "惊恐": "惊恐", "爱情": "爱心", "糗大了": "囧", "哈欠": "困",
              "冷汗": "汗", "饥饿": "可怜", "奋斗": "加油"}


def wx_face(name: str) -> str:
    n = FACE_ALIAS.get(name, name)
    return f"[{n}]" if n in WX_FACES else ""


class OneBotError(RuntimeError):
    pass


class IdMap:
    """wxid ↔ 固定的整数编号（从 10001 开始）。"""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.execute("CREATE TABLE IF NOT EXISTS ids(id INTEGER PRIMARY KEY AUTOINCREMENT, wxid TEXT UNIQUE)")
        if not self.conn.execute("SELECT 1 FROM sqlite_sequence WHERE name='ids'").fetchone():
            self.conn.execute("INSERT INTO sqlite_sequence(name, seq) VALUES ('ids', 10000)")
        self.conn.commit()
        self.lock = threading.Lock()
        self._fw: dict[str, int] = {}
        self._bw: dict[int, str] = {}

    def id(self, wxid: str) -> int:
        wxid = str(wxid or "").strip()
        if not wxid:
            return 0
        v = self._fw.get(wxid)
        if v:
            return v
        with self.lock:
            r = self.conn.execute("SELECT id FROM ids WHERE wxid=?", (wxid,)).fetchone()
            if not r:
                self.conn.execute("INSERT INTO ids(wxid) VALUES (?)", (wxid,))
                self.conn.commit()
                r = self.conn.execute("SELECT id FROM ids WHERE wxid=?", (wxid,)).fetchone()
        self._fw[wxid], self._bw[int(r[0])] = int(r[0]), wxid
        return int(r[0])

    def wxid(self, i) -> str:
        try:
            i = int(i)
        except (TypeError, ValueError):
            return ""
        v = self._bw.get(i)
        if v:
            return v
        with self.lock:
            r = self.conn.execute("SELECT wxid FROM ids WHERE id=?", (i,)).fetchone()
        if r:
            self._bw[i], self._fw[r[0]] = r[0], i
            return r[0]
        return ""


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s or "")


def similar(a: str, b: str) -> bool:
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return False
    return a == b or SequenceMatcher(None, a, b).ratio() >= 0.85


_HELD: dict[str, object] = {}                 # 自己这份解密副本目录的锁（进程活着就一直拿着）


def _try_lock(path: Path):
    """非阻塞拿文件锁：拿到返回打开的文件，别人拿着返回 None。"""
    import sys
    f = open(path, "a+")
    try:
        if sys.platform == "win32":
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    return f


def work_dir(root: Path, account: str) -> Path:
    """解密副本放的地方：每个进程一份（体检、wxtail 和正在跑的她各用各的，互不覆盖），目录里有把锁；
    别的进程留下的（锁没人拿着＝那个进程已经退出）顺手清掉。"""
    import shutil
    base = Path(root) / "data" / "wechat" / "cache" / account
    base.mkdir(parents=True, exist_ok=True)
    mine = base / f"p{os.getpid()}"
    for d in base.iterdir():
        try:
            if d.is_dir() and d.name.startswith("p") and d != mine:
                lk = _try_lock(d / "lock")
                if lk is not None:                            # 没人用了
                    lk.close()
                    shutil.rmtree(d, ignore_errors=True)
            elif d.is_file() and (d.name.endswith(".base") or d.name.endswith(".plain.db")):
                d.unlink(missing_ok=True)                     # 1.x 留下的
        except OSError:
            continue
    mine.mkdir(parents=True, exist_ok=True)
    key = str(mine)
    if key not in _HELD:
        lk = _try_lock(mine / "lock")
        if lk is not None:
            _HELD[key] = lk
    return mine


def open_data(opts: dict, root: Path, progress=None, deep: bool = False) -> tuple[WeChatData | None, list[str]]:
    """找数据目录 → 选账号 → 拿密钥 → 开库。返回 (库, 说明)。"""
    from aigroup.wechat import linux
    from aigroup.wechat.keys import get_keys
    if linux.wanted(opts):
        linux.enable(opts)
    notes: list[str] = []
    data_root = find_data_root(str(opts.get("data_dir") or ""))
    if not data_root:
        return None, ["没找到微信的数据目录（xwechat_files）：微信装好、登录过吗？也可以在 config.yaml 的 wechat.data_dir 里手填"]
    accs = list_accounts(data_root)
    want = str(opts.get("account") or "").strip()
    if want:
        accs = [a for a in accs if a["account"] == want or a["wxid"] == want]
        if not accs:                                          # 配了账号就只认它：不悄悄换成别的号
            return None, [f"{data_root} 下面没有 config 里写的账号 {want}（换了号的话重新跑一次 setup）"]
    if not accs:
        return None, [f"{data_root} 下面没有账号目录"]
    cache = Path(root) / "data" / "wechat" / "keys.json"
    for a in accs[:3]:
        keys, n = get_keys(a["dir"], a["account"], cache, deep=deep, progress=progress)
        notes += [f"[{a['account']}] {x}" for x in n]
        d = WeChatData(a["dir"], keys, work_dir(Path(root), a["account"]), a["wxid"])
        if not d.missing_keys():
            return d, notes
        if want:
            break
    return None, notes


def accounts_here(opts: dict) -> list[dict]:
    """这台电脑上的微信号（最近用过的在前），每个带 logged：现在有没有哪个开着的微信登录着它（None = 认不出）。"""
    from aigroup.wechat.keys import owner_pids, weixin_procs
    data_root = find_data_root(str(opts.get("data_dir") or ""))
    if not data_root:
        return []
    accs = list_accounts(data_root)
    procs = weixin_procs()
    for a in accs:
        own = owner_pids(a["dir"], procs) if procs else []
        a["logged"] = None if own is None else bool(own)
    return accs


def forget_account(root: Path, account: str) -> None:
    """配置向导里为了认昵称读过、但没选的号（比如你的大号）：密钥和解密副本都删掉，不留在她的数据里。"""
    import json
    import shutil
    kp = Path(root) / "data" / "wechat" / "keys.json"
    try:
        d = json.loads(kp.read_text(encoding="utf-8"))
        if account in d:
            d.pop(account)
            kp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    except (OSError, ValueError):
        pass
    shutil.rmtree(Path(root) / "data" / "wechat" / "cache" / account, ignore_errors=True)


def _running() -> bool | None:
    """微信在不在运行（Windows / Linux 以外返回 None＝不知道）。"""
    from aigroup.wechat import linux
    if not linux.active():
        return None
    from aigroup.wechat.keys import weixin_pids
    return bool(weixin_pids())


def send_problem(d, chat: str, titles: dict | None = None) -> str:
    """这个群现在发不了的原因（空 = 能发）：没有群名、和别的会话同名。setup / check / wxdiag / 发送前都用它。"""
    from aigroup.wechat.wxui import title_for
    name = d.room_name(chat)
    if not name or name == chat:
        return f"群 {chat} 还没有群名（窗口标题是一串成员名，认不出来）：在微信里给群起个名字（群聊名称）"
    title = title_for(titles or {}, chat, name)
    twins = d.same_name(chat)
    if twins and title == name:
        return (f"「{name}」和别的会话（{'、'.join(twins[:3])}）同名，分不清是哪个窗口：给群改个名字"
                f"（或者在 config.yaml 的 wechat.window_titles 里按群 id 写一个不同的窗口标题）")
    return ""


class WeChatBot:
    can_react = False                     # 微信不能给消息点表情（chat/ 据此不让大模型选“点表情”）
    can_quote = False                     # 也不能引用回复（reply 段直接丢掉）
    can_voice = False                     # 13.0 也不能发语音（窗口操作发不了语音文件）
    history_by_id = True                  # 补消息时只按消息 id 去重（历史就是本地库，id 稳定）
    prompt_hint = ("\n- 微信的小表情可以直接写成 [捂脸]、[旺柴]、[破涕为笑]、[裂开]、[奸笑] 这种，粘进去就是表情"
                   "（偶尔用，一条里最多一个）。")

    def __init__(self, opts: dict, root: Path):
        self.opts = dict(opts or {})
        self.root = Path(root)
        self.ids = IdMap(self.root / "data" / "wechat" / "ids.db")
        self.groups = [str(g).strip() for g in self.opts.get("groups") or [] if str(g).strip()]
        self.interval = float(self.opts.get("poll_seconds", 1.0))
        self.confirm_timeout = float(self.opts.get("confirm_seconds", 10.0))
        self.send_gap = float(self.opts.get("send_gap_seconds", 1.2))
        self.max_per_hour = int(self.opts.get("max_sends_per_hour", 80))
        self.running_check = self._her_running
        from aigroup.wechat import linux, wxui
        if linux.wanted(self.opts):                            # 服务器上的 Linux 微信（Docker 里），见 wechat/linux.py
            linux.enable(self.opts)
        self.sender = wxui.Sender(self.opts)
        self.sender.own = self.own_pids                        # 开了两个微信（大号 + 小号）：只用她那个微信的窗口
        self._own: tuple[float, list[int] | None] = (-1e9, None)
        self.self_id: int | None = None
        self.self_wxid = ""
        self.nick = ""
        self.handlers: list = []
        self.on_connect: list = []
        self.on_self_message: list = []            # async def h(event)：自己的号在别处（手机）说的话
        self.on_backlog: list = []                 # async def h()：库里冒出一批旧消息（微信刚开、同步离线消息）
        self.stale_seconds = float(self.opts.get("stale_seconds", 600))
        self._backlog = False
        self.connected = asyncio.Event()
        self.data: WeChatData | None = None
        self.notes: list[str] = []
        self.wm: dict[str, int] = {}
        self.members: dict[str, set[str]] = {}
        self._self_seen: collections.deque = collections.deque(maxlen=80)
        self._seen_ids: collections.deque = collections.deque(maxlen=3000)
        self._inflight: list[list] = []            # 正在发 / 刚发的 [群, 种类, 内容, 开始时间, 确认失败]
        self._sent_times: collections.deque = collections.deque(maxlen=500)
        self._skews: collections.deque = collections.deque(maxlen=7)     # 电脑时钟比微信的消息时间快多少秒（拿她自己发的消息量）
        self._skew_warned = False
        self._win_ok: dict[str, bool] = {}
        self._last_send = 0.0
        self._send_lock = asyncio.Lock()
        self._dlock = threading.Lock()             # 读库只在一个线程里做
        self._task: asyncio.Task | None = None
        self._bg: set = set()
        self._stop = False
        self._kick = asyncio.Event()
        self._last_contacts = 0.0
        self._last_keys = 0.0
        self._key_backoff = 300.0
        self._key_thread: threading.Thread | None = None
        self._last_alive = 0.0
        self._errors = 0
        self.alive: bool | None = None
        self.search_fail = 0
        self.last_error = ""
        self.last_how = ""
        self.stats = {"recv": 0, "sent": 0, "send_fail": 0, "own": 0, "reconnects": 0}

    # ---------------- 状态 ----------------
    @property
    def ready(self) -> bool:
        return self.data is not None and self.connected.is_set()

    def gid(self, chat: str) -> int:
        return self.ids.id(chat)

    def chat_of(self, gid) -> str:
        return self.ids.wxid(gid)

    def status(self) -> dict:
        """给小屋看的：接没接上、每个群发消息用什么方式、发了多少。"""
        d = self.data
        out = {"ready": self.ready, "running": self.alive, "self_wxid": self.self_wxid, "nick": self.nick,
               "account": d.account_dir.name if d else "", "error": "" if self.ready else self.last_error,
               "last_error": self.last_error, "last_how": self.last_how, "stats": dict(self.stats),
               "hour_sends": self._hour_count(), "max_per_hour": self.max_per_hour,
               "missing_keys": d.missing_keys() if d else [], "search": bool(self.sender.search_fallback),
               "groups": []}
        try:
            ws = self._windows()
        except Exception:  # noqa: BLE001
            ws = []
        own = self.own_pids()
        out["pid"] = own[0] if own else None
        out["not_logged_in"] = own == []
        for g in self.groups:
            name = d.room_name(g) if d else g
            win = None
            try:
                from aigroup.wechat import wxui
                win = wxui.chat_window(wxui.title_for(self.sender.titles, g, name), ws) if ws else None
            except Exception:  # noqa: BLE001
                pass
            out["groups"].append({"id": self.gid(g), "chat": g, "name": name, "window": bool(win),
                                  "problem": self.send_problem(g) if d else "",
                                  "ambiguous": (d.ambiguous(g)[:3] if d else []),
                                  "members": len(d.members(g)) if d else 0})
        return out

    # ---------------- 启停 ----------------
    def start(self):
        self._task = asyncio.create_task(self._run())

    async def stop(self):
        self._stop = True
        if self._task:
            self._task.cancel()
        if self.data:
            self.data.close()

    async def _connected(self):
        self.connected.set()
        log.info("微信已接上：%s（%s），群 %s", self.nick or "?", self.self_wxid,
                 [self.data.room_name(g) for g in self.groups])
        for h in self.on_connect:
            try:
                await h()
            except Exception as e:  # noqa: BLE001
                log.warning("微信接上后的初始化失败：%s", e)

    async def _run(self):
        warned = ""
        while not self._stop:
            if self.data is None:
                try:
                    await asyncio.to_thread(self._init_blocking)
                    warned = ""
                    self.connected.set()                      # 先立起来：免得下面的“微信在不在”检查又接一遍
                    self._spawn(self._connected())
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001
                    msg = str(e)
                    self.last_error = msg
                    if msg != warned:
                        log.warning("微信还没接上：%s（30 秒后再试；python run.py wxdiag 可以看详细情况）", msg)
                        warned = msg
                    await asyncio.sleep(30)
                    continue
            await self._check_alive()
            try:
                events = await asyncio.to_thread(self._poll_locked)
                self._errors = 0
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self._errors += 1
                log.info("读微信消息出错（第 %d 次）：%s", self._errors, e)
                events = []
                if self._errors >= 10:                      # 一直读不了：从头再接一次（换了账号、库被挪了……）
                    log.warning("读微信的库连续出错，重新接一次")
                    self._reset()
                await asyncio.sleep(3)
            if self._backlog:
                self._backlog = False
                for h in self.on_backlog:
                    self._spawn(self._safe_call(h))
            for ev in events:
                hs = self.on_self_message if ev.get("post_type") == "message_sent" else self.handlers
                for h in hs:
                    self._spawn(self._safe(h, ev))
            try:
                await asyncio.wait_for(self._kick.wait(), self.interval)
            except asyncio.TimeoutError:
                pass
            self._kick.clear()

    def _reset(self):
        self.connected.clear()
        if self.data:
            try:
                self.data.close()
            except Exception:  # noqa: BLE001
                pass
        self.data = None
        self.stats["reconnects"] += 1

    def own_pids(self, max_age: float = 20.0) -> list[int] | None:
        """登录着她的号的那个微信的进程（主进程在前）；None = 认不出（不是 Windows / 系统接口不能用）；[] = 她的号没登录。"""
        from aigroup.wechat import linux
        if not linux.active() or self.data is None:
            return None
        t, v = self._own
        if time.monotonic() - t < max_age:
            return v
        from aigroup.wechat.keys import owner_pids
        try:
            v = owner_pids(self.data.account_dir)
        except Exception as e:  # noqa: BLE001
            log.debug("认她的微信进程失败：%s", e)
            v = None
        if v != self._own[1] and v is not None:
            if v:
                log.info("她的微信：进程 %s", v[0])
            else:
                log.warning("她的号没在这台电脑的微信里登录（开着的可能是别的号）：先不说话")
        self._own = (time.monotonic(), v)
        return v

    def _her_running(self) -> bool | None:
        """她的号的微信在不在运行：开了两个微信时只看她那个（大号开着、小号关了 = 没在运行）。"""
        r = _running()
        if not r:
            return r
        own = self.own_pids(0)
        return r if own is None else bool(own)

    def _windows(self):
        from aigroup.wechat import wxui
        own = self.own_pids()
        return wxui.windows(own) if own else ([] if own == [] else wxui.windows())

    async def _check_alive(self):
        """每 30 秒看一眼微信还开着没。关了：先不说话；开回来：重新跑一遍 on_connect（拉成员、补消息）。"""
        if time.monotonic() - self._last_alive < 30:
            return
        self._last_alive = time.monotonic()
        try:
            alive = await asyncio.to_thread(self.running_check)
        except Exception:  # noqa: BLE001
            alive = None
        was = self.alive
        self.alive = alive
        if alive and self.data is not None:
            try:
                await asyncio.to_thread(self._check_windows)
            except Exception as e:  # noqa: BLE001
                log.debug("看单独窗口失败：%s", e)
        if alive is False and self.connected.is_set():
            self.connected.clear()
            self.last_error = "微信没在运行"
            log.warning("微信关掉了：她先不说话，微信开回来会自动接上")
        elif alive and self.data is not None and not self.connected.is_set() and was is not True:
            log.info("微信又开起来了")
            self.stats["reconnects"] += 1
            self.connected.set()
            self._spawn(self._connected())

    def _check_windows(self):
        """哪个群的单独窗口不见了（微信重启 / 更新以后会关掉）：在日志里说一声，窗口回来了也说一声。"""
        from aigroup.wechat import linux, wxui
        if not linux.active():
            return
        ws = self._windows()
        for g in self.groups:
            name = self.data.room_name(g)
            ok = bool(wxui.chat_window(wxui.title_for(self.sender.titles, g, name), ws))
            if self._win_ok.get(g, True) and not ok:
                log.warning("「%s」的单独窗口不见了（微信重启过？）：在微信里双击这个群重新打开，不然她发不了消息", name)
            elif not self._win_ok.get(g, True) and ok:
                log.info("「%s」的单独窗口回来了", name)
            self._win_ok[g] = ok

    def _spawn(self, coro):
        t = asyncio.create_task(coro)
        self._bg.add(t)
        t.add_done_callback(self._bg.discard)
        return t

    @staticmethod
    async def _safe_call(h):
        try:
            await h()
        except Exception as e:  # noqa: BLE001
            log.info("补消息失败：%s", e)

    @staticmethod
    async def _safe(h, ev):
        try:
            await h(ev)
        except Exception as e:  # noqa: BLE001
            log.exception("处理微信消息失败：%s", e)

    def _init_blocking(self):
        d, notes = open_data(self.opts, self.root)
        self.notes = notes
        if d is None:
            raise OneBotError("；".join(notes[-3:]) or "打不开微信的库")
        d.refresh_all()
        d.load_contacts(force=True)
        self.data = d
        self.self_wxid = d.self_wxid
        self.self_id = self.ids.id(d.self_wxid)
        self.nick = (d.contacts.get(d.self_wxid) or {}).get("nick") or ""
        for g in self.groups:
            self.gid(g)
            self.wm[g] = d.max_seq(g)                         # 从现在开始听；之前的由 catch_up 从库里补
            self.members[g] = set(d.members(g))
            if g not in d.rooms:
                log.warning("白名单里的 %s 不是她在的群（群 id 写错了？她被移出群了？）", g)
            elif not d.tables_for(g):
                log.info("群 %s（%s）在库里还没有消息记录：有人说话后就能收到", d.room_name(g), g)
        self._last_contacts = time.monotonic()

    def _poll_locked(self) -> list[dict]:
        with self._dlock:
            return self._poll_blocking()

    def _ours(self, m: WxMsg) -> bool:
        """这条自己的消息是不是她正在发 / 刚发的（不是的话就是在手机上用这个号说的）。"""
        for chat, kind, content, _t0, failed in list(self._inflight):
            if chat != m.chat:
                continue
            hit = (kind == "text" and similar(m.text, content)) or (kind == "file" and m.type in (3, 47, 49, 43, 11000))
            if hit:
                return not failed                 # 确认超时以后才冒出来：程序已经当它没发出去，交给 message_sent 记下
        return False

    def _poll_blocking(self) -> list[dict]:
        """读一轮。水位线（每个群看到哪了）和事件一起在最后交出去：中途出错，这一轮整个重来，不会丢消息。"""
        d = self.data
        events: list[dict] = []
        changed = d.refresh_all()
        if d.missing_keys():
            self._fetch_keys_later(d)
        wm = dict(self.wm)
        seen, selfs, backlog = [], [], False
        stats = {"recv": 0, "own": 0}
        if changed:
            for g in self.groups:
                for m in d.new_messages(g, wm.get(g, 0)):
                    wm[g] = max(wm.get(g, 0), m.seq)
                    key = (g, m.local_id, m.shard)
                    if key in self._seen_ids or key in seen:
                        continue
                    seen.append(key)
                    if m.ts and time.time() - self.skew() - m.ts > self.stale_seconds:
                        # 微信刚开时同步下来的离线消息：不当成刚说的话一条条接，交给 catch_up 补进记录
                        backlog = True
                        continue
                    if m.sender == self.self_wxid:
                        selfs.append(m)
                        if not self._ours(m) and m.text:
                            ev = self.to_event(m)
                            if ev:
                                ev["post_type"] = "message_sent"
                                stats["own"] += 1
                                events.append(ev)
                        continue
                    ev = self.to_event(m)
                    if ev:
                        stats["recv"] += 1
                        events.append(ev)
        # 到这里都没出错：一起生效
        self.wm = wm
        self._seen_ids.extend(seen)
        self._self_seen.extend(selfs)
        self._backlog = self._backlog or backlog
        for k, v in stats.items():
            self.stats[k] += v
        if time.monotonic() - self._last_contacts > 60:
            self._last_contacts = time.monotonic()
            try:
                events += self._member_changes(d)
            except Exception as e:  # noqa: BLE001
                log.debug("读通讯录失败（下分钟再试）：%s", e)
        return events

    def _member_changes(self, d) -> list[dict]:
        out = []
        if d.load_contacts():
            for g in self.groups:
                now = set(d.members(g))
                old = self.members.get(g)
                if old:
                    for u in sorted(now - old):
                        if u != self.self_wxid:
                            out.append({"post_type": "notice", "notice_type": "group_increase",
                                        "group_id": self.gid(g), "user_id": self.ids.id(u)})
                if now:
                    self.members[g] = now
        return out

    def _fetch_keys_later(self, d):
        """新分片（message_1.db…）没密钥：另开一个线程去找（扫内存可能要几十秒，不能卡住收消息），找不到就越隔越久再试。"""
        if self._key_thread and self._key_thread.is_alive():
            return
        if time.monotonic() - self._last_keys < self._key_backoff:
            return
        self._last_keys = time.monotonic()

        def work():
            from aigroup.wechat.keys import get_keys
            try:
                keys, _ = get_keys(d.account_dir, d.account_dir.name, self.root / "data" / "wechat" / "keys.json")
            except Exception as e:  # noqa: BLE001
                log.info("补拿密钥出错：%s", e)
                keys = {}
            with self._dlock:
                d.keys.update(keys)
                miss = d.missing_keys()
            if miss:
                self._key_backoff = min(3600.0, self._key_backoff * 2)
                log.warning("有新的消息库还没拿到密钥：%s（%d 分钟后再试；也可以 python run.py wxkeys --deep）",
                            miss, int(self._key_backoff // 60))
            else:
                self._key_backoff = 300.0
                log.info("新的消息库拿到密钥了")
        self._key_thread = threading.Thread(target=work, daemon=True, name="wx-keys")
        self._key_thread.start()

    # ---------------- 收 ----------------
    def to_event(self, m: WxMsg) -> dict | None:
        d = self.data
        g = m.chat
        gid, uid = self.gid(g), self.ids.id(m.sender) if m.sender else 0
        if not uid:
            return None
        if m.pat_target:
            if m.pat_target == self.self_wxid and m.sender != self.self_wxid:
                return {"post_type": "notice", "notice_type": "notify", "sub_type": "poke", "group_id": gid,
                        "user_id": uid, "target_id": self.self_id, "time": m.ts}
            return None
        if not m.text:
            return None
        segs: list[dict] = []
        if m.quote_id:
            segs.append({"type": "reply", "data": {"id": str(m.quote_id)}})
        if m.emoji_md5 or m.emoji_url:
            segs.append({"type": "image", "data": {"url": m.emoji_url, "file": m.emoji_md5, "sub_type": 1,
                                                   "summary": "[动画表情]"}})
        elif m.type == 3 and m.img:                             # 13.1 图片：本地 .dat 解开再看（ingest 里按需取）
            segs.append({"type": "image", "data": {"url": f"wximg:{g}:{m.img}:{m.ts}", "file": m.img}})
        elif m.type == 1 or (m.type == 49 and m.quote_id):
            segs += self.split_ats(m.text, g, m.ats)
        else:
            segs.append({"type": "text", "data": {"text": m.text + (f" {m.link}" if m.link else "")}})
        name = d.display(m.sender, g)
        return {"post_type": "message", "message_type": "group", "group_id": gid, "user_id": uid,
                "message_id": m.mid, "message_seq": m.seq, "time": m.ts, "self_id": self.self_id,
                "sender": {"user_id": uid, "card": name,
                           "nickname": (d.contacts.get(m.sender) or {}).get("nick") or name},
                "message": segs}

    async def fetch_image(self, url: str, wait: float = 8.0, wait_full: float = 25.0) -> bytes | None:
        """13.1 wximg:<群>:<文件名>:<时间> → 解开的图片字节。刚收到的图可能还没下载完：等一会儿再找。"""
        try:
            _, chat, name, ts = url.split(":", 3)
        except ValueError:
            return None
        if self.data is None:
            return None
        st = getattr(self, "_images", None)
        if st is None or st.acc != Path(self.data.account_dir):
            from aigroup.wechat.wximg import ImageStore
            code = self.opts.get("img_code")
            st = self._images = ImageStore(self.data.account_dir, self.data.account_dir.name,
                                           self.opts.get("data_dir") or None, int(code) if code else None)
        t0 = time.monotonic()
        self.last_was_thumb = False
        while True:
            # 30.1 原图还在下（解开是半截 / 一片白）：先等原图，别马上拿缩略图去看（10-27 21:05 缩略图被看成了“某某 通知 请查收”）
            late = time.monotonic() - t0 > wait_full
            try:
                out = await asyncio.to_thread(st.load, chat, name, float(ts or 0), late)
            except Exception as e:  # noqa: BLE001
                log.debug("读图片失败：%s", e)
                out = None
            if out:
                self.last_was_thumb = bool(getattr(st, "thumb", False))
                return out
            if late or time.monotonic() - t0 > (wait_full if getattr(st, "partial", False) else wait):
                if late or not getattr(st, "partial", False):
                    return None
            await asyncio.sleep(2)

    def split_ats(self, text: str, chat: str, ats: list[str]) -> list[dict]:
        """“@名字\\u2005”换成 at 段；@ 到自己（不管 atuserlist 有没有）都认出来。"""
        d = self.data
        cand: dict[str, str] = {}
        for u in ats:
            for nm in {d.display(u, chat), (d.contacts.get(u) or {}).get("nick") or ""}:
                if nm:
                    cand[nm] = u
        for nm in d.self_names(chat) + [str(x) for x in self.opts.get("self_names") or []]:
            if nm:
                cand[nm] = self.self_wxid
        if "notify@all" in ats or "@all" in ats:
            cand.setdefault("所有人", "all")
        if not cand:
            return [{"type": "text", "data": {"text": text}}]
        names = sorted(cand, key=len, reverse=True)
        pat = re.compile("@(" + "|".join(re.escape(n) for n in names) + ")(?:[  ]|$)")
        segs, pos = [], 0
        for mm in pat.finditer(text):
            if mm.start() > pos:
                segs.append({"type": "text", "data": {"text": text[pos:mm.start()]}})
            u = cand[mm.group(1)]
            segs.append({"type": "at", "data": {"qq": "all" if u == "all" else str(self.ids.id(u)),
                                                "name": mm.group(1)}})
            pos = mm.end()
        if pos < len(text):
            segs.append({"type": "text", "data": {"text": text[pos:]}})
        return segs or [{"type": "text", "data": {"text": text}}]

    # ---------------- 发 ----------------
    def render(self, gid: int, segs: list[dict]) -> tuple[str, list[str]]:
        from aigroup.onebot import FACES
        chat = self.chat_of(gid)
        parts, files = [], []
        for s in segs:
            t, dd = s.get("type"), s.get("data") or {}
            if t == "text":
                parts.append(str(dd.get("text", "")))
            elif t == "at":
                q = str(dd.get("qq", ""))
                u = self.ids.wxid(q) if q.isdigit() else ""
                nm = self.data.display(u, chat) if u else (dd.get("name") or q)
                if q != "all":                                  # 不 @所有人
                    parts.append(f"@{nm} ")
            elif t == "image":
                f = str(dd.get("file") or "")
                if f.startswith("base64://"):
                    raw = base64.b64decode(f[9:])
                    ext = (".gif" if raw[:3] == b"GIF" else ".png" if raw[:4] == b"\x89PNG"
                           else ".webp" if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP" else ".jpg")
                    out = str(self.opts.get("outbox") or "") or tempfile.gettempdir()   # 微信在容器里时：放挂进去的目录
                    Path(out).mkdir(parents=True, exist_ok=True)
                    p = Path(out) / f"aiwx_{int(time.time() * 1000)}{ext}"
                    p.write_bytes(raw)
                    files.append(str(p))
                elif f and os.path.exists(f):
                    files.append(f)
            elif t == "face":
                try:
                    parts.append(wx_face(FACES.get(int(dd.get("id")), "")))
                except (TypeError, ValueError):
                    pass
        return re.sub(r"[ \t]+", " ", "".join(parts)).strip(), files

    def skew(self) -> float:
        if not self._skews:
            return 0.0
        v = sorted(self._skews)
        return v[len(v) // 2]

    def _hour_count(self) -> int:
        now = time.monotonic()
        return sum(1 for t in self._sent_times if now - t < 3600)

    async def send_group(self, group_id: int, segs: list[dict]) -> int | None:
        if not self.ready:
            raise OneBotError("微信还没接上" if self.alive is not False else "微信没在运行")
        chat = self.chat_of(group_id)
        if chat not in self.groups:
            raise OneBotError(f"群 {group_id} 不在白名单里")
        text, files = self.render(group_id, segs)
        try:
            if not text and not files:
                return None
            name = self.data.room_name(chat)
            bad = self.send_problem(chat)
            if bad:
                raise OneBotError(bad)
            async with self._send_lock:
                mids = []
                for item in ([("text", text)] if text else []) + [("file", f) for f in files]:
                    mids.append(await self._send_one(chat, name, item))
                return mids[0] if mids else None
        finally:
            for f in files:                                      # 临时图片不管发没发出去都删掉
                try:
                    if Path(f).name.startswith("aiwx_"):
                        os.unlink(f)
                except OSError:
                    pass

    def send_problem(self, chat: str) -> str:
        return send_problem(self.data, chat, self.sender.titles)

    async def _send_one(self, chat: str, name: str, item: tuple) -> int:
        if self._hour_count() >= self.max_per_hour:
            self.last_error = f"这一小时已经发了 {self.max_per_hour} 条（max_sends_per_hour），先停一停"
            raise OneBotError(self.last_error)
        wait = self.send_gap - (time.monotonic() - self._last_send)
        if wait > 0:
            await asyncio.sleep(wait)
        t0 = time.monotonic()
        wall0 = time.time()
        mark = {(m.chat, m.local_id, m.shard) for m in list(self._self_seen)}
        fl = [chat, item[0], item[1], t0, False]             # 最后一个：确认失败了（之后才在库里冒出来就当她在别处发的记下）
        self._inflight.append(fl)
        loop = asyncio.get_running_loop()
        try:
            try:
                from aigroup.wechat.wxui import title_for
                how = await asyncio.to_thread(self.sender.paste_send, name,
                                              item[1] if item[0] == "text" else None,
                                              [item[1]] if item[0] == "file" else None,
                                              not self.data.ambiguous(chat),
                                              title_for(self.sender.titles, chat, name))
            except Exception as e:  # noqa: BLE001
                fl[4] = True
                self.stats["send_fail"] += 1
                self.last_error = f"发送失败：{e}"
                raise OneBotError(str(e)) from e
            finally:
                self._last_send = time.monotonic()
            self.last_how = how
            self._sent_times.append(time.monotonic())
            m = await self._confirm(chat, item, mark)
        finally:
            loop.call_later(60, self._drop_inflight, fl)
        if m is None:
            fl[4] = True
            self.stats["send_fail"] += 1
            if how == "search":                              # 搜索方式只要有一次没核对上就不再用（可能发错了地方）
                self.sender.search_fallback = False
                log.error("用搜索打开「%s」发的消息没在群里看到，已停用搜索方式：请在微信里双击这个群单独打开它的窗口", name)
            self.last_error = f"在「{name}」里没看到刚发的消息（{how}）"
            raise OneBotError(self.last_error)
        self.stats["sent"] += 1
        if m.ts:
            self._skews.append(wall0 - m.ts)
            sk = self.skew()
            if abs(sk) > 300 and not self._skew_warned:
                self._skew_warned = True
                log.warning("电脑的时钟和微信差了 %d 秒：已经按这个差值校正（最好开着 Windows 的自动对时）", int(sk))
        if not m.server_id:                                   # 刚发出去时服务器 id 可能还没回来：等一下，引用她的话才对得上
            sid = await self._wait_server_id(m)
            if sid:
                m.server_id = sid
        return m.mid

    async def _wait_server_id(self, m: WxMsg, timeout: float = 4.0) -> int:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            await asyncio.sleep(0.6)

            def look():
                with self._dlock:
                    self.data.refresh_all()
                    return self.data.server_id_of(m.chat, m.shard, m.local_id)
            try:
                sid = await asyncio.to_thread(look)
            except Exception:  # noqa: BLE001
                return 0
            if sid:
                return sid
        return 0

    def _drop_inflight(self, fl):
        try:
            self._inflight.remove(fl)
        except ValueError:
            pass

    async def _confirm(self, chat: str, item, mark: set):
        """库里出现了她刚发的那条（发之前就看到过的不算；不比时间，电脑时钟不准也没关系）。"""
        end = time.monotonic() + self.confirm_timeout
        while time.monotonic() < end:
            self._kick.set()
            await asyncio.sleep(min(0.5, self.interval * 2))
            for m in list(self._self_seen):
                if m.chat != chat or (m.chat, m.local_id, m.shard) in mark:
                    continue
                if item[0] == "text" and m.type in (1, 49) and similar(m.text, item[1]):
                    return m
                if item[0] == "file" and m.type in (3, 47, 49, 43, 11000):
                    return m
        return None

    # ---------------- 历史 / 成员（给 catch_up、refresh_members） ----------------
    def history_blocking(self, chat: str, count: int, until_seq: int) -> list[dict]:
        """库里这个群 until_seq 之前（含）的最后 count 条，转成 OneBot 消息（自己的也在里面，user_id 是她）。"""
        d = self.data
        rows = d.last_messages(chat, count, until_seq)
        out = []
        for m in rows:
            if m.pat_target:
                continue
            ev = self.to_event(m)
            if ev:
                out.append(ev)
        return out

    # ---------------- OneBot 动作 ----------------
    async def call(self, action: str, **p) -> dict | list:
        if action == "get_login_info":
            return {"user_id": self.self_id or 0, "nickname": self.nick or self.self_wxid}
        if self.data is None:
            raise OneBotError("微信还没接上")
        d = self.data
        chat = self.chat_of(p.get("group_id")) if p.get("group_id") is not None else ""
        if action == "get_group_info":
            return {"group_id": p.get("group_id"), "group_name": d.room_name(chat),
                    "member_count": len(d.members(chat))}
        if action == "get_group_list":
            def lst():
                with self._dlock:
                    return d.groups()
            return [{"group_id": self.gid(g["id"]), "group_name": g["name"], "member_count": g["members"]}
                    for g in await asyncio.to_thread(lst)]
        if action == "get_group_member_info":
            u = self.ids.wxid(p.get("user_id"))
            name = d.display(u, chat)
            return {"user_id": p.get("user_id"), "card": name, "nickname": (d.contacts.get(u) or {}).get("nick") or name}
        if action == "get_group_member_list":
            def mem():
                with self._dlock:
                    d.load_contacts()
                    cards = (d.rooms.get(chat) or {}).get("cards") or {}
                    return [{"user_id": self.ids.id(u), "card": cards.get(u, ""),
                             "nickname": (d.contacts.get(u) or {}).get("nick") or ""} for u in d.members(chat)]
            return await asyncio.to_thread(mem)
        if action == "get_group_msg_history":
            if chat not in self.groups:
                return {"messages": []}
            count = max(1, min(2000, int(p.get("count") or 80)))
            until = self.wm.get(chat, 0)

            def hist():
                with self._dlock:
                    return self.history_blocking(chat, count, until)
            return {"messages": await asyncio.to_thread(hist)}
        raise OneBotError(f"微信不支持 {action}")
