"""看效果、自己调整：她说完以后有没有人接（qq_reply_effect）、群友的回复节奏；
以及每晚的活（nightly）：群日报、重新拟合门控、训练 AI 腔检测器、黑话变群梗词条、仔细回想画像、清理旧数据。
"""
from __future__ import annotations

import json
import logging
import time
from datetime import timedelta

from aigroup.chat.plan import bigram_overlap

log = logging.getLogger("aigroup.group")


def effect_score(direct: bool, target_spoke: bool, topic: bool, followups: int) -> float:
    if direct:
        return 1.0
    if target_spoke or topic:
        return 0.6
    return 0.2 if followups else 0.0


SCHEMA = """
CREATE TABLE IF NOT EXISTS qq_reply_effect (   -- 她说完以后有没有人接
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    group_id INTEGER NOT NULL,
    gate_id INTEGER,
    mentioned INTEGER NOT NULL DEFAULT 0,
    target_uid INTEGER,
    her_ids TEXT NOT NULL DEFAULT '[]',     -- 她这几条的 QQ 消息 id
    text TEXT NOT NULL DEFAULT '',
    done INTEGER NOT NULL DEFAULT 0,
    followups INTEGER NOT NULL DEFAULT 0,  -- 5 分钟内群友说了几句
    direct INTEGER NOT NULL DEFAULT 0,     -- 有人回复她 / @她 / 叫她
    score REAL                             -- 1 直接接她的话 · 0.6 接着话题 / 对象回了 · 0.2 群里在聊别的 · 0 冷场
);
CREATE INDEX IF NOT EXISTS idx_qq_reply_effect ON qq_reply_effect(done, ts);
"""


