"""把微信正在用的加密库（主库 + -wal）实时同步成一份可以直接用 sqlite3 读的明文副本。

    只读，不碰微信的文件（Windows 上微信开着库时允许别人共享读）。
    增量：
    - 主库：按页比较页尾 HMAC，只解密变了的页（微信做 checkpoint 时才会变）
    - -wal：只解析新追加的帧；只用“已提交”的帧（遇到提交帧才落盘），校验 salt、校验和、每页 HMAC，
      读到写了一半的帧就停下，下一轮再读
    - wal 被重置（salt 变了）：先把上一代 wal 盖上去的页恢复成主库里的样子，再从头套新一代
    副本的文件头改成“非 WAL 模式”，用 immutable 只读打开，和微信互不影响。
"""
from __future__ import annotations

import logging
import os
import struct
import time
from pathlib import Path

from aigroup.wechat.crypto import MAC_OFF, PAGE, decrypt_page, mac_key, open_shared, page_ok, verify_key

log = logging.getLogger("aigroup.wechat.db")

WAL_HDR = 32
FRAME_HDR = 24
FRAME = FRAME_HDR + PAGE
CHUNK = 256
_M = 0xFFFFFFFF


def wal_cksum(data: bytes, s0: int, s1: int, big: bool) -> tuple[int, int]:
    n = len(data) // 4
    w = struct.unpack((">" if big else "<") + "I" * n, data[:n * 4])
    for i in range(0, n, 2):
        s0 = (s0 + w[i] + s1) & _M
        s1 = (s1 + w[i + 1] + s0) & _M
    return s0, s1


def _stat(p: Path) -> tuple[float, int]:
    try:
        st = p.stat()
        return st.st_mtime_ns, st.st_size
    except OSError:
        return 0, -1


class KeyMismatch(OSError):
    """库还在，但手里的密钥解不开了（微信重建了这个库）：上层丢掉这个密钥重新去找。"""


