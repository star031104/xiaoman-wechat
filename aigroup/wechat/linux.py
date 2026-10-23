"""Linux 版微信（4.x）：服务器上 24 小时挂着用。和 Windows 版一样只读、不注入、不改微信的任何东西。

    典型部署：微信跑在 Docker 容器里（ricwang/docker-wechat，网页 VNC 扫码登录），本程序跑在宿主机上：
    - 读库：容器里的 /root/xwechat_files 挂到宿主机一个目录，config.yaml 的 wechat.data_dir 填那个目录
    - 拿密钥：宿主机能看到容器里的进程（同一个内核），用 /proc/<pid>/maps + /proc/<pid>/mem 只读内存
      （要 root：systemd 服务里 User=root，或者给 python 加 CAP_SYS_PTRACE）
    - 发消息：xdotool + xclip 在微信所在的 X 显示里操作窗口；微信在容器里时，命令前面加
      wechat.x_exec（默认 ["docker", "exec", "-i", "-e", "DISPLAY=:0", 容器名]），容器名填 wechat.container
    服务器上没有人用键盘鼠标，所以不用等“你停手”；也没有锁屏这回事。
"""
from __future__ import annotations

import logging
import os
import re
import shlex
import subprocess
import time
from pathlib import Path

log = logging.getLogger("aigroup.wechat.linux")

ENABLED = False                                 # enable() 之后：keys / wxui 用这里的 Linux 实现

NAMES = {"wechat", "weixin", "wechatappex"}     # 进程名（/proc/<pid>/comm，最多 15 个字符）


# ---------------- 进程 ----------------
def _comm(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/comm").read_text(errors="ignore").strip()
    except OSError:
        return ""


def _stat(pid: int) -> tuple[int, int]:
    """(父进程, 线程数)"""
    try:
        s = Path(f"/proc/{pid}/stat").read_text(errors="ignore")
        rest = s[s.rindex(")") + 2:].split()
        return int(rest[1]), int(rest[17])
    except (OSError, ValueError, IndexError):
        return 0, 0


def weixin_procs(names: set[str] | None = None) -> list[tuple[int, int, int]]:
    """所有微信进程：(pid, 父进程 pid, 线程数)。只要主程序（wechat），不要它的小工具进程。"""
    names = names or NAMES
    out = []
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        pid = int(d)
        if _comm(pid).lower() in names:
            pp, th = _stat(pid)
            out.append((pid, pp, th))
    return out


def file_users(paths: list[str | os.PathLike]) -> list[int] | None:
    """哪些进程开着这些文件。微信在容器里时它看到的路径和宿主机不一样，所以按“账号目录名/db_storage/…”
    这一段尾巴来比。读不了别人的 /proc/<pid>/fd（不是 root）时返回 None（＝不知道）。"""
    tails = []
    for p in paths:
        parts = Path(p).parts
        if "db_storage" in parts:
            i = parts.index("db_storage")
            tails.append("/" + "/".join(parts[max(0, i - 1):]))
        else:
            tails.append(str(Path(p).resolve()))
    users, denied = [], 0
    for pid, _, _ in weixin_procs():
        try:
            fds = os.listdir(f"/proc/{pid}/fd")
        except PermissionError:
            denied += 1
            continue
        except OSError:
            continue
        for fd in fds:
            try:
                link = os.readlink(f"/proc/{pid}/fd/{fd}")
            except OSError:
                continue
            if any(link.endswith(t) for t in tails):
                users.append(pid)
                break
    if denied and not users:
        return None
    return users


class ProcMem:
    """只读打开一个进程的内存：和 keys.ProcMem（Windows）同样的 regions() / read() / close()。"""

    def __init__(self, pid: int):
        self.pid = pid
        try:
            self.fd = os.open(f"/proc/{pid}/mem", os.O_RDONLY)
        except PermissionError as e:
            raise PermissionError(f"读不了微信进程 {pid} 的内存：要用 root 运行（systemd 服务里 User=root），"
                                  "或者 sudo setcap cap_sys_ptrace+ep 给 python") from e
        except OSError as e:
            raise PermissionError(f"打不开微信进程 {pid}：{e}") from e

    def regions(self, private_only: bool = False, max_size: int = 0x10000000):
        try:
            lines = Path(f"/proc/{self.pid}/maps").read_text(errors="ignore").splitlines()
        except OSError:
            return
        for ln in lines:
            parts = ln.split(None, 5)
            if len(parts) < 5 or "r" not in parts[1]:
                continue
            path = parts[5].strip() if len(parts) > 5 else ""
            if path in ("[vvar]", "[vsyscall]", "[vdso]") or path.startswith("/dev/"):
                continue
            if private_only and path and not (path == "[heap]" or path.startswith("[anon")):
                continue
            a, b = (int(x, 16) for x in parts[0].split("-"))
            if 0 < b - a <= max_size:
                yield a, b - a

    def read(self, addr: int, n: int) -> bytes | None:
        if n <= 0 or addr < 0x10000:
            return None
        try:
            return os.pread(self.fd, n, addr) or None
        except (OSError, OverflowError, ValueError):
            return None

    def close(self):
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None


# ---------------- X 窗口（xdotool / xclip） ----------------
IMAGE_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
              ".webp": "image/webp", ".bmp": "image/bmp"}


