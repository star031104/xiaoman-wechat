"""读微信 4.x 的本地数据：群、群成员、群名片、新消息。全部只读（读解密后的副本）。

    库的布局（<数据目录>/<账号目录>/db_storage/）：
    - contact/contact.db  contact（username, nick_name, remark…）、chat_room（群主、ext_buffer 里有群名片）、chatroom_member
    - message/message_N.db  每个会话一张 Msg_<md5(会话 id)>，可能分在好几个分片里；Name2Id 把 real_sender_id 换成 wxid
      local_type 低 32 位是类型（1 文字、3 图、34 语音、43 视频、47 表情、49 卡片/引用…、10000 系统），高 32 位是子类型
      message_content 可能是 zstd 压缩的；群消息正文前面可能带“wxid_xxx:\\n”
"""
from __future__ import annotations

import glob
import hashlib
import html
import json
import logging
import os
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from aigroup.wechat.dbview import DBView, KeyMismatch

log = logging.getLogger("aigroup.wechat.db")

ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"


# ---------------- 找数据目录 ----------------
def _path_in(text: str) -> str | None:
    text = (text or "").strip().lstrip("﻿")
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            for k in ("dataDir", "data_dir", "fileSavePath", "savePath", "path", "defaultFileSavePath"):
                v = obj.get(k)
                if isinstance(v, str) and v.strip():
                    return v.strip()
    except ValueError:
        pass
    m = re.search(r"[A-Za-z]:[\\/][^\x00-\x1f\"'<>|*?]+", text)
    return m.group(0).strip() if m else None


def _root_with_accounts(p: str | None) -> Path | None:
    if not p:
        return None
    p = os.path.normpath(p)
    if re.fullmatch(r"[A-Za-z]:", p):
        p += os.sep
    for c in (Path(p), Path(p) / "xwechat_files"):
        try:
            if c.is_dir() and any((d / "db_storage").is_dir() for d in c.iterdir() if d.is_dir()):
                return c
        except OSError:
            continue
    return None


def find_data_root(hint: str = "") -> Path | None:
    """微信的数据目录（下面是一个个账号目录）。hint：config 里手填的。"""
    r = _root_with_accounts(hint)
    if r:
        return r
    for env in ("APPDATA", "LOCALAPPDATA"):
        base = os.environ.get(env, "")
        for d in (Path(base) / "Tencent" / "xwechat" / "config", Path(base) / "Tencent" / "xwechat"):
            if not base or not d.is_dir():
                continue
            for f in d.glob("*"):
                if not f.is_file() or f.stat().st_size > 65536:
                    continue
                for enc in ("utf-8", "gbk"):
                    try:
                        r = _root_with_accounts(_path_in(f.read_text(encoding=enc)))
                        break
                    except (UnicodeDecodeError, OSError):
                        r = None
                if r:
                    return r
    home = os.environ.get("USERPROFILE") or str(Path.home())
    for b in (Path(home) / "Documents", Path(home)):
        r = _root_with_accounts(str(b))
        if r:
            return r
    for drive in "CDEFG":
        r = _root_with_accounts(f"{drive}:\\xwechat_files")
        if r:
            return r
    return None


def list_accounts(root: Path) -> list[dict]:
    out = []
    for d in sorted(Path(root).iterdir()):
        dbs = d / "db_storage"
        if not dbs.is_dir():
            continue
        mt = max((p.stat().st_mtime for p in dbs.rglob("*.db*")), default=0)
        out.append({"dir": d, "account": d.name, "wxid": account_wxid(d.name), "mtime": mt})
    out.sort(key=lambda x: -x["mtime"])
    return out


def account_wxid(dirname: str) -> str:
    """账号目录名 = 微信号 + “_” + 4 位后缀。"""
    return re.sub(r"_[0-9A-Za-z]{4}$", "", dirname)


