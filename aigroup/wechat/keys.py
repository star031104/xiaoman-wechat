"""拿微信数据库的密钥：只读微信进程内存（ReadProcessMemory），不注入、不 hook、不改微信的任何东西。

    每个库有自己的 32 字节密钥。拿到后存进 data/wechat/keys.json；微信更新不会重新加密库，
    所以存下来的密钥跨版本一直能用（每次启动都用库第 1 页的 HMAC 再验一遍，验不过才重新找）。

    找密钥的办法按顺序试，每一步拿到的都要过 HMAC 校验（零误报）：
    1. 缓存
    2. Config.Cipher：微信 4.1 把每个库的 x'密钥+salt' 配置异或后放在 com.Tencent.WCDB.Config.Cipher 对象里。
       结构偏移是在 wxid/wechatauto-replica（Apache-2.0）的基础上放宽成“附近扫一遍”，小版本挪了位置也能找到
    3. 明文 x'…' 串（4.0.x 的老办法，以防哪天又变回去）
    4. 深度扫描（python run.py wxkeys --deep）：把微信堆内存里每个像密钥的 32 字节都拿来试 HMAC。
       最慢（几十秒到几分钟，用上所有 CPU），但不依赖任何版本细节——前面都失败时的兜底
"""
from __future__ import annotations

import json
import logging
import os
import re
import struct
import sys
import time
from pathlib import Path

from aigroup.wechat.crypto import PAGE, mac_key, open_shared, probable_key, verify_key

log = logging.getLogger("aigroup.wechat.keys")

CIPHER_NAME = b"com.Tencent.WCDB.Config.Cipher"
XOR_MASK = bytes.fromhex("d2c7442458020000004889442450488b450048844c2448488944254048584c24")
HEX_RE = re.compile(rb"[xX]'([0-9a-fA-F]{64,192})'")
NEEDED = ("message/message_", "contact/contact.db", "session/session.db")


# ---------------- 库文件 ----------------
def db_files(account_dir: str | os.PathLike) -> dict[str, Path]:
    """账号目录下所有加密库：相对路径（用 / 分隔）→ 绝对路径。"""
    base = Path(account_dir) / "db_storage"
    out = {}
    if not base.is_dir():
        return out
    for p in base.rglob("*.db"):
        rel = p.relative_to(base).as_posix()
        if rel.startswith("migrate/"):
            continue
        out[rel] = p
    return out


def page1(path: Path) -> bytes:
    try:
        with open_shared(path) as f:
            return f.read(PAGE)
    except OSError:
        return b""


def needed(rel: str) -> bool:
    return any(rel.startswith(n) for n in NEEDED)


class KeyChecker:
    """把候选密钥对每个库的第 1 页验 HMAC。"""

    def __init__(self, files: dict[str, Path]):
        self.files = files
        self.p1 = {rel: page1(p) for rel, p in files.items()}
        self.p1 = {k: v for k, v in self.p1.items() if len(v) == PAGE}
        self.tried: set[bytes] = set()
        self.phrases: list[bytes] = []
        self.stats: dict = {}            # 从 x'…' 里抠出来、当裸密钥验不过的（可能是口令，见 strategy_passphrase）

    def check(self, cand: bytes, keys: dict[str, bytes], phrase: bool = False) -> int:
        if cand in self.tried or not probable_key(cand):
            return 0
        self.tried.add(cand)
        hit = 0
        for rel, pg in self.p1.items():
            if rel not in keys and verify_key(cand, pg):
                keys[rel] = cand
                hit += 1
        if not hit and phrase and len(self.phrases) < 32:
            self.phrases.append(cand)
        return hit

    def missing(self, keys: dict[str, bytes]) -> list[str]:
        return [r for r in self.p1 if r not in keys]


# ---------------- 缓存 ----------------
def load_cache(path: Path, account: str) -> dict[str, bytes]:
    try:
        d = json.loads(Path(path).read_text(encoding="utf-8"))
        return {rel: bytes.fromhex(h) for rel, h in (d.get(account) or {}).items()}
    except (OSError, ValueError, AttributeError):
        return {}


