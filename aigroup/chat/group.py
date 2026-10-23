"""群聊的总装：GroupChat 把各部分拼起来。

一条消息走过的路：
    qq/onebot.py 收到 → ingest.py 记下、凑一批 → speak.py 算要不要说（plan.py 的 decide + gate.py 的概率阈值）
    → prompt.py 拼提示词 → 大模型 → plan.py 解析 → quality.py 质检挑一条 → 发出去 → feedback.py 看有没有人接

GroupChat 由这些 Mixin 组成（每个文件管一件事，彼此通过 self 上的共用属性协作）：
    IngestMixin    chat/ingest.py     收消息、补消息、记录
    SpeakMixin     chat/speak.py      判断、说话、复读、点表情
    IdleMixin      chat/idle.py       冒泡、早上补回、戳回去、改签名
    FeedbackMixin  chat/feedback.py   说完的效果、回复节奏、每晚的活（nightly）
    LifeMixin      chat/life.py       她此刻在干嘛、感兴趣的话题
    PromptMixin    chat/prompt.py     提示词（聊天记录、群里的人、规则）
    PeopleMixin    memory/people.py   群友：画像、别名、成员列表
    DigestMixin    memory/digest.py   整理：群里的事和梗、画像、群日报
    ListenerMixin  chat/listener.py   21.1 独立的听者：她主动插话前，另一个模型判时机
    SocialMixin    chat/social.py     21.0 九问自评记录、等等队列、擦边再问、人卡、四条硬底线

设置都在 chat/settings.py；每个群的运行时状态在 chat/state.py。
"""
from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from collections import ChainMap

from aigroup.agent.mixin import AgentMixin
from aigroup.chat.checks import ChecksMixin
from aigroup.memory.autobio import AutobioMixin
from aigroup.chat.feedback import SCHEMA as EFFECT_SCHEMA
from aigroup.chat.feed import FeedMixin
from aigroup.chat.feedback import FeedbackMixin
from aigroup.chat.gate import GateLog, active_weights
from aigroup.chat.grow import GrowMixin
from aigroup.chat.humane import HumaneMixin
from aigroup.chat.idle import IdleMixin
from aigroup.chat.ingest import IngestMixin
from aigroup.chat.insight import InsightMixin
from aigroup.chat.life import LifeMixin
from aigroup.chat.mood import Mood
from aigroup.chat.plan import NAME_TAIL
from aigroup.chat.prompt import PromptMixin
from aigroup.chat.settings import DEAD_KNOBS, DEFAULTS, GLOBAL_ONLY, KNOBS, group_key
from aigroup.chat.listener import ListenerMixin
from aigroup.chat.social import SocialMixin
from aigroup.chat.speak import SpeakMixin
from aigroup.chat.state import GroupState
from aigroup.chat.trace import Tracer
from aigroup.core.store import ensure_columns
from aigroup.eval.weekly import EvalMixin
from aigroup.memory.digest import DigestMixin
from aigroup.memory.style import SCHEMA as STYLE_SCHEMA
from aigroup.memory.style import StyleMixin
from aigroup.memory.facts import Memory
from aigroup.memory.learn import Learner
from aigroup.memory.lore import Lore
from aigroup.memory.people import PEOPLE_COLUMNS, PeopleMixin
from aigroup.memory.people import SCHEMA as PEOPLE_SCHEMA
from aigroup.memory.stickers import Stickers
from aigroup.net.knowledge import NetMixin

log = logging.getLogger("aigroup.group")

SCHEMA = """
CREATE TABLE IF NOT EXISTS qq_group_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    group_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL,              -- user（群友，包括他）/ assistant（她）
    content TEXT NOT NULL,
    qq_msg_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_qq_group_messages ON qq_group_messages(group_id, id);
CREATE INDEX IF NOT EXISTS idx_qq_msg_user ON qq_group_messages(user_id, group_id);
CREATE INDEX IF NOT EXISTS idx_qq_msg_qqid ON qq_group_messages(group_id, qq_msg_id);
CREATE INDEX IF NOT EXISTS idx_qq_msg_ts ON qq_group_messages(ts);
CREATE INDEX IF NOT EXISTS idx_qq_msg_gts ON qq_group_messages(group_id, ts);
CREATE INDEX IF NOT EXISTS idx_qq_msg_grole ON qq_group_messages(group_id, role, id);
"""
MSG_COLUMNS = {"to_her": "INTEGER NOT NULL DEFAULT 0",   # 这条是在叫她（@、回复她、叫名字、戳）
               "reply_to": "INTEGER"}                    # 引用了哪条消息（QQ 消息 id）


