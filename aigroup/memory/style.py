"""13.0 说话指纹、她自己的风格审计、画像的历史版本——都是每晚统计，不调大模型（不花钱）。

- 说话指纹：每个群友最近 30 天在这个群的打字习惯（平均几个字、爱不爱发表情包 / 语音、问句多不多、常在几点出现、
  语音里常带的情绪），写成一句话放进“群里的人”（“打字习惯：平均 7 个字，爱发表情包，常在深夜冒出来”）。
  比大模型写的“说话风格”更准，也能看出一个人最近变了没有。
- 风格审计：她昨天在每个群说的话和群友比——句子是不是长了、口头禅是不是用太多、助手腔有没有冒出来、
  是不是老把问题抛回去；有问题就写一两句“今天注意”放进提示词（参考 Persistent Personas：长对话里人设会慢慢走样）。
- 画像版本：每次改画像前把旧的存一份（qq_profile_versions，每人每群留 20 份），小屋里能看改了什么、退回去。
"""
from __future__ import annotations

import json
import re
import time
from collections import Counter
from datetime import datetime

SCHEMA = """
CREATE TABLE IF NOT EXISTS qq_profile_versions (   -- 13.0 画像的历史版本（改之前的那份）
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    group_id INTEGER NOT NULL,
    profile TEXT NOT NULL,
    ts REAL NOT NULL,
    source TEXT NOT NULL DEFAULT ''      -- digest / deep / web / seed
);
CREATE INDEX IF NOT EXISTS idx_qq_profile_versions ON qq_profile_versions(user_id, group_id, id);
"""

BUCKETS = (("早上", range(6, 11)), ("中午", range(11, 14)), ("下午", range(14, 18)), ("晚上", range(18, 23)),
           ("深夜", (23, 0, 1, 2, 3, 4, 5)))
ASSISTANT_TONE = re.compile(r"希望(对你|能)?有(所)?帮助|作为(一个)?(AI|人工智能)|总的来说|综上|首先.*其次|有什么(我)?可以帮|"
                            r"请注意|建议你|您")
PUSH_BACK = re.compile(r"(你呢|你觉得呢|你们呢|你说呢)[？?]?$")


def fingerprint(rows: list[tuple[float, str]], tz=None) -> dict:
    """[(ts, 内容)] → 打字习惯的统计。少于 20 句返回 {}。"""
    rows = [(ts, str(c or "")) for ts, c in rows if c]
    if len(rows) < 20:
        return {}
    texts = [c for _, c in rows]
    plain = [re.sub(r"\[[^\]]*\]|@\S+", "", c).strip() for c in texts]
    words = [p for p in plain if p]
    n = len(texts)
    hours = Counter(datetime.fromtimestamp(ts, tz).hour for ts, _ in rows)
    top = max(BUCKETS, key=lambda b: sum(hours[h] for h in b[1]))
    top_share = sum(hours[h] for h in top[1]) / n
    voice = [c for c in texts if c.startswith("[语音")]
    vt = Counter(t for c in voice for t in re.findall(r"(带笑|在哭|听起来[^，：\]]+)", c))
    return {
        "n": n,
        "avg_len": round(sum(len(p) for p in words) / max(1, len(words)), 1),
        "sticker": round(sum(1 for c in texts if c.startswith("[表情包") or c.startswith("[动画表情")) / n, 2),
        "image": round(sum(1 for c in texts if c.startswith("[图片")) / n, 2),
        "voice": round(len(voice) / n, 2),
        "question": round(sum(1 for p in words if re.search(r"[?？]|吗$|呢$|啥|什么|怎么|咋", p)) / max(1, len(words)), 2),
        "laugh": round(sum(1 for p in words if re.search(r"哈哈|hhh|233|笑死|xswl", p, re.I)) / max(1, len(words)), 2),
        "period": top[0] if top_share >= 0.4 else "",
        "voice_mood": vt.most_common(1)[0][0] if vt else "",
    }


def fingerprint_line(fp: dict) -> str:
    if not fp:
        return ""
    bits = [f"平均 {fp['avg_len']:.0f} 个字" + ("（很短）" if fp["avg_len"] <= 6 else "（爱打长句）" if fp["avg_len"] >= 25 else "")]
    if fp.get("sticker", 0) >= 0.15:
        bits.append("爱发表情包")
    if fp.get("voice", 0) >= 0.1:
        bits.append("常发语音" + (f"（常{fp['voice_mood']}）" if fp.get("voice_mood") else ""))
    if fp.get("image", 0) >= 0.12:
        bits.append("爱发图")
    if fp.get("question", 0) >= 0.3:
        bits.append("问题多")
    if fp.get("laugh", 0) >= 0.25:
        bits.append("动不动就哈哈哈")
    if fp.get("period"):
        bits.append(f"常在{fp['period']}说话")
    return "，".join(bits)