# ---------------- 解析 ----------------
def unpack_text(b) -> str:
    """消息正文：bytes（可能 zstd 压缩、可能有尾部填充）→ str。"""
    if b is None:
        return ""
    if isinstance(b, str):
        return b
    b = bytes(b)
    if b[:4] == ZSTD_MAGIC:
        try:
            import zstandard
            b = zstandard.ZstdDecompressor().decompress(b, max_output_size=4 * 1024 * 1024)
        except Exception:  # noqa: BLE001
            m = re.search(rb"<\?xml|<msg|<appmsg|<emoji|<sysmsg", b)
            if not m:
                return ""
            b = b[m.start():]
    t = b.decode("utf-8", errors="replace")
    return t.replace("\x00", "").strip()


def pb_fields(buf: bytes, depth: int = 0):
    """极简 protobuf 解析：yield (字段号, 线型, 值)。坏数据就停。"""
    i, n = 0, len(buf)
    while i < n:
        key, i = _varint(buf, i)
        if i is None:
            return
        fn, wt = key >> 3, key & 7
        if wt == 0:
            v, i = _varint(buf, i)
            if i is None:
                return
        elif wt == 2:
            ln, i = _varint(buf, i)
            if i is None or i + ln > n:
                return
            v, i = buf[i:i + ln], i + ln
        elif wt == 1:
            v, i = buf[i:i + 8], i + 8
        elif wt == 5:
            v, i = buf[i:i + 4], i + 4
        else:
            return
        yield fn, wt, v


def _varint(b: bytes, i: int):
    r, s = 0, 0
    while i < len(b):
        c = b[i]
        i += 1
        r |= (c & 0x7F) << s
        if not c & 0x80:
            return r, i
        s += 7
        if s > 63:
            break
    return None, None


def room_cards(ext_buffer) -> dict[str, str]:
    """chat_room.ext_buffer 里的群名片：{wxid: 群里的名字}（找“字段 1 是 wxid、字段 2 是名字”的子消息）。"""
    out: dict[str, str] = {}
    if not ext_buffer:
        return out

    def walk(buf: bytes, depth: int):
        if depth > 4:
            return
        subs = []
        for fn, wt, v in pb_fields(buf):
            if wt == 2 and v:
                subs.append((fn, v))
        f = {}
        for fn, v in subs:
            f.setdefault(fn, v)
        uid, name = f.get(1), f.get(2)
        if uid and name is not None:
            try:
                u, nm = uid.decode("utf-8"), name.decode("utf-8")
                if re.fullmatch(r"[A-Za-z][\w\-.@]{3,}", u) and len(nm) <= 64 and "\x00" not in nm:
                    if nm.strip():
                        out[u] = nm.strip()
                    return
            except UnicodeDecodeError:
                pass
        for _, v in subs:
            walk(v, depth + 1)
    try:
        walk(bytes(ext_buffer), 0)
    except Exception:  # noqa: BLE001
        pass
    return out


def xml_tag(x: str, tag: str) -> str:
    m = re.search(rf"<{tag}(?:\s[^>]*)?>(.*?)</{tag}>", x or "", re.S)
    if not m:
        return ""
    v = m.group(1).strip()
    c = re.fullmatch(r"<!\[CDATA\[(.*)\]\]>", v, re.S)
    return html.unescape(c.group(1) if c else v).strip()


def xml_attr(x: str, tag: str, attr: str) -> str:
    m = re.search(rf"<{tag}\b[^>]*\b{attr}\s*=\s*\"([^\"]*)\"", x or "", re.S)
    return html.unescape(m.group(1)) if m else ""


@dataclass
class WxMsg:
    chat: str
    local_id: int
    server_id: int
    seq: int
    ts: int
    type: int
    sub: int
    sender: str                      # wxid（自己发的 = self_wxid）
    text: str = ""                   # 给大脑看的文字（占位后的）
    raw: str = ""
    ats: list[str] = field(default_factory=list)
    quote_id: int | None = None      # 引用的那条的 server_id
    quote_sender: str = ""
    emoji_md5: str = ""
    emoji_url: str = ""
    pat_target: str = ""             # 拍一拍：被拍的人
    link: str = ""
    shard: str = ""
    img: str = ""                    # 13.1 图片消息：本地 .dat 的文件名（packed_info_data 里的 32 位十六进制）

    @property
    def mid(self) -> int:
        return self.server_id or -self.local_id


