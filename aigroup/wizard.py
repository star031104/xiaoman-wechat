"""配置向导（setup.bat → python run.py setup）：一次配好微信群聊角色。

1. 大模型接口：旁边装了 ai-life-group（QQ 群版）就问要不要用它那一套；否则填你自己的接口地址和 Key（放在最后一步做）
2. 找到电脑上的微信、她的小号，拿数据库密钥（只读微信内存，不改微信；拿不到可以选深度扫描）
3. 选她待哪几个群（白名单）、群里哪个是你、人设、群里怎么叫她、她怎么看你
4. 检查每个群怎么发消息（有单独窗口最好；没有就看群名搜得准不准）
5. 写 config.yaml（只写和默认不一样的项）和 .env（密钥）；问要不要开机自启、现在启动

    python run.py setup            全部来一遍（已经配好的项回车就保留原样）
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import yaml

from aigroup.core.config import ROOT
from aigroup.core.util import ask, say, yes


def pick_numbers(v: str, n: int) -> list[int]:
    """“1,3 4” → [0, 2, 3]（编号从 1 开始；越界的丢掉）。"""
    out = []
    for x in re.split(r"[\s,，、]+", v.strip()):
        if x.isdigit() and 1 <= int(x) <= n and int(x) - 1 not in out:
            out.append(int(x) - 1)
    return out


def launch_hidden() -> None:
    """在后台启动她：wscript 跑 start.vbs（没有黑框，只有托盘图标）。"""
    from aigroup.core.config import CODE
    vbs = CODE / "start.vbs"
    if sys.platform == "win32" and vbs.exists():
        subprocess.Popen(["wscript.exe", str(vbs), "wechat"], cwd=str(CODE),
                         creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
        return
    subprocess.Popen(["cmd", "/c", str(CODE / "start.bat"), "wechat"], cwd=str(CODE),
                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _forget_others(keep: str) -> None:
    """只留她这个号的密钥和解密副本：以前读过的别的号（比如选错成了你的大号）都删掉。"""
    import json

    from aigroup.wechat.bot import forget_account
    names = set()
    try:
        names |= set(json.loads((ROOT / "data" / "wechat" / "keys.json").read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass
    cache = ROOT / "data" / "wechat" / "cache"
    if cache.is_dir():
        names |= {x.name for x in cache.iterdir() if x.is_dir()}
    for a in names - {keep}:
        forget_account(ROOT, a)


def _pick_account(wx_opts: dict, old_account: str, others: list[str]):
    """开了不止一个微信（比如大号 + 小号）：列出来让你选哪个是她的小号。
    返回选好的库；只有一个号时返回 None（交给后面的老流程）；放弃返回 False。"""
    from aigroup.wechat.bot import accounts_here, forget_account, open_data
    accs = accounts_here(wx_opts)
    logged = [a for a in accs if a["logged"]]
    if not logged and len(accs) > 1 and any(a["logged"] is None for a in accs):
        logged = accs[:5]                                             # 认不出谁登录着：都列出来让你选
    if len(logged) <= 1:
        if len(logged) == 1:
            wx_opts["account"] = logged[0]["account"]                 # 只开着一个：就是它（别挑到以前登录过的旧号）
        return None
    print(f"  这台电脑上有 {len(logged)} 个微信号，要分清哪个是她的小号（第一次要多等一会儿）…")
    opened = []
    for a in logged[:5]:
        dd, notes = open_data({**wx_opts, "account": a["account"]}, ROOT)
        nick = ""
        if dd is not None:
            try:
                dd.refresh_all()
                dd.load_contacts(force=True)
                nick = (dd.contacts.get(dd.self_wxid) or {}).get("nick") or ""
            except Exception:  # noqa: BLE001
                pass
        opened.append((a, dd, nick, notes))
    for i, (a, dd, nick, notes) in enumerate(opened, 1):
        tail = "" if dd is not None else "（读不到库：" + ("；".join(notes[-1:]) or "密钥没拿到") + "）"
        print(f"  {i}. {nick or '（没取到昵称）'}（{a['wxid']}）{tail}")
    dflt = next((str(i) for i, (a, *_r) in enumerate(opened, 1) if a["account"] == old_account), "")
    pick = None
    for _ in range(5):
        v = ask("哪个是她（小满）的小号？输入编号（别选你自己的大号）", dflt)
        if v.lower() == "q":
            break
        if v.isdigit() and 1 <= int(v) <= len(opened) and opened[int(v) - 1][1] is not None:
            pick = opened[int(v) - 1]
            break
        say(False, "输入上面列出来、读得到库的编号（q 退出）")
    for a, dd, _n, _x in opened:
        if pick is None or a is not pick[0]:
            others.append(a["wxid"])
            if dd is not None:
                dd.close()
            forget_account(ROOT, a["account"])                         # 没选的号（你的大号）：密钥、副本都不留
    if pick is None:
        return False
    return pick[1]


def list_personas(root: Path = ROOT) -> list[dict]:
    from aigroup.persona import load_persona
    out = []
    for f in sorted((root / "persona").glob("*.md")):
        if f.name == "template.md":
            continue
        try:
            p = load_persona(f)
            out.append({"file": f"persona/{f.name}", "name": p.name, "identity": getattr(p, "identity", "")})
        except Exception:  # noqa: BLE001
            continue
    return out


def new_persona_from_template(root: Path = ROOT) -> dict:
    """还没有自己的人设：从仓库的 persona/template.md（一个写好的示例）复制一份到她的家里，让你改成自己的角色。"""
    from aigroup.core.config import CODE
    print("  你还没有自己的人设。她是谁由你来定：先从模板（一个写好的示例角色）复制一份，你再改成自己的。")
    name = ask("  她叫什么名字", "小满").strip() or "小满"
    fn = ask("  人设文件名（英文或拼音）", "her").strip() or "her"
    fn = re.sub(r"[^A-Za-z0-9_-]", "", fn) or "her"
    dst = root / "persona" / f"{fn}.md"
    dst.parent.mkdir(parents=True, exist_ok=True)
    if not dst.exists():
        text = (CODE / "persona" / "template.md").read_text(encoding="utf-8")
        if name != "小满":
            text = text.replace("name: 小满 ", f"name: {name} ", 1).replace("小满：", f"{name}：")
        dst.write_text(text, encoding="utf-8")
    say(True, f"写好了 {dst}：里面是示例内容，记得把身份、性格、说话方式、对话示例都改成你自己的角色（改完重启她生效）")
    try:
        if sys.platform == "win32":
            os.startfile(str(dst))                     # noqa: S606
    except OSError:
        pass
    return {"file": f"persona/{fn}.md", "name": name, "identity": ""}


def build_config(old: dict, *, persona_file: str, keys: dict[str, str], owner: str, groups: list[str],
                 aliases: list[str], owner_note: str, account: str, data_dir: str) -> str:
    """config.yaml 只写和 config.example.yaml 不一样的项（别的用默认值）；原来手动加的设置保留。"""
    d = dict(old or {})
    d["persona_file"] = persona_file
    if keys:
        d["keys"] = {**(d.get("keys") or {}), **{k: f"${{{v}}}" for k, v in keys.items()}}
    w = dict(d.get("wechat") or {})
    w.update({"owner": owner, "groups": groups, "aliases": aliases, "account": account})
    if data_dir:
        w["data_dir"] = data_dir
    g = dict(w.get("group") or {})
    g["owner_note"] = owner_note
    w["group"] = g
    d["wechat"] = w
    head = ("# 小满的设置（setup.bat 写的；没写的项用 config.wechat.example.yaml 里的默认值，想改就从那里抄过来）\n"
            "# 密钥都在 .env 里，这里只写 ${名字} 引用\n\n")
    return head + yaml.safe_dump(d, allow_unicode=True, sort_keys=False, default_flow_style=None)


def sibling_qq(root: Path = ROOT) -> Path | None:
    """旁边的 QQ 群版（12.0 起是 home/qq；以前是旁边的 ai-life-group 文件夹），而且它配过大模型接口（config.llm.yaml）。"""
    for d in (root.parent / "qq", root.parent / "ai-life-group", root.parent.parent / "ai-life-group"):
        try:
            if (d / "config.llm.yaml").exists() and d.resolve() != root.resolve():
                return d
        except OSError:
            continue
    return None


def borrow_llm(src: Path, root: Path = ROOT) -> int:
    """把 QQ 群版的大模型配置（config.llm.yaml + 里面用到的 Key）拿过来；返回拿了几个 Key。
    这是你自己配的接口，不是程序预置的。"""
    from aigroup.core.secrets import read_env, write_env
    text = (src / "config.llm.yaml").read_text(encoding="utf-8")
    dst = root / "config.llm.yaml"
    if dst.exists():
        b = root / "data" / "backup" / f"config.llm.yaml.{time.strftime('%Y%m%d-%H%M%S')}"
        b.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(dst, b)
    dst.write_text(text, encoding="utf-8")
    env = read_env(src)
    need = {v for v in re.findall(r"\$\{([A-Za-z0-9_]+)\}", text) if env.get(v)}
    try:
        other = yaml.safe_load((src / "config.yaml").read_text(encoding="utf-8")) or {}
        for v in (other.get("keys") or {}).values():
            m = re.fullmatch(r"\$\{([A-Za-z0-9_]+)\}", str(v))
            if m and env.get(m.group(1)):
                need.add(m.group(1))
    except (OSError, yaml.YAMLError):
        pass
    if need:
        write_env(root, {k: env[k] for k in sorted(need)})
    return len(need)


def main(argv: list[str] | None = None) -> int:
    from aigroup.core.secrets import read_env, write_env
    from aigroup.wechat.bot import open_data
    argv = argv or []
    print("=" * 56 + "\n  xiaoman-wechat 配置向导\n" + "=" * 56)
    print("要准备：\n"
          "  · 给她一个微信小号，用你的微信把它拉进想让她待的群\n"
          "  · 这台电脑上的微信（4.x）登录她的小号，并保持开着\n"
          "⚠️ 自动化操作微信有被限制的风险，一定用小号，别用主号。\n")
    if sys.platform != "win32" and not sys.platform.startswith("linux"):
        say(False, "微信版只能在 Windows 或 Linux 上用")
        return 1
    cfg_path = ROOT / "config.yaml"
    old: dict = {}
    if cfg_path.exists():
        try:
            old = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        except Exception:  # noqa: BLE001
            old = {}
    old_w = old.get("wechat") or {}
    read_env(ROOT)

    # 1. 大模型接口（程序里不预置任何接口，用你自己的）
    from aigroup.llm.custom import load_custom
    print("① 大模型接口：用你自己的（任何 OpenAI 兼容的接口），要准备好接口地址和 Key")
    have_llm = bool(((load_custom(ROOT).get("llm") or {}).get("profiles") or {})) or any(
        ((old.get("llm") or {}).get("routes") or {}).get("chat") or [])
    src = sibling_qq()
    own_llm = False
    if have_llm:
        own_llm = yes("  已经配过接口了，要再加一个吗", False)
    elif src and yes(f"  在 {src} 找到了 QQ 群版的大模型配置，微信版也用这一套吗", True):
        n = borrow_llm(src)
        say(True, f"用上了 QQ 群版的大模型配置（{n} 个 Key 写进了 .env）")
    else:
        own_llm = True
        say(None, "还没有接口：配完微信之后会让你填地址和 Key、测连通、选模型")
    key_names: dict[str, str] = {}
    env_new: dict[str, str] = {}

    # 2. 微信
    print("\n② 她的微信")
    d = None
    wx_opts = {k: v for k, v in old_w.items() if k != "account"}      # 重新选号：不被上次记的账号卡住（取最近在用的那个）
    others: list[str] = []                                             # 这台电脑上登录着的别的号（多半是你的大号）
    d = _pick_account(wx_opts, str(old_w.get("account") or ""), others)
    if d is False:
        return 1
    if d is not None:
        wx_opts["account"] = d.account_dir.name
    for _ in range(5 if d is None else 0):
        print("  正在找微信、读密钥（第一次可能要十几秒）…")
        d, notes = open_data(wx_opts, ROOT, progress=lambda m: print("    " + m))
        for n in notes:
            print("    " + n)
        if d is not None:
            break
        say(False, "还没读到微信的库")
        print("  · 微信开着、登录了她的小号吗？\n  · 都对的话，回车再试；输入 deep 用深度扫描（慢，几分钟，不挑微信版本）")
        v = ask("  回车重试 / deep / q 退出", "")
        if v.lower() == "q":
            return 1
        if v.lower() == "deep":
            d, notes = open_data(wx_opts, ROOT, progress=lambda m: print("    " + m), deep=True)
            for n in notes:
                print("    " + n)
            if d is not None:
                break
    if d is None:
        say(False, "读不到微信的库。运行 python run.py wxdiag，看看是哪一步断了")
        return 1
    if d.self_wxid in others:
        others.remove(d.self_wxid)
    _forget_others(d.account_dir.name)
    d.refresh_all()
    d.load_contacts(force=True)
    me = (d.contacts.get(d.self_wxid) or {}).get("nick") or ""
    say(True, f"读到了：她的微信是 {me or '（没取到昵称）'}（{d.self_wxid}）")

    # 3. 选群
    print("\n③ 她待哪几个群")
    groups: list[dict] = []
    for _ in range(6):
        groups = [g for g in d.groups() if g["members"] or g["has_msgs"]]
        if groups:
            break
        ask("她的小号还不在任何群里：先用你的微信把它拉进群，在群里随便说句话，然后回车刷新")
        d.refresh_all()
        d.load_contacts(force=True)
    if not groups:
        say(False, "还是没有群。拉她进群后再运行一次 setup.bat")
        return 1
    groups = groups[:40]
    for i, g in enumerate(groups, 1):
        nm = g["name"]
        if nm == g["id"]:                                    # 没起群名：列几个成员认一认
            who = "、".join(d.display(u, g["id"]) for u in d.members(g["id"])[:4] if u != d.self_wxid)
            nm = f"（没有群名：{who}…）"
        print(f"  {i}. {nm}（{g['members'] or '?'} 人{'' if g['has_msgs'] else '，还没有消息'}）")
    old_groups = [str(x) for x in old_w.get("groups") or []]
    default = ",".join(str(i + 1) for i, g in enumerate(groups) if g["id"] in old_groups) or \
        ("1" if len(groups) == 1 else "")
    picked: list[str] = []
    for _ in range(5):
        picked = [groups[i]["id"] for i in pick_numbers(ask("输入编号，多个用逗号隔开（白名单：别的群她一律不说话）",
                                                              default), len(groups))]
        if picked:
            break
        say(False, "至少选一个群")
    if not picked:
        return 1

    # 4. 你
    print("\n④ 群里哪个是你")
    mem: dict[str, str] = {}
    for g in picked:
        for u in d.members(g):
            if u != d.self_wxid:
                mem.setdefault(u, d.display(u, g))
    owner = str(old_w.get("owner") or "")
    big = [u for u in others if u in mem]                    # 开着的另一个微信号也在这些群里：多半就是你
    if big and not owner:
        owner = big[0]
        print(f"  这台电脑上开着的另一个微信「{mem[owner]}」也在这些群里，先当成是你（不对就在下面换）")
    people = sorted(mem.items(), key=lambda x: x[1])
    if people:
        kw = ask("输入你在群里的名字（或其中几个字）来找你", mem.get(owner, ""))
        hits = [(u, n) for u, n in people if kw and (kw in n or kw == u)] or people
        for i, (u, n) in enumerate(hits[:30], 1):
            print(f"  {i}. {n}（{u}）")
        v = ask("哪个是你（编号）", "1" if len(hits) == 1 else "")
        if v.isdigit() and 1 <= int(v) <= min(30, len(hits)):
            owner = hits[int(v) - 1][0]
    if not owner:
        owner = ask("没找到：直接输入你的微信号（wxid_ 开头那个）", owner)
    if not owner:
        say(False, "要知道哪个是你")
        return 1
    say(True, f"你是 {mem.get(owner) or d.display(owner)}（{owner}）")

    # 5. 人设、名字
    print("\n⑤ 她")
    ps = list_personas()
    if not ps:
        ps = [new_persona_from_template()]
    print("她用哪个人设？（home/wechat/persona/ 里的文件；想再写一个：复制 persona/template.md 改好放进去，再来选）")
    for i, p in enumerate(ps, 1):
        print(f"  {i}. {p['name']}（{p.get('identity') or '—'}）  {p['file']}")
    old_pf = str(old.get("persona_file") or "")
    dflt = next((str(i) for i, p in enumerate(ps, 1) if old_pf.endswith(p["file"])), "1")
    v = ask("选哪个", dflt)
    chosen = ps[int(v) - 1] if v.isdigit() and 1 <= int(v) <= len(ps) else (ps[0] if ps else
                                                                          {"file": old_pf, "name": "她"})
    auto = sorted({n for g in picked for n in d.self_names(g)})
    if auto:
        print(f"  她的微信昵称 / 群昵称：{'、'.join(auto)}（这些自动算叫她）")
    old_alias = [a for a in old_w.get("aliases") or [] if a]
    al = ask(f"群里除了“{chosen['name']}”，大家还会怎么叫她？（逗号隔开，可以不填）", "，".join(old_alias))
    aliases = [a.strip() for a in re.split(r"[,，、\s]+", al) if len(a.strip()) >= 2]
    note = ask("她怎么看你？（一句话）",
               str((old_w.get("group") or {}).get("owner_note") or "把你拉进群的老朋友，爱和你互损"))

    # 6. 发消息的方式
    print("\n⑥ 发消息")
    from aigroup.wechat import wxui
    from aigroup.wechat.keys import owner_pids
    own = owner_pids(d.account_dir)
    ws = wxui.windows(own) if own else ([] if own == [] else wxui.windows())   # 开了两个微信：只看她那个的窗口
    if others and own:
        print("  （你的大号也开着没关系：她只认小号那个微信里的窗口；小号那边的群要单独开窗口，大号那边的不算）")
    from aigroup.wechat.bot import send_problem
    for g in picked:
        nm = d.room_name(g)
        bad = send_problem(d, g, old_w.get("window_titles"))
        if bad:                                              # 没群名 / 同名：开了窗口也发不了，先说这个
            say(False, bad)
        elif wxui.chat_window(wxui.title_for(old_w.get("window_titles") or {}, g, nm), ws):
            say(True, f"「{nm}」有单独窗口：可以发消息")
        else:
            say(False, f"「{nm}」还没有单独窗口：在微信里双击这个群，让它单独开一个窗口（可以最小化，以后别关）——"
                       "没有单独窗口她发不了消息（只认窗口标题，保证不会发错群）")
    print("  另外：电脑可以关显示器，但别锁屏（锁屏时没法发消息）；她发消息时会短暂切到微信窗口，发完切回来")

    # 7. 写配置
    text = build_config(old, persona_file=chosen["file"], keys=key_names, owner=owner, groups=picked,
                        aliases=aliases, owner_note=note, account=d.account_dir.name,
                        data_dir=str(old_w.get("data_dir") or ""))
    if cfg_path.exists():
        b = ROOT / "data" / "backup" / f"config.yaml.{time.strftime('%Y%m%d-%H%M%S')}"
        b.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(cfg_path, b)
        say(None, f"原来的 config.yaml 备份在 {b.relative_to(ROOT)}")
    if env_new:
        write_env(ROOT, env_new)
    tmp = cfg_path.with_suffix(".yaml.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(cfg_path)
    try:
        from aigroup.core.config import load_config
        c = load_config(cfg_path)
        w = c.get("wechat") or {}
        assert [str(x) for x in w.get("groups")] == picked and str(w.get("owner")) == owner
    except Exception as e:  # noqa: BLE001
        say(False, f"写完核对不对（{e}）：看一下 config.yaml")
        return 1
    say(True, f"config.yaml 写好了：{chosen['name']}，群 {[d.room_name(g) for g in picked]}；密钥在 .env 里（别发给别人）")
    if own_llm:
        import asyncio

        from aigroup.llm.custom import add_interactive
        try:
            asyncio.run(add_interactive(load_config(cfg_path), ("helper", "memory", "night"), ROOT))
        except Exception as e:  # noqa: BLE001
            say(False, f"大模型接口没加上：{e}（之后双击 scripts\\llm.bat 再加）")

    try:
        c2 = load_config(cfg_path)
        if not ((c2.get("llm") or {}).get("routes") or {}).get("chat"):
            say(False, "还没有群里说话用的大模型接口（llm.routes.chat 是空的）：双击 scripts\\llm.bat 加一个再启动")
            return 1
    except Exception:  # noqa: BLE001
        pass
    print()
    if yes("开机自动启动她吗", False):
        from aigroup.core.config import CODE
        subprocess.run(["cmd", "/c", str(CODE / "scripts" / "autostart_on.bat"), "wechat", "nopause"], cwd=str(CODE))
    from aigroup.app import single_instance
    lk = single_instance(ROOT / "data" / "run.lock")
    if lk is None:                                           # 她正在跑：让她重启一下，用上新的设置
        (ROOT / "data" / "tray_cmd").write_text("restart", encoding="utf-8")
        say(True, "她已经在运行了：已经让她重启，用上新的设置")
        return 0
    lk.close()
    if yes("现在就启动她吗", True):
        launch_hidden()
        say(True, "已经在后台启动了（右下角托盘图标）。去群里 @ 她试试")
        return 0
    print("以后双击 start.vbs 启动她（不开黑窗口）。日志在 data/logs/group.log。")
    return 0