def save_cache(path: Path, account: str, keys: dict[str, bytes]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        d = {}
    d[account] = {rel: k.hex() for rel, k in sorted(keys.items())}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


# ---------------- 读进程内存（Windows） ----------------
class ProcMem:
    """只读打开一个进程。regions() 列出可读的内存块；read() 读一段。"""

    MEM_COMMIT, MEM_PRIVATE = 0x1000, 0x20000
    PAGE_GUARD, PAGE_NOACCESS = 0x100, 0x01

    def __init__(self, pid: int):
        import ctypes
        from ctypes import wintypes
        self.ctypes, self.pid = ctypes, pid
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)

        class MBI(ctypes.Structure):
            _fields_ = [("BaseAddress", ctypes.c_void_p), ("AllocationBase", ctypes.c_void_p),
                        ("AllocationProtect", wintypes.DWORD), ("PartitionId", wintypes.WORD),
                        ("RegionSize", ctypes.c_size_t), ("State", wintypes.DWORD),
                        ("Protect", wintypes.DWORD), ("Type", wintypes.DWORD)]
        self.MBI = MBI
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.VirtualQueryEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.POINTER(MBI), ctypes.c_size_t]
        k32.VirtualQueryEx.restype = ctypes.c_size_t
        k32.ReadProcessMemory.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t,
                                          ctypes.POINTER(ctypes.c_size_t)]
        k32.ReadProcessMemory.restype = wintypes.BOOL
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        self.k32 = k32
        self.h = k32.OpenProcess(0x0010 | 0x0400, False, pid)          # VM_READ | QUERY_INFORMATION
        if not self.h:
            raise PermissionError(f"打不开微信进程 {pid}（错误码 {ctypes.get_last_error()}；"
                                  "微信是用管理员身份开的话，这个程序也要用管理员身份运行）")

    def regions(self, private_only: bool = False, max_size: int = 0x10000000):
        c = self.ctypes
        addr = 0
        mbi = self.MBI()
        while addr < 0x7FFFFFFFFFFF:
            if not self.k32.VirtualQueryEx(self.h, c.c_void_p(addr), c.byref(mbi), c.sizeof(mbi)):
                break
            base, size = mbi.BaseAddress or 0, mbi.RegionSize
            if (mbi.State == self.MEM_COMMIT and not (mbi.Protect & self.PAGE_GUARD)
                    and (mbi.Protect & 0xFF) & 0xEE and 0 < size <= max_size
                    and (not private_only or mbi.Type == self.MEM_PRIVATE)):
                yield base, size
            addr = base + size
            if size == 0:
                break

    def read(self, addr: int, n: int) -> bytes | None:
        c = self.ctypes
        if n <= 0 or not (0x10000 <= addr < 0x7FFFFFFFFFFF):
            return None
        buf = c.create_string_buffer(n)
        got = c.c_size_t(0)
        if self.k32.ReadProcessMemory(self.h, c.c_void_p(addr), buf, n, c.byref(got)) and got.value:
            return buf.raw[:got.value]
        return None

    def close(self):
        if self.h:
            self.k32.CloseHandle(self.h)
            self.h = None


_WIN_PROCMEM = ProcMem


def weixin_procs() -> list[tuple[int, int, int]]:
    """所有 Weixin.exe 进程：(pid, 父进程 pid, 线程数)（用系统快照，不调 tasklist）。"""
    if sys.platform != "win32":
        return []
    import ctypes
    from ctypes import wintypes

    class PE(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_void_p), ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                    ("szExeFile", ctypes.c_wchar * 260)]
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PE)]
    k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PE)]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    snap = k32.CreateToolhelp32Snapshot(0x2, 0)
    if not snap or snap == wintypes.HANDLE(-1).value:
        return []
    out = []
    try:
        pe = PE()
        pe.dwSize = ctypes.sizeof(PE)
        ok = k32.Process32FirstW(snap, ctypes.byref(pe))
        while ok:
            if pe.szExeFile.lower() == "weixin.exe":
                out.append((pe.th32ProcessID, pe.th32ParentProcessID, pe.cntThreads))
            ok = k32.Process32NextW(snap, ctypes.byref(pe))
    finally:
        k32.CloseHandle(snap)
    return out


def weixin_pids() -> list[int]:
    """所有 Weixin.exe 进程。主进程（线程最多）排在最前。"""
    return [p for p, _, _ in sorted(weixin_procs(), key=lambda x: -x[2])]