class XError(RuntimeError):
    pass


def _prefix(opts: dict) -> list[str]:
    x = opts.get("x_exec")
    if isinstance(x, str) and x.strip():
        return shlex.split(x)
    if isinstance(x, list) and x:
        return [str(v) for v in x]
    c = str(opts.get("container") or "").strip()
    if c:
        return ["docker", "exec", "-i", "-e", f"DISPLAY={opts.get('display') or ':0'}", c]
    return []


def xrun(opts: dict, args: list[str], data: bytes | None = None, timeout: float = 10.0) -> str:
    """在微信所在的显示里跑一个命令（xdotool / xclip），返回标准输出。"""
    env = None
    pre = _prefix(opts)
    if not pre:
        env = {**os.environ, "DISPLAY": str(opts.get("display") or os.environ.get("DISPLAY") or ":0")}
    try:
        r = subprocess.run(pre + args, input=data, capture_output=True, timeout=timeout, env=env)
    except FileNotFoundError as e:
        raise XError(f"没有 {e.filename}：装上 xdotool、xclip（微信在容器里时，宿主机要有 docker）") from e
    except subprocess.TimeoutExpired as e:
        raise XError(f"{args[0]} 超时") from e
    if r.returncode not in ((0, 1) if args[0] == "sh" else (0,)):
        raise XError(f"{' '.join(args[:2])} 失败：{r.stderr.decode(errors='ignore')[:200]}")
    return r.stdout.decode(errors="ignore")


_LIST = ('for w in $(xdotool search --onlyvisible --name "." 2>/dev/null); do '
         'eval $(xdotool getwindowgeometry --shell $w 2>/dev/null); '
         'printf "%s\\t%s\\t%s\\t%s\\t%s\\t%s\\n" "$w" "${X:-0}" "${Y:-0}" "${WIDTH:-0}" "${HEIGHT:-0}" '
         '"$(xdotool getwindowname $w 2>/dev/null)"; done')


def windows(opts: dict) -> list[dict]:
    """微信所在显示里所有看得见的顶层窗口（和 Windows 版 wxui.windows() 一样的字段）。
    显示里只有微信一个程序（容器 / 专门的虚拟桌面），所以不按进程筛。"""
    try:
        out = xrun(opts, ["sh", "-c", _LIST], timeout=15)
    except XError as e:
        log.debug("列窗口失败：%s", e)
        return []
    ws = []
    for ln in out.splitlines():
        f = ln.split("\t", 5)
        if len(f) < 6 or not f[0].isdigit():
            continue
        x, y, w, h = (int(v) if re.fullmatch(r"-?\d+", v) else 0 for v in f[1:5])
        if w < 50 or h < 50:
            continue
        ws.append({"hwnd": int(f[0]), "title": f[5], "cls": "", "pid": 0, "rect": (x, y, x + w, y + h),
                   "visible": True, "iconic": False})
    return ws