class FeedbackMixin:
    def evaluate_effects(self, now: float | None = None, window: float = 300.0) -> int:
        """她说完 5 分钟后看一眼：有没有人回复 / @ / 叫她，她接话的那个人有没有回，大家是不是接着这个话题说。"""
        now = now or time.time()
        lat = self.latency_stats(None)
        if lat.get("n", 0) >= 50:                               # 冷场判断按这个群自己的节奏：平均回复间隔的 10 倍（2~15 分钟）
            window = max(120.0, min(900.0, 10 * float(lat["mean"])))
        rows = self.db.conn.execute("SELECT * FROM qq_reply_effect WHERE done=0 AND ts<?", (now - window,)).fetchall()
        for r in rows:
            ids = set(json.loads(r["her_ids"] or "[]"))
            after = self.db.conn.execute(
                "SELECT user_id, content, to_her, reply_to, ts FROM qq_group_messages WHERE group_id=? AND role='user' "
                "AND ts>? AND ts<=? ORDER BY id", (r["group_id"], r["ts"], r["ts"] + window)).fetchall()
            direct = any(x["to_her"] or (x["reply_to"] and x["reply_to"] in ids) for x in after)
            target = bool(r["target_uid"]) and any(int(x["user_id"]) == int(r["target_uid"]) and x["ts"] <= r["ts"] + 120
                                                   for x in after)
            topic = any(bigram_overlap(r["text"], x["content"]) >= 0.2 for x in after[:6])
            score = effect_score(direct, target, topic, len(after))
            if score <= 0 and not r["mentioned"] and self.mood is not None:
                self.mood.on_ignored(int(r["group_id"]))
            self.db.conn.execute("UPDATE qq_reply_effect SET done=1, followups=?, direct=?, score=? WHERE id=?",
                                 (len(after), int(direct), score, r["id"]))
        if rows:
            self.db.conn.commit()
        return len(rows)

    def effect_stats(self, since: float, gid: int | None = None) -> dict:
        sql = ("SELECT COUNT(*) n, AVG(score) avg, SUM(score>=0.6) engaged, SUM(score=0) cold, SUM(mentioned=0) active "
               "FROM qq_reply_effect WHERE done=1 AND ts>=?")
        args: tuple = (since,)
        if gid is not None:                                  # 2.2 每个群分开看
            sql += " AND group_id=?"
            args = (since, int(gid))
        r = self.db.conn.execute(sql, args).fetchone()
        n = r["n"] or 0
        return {"n": n, "avg": round(r["avg"] or 0, 3), "engaged": r["engaged"] or 0, "cold": r["cold"] or 0,
                "rate": round((r["engaged"] or 0) / n, 3) if n else None}

    def latency_stats(self, gid: int | None) -> dict:
        """群友之间的回复间隔（相邻两条、不同的人、30 分钟内），每小时最多算一次。Kalman 2006：服从幂律，
        至少 80% 在平均以内、97% 在 10 倍平均以内。"""
        key = f"qq_latency_{gid or 'all'}"
        cur = self.db.get(key) or {}
        if cur and time.time() - float(cur.get("ts", 0)) < 3600:
            return cur
        return self.refresh_latency(gid)

    def refresh_latency(self, gid: int | None = None) -> dict:
        sql = "SELECT group_id, user_id, ts FROM qq_group_messages WHERE role='user'"
        args: list = []
        if gid:
            sql += " AND group_id=?"
            args.append(gid)
        rows = self.db.conn.execute(sql + " ORDER BY id DESC LIMIT 4000", args).fetchall()[::-1]
        gaps = []
        for a, b in zip(rows, rows[1:]):
            if a["group_id"] == b["group_id"] and a["user_id"] != b["user_id"] and 0 < b["ts"] - a["ts"] < 1800:
                gaps.append(b["ts"] - a["ts"])
        gaps.sort()
        out = {"ts": time.time(), "n": len(gaps)}
        if gaps:
            out.update(mean=round(sum(gaps) / len(gaps), 1), median=round(gaps[len(gaps) // 2], 1),
                       q=[round(gaps[min(len(gaps) - 1, int(i * len(gaps) / 20))], 1) for i in range(20)])
        self.db.set(f"qq_latency_{gid or 'all'}", out)
        return out

    def train_detector(self):
        from aigroup.chat.detector import train_from_db
        det = train_from_db(self.db, mask=self.detector_mask())
        info = self.db.get("ai_detector_info") or {}
        self.detector = det                      # 22.3 没过线就不用（以前过不了线时还留着旧模型）
        if det is not None:
            log.info("AI 腔检测器训练好了（留出 AUC %s，她的话 p10/p50/p90 = %s）", info.get("auc"), info.get("her_p"))
        else:
            log.info("AI 腔检测器这次没过线，先不用（%s）", info)

    def detector_mask(self) -> list[str]:
        """22.3 训练 / 打分前要换成占位符的词：群友的名字和外号、她的名字和口头禅（不然学成了“谁在说话”）。"""
        out: list[str] = []
        for gid in self.groups:
            for uid in self.names(gid):
                try:
                    out += self.names_of(gid, int(uid)) if hasattr(self, "names_of") else [self.names(gid)[uid]]
                except (ValueError, KeyError):
                    continue
        p = self.brain.persona
        out += [p.name, getattr(p, "formal_name", "") or ""] + list(getattr(p, "catchphrases", None) or []) + list(self.aliases or [])
        return sorted({str(w).strip() for w in out if w and len(str(w).strip()) >= 2})

    def auto_lore(self):
        if self.lore is None:
            return
        for gid in self.groups:
            jg = [dict(r) for r in self.db.conn.execute(
                "SELECT term, meaning FROM qq_jargon WHERE group_id=? AND is_jargon=1 AND status!='no' AND meaning!=''",
                (gid,)).fetchall()]
            memes = self.db.get(f"qq_memes_{gid}") or []
            self.lore.sync_auto(gid, jg, [(m[0], m[1]) for m in memes if isinstance(m, list) and len(m) == 2])

    async def nightly(self):
        """每天一次：昨天的群日报、用自己的数据重新拟合门控、清理旧数据。"""
        now = self.brain.now()
        key = f"qq_nightly_{now:%Y-%m-%d}"
        if self.db.get(key):
            return
        self.db.set(key, True)
        yday = now - timedelta(days=1)
        for gid in sorted(self.groups):
            try:
                await self.daily_report(gid, yday)
            except Exception as e:  # noqa: BLE001
                log.info("群 %s 日报出错：%s", gid, e)
        if self.g.get("weekly_reflect", True) and hasattr(self, "weekly_reflect"):
            try:
                await self.weekly_reflect()                      # 每周一：回想这一周，人物跟着经历慢慢变
            except Exception as e:  # noqa: BLE001
                log.info("每周回想出错：%s", e)
        if now.weekday() == 0 and hasattr(self, "weekly_eval"):
            try:
                await self.weekly_eval()                           # 4.3 每周一：拿真实聊天评一次分
            except Exception as e:  # noqa: BLE001
                log.info("每周评测出错：%s", e)
            try:                                                   # 21.1 一周一张表（每个问题分开看）
                from aigroup.eval.vector import save_snapshot, weekly_vector
                for gid in self.groups:
                    save_snapshot(self.db, weekly_vector(self, gid))
            except Exception as e:  # noqa: BLE001
                log.info("每周表出错：%s", e)
        if hasattr(self, "update_fingerprints"):
            try:                                                   # 13.0 说话指纹、她自己的风格审计（只统计，不花钱）
                nfp = self.update_fingerprints()
                nst = self.style_audit(yday)
                log.info("每晚统计：%d 个人的打字习惯；%d 个群里她有要注意的说话毛病", nfp, nst)
            except Exception as e:  # noqa: BLE001
                log.info("统计说话习惯出错：%s", e)
        if self.g.get("metrics_daily", True):
            try:                                                   # 22.0 每天的确定性指标、个性保留
                from aigroup.eval.daily import run_daily
                for m in run_daily(self, yday):
                    log.info("每天指标（群 %s）：她 %s 句，说自己经历 %s 句、没来源 %s；检查命中 %s；味道 %s",
                             m["gid"], m["her"], m["self_claims"], m["ungrounded"], m["checks"], m["flavor"])
            except Exception as e:  # noqa: BLE001
                log.info("每天指标出错：%s", e)
        if hasattr(self, "update_closeness"):
            try:
                self.update_closeness(yday)                        # 昨天和她聊过的人熟一点
            except Exception as e:  # noqa: BLE001
                log.info("更新熟悉度出错：%s", e)
        if self.g.get("monthly_self", True) and hasattr(self, "monthly_self") and now.day <= 3:
            try:
                await self.monthly_self()                          # 每月初：整理一次“我是怎样的人”
            except Exception as e:  # noqa: BLE001
                log.info("每月整理出错：%s", e)
        import asyncio

        from aigroup.chat.gate import refit
        try:
            w = await asyncio.to_thread(refit, self.db, f"{now:%Y-%m-%d}")     # 几千条要算一两秒：别卡住聊天
            last = self.db.get("gate_refit_last") or {}
            if w:
                log.info("用自己的数据重新拟合了发言权重（验证集 logloss %s → %s）", last.get("old_loss"), last.get("new_loss"))
            elif last.get("day") == f"{now:%Y-%m-%d}":
                log.info("重新拟合的权重没比现在的好，先不换")
        except Exception as e:  # noqa: BLE001
            log.info("重新拟合发言权重出错：%s", e)
        try:                                                      # 20.2 最近几天的事实补算“当时谁在”
            await asyncio.to_thread(self.memory.refresh_presence)
        except Exception as e:  # noqa: BLE001
            log.info("补算在场的人出错：%s", e)
        try:
            n = await asyncio.to_thread(self.memory.embed_pending, 2000)
            if n:
                log.info("给 %d 条消息补了向量", n)
        except Exception as e:  # noqa: BLE001
            log.info("补向量出错：%s", e)
        if hasattr(self, "favors_nightly"):
            try:                                                  # 21.1 人情账
                await self.favors_nightly()
            except Exception as e:  # noqa: BLE001
                log.info("人情账出错：%s", e)
        try:                                                      # 16.0 关系网、相处经验、小判断器
            self.update_rel_graph()
            if self.g.get("rel_experience", True):
                await self.rel_experience()
            if self.g.get("rel_tone", True) and now.weekday() in (0, 3):     # 20.2 关系色彩：一周两次就够
                await self.rel_tone()
            if self.g.get("label_nb", True):
                from aigroup.eval.label import weak_labels
                await asyncio.to_thread(weak_labels, self.db)        # 20.0 没人标的、程序能推出来的弱标签
                await asyncio.to_thread(self.train_label_nb)
        except Exception as e:  # noqa: BLE001
            log.info("关系网 / 相处经验 / 小判断器出错：%s", e)
        if hasattr(self, "build_interests"):
            try:                                                  # 20.2 “刷到的”：兴趣表、分享后的反馈
                self.feed_learn()
                for gid in sorted(self.groups):
                    self.build_interests(gid)
            except Exception as e:  # noqa: BLE001
                log.info("整理兴趣表出错：%s", e)
        if self.g.get("person_spec", True) and hasattr(self, "build_specs"):
            try:                                                  # 19.1 相处说明书（每人一周写一次）
                await self.build_specs()
            except Exception as e:  # noqa: BLE001
                log.info("相处说明书出错：%s", e)
        for name in ("train_detector", "refresh_latency", "auto_lore", "deep_profiles"):   # 3.0 / 2.2 / 3.1：AI 腔检测器、回复延迟、群梗词条、仔细回想画像
            fn = getattr(self, name, None)
            if fn:
                try:
                    r = fn()
                    if asyncio.iscoroutine(r):
                        await r
                except Exception as e:  # noqa: BLE001
                    log.info("%s 出错：%s", name, e)
        self.prune()
        if now.weekday() == 6:                                   # 每周日整理一次数据库文件（删掉的数据真正腾出空间）
            try:
                self.db.conn.commit()
                self.db.conn.execute("VACUUM")
            except Exception as e:  # noqa: BLE001
                log.info("VACUUM 没做成：%s", e)
