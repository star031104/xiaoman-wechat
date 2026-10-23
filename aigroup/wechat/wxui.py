"""往微信里发消息：只用 Windows 的普通窗口操作（找窗口、切到前台、剪贴板、按键），不碰微信进程。

    最稳的用法：把每个群在微信里“单独窗口打开”（会话列表里双击这个群）。窗口标题就是群名，
    这里按标题找到窗口 → 切前台 → 点一下输入框 → 粘贴 → 回车。发完把前台还给你原来的窗口。
    没有单独窗口时可以退回“主窗口 Ctrl+F 搜群名 → 回车打开 → 粘贴发送”（setup 会检查群名不会搜错）。
    发没发出去、发到了哪，由上层去本地数据库里核对（看得见自己刚发的那条才算发出去）。

    几个保护：
    - 你正在用电脑（键盘鼠标最近 idle_seconds 秒内有动作）就先等等，免得抢你的键盘
    - 每次按键前都确认前台还是微信那个窗口，不是就放弃这次发送（绝不把字打进别的程序）
    - 剪贴板里原来的文字发完还原
    - 屏幕锁了发不了（Windows 锁屏时不许模拟按键）：可以关显示器，但别锁屏
"""
from __future__ import annotations

import logging
import os
import sys
import time

log = logging.getLogger("aigroup.wechat.ui")


class UIError(RuntimeError):
    pass


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    try:
        ctypes.WinDLL("shcore").SetProcessDpiAwareness(2)
    except Exception:  # noqa: BLE001
        try:
            user32.SetProcessDPIAware()
        except Exception:  # noqa: BLE001
            pass

    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetAncestor.restype = wintypes.HWND
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindow.argtypes = [wintypes.HWND]
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.BringWindowToTop.argtypes = [wintypes.HWND]
    user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user32.SetClipboardData.restype = wintypes.HANDLE
    user32.GetClipboardData.argtypes = [wintypes.UINT]
    user32.GetClipboardData.restype = wintypes.HANDLE
    user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
    user32.keybd_event.argtypes = [wintypes.BYTE, wintypes.BYTE, wintypes.DWORD, ctypes.c_void_p]
    user32.GetDpiForWindow.argtypes = [wintypes.HWND]
    user32.GetDpiForWindow.restype = wintypes.UINT
    user32.OpenInputDesktop.restype = wintypes.HANDLE
    user32.CloseDesktop.argtypes = [wintypes.HANDLE]
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalSize.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalSize.restype = ctypes.c_size_t
    user32.EnumClipboardFormats.argtypes = [wintypes.UINT]
    user32.EnumClipboardFormats.restype = wintypes.UINT
    kernel32.GetCurrentThreadId.restype = wintypes.DWORD
    kernel32.GetTickCount.restype = wintypes.DWORD

    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                    ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

    class _INPUTU(ctypes.Union):
        _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("pad", ctypes.c_byte * 32)]

    class INPUT(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("u", _INPUTU)]

    user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]

VK = {"ctrl": 0x11, "shift": 0x10, "alt": 0x12, "enter": 0x0D, "a": 0x41, "v": 0x56, "f": 0x46, "esc": 0x1B}
CF_UNICODETEXT, CF_HDROP = 13, 15


# ---------------- 窗口 ----------------
def windows(pids: list[int] | None = None) -> list[dict]:
    """微信进程的所有顶层窗口：hwnd、标题、类名、位置、是否可见 / 最小化。"""
    if sys.platform != "win32":
        return []
    if pids is None:
        from aigroup.wechat.keys import weixin_pids
        pids = weixin_pids()
    pidset = set(pids)
    out = []

    def cb(h, _):
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(h, ctypes.byref(pid))
        if pid.value in pidset:
            t = ctypes.create_unicode_buffer(256)
            c = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(h, t, 256)
            user32.GetClassNameW(h, c, 256)
            r = wintypes.RECT()
            user32.GetWindowRect(h, ctypes.byref(r))
            out.append({"hwnd": h, "title": t.value, "cls": c.value, "pid": pid.value,
                        "rect": (r.left, r.top, r.right, r.bottom), "visible": bool(user32.IsWindowVisible(h)),
                        "iconic": bool(user32.IsIconic(h))})
        return True
    user32.EnumWindows(WNDENUMPROC(cb), 0)
    return out


MAIN_TITLES = ("微信", "Weixin", "WeChat")