def instances(procs: list[tuple[int, int, int]] | None = None) -> list[list[int]]:
    """按“开了几个微信”分组：同一个微信的主进程和它的子进程放一组（每组主进程在前）。
    开了两个微信（大号 + 小号）就是两组。"""
    procs = weixin_procs() if procs is None else procs
    parent = {p: pp for p, pp, _ in procs}
    threads = {p: t for p, _, t in procs}

    def root(p):
        seen = set()
        while parent.get(p) in parent and p not in seen:       # 父进程也是 Weixin.exe：往上找
            seen.add(p)
            p = parent[p]
        return p
    groups: dict[int, list[int]] = {}
    for p in parent:
        groups.setdefault(root(p), []).append(p)
    return [sorted(g, key=lambda x: (x != r, -threads.get(x, 0))) for r, g in
            sorted(groups.items(), key=lambda kv: -sum(threads.get(x, 0) for x in kv[1]))]


def file_users(paths: list[str | os.PathLike]) -> list[int] | None:
    """哪些进程开着这些文件（Windows 的 Restart Manager，和“文件被占用”提示用的是同一个）。
    出错返回 None（= 不知道），没人开着返回 []。"""
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes
    paths = [str(x) for x in paths if Path(x).exists()]
    if not paths:
        return []

    class RM_UNIQUE_PROCESS(ctypes.Structure):
        _fields_ = [("dwProcessId", wintypes.DWORD), ("ProcessStartTime", wintypes.FILETIME)]

    class RM_PROCESS_INFO(ctypes.Structure):
        _fields_ = [("Process", RM_UNIQUE_PROCESS), ("strAppName", ctypes.c_wchar * 256),
                    ("strServiceShortName", ctypes.c_wchar * 64), ("ApplicationType", ctypes.c_int),
                    ("AppStatus", wintypes.ULONG), ("TSSessionId", wintypes.DWORD), ("bRestartable", wintypes.BOOL)]
    try:
        rm = ctypes.WinDLL("rstrtmgr")
    except OSError:
        return None
    h = wintypes.DWORD()
    key = ctypes.create_unicode_buffer(64)
    if rm.RmStartSession(ctypes.byref(h), 0, key) != 0:
        return None
    try:
        arr = (wintypes.LPCWSTR * len(paths))(*paths)
        if rm.RmRegisterResources(h, len(paths), arr, 0, None, 0, None) != 0:
            return None
        need, n, reason = wintypes.UINT(0), wintypes.UINT(0), wintypes.DWORD(0)
        r = rm.RmGetList(h, ctypes.byref(need), ctypes.byref(n), None, ctypes.byref(reason))
        if r == 0:
            return []
        if r != 234:                                              # ERROR_MORE_DATA
            return None
        buf = (RM_PROCESS_INFO * (need.value + 4))()
        n = wintypes.UINT(need.value + 4)
        if rm.RmGetList(h, ctypes.byref(need), ctypes.byref(n), buf, ctypes.byref(reason)) != 0:
            return None
        return [int(buf[i].Process.dwProcessId) for i in range(n.value)]
    finally:
        rm.RmEndSession(h)


def _db_files_of(account_dir: Path) -> list[Path]:
    dbs = Path(account_dir) / "db_storage"
    return [dbs / "contact" / "contact.db", dbs / "session" / "session.db", dbs / "message" / "message_0.db"]


