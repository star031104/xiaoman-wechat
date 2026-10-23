"""微信接入的自检和小工具。

    python run.py wxdiag              全面体检：微信版本、数据目录、密钥、能不能读到群消息、窗口
    python run.py wxkeys [--deep]     只拿密钥（--deep：前面的办法都不行时，把内存整个筛一遍，慢但不挑版本）
    python run.py wxsend 群名 文字      试着往群里发一句（发完在库里核对）
    python run.py wxtail 群名          实时打印群里的新消息（只读，不回话），Ctrl+C 结束
    python run.py wxexport             把某个号（比如你的大号）里几个群的聊天记录导出成文本（给 seedgen 整理、认人用）
"""
from __future__ import annotations

import asyncio
import platform
import struct
import sys
import time

from aigroup.core.config import ROOT, load_config
from aigroup.core.util import say


def _wcfg() -> dict:
    try:
        w = dict(load_config().get("wechat") or {})
    except Exception:  # noqa: BLE001
        return {}
    from aigroup.wechat import linux
    if linux.wanted(w):                                  # 服务器上的 Linux 微信
        linux.enable(w)
    return w


def exe_version(pid: int) -> tuple[str, str]:
    """(Weixin.exe 路径, 文件版本)。"""
    if sys.platform != "win32":
        return "", ""
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32")
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                               ctypes.POINTER(wintypes.DWORD)]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    h = k32.OpenProcess(0x1000, False, pid)
    path = ""
    if h:
        buf = ctypes.create_unicode_buffer(1024)
        n = wintypes.DWORD(1024)
        if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)):
            path = buf.value
        k32.CloseHandle(h)
    ver = ""
    if path:
        v = ctypes.WinDLL("version")
        sz = v.GetFileVersionInfoSizeW(path, None)
        if sz:
            data = ctypes.create_string_buffer(sz)
            if v.GetFileVersionInfoW(path, 0, sz, data):
                p = ctypes.c_void_p()
                ln = wintypes.UINT()
                if v.VerQueryValueW(data, "\\", ctypes.byref(p), ctypes.byref(ln)) and ln.value >= 52:
                    raw = ctypes.string_at(p, 52)
                    ms, ls = struct.unpack_from("<II", raw, 8)
                    ver = f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}.{ls & 0xFFFF}"
    return path, ver


