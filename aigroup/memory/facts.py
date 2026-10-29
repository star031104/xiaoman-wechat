"""群聊记忆（2.0）：她能想起很久以前群里说过的话和事。

依据 GroupMemBench（2026）：群聊里纯 BM25 检索胜过大多数“智能记忆”系统——入库前做重度抽取会丢掉“谁说的”。所以：
- 原始消息永远保留、可以直接检索（只在本群里查，不跨群）
- 另有一张事实表 qq_facts：整理印象时顺便记下“谁 / 关于谁 / 什么事”，每条带说话人、群、原消息 id、
  生效时间 t_valid 和失效时间 t_invalid（Graphiti 的双时间轴：新事实和旧的矛盾时只给旧的标失效，不删）
- 检索：消息和事实各自 BM25 打分 → RRF（k=60）融合 → 按“强度”微调
  （24.0：从上次被群友提起算淡忘，半衰期 30~90 天看重要度；被她想起不加强；要过证据门槛；带“3 天前”）
- 分词：有 jieba 就用 jieba，没有就用字的二元组（中文够用，不用装东西）

recall() 返回给提示词用的几行；聊天记录窗口里已经有的消息不重复给。
"""
from __future__ import annotations

import math
import re
import time
from collections import Counter
from datetime import datetime

SCHEMA = """
CREATE TABLE IF NOT EXISTS qq_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id INTEGER NOT NULL,
    speaker_id INTEGER,                 -- 谁说的（从谁的话里看出来的）
    subject_id INTEGER,                 -- 关于谁（0 = 关于群 / 大家，-1 = 关于她自己）
    text TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'fact',  -- fact 事实 / event 发生的事 / plan 约好的事 / opinion 她的看法
    msg_id INTEGER,                     -- 原消息（qq_group_messages.id）
    t_valid REAL NOT NULL,
    t_invalid REAL,                     -- 被新说法替代的时间（NULL = 现在还成立）
    hits INTEGER NOT NULL DEFAULT 0,
    last_hit REAL,
    created REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_qq_facts ON qq_facts(group_id, t_invalid);
CREATE TABLE IF NOT EXISTS qq_embed (       -- 3.0（可选）：消息的向量，给语义召回用
    msg_id INTEGER PRIMARY KEY,
    vec BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS qq_forgotten (   -- 24.0 删掉的记忆留个“指纹”：以后整理时不再学回来（dsh-soul 的做法）
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id INTEGER NOT NULL,
    kind TEXT NOT NULL,                     -- fact 记的事 / lore 群梗
    text TEXT NOT NULL DEFAULT '',
    keys TEXT NOT NULL DEFAULT '[]',
    ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_qq_forgotten ON qq_forgotten(group_id, kind);
"""
K_RRF = 60
HALF_LIFE_DAYS = 30.0
# 24.0 召回门槛（dsh-fish-memory）：BM25 分 s 换成“证据分”C = s/(s+1)；低于 MIN_CONF 的不给，没有一条够强的就只给 1 条
MIN_CONF = 0.6
STRONG_CONF = 0.8
MSG_MIN_CONF = 0.5                 # 老消息的门槛低一点（一句话本来就短）
RECALL_COOLDOWN = 30 * 60          # 同一条事实刚给过她：半小时内不再给（除非特别相关），免得她反复提同一件事
STOP = set("的了是我你他她它们在有就不也都和吗呢吧啊哈嗯哦呀啦么这那个一")

try:  # 可选：有 jieba 用 jieba
    import jieba  # type: ignore
    jieba.setLogLevel(60)
except Exception:  # noqa: BLE001
    jieba = None


# ---------------- 31.0 记之前核对原话（思路来自 memory_companion：总结带逐字引文 + 规则复核） ----------------
HEARSAY_RX = re.compile(r"(他|她|ta|TA|[\u4e00-\u9fff]{1,3})(说|讲)(过|了)?(他|她|自己)|听说|据说|听.{0,4}说|好像是|传言")
INTENT_RX = re.compile(r"打算|准备|计划|想要|想去|考虑|可能会|也许会|说不定|估计会|下次要|过几天要")
TEMP_RX = re.compile(r"今天|今晚|这会儿?|刚才|刚刚|现在有点|暂时|这两天|这几天|一会儿?|待会")
JOKE_RX = re.compile(r"doge|狗头|开玩笑|开个玩笑|逗你|假如|如果我是|要是我是|🐶|\[(奸笑|旺柴|狗头)\]")
NEG_RX = re.compile(r"不|没|别|无|非|未")