SPLIT_SENDER = re.compile(r"^([A-Za-z][\w\-.@]{2,}):\n", re.S)


def parse_row(chat: str, r, self_wxid: str, name2id: dict[int, str], shard: str = "") -> WxMsg:
    lt = int(r["local_type"] or 0)
    base, sub = lt & 0xFFFFFFFF, lt >> 32
    body = unpack_text(r["message_content"])
    if not body and "compress_content" in r.keys():
        body = unpack_text(r["compress_content"])
    sender = name2id.get(int(r["real_sender_id"] or 0), "") if r["real_sender_id"] is not None else ""
    m = SPLIT_SENDER.match(body)
    if m and chat.endswith("@chatroom"):
        sender = sender or m.group(1)
        body = body[m.end():]
    m = WxMsg(chat=chat, local_id=int(r["local_id"] or 0), server_id=int(r["server_id"] or 0),
              seq=int(r["sort_seq"] or 0), ts=int(r["create_time"] or 0), type=base, sub=sub,
              sender=sender, raw=body, shard=shard)
    if base == 3 and "packed_info_data" in r.keys():
        from aigroup.wechat.wximg import packed_image_name
        m.img = packed_image_name(r["packed_info_data"])
    src = unpack_text(r["source"]) if "source" in r.keys() else ""
    at = xml_tag(src, "atuserlist")
    if at:
        m.ats = [a for a in re.split(r"[,;\s]+", at) if a]
    fill(m)
    return m


def fill(m: WxMsg) -> None:
    """按类型写出给大脑看的文字。"""
    b = m.raw
    if m.type == 1:
        m.text = b
    elif m.type == 3:
        m.text = "[图片]"
    elif m.type == 34:
        m.text = "[语音]"
    elif m.type == 43:
        m.text = "[视频]"
    elif m.type in (47, 11000) or (m.type == 49 and "<emoji" in b):
        m.text = "[表情包]"
        m.emoji_md5 = xml_attr(b, "emoji", "md5")
        m.emoji_url = xml_attr(b, "emoji", "cdnurl")
    elif m.type == 48:
        m.text = f"[位置：{xml_attr(b, 'location', 'poiname') or xml_attr(b, 'location', 'label')}]".replace("：]", "]")
    elif m.type == 42:
        m.text = "[名片]"
    elif m.type == 50:
        m.text = "[通话]"
    elif m.type == 49:
        top = re.sub(r"<refermsg>.*?</refermsg>", "", b, flags=re.S)     # 引用里也有 <type>，先拿掉
        t = xml_tag(top, "type")
        at = int(t) if t.isdigit() else m.sub
        title = xml_tag(top, "title")
        if at == 57:                                         # 引用
            ref = re.search(r"<refermsg>(.*?)</refermsg>", b, re.S)
            r = ref.group(1) if ref else ""
            sid = xml_tag(r, "svrid")
            m.quote_id = int(sid) if sid.isdigit() else None
            m.quote_sender = xml_tag(r, "chatusr") or xml_tag(r, "fromusr")
            m.text = title
        elif at == 62:
            m.pat_target = xml_tag(b, "pattedusername")
            m.sender = xml_tag(b, "fromusername") or m.sender
            m.text = "[拍一拍]"
        elif at == 5:
            m.link = xml_tag(b, "url")
            m.text = f"[分享：{title[:60]}]" if title else "[链接]"
        elif at == 6:
            m.text = f"[文件:{title}]"
        elif at in (33, 36, 44):
            m.text = f"[小程序：{title[:40]}]" if title else "[小程序]"
        elif at in (19, 40):
            m.text = "[合并转发]"
        elif at == 2000:
            m.text = "[转账]"
        elif at in (2001, 2003):
            m.text = "[红包]"
        elif at in (4, 3, 76, 92):
            m.text = f"[分享：{title[:60]}]" if title else "[分享]"
        else:
            m.text = f"[分享：{title[:60]}]" if title else "[卡片消息]"
    elif m.type == 10000 or m.type == 10002:
        if "<pattedusername>" in b:
            m.pat_target = xml_tag(b, "pattedusername")
            m.sender = xml_tag(b, "fromusername") or m.sender        # 系统消息的发送者可能记成群本身
            m.text = "[拍一拍]"
        else:
            m.text = ""                                      # 系统提示（进群、撤回…）不当聊天
    else:
        m.text = f"[{m.type}]" if not b else ""