def main_window(ws: list[dict] | None = None) -> dict | None:
    ws = ws if ws is not None else windows()
    cand = [w for w in ws if w["title"] in MAIN_TITLES and (w["visible"] or w["iconic"])]
    if not cand:
        return None

    def area(w):
        l, t, r, b = w["rect"]
        return (r - l) * (b - t)
    return max(cand, key=area)


def title_for(titles: dict, chat: str, name: str) -> str:
    """这个群的单独窗口标题：window_titles 里按群 id（或群名）写了就用它，否则就是群名。"""
    t = dict(titles or {})
    return str(t.get(chat) or t.get(name) or name or "")


def chat_windows(name: str, ws: list[dict] | None = None) -> list[dict]:
    """标题就是这个群名的单独聊天窗口（不要“聊天记录”窗口）。"""
    ws = ws if ws is not None else windows()
    name = (name or "").strip()
    if not name:
        return []
    return [w for w in ws if w["title"].strip() == name and (w["visible"] or w["iconic"]) and "聊天记录" not in w["title"]]


def chat_window(name: str, ws: list[dict] | None = None) -> dict | None:
    """单独打开的聊天窗口：标题就是群名（不要“聊天记录”窗口）。"""
    hits = chat_windows(name, ws)
    return hits[0] if hits else None


# ---------------- 输入 ----------------
def idle_seconds() -> float:
    li = LASTINPUTINFO()
    li.cbSize = ctypes.sizeof(li)
    user32.GetLastInputInfo(ctypes.byref(li))
    return ((kernel32.GetTickCount() - li.dwTime) & 0xFFFFFFFF) / 1000.0


def screen_locked() -> bool:
    """锁屏（或者 UAC 弹窗）时输入桌面切走了，打不开。"""
    h = user32.OpenInputDesktop(0, False, 0x0100)          # DESKTOP_SWITCHDESKTOP
    if not h:
        return True
    user32.CloseDesktop(h)
    return False


def _key(vk: int, up: bool = False):
    user32.keybd_event(vk, 0, 2 if up else 0, None)


def press(*names: str, hold: float = 0.03):
    vks = [VK[n] for n in names]
    for v in vks:
        _key(v)
        time.sleep(hold)
    for v in reversed(vks):
        _key(v, True)
        time.sleep(0.01)


def foreground_root() -> int:
    h = user32.GetForegroundWindow()
    if not h:
        return 0
    return user32.GetAncestor(h, 2) or h                    # GA_ROOT：只认这一个窗口（主窗口在前台不算单独窗口在前台）


def focus(hwnd: int, timeout: float = 2.0) -> bool:
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)                          # SW_RESTORE
    fg = user32.GetForegroundWindow()
    me = kernel32.GetCurrentThreadId()
    other = user32.GetWindowThreadProcessId(fg, None) if fg else 0
    end = time.monotonic() + timeout
    tries = 0
    while time.monotonic() < end:
        if tries:                                           # 第一次直接切；切不过去再按一下 Alt 让系统放行
            _key(VK["alt"])
            _key(VK["alt"], True)
        tries += 1
        attached = bool(other and other != me and user32.AttachThreadInput(me, other, True))
        try:
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
        finally:
            if attached:
                user32.AttachThreadInput(me, other, False)
        time.sleep(0.15)
        if foreground_root() == (user32.GetAncestor(hwnd, 2) or hwnd):
            return True
    return False


def click(x: int, y: int):
    """SendInput 绝对坐标单击（微信的渲染层对 SendInput 有反应）。"""
    vx, vy = user32.GetSystemMetrics(76), user32.GetSystemMetrics(77)
    vw, vh = max(1, user32.GetSystemMetrics(78)), max(1, user32.GetSystemMetrics(79))
    ax = int((x - vx) * 65535 / max(1, vw - 1))
    ay = int((y - vy) * 65535 / max(1, vh - 1))
    pt = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    flags = 0x0001 | 0x8000 | 0x4000                        # MOVE | ABSOLUTE | VIRTUALDESK
    seq = []
    for f in (flags, 0x0002 | 0x8000 | 0x4000, 0x0004 | 0x8000 | 0x4000):
        i = INPUT()
        i.type = 0
        i.u.mi = MOUSEINPUT(ax, ay, 0, f, 0, 0)
        seq.append(i)
    for i in seq:
        user32.SendInput(1, ctypes.byref(i), ctypes.sizeof(INPUT))
        time.sleep(0.03)
    user32.SetCursorPos(pt.x, pt.y)


