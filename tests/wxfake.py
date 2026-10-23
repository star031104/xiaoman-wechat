"""测试用：造一份和微信 4.x 一样格式的加密库（SQLCipher 4 页格式 + 加密 wal）。"""
from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import struct
import time
from pathlib import Path

from aigroup.wechat.crypto import PAGE, encrypt_page
from aigroup.wechat.dbview import FRAME, FRAME_HDR, WAL_HDR, wal_cksum


def make_plain(path: Path) -> None:
    """建一个空库，保留区 80 字节（和 SQLCipher 一样），之后建的表都按这个格式。"""
    path = Path(path)
    for suf in ("", "-wal", "-shm"):
        Path(str(path) + suf).unlink(missing_ok=True)
    c = sqlite3.connect(path)
    c.execute("PRAGMA page_size=4096")
    c.execute("CREATE TABLE _x(a)")
    c.execute("DROP TABLE _x")
    c.commit()
    c.execute("VACUUM")
    c.close()
    b = bytearray(path.read_bytes())
    b[20] = 80
    assert b[100] == 0x0D and b[103:105] == b"\x00\x00", "page 1 应该是空表"
    b[105:107] = struct.pack(">H", PAGE - 80)          # 空页的内容区起点挪到保留区前面
    path.write_bytes(bytes(b))


def _iv(pgno: int, page: bytes) -> bytes:
    """内容不变的页密文也不变（和微信一样：没改过的页不会重写）。"""
    return hashlib.sha256(struct.pack('<I', pgno) + page).digest()[:16]


def encrypt_file(plain: Path, out: Path, key: bytes, salt: bytes) -> None:
    data = Path(plain).read_bytes()
    n = len(data) // PAGE
    res = bytearray()
    for pgno in range(1, n + 1):
        page = data[(pgno - 1) * PAGE: pgno * PAGE]
        res += encrypt_page(key, page, pgno, salt, _iv(pgno, page))
    Path(out).write_bytes(bytes(res))


def encrypt_wal(plain_wal: Path, out: Path, key: bytes, salt: bytes, recompute_ck: bool = True) -> None:
    raw = Path(plain_wal).read_bytes()
    if len(raw) < WAL_HDR:
        Path(out).write_bytes(raw)
        return
    hdr = raw[:WAL_HDR]
    magic = struct.unpack(">I", hdr[:4])[0]
    big = bool(magic & 1)
    ck = wal_cksum(hdr[:24], 0, 0, big)
    res = bytearray(hdr)
    i = 0
    while WAL_HDR + (i + 1) * FRAME <= len(raw):
        fr = raw[WAL_HDR + i * FRAME: WAL_HDR + (i + 1) * FRAME]
        fh, page = fr[:FRAME_HDR], fr[FRAME_HDR:]
        pgno = struct.unpack(">I", fh[:4])[0]
        enc = encrypt_page(key, page, pgno, salt, _iv(pgno, page))
        if recompute_ck:
            ck = wal_cksum(fh[:8] + enc, ck[0], ck[1], big)
            fh = fh[:16] + struct.pack(">II", *ck)
        res += fh + enc
        i += 1
    Path(out).write_bytes(bytes(res))