def owner_pids(account_dir: str | os.PathLike, procs: list[tuple[int, int, int]] | None = None) -> list[int] | None:
    """登录着这个账号的那个微信的进程（主进程在前）。开了两个微信时靠它分清哪个是她的。
    = 开着这个号的库文件的 Weixin.exe + 它的子进程；它的父进程也是 Weixin.exe、而且没开着别的号的库时也算上
    （窗口可能在父进程里）。认不出来返回 None；这个号没在任何一个微信里登录返回 []。"""
    account_dir = Path(account_dir)
    users = file_users(_db_files_of(account_dir))
    if users is None:
        return None
    procs = weixin_procs() if procs is None else procs
    parent = {p: pp for p, pp, _ in procs}
    threads = {p: t for p, _, t in procs}
    hold = [u for u in users if u in parent]
    if not hold:
        return []
    others: set[int] = set()                                   # 开着别的号的库的进程（你的大号）
    try:
        for sib in account_dir.parent.iterdir():
            if sib != account_dir and (sib / "db_storage").is_dir():
                others |= set(file_users(_db_files_of(sib)) or [])
    except OSError:
        pass
    own = set(hold)
    for h in hold:                                             # 往上：父进程也是 Weixin.exe、没开着别的号
        p = parent.get(h)
        while p in parent and p not in others and p not in own:
            own.add(p)
            p = parent.get(p)
    changed = True
    while changed:                                             # 往下：这些进程的子进程
        changed = False
        for p, pp in parent.items():
            if pp in own and p not in own and p not in others:
                own.add(p)
                changed = True
    return sorted(own, key=lambda x: -threads.get(x, 0))


def find_all(mem, needles: list[bytes], private_only: bool = False) -> dict[bytes, list[int]]:
    """一次扫完所有可读内存，找每个 needle 出现的地址。"""
    hits: dict[bytes, list[int]] = {n: [] for n in needles}
    for base, size in mem.regions(private_only):
        buf = mem.read(base, size)
        if not buf:
            continue
        for n in needles:
            pos = buf.find(n)
            while pos >= 0:
                hits[n].append(base + pos)
                pos = buf.find(n, pos + 1)
    return hits


def _hex_runs(text: bytes):
    """x'…' 里的十六进制串 → 候选 (密钥, salt 或 None)。"""
    for m in HEX_RE.finditer(text):
        run = m.group(1).decode().lower()
        starts = {0, max(0, len(run) - 64)}
        if len(run) > 96:
            starts |= set(range(0, len(run) - 63, 32))
        for s in sorted(starts):
            if s + 64 <= len(run):
                yield bytes.fromhex(run[s:s + 64])


def _xor(blob: bytes, mask: bytes = XOR_MASK) -> bytes:
    return bytes(v ^ mask[i % len(mask)] for i, v in enumerate(blob))


def strategy_cipher_config(mem, chk: KeyChecker, keys: dict[str, bytes]) -> int:
    """Config.Cipher 对象 → 异或解出 x'…' → 验 HMAC。"""
    u64 = lambda b, o: struct.unpack_from("<Q", b, o)[0]  # noqa: E731
    names = find_all(mem, [CIPHER_NAME])[CIPHER_NAME]
    st = chk.stats = {"名字串": len(names), "引用": 0, "配置块": 0, "候选": 0}     # 0 个找到时看是哪一步断了
    if not names:
        return 0
    pairs = [struct.pack("<QQ", a, len(CIPHER_NAME)) for a in names]
    refs = find_all(mem, pairs)
    st["引用"] = sum(len(v) for v in refs.values())
    found = 0
    seen_blob: set[int] = set()
    for pair, addrs in refs.items():
        for q in addrs:
            node = (mem.read(q - 0x40, 0x40) or b"") + (mem.read(q, 0x80) or b"")   # 引用处前后都看看（偏移随版本变）
            if len(node) < 0x40:
                continue
            ptrs = []
            for o in range(0, len(node) - 7, 8):
                p = u64(node, o)
                if 0x10000 <= p < 0x7FFFFFFFFFFF:
                    ptrs.append(p)
            for cp in ptrs:
                obj = mem.read(cp, 0x200)
                if not obj or len(obj) < 0x20:
                    continue
                for o in range(0, len(obj) - 0x17, 8):
                    for dp_off, dl_off in ((o, o + 8), (o, o + 16)):
                        if dl_off + 8 > len(obj):
                            continue
                        dp, dl = u64(obj, dp_off), u64(obj, dl_off)
                        if not (70 <= dl <= 1024 and 0x10000 <= dp < 0x7FFFFFFFFFFF) or dp in seen_blob:
                            continue
                        blob = mem.read(dp, int(dl))
                        if not blob or len(blob) != dl:
                            continue
                        seen_blob.add(dp)
                        for text in (_xor(blob), blob):
                            for cand in _hex_runs(text):
                                st["候选"] += 1
                                st["配置块"] = len(seen_blob)
                                found += chk.check(cand, keys, phrase=True)
                if not chk.missing(keys):
                    return found
    return found