def dpi_scale(hwnd: int) -> float:
    try:
        return max(1.0, user32.GetDpiForWindow(hwnd) / 96.0)
    except Exception:  # noqa: BLE001
        return 1.0


# ---------------- 剪贴板 ----------------
def _open_clip():
    for _ in range(20):
        if user32.OpenClipboard(None):
            return True
        time.sleep(0.05)
    raise UIError("剪贴板被别的程序占着")


def clip_get_text() -> str | None:
    _open_clip()
    try:
        if not user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
            return None
        h = user32.GetClipboardData(CF_UNICODETEXT)
        if not h:
            return None
        p = kernel32.GlobalLock(h)
        try:
            return ctypes.wstring_at(p)
        finally:
            kernel32.GlobalUnlock(h)
    finally:
        user32.CloseClipboard()


_GDI_FORMATS = {2, 3, 9, 14, 0x0080, 0x0082, 0x0083, 0x008E}   # 位图 / 元文件 / 调色板这类不是内存块的，Windows 会从别的格式自动合成


def clip_save() -> list[tuple[int, bytes]]:
    """把剪贴板里现在的东西（文字、图片、复制的文件……能存的格式都存）拷一份，发完原样放回去。"""
    out: list[tuple[int, bytes]] = []
    _open_clip()
    try:
        fmt = user32.EnumClipboardFormats(0)
        while fmt:
            if fmt not in _GDI_FORMATS:
                h = user32.GetClipboardData(fmt)
                if h:
                    n = kernel32.GlobalSize(h)
                    p = kernel32.GlobalLock(h) if n else None
                    if p:
                        try:
                            out.append((fmt, ctypes.string_at(p, n)))
                        finally:
                            kernel32.GlobalUnlock(h)
            fmt = user32.EnumClipboardFormats(fmt)
    finally:
        user32.CloseClipboard()
    return out


def clip_restore(items: list[tuple[int, bytes]]):
    _open_clip()
    try:
        user32.EmptyClipboard()
        for fmt, data in items:
            h = kernel32.GlobalAlloc(0x0042, max(1, len(data)))
            p = kernel32.GlobalLock(h) if h else None
            if not p:
                continue
            ctypes.memmove(p, data, len(data))
            kernel32.GlobalUnlock(h)
            if not user32.SetClipboardData(fmt, h):
                kernel32.GlobalFree(h)
    finally:
        user32.CloseClipboard()


def _clip_set(fmt: int, data: bytes):
    _open_clip()
    try:
        user32.EmptyClipboard()
        h = kernel32.GlobalAlloc(0x0042, len(data))          # GMEM_MOVEABLE | ZEROINIT
        p = kernel32.GlobalLock(h) if h else None
        if not p:
            raise UIError("剪贴板内存分配失败")
        ctypes.memmove(p, data, len(data))
        kernel32.GlobalUnlock(h)
        if not user32.SetClipboardData(fmt, h):
            kernel32.GlobalFree(h)
            raise UIError("写剪贴板失败")
    finally:
        user32.CloseClipboard()


def clip_set_text(text: str):
    _clip_set(CF_UNICODETEXT, (text + "\x00").encode("utf-16-le"))


def clip_set_files(paths: list[str]):
    import struct
    files = ("\x00".join(os.path.abspath(p) for p in paths) + "\x00\x00").encode("utf-16-le")
    head = struct.pack("<IiiII", 20, 0, 0, 0, 1)             # DROPFILES，fWide=1
    _clip_set(CF_HDROP, head + files)