def diag() -> int:
    from aigroup.wechat import wxui
    from aigroup.wechat.bot import open_data
    from aigroup.wechat.keys import instances, owner_pids, weixin_pids
    from aigroup.wechat.wxdb import find_data_root, list_accounts
    w = _wcfg()
    print("=" * 60 + "\n  微信接入体检（有问题把下面全部内容贴到 GitHub Issue 里）\n" + "=" * 60)
    print(f"系统：{platform.platform()}；Python {platform.python_version()} {struct.calcsize('P') * 8} 位")
    if struct.calcsize("P") != 8:
        say(False, "要用 64 位的 Python（32 位读不了 64 位微信的内存）")
    pids = weixin_pids()
    if not pids:
        say(False, "微信没在运行：先打开微信、用她的小号登录")
    for pid in pids[:1]:
        path, ver = exe_version(pid)
        say(True, f"微信进程 {pid}：{ver or '版本未知'}  {path}")
    inst = instances()
    print(f"   （共 {len(pids)} 个 Weixin.exe 进程，开着 {len(inst)} 个微信）")
    root = find_data_root(str(w.get("data_dir") or ""))
    if not root:
        say(False, "没找到数据目录 xwechat_files（在 config.yaml 里填 wechat.data_dir）")
        return 1
    say(True, f"数据目录：{root}")
    for a in list_accounts(root):
        own = owner_pids(a["dir"]) if pids else []
        st = "登录状态认不出" if own is None else (f"正登录在进程 {own[0]}" if own else "没登录")
        mark = " ← config 里选的" if str(w.get("account") or "") in (a["account"], a["wxid"]) and w.get("account") else ""
        print(f"   账号目录 {a['account']}（微信号 {a['wxid']}），{st}，最后改动 "
              f"{time.strftime('%m-%d %H:%M', time.localtime(a['mtime']))}{mark}")
    t0 = time.time()
    d, notes = open_data(w, ROOT, progress=lambda s: print("   " + s))
    for n in notes:
        print("   " + n)
    if d is None:
        say(False, f"库没打开（{time.time() - t0:.1f} 秒）。可以试试：python run.py wxkeys --deep")
    else:
        say(True, f"库打开了（{time.time() - t0:.1f} 秒），账号 {d.self_wxid}；消息分片 {d.shards()}")
        d.refresh_all()
        d.load_contacts(force=True)
        me = d.contacts.get(d.self_wxid) or {}
        print(f"   她的昵称：{me.get('nick') or '（通讯录里没有自己）'}；联系人 {len(d.contacts)}，群 {len(d.rooms)}")
        gs = d.groups()
        want = [str(g) for g in w.get("groups") or []]
        print("   群（前 15 个）：")
        for g in gs[:15]:
            mark = "★" if g["id"] in want else " "
            print(f"   {mark} {g['name']}  {g['id']}  {g['members']} 人  {'有消息' if g['has_msgs'] else '无消息记录'}")
        for gid in want:
            print(f"\n   「{d.room_name(gid)}」最近 5 条：")
            ms = d.new_messages(gid, 0, limit=100000)[-5:]
            for m in ms:
                who = "她" if m.sender == d.self_wxid else d.display(m.sender, gid)
                print(f"     {time.strftime('%m-%d %H:%M', time.localtime(m.ts))} {who}：{(m.text or m.raw)[:40]!r}"
                      f"{'  @' + ','.join(m.ats) if m.ats else ''}")
            if not ms:
                print("     （没有消息记录）")
        print("   她在群里的名字：" + "、".join(d.self_names(want[0]) if want else d.self_names()))
    from aigroup.wechat import linux
    if linux.active():
        own = owner_pids(d.account_dir) if d is not None else None
        if d is not None and len(inst) > 1:
            say(bool(own), f"开着 {len(inst)} 个微信：她的号在进程 {own[0]}，只用这个微信的窗口" if own else
                ("开着好几个微信，但她的号没登录在任何一个里" if own == [] else "开着好几个微信，认不出哪个是她的号（发消息时同名窗口会拒发）"))
        ws = wxui.windows(own) if own else wxui.windows(pids)
        print("\n   微信窗口：")
        for x in ws:
            if x["visible"] or x["iconic"]:
                l, t, r, b = x["rect"]
                print(f"     {x['title']!r:28} 类 {x['cls']:28} {r - l}x{b - t}{' 最小化' if x['iconic'] else ''}")
        m = wxui.main_window(ws)
        say(bool(m), "主窗口：" + (repr(m["title"]) if m else "没找到"))
        if d is not None:
            for gid in [str(g) for g in w.get("groups") or []]:
                nm = d.room_name(gid)
                from aigroup.wechat.bot import send_problem
                bad = send_problem(d, gid, w.get("window_titles"))
                if bad:
                    say(False, bad)
                cw = wxui.chat_window(wxui.title_for(w.get("window_titles") or {}, gid, nm), ws)
                say(bool(cw), f"「{nm}」单独窗口：" + ("找到了" if cw else "没有（在微信里双击这个群，单独打开它；没有单独窗口发不了消息）"))
        print(f"   锁屏：{'是' if wxui.screen_locked() else '否'}；键盘鼠标空闲 {wxui.idle_seconds():.0f} 秒")
    return 0 if d is not None else 1


def keys_cmd(argv: list[str]) -> int:
    from aigroup.wechat.keys import get_keys
    from aigroup.wechat.wxdb import find_data_root, list_accounts
    w = _wcfg()
    root = find_data_root(str(w.get("data_dir") or ""))
    if not root:
        say(False, "没找到数据目录")
        return 1
    accs = list_accounts(root)
    want = str(w.get("account") or "")
    accs = [a for a in accs if not want or a["account"] == want or a["wxid"] == want] or accs
    a = accs[0]
    print(f"账号目录：{a['dir']}")
    keys, notes = get_keys(a["dir"], a["account"], ROOT / "data" / "wechat" / "keys.json",
                           deep="--deep" in argv, progress=lambda s: print("  " + s))
    for n in notes:
        print("  " + n)
    for rel in sorted(keys):
        print(f"  ✅ {rel}")
    return 0 if keys else 1