# 一个人同一时间只有一个值的属性：改口时旧的那条失效（多值的事——喜好、习惯、约定——可以并存）
SINGLE_ATTRS = {
    "birthday": re.compile(r"生日"),
    "school": re.compile(r"(在|读|上)[\u4e00-\u9fff]{0,8}(大学|学院|研究生|高中|中学)|在读|读研|读博|考上"),
    "major": re.compile(r"专业|学的是"),
    "job": re.compile(r"(在|去)[\u4e00-\u9fff A-Za-z]{1,12}(实习|上班|工作|入职)|offer|签了|转正|离职|辞职|换工作"),
    "city": re.compile(r"(住在|在)[\u4e00-\u9fff]{2,4}(市|这边)?(生活|定居|住)|搬到|搬去"),
    "grade": re.compile(r"大[一二三四五]|研[一二三]|博[一二三四五]|高[一二三]"),
    "partner": re.compile(r"男朋友|女朋友|对象|分手|在一起了|单身|脱单"),
}


def _plain(t: str) -> str:
    return re.sub(r"[\s，,。.！!？?…~～、：:；;“”\"'‘’（）()【】\[\]]", "", str(t or ""))


def single_attr(text: str) -> str:
    """这条事说的是哪种单值属性（生日 / 学校 / 工作……）；不是就返回空。"""
    for k, rx in SINGLE_ATTRS.items():
        if rx.search(text or ""):
            return k
    return ""


def find_quote(quote: str, rows: list[dict]) -> dict | None:
    """引文（去掉标点空格后至少 4 个字）逐字出现在哪条群友消息里；找不到返回 None。"""
    q = _plain(quote)
    if len(q) < 4:
        return None
    for r in reversed(rows):
        if r.get("role") == "user" and q in _plain(r.get("content") or ""):
            return r
    return None


def vet_fact(text: str, quote: str, subject: int, src: str, rows: list[dict]) -> dict:
    """31.0 一条要记的事和它的引文对一下：
    - 引文对不上任何一条原话 → 只能算“猜”，不挂门牌
    - 引文出自别人的话 → 来源是“别人”（不是本人说自己的）
    - 原话是转述（他说 / 听说）→ “别人”；是开玩笑、假设 → “猜”
    - 原话是打算 / 准备 → 两周后过期；是“今天 / 这两天 / 暂时”这种临时状态 → 三天后过期（调用方没给有效期时）
    - 要记的事和原话一个有否定一个没有（“不喜欢香菜”对“喜欢香菜”）→ “猜”
    判断都看原话本身，不看模型改写后的说法。返回 {"row", "src", "until_days", "why"}。"""
    out = {"row": None, "src": src, "until_days": 0, "why": ""}
    row = find_quote(quote, rows) if quote else None
    if row is None:
        if src in ("self", "other", ""):
            out.update(src="guess", why="引文对不上原话")
        return out
    out["row"] = row
    said = str(row.get("content") or "")
    author = int(row.get("user_id") or 0)
    if subject > 0 and author and author != subject and src in ("self", ""):
        out.update(src="other", why="原话是别人说的")
    if HEARSAY_RX.search(said) and out["src"] in ("self", ""):
        out.update(src="other", why="原话是转述")
    if JOKE_RX.search(said):
        out.update(src="guess", why="原话像在开玩笑 / 假设")
    if bool(NEG_RX.search(_plain(quote))) != bool(NEG_RX.search(_plain(text))):
        out.update(src="guess", why="否定对不上")
    if INTENT_RX.search(said):
        out["until_days"] = 14
    elif TEMP_RX.search(said):
        out["until_days"] = 3
    return out


def tokens(text: str) -> list[str]:
    t = re.sub(r"\[[^\]]{0,40}\]|@\S+|https?://\S+", " ", str(text or "").lower())
    out: list[str] = []
    for w in re.findall(r"[a-z0-9]+", t):
        if len(w) >= 2:
            out.append(w)
    for seg in re.findall(r"[一-鿿]+", t):
        if jieba is not None:
            out += [w for w in jieba.lcut(seg) if w not in STOP and len(w) >= 1 and not (len(w) == 1 and w in STOP)]
        else:
            chars = [c for c in seg]
            out += [chars[i] + chars[i + 1] for i in range(len(chars) - 1)]
            out += [c for c in chars if c not in STOP]
    return out


