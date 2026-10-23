"""13.1 微信 4.x 群里的图片：本地 msg/attach 里的 .dat 是加密的，这里解开给看图模型看（只读，不改微信的文件）。

V2 格式（4.x）：开头 07 08 'V2' 08 07，接着 AES 段长度、XOR 段长度（都是 4 字节小端）、1 字节标志；
    第 15 字节起 AES-128-ECB 的头部 → 16 字节分隔（跳过）→ 剩下的按 1 字节异或。
钥匙是“账号级”的，所有图片共用一把：code = xwechat/net/kvcomm 下 key_<code>_….statistic 文件名里的第一个数字，
    aes_key = md5(f"{code}{wxid}").hexdigest()[:16]（16 个字符直接当 16 字节用），xor_key = code & 0xFF；
    拿一张图解开头 16 字节、看是不是 JPEG / PNG / GIF 的文件头，对了才用。
    （参考：UpXuu《微信 4.x 图片 dat 加密全解析》、lc044《探究微信 4.0 图片加密方式》）
V1：同样的布局，AES 钥匙固定是 cfcd208495d565ef。V0（3.x）：整个文件按 1 字节异或，钥匙从 JPEG 头 FF D8 猜。
消息里的 packed_info_data 带着图片文件名（32 位十六进制），文件在 msg/attach/<md5(群 id)>/<年-月>/Img/<名>.dat，
    原图没下载时只有缩略图 <名>_t.dat（看图模型够用了）。原图很多是 wxgf（微信自己的 HEVC 格式）：有 ffmpeg 就转成 JPEG，
    没有就用缩略图。
"""
from __future__ import annotations

import glob
import hashlib
import logging
import os
import re
import time
from datetime import datetime
from pathlib import Path

log = logging.getLogger("aigroup.wechat")

MAGIC_V2 = b"\x07\x08V2\x08\x07"
MAGIC_V1 = b"\x07\x08V1\x08\x07"
V1_KEY = b"cfcd208495d565ef"
IMG_HEADS = (b"\xff\xd8\xff", b"\x89PNG", b"GIF8", b"RIFF")


def is_image(b: bytes) -> bool:
    return any(b.startswith(h) for h in IMG_HEADS)


def _aes_ecb(key: bytes, data: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    return Cipher(algorithms.AES(key), modes.ECB()).decryptor().update(data)


def decrypt(data: bytes, aes_key: bytes | None, xor_key: int | None) -> bytes | None:
    """一个 .dat → 图片字节；解不开返回 None。"""
    if data[:6] in (MAGIC_V2, MAGIC_V1):
        key = V1_KEY if data[:6] == MAGIC_V1 else aes_key
        if not key or len(data) < 31:
            return None
        a = int.from_bytes(data[6:10], "little")
        x = int.from_bytes(data[10:14], "little")
        head = _aes_ecb(key, data[15:15 + a]) if a else b""
        tail = data[15 + a + 16:15 + a + 16 + x] if x else b""
        if x and xor_key is None:
            return None
        out = head + bytes(b ^ xor_key for b in tail) if x else head
        if out[:4] == b"wxgf":                           # 微信自己的 HEVC 格式：用 ffmpeg 转成 JPEG
            return wxgf_to_jpeg(out)
        if len(out) > 1 and out[:2] == b"\xff\xd8":      # AES 段可能带 PKCS7 填充：JPEG 不在乎，原样用
            return out
        return out if is_image(out) else None
    if len(data) > 2:                                    # V0：整个文件异或
        k = data[0] ^ 0xFF
        if data[1] ^ k == 0xD8:
            return bytes(b ^ k for b in data)
        k = data[0] ^ 0x89
        if data[1] ^ k == 0x50:
            return bytes(b ^ k for b in data)
    return None


def wxgf_to_jpeg(data: bytes) -> bytes | None:
    """wxgf = 一个小头 + HEVC 码流：从第一个起始码（00 00 00 01）开始交给 ffmpeg 解第一帧。没有 ffmpeg 返回 None。"""
    import shutil
    import subprocess
    import tempfile
    i = data.find(b"\x00\x00\x00\x01")
    if i < 0 or not shutil.which("ffmpeg"):
        return None
    with tempfile.TemporaryDirectory() as td:
        src, dst = os.path.join(td, "a.h265"), os.path.join(td, "o.jpg")
        with open(src, "wb") as f:
            f.write(data[i:])
        try:
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "hevc", "-i", src, "-frames:v", "1", "-q:v", "3", dst],
                           capture_output=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            return None
        return open(dst, "rb").read() if os.path.exists(dst) and os.path.getsize(dst) > 100 else None