class DBView:
    def __init__(self, src: str | os.PathLike, key: bytes, work: str | os.PathLike, name: str | None = None):
        self.src = Path(src)
        self.wal = Path(str(self.src) + "-wal")
        self.key = key
        self.work = Path(work)
        self.work.mkdir(parents=True, exist_ok=True)
        name = name or self.src.name
        self.base_path = self.work / (name + ".base")
        self.view_path = self.work / (name + ".plain.db")
        self.mk: bytes | None = None
        self._salt = b""
        self.fp: list[bytes] = []                 # 主库每页页尾 HMAC（判断哪页变了）
        self.overlay: set[int] = set()            # 当前被 wal 盖住的页
        self.wal_salt: bytes | None = None
        self.wal_idx = 0                          # 下一轮从第几帧接着读（总是停在提交点）
        self.wal_ck: tuple[int, int] = (0, 0)     # 提交点的累计校验和
        self.wal_big = False
        self.check_ck = True
        self.wal_pages = 0                        # 最近一次提交时的库大小（页数）；0＝wal 没有提交
        self.main_pages = 0
        self.stamp = None
        self.dirty = True
        self.version = 0                          # 每次内容变了 +1（上层据此决定要不要重新查）
        self.torn = 0
        for p in (self.base_path, self.view_path):
            p.unlink(missing_ok=True)

    # ---------------- 主库 ----------------
    def _read_page(self, f, pgno: int) -> bytes:
        f.seek((pgno - 1) * PAGE)
        return f.read(PAGE)

    def _sync_main(self, base, view) -> None:
        size = self.src.stat().st_size
        n = size // PAGE
        bad = 0
        with open_shared(self.src) as f:
            p1 = self._read_page(f, 1)
            if len(p1) < PAGE:
                raise OSError("库文件太小")
            if self.mk is None or not page_ok(self.mk, p1, 1):
                for _ in range(4):                          # 可能只是正好读到写了一半；也可能库被重建了（salt / 密钥变了）
                    if verify_key(self.key, p1):
                        break
                    time.sleep(0.1)
                    p1 = self._read_page(f, 1)
                else:
                    raise KeyMismatch(f"{self.src.name} 的密钥对不上了（微信重建了这个库？）")
                if self.mk is not None and p1[:16] != self._salt:
                    self.fp.clear()                         # salt 变了＝新库：旧的页指纹全作废
                self.mk = mac_key(self.key, p1[:16])
                self._salt = p1[:16]
            got = 0
            for pgno in range(1, n + 1):
                if (pgno - 1) % CHUNK == 0:                  # 一次读 CHUNK 页（库可能有几个 G，别一口气读进内存）
                    f.seek((pgno - 1) * PAGE)
                    data = f.read(CHUNK * PAGE)
                k = (pgno - 1) % CHUNK
                page = data[k * PAGE:(k + 1) * PAGE]
                if len(page) < PAGE:
                    break
                got = pgno
                tail = page[MAC_OFF:]
                if pgno <= len(self.fp) and self.fp[pgno - 1] == tail:
                    continue
                if not page.strip(b"\x00"):                # 预留的空页（Linux 版微信一建库就占好 25MB）：不用验，也别重读
                    if pgno > len(self.fp):
                        self.fp.append(tail)
                    else:
                        self.fp[pgno - 1] = tail
                    continue
                ok = page_ok(self.mk, page, pgno)
                for _ in range(3):                      # 撕裂（微信正在写这页）：单独重读几次
                    if ok:
                        break
                    time.sleep(0.05)
                    page = self._read_page(f, pgno)
                    tail = page[MAC_OFF:]
                    ok = page_ok(self.mk, page, pgno)
                if not ok:
                    bad += 1
                    if pgno > len(self.fp):
                        self.fp.append(b"")             # 占位：下次一定重读
                    continue
                plain = decrypt_page(self.key, page, pgno)
                base.seek((pgno - 1) * PAGE)
                base.write(plain)
                if pgno not in self.overlay:
                    view.seek((pgno - 1) * PAGE)
                    view.write(plain)
                if pgno <= len(self.fp):
                    self.fp[pgno - 1] = tail
                else:
                    self.fp.append(tail)
        n = got
        if n < len(self.fp):
            del self.fp[n:]
            base.truncate(n * PAGE)
        self.torn += bad
        if bad:
            self.dirty = True
        base.seek(0)
        h = base.read(100)
        self.main_pages = struct.unpack(">I", h[28:32])[0] if len(h) >= 32 else n
        if not self.main_pages or self.main_pages > n:
            self.main_pages = n

    # ---------------- wal ----------------
    def _restore_overlay(self, base, view) -> None:
        for pgno in sorted(self.overlay):
            base.seek((pgno - 1) * PAGE)
            page = base.read(PAGE)
            if len(page) == PAGE:
                view.seek((pgno - 1) * PAGE)
                view.write(page)
        self.overlay.clear()
        self.wal_pages = 0

    def _sync_wal(self, base, view) -> None:
        if not self.wal.exists() or self.wal.stat().st_size < WAL_HDR:
            if self.overlay or self.wal_salt:
                self._restore_overlay(base, view)
            self.wal_salt, self.wal_idx = None, 0
            return
        with open_shared(self.wal) as w:
            hdr = w.read(WAL_HDR)
            if len(hdr) < WAL_HDR:
                return
            magic = struct.unpack(">I", hdr[:4])[0]
            if magic not in (0x377F0682, 0x377F0683):
                return
            salt = hdr[16:24]
            if salt != self.wal_salt:                     # 新的一代：之前盖上去的先揭掉
                self._restore_overlay(base, view)
                self.wal_salt = salt
                self.wal_idx = 0
                self.wal_big = bool(magic & 1)
                ck = wal_cksum(hdr[:24], 0, 0, self.wal_big)
                self.check_ck = ck == struct.unpack(">II", hdr[24:32])
                if not self.check_ck:
                    log.debug("%s：wal 头校验和对不上，改为只靠 salt + HMAC 判断帧", self.src.name)
                self.wal_ck = ck
            size = self.wal.stat().st_size
            total = (size - WAL_HDR) // FRAME
            i, ck = self.wal_idx, self.wal_ck
            pending: list[tuple[int, bytes]] = []
            w.seek(WAL_HDR + i * FRAME)
            while i < total:
                fr = w.read(FRAME)
                if len(fr) < FRAME:
                    break
                fh, page = fr[:FRAME_HDR], fr[FRAME_HDR:]
                if fh[8:16] != salt:
                    break
                pgno, commit = struct.unpack(">II", fh[:8])
                if self.check_ck:
                    ck2 = wal_cksum(fh[:8] + page, ck[0], ck[1], self.wal_big)
                    if ck2 != struct.unpack(">II", fh[16:24]):
                        if i == 0 and pgno and page_ok(self.mk, page, pgno):
                            # 第一帧就对不上、但页本身是好的：这个库的校验和算法和标准 SQLite 不一样，只靠 salt + HMAC
                            log.info("%s：wal 校验和和标准算法不一致，改为只靠 salt + 每页 HMAC 判断", self.src.name)
                            self.check_ck = False
                        else:
                            self.dirty = True             # salt 对但校验和不对：正在写，下轮再看
                            break
                    ck = ck2
                if not pgno or not page_ok(self.mk, page, pgno):
                    self.dirty = True                     # 多半是写了一半：下轮再看
                    break
                pending.append((pgno, page))
                i += 1
                if commit:
                    for pg, pp in pending:
                        view.seek((pg - 1) * PAGE)
                        view.write(decrypt_page(self.key, pp, pg))
                        self.overlay.add(pg)
                    pending.clear()
                    self.wal_pages = commit
                    self.wal_idx, self.wal_ck = i, ck

    # ---------------- 对外 ----------------
    def needs_refresh(self) -> bool:
        return self.dirty or (_stat(self.src), _stat(self.wal)) != self.stamp

    def refresh(self, force: bool = False) -> bool:
        """同步一次；内容有变化返回 True。"""
        st = (_stat(self.src), _stat(self.wal))
        if not force and not self.dirty and st == self.stamp:
            return False
        self.dirty = False
        main_changed = self.stamp is None or st[0] != self.stamp[0] or force
        mode_b = "r+b" if self.base_path.exists() else "w+b"
        mode_v = "r+b" if self.view_path.exists() else "w+b"
        with open(self.base_path, mode_b) as base, open(self.view_path, mode_v) as view:
            if main_changed or not self.fp:
                self._sync_main(base, view)
            self._sync_wal(base, view)
            pages = self.wal_pages or self.main_pages
            view.truncate(pages * PAGE)
            view.seek(0)
            h = bytearray(view.read(100))
            if len(h) == 100:
                h[18] = h[19] = 1                          # 副本当作普通（非 WAL）库
                h[28:32] = struct.pack(">I", pages)
                h[92:96] = h[24:28]
                view.seek(0)
                view.write(bytes(h))
        self.stamp = st
        self.version += 1
        return True

    def connect(self):
        import sqlite3
        uri = "file:" + self.view_path.resolve().as_posix() + "?mode=ro&immutable=1"
        conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.text_factory = bytes
        return conn

    def close(self):
        for p in (self.base_path, self.view_path):
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass
