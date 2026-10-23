"""群里的人：每个群友的计数、画像（按群分开）、别名（真名 / 外号）、群成员列表。

- qq_people：跨群按 QQ 号的计数、最新一份画像、别名（小屋看的是这一份）
- qq_profiles：她在每个群里对这个人的了解（在 A 群知道的事不会在 B 群说）
- 画像怎么写在 memory/digest.py；画像怎么放进提示词在 chat/prompt.py 的 roster()
"""
from __future__ import annotations

import json
import logging
import re
import time

from aigroup.core.safety import clean_name

log = logging.getLogger("aigroup.group")


# 12.1 画像写得更细：每个字段最多多少字、记得几件事（以前是 100 字、6 件）
PROFILE_CHARS = 160
IMPRESSION_CHARS = 50
PROFILE_MEMORIES = 8
MEMORY_CHARS = 80

PROFILE_FIELDS = ["称呼", "性格", "说话风格", "口头禅", "兴趣", "看法", "近况", "群里关系", "和你", "怎么聊", "他的反应", "在意",
                  "别碰", "记得的事"]      # 14.0 加了“他的反应”（他的 if-then）和“在意”（他在群里图什么）


def familiarity(msgs: int, mentions: int) -> str:
    """她和这个人有多熟：主要看他找她的次数，其次看他在群里多活跃。"""
    score = 3 * int(mentions or 0) + min(int(msgs or 0), 300) / 10
    return "很熟" if score >= 40 else "挺熟" if score >= 12 else "刚认识"


def active_time(hours: list[int]) -> str:
    """从 24 小时的发言分布看他什么时候常出现。"""
    if not hours or sum(hours) < 15:
        return ""
    buckets = {"早上": range(6, 11), "中午": range(11, 14), "下午": range(14, 18), "晚上": range(18, 23),
               "深夜": [23, 0, 1, 2, 3, 4, 5]}
    tot = sum(hours)
    top = max(buckets, key=lambda k: sum(hours[h] for h in buckets[k]))
    share = sum(hours[h] for h in buckets[top]) / tot
    return f"常在{top}出现" if share >= 0.4 else ""


def profile_line(name: str, prof: dict, fam: str, when: str, owner_note: str = "") -> str:
    """一个人的画像拼成一行，给提示词用。"""
    p = prof or {}
    bits = []
    if owner_note:
        bits.append(owner_note)
    if p.get("印象"):
        bits.append(str(p["印象"]))
    for k in ("称呼", "性格", "说话风格", "口头禅", "兴趣", "看法", "近况", "群里关系", "和你", "怎么聊", "他的反应", "在意"):
        if p.get(k):
            bits.append(f"{k}：{p[k]}")
    if p.get("记得的事"):
        v = p["记得的事"]
        bits.append("记得：" + ("；".join(map(str, v[:PROFILE_MEMORIES])) if isinstance(v, list) else str(v)))
    if p.get("别碰"):
        bits.append(f"别碰：{p['别碰']}")
    bits.append(f"熟悉度：{fam}" + (f"，{when}" if when else ""))
    return f"- {name}：" + "；".join(bits)


PROFILE_KEYS = ["印象"] + PROFILE_FIELDS   # 小屋里能改的画像字段

SCHEMA = """
CREATE TABLE IF NOT EXISTS qq_people (     -- 群友（跨群按 QQ 号）：计数、最新一份画像（只给小屋看）
    user_id INTEGER PRIMARY KEY,
    name TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    msgs INTEGER NOT NULL DEFAULT 0,
    last_seen REAL
);
CREATE TABLE IF NOT EXISTS qq_profiles (   -- 2.0：她心里每个人的样子，按群分开（在 A 群知道的事不会在 B 群说）
    user_id INTEGER NOT NULL,
    group_id INTEGER NOT NULL,
    profile TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    msgs INTEGER NOT NULL DEFAULT 0,
    since_update INTEGER NOT NULL DEFAULT 0,
    last_seen REAL,
    updated REAL,
    PRIMARY KEY (user_id, group_id)
);
"""
PEOPLE_COLUMNS = {
    "profile": "TEXT NOT NULL DEFAULT ''",
    "mentions": "INTEGER NOT NULL DEFAULT 0",     # 这个人找她（@她、回复她、叫她、戳她）的次数
    "hours": "TEXT NOT NULL DEFAULT ''",          # 24 个小时各说了多少句（JSON 列表）
    "since_update": "INTEGER NOT NULL DEFAULT 0",
    "updated": "REAL",
    "aliases": "TEXT NOT NULL DEFAULT ''",        # 3.0.3：大家还怎么叫他（真名、外号），“、”隔开；只有你在小屋 / 配置里填的才有
}


class PeopleMixin:
    @staticmethod
    def split_aliases(v) -> list[str]:
        items = v if isinstance(v, (list, tuple)) else re.split(r"[,，、/\s]+", str(v or ""))
        out = []
        for a in items:
            a = str(a).strip()[:12]
            if len(a) >= 2 and a not in out:
                out.append(a)
        return out[:6]

    def aliases_of(self, uid: int) -> list[str]:
        r = self.db.conn.execute("SELECT aliases FROM qq_people WHERE user_id=?", (uid,)).fetchone()
        return self.split_aliases(r["aliases"]) if r else []

    def set_aliases(self, uid: int, aliases, name: str = "") -> list[str]:
        al = self.split_aliases(aliases)
        self.db.conn.execute("INSERT OR IGNORE INTO qq_people(user_id, name) VALUES (?, ?)", (uid, name or str(uid)))
        self.db.conn.execute("UPDATE qq_people SET aliases=? WHERE user_id=?", ("、".join(al), uid))
        self.db.conn.commit()
        self._alias_cache = None
        return al

    def alias_map(self) -> dict[str, int]:
        """别名 → QQ 号（她自己的名字不算；两个人填了同一个别名就都不认，免得认错人）。"""
        if getattr(self, "_alias_cache", None) is not None:
            return self._alias_cache
        seen: dict[str, int] = {}
        dup: set[str] = set()
        for r in self.db.conn.execute("SELECT user_id, aliases FROM qq_people WHERE aliases!=''").fetchall():
            for a in self.split_aliases(r["aliases"]):
                if a in self.aliases:
                    continue
                if a in seen and seen[a] != int(r["user_id"]):
                    dup.add(a)
                seen[a] = int(r["user_id"])
        self._alias_cache = {a: u for a, u in seen.items() if a not in dup}
        return self._alias_cache

    def alias_hits(self, text: str) -> list[int]:
        """一段话里用真名 / 外号提到了谁（英文别名按整词、不分大小写）。"""
        t = text or ""
        low = t.lower()
        out = []
        for a, uid in self.alias_map().items():
            if re.fullmatch(r"[A-Za-z0-9_]+", a):
                hit = re.search(rf"(?<![A-Za-z0-9_]){re.escape(a.lower())}(?![A-Za-z0-9_])", low)
            else:
                hit = a in t
            if hit and uid not in out:
                out.append(uid)
        return out

    async def refresh_members(self):
        """3.0.3：连上 QQ 后拉一遍群成员：群名片记下来（没说过话的人也知道叫什么，@ 他不再显示成 QQ 号）；
        配置里 people_aliases（“群名片或昵称: 别名、别名”）里新写的别名补进去（配置没变就不动）。"""
        seeds = self.g.get("people_aliases") or {}
        for gid in self.groups:
            try:
                ms = await self.ob.call("get_group_member_list", group_id=gid)
            except Exception as e:  # noqa: BLE001
                log.info("群 %s 拉成员列表失败：%s", gid, e)
                continue
            if not isinstance(ms, list):
                continue
            names = {}
            for m in ms:
                try:
                    uid = int(m.get("user_id") or 0)
                except (TypeError, ValueError):
                    continue
                if not uid or uid == (self.ob.self_id or 0):
                    continue
                card, nick = str(m.get("card") or "").strip(), str(m.get("nickname") or "").strip()
                name = clean_name(card or nick or str(uid))
                names[str(uid)] = name
                self.db.conn.execute("INSERT OR IGNORE INTO qq_people(user_id, name) VALUES (?, ?)", (uid, name))
                self.db.conn.execute("UPDATE qq_people SET name=? WHERE user_id=?", (name, uid))
                if isinstance(seeds, dict):
                    for k, v in seeds.items():
                        if str(k).strip() not in {card, nick, str(uid)}:
                            continue
                        seed = "、".join(self.split_aliases(v))
                        if self.db.get(f"qq_alias_seed_{uid}") == seed:
                            continue                          # 配置没变：不动（小屋里删掉的不会又冒出来）
                        have = self.aliases_of(uid)
                        add = [a for a in self.split_aliases(v) if a not in have]
                        if add:
                            self.set_aliases(uid, have + add)
                            log.info("群 %s：给 %s 加上了别名 %s", gid, name, "、".join(add))
                        self.db.set(f"qq_alias_seed_{uid}", seed)
            self.db.set(f"qq_members_{gid}", names)
            self.db.conn.commit()
        self._alias_cache = None

    def note_mention(self, uid: int):
        self.db.conn.execute("UPDATE qq_people SET mentions=mentions+1 WHERE user_id=?", (uid,))
        self.db.conn.commit()

    def person(self, uid: int, gid: int | None = None) -> dict | None:
        """一个人。gid：用她在这个群里对他的了解（画像按群分开）；不给 gid 是小屋里看的那份（最新的）。"""
        r = self.db.conn.execute("SELECT * FROM qq_people WHERE user_id=?", (uid,)).fetchone()
        if not r:
            return None
        prof_raw, note = r["profile"], r["note"]
        if gid is not None:
            pg = self.db.conn.execute("SELECT profile, note FROM qq_profiles WHERE user_id=? AND group_id=?",
                                      (uid, gid)).fetchone()
            prof_raw, note = (pg["profile"], pg["note"]) if pg else ("", "")
        try:
            prof = json.loads(prof_raw) if prof_raw else {}
        except ValueError:
            prof = {}
        try:
            hours = json.loads(r["hours"]) if r["hours"] else []
        except ValueError:
            hours = []
        if hasattr(self, "closeness_label"):                    # 熟悉度是慢慢涨的分数（chat/grow.py）
            fam = self.closeness_label(int(r["user_id"]))
        else:
            fam = "很熟" if self.owner_id and int(r["user_id"]) == self.owner_id else familiarity(r["msgs"], r["mentions"])
        return {"user_id": r["user_id"], "name": r["name"], "note": note, "msgs": r["msgs"],
                "mentions": r["mentions"], "profile": prof, "familiarity": fam,
                "when": active_time(hours), "last_seen": r["last_seen"],
                "aliases": self.split_aliases(r["aliases"] if "aliases" in r.keys() else "")}

    def groups_of(self, uid: int) -> list[int]:
        return [int(r["group_id"]) for r in self.db.conn.execute(
            "SELECT group_id FROM qq_profiles WHERE user_id=?", (uid,)).fetchall()]

    def _set_profile(self, uid: int, gid: int, prof: dict, source: str = ""):
        if hasattr(self, "save_profile_version"):              # 13.0 改之前存一份旧的（小屋里能看、能退回）
            try:
                self.save_profile_version(uid, gid, source)
            except Exception:  # noqa: BLE001
                pass
        js = json.dumps(prof, ensure_ascii=False)
        note = str(prof.get("印象") or "")[:80]
        now = time.time()
        self.db.conn.execute(
            "INSERT INTO qq_profiles(user_id, group_id, profile, note, since_update, updated) VALUES (?,?,?,?,0,?) "
            "ON CONFLICT(user_id, group_id) DO UPDATE SET profile=excluded.profile, "
            "note=CASE WHEN excluded.note!='' THEN excluded.note ELSE note END, since_update=0, updated=excluded.updated",
            (uid, gid, js, note, now))
        self.db.conn.execute("UPDATE qq_people SET profile=?, note=CASE WHEN ?!='' THEN ? ELSE note END, since_update=0, "
                             "updated=? WHERE user_id=?", (js, note, note, now, uid))

    def _set_note(self, uid: int, gid: int, note: str):
        self.db.conn.execute(
            "INSERT INTO qq_profiles(user_id, group_id, note) VALUES (?,?,?) "
            "ON CONFLICT(user_id, group_id) DO UPDATE SET note=excluded.note", (uid, gid, note))
        self.db.conn.execute("UPDATE qq_people SET note=? WHERE user_id=?", (note, uid))