class BM25:
    def __init__(self, docs: list[list[str]], k1: float = 1.5, b: float = 0.75):
        self.docs, self.k1, self.b = docs, k1, b
        self.n = len(docs)
        self.avg = (sum(len(d) for d in docs) / self.n) if self.n else 0.0
        df: Counter = Counter()
        for d in docs:
            df.update(set(d))
        n_eff = max(self.n, 50)        # 24.0 库很小时 idf 都很低，证据门槛会把什么都挡掉：按至少 50 条算
        self.idf = {w: math.log(1 + (n_eff - c + 0.5) / (c + 0.5)) for w, c in df.items()}
        self.tf = [Counter(d) for d in docs]

    def scores(self, q: list[str], weights: dict | None = None) -> list[float]:
        """weights（24.0）：查询词的权重（说话的人自己的口头禅权重低，见 own_weights）；没给就都是 1。"""
        out = []
        qs = [w for w in set(q) if w in self.idf]
        wt = {w: float((weights or {}).get(w, 1.0)) for w in qs}
        for d, tf in zip(self.docs, self.tf):
            s, L = 0.0, len(d) or 1
            for w in qs:
                f = tf.get(w, 0)
                if f:
                    s += wt[w] * self.idf[w] * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * L / (self.avg or 1)))
            out.append(s)
        return out


def own_weights(query_tokens: list[str], own_texts: list[str]) -> dict[str, float]:
    """24.0（dsh-fish-memory）：词在“说话的人自己的话”里越常见，越像口头禅，召回时权重越低。
    own_texts：这个人最近说的话。出现在他 30% 以上的话里的词权重压到 0.25 左右。"""
    if not own_texts or not query_tokens:
        return {}
    docs = [set(tokens(t)) for t in own_texts if t]
    n = len(docs)
    if n < 10:
        return {}
    out = {}
    for w in set(query_tokens):
        df = sum(1 for d in docs if w in d) / n
        if df > 0.05:
            out[w] = max(0.15, 1.0 - 2.5 * df)
    return out


def conf(s: float) -> float:
    return s / (s + 1.0) if s > 0 else 0.0


def rel_day(ts: float, now: float, tz=None) -> str:
    """24.0 记的事带相对时间：“3 天前 10-23”“今天”“昨天 10-25”。"""
    a = datetime.fromtimestamp(float(ts), tz)
    b = datetime.fromtimestamp(float(now), tz)
    d = (b.date() - a.date()).days
    md = a.strftime("%m-%d")
    if d <= 0:
        return "今天"
    if d == 1:
        return f"昨天 {md}"
    if d < 60:
        return f"{d} 天前 {md}"
    return f"{d // 30} 个月前 {md}"


def _fp_text(text: str) -> str:
    return re.sub(r"[\s\W_]+", "", str(text or "").lower())


def forgotten_match(db, gid: int, kind: str, text: str = "", keys: list[str] | None = None) -> bool:
    """24.0 这条以前被删过吗（文本很像，或者梗的关键词有重合）。"""
    try:
        rows = db.conn.execute("SELECT text, keys FROM qq_forgotten WHERE group_id=? AND kind=?", (gid, kind)).fetchall()
    except Exception:  # noqa: BLE001
        return False
    if not rows:
        return False
    from difflib import SequenceMatcher
    t = _fp_text(text)
    ks = {str(k).strip().lower() for k in (keys or []) if str(k).strip()}
    import json as _json
    for r in rows:
        if t and r["text"] and SequenceMatcher(None, t, _fp_text(r["text"])).ratio() >= 0.75:
            return True
        if ks:
            try:
                old = {str(k).strip().lower() for k in _json.loads(r["keys"] or "[]")}
            except ValueError:
                old = set()
            if ks & old:
                return True
    return False


def remember_forgotten(db, gid: int, kind: str, text: str = "", keys: list[str] | None = None):
    import json as _json
    db.conn.execute("INSERT INTO qq_forgotten(group_id, kind, text, keys, ts) VALUES (?,?,?,?,?)",
                    (gid, kind, str(text or "")[:200], _json.dumps(list(keys or []), ensure_ascii=False), time.time()))


def rrf(*ranked: list) -> dict:
    """几个排好序的列表（元素是 key）→ {key: 融合分}。"""
    out: dict = {}
    for lst in ranked:
        for r, k in enumerate(lst):
            out[k] = out.get(k, 0.0) + 1.0 / (K_RRF + r + 1)
    return out


class Embedder:
    """可选的语义召回（3.0）：配置里写了 memory_embed_model（比如 Qwen/Qwen3-Embedding-0.6B、BAAI/bge-small-zh-v1.5）
    并且装了 sentence-transformers 才启用。向量存在 qq_embed 里，新消息在后台慢慢补（每次最多 256 条）。"""

    def __init__(self, name: str):
        from sentence_transformers import SentenceTransformer  # type: ignore
        self.model = SentenceTransformer(name)

    def encode(self, texts: list[str]):
        import numpy as np  # type: ignore
        v = self.model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
        return np.asarray(v, dtype="float32")