def send_cmd(argv: list[str]) -> int:
    args = [a for a in argv if not a.startswith("--")]
    if len(args) < 2:
        print("用法：python run.py wxsend 群名 要发的话")
        return 1
    name, text = args[0], " ".join(args[1:])

    async def go():
        from aigroup.wechat.bot import WeChatBot
        w = _wcfg()
        bot = WeChatBot(w, ROOT)
        try:
            await asyncio.to_thread(bot._init_blocking)
        except Exception as e:  # noqa: BLE001
            say(False, f"读不了微信：{e}（python run.py wxdiag 看详细）")
            return 1
        bot.connected.set()
        gid = next((g for g in bot.data.rooms if bot.data.room_name(g) == name or g == name), None)
        if not gid:
            say(False, f"没有叫「{name}」的群")
            return 1
        bot.groups = [gid]
        bot.wm[gid] = bot.data.max_seq(gid)
        poll = asyncio.create_task(bot._run())
        try:
            t0 = time.time()
            mid = await bot.send_group(bot.gid(gid), [{"type": "text", "data": {"text": text}}])
            say(True, f"发出去了，在库里核对到了（{time.time() - t0:.1f} 秒，消息 id {mid}）")
            return 0
        except Exception as e:  # noqa: BLE001
            say(False, f"没发成功：{e}")
            return 1
        finally:
            poll.cancel()
    return asyncio.run(go())


def tail_cmd(argv: list[str]) -> int:
    args = [a for a in argv if not a.startswith("--")]

    async def go():
        from aigroup.wechat.bot import WeChatBot
        w = _wcfg()
        bot = WeChatBot(w, ROOT)
        try:
            await asyncio.to_thread(bot._init_blocking)
        except Exception as e:  # noqa: BLE001
            say(False, f"读不了微信：{e}（python run.py wxdiag 看详细）")
            return 1
        if not args and not bot.groups:
            say(False, "config.yaml 里还没有群（wechat.groups）：写上群名试试，python run.py wxtail 群名")
            return 1
        if args:
            gid = next((g for g in bot.data.rooms if bot.data.room_name(g) == args[0] or g == args[0]), None)
            if not gid:
                say(False, f"没有叫「{args[0]}」的群")
                return 1
            bot.groups = [gid]
            bot.wm[gid] = bot.data.max_seq(gid)
        print(f"在听：{[bot.data.room_name(g) for g in bot.groups]}（Ctrl+C 结束）")

        async def show(ev):
            if ev.get("post_type") == "message":
                txt = "".join(s["data"].get("text", "") if s["type"] == "text" else
                              f"@{s['data'].get('name') or s['data'].get('qq')} " if s["type"] == "at" else
                              f"[{s['type']}]" for s in ev["message"])
                print(f"{time.strftime('%H:%M:%S')} {ev['sender']['card']}：{txt}")
            else:
                print(f"{time.strftime('%H:%M:%S')} 事件：{ev}")
        bot.handlers.append(show)
        bot.connected.set()
        await bot._run()
    try:
        return asyncio.run(go()) or 0
    except KeyboardInterrupt:
        return 0