class XSender:
    """和 wxui.Sender 一样的接口：paste_send(群名, 文字 / 文件)。"""

    def __init__(self, opts: dict | None = None):
        o = dict(opts or {})
        self.opts = o
        self.send_key = str(o.get("send_key") or "enter").lower()
        self.click_input = bool(o.get("click_input", True))
        self.search_fallback = bool(o.get("search_fallback", False))
        self.titles: dict[str, str] = dict(o.get("window_titles") or {})
        self.own = None
        self.path_map = dict(o.get("path_map") or {})        # 宿主机路径前缀 → 微信看到的路径前缀（发文件用）
        self.paste_images = bool(o.get("paste_images", True))   # 17.1 图片按图片发（关掉就按文件发）

    def _x(self, *args: str, data: bytes | None = None):
        return xrun(self.opts, list(args), data=data)

    def remap_hidden(self, name: str = "") -> int:
        """20.4.2 微信的窗口还在、只是被藏起来了（最小化 / 关到托盘 / VNC 里点了关闭；网页 VNC 里看是一片黑）：
        把“微信”主窗口和这个群的单独窗口重新显示出来。返回显示了几个。"""
        from aigroup.wechat.wxui import MAIN_TITLES
        want = set(MAIN_TITLES) | ({self.titles.get(name, name)} if name else set()) | set(self.titles.values())
        try:
            out = xrun(self.opts, ["sh", "-c", 'for w in $(xdotool search --name "." 2>/dev/null); do '
                                               'printf "%s\\t%s\\n" "$w" "$(xdotool getwindowname $w 2>/dev/null)"; done'],
                       timeout=15)
        except XError:
            return 0
        n = 0
        for ln in out.splitlines():
            wid, _, title = ln.partition("\t")
            if wid.isdigit() and title in want:
                try:
                    self._x("xdotool", "windowmap", wid)
                    n += 1
                except XError:
                    continue
        if n:
            log.info("微信窗口被藏起来了，重新显示了 %d 个", n)
            time.sleep(0.8)
        return n

    def target(self, name: str) -> tuple[dict | None, str]:
        from aigroup.wechat.wxui import MAIN_TITLES, chat_windows, main_window
        ws = windows(self.opts)
        if not chat_windows(name, ws) and self.remap_hidden(name):
            ws = windows(self.opts)
        hits = chat_windows(name, ws)
        if len(hits) > 1:
            raise XError(f"找到 {len(hits)} 个「{name}」窗口，分不清，这次不发")
        if hits:
            return hits[0], "window"
        m = main_window([w for w in ws if w["title"] in MAIN_TITLES] or ws)
        return (m, "search") if m else (None, "")

    def _focus(self, wid: int) -> bool:
        self._x("xdotool", "windowactivate", "--sync", str(wid))
        time.sleep(0.15)
        try:
            return self._x("xdotool", "getactivewindow").strip() == str(wid)
        except XError:
            return False

    def _guard(self, wid: int):
        if self._x("xdotool", "getactivewindow").strip() != str(wid):
            raise XError("前台被别的窗口抢走了，这次不发")

    def _click_input(self, w: dict, rel_x: float):
        l, t, r, b = w["rect"]
        self._x("xdotool", "mousemove", str(int(l + (r - l) * rel_x)), str(int(b - 60)), "click", "1")
        time.sleep(0.12)

    def _clip_text(self, text: str):
        self._x("xclip", "-selection", "clipboard", "-i", data=text.encode("utf-8"))

    def _clip_files(self, paths: list[str]):
        # 17.1 一张图：按图片放进剪贴板（image/png 这类），微信发出去是“图片”；
        # 以前一律放 text/uri-list，微信当成文件，群里看到的是一个 aiwx_xxx.png 文件
        if len(paths) == 1 and self.paste_images:
            q = Path(str(paths[0]))
            mime = IMAGE_MIME.get(q.suffix.lower())
            if mime and q.is_file():
                self._x("xclip", "-selection", "clipboard", "-t", mime, "-i", data=q.read_bytes())
                return
        uris = []
        for p in paths:
            p = str(p)
            for a, b in self.path_map.items():
                if p.startswith(str(a)):
                    p = str(b) + p[len(str(a)):]
                    break
            uris.append("file://" + p)
        self._x("xclip", "-selection", "clipboard", "-t", "text/uri-list", "-i",
                data=("\r\n".join(uris) + "\r\n").encode("utf-8"))

    def _open_by_search(self, w: dict, name: str):
        wid = w["hwnd"]
        self._guard(wid)
        self._x("xdotool", "key", "--clearmodifiers", "ctrl+f")
        time.sleep(0.4)
        self._clip_text(name)
        self._x("xdotool", "key", "--clearmodifiers", "ctrl+a", "ctrl+v")
        time.sleep(1.4)
        self._guard(wid)
        self._x("xdotool", "key", "Return")
        time.sleep(0.9)

    def paste_send(self, name: str, text: str | None = None, files: list[str] | None = None,
                   allow_search: bool = True, title: str | None = None) -> str:
        w, how = self.target(title or self.titles.get(name, name))
        if not w:
            raise XError("没找到微信窗口（微信开着、登录了吗？用网页 VNC 看一眼）")
        if how == "search" and not (self.search_fallback and allow_search):
            raise XError(f"没找到「{name}」的单独窗口：在微信里双击这个群，让它单独开一个窗口")
        wid = w["hwnd"]
        if not self._focus(wid):
            raise XError("没法把微信窗口切到前台")
        if how == "search":
            self._open_by_search(w, name)
        if self.click_input:
            self._guard(wid)
            self._click_input(w, 0.62 if how == "search" else 0.45)
        if files:
            self._clip_files(files)
        else:
            self._clip_text(text or "")
        self._guard(wid)
        self._x("xdotool", "key", "--clearmodifiers", "ctrl+a", "ctrl+v")
        time.sleep(0.35 if not files else 0.9)
        self._guard(wid)
        self._x("xdotool", "key", "--clearmodifiers", "ctrl+Return" if self.send_key in ("ctrl+enter", "ctrl-enter")
                else "Return")
        time.sleep(0.15)
        return how


# ---------------- 打开 Linux 模式 ----------------
def wanted(opts: dict) -> bool:
    """在 Linux 上、config 里写了 wechat.linux: true（或者填了 container / x_exec）就用 Linux 模式。"""
    import sys
    o = opts or {}
    return sys.platform.startswith("linux") and bool(o.get("linux") or o.get("container") or o.get("x_exec"))


def enable(opts: dict) -> None:
    """把 keys（进程、内存、谁开着库）和 wxui（窗口、发送）换成 Linux 实现。可以重复调。"""
    global ENABLED
    from aigroup.wechat import keys, wxui
    X_OPTS.clear()
    X_OPTS.update(dict(opts or {}))
    keys.ProcMem, keys.file_users, keys.weixin_procs = ProcMem, file_users, weixin_procs
    wxui.windows = lambda pids=None: windows(X_OPTS)
    wxui.idle_seconds = lambda: 1e9                       # 服务器上没人用键盘鼠标
    wxui.screen_locked = lambda: False
    wxui.Sender, wxui.UIError = XSender, XError
    ENABLED = True


X_OPTS: dict = {}


def active() -> bool:
    import sys
    return sys.platform == "win32" or ENABLED