def find_codes(data_dir: str | os.PathLike, extra: list[str] | None = None) -> list[int]:
    """kvcomm 目录里 key_<code>_….statistic 的 code（可能有好几个，挨个试）。"""
    d = Path(data_dir)
    roots = [d.parent / "xwechat", d.parent, d, Path.home() / ".xwechat",
             Path(os.environ.get("APPDATA", "")) / "Tencent" / "xwechat", *[Path(x) for x in extra or []]]
    out: list[int] = []
    for r in roots:
        for sub in ("net/kvcomm", "ilink/kvcomm", "*/kvcomm"):
            for p in glob.glob(str(r / sub / "key_*")):
                m = re.match(r"key_(\d+)_", os.path.basename(p))
                if m and int(m.group(1)) not in out:
                    out.append(int(m.group(1)))
    return out


def derive(code: int, wxid: str) -> tuple[bytes, int]:
    wxid = re.sub(r"_[0-9a-f]{4}$", "", wxid)          # 账号目录名 wxid_xxx_b929 → wxid_xxx
    return hashlib.md5(f"{code}{wxid}".encode()).hexdigest()[:16].encode(), code & 0xFF


class ImageStore:
    """按消息里的图片名找到 .dat、解开。钥匙第一次用的时候找（找不到就一直不看图，不影响别的）。"""

    def __init__(self, account_dir: str | os.PathLike, wxid: str, data_dir: str | os.PathLike | None = None,
                 code: int | None = None):
        self.acc = Path(account_dir)
        self.wxid = wxid or self.acc.name
        self.data_dir = Path(data_dir) if data_dir else self.acc.parent
        self.code = code
        self.key: tuple[bytes, int] | None = None
        self._tried = 0.0

    def _sample(self) -> list[Path]:
        fs = sorted(self.acc.glob("msg/attach/*/*/Img/*.dat"), key=lambda p: -p.stat().st_mtime)
        return fs[:6]

    def _find_key(self) -> tuple[bytes, int] | None:
        if self.key or time.time() - self._tried < 600:
            return self.key
        self._tried = time.time()
        samples = [p.read_bytes()[:64] for p in self._sample()]
        samples = [s for s in samples if s[:6] == MAGIC_V2]
        codes = [self.code] if self.code else find_codes(self.data_dir)
        for code in codes:
            k = derive(code, self.wxid)
            if not samples or any(is_image(_aes_ecb(k[0], s[15:31])) for s in samples):
                self.key = k
                log.info("微信图片的钥匙找到了（code %s…）", str(code)[:3])
                return k
        if codes or samples:
            log.info("微信图片的钥匙没找到：群里的图先看不了（找到 %d 个候选 code）", len(codes))
        return None

    def path_for(self, chat: str, name: str, ts: float) -> Path | None:
        ps = self.paths_for(chat, name, ts)
        return ps[0] if ps else None

    def paths_for(self, chat: str, name: str, ts: float) -> list[Path]:
        """原图 > 高清 > 缩略图；按消息时间的月份找，找不到再扫别的月份。"""
        if not re.fullmatch(r"[0-9a-f]{32}", name or ""):
            return []
        base = self.acc / "msg" / "attach" / hashlib.md5(chat.encode()).hexdigest()
        months = [datetime.fromtimestamp(ts).strftime("%Y-%m")] if ts else []
        months += [p.name for p in sorted(base.glob("*"), reverse=True) if p.name not in months][:3]
        for mo in months:
            ps = [base / mo / "Img" / f"{name}{suf}.dat" for suf in ("", "_h", "_t")]
            ps = [p for p in ps if p.is_file()]
            if ps:
                return ps
        return []

    def load(self, chat: str, name: str, ts: float) -> bytes | None:
        """原图解不开（比如 wxgf 又没有 ffmpeg）就用缩略图。"""
        for p in self.paths_for(chat, name, ts):
            data = p.read_bytes()
            k = self._find_key() if data[:6] == MAGIC_V2 else None
            out = decrypt(data, k[0] if k else None, k[1] if k else None)
            if out:
                return out
            log.debug("这张图没解开：%s", p.name)
        return None


def packed_image_name(packed) -> str:
    """消息的 packed_info_data 里的图片文件名（32 位十六进制）。"""
    b = packed if isinstance(packed, (bytes, bytearray)) else str(packed or "").encode("latin-1", "ignore")
    m = re.search(rb"(?<![0-9a-f])([0-9a-f]{32})(?![0-9a-f])", bytes(b))
    return m.group(1).decode() if m else ""
