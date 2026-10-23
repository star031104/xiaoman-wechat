"""表情包库（2.0）：跟着群友“偷”表情包，懂了它的意思再用。

做法参考 StickerConv（发表情三步：该不该发 → 想表达什么情绪 → 从候选里挑一张；入库存“描述 + 情绪 + 什么时候用”）、
MaiBot 的 steal_emoji（库有上限，满了换掉最久没用的），以及 PunMemeCN 的结论（看图模型看不懂中文梗图，
所以表情包的意思主要从“群友在什么话后面发它、发完大家什么反应”里学，有看图模型时再参考图片本身）。

- 群里同一张表情包被 ≥2 次（默认要 ≥2 个不同的人）发过，就下载存到 data/stickers/，记下它出现时前后的聊天
- 整理群聊时给还没懂的表情包“猜意思”：描述、情绪（开心/大笑/无语/……）、什么时候用
- 聊天时把每种情绪各一张告诉她；她在计划 JSON 里写 "sticker": "情绪" 就从库里挑一张，跟在文字后面发
- 每次最多一张；别人发表情包时她不拿表情包回表情包；小屋“学到的”页能看、能点不用
"""
from __future__ import annotations

from aigroup.core.config import chan_name
import hashlib
import json
import logging
import random
import re
import time
from pathlib import Path

log = logging.getLogger("aigroup.stickers")


def image_ok(data: bytes) -> bool:
    """17.1 是一张完整的图（Pillow 能从头解到尾；动图只看第一帧）。"""
    try:
        import io

        from PIL import Image
        im = Image.open(io.BytesIO(data))
        im.load()
        return im.width > 0 and im.height > 0
    except Exception:  # noqa: BLE001
        return False

SCHEMA = """
CREATE TABLE IF NOT EXISTS qq_stickers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL UNIQUE,
    url TEXT NOT NULL DEFAULT '',
    path TEXT NOT NULL DEFAULT '',          -- 存在 data/stickers/ 里的文件
    groups TEXT NOT NULL DEFAULT '[]',      -- 在哪些群见过
    users TEXT NOT NULL DEFAULT '[]',       -- 谁发过
    seen INTEGER NOT NULL DEFAULT 0,
    contexts TEXT NOT NULL DEFAULT '[]',    -- 发它前后的几句话
    descr TEXT NOT NULL DEFAULT '',
    emotion TEXT NOT NULL DEFAULT '',
    usage TEXT NOT NULL DEFAULT '',
    used INTEGER NOT NULL DEFAULT 0,        -- 她用过几次
    status TEXT NOT NULL DEFAULT 'new',     -- new 自动 / ok 点了用 / no 点了不用
    created REAL, updated REAL, last_used REAL
);
"""
EMOTIONS = ["开心", "大笑", "喜欢", "得意", "惊讶", "无语", "疑惑", "难过", "哭", "生气", "害羞", "困", "加油", "摸鱼", "拒绝", "坏笑"]
MAX_STICKERS = 64


def sticker_key(url: str, file: str = "") -> str:
    f = re.sub(r"\.(gif|png|jpe?g|webp|image)$", "", str(file or ""), flags=re.I)
    if re.fullmatch(r"[0-9a-fA-F]{16,64}", f):
        return f.lower()
    m = re.search(r"[0-9A-Fa-f]{32}", url or "")
    return (m.group(0) if m else hashlib.md5((url or file).encode()).hexdigest()).lower()