def audit(her: list[str], others: list[str], catchphrases: list[str]) -> list[str]:
    """她昨天说的话 vs 群友说的话 → 要注意的地方（最多 3 条）。说得太少（<8 句）不评。"""
    her = [re.sub(r"\[[^\]]*\]|@\S+", "", x).strip() for x in her]
    her = [x for x in her if x]
    if len(her) < 8:
        return []
    others = [re.sub(r"\[[^\]]*\]|@\S+", "", x).strip() for x in others]
    others = [x for x in others if x]
    out = []
    h_avg = sum(len(x) for x in her) / len(her)
    o_avg = (sum(len(x) for x in others) / len(others)) if others else 0
    if o_avg and h_avg > max(18.0, o_avg * 2.2):
        out.append(f"你昨天平均一句 {h_avg:.0f} 个字，群友才 {o_avg:.0f} 个：说短一点，一句话的事别写一段")
    for cp in catchphrases[:6]:
        k = sum(1 for x in her if cp and cp in x)
        if cp and k >= max(3, len(her) * 0.2):
            out.append(f"“{cp}”昨天说了 {k} 次，今天少用")
            break
    tone = [x for x in her if ASSISTANT_TONE.search(x)]
    if tone:
        out.append(f"昨天有 {len(tone)} 句像客服（比如“{tone[0][:16]}”），今天别这样说话")
    back = sum(1 for x in her if PUSH_BACK.search(x))
    if back >= max(3, len(her) * 0.25):
        out.append("昨天老把问题抛回给别人（“你呢？”），今天直接说自己的")
    return out[:3]


class StyleMixin:
    def update_fingerprints(self, days: int = 30) -> int:
        since = time.time() - days * 86400
        n = 0
        for gid in sorted(self.groups):
            for r in self.db.conn.execute("SELECT user_id FROM qq_group_messages WHERE group_id=? AND role='user' AND ts>? "
                                          "GROUP BY user_id HAVING COUNT(*)>=20", (gid, since)).fetchall():
                uid = int(r["user_id"])
                rows = [(x["ts"], x["content"]) for x in self.db.conn.execute(
                    "SELECT ts, content FROM qq_group_messages WHERE group_id=? AND user_id=? AND role='user' AND ts>? "
                    "ORDER BY id DESC LIMIT 600", (gid, uid, since))]
                fp = fingerprint(rows, self.cfg.tz)
                if fp:
                    fp["line"] = fingerprint_line(fp)
                    self.db.set(f"qq_fp_{gid}_{uid}", fp)
                    n += 1
        return n

    def fingerprint_of(self, uid: int, gid: int) -> str:
        fp = self.db.get(f"qq_fp_{gid}_{uid}") or {}
        return str(fp.get("line") or "") if isinstance(fp, dict) else ""

    def style_audit(self, day: datetime) -> int:
        a = day.replace(hour=0, minute=0, second=0, microsecond=0)
        t0, t1 = a.timestamp(), a.timestamp() + 86400
        p = self.brain.persona
        n = 0
        for gid in sorted(self.groups):
            rows = self.db.conn.execute("SELECT role, content FROM qq_group_messages WHERE group_id=? AND ts>=? AND ts<?",
                                        (gid, t0, t1)).fetchall()
            her = [r["content"] for r in rows if r["role"] == "assistant"]
            others = [r["content"] for r in rows if r["role"] == "user"]
            notes = audit(her, others, list(p.catchphrases or []))
            self.db.set(f"qq_style_note_{gid}", {"day": f"{day:%Y-%m-%d}", "notes": notes} if notes else None)
            n += bool(notes)
        return n

    def style_block(self, gid: int) -> str:
        d = self.db.get(f"qq_style_note_{gid}") or {}
        notes = d.get("notes") if isinstance(d, dict) else None
        if not notes:
            return ""
        return "# 你昨天回看自己说的话，发现的小毛病（今天注意）\n" + "\n".join(f"- {x}" for x in notes)

    # ---------- 画像版本 ----------
    def save_profile_version(self, uid: int, gid: int, source: str = "", keep: int = 20):
        r = self.db.conn.execute("SELECT profile FROM qq_profiles WHERE user_id=? AND group_id=?", (uid, gid)).fetchone()
        if not r or not r["profile"]:
            return
        last = self.db.conn.execute("SELECT profile FROM qq_profile_versions WHERE user_id=? AND group_id=? "
                                    "ORDER BY id DESC LIMIT 1", (uid, gid)).fetchone()
        if last and last["profile"] == r["profile"]:
            return
        self.db.conn.execute("INSERT INTO qq_profile_versions(user_id, group_id, profile, ts, source) VALUES (?,?,?,?,?)",
                             (uid, gid, r["profile"], time.time(), source))
        self.db.conn.execute("DELETE FROM qq_profile_versions WHERE user_id=? AND group_id=? AND id NOT IN "
                             "(SELECT id FROM qq_profile_versions WHERE user_id=? AND group_id=? ORDER BY id DESC LIMIT ?)",
                             (uid, gid, uid, gid, keep))

    def profile_versions(self, uid: int, gid: int, limit: int = 20) -> list[dict]:
        out = []
        for r in self.db.conn.execute("SELECT id, profile, ts, source FROM qq_profile_versions WHERE user_id=? AND group_id=? "
                                      "ORDER BY id DESC LIMIT ?", (uid, gid, limit)):
            try:
                prof = json.loads(r["profile"])
            except ValueError:
                prof = {}
            out.append({"id": r["id"], "ts": r["ts"], "source": r["source"], "profile": prof})
        return out


def profile_diff(old: dict, new: dict) -> list[str]:
    """两份画像差在哪（给小屋看）：['近况：A → B', '+看法：…', '-别碰']"""
    out = []
    for k in dict.fromkeys([*old, *new]):
        a, b = old.get(k), new.get(k)
        if a == b:
            continue
        if a in (None, "", []):
            out.append(f"+{k}：{b}")
        elif b in (None, "", []):
            out.append(f"-{k}")
        else:
            out.append(f"{k}：{a} → {b}")
    return out
