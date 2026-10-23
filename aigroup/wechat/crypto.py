"""微信 4.x 本地数据库（WCDB / SQLCipher 4）的页级解密和校验。

    每页 4096 字节，页尾 80 字节保留区 = IV(16) + HMAC-SHA512(64)。
    第 1 页前 16 字节是 salt（明文），加密区从 16 开始。
    裸密钥（raw key）32 字节直接当 AES-256 密钥；HMAC 密钥 = PBKDF2-SHA512(密钥, salt^0x3a, 2 次)。
    HMAC 覆盖“加密区 + IV + 页号（小端 4 字节）”，所以每页都能单独验真：
    读到微信正写了一半的页（撕裂）会验不过，下次再读。

    参考：SQLCipher 4 文档；wechatauto-replica（Apache-2.0）的 db.py。
"""
from __future__ import annotations

import hashlib
import hmac
import struct

PAGE = 4096
RESERVE = 80
IV_OFF = PAGE - RESERVE            # 4016
MAC_OFF = IV_OFF + 16              # 4032
SQLITE_MAGIC = b"SQLite format 3\x00"


def _aes_cbc_decrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    d = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    return d.update(data) + d.finalize()


def _aes_cbc_encrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    e = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    return e.update(data) + e.finalize()


def mac_key(enc_key: bytes, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha512", enc_key, bytes(b ^ 0x3A for b in salt), 2, dklen=32)


def page_mac(mkey: bytes, page: bytes, pgno: int) -> bytes:
    start = 16 if pgno == 1 else 0
    h = hmac.new(mkey, page[start:MAC_OFF], hashlib.sha512)
    h.update(struct.pack("<I", pgno))
    return h.digest()


def page_ok(mkey: bytes, page: bytes, pgno: int) -> bool:
    return len(page) == PAGE and hmac.compare_digest(page_mac(mkey, page, pgno), page[MAC_OFF:PAGE])


def verify_key(enc_key: bytes, page1: bytes) -> bool:
    """这个 32 字节密钥能不能解开这个库（只看第 1 页的 HMAC，零误报）。"""
    if len(enc_key) != 32 or len(page1) < PAGE:
        return False
    return page_ok(mac_key(enc_key, page1[:16]), page1[:PAGE], 1)


def decrypt_page(enc_key: bytes, page: bytes, pgno: int) -> bytes:
    """一页密文 → 一页明文（保留区填 0；第 1 页开头补回 SQLite 文件头）。"""
    iv = page[IV_OFF:MAC_OFF]
    if pgno == 1:
        return SQLITE_MAGIC + _aes_cbc_decrypt(enc_key, iv, page[16:IV_OFF]) + b"\x00" * RESERVE
    return _aes_cbc_decrypt(enc_key, iv, page[:IV_OFF]) + b"\x00" * RESERVE


def encrypt_page(enc_key: bytes, plain: bytes, pgno: int, salt: bytes, iv: bytes) -> bytes:
    """测试用：明文页 → 和微信一样格式的密文页。"""
    mk = mac_key(enc_key, salt)
    if pgno == 1:
        body = salt + _aes_cbc_encrypt(enc_key, iv, plain[16:IV_OFF])
    else:
        body = _aes_cbc_encrypt(enc_key, iv, plain[:IV_OFF])
    page = body + iv
    start = 16 if pgno == 1 else 0
    h = hmac.new(mk, page[start:MAC_OFF], hashlib.sha512)
    h.update(struct.pack("<I", pgno))
    return page + h.digest()


def probable_key(b: bytes) -> bool:
    """像不像随机密钥（筛掉全 0、指针、ASCII 这类）。"""
    return len(b) == 32 and len(set(b)) >= 14 and b.count(0) <= 3


def open_shared(path):
    """只读打开微信的库文件，而且不挡微信删除 / 改名它（Windows 上 Python 的 open() 不给别人删的权限：
    微信更新时要删 -wal 或者挪库，正好碰上我们在读就会失败）。别的系统就是普通 open。"""
    import sys
    if sys.platform != "win32":
        return open(path, "rb")
    try:
        return _open_shared_win(path)
    except FileNotFoundError:
        raise
    except Exception:  # noqa: BLE001                      # 万一这条路走不通：退回普通 open（只是不放行删除）
        return open(path, "rb")


def _open_shared_win(path):
    import ctypes
    import msvcrt
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                                wintypes.DWORD, wintypes.HANDLE]
    k32.CreateFileW.restype = wintypes.HANDLE
    h = k32.CreateFileW(str(path), 0x80000000, 0x1 | 0x2 | 0x4, None, 3, 0x80, None)   # GENERIC_READ；共享读写删；OPEN_EXISTING
    if not h or h == wintypes.HANDLE(-1).value:
        err = ctypes.get_last_error()
        if err in (2, 3):
            raise FileNotFoundError(str(path))
        raise OSError(f"打不开 {path}（错误码 {err}）")
    import os
    fd = msvcrt.open_osfhandle(h, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    return os.fdopen(fd, "rb")