def md5(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()


def _s(v) -> str:
    if v is None:
        return ""
    if isinstance(v, bytes):
        return v.decode("utf-8", errors="replace")
    return str(v)


# ---------------- 库 ----------------
class WeChatData:
    """account_dir 下的几个库；keys：rel → 密钥。所有方法都是阻塞的，上层放到线程里调。"""

    def __init__(self, account_dir: str | os.PathLike, keys: dict[str, bytes], work: str | os.PathLike,
                 self_wxid: str = ""):
        self.account_dir = Path(account_dir)
        self.base = self.account_dir / "db_storage"
        self.keys = dict(keys)
        self.work = Path(work)
        self.self_wxid = self_wxid or account_wxid(self.account_dir.name)
        self.views: dict[str, DBView] = {}
        self._conns: dict[str, tuple[int, sqlite3.Connection]] = {}
        self._name2id: dict[str, tuple[int, dict[int, str]]] = {}
        self._tables: dict[str, tuple[int, set[str]]] = {}
        self.contacts: dict[str, dict] = {}
        self.rooms: dict[str, dict] = {}
        self._contact_ver = -1

    # ---- 视图 ----
    def shards(self) -> list[str]:
        out = []
        for p in sorted(glob.glob(str(self.base / "message" / "message_*.db"))):
            if re.fullmatch(r"message_\d+\.db", os.path.basename(p)):
                out.append("message/" + os.path.basename(p))
        return out

    def missing_keys(self) -> list[str]:
        need = self.shards() + ["contact/contact.db"]
        return [r for r in need if r not in self.keys]

    def view(self, rel: str) -> DBView | None:
        v = self.views.get(rel)
        if v is None:
            k = self.keys.get(rel)
            p = self.base / rel
            if not k or not p.exists():
                return None
            v = DBView(p, k, self.work, rel.replace("/", "__"))
            self.views[rel] = v
        return v

    def _refresh(self, rel: str, v: DBView) -> bool:
        """刷新前先关掉这个库的连接（Windows 上开着的文件不好改大小）。"""
        if not v.needs_refresh():
            return False
        old = self._conns.pop(rel, None)
        if old:
            old[1].close()
        try:
            return v.refresh()
        except KeyMismatch as e:                              # 丢掉这个库的密钥和副本：missing_keys() 会报出来，上层去重新找
            log.warning("%s", e)
            self.keys.pop(rel, None)
            self.views.pop(rel, None)
            self._tables.pop(rel, None)
            self._name2id.pop(rel, None)
            v.close()
            return False
        except OSError as e:
            log.debug("刷新 %s 失败：%s", rel, e)
            return False

    def conn(self, rel: str, refresh: bool = True) -> sqlite3.Connection | None:
        v = self.view(rel)
        if v is None:
            return None
        if refresh:
            self._refresh(rel, v)
        old = self._conns.get(rel)
        if old and old[0] == v.version:
            return old[1]
        if old:
            old[1].close()
        if not v.view_path.exists():
            return None
        c = v.connect()
        self._conns[rel] = (v.version, c)
        return c

    def refresh_all(self) -> bool:
        """刷新所有消息分片；有变化返回 True。"""
        ch = False
        for rel in self.shards():
            v = self.view(rel)
            if v is None:
                continue
            ch = self._refresh(rel, v) or ch
        return ch

    def versions(self) -> tuple:
        return tuple((r, v.version) for r, v in sorted(self.views.items()) if r.startswith("message/"))

    # ---- 联系人 / 群 ----
    def load_contacts(self, force: bool = False) -> bool:
        c = self.conn("contact/contact.db")
        v = self.views.get("contact/contact.db")
        if c is None or v is None:
            return False
        if v.version == self._contact_ver and not force:
            return False
        self._contact_ver = v.version
        cols = {_s(r[1]) for r in c.execute("PRAGMA table_info(contact)")}
        sel = ", ".join(x for x in ("id", "username", "nick_name", "remark", "alias") if x in cols)
        contacts, by_id = {}, {}
        for r in c.execute(f"SELECT {sel} FROM contact"):
            u = _s(r["username"])
            if not u:
                continue
            d = {"username": u, "nick": _s(r["nick_name"]), "remark": _s(r["remark"]) if "remark" in cols else ""}
            contacts[u] = d
            if "id" in cols:
                by_id[r["id"]] = u
        rooms = {}
        try:
            rcols = {_s(r[1]) for r in c.execute("PRAGMA table_info(chat_room)")}
            sel = ", ".join(x for x in ("id", "username", "owner", "ext_buffer") if x in rcols)
            members: dict = {}
            try:
                for r in c.execute("SELECT room_id, member_id FROM chatroom_member"):
                    u = by_id.get(r["member_id"])
                    if u:
                        members.setdefault(r["room_id"], []).append(u)
            except sqlite3.Error:
                pass
            for r in c.execute(f"SELECT {sel} FROM chat_room"):
                u = _s(r["username"])
                if not u:
                    continue
                rooms[u] = {"username": u, "owner": _s(r["owner"]) if "owner" in rcols else "",
                            "members": members.get(r["id"], []) if "id" in rcols else [],
                            "cards": room_cards(r["ext_buffer"]) if "ext_buffer" in rcols else {},
                            "name": (contacts.get(u) or {}).get("nick") or ""}
        except sqlite3.Error as e:
            log.debug("读群表失败：%s", e)
        for u, d in contacts.items():                          # 没进 chat_room 表的群（刚加的）
            if u.endswith("@chatroom") and u not in rooms:
                rooms[u] = {"username": u, "owner": "", "members": [], "cards": {}, "name": d["nick"]}
        self.contacts, self.rooms = contacts, rooms
        return True

    def groups(self) -> list[dict]:
        self.load_contacts()
        out = []
        for u, r in self.rooms.items():
            out.append({"id": u, "name": r["name"] or u, "members": len(r["members"]),
                        "has_msgs": bool(self.tables_for(u))})
        out.sort(key=lambda g: (not g["has_msgs"], -g["members"]))
        return out

    def display(self, wxid: str, room: str = "") -> str:
        r = self.rooms.get(room) or {}
        card = (r.get("cards") or {}).get(wxid)
        c = self.contacts.get(wxid) or {}
        return card or c.get("nick") or c.get("remark") or wxid

    def self_names(self, room: str = "") -> list[str]:
        out = []
        r = self.rooms.get(room) or {}
        for v in ((r.get("cards") or {}).get(self.self_wxid), (self.contacts.get(self.self_wxid) or {}).get("nick")):
            if v and v not in out:
                out.append(v)
        return out

    def room_name(self, room: str) -> str:
        return (self.rooms.get(room) or {}).get("name") or (self.contacts.get(room) or {}).get("nick") or room

    def ambiguous(self, room: str) -> list[str]:
        """用群名搜索时可能先搜到的别的会话（名字里包含这个群名的联系人 / 群）。"""
        name = self.room_name(room)
        out = []
        for u, c in self.contacts.items():
            if u == room or not name:
                continue
            for v in (c.get("nick"), c.get("remark")):
                if v and name in v:
                    out.append(v)
                    break
        return out

    def same_name(self, room: str) -> list[str]:
        """名字和这个群一模一样的别的会话（它们的单独窗口标题也一样，分不清）。"""
        name = self.room_name(room)
        if not name:
            return []
        return [u for u, c in self.contacts.items() if u != room and name in (c.get("nick"), c.get("remark"))]

    def server_id_of(self, chat: str, shard: str, local_id: int) -> int:
        tn = "Msg_" + md5(chat)
        c = self.conn(shard, refresh=False) if shard else None
        if c is None:
            return 0
        try:
            r = c.execute(f"SELECT server_id FROM {tn} WHERE local_id=?", (local_id,)).fetchone()
        except sqlite3.Error:
            return 0
        return int(r[0] or 0) if r else 0

    def members(self, room: str) -> list[str]:
        return list((self.rooms.get(room) or {}).get("members") or [])

    # ---- 消息 ----
    def _shard_tables(self, rel: str, c: sqlite3.Connection) -> set[str]:
        v = self.views[rel]
        t = self._tables.get(rel)
        if t and t[0] == v.version:
            return t[1]
        names = {_s(r[0]) for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self._tables[rel] = (v.version, names)
        return names

    def _n2i(self, rel: str, c: sqlite3.Connection) -> dict[int, str]:
        v = self.views[rel]
        t = self._name2id.get(rel)
        if t and t[0] == v.version:
            return t[1]
        try:
            mp = {int(r[0]): _s(r[1]) for r in c.execute("SELECT rowid, user_name FROM Name2Id")}
        except sqlite3.Error:
            mp = {}
        self._name2id[rel] = (v.version, mp)
        return mp

    def tables_for(self, chat: str) -> list[tuple[str, sqlite3.Connection, str]]:
        tn = "Msg_" + md5(chat)
        out = []
        for rel in self.shards():
            c = self.conn(rel, refresh=False) or self.conn(rel)
            if c is not None and tn in self._shard_tables(rel, c):
                out.append((rel, c, tn))
        return out

    def max_seq(self, chat: str) -> int:
        best = 0
        for rel, c, tn in self.tables_for(chat):
            r = c.execute(f"SELECT MAX(sort_seq) FROM {tn}").fetchone()
            best = max(best, int(r[0] or 0))
        return best

    def _cols(self, c, tn) -> list[str]:
        cols = {_s(r[1]) for r in c.execute(f"PRAGMA table_info({tn})")}
        return [x for x in ("local_id", "server_id", "local_type", "sort_seq", "real_sender_id", "create_time",
                            "message_content", "compress_content", "source", "packed_info_data") if x in cols]

    def last_messages(self, chat: str, count: int, until: int | None = None) -> list[WxMsg]:
        """这个群（sort_seq ≤ until 的）最后 count 条，按时间先后。"""
        out: list[WxMsg] = []
        for rel, c, tn in self.tables_for(chat):
            want = self._cols(c, tn)
            n2i = self._n2i(rel, c)
            where, args = ("WHERE sort_seq <= ?", (until,)) if until else ("", ())
            for r in c.execute(f"SELECT {', '.join(want)} FROM {tn} {where} ORDER BY sort_seq DESC LIMIT ?",
                               args + (count,)):
                try:
                    out.append(parse_row(chat, _RowView(r, want), self.self_wxid, n2i, rel))
                except Exception as e:  # noqa: BLE001
                    log.debug("解析消息失败：%s", e)
        out.sort(key=lambda m: m.seq)
        return out[-count:]

    def new_messages(self, chat: str, since: int, limit: int = 200) -> list[WxMsg]:
        out: list[WxMsg] = []
        for rel, c, tn in self.tables_for(chat):
            want = self._cols(c, tn)
            n2i = self._n2i(rel, c)
            for r in c.execute(f"SELECT {', '.join(want)} FROM {tn} WHERE sort_seq > ? ORDER BY sort_seq LIMIT ?",
                               (since, limit)):
                try:
                    out.append(parse_row(chat, _RowView(r, want), self.self_wxid, n2i, rel))
                except Exception as e:  # noqa: BLE001
                    log.debug("解析消息失败：%s", e)
        out.sort(key=lambda m: m.seq)
        return out[:limit]

    def close(self):
        for _, c in self._conns.values():
            try:
                c.close()
            except Exception:  # noqa: BLE001
                pass
        self._conns.clear()


class _RowView:
    """sqlite3.Row 缺列时补 None（不同版本的表列不完全一样）。"""

    def __init__(self, r, cols):
        self.r, self.cols = r, cols

    def keys(self):
        return self.cols

    def __getitem__(self, k):
        return self.r[k] if k in self.cols else None