def strategy_plain_hex(mem, chk: KeyChecker, keys: dict[str, bytes]) -> int:
    found = 0
    for base, size in mem.regions():
        buf = mem.read(base, size)
        if not buf or b"x'" not in buf and b"X'" not in buf:
            continue
        for cand in _hex_runs(buf):
            found += chk.check(cand, keys, phrase=True)
        if not chk.missing(keys):
            break
    return found


def strategy_passphrase(chk: KeyChecker, keys: dict[str, bytes], progress=None) -> int:
    """有的文章说 4.1 内存里放的是“口令”而不是裸密钥：库密钥 = PBKDF2-SHA512(口令, 库头 16 字节 salt, 256000 次)。
    对 x'…' 里抠出来、当裸密钥验不过的候选再这样派生一遍（每个约 0.2 秒，只在前面都没找到时才试）。"""
    import hashlib
    miss = [r for r in chk.missing(keys) if needed(r)]
    if not miss or not chk.phrases:
        return 0
    if progress:
        progress(f"试口令派生：{len(chk.phrases)} 个候选 × {len(miss)} 个库（可能要一分钟）…")
    found = 0
    for cand in chk.phrases:
        for pw in (cand, cand.hex().encode()):
            for rel in [r for r in miss if r not in keys]:
                pg = chk.p1[rel]
                k = hashlib.pbkdf2_hmac("sha512", pw, pg[:16], 256000, dklen=32)
                if verify_key(k, pg):
                    keys[rel] = k
                    found += 1
                    # 同一个口令多半能解开所有库：剩下的也用它试
                    for r2 in [r for r in chk.missing(keys)]:
                        k2 = hashlib.pbkdf2_hmac("sha512", pw, chk.p1[r2][:16], 256000, dklen=32)
                        if verify_key(k2, chk.p1[r2]):
                            keys[r2] = k2
                            found += 1
                    return found
    return found


