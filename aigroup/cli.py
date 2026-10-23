"""命令行：python run.py [命令]（不带命令 = 启动她）。

加一个新命令：写一个 cmd_xxx(argv) -> int 函数，再在下面的 COMMANDS 里登记一行（名字、说明、函数）。
"""
from __future__ import annotations

import asyncio
import logging
import sys

log = logging.getLogger("aigroup")

EXIT_NO_RESTART = 3          # start.bat / start.sh 看到这个就不再自动重启


def _setup_console():
    if sys.platform == "win32":
        for s in (sys.stdout, sys.stderr):
            try:
                s.reconfigure(encoding="utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                pass
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s | %(message)s",
                        datefmt="%m-%d %H:%M:%S")
    for noisy in ("httpx", "openai", "aiohttp.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _quiet():
    logging.getLogger().setLevel(logging.WARNING)


# ---------------- 各个命令 ----------------
async def _run():
    from aigroup.core import secrets
    from aigroup.core.config import load_config
    cfg = load_config()
    if not cfg.has_user_config:
        print("还没有 config.yaml：先双击 setup.bat（或者 python run.py setup）配置一次。")
        return EXIT_NO_RESTART
    if secrets.migrate(cfg.root):                # config.yaml 里的明文密钥挪进 .env
        cfg = load_config()
    secrets.install_redaction(secrets.secret_values(cfg))
    from aigroup.app import quiet_console_text, run, setup_file_logging
    log.info("日志也会写进 %s", setup_file_logging(cfg.root))
    quiet_console_text(not cfg.get("log_text_console", False))
    return EXIT_NO_RESTART if await run(cfg) == "stop" else 0


def cmd_run(argv: list[str]) -> int:
    try:
        return asyncio.run(_run())
    except KeyboardInterrupt:
        return 0


async def _check():
    from aigroup.app import check as check_cfg
    from aigroup.core.config import load_config, resolve
    from aigroup.llm.client import LLM
    from aigroup.persona import load_persona
    cfg = load_config()
    ok = True
    for p in check_cfg(cfg):
        print("❌", p)
        ok = False
    p = load_persona(resolve(cfg, cfg.persona_file))
    print(f"✅ 人设：{p.name}（{cfg.persona_file}）" + ("".join(f"\n   ⚠️ {w}" for w in p.warnings)))
    q = cfg.get("wechat") or {}
    try:
        from aigroup.wechat import wxui
        from aigroup.wechat.bot import open_data
        d, notes = await asyncio.to_thread(open_data, dict(q), cfg.root)
        if d is None:
            raise RuntimeError("；".join(notes[-3:]))
        d.refresh_all()
        d.load_contacts(force=True)
        print(f"✅ 微信：{(d.contacts.get(d.self_wxid) or {}).get('nick') or ''}（{d.self_wxid}）")
        ws = wxui.windows()
        for g in q.get("groups") or []:
            g = str(g)
            nm = d.room_name(g)
            if g not in d.rooms:
                print(f"❌ 不在群 {g}")
                ok = False
                continue
            from aigroup.wechat.bot import send_problem
            bad = send_problem(d, g, q.get("window_titles"))
            w = wxui.chat_window(wxui.title_for(q.get("window_titles") or {}, g, nm), ws)
            if bad:
                print(f"❌ {bad}")
                ok = False
            else:
                print(f"✅ 在群「{nm}」（{len(d.members(g))} 人）；" + ("有单独窗口" if w else
                      "❌ 没有单独窗口（发不了消息：在微信里双击这个群单独打开它）"))
        own = str(q.get("owner") or "")
        if own:
            print(f"✅ 你：{d.display(own)}（{own}）")
        else:
            print("❌ wechat.owner 没填（你自己的微信号）")
            ok = False
        if wxui.screen_locked():
            print("⚠️ 屏幕锁着：锁屏时发不了消息（关显示器没关系）")
    except Exception as e:  # noqa: BLE001
        print(f"❌ 读不了微信：{e}（python run.py wxdiag 看详细）")
        ok = False
    try:
        r = await LLM(cfg).chat([{"role": "user", "content": "回一个字：好"}], max_tokens=10)
        print(f"✅ 大模型：{r.strip()[:20]}")
    except Exception as e:  # noqa: BLE001
        print(f"❌ 大模型调不通：{str(e)[:200]}")
        ok = False
    return ok



def cmd_check(argv: list[str]) -> int:
    return 0 if asyncio.run(_check()) else 1


def cmd_setup(argv: list[str]) -> int:
    _quiet()
    from aigroup.wizard import main
    return main(argv)


def cmd_people(argv: list[str]) -> int:
    import json

    from aigroup.core.config import load_config, resolve
    from aigroup.core.store import Store
    from aigroup.memory.people import active_time, familiarity
    c = load_config()
    db = Store(resolve(c, "data/group.db"))
    try:
        rows = db.conn.execute("SELECT * FROM qq_people ORDER BY msgs DESC").fetchall()
    except Exception:  # noqa: BLE001
        rows = []
    if not rows:
        print("她还不认识群里的人（群里聊一阵子就有了）。")
    for r in rows:
        prof = json.loads(r["profile"]) if r["profile"] else {}
        hours = json.loads(r["hours"]) if r["hours"] else []
        print(f"\n{r['name']}（{r['user_id']}）· 说了 {r['msgs']} 句 · 找她 {r['mentions']} 次 · "
              f"{familiarity(r['msgs'], r['mentions'])}" + (f" · {active_time(hours)}" if active_time(hours) else ""))
        if not prof and r["note"]:
            print(f"  印象：{r['note']}")
        for k, v in prof.items():
            print(f"  {k}：{'；'.join(v) if isinstance(v, list) else v}")
    return 0


def _forget(argv: list[str], what: str) -> int:
    from aigroup.core.config import load_config, resolve
    from aigroup.core.store import Store
    from aigroup.memory.forget import forget_group, forget_person
    if not argv or not argv[0].strip():
        print(f"用法：python run.py forget-{what} 编号（{'群 id' if what == 'group' else '群友编号，小屋“群里的人”里看'}）")
        return 2
    c = load_config()
    db = Store(resolve(c, "data/group.db"))
    key = argv[0].strip()
    if what == "group" and key.endswith("@chatroom"):            # 微信群 id → 编号
        import sqlite3
        from pathlib import Path
        ids = Path(c.root) / "data" / "wechat" / "ids.db"
        row = sqlite3.connect(ids).execute("SELECT id FROM ids WHERE wxid=?", (key,)).fetchone() if ids.exists() else None
        if not row:
            print("没找到这个群")
            return 1
        key = str(row[0])
    if not key.isdigit():
        print("要写编号（数字）")
        return 2
    if "--yes" not in argv and input(f"真的要彻底删掉{'这个群' if what == 'group' else '这个人'}（{key}）在她这里的所有记录吗？输入 yes：").strip() != "yes":
        print("没删")
        return 1
    n = (forget_group if what == "group" else forget_person)(db, int(key))
    print("删掉了：" + ("、".join(f"{k} {v} 条" for k, v in n.items() if v) or "（本来就没有）"))
    print("data/backup/ 里的旧备份没动：要连备份一起清，把那几个文件删掉。")
    return 0


def cmd_forget_person(argv: list[str]) -> int:
    return _forget(argv, "person")


def cmd_forget_group(argv: list[str]) -> int:
    return _forget(argv, "group")


def cmd_llm(argv: list[str]) -> int:
    _quiet()
    from aigroup.llm.custom import main
    return main(argv)


def cmd_wxdiag(argv: list[str]) -> int:
    from aigroup.wechat import diag
    return diag.diag()


def cmd_wxkeys(argv: list[str]) -> int:
    from aigroup.wechat import diag
    return diag.keys_cmd(argv)


def cmd_wxsend(argv: list[str]) -> int:
    from aigroup.wechat import diag
    return diag.send_cmd(argv)


def cmd_wxtail(argv: list[str]) -> int:
    from aigroup.wechat import diag
    return diag.tail_cmd(argv)


def cmd_wxexport(argv: list[str]) -> int:
    from aigroup.wechat import diag
    return diag.export_cmd(argv)


def cmd_seed(argv: list[str]) -> int:
    from aigroup.seed import main
    return main(argv)


def cmd_seedgen(argv: list[str]) -> int:
    from aigroup.onboard import seedgen_cmd
    return seedgen_cmd(argv)


def cmd_onboard(argv: list[str]) -> int:
    from aigroup.onboard import onboard_cmd
    return onboard_cmd(argv)


def cmd_replay(argv: list[str]) -> int:
    from aigroup.eval.replay import main
    return main(argv)


def cmd_calibrate(argv: list[str]) -> int:
    from aigroup.eval.replay import main
    return main(["calibrate"] + argv)


# 名字 → (说明, 函数)
COMMANDS = {
    "run": ("启动她：接上电脑上的微信、在群里聊天；不带命令就是这个", cmd_run),
    "setup": ("配置向导：找到微信和她的账号、拿密钥、选群、选你 + 人设 + 大模型接口", cmd_setup),
    "check": ("检查配置、连一下微信和大模型（不在群里说话）", cmd_check),
    "llm": ("大模型接口：填地址和 Key → 测连通 → 列出模型选一个 → 试聊 → 加进路由", cmd_llm),
    "people": ("看看她心里群里每个人的样子（画像）", cmd_people),
    "forget-person": ("彻底忘掉一个人：聊天记录、画像、事实……全删（python run.py forget-person 编号）", cmd_forget_person),
    "forget-group": ("彻底忘掉一个群（退群以后用；python run.py forget-group 群 id 或编号）", cmd_forget_group),
    "wxdiag": ("微信接入体检：版本、数据目录、密钥、能不能读到群消息、窗口、锁屏", cmd_wxdiag),
    "wxkeys": ("重新拿微信数据库的密钥（--deep：慢，但不挑微信版本）", cmd_wxkeys),
    "wxsend": ("试发一句：python run.py wxsend 群名 要说的话（发完在库里核对）", cmd_wxsend),
    "wxtail": ("实时打印群里的新消息（只读，不回话）：python run.py wxtail [群名]", cmd_wxtail),
    "wxexport": ("把某个号（比如你的大号）里几个群的聊天记录导出成文本（给 seedgen 整理用；只读）", cmd_wxexport),
    "onboard": ("进群前的准备（一条龙）：导出群里以前的聊天记录 → 大模型整理成“认识大家”的资料草稿 → 你看过改过 → 灌给她", cmd_onboard),
    "seedgen": ("把导出的聊天记录整理成 data/seed_draft.yaml（每个群在聊什么、群梗、每个人的画像）：python run.py seedgen [导出目录]", cmd_seedgen),
    "seed": ("把写好的“认识大家”的资料（群笔记、每个人的画像、别名、群梗、按群的开关）灌给她：python run.py seed 文件.yaml", cmd_seed),
    "replay": ("回放最近 7 天（--llm 20 用现在的模型重跑 20 条、--judge 20 评审、--export 导出脱敏包）", cmd_replay),
    "calibrate": ("拿群友自己打的字过一遍质检，看会不会误伤", cmd_calibrate),
}


def usage() -> str:
    cmds = COMMANDS
    w = max(map(len, cmds))
    return "用法：python run.py [命令]\n\n" + "\n".join(f"  {k.ljust(w)}  {v[0]}" for k, v in cmds.items())


def main(argv: list[str] | None = None) -> int:
    _setup_console()
    argv = list(sys.argv[1:] if argv is None else argv)
    args = [a for a in argv if not a.startswith("--")]
    name = args[0] if args else "run"
    if name not in COMMANDS:
        print(usage())
        return 0 if name in ("help", "-h") else 1
    rest = argv[argv.index(name) + 1:] if name in argv else argv
    return int(COMMANDS[name][1](rest) or 0)