class FakeWeChatDB:
    """一份“微信在用”的库：plain 是明文的真身，enc 是加密后放给程序读的那份。"""

    def __init__(self, plain: Path, enc: Path, key: bytes, salt: bytes | None = None):
        self.plain, self.enc, self.key = Path(plain), Path(enc), key
        self.salt = salt or os.urandom(16)
        self.enc.parent.mkdir(parents=True, exist_ok=True)
        make_plain(self.plain)
        self.conn = sqlite3.connect(self.plain, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA wal_autocheckpoint=0")

    def execute(self, sql, params=()):
        self.conn.execute(sql, params)
        self.conn.commit()

    def executescript(self, sql):
        self.conn.executescript(sql)
        self.conn.commit()

    def checkpoint(self, mode: str = "TRUNCATE"):
        self.conn.execute(f"PRAGMA wal_checkpoint({mode})")

    def publish(self):
        """把当前明文主库 + wal 加密写到 enc（模拟微信磁盘上的样子）。"""
        tmp = self.plain.with_suffix(".snap")
        shutil.copyfile(self.plain, tmp)
        encrypt_file(tmp, self.enc, self.key, self.salt)
        wal = Path(str(self.plain) + "-wal")
        ew = Path(str(self.enc) + "-wal")
        if wal.exists() and wal.stat().st_size >= WAL_HDR:
            shutil.copyfile(wal, tmp)
            encrypt_wal(tmp, ew, self.key, self.salt)
        else:
            ew.unlink(missing_ok=True)
        tmp.unlink(missing_ok=True)

    def close(self):
        self.conn.close()


# ---------------- 一个假的微信账号目录 ----------------
def _pb_varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def pb_field(fn: int, v) -> bytes:
    if isinstance(v, int):
        return _pb_varint(fn << 3) + _pb_varint(v)
    if isinstance(v, str):
        v = v.encode()
    return _pb_varint(fn << 3 | 2) + _pb_varint(len(v)) + v


def ext_buffer(cards: dict[str, str]) -> bytes:
    """chat_room.ext_buffer：repeated 成员 {1: wxid, 2: 群名片, 3: 标志}。"""
    out = pb_field(1, 3)
    for u, n in cards.items():
        out += pb_field(4, pb_field(1, u) + pb_field(2, n) + pb_field(3, 0))
    return out


class FakeAccount:
    SELF = "wxid_her"

    def __init__(self, root: Path):
        import hashlib as _h
        import threading
        self._h = _h
        self._lock = threading.RLock()
        self.root = Path(root) / "xwechat_files"
        self.acc = self.root / "wxid_her_ab12"
        st = self.acc / "db_storage"
        self.keys = {"contact/contact.db": os.urandom(32), "message/message_0.db": os.urandom(32)}
        self.contact = FakeWeChatDB(Path(root) / "plain_contact.db", st / "contact" / "contact.db",
                                    self.keys["contact/contact.db"])
        self.msg = FakeWeChatDB(Path(root) / "plain_msg.db", st / "message" / "message_0.db",
                                self.keys["message/message_0.db"])
        self.contact.executescript(
            "CREATE TABLE contact(id INTEGER PRIMARY KEY, username TEXT UNIQUE, alias TEXT, nick_name TEXT, remark TEXT,"
            " local_type INTEGER);"
            "CREATE TABLE chat_room(id INTEGER PRIMARY KEY, username TEXT UNIQUE, owner TEXT, ext_buffer BLOB);"
            "CREATE TABLE chatroom_member(room_id INTEGER, member_id INTEGER);")
        self.msg.executescript("CREATE TABLE Name2Id(user_name TEXT PRIMARY KEY, is_session INTEGER);")
        self.seq = 1_700_000_000_000
        self.sid = 5_000_000_000_000_000_000
        self.add_contact(self.SELF, "小满")
        self.publish()

    def add_contact(self, u: str, nick: str, remark: str = ""):
        self.contact.execute("INSERT INTO contact(username, nick_name, remark) VALUES (?,?,?) ON CONFLICT(username) "
                             "DO UPDATE SET nick_name=excluded.nick_name, remark=excluded.remark", (u, nick, remark))

    def add_room(self, room: str, name: str, members: dict[str, str], cards: dict[str, str] | None = None):
        self.add_contact(room, name)
        for u, n in members.items():
            self.add_contact(u, n)
        c = self.contact.conn
        c.execute("INSERT INTO chat_room(username, owner, ext_buffer) VALUES (?,?,?) ON CONFLICT(username) DO UPDATE "
                  "SET owner=excluded.owner, ext_buffer=excluded.ext_buffer",
                  (room, next(iter(members)), ext_buffer(cards or {})))
        rid = c.execute("SELECT id FROM chat_room WHERE username=?", (room,)).fetchone()[0]
        c.execute("DELETE FROM chatroom_member WHERE room_id=?", (rid,))
        for u in members:
            mid = c.execute("SELECT id FROM contact WHERE username=?", (u,)).fetchone()[0]
            c.execute("INSERT INTO chatroom_member VALUES (?,?)", (rid, mid))
        c.commit()

    def table(self, chat: str) -> str:
        t = "Msg_" + self._h.md5(chat.encode()).hexdigest()
        self.msg.conn.execute(
            f"CREATE TABLE IF NOT EXISTS {t}(local_id INTEGER PRIMARY KEY AUTOINCREMENT, server_id INTEGER, "
            "local_type INTEGER, sort_seq INTEGER, real_sender_id INTEGER, create_time INTEGER, status INTEGER, "
            "upload_status INTEGER, download_status INTEGER, server_seq INTEGER, origin_source INTEGER, source TEXT, "
            "message_content TEXT, compress_content TEXT, packed_info_data BLOB)")
        return t

    def name_id(self, u: str) -> int:
        c = self.msg.conn
        c.execute("INSERT OR IGNORE INTO Name2Id(user_name) VALUES (?)", (u,))
        return c.execute("SELECT rowid FROM Name2Id WHERE user_name=?", (u,)).fetchone()[0]

    def add_msg(self, *a, **k) -> int:
        with self._lock:
            return self._add_msg(*a, **k)

    def _add_msg(self, chat: str, sender: str, content, mtype: int = 1, source=None, prefix: bool = True,
                 ts: int | None = None, publish: bool = True) -> int:
        """prefix：群消息正文前面带“wxid:\\n”（微信对别人发的群消息是这样存的）。"""
        t = self.table(chat)
        if isinstance(content, str) and prefix and sender != self.SELF and chat.endswith("@chatroom"):
            content = f"{sender}:\n{content}"
        self.seq += 1000
        self.sid += 7
        self.msg.conn.execute(
            f"INSERT INTO {t}(server_id, local_type, sort_seq, real_sender_id, create_time, source, message_content) "
            "VALUES (?,?,?,?,?,?,?)",
            (self.sid, mtype, self.seq, self.name_id(sender), int(ts or time.time()), source, content))
        self.msg.conn.commit()
        if publish:
            self.publish()
        return self.sid

    def publish(self):
        self.contact.publish()
        self.msg.publish()