class GroupChat(FeedMixin, IngestMixin, SpeakMixin, IdleMixin, FeedbackMixin, LifeMixin, PromptMixin, PeopleMixin, DigestMixin,
                NetMixin, GrowMixin, EvalMixin, StyleMixin, InsightMixin, AgentMixin, HumaneMixin, SocialMixin,
                ListenerMixin, ChecksMixin, AutobioMixin):
    """她在群里的一切：收消息 → 判断要不要说 → 说 → 看效果；空闲时冒泡、整理记忆。

    这个类只负责把各部分装起来、放共用的小工具；具体的活在各个 Mixin 里（见文件开头的说明）。
    各部分之间只通过 self 上的这些东西打交道：cfg、brain（人设 / 大模型 / 数据库）、ob（QQ）、db、g（设置）、
    state（每个群的运行时状态）、以及下面 __init__ 里建的几个组件（gate、learner、memory、stickers、tracer、lore、mood、detector）。
    """

    def __init__(self, cfg, brain, ob, owner_id: int, groups: list[int], aliases: list[str]):
        self.cfg, self.brain, self.ob = cfg, brain, ob
        self.db = brain.db
        self._boot_ts = time.time()                       # 5.0 这次启动的时间（重启前没回的 @ 要补）
        self.owner_id = int(owner_id or 0)
        self.groups = {int(g) for g in groups or []}
        p = brain.persona
        self.aliases = [a for a in {p.name, getattr(p, "formal_name", "") or "", *(aliases or [])} if a and len(a) >= 2]
        q = cfg.get("wechat" if cfg.get("channel") == "wechat" else "qq") or {}
        self.not_aliases = {str(a).strip() for a in (q.get("not_aliases") or []) if str(a).strip()}   # 18.1 这些不算叫她
        self.aliases = [a for a in self.aliases if a not in self.not_aliases]
        from aigroup.core.config import chan_group, set_channel
        self.channel = str(cfg.get("channel") or "wechat")
        set_channel(self.channel)
        from aigroup.chat.settings import defaults_for
        self.g = {**defaults_for(self.channel), **chan_group(cfg)}
        over = brain.db.get("web_overrides")              # 在小屋里调过的“性格开关”
        if isinstance(over, dict):
            dead = [k for k in over if k in DEAD_KNOBS]
            if dead:                                      # 1.4 删掉的开关：清掉，别一直留在数据里
                over = {k: v for k, v in over.items() if k not in DEAD_KNOBS}
                brain.db.set("web_overrides", over or None)
                log.info("清掉了已经不用的性格开关：%s", "、".join(dead))
            self.g.update({k: v for k, v in over.items() if k in DEFAULTS or k in self.g})
        self.gover: dict[int, dict] = {}                  # 2.2 每个群单独调过的开关（盖在 self.g 上面）
        for gid in self.groups:
            o = brain.db.get(group_key(gid))
            if isinstance(o, dict):
                self.gover[gid] = {k: v for k, v in o.items() if k in KNOBS and k not in GLOBAL_ONLY}
        self.state: dict[int, GroupState] = {}
        self.db.conn.executescript(SCHEMA + PEOPLE_SCHEMA + EFFECT_SCHEMA + STYLE_SCHEMA)
        ensure_columns(self.db.conn, "qq_people", PEOPLE_COLUMNS)
        ensure_columns(self.db.conn, "qq_group_messages", MSG_COLUMNS)
        self.db.conn.commit()
        self._migrate_20()
        self.gate = GateLog(self.db)
        self.learner = Learner(self.db, brain)
        from aigroup.memory.facts import load_embedder
        self.memory = Memory(self.db, cfg.tz, load_embedder(str(self.g.get("memory_embed_model") or ""), cfg))
        from pathlib import Path
        self.stickers = Stickers(self.db, Path(cfg.root))
        self.tracer = Tracer(self.db)
        self.lore = Lore(self.db) if self.g.get("lore", True) else None
        self.mood = Mood(self.db, cfg.tz) if self.g.get("mood", True) else None
        from aigroup.net.knowledge import Knowledge
        from aigroup.net.links import LinkReader
        from aigroup.net.search import WebSearch
        self.links = LinkReader(cfg, brain)                 # 读群友发的链接
        self.web = WebSearch(cfg)                           # 不知道的先搜一搜
        self.web.kv = self.db                               # 19.0 记 Tavily 这个月用了几次、免费额度用完没
        self.knowledge = Knowledge(self.db)
        from aigroup.net.voice import Hearing, Voice
        self.hearing = Hearing(cfg)                         # 13.0 听懂群友的语音
        self.voice = Voice(cfg)                             # 13.0 她偶尔发语音（服务器本地合成）
        from aigroup.net.photo import PhotoMaker
        self.photos = PhotoMaker(cfg, self.db, brain.persona, int(self.g.get("photo_per_day", 1)))
        self.photos.llm = brain.llm   # 偶尔晒照片
        self._grow_init()
        self._checks_init()
        from aigroup.net.where import Where
        self.where = Where(self.db, cfg.tz)                 # 4.4 群友在哪、那边天气
        from aigroup.eval.weekly import note_persona
        if note_persona(self.db, brain.persona):           # 4.3 人设改过：记一版，每周评测时和上一版比一比
            log.info("人设改过了，下次每周评测会和上一版比一比")                                   # 熟悉度、约好的事                 # 查到的东西记下来
        self._actions: dict[str, list[float]] = {}          # 戳回去、改签名这些小动作的时间（限频）
        self.weights, self.weights_src = active_weights(self.db)
        from aigroup.chat.detector import Detector
        self.detector = Detector.load(self.db)                    # AI 腔小分类器（没训练过就是 None）
        self._profiling = False
        self._bg: set[asyncio.Task] = set()
        self._digesting: set[int] = set()
        self._tmap: dict[int, dict] = {}
        self._clean_interests()

    # ---------------- 2.0 升级老数据 ----------------
    def _migrate_20(self):
        if self.db.get("migr_20"):
            return
        # 画像按群分开：老的全局画像复制到这个人说过话的每个群
        rows = self.db.conn.execute("SELECT q.user_id, m.group_id, COUNT(*) n, MAX(m.ts) last FROM qq_people q "
                                    "JOIN qq_group_messages m ON m.user_id=q.user_id AND m.role='user' "
                                    "GROUP BY q.user_id, m.group_id").fetchall()
        for r in rows:
            q = self.db.conn.execute("SELECT profile, note, since_update FROM qq_people WHERE user_id=?",
                                     (r["user_id"],)).fetchone()
            self.db.conn.execute(
                "INSERT OR IGNORE INTO qq_profiles(user_id, group_id, profile, note, msgs, since_update, last_seen) "
                "VALUES (?,?,?,?,?,?,?)", (r["user_id"], r["group_id"], q["profile"] or "", q["note"] or "", r["n"],
                                           q["since_update"] or 0, r["last"]))
        old_self = self.db.get("qq_self_notes")                   # 她自己的事：也按群分开
        if old_self:
            for gid in self.groups:
                if not self.db.get(f"qq_self_notes_{gid}"):
                    self.db.set(f"qq_self_notes_{gid}", old_self)
        old_cnt = self.db.get("qq_digest_count")
        if old_cnt is not None:
            self.db.set("qq_digest_count", None)
        self.db.conn.commit()
        self.db.set("migr_20", True)

    def prune(self):
        """清理旧数据：门控日志留 90 天、回放留 14 天、每天的统计和安排留 60 天。群消息一直留（以后要想起来）。"""
        try:
            self.gate.prune(90)
            self.tracer.prune()
            cutoff = (self.brain.now()).timestamp() - 60 * 86400
            from datetime import datetime as _dt
            for (k,) in self.db.conn.execute("SELECT k FROM kv WHERE k LIKE 'llm_stats_%' OR k LIKE 'life_%' "
                                             "OR k LIKE 'backup_%' OR k LIKE 'qq_nightly_%' OR k LIKE 'qq_daily_%'"):
                m = re.search(r"(\d{4}-\d{2}-\d{2})", k)
                if m and _dt.strptime(m.group(1), "%Y-%m-%d").timestamp() < cutoff:
                    self.db.conn.execute("DELETE FROM kv WHERE k=?", (k,))
            self.db.conn.execute("DELETE FROM qq_reply_effect WHERE ts<?", (time.time() - 180 * 86400,))
            keep = int(self.g.get("keep_days", 0) or 0)
            if keep > 0:                                        # 13.0 原始消息只留这么多天（事实和画像不受影响）
                old = time.time() - keep * 86400
                self.db.conn.execute("DELETE FROM qq_embed WHERE msg_id IN (SELECT id FROM qq_group_messages WHERE ts<?)", (old,))
                n_old = self.db.conn.execute("DELETE FROM qq_group_messages WHERE ts<?", (old,)).rowcount
                if n_old:
                    log.info("清掉了 %d 条 %d 天以前的群消息", n_old, keep)
            n_img = self.db.conn.execute("SELECT COUNT(*) FROM kv WHERE k LIKE 'qq_img_%'").fetchone()[0]
            self.db.conn.execute("DELETE FROM kv WHERE k IN (SELECT k FROM kv WHERE k LIKE 'qq_stkdesc_%' "
                                 "ORDER BY rowid LIMIT MAX(0, (SELECT COUNT(*) FROM kv WHERE k LIKE 'qq_stkdesc_%') - 3000))")
            if n_img > 3000:                                    # 看过的图的描述：太多了就清掉一半（只是缓存）
                self.db.conn.execute("DELETE FROM kv WHERE k IN (SELECT k FROM kv WHERE k LIKE 'qq_img_%' LIMIT ?)",
                                     (n_img // 2,))
            self.db.conn.commit()
        except Exception as e:  # noqa: BLE001
            log.info("清理旧数据出错：%s", e)

    def _st(self, gid: int) -> GroupState:
        if gid not in self.state:
            st = GroupState()
            row = self.db.conn.execute("SELECT MAX(ts) t FROM qq_group_messages WHERE group_id=?", (gid,)).fetchone()
            st.last_msg_ts = (row["t"] if row else 0) or 0.0
            row = self.db.conn.execute(
                "SELECT MAX(ts) t FROM qq_group_messages WHERE group_id=? AND role='assistant'", (gid,)).fetchone()
            st.last_reply_ts = (row["t"] if row else 0) or 0.0
            self.state[gid] = st
        return self.state[gid]

    def names(self, gid: int, members: bool = False) -> dict[str, str]:
        """这个群最近说过话的人：QQ 号 → 名字。members：再补上群成员列表里的（只用来把 @某人 写成名字）。"""
        rows = self.db.conn.execute(
            "SELECT user_id, name FROM qq_group_messages WHERE group_id=? AND role='user' ORDER BY id DESC LIMIT 300",
            (gid,)).fetchall()
        out: dict[str, str] = {}
        for r in rows:
            out.setdefault(str(r["user_id"]), r["name"])
        if members:
            for k, v in (self.db.get(f"qq_members_{gid}") or {}).items():
                out.setdefault(str(k), v)
        return out

    def muted(self, gid: int) -> bool:
        return bool(self.db.get(f"qq_mute_{gid}"))

    def _called(self, text: str) -> bool:
        """群友打的字里叫了她的名字（“小满足”“小满月”这种不算）。"""
        for a in self.aliases:
            for m in re.finditer(re.escape(a), text or ""):
                nxt = text[m.end():m.end() + 1]
                if not nxt or nxt not in NAME_TAIL:
                    return True
        return False

    def gk(self, gid: int | None):
        """这个群实际用的开关：这个群单独调过的 → 全局（小屋 / config.yaml / 默认）。"""
        o = self.gover.get(int(gid)) if gid is not None else None
        return ChainMap(o, self.g) if o else self.g

    def secs(self, key: str, gid: int | None = None) -> float:
        g = self.gk(gid)
        v = g.get(key) or [0, 0]
        lo, hi = (float(v[0]), float(v[1])) if isinstance(v, (list, tuple)) else (float(v), float(v))
        return random.uniform(lo, max(lo, hi)) * float(g.get("pace", 1.0))

    def _spawn(self, coro) -> asyncio.Task:
        t = asyncio.create_task(coro)
        self._bg.add(t)

        def done(task: asyncio.Task):
            self._bg.discard(task)
            if not task.cancelled() and task.exception() is not None:
                log.warning("后台任务出错：%r", task.exception())
        t.add_done_callback(done)
        return t

    def _allow(self, action: str, per_hour: int) -> bool:
        now = time.time()
        ts = [t for t in self._actions.get(action, []) if now - t < 3600]
        if len(ts) >= per_hour:
            self._actions[action] = ts
            return False
        self._actions[action] = ts + [now]
        return True


