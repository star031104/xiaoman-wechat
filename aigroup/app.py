"""运行微信群里的她：接上电脑（或服务器 Docker 里）的微信、听群消息、每天排她自己的安排、群冷了冒泡、每天备份。

收发消息的那一头是 aigroup/wechat/bot.py 的 WeChatBot（读微信本地库收、操作窗口发），群 id 形如 xxx@chatroom；
消息在程序内部统一用 OneBot 11 的消息段格式（aigroup/onebot.py）。
"""
from __future__ import annotations

import asyncio
import logging
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

log = logging.getLogger("aigroup")


def setup_file_logging(root: Path) -> Path:
    d = Path(root) / "data" / "logs"
    d.mkdir(parents=True, exist_ok=True)
    p = d / "group.log"
    h = RotatingFileHandler(p, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    h.setLevel(logging.INFO)
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s | %(message)s", "%m-%d %H:%M:%S"))
    logging.getLogger().addHandler(h)
    return p


class _NoChatText(logging.Filter):
    """13.0 控制台（服务器上就是 systemd 日志）不写聊天正文：aigroup.text 这个 logger 记的是大模型原文、她的想法、
    群友说的话，只进她家里的日志文件（data/logs/group.log，小屋“日志”页看的就是它），不进 journald / syslog。
    config.yaml 里写 log_text_console: true 可以让控制台也显示（自己电脑上调试用）。"""

    def filter(self, record: logging.LogRecord) -> bool:
        return not record.name.startswith("aigroup.text")


def quiet_console_text(enabled: bool = True):
    for h in logging.getLogger().handlers:
        if isinstance(h, logging.StreamHandler) and not isinstance(h, RotatingFileHandler):
            if enabled and not any(isinstance(f, _NoChatText) for f in h.filters):
                h.addFilter(_NoChatText())


def single_instance(path: Path):
    """同一个角色只能跑一个（不然两个进程抢着回话）。拿到锁返回打开的文件（要一直留着）；已经有一个在跑返回 None。"""
    path.parent.mkdir(parents=True, exist_ok=True)
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


def check(cfg) -> list[str]:
    """启动前检查：返回问题列表（空＝没问题）。"""
    probs = []
    q = cfg.get("wechat") or {}
    if sys.platform != "win32" and not sys.platform.startswith("linux"):
        probs.append("微信版只能在 Windows 或 Linux 上跑（要读微信的本地库、操作微信窗口）")
    if not [g for g in (q.get("groups") or []) if str(g).strip().endswith("@chatroom")]:
        probs.append("wechat.groups 是空的：她至少要在一个群里（白名单，填群 id，形如 123456@chatroom）")
    try:
        from aigroup.core.config import resolve
        from aigroup.persona import load_persona
        pf = resolve(cfg, cfg.persona_file)
        if not pf.exists():
            raise FileNotFoundError("还没有她的人设：双击 setup.bat（或 python run.py setup），会从 persona/template.md 复制一份给你改")
        load_persona(pf)
    except FileNotFoundError as e:
        probs.append(f"人设文件 {cfg.get('persona_file')} 不存在。{e}" if "setup" in str(e) else str(e))
    except Exception as e:  # noqa: BLE001
        probs.append(f"人设文件读不了（{cfg.get('persona_file')}）：{type(e).__name__}: {str(e)[:80]}"
                     "（文件在不在？用记事本另存时编码要选 UTF-8）")
    if not ((cfg.get("llm") or {}).get("routes") or {}).get("chat"):
        probs.append("还没配置大模型接口：双击 scripts\\llm.bat（或 setup.bat）填你的接口地址和 Key、选一个模型")
    return probs


async def add_self_names(ob, gc):
    names = set()
    for g in ob.groups:
        names |= set(ob.data.self_names(g))
    new = [n for n in names if n and len(n) >= 2 and n not in gc.aliases and n not in getattr(gc, "not_aliases", ())]
    if new:
        gc.aliases += new
        log.info("群里叫这些名字也算叫她：%s", "、".join(gc.aliases))


async def record_own(gc, ev: dict):
    """她的号在手机上（不是程序）发的话：记进聊天记录，当成她说的（她以后知道自己说过）。"""
    from aigroup.core.safety import clean_member_text
    from aigroup.onebot import flatten
    gid = int(ev.get("group_id") or 0)
    if gid not in gc.groups:
        return
    f = flatten(ev.get("message"), gc.ob.self_id, gc.names(gid, members=True))
    text = clean_member_text(f.text)
    if text:
        gc._record(gid, int(gc.ob.self_id or 0), gc.brain.persona.name, text, ev.get("message_id"), role="assistant",
                   ts=float(ev.get("time") or 0) or None)
        logging.getLogger("aigroup.text").info("群 %s：记下了她的号在别处说的「%s」", gid, text[:30])


async def run(cfg):
    """返回 None＝正常退出（start.bat 会重开）；"stop"＝别重开（配置有问题 / 已经有一个在跑）。"""
    from aigroup.brain import GroupBrain
    from aigroup.chat import plan
    from aigroup.chat.group import GroupChat
    probs = check(cfg)
    for p in probs:
        log.error("配置有问题：%s", p)
    if probs:
        log.error("改好再启动：双击 setup.bat 重新配置一次，大模型接口也可以只双击 scripts\\llm.bat 加")
        return "stop"
    lock = single_instance(Path(cfg.root) / "data" / "run.lock")
    if lock is None:
        log.warning("她已经在另一个窗口里运行了，这个窗口不再重复启动")
        return "stop"
    brain = GroupBrain(cfg)
    cu_lock = asyncio.Lock()
    from aigroup.wechat.bot import WX_FACES, WeChatBot
    q = cfg.get("wechat") or {}
    ob = WeChatBot(dict(q), Path(cfg.root))
    plan.KEEP_TAGS |= WX_FACES                                 # [捂脸] 这种微信小表情别当标签删掉
    groups = [ob.gid(g) for g in ob.groups]
    owner = str(q.get("owner") or "").strip()
    gc = GroupChat(cfg, brain, ob, ob.ids.id(owner) if owner else 0, groups, list(q.get("aliases") or []))

    async def catch_up():                                      # 几处都会触发，排着队来，免得同一批记两遍
        async with cu_lock:
            await gc.catch_up()
    ob.handlers.append(gc.on_event)
    ob.on_connect.append(gc.refresh_members)                   # 先拉群成员（名字 / 别名），再补消息
    ob.on_connect.append(lambda: add_self_names(ob, gc))       # 她的微信昵称、群昵称也算在叫她
    ob.on_connect.append(catch_up)                             # 接上微信后从本地库补上她不在时群里说的话
    ob.on_backlog.append(catch_up)                             # 微信刚开、同步下来一批离线消息：也走补消息
    ob.on_self_message.append(lambda ev: record_own(gc, ev))   # 在手机上用她的号说的话：记成她说的
    ob.start()
    log.info("%s 启动了（微信）：群 %s", brain.persona.name, ob.groups)
    from aigroup.core.releases import note_deploy
    note_deploy()                                              # 31.2 发版记录以启动时真在跑的提交为准
    from aigroup.web.server import GroupHome
    home = GroupHome(cfg, brain, ob, gc)
    try:
        await home.start()                                     # 小屋：http://127.0.0.1:8768
    except OSError as e:
        log.warning("小屋没开起来（端口 %s 被占用？）：%s", home.port, e)
    bg = [asyncio.create_task(gc.learn_interests())]          # 第一次：从人设里记下她感兴趣的话题

    async def daily():
        now = brain.now()
        await brain.plan_life(now)                            # 她今天自己的安排（群里聊起来前后一致）
        key = f"backup_{now:%Y-%m-%d}"
        if not brain.db.get(key):
            try:
                d = Path(cfg.root) / "data" / "backup"
                await asyncio.to_thread(brain.db.backup, d / f"group-{now:%Y%m%d}.db")   # 别卡住聊天
                for old in sorted(d.glob("group-*.db"))[:-3]:   # 留 3 天
                    old.unlink(missing_ok=True)
                brain.db.set(key, True)
            except Exception as e:  # noqa: BLE001
                log.info("备份失败：%s", e)
        if now.hour >= 5:                                     # 早上：昨天的群日报、重新拟合发言权重、清理旧数据
            await gc.nightly()
            from aigroup.chat.gate import active_weights
            gc.weights, gc.weights_src = active_weights(brain.db)

    tray = asyncio.Event()
    tray_cmd = {"cmd": ""}

    async def watch_tray():                        # 托盘里点了“重启 / 退出”：它往 data/tray_cmd 里写一个字
        p = Path(cfg.root) / "data" / "tray_cmd"
        born = time.time() - 2
        while True:
            await asyncio.sleep(1)
            try:
                cmd = p.read_text(encoding="utf-8").strip() if p.exists() else ""
                if cmd:
                    old = p.stat().st_mtime < born          # 12.0 启动前就留在那的（比如没在跑时别人写的“重启”）：不算
                    p.unlink(missing_ok=True)
                    cmd = "" if old else cmd
            except OSError:
                cmd = ""
            if cmd in ("stop", "restart"):
                tray_cmd["cmd"] = cmd
                tray.set()
    bg.append(asyncio.create_task(watch_tray()))

    last_daily = 0.0
    try:
        while True:
            if time.monotonic() - last_daily > 1800:
                last_daily = time.monotonic()
                try:
                    await daily()
                except Exception as e:  # noqa: BLE001
                    log.info("每天的整理没做完：%s（等会儿再试）", e)
            if ob.ready:
                try:
                    await gc.idle_tick(1.0)
                except Exception as e:  # noqa: BLE001
                    log.info("冒泡检查出错：%s", e)
            try:
                await asyncio.wait_for(tray.wait(), 60)
            except asyncio.TimeoutError:
                continue
            log.info("托盘里点了%s", "退出" if tray_cmd["cmd"] == "stop" else "重启")
            return "stop" if tray_cmd["cmd"] == "stop" else None
    finally:
        for t in bg:
            t.cancel()
        try:
            await home.stop()
        except Exception:  # noqa: BLE001
            pass
        await ob.stop()
        lock.close()