class APIEmbedder:
    """13.0：用接口算向量（OpenAI 兼容的 /embeddings，比如硅基流动免费的 BAAI/bge-m3），不用在本机装模型。
    config.llm.yaml 里 routes.embed 写一个接口就启用。召回时查询句的向量是同步算的：超时很短，失败就只用关键词。"""

    def __init__(self, profile: dict, timeout: float = 2.5):
        self.base = str(profile.get("base_url") or "").rstrip("/")
        self.key = str(profile.get("api_key") or "")
        self.model = str(profile.get("model") or "BAAI/bge-m3")
        self.timeout = float(profile.get("timeout", timeout) or timeout)
        self.proxy = profile.get("_proxy")
        self._cache: dict[str, object] = {}
        self._down_until = 0.0

    def encode(self, texts: list[str]):
        import httpx
        import numpy as np  # type: ignore
        texts = [str(t or " ")[:500] or " " for t in texts]
        if len(texts) == 1 and texts[0] in self._cache:
            return np.asarray([self._cache[texts[0]]], dtype="float32")
        if len(texts) == 1 and time.time() < self._down_until:      # 接口刚出过错：聊天时先别等它
            raise RuntimeError("向量接口暂时不可用")
        try:
            return self._encode(texts, httpx, np)
        except Exception:
            if len(texts) == 1:
                self._down_until = time.time() + 300
            raise

    def _encode(self, texts, httpx, np):
        out = []
        with httpx.Client(timeout=self.timeout if len(texts) == 1 else 60, proxy=self.proxy) as cli:
            for i in range(0, len(texts), 32):
                r = cli.post(f"{self.base}/embeddings", headers={"Authorization": f"Bearer {self.key}"},
                             json={"model": self.model, "input": texts[i:i + 32], "encoding_format": "float"})
                r.raise_for_status()
                out += [d["embedding"] for d in sorted(r.json()["data"], key=lambda d: d.get("index", 0))]
        v = np.asarray(out, dtype="float32")
        v /= np.maximum(np.linalg.norm(v, axis=1, keepdims=True), 1e-6)
        if len(texts) == 1:
            if len(self._cache) > 256:
                self._cache.clear()
            self._cache[texts[0]] = v[0]
        return v


def embed_profile(cfg) -> dict | None:
    """config 里 llm.routes.embed 的第一个接口（带上 Key）。没配返回 None。"""
    llm = cfg.get("llm") or {}
    names = list((llm.get("routes") or {}).get("embed") or [])
    profiles = llm.get("profiles") or {}
    for n in names:
        p = dict(profiles.get(n) or {})
        if not p:
            continue
        p["api_key"] = p.get("api_key") or (cfg.get("keys") or {}).get(p.get("key") or "", "")
        if p.get("use_proxy") and cfg.get("proxy"):
            p["_proxy"] = cfg.get("proxy")
        if p.get("api_key") and p.get("base_url"):
            return p
    return None


def load_embedder(name: str, cfg=None):
    prof = embed_profile(cfg) if cfg is not None else None
    if prof:
        return APIEmbedder(prof)
    if not name:
        return None
    try:
        return Embedder(name)
    except Exception as e:  # noqa: BLE001
        import logging
        logging.getLogger("aigroup.memory").info("语义召回没开起来（%s）：%s", name, e)
        return None