class Stickers:
    def __init__(self, db, root: Path, min_seen: int = 2, min_users: int = 2):
        self.db, self.dir = db, Path(root) / "data" / "stickers"
        self.min_seen, self.min_users = min_seen, min_users
        db.conn.executescript(SCHEMA)
        db.conn.commit()

    def seen(self, gid: int, uid: int, key: str, url: str, context: list[str]) -> dict | None:
        """群里有人发了一张表情包。返回这条记录（攒够次数、还没下载时 need_fetch=True）。"""
        if not key:
            return None
        now = time.time()
        r = self.db.conn.execute("SELECT * FROM qq_stickers WHERE key=?", (key,)).fetchone()
        if r is None:
            self.db.conn.execute("INSERT INTO qq_stickers(key, url, groups, users, seen, contexts, created, updated) "
                                 "VALUES (?,?,?,?,1,?,?,?)", (key, url, json.dumps([gid]), json.dumps([uid]),
                                                             json.dumps([context[-4:]], ensure_ascii=False), now, now))
        else:
            groups = sorted(set(json.loads(r["groups"] or "[]")) | {gid})
            users = sorted(set(json.loads(r["users"] or "[]")) | {uid})[-20:]
            ctx = (json.loads(r["contexts"] or "[]") + [context[-4:]])[-5:]
            self.db.conn.execute("UPDATE qq_stickers SET url=?, groups=?, users=?, seen=seen+1, contexts=?, updated=? "
                                 "WHERE key=?", (url or r["url"], json.dumps(groups), json.dumps(users),
                                                 json.dumps(ctx, ensure_ascii=False), now, key))
        self.db.conn.commit()
        r = dict(self.db.conn.execute("SELECT * FROM qq_stickers WHERE key=?", (key,)).fetchone())
        r["need_fetch"] = (not r["path"] and r["seen"] >= self.min_seen
                           and len(json.loads(r["users"])) >= self.min_users and r["status"] != "no")
        return r

    def save_file(self, key: str, data: bytes) -> str:
        if not image_ok(data):                                  # 17.1 下载不完整 / 不是图：不存（下次再见到会重新下载）
            log.info("表情包（%s）下载得不完整，没存", key[:8])
            return ""
        self.dir.mkdir(parents=True, exist_ok=True)
        ext = (".gif" if data[:3] == b"GIF" else ".png" if data[:4] == b"\x89PNG"
               else ".webp" if data[:4] == b"RIFF" and data[8:12] == b"WEBP" else ".jpg")
        p = self.dir / f"{key}{ext}"
        p.write_bytes(data)
        self.db.conn.execute("UPDATE qq_stickers SET path=? WHERE key=?", (p.name, key))
        self.db.conn.commit()
        self._evict()
        return p.name

    def _evict(self):
        # 最久没见到、也没用过的先淘汰；刚存的 24 小时内不淘汰（2.0 把时间戳和次数加在一起排，新存的会立刻被删）
        rows = self.db.conn.execute(
            "SELECT id, path FROM qq_stickers WHERE path!='' AND status!='ok' AND IFNULL(created,0)<? "
            "ORDER BY MAX(IFNULL(last_used,0), IFNULL(updated,0)) ASC", (time.time() - 86400,)).fetchall()
        n = self.db.conn.execute("SELECT COUNT(*) n FROM qq_stickers WHERE path!=''").fetchone()["n"]
        for r in rows[:max(0, n - MAX_STICKERS)]:
            try:
                (self.dir / r["path"]).unlink(missing_ok=True)
            except OSError:
                pass
            self.db.conn.execute("UPDATE qq_stickers SET path='' WHERE id=?", (r["id"],))
        self.db.conn.commit()

    def unlabeled(self, limit: int = 6) -> list[dict]:
        return [dict(r) for r in self.db.conn.execute(
            "SELECT * FROM qq_stickers WHERE path!='' AND emotion='' AND status!='no' ORDER BY seen DESC LIMIT ?",
            (limit,)).fetchall()]

    async def label(self, llm, limit: int = 6) -> int:
        """从“群友在什么话后面发它”猜表情包的意思（不看图也能猜个大概）。"""
        rows = self.unlabeled(limit)
        if not rows:
            return 0
        blocks = []
        for r in rows:
            ctx = json.loads(r["contexts"] or "[]")
            blocks.append(f"## {r['key'][:8]}\n" + "\n".join("  · " + " / ".join(c) for c in ctx[-4:] if c))
        data = await llm.json([{"role": "user", "content":
            f"{chan_name()}里有几张表情包，下面是每张被发出来时前面的几句聊天（看不到图本身）。猜一猜每张大概表达什么，"
            f"情绪只能从这些里选一个：{'、'.join(EMOTIONS)}。\n"
            '只输出 JSON：{"编号": {"descr": "猜它画的什么（15 字内）", "emotion": "情绪", "usage": "什么时候发（15 字内）"}}\n\n'
            + "\n\n".join(blocks)}], default={}, role="memory")
        n = 0
        for r in rows:
            v = (data or {}).get(r["key"][:8]) if isinstance(data, dict) else None
            if not isinstance(v, dict):
                continue
            emo = str(v.get("emotion") or "").strip()
            if emo not in EMOTIONS:
                emo = next((e for e in EMOTIONS if e in emo), "")
            if not emo:
                continue
            self.db.conn.execute("UPDATE qq_stickers SET descr=?, emotion=?, usage=?, updated=? WHERE id=?",
                                 (str(v.get("descr") or "")[:30], emo, str(v.get("usage") or "")[:30], time.time(), r["id"]))
            n += 1
        self.db.conn.commit()
        return n

    def menu(self, gid: int) -> list[dict]:
        """这个群能用的表情包：每种情绪挑一张（在这个群见过的优先）。"""
        rows = [dict(r) for r in self.db.conn.execute(
            "SELECT * FROM qq_stickers WHERE path!='' AND emotion!='' AND status!='no' ORDER BY seen DESC").fetchall()]
        out: dict[str, dict] = {}
        for r in sorted(rows, key=lambda r: -r["seen"]):
            if gid in json.loads(r["groups"] or "[]"):           # 只用这个群见过的：别把 A 群的私梗带到 B 群
                out.setdefault(r["emotion"], r)
        return list(out.values())

    def choose(self, gid: int, emotion: str, rand: random.Random | None = None) -> dict | None:
        emotion = (emotion or "").strip()
        cands = [r for r in self.menu(gid) if r["emotion"] == emotion or (emotion and emotion in r["usage"])]
        if not cands:
            cands = [dict(r) for r in self.db.conn.execute(
                "SELECT * FROM qq_stickers WHERE path!='' AND status!='no' AND emotion=?", (emotion,)).fetchall()
                if gid in json.loads(r["groups"] or "[]")]
        if not cands:
            return None
        r = (rand or random).choice(cands)
        p = self.dir / r["path"]
        if not p.exists():
            return None
        if not image_ok(p.read_bytes()):                        # 17.1 以前存坏的（只下了一半）：不发，清掉等下次重新下载
            log.info("表情包 %s 是坏的，清掉了", r["path"])
            self.db.conn.execute("UPDATE qq_stickers SET path='' WHERE id=?", (r["id"],))
            self.db.conn.commit()
            try:
                p.unlink()
            except OSError:
                pass
            return None
        r["file"] = p
        return r

    def used(self, sid: int):
        self.db.conn.execute("UPDATE qq_stickers SET used=used+1, last_used=? WHERE id=?", (time.time(), sid))
        self.db.conn.commit()

    def prompt_block(self, gid: int) -> str:
        m = self.menu(gid)
        if not m:
            return ""
        return ("# 你存的表情包（想发就在 JSON 里写 \"sticker\": \"情绪\"，一次最多一张，跟在文字后面；"
                "别人发表情包时别拿表情包回表情包；大多数时候不用发）\n"
                + "、".join(f"{r['emotion']}（{r['usage'] or r['descr']}）" for r in m))
