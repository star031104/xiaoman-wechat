"""把事先写好的“认识大家”的资料灌进她的数据（python run.py seed 文件.yaml）。

她刚进群什么都不认识；这个文件可以让她第一天就知道每个群在聊什么、群里每个人是谁、大家怎么叫他、
有哪些梗、哪些话题别碰。文件放在 data/ 里（不进 git），格式：

    groups:
      - name: 群名                      # 或者 chat: xxxxx@chatroom
        notes: |                        # 这个群最近的事和梗（她每整理一次群聊会在这个基础上更新）
          - ……
        norms: |                        # 这个群不成文的规矩
          - ……
        self_notes: |                   # 她在这个群里说过的关于自己的事（可以不写）
        knobs: {talkativeness: 0.25}    # 只给这个群调的性格开关（小屋“性格开关”里能看到、能改）
        lore:                           # 群梗词条：聊到关键词才告诉她这个梗的意思
          - keys: [关键词, 关键词]
            text: 这个梗是什么意思
        people:
          - wxid: wxid_xxx              # 微信号（导出的聊天记录开头有）
            name: 群名片
            aliases: [外号, 真名]        # 大家还怎么叫他
            profile: {印象: …, 称呼: …, 性格: …, 说话风格: …, 兴趣: …, 近况: …, 群里关系: …, 和你: …, 怎么聊: …, 别碰: …,
                      记得的事: [ …, … ]}

重跑会覆盖同样的内容（画像、群笔记、开关），不会删别的。改完要让她重启一下（托盘 → 重启）。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import yaml

from aigroup.core.config import ROOT, load_config, resolve
from aigroup.core.util import say


def _clean_profile(prof: dict) -> dict:
    from aigroup.memory.people import MEMORY_CHARS, PROFILE_CHARS, PROFILE_KEYS, PROFILE_MEMORIES
    out = {}
    for k in PROFILE_KEYS:
        v = (prof or {}).get(k)
        if k == "记得的事":
            items = v if isinstance(v, list) else str(v or "").split("\n")
            items = [str(i).strip()[:MEMORY_CHARS] for i in items if str(i).strip()][:PROFILE_MEMORIES]
            if items:
                out[k] = items
        elif v:
            out[k] = str(v).strip()[:PROFILE_CHARS]
    return out


def apply(seed: dict, db, ids, chat_of_name, now: float | None = None) -> dict:
    """把 seed 写进 db。ids：wxid → 编号；chat_of_name：群名 → 群的 chat id（找不到返回 ""）。返回统计。"""
    from aigroup.chat.settings import GLOBAL_ONLY, KNOBS, clean_knob, group_key
    from aigroup.core.store import ensure_columns
    from aigroup.memory.lore import Lore
    from aigroup.memory.people import PEOPLE_COLUMNS
    from aigroup.memory.people import SCHEMA as PEOPLE_SCHEMA
    now = now or time.time()
    db.conn.executescript(PEOPLE_SCHEMA)
    ensure_columns(db.conn, "qq_people", PEOPLE_COLUMNS)
    lore = Lore(db)
    st = {"groups": 0, "people": 0, "lore": 0, "missing": []}
    for g in seed.get("groups") or []:
        chat = str(g.get("gid") or g.get("chat") or "").strip() or chat_of_name(str(g.get("name") or ""))   # QQ 写 gid，微信写 chat / name
        if not chat:
            st["missing"].append(str(g.get("name") or "?"))
            continue
        gid = ids(chat)
        st["groups"] += 1
        for key, field, cap in (("notes", "qq_group_notes_", 1500), ("norms", "qq_group_norms_", 800),
                                ("self_notes", "qq_self_notes_", 1200)):
            if str(g.get(key) or "").strip():
                db.set(f"{field}{gid}", str(g[key]).strip()[:cap])
        knobs = {k: clean_knob(k, v) for k, v in (g.get("knobs") or {}).items() if k in KNOBS and k not in GLOBAL_ONLY}
        knobs = {k: v for k, v in knobs.items() if v is not None}
        if knobs:
            db.set(group_key(gid), {**(db.get(group_key(gid)) or {}), **knobs})
        for e in g.get("lore") or []:
            if lore.add(gid, [str(k) for k in e.get("keys") or []], str(e.get("text") or ""), source="manual"):
                st["lore"] += 1
        seeded = set(db.get(f"qq_seeded_{gid}") or [])
        for p in g.get("people") or []:
            wxid = str(p.get("qq") or p.get("wxid") or "").strip()                 # QQ 写 qq，微信写 wxid
            if not wxid:
                continue
            uid = ids(wxid)
            seeded.add(int(uid))              # 5.0 记下哪些人是“事先听说的”：真正说上话以前，她不把听来的私事说出来
            name = str(p.get("name") or wxid)
            db.conn.execute("INSERT OR IGNORE INTO qq_people(user_id, name) VALUES (?, ?)", (uid, name))
            if p.get("aliases"):
                al = p["aliases"] if isinstance(p["aliases"], list) else str(p["aliases"]).replace("，", "、").split("、")
                db.conn.execute("UPDATE qq_people SET aliases=? WHERE user_id=?",
                                ("、".join(dict.fromkeys(str(a).strip() for a in al if str(a).strip())), uid))
            prof = _clean_profile(p.get("profile") or {})
            if prof:
                js, note = json.dumps(prof, ensure_ascii=False), str(prof.get("印象") or "")[:80]
                db.conn.execute(
                    "INSERT INTO qq_profiles(user_id, group_id, profile, note, since_update, updated) VALUES (?,?,?,?,0,?) "
                    "ON CONFLICT(user_id, group_id) DO UPDATE SET profile=excluded.profile, note=excluded.note, "
                    "updated=excluded.updated", (uid, gid, js, note, now))
                db.conn.execute("UPDATE qq_people SET profile=?, note=?, updated=? WHERE user_id=?", (js, note, now, uid))
            st["people"] += 1
        db.set(f"qq_seeded_{gid}", sorted(seeded))
    db.conn.commit()
    return st


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 1
    path = Path(argv[0])
    if not path.is_absolute():
        path = ROOT / path
    try:
        seed = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as e:
        say(False, f"读不了 {path}：{e}")
        return 1
    c = load_config()
    from aigroup.core.store import Store
    if c.channel == "qq":                                       # QQ：群号、QQ 号就是编号
        db = Store(resolve(c, "data/group.db"))
        st = apply(seed, db, lambda x: int(str(x).strip()), lambda name: "")
        say(True, f"写好了：{st['groups']} 个群、{st['people']} 个人、{st['lore']} 条群梗")
        for m in st["missing"]:
            say(False, f"「{m}」没写群号（gid）")
        print("  让她重启一下就生效；小屋“群友”“学到的”页能看到、能改")
        return 0 if not st["missing"] else 1
    w = dict(c.get("wechat") or {})
    from aigroup.wechat.bot import IdMap, open_data
    ids = IdMap(ROOT / "data" / "wechat" / "ids.db")
    d = None
    if any(not g.get("chat") for g in seed.get("groups") or []):
        print("  正在读她的微信，按群名找群…")
        d, notes = open_data(w, ROOT)
        if d is None:
            for n in notes:
                print("    " + n)
            say(False, "读不了她的微信（她的小号的微信开着吗？），没法按群名找群；也可以在文件里写 chat: 群id")
            return 1
        d.load_contacts(force=True)

    def by_name(name: str) -> str:
        if d is None or not name:
            return ""
        hits = [g for g in d.rooms if d.room_name(g) == name]
        return hits[0] if len(hits) == 1 else ""
    db = Store(resolve(c, "data/group.db"))
    st = apply(seed, db, ids.id, by_name)
    if d is not None:
        d.close()
    say(True, f"写好了：{st['groups']} 个群、{st['people']} 个人、{st['lore']} 条群梗")
    for m in st["missing"]:
        say(False, f"没找到群「{m}」（她的小号在这个群里吗？群名一字不差吗？）")
    print("  让她重启一下就生效（托盘图标右键 → 重启）；小屋“群友”页能看到、能改")
    return 0 if not st["missing"] else 1