class Memory:
    def __init__(self, db, tz=None, embedder=None):
        self.db, self.tz = db, tz
        self.embedder = embedder
        db.conn.executescript(SCHEMA)
        cols = {r[1] for r in db.conn.execute("PRAGMA table_info(qq_facts)")}
        if "source" not in cols:                 # 15.0 这件事是怎么知道的：self 他自己说的 / other 别人说他的 / guess 你猜的
            db.conn.execute("ALTER TABLE qq_facts ADD COLUMN source TEXT NOT NULL DEFAULT ''")
        if "evidence" not in cols:               # 16.0 这件事有几次独立的证据；“你猜的”要 ≥2 次才拿来用
            db.conn.execute("ALTER TABLE qq_facts ADD COLUMN evidence INTEGER NOT NULL DEFAULT 1")
        if "heard_by" not in cols:               # 20.2 这件事被说出来的时候谁在（那条消息前 30 分钟到后 2 小时说过话的人）
            db.conn.execute("ALTER TABLE qq_facts ADD COLUMN heard_by TEXT NOT NULL DEFAULT ''")
        if "last_said" not in cols:              # 24.0 群友上一次又提起这件事的时间（只有再被提起才加强，被她想起不算）
            db.conn.execute("ALTER TABLE qq_facts ADD COLUMN last_said REAL")
        db.conn.commit()
        self.last_new = False
        self._idx: dict[int, tuple] = {}         # gid → (建索引时的最大消息 id, 行, BM25)；每 200 条新消息重建一次

    # ---------------- 写 ----------------
    def add_fact(self, gid: int, text: str, *, speaker: int | None = None, subject: int | None = None,
                 kind: str = "fact", msg_id: int | None = None, replaces: list[int] | None = None,
                 now: float | None = None, source: str = "", until: float | None = None) -> int | None:
        """until（15.0）：这件事到什么时候就不算数了（“这周在赶论文”→ 周日），到点自动当成过期；source 见建表处。
        24.0：msg_id 是这件事出自哪条消息（“门牌”）。同一件事又记一遍时，只有出自一条更新的消息才算“又被提起了”
        （证据 +1、重新算淡忘）；整理时重叠的那段聊天被重复抽出来不算。删过的事（qq_forgotten）不再记。"""
        text = re.sub(r"\s+", " ", str(text or "")).strip()[:120]
        if len(text) < 4:
            return None
        now = now or time.time()
        self.last_new = False                  # 24.0 这次是不是真记了一条新的（同一件事再记一遍不算）：日志里的条数用它
        if forgotten_match(self.db, gid, "fact", text):
            return None
        dup = self.db.conn.execute(
            "SELECT id, text, source, msg_id FROM qq_facts WHERE group_id=? AND (t_invalid IS NULL OR t_invalid>?) "
            "AND IFNULL(subject_id,0)=IFNULL(?,0)", (gid, now, subject)).fetchall()
        from difflib import SequenceMatcher
        for r in dup:
            if SequenceMatcher(None, r["text"], text).ratio() >= 0.85:      # 同一件事又说了一遍
                again = bool(msg_id) and (not r["msg_id"] or int(msg_id) > int(r["msg_id"]))
                if again:
                    self.db.conn.execute("UPDATE qq_facts SET evidence=evidence+1, last_said=?, msg_id=? WHERE id=?",
                                         (now, int(msg_id), r["id"]))
                elif msg_id and not r["msg_id"]:
                    self.db.conn.execute("UPDATE qq_facts SET msg_id=? WHERE id=?", (int(msg_id), r["id"]))
                if r["source"] == "guess" and source in ("self", "other"):   # 猜的被证实了：升级来源
                    self.db.conn.execute("UPDATE qq_facts SET source=? WHERE id=?", (source, r["id"]))
                self.db.conn.commit()
                return int(r["id"])
        attr = single_attr(text) if subject is not None and kind == "fact" and source != "guess" else ""
        if attr:                                         # 31.0 单值属性改口了（换了实习、换了学校）：同一个人旧的那条自动失效
            replaces = list(replaces or [])
            for r in dup:
                if single_attr(r["text"]) == attr and r["source"] != "guess" and int(r["id"]) not in replaces:
                    replaces.append(int(r["id"]))
        for rid in replaces or []:                       # 新说法替代旧的：旧的标失效（不删，以后还能查“以前”）
            self.db.conn.execute("UPDATE qq_facts SET t_invalid=? WHERE id=? AND group_id=? AND (t_invalid IS NULL OR t_invalid>?)",
                                 (now, int(rid), gid, now))
        cur = self.db.conn.execute(
            "INSERT INTO qq_facts(group_id, speaker_id, subject_id, text, kind, msg_id, t_valid, t_invalid, created, source, "
            "heard_by, last_said) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (gid, speaker, subject, text, kind if kind in ("fact", "event", "plan", "opinion", "experience") else "fact", msg_id, now,
             until if until and until > now else None, now, source if source in ("self", "other", "guess", "said") else "",
             ",".join(str(u) for u in self.present(gid, msg_id)), now))
        self.db.conn.commit()
        self.last_new = True
        return int(cur.lastrowid)

    def forget_fact(self, fid: int) -> bool:
        """24.0 删一条记的事，并留下指纹：以后整理群聊时不会再把它学回来。"""
        r = self.db.conn.execute("SELECT group_id, text FROM qq_facts WHERE id=?", (int(fid),)).fetchone()
        if not r:
            return False
        remember_forgotten(self.db, int(r["group_id"]), "fact", r["text"])
        self.db.conn.execute("DELETE FROM qq_facts WHERE id=?", (int(fid),))
        self.db.conn.commit()
        return True

    def locate(self, gid: int, text: str, rows: list[dict]) -> tuple[int | None, int | None, float]:
        """24.0 给一条记的事找“门牌”：在整理时看的那段聊天里，最像它的那条群友消息 → (消息 id, 谁说的, 证据分)。"""
        cand = [r for r in rows if r.get("role") == "user" and len(str(r.get("content") or "")) >= 2]
        if not cand:
            return None, None, 0.0
        bm = BM25([tokens(r["content"]) for r in cand])
        sc = bm.scores(tokens(text))
        i = max(range(len(cand)), key=lambda k: sc[k])
        if sc[i] <= 0:
            return None, None, 0.0
        return int(cand[i]["id"]), int(cand[i].get("user_id") or 0) or None, conf(sc[i])

    # ---------------- 20.2 谁在场 ----------------
    @staticmethod
    def presence_note(f: dict, names: dict | None, focus: list | None) -> str:
        """20.2 在聊的人当时不在（不是说这件事的人、也不在 heard_by 里）：提醒她别默认他知道。"""
        hb = str(f.get("heard_by") or "")
        if not hb or not focus:
            return ""
        heard = {int(x) for x in hb.split(",") if x.strip().lstrip("-").isdigit()}
        miss = []
        for u in focus:
            try:
                u = int(u)
            except (TypeError, ValueError):
                continue
            if u in heard or u == f.get("speaker_id") or u == f.get("subject_id"):
                continue
            nm = (names or {}).get(str(u))
            if nm:
                miss.append(nm)
        if not miss:
            return ""
        return f"（{'、'.join(miss[:2])}当时不在，可能不知道：提的时候说“上次谁说……”，别当他知道）"

    def present(self, gid: int, msg_id: int | None, before: float = 1800, after: float = 7200) -> list[int]:
        """那条消息前 30 分钟到后 2 小时在群里说过话的人（微信、QQ 都没有“已读”，用说话近似；一句没说的当“可能没看到”）。"""
        if not msg_id:
            return []
        r = self.db.conn.execute("SELECT ts FROM qq_group_messages WHERE id=?", (msg_id,)).fetchone()
        if not r:
            return []
        t = float(r["ts"])
        rows = self.db.conn.execute("SELECT DISTINCT user_id FROM qq_group_messages WHERE group_id=? AND role='user' "
                                    "AND ts BETWEEN ? AND ?", (gid, t - before, t + after)).fetchall()
        return sorted(int(x["user_id"]) for x in rows if x["user_id"])

    def refresh_presence(self, days: float = 3) -> int:
        """每晚：最近几天的事实重新算一次在场的人（记下来的时候“之后 2 小时”还没到）。"""
        n = 0
        for r in self.db.conn.execute("SELECT id, group_id, msg_id FROM qq_facts WHERE created>? AND msg_id IS NOT NULL",
                                      (time.time() - days * 86400,)).fetchall():
            hb = ",".join(str(u) for u in self.present(r["group_id"], r["msg_id"]))
            self.db.conn.execute("UPDATE qq_facts SET heard_by=? WHERE id=?", (hb, r["id"]))
            n += 1
        self.db.conn.commit()
        return n

    def facts(self, gid: int, subject: int | None = None, limit: int = 40, valid_only: bool = True) -> list[dict]:
        sql = "SELECT * FROM qq_facts WHERE group_id=?"
        args: list = [gid]
        if valid_only:
            sql += " AND (t_invalid IS NULL OR t_invalid>?)"
            args.append(time.time())
        if subject is not None:
            sql += " AND subject_id=?"
            args.append(subject)
        return [dict(r) for r in self.db.conn.execute(sql + " ORDER BY id DESC LIMIT ?", (*args, limit)).fetchall()]

    # ---------------- 想起来 ----------------
    def strength(self, created: float, hits: int = 0, now: float | None = None, *, evidence: int = 1,
                 last_said: float | None = None) -> float:
        """24.0 记忆的强度（dsh-fish-memory）：从“上一次被群友提起”算起淡忘；半衰期随重要度变长
        （H = 30 × (1 + 2 × 重要度) 天，重要度看被独立提起了几次）；被提起过 2 次以上的有个地板，不会完全忘掉。
        被她想起来（召回）不加强——以前召回一次 +0.05，结果过时的老事越想越强、霸占名额。hits 只留着做统计。"""
        now = now or time.time()
        base = max(float(created or now), float(last_said or 0))
        age = max(0.0, now - base) / 86400
        imp = min(1.0, max(0, int(evidence or 1) - 1) / 3)
        s = 0.5 ** (age / (HALF_LIFE_DAYS * (1 + 2 * imp)))
        return max(s, 0.08) if int(evidence or 1) >= 2 else s

    def _strength_of(self, f, now: float) -> float:
        g = f.get if isinstance(f, dict) else (lambda k, d=None: f[k] if k in f.keys() else d)
        return self.strength(g("created"), g("hits") or 0, now, evidence=g("evidence") or 1, last_said=g("last_said"))

    def recall(self, gid: int, query: str, *, skip_after_id: int | None = None, k_msgs: int = 4, k_facts: int = 4,
               max_docs: int = 4000, now: float | None = None, own_texts: list[str] | None = None) -> dict:
        """在本群里找和 query 相关的老消息和事实。skip_after_id：这个 id 之后的消息已经在聊天记录里了，不重复给。
        24.0：own_texts 是在说话的人自己最近的话（压掉他的口头禅）；事实要过证据门槛 MIN_CONF，
        没有一条够强（STRONG_CONF）就只给 1 条；半小时内刚给过的不再给。"""
        now = now or time.time()
        q = tokens(query)
        out = {"messages": [], "facts": []}
        if not q:
            return out
        wts = own_weights(q, own_texts or [])
        rows, bm = self._index(gid, max_docs)
        if skip_after_id:                                     # 聊天记录窗口里已经有的不重复给
            keep = [i for i, r in enumerate(rows) if r["id"] < skip_after_id]
        else:
            keep = list(range(len(rows)))
        if keep:
            allsc = bm.scores(q, wts)
            rows = [rows[i] for i in keep]
            sc = [allsc[i] for i in keep]
            ranked = sorted(range(len(rows)), key=lambda i: -sc[i])
            ranked = [i for i in ranked if conf(sc[i]) >= MSG_MIN_CONF][:30]
            rec = sorted(ranked, key=lambda i: -rows[i]["ts"])           # 第二路：越近越好
            dense = self._dense(rows, query)                              # 第三路（可选）：语义相近
            fused = rrf([("m", i) for i in ranked], [("m", i) for i in rec], [("m", i) for i in dense])
            sc = [s + (0.5 if i in set(dense[:10]) else 0.0) for i, s in enumerate(sc)]
            best = sorted(fused, key=lambda k: -fused[k] * (1 + sc[k[1]] / (1 + max(sc))))[:k_msgs]
            out["messages"] = sorted((dict(rows[i]) for _, i in best), key=lambda r: r["ts"])
        frows = self.db.conn.execute("SELECT * FROM qq_facts WHERE group_id=? AND (t_invalid IS NULL OR t_invalid>?) "
                                     "AND NOT (source='guess' AND evidence<2) "      # 16.0 只猜过一次的先不拿来用
                                     "ORDER BY id DESC LIMIT 2000", (gid, time.time())).fetchall()
        if frows:
            bm = BM25([tokens(r["text"]) for r in frows])
            sc = bm.scores(q, wts)
            cs = [conf(x) for x in sc]
            ranked = [i for i in sorted(range(len(frows)), key=lambda i: -sc[i]) if cs[i] >= MIN_CONF][:30]
            ranked = [i for i in ranked if not (frows[i]["last_hit"] and now - float(frows[i]["last_hit"]) < RECALL_COOLDOWN
                                                and cs[i] < 0.9)]
            by_strength = sorted(ranked, key=lambda i: -self._strength_of(frows[i], now))
            fused = rrf(ranked, by_strength)
            k = k_facts if any(cs[i] >= STRONG_CONF for i in ranked) else 1
            pick = sorted(fused, key=lambda i: -fused[i])[:k]
            out["facts"] = [dict(frows[i]) for i in pick]
            for i in pick:                                    # 只记“什么时候给过她”（冷却、统计用），不加强
                self.db.conn.execute("UPDATE qq_facts SET hits=hits+1, last_hit=? WHERE id=?", (now, frows[i]["id"]))
            self.db.conn.commit()
        return out

    def _dense(self, rows: list[dict], query: str, k: int = 20) -> list[int]:
        if self.embedder is None or not rows:
            return []
        try:
            import numpy as np  # type: ignore
            ids = [r["id"] for r in rows]
            got = {}
            for i in range(0, len(ids), 900):
                chunk = ids[i:i + 900]
                q = ",".join("?" * len(chunk))
                for r in self.db.conn.execute(f"SELECT msg_id, vec FROM qq_embed WHERE msg_id IN ({q})", chunk):
                    got[r["msg_id"]] = np.frombuffer(r["vec"], dtype="float32")
            have = [i for i, mid in enumerate(ids) if mid in got]
            if not have:
                return []
            qv = self.embedder.encode([query[-300:]])[0]
            have = [i for i in have if len(got[ids[i]]) == len(qv)]     # 换过向量模型：老向量（长度不一样）不用
            sims = [(float(got[ids[i]] @ qv), i) for i in have]
            return [i for s, i in sorted(sims, reverse=True)[:k] if s > 0.35]
        except Exception:  # noqa: BLE001
            return []

    def embed_pending(self, limit: int = 256) -> int:
        """给还没有向量的消息补向量（后台慢慢做）。"""
        if self.embedder is None:
            return 0
        rows = self.db.conn.execute(
            "SELECT m.id, m.content FROM qq_group_messages m LEFT JOIN qq_embed e ON e.msg_id=m.id "
            "WHERE e.msg_id IS NULL AND length(m.content)>=4 ORDER BY m.id DESC LIMIT ?", (limit,)).fetchall()
        if not rows:
            return 0
        vecs = self.embedder.encode([r["content"][:300] for r in rows])
        dim = int(vecs.shape[1]) if len(vecs) else 0
        if dim and not getattr(self, "_dim_checked", False):        # 换过向量模型：长度不一样的老向量删掉，慢慢重算
            self._dim_checked = True
            n_old = self.db.conn.execute("DELETE FROM qq_embed WHERE length(vec)!=?", (dim * 4,)).rowcount
            if n_old:
                import logging
                logging.getLogger("aigroup.memory").info("换了向量模型：%d 条老向量删掉重算", n_old)
        self.db.conn.executemany("INSERT OR REPLACE INTO qq_embed(msg_id, vec) VALUES (?,?)",
                                 [(r["id"], v.tobytes()) for r, v in zip(rows, vecs)])
        self.db.conn.commit()
        return len(rows)

    def _index(self, gid: int, max_docs: int = 4000):
        top = self.db.conn.execute("SELECT MAX(id) m FROM qq_group_messages WHERE group_id=?", (gid,)).fetchone()["m"] or 0
        cur = self._idx.get(gid)
        if cur and top - cur[0] < 25:                         # 聊天记录窗口只带最近 30 条：落后太多就重建，别让中间那段想不起来
            return cur[1], cur[2]
        rows = self.db.conn.execute("SELECT id, ts, name, role, content FROM qq_group_messages WHERE group_id=? "
                                    "ORDER BY id DESC LIMIT ?", (gid, max_docs)).fetchall()
        rows = [dict(r) for r in rows if len(r["content"]) >= 4 and not r["content"].startswith("[进群了]")]
        bm = BM25([tokens(r["content"]) for r in rows])
        self._idx[gid] = (top, rows, bm)
        return rows, bm

    def prompt_block(self, found: dict, names: dict | None = None, focus: list | None = None) -> str:
        from aigroup.core.safety import clean_member_text, quote_member
        lines = []
        for f in found.get("facts") or []:
            who = ""
            if f.get("subject_id") == -1:
                who = "你自己："
            elif f.get("subject_id") and names and str(f["subject_id"]) in names:
                who = f"{names[str(f['subject_id'])]}："
            day = rel_day(f.get("last_said") or f["t_valid"], time.time(), self.tz)
            src = {"self": "（他自己说的）", "other": "（听别人说的，未必准）", "guess": "（你猜的，别当真说出来）"}.get(
                str(f.get("source") or "") if f.get("subject_id") not in (-1, 0) else "", "")
            exp = (f"（到 {datetime.fromtimestamp(f['t_invalid'], self.tz):%m-%d} 为止）"
                   if f.get("t_invalid") else "")
            fuzzy = ""
            if f.get("created") and self._strength_of(f, time.time()) < 0.35:
                fuzzy = "（记得不太清了：说的时候带个“好像”“我记得是”）"     # 20.0 记忆会淡：太久没想起的别说得斩钉截铁
            lines.append(f"- （{day}）{who}{clean_member_text(f['text'], 120)}{src}{exp}{fuzzy}{self.presence_note(f, names, focus)}")
        for m in found.get("messages") or []:
            day = datetime.fromtimestamp(m["ts"], self.tz).strftime("%m-%d %H:%M")
            who = "你" if m["role"] == "assistant" else m["name"]
            txt = clean_member_text(m["content"], 80)                 # 老数据可能没洗过：和聊天记录一样消毒、加引号
            old = "（很久以前的，印象有点模糊）" if time.time() - float(m["ts"]) > 30 * 86400 else ""
            lines.append(f"- [{day}] {who} 说过：{txt if m['role'] == 'assistant' else quote_member(txt)}{old}")
        if not lines:
            return ""
        return "# 你隐约想起来的（以前群里的事，聊到了可以自然提一下，没聊到就别硬提；可能已经过时）\n" + "\n".join(lines)