# ---------------- 发送 ----------------
class Sender:
    def __init__(self, opts: dict | None = None):
        o = dict(opts or {})
        self.send_key = str(o.get("send_key") or "enter").lower()
        self.click_input = bool(o.get("click_input", True))
        self.idle_need = float(o.get("idle_seconds", 2.0))
        self.idle_wait = float(o.get("max_idle_wait", 60.0))
        self.restore_focus = bool(o.get("restore_focus", True))
        self.search_fallback = bool(o.get("search_fallback", False))   # 默认不用：搜索打开的会话核对不了是不是那个群
        self.titles: dict[str, str] = dict(o.get("window_titles") or {})     # 群 id（或群名）→ 单独窗口的标题
        self.own = None          # 返回“登录着她的号的那个微信”的进程（开了两个微信时用来分清）；None = 不知道

    def _wait_idle(self):
        """等你停手；一直不停就不发（绝不在你打字的时候抢键盘）。"""
        end = time.monotonic() + self.idle_wait
        while idle_seconds() < self.idle_need:
            if time.monotonic() >= end:
                raise UIError(f"你一直在用键盘鼠标（等了 {int(self.idle_wait)} 秒），这次先不发")
            time.sleep(0.3)

    def target(self, name: str) -> tuple[dict | None, str]:
        own = self.own() if callable(self.own) else None
        if own == []:
            raise UIError("她的号现在没在这台电脑的微信里登录（开着的是别的号？）")
        ws = windows(own) if own else windows()
        hits = chat_windows(name, ws)
        if len(hits) > 1:                                     # 开了两个微信、两边都开着这个群的窗口：分不清就不发
            raise UIError(f"找到 {len(hits)} 个「{name}」窗口（开了两个微信？），分不清哪个是她的号，这次不发")
        if hits:
            return hits[0], "window"
        m = main_window(ws)
        if m and own is None and len({w["pid"] for w in ws if w["title"] in MAIN_TITLES}) > 1:
            return None, ""                                   # 好几个微信主窗口、又认不出哪个是她：不用搜索方式
        return (m, "search") if m else (None, "")

    def _guard(self, hwnd: int):
        if foreground_root() != (user32.GetAncestor(hwnd, 2) or hwnd):
            raise UIError("前台被别的窗口抢走了，这次不发")

    def _click_input(self, hwnd: int, rel_x: float):
        r = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(r))
        s = dpi_scale(hwnd)
        x = int(r.left + (r.right - r.left) * rel_x)
        y = int(r.bottom - 60 * s)
        click(x, y)
        time.sleep(0.12)

    def _open_by_search(self, hwnd: int, name: str):
        self._guard(hwnd)
        press("ctrl", "f")
        time.sleep(0.4)
        clip_set_text(name)
        self._guard(hwnd)
        press("ctrl", "a")
        press("ctrl", "v")
        time.sleep(1.4)                                       # 等搜索结果出来
        self._guard(hwnd)
        press("enter")
        time.sleep(0.9)

    def paste_send(self, name: str, text: str | None = None, files: list[str] | None = None,
                   allow_search: bool = True, title: str | None = None) -> str:
        """把文字或文件发到名叫 name 的群。返回用的方式（window / search）。核对交给上层。"""
        if sys.platform != "win32":
            raise UIError("只能在 Windows 上发微信消息")
        if screen_locked():
            raise UIError("屏幕锁着（或者没有前台窗口），模拟不了按键：别锁屏，关显示器没关系")
        self._wait_idle()
        w, how = self.target(title or self.titles.get(name, name))
        if not w:
            raise UIError("没找到微信窗口（微信开着吗？）")
        if how == "search" and not (self.search_fallback and allow_search):
            raise UIError(f"没找到「{name}」的单独窗口：在微信里双击这个群，让它单独开一个窗口"
                          + ("" if allow_search else "（这个群名会搜到别的会话，不能用搜索方式发）"))
        prev = user32.GetForegroundWindow()
        old_clip = None
        try:
            old_clip = clip_save()
        except Exception:  # noqa: BLE001
            pass
        hwnd = w["hwnd"]
        try:
            if not focus(hwnd):
                raise UIError("没法把微信窗口切到前台")
            if how == "search":
                self._open_by_search(hwnd, name)
            if self.click_input:
                self._guard(hwnd)
                self._click_input(hwnd, 0.62 if how == "search" else 0.45)
            if files:
                clip_set_files(files)
            else:
                clip_set_text(text or "")
            self._guard(hwnd)
            press("ctrl", "a")
            press("ctrl", "v")
            time.sleep(0.35 if not files else 0.9)
            self._guard(hwnd)
            if self.send_key in ("ctrl+enter", "ctrl-enter"):
                press("ctrl", "enter")
            else:
                press("enter")
            time.sleep(0.15)
        finally:
            try:
                if old_clip is not None:
                    clip_restore(old_clip)
            except Exception:  # noqa: BLE001
                pass
            if self.restore_focus and prev and prev != hwnd and user32.IsWindow(prev):
                try:
                    focus(prev, timeout=0.6)
                except Exception:  # noqa: BLE001
                    pass
        return how