def export_cmd(argv: list[str]) -> int:
    """从电脑上开着的某个微信号（一般是你的大号）导出几个群的全部聊天记录，存成文本放在 data/export/ 里。
    只读；导完把这个号的密钥和解密副本删掉（如果它不是她的号）。"""
    from aigroup.core.util import ask
    from aigroup.wechat.bot import accounts_here, forget_account, open_data
    w = _wcfg()
    mine = str(w.get("account") or "")
    accs = accounts_here(w)
    if not accs:
        say(False, "没找到微信的数据目录：微信装好、登录过吗？")
        return 1
    print("从哪个号导出？（要导哪个号，那个微信就得开着、登录着）")
    for i, a in enumerate(accs, 1):
        st = "登录着" if a["logged"] else ("没登录" if a["logged"] is False else "不知道登没登录")
        print(f"  {i}. {a['wxid']}（{st}）{'  ← 她的小号' if mine in (a['account'], a['wxid']) and mine else ''}")
    dflt = next((str(i) for i, a in enumerate(accs, 1) if a["logged"] and mine not in (a["account"], a["wxid"])), "1")
    v = ask("编号", dflt)
    if not v.isdigit() or not 1 <= int(v) <= len(accs):
        return 1
    acc = accs[int(v) - 1]
    print("  正在读这个号的库（第一次要十几秒）…")
    d, notes = open_data({**w, "account": acc["account"]}, ROOT, progress=lambda m: print("    " + m))
    if d is None:
        for n in notes:
            print("    " + n)
        say(False, "这个号的库读不了（微信开着、登录着这个号吗？）")
        return 1
    try:
        d.refresh_all()
        d.load_contacts(force=True)
        limit = 30
        for a in argv:
            if a.startswith("--max="):
                limit = int(a.split("=", 1)[1] or 30)
        gs = [g for g in d.groups() if g["has_msgs"] and 0 < (g["members"] or 0) <= limit]
        gs.sort(key=lambda g: g["members"] or 0)
        if not gs:
            say(False, f"这个号里没有 {limit} 人以内、有聊天记录的群（--max=人数 可以放宽）")
            return 1
        print(f"\n{limit} 人以内的群（人少的在前）：")
        for i, g in enumerate(gs[:40], 1):
            who = "、".join(d.display(u, g["id"]) for u in d.members(g["id"])[:6])
            print(f"  {i}. {g['name']}（{g['members']} 人：{who}）")
        from aigroup.wizard import pick_numbers
        picked = [gs[i] for i in pick_numbers(ask("导哪几个群？编号用逗号隔开", ""), min(40, len(gs)))]
        if not picked:
            return 1
        out = ROOT / "data" / "export" / time.strftime("%Y%m%d-%H%M%S")
        out.mkdir(parents=True, exist_ok=True)
        for g in picked:
            gid = g["id"]
            ms = d.new_messages(gid, 0, limit=10 ** 7)
            cnt: dict[str, int] = {}
            lines = []
            for m in ms:
                who = "我" if m.sender == d.self_wxid else d.display(m.sender, gid) if m.sender else "系统"
                cnt[m.sender or ""] = cnt.get(m.sender or "", 0) + 1
                txt = (m.text or m.raw or "").replace("\r", " ").replace("\n", " ⏎ ")
                lines.append(f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(m.ts))} {who}：{txt[:500]}")
            head = [f"# {g['name']}", f"导出时间 {time.strftime('%Y-%m-%d %H:%M')}，共 {len(ms)} 条", "", "## 成员（“我”= 导出用的这个号）"]
            for u in d.members(gid):
                c = d.contacts.get(u) or {}
                head.append(f"- {'我' if u == d.self_wxid else d.display(u, gid)}｜微信昵称 {c.get('nick') or ''}"
                            f"｜备注 {c.get('remark') or ''}｜{u}｜说了 {cnt.get(u, 0)} 条")
            safe = "".join(ch for ch in g["name"] if ch.isalnum() or ch in "-_ ")[:30].strip() or "群"
            f = out / f"{safe}.txt"
            f.write_text("\n".join(head + ["", "## 聊天记录"] + lines) + "\n", encoding="utf-8")
            say(True, f"「{g['name']}」{len(ms)} 条 → {f}")
        print(f"\n导出在 {out}（只在你电脑上；data/ 不会进 git）")
    finally:
        d.close()
        if not mine or mine not in (acc["account"], acc["wxid"]):     # 不是她的号：密钥、解密副本都不留
            forget_account(ROOT, acc["account"])
            print("  这个号的密钥和解密副本已经删掉了（导出的聊天记录还在）")
    return 0