# ---------------- 深度扫描 ----------------
def _deep_worker(args):
    pid, regions, targets, stride = args
    import numpy as np

    from aigroup.wechat.crypto import page_ok
    Mem = ProcMem
    if Mem is _WIN_PROCMEM and sys.platform.startswith("linux"):   # 子进程是重新 import 的：按系统选
        from aigroup.wechat.linux import ProcMem as Mem
    mem = Mem(pid)
    out = {}
    try:
        todo = dict(targets)                           # rel → (salt, page1)
        for base, size in regions:
            buf = mem.read(base, size)
            if not buf or len(buf) < 64:
                continue
            a = np.frombuffer(buf[:len(buf) // 8 * 8], dtype=np.uint8).reshape(-1, 8)
            z = (a == 0).sum(1)
            pr = ((a >= 0x20) & (a < 0x7F)).all(1)
            n = len(z) - 3
            if n <= 0:
                continue
            zz = z[:n] + z[1:n + 1] + z[2:n + 2] + z[3:n + 3]
            pp = pr[:n] | pr[1:n + 1] | pr[2:n + 2] | pr[3:n + 3]
            ok = (zz <= 2) & ~pp
            if stride == 16:
                ok[1::2] = False
            for i in np.nonzero(ok)[0]:
                cand = buf[i * 8: i * 8 + 32]
                if len(set(cand)) < 20:
                    continue
                for rel, (salt, pg) in list(todo.items()):
                    if page_ok(mac_key(cand, salt), pg, 1):
                        out[rel] = cand.hex()
                        del todo[rel]
                if not todo:
                    return out
    finally:
        mem.close()
    return out


def strategy_deep(pid: int, chk: KeyChecker, keys: dict[str, bytes], stride: int = 8, progress=print) -> int:
    """不依赖任何版本细节：把每个像密钥的 32 字节都拿来验。需要 numpy；用上所有 CPU。"""
    import multiprocessing as mp
    miss = [r for r in chk.missing(keys) if needed(r)] or chk.missing(keys)
    if not miss:
        return 0
    targets = {r: (chk.p1[r][:16], chk.p1[r]) for r in miss}
    mem = ProcMem(pid)
    try:
        regs = list(mem.regions(private_only=True))
    finally:
        mem.close()
    total = sum(s for _, s in regs)
    progress(f"深度扫描：{len(regs)} 块内存，共 {total / 1e6:.0f} MB，找 {len(miss)} 个库的密钥（可能要几分钟）…")
    ncpu = max(1, (os.cpu_count() or 2) - 1)
    chunks: list[list] = [[] for _ in range(ncpu * 4)]
    sizes = [0] * len(chunks)
    for r in sorted(regs, key=lambda x: -x[1]):
        i = sizes.index(min(sizes))
        chunks[i].append(r)
        sizes[i] += r[1]
    found = 0
    t0 = time.time()
    with mp.get_context("spawn").Pool(ncpu) as pool:
        done = 0
        for res in pool.imap_unordered(_deep_worker, [(pid, c, targets, stride) for c in chunks if c]):
            done += 1
            for rel, h in res.items():
                k = bytes.fromhex(h)
                if rel not in keys:
                    found += chk.check(k, keys) or 0
            progress(f"  {done}/{len([c for c in chunks if c])}（{time.time() - t0:.0f} 秒，已找到 {found} 个）")
            if not [r for r in miss if r not in keys]:
                pool.terminate()
                break
    return found


# ---------------- 总入口 ----------------
def get_keys(account_dir: str | os.PathLike, account: str, cache: Path, deep: bool = False,
             progress=None) -> tuple[dict[str, bytes], list[str]]:
    """返回 (能用的密钥, 说明)。只缓存验过的。"""
    say = progress or (lambda s: None)
    files = db_files(account_dir)
    chk = KeyChecker(files)
    notes: list[str] = []
    keys: dict[str, bytes] = {}
    for rel, k in load_cache(cache, account).items():
        if rel in chk.p1 and verify_key(k, chk.p1[rel]):
            keys[rel] = k
    if keys:
        notes.append(f"缓存里的密钥能用 {len(keys)} 个")
    if [r for r in chk.missing(keys) if needed(r)]:
        pids = weixin_pids()
        own = owner_pids(account_dir)
        if own:                                               # 开了好几个微信：先找登录着这个号的那个
            pids = own + [p for p in pids if p not in own]
        if not pids:
            notes.append("微信没在运行：先把微信打开、登录好")
        for pid in pids[:4]:
            try:
                mem = ProcMem(pid)
            except PermissionError as e:
                notes.append(str(e))
                continue
            try:
                for name, fn in (("Config.Cipher", strategy_cipher_config), ("明文 x'…'", strategy_plain_hex)):
                    t0 = time.time()
                    try:
                        n = fn(mem, chk, keys)
                    except Exception as e:  # noqa: BLE001
                        notes.append(f"{name} 出错：{type(e).__name__}: {e}")
                        continue
                    say(f"{name}：找到 {n} 个（{time.time() - t0:.1f} 秒）")
                    if not n and fn is strategy_cipher_config and chk.stats:
                        notes.append("Config.Cipher 这一路：" + "、".join(f"{k} {v}" for k, v in chk.stats.items()))
                    if n:
                        notes.append(f"进程 {pid} 用 {name} 找到 {n} 个")
                    if not [r for r in chk.missing(keys) if needed(r)]:
                        break
            finally:
                mem.close()
            if not [r for r in chk.missing(keys) if needed(r)]:
                break
        if [r for r in chk.missing(keys) if needed(r)] and chk.phrases:
            try:
                n = strategy_passphrase(chk, keys, progress=say)
                if n:
                    notes.append(f"按口令派生找到 {n} 个")
            except Exception as e:  # noqa: BLE001
                notes.append(f"口令派生出错：{type(e).__name__}: {e}")
        if deep and pids and [r for r in chk.missing(keys) if needed(r)]:
            try:
                n = strategy_deep(pids[0], chk, keys, progress=say)
                notes.append(f"深度扫描找到 {n} 个")
            except ImportError:
                notes.append("深度扫描要 numpy：pip install numpy")
            except Exception as e:  # noqa: BLE001
                notes.append(f"深度扫描出错：{type(e).__name__}: {e}")
    if keys:
        save_cache(cache, account, keys)
    miss = [r for r in chk.missing(keys) if needed(r)]
    if miss:
        notes.append("还缺：" + "、".join(miss))
    return keys, notes

