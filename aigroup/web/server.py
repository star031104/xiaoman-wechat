"""小满的小屋：http://127.0.0.1:8768（只能在这台电脑上打开）。

看她此刻在干嘛、今天的安排、每个群聊了什么、她心里每个群友的样子；
可以改画像、记的梗、她的兴趣，调插话概率这些“性格开关”（马上生效），让某个群先别说话，替她说一句，
管大模型接口，看日志。
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path

from aiohttp import web

from aigroup.chat.settings import defaults_for
from aigroup.core.config import chan_group
from aigroup.chat.settings import (  # noqa: F401  （测试从这里拿 clean_knob）
    KNOBS,
    clean_knob,
)
from aigroup.memory.people import MEMORY_CHARS, PROFILE_CHARS, PROFILE_KEYS, PROFILE_MEMORIES

log = logging.getLogger("aigroup.web")
PAGE = Path(__file__).with_name("page.html")
TOKENS = Path(__file__).resolve().parent / "tokens.css"          # 颜色、时段、深浅色、组件
AVATAR_EXT = (".webp", ".png", ".jpg", ".jpeg")
LOCAL = ("127.0.0.1", "localhost", "[::1]", "::1")

@web.middleware
async def local_only(req, handler):
    host = (req.host or "").split(":")[0].lower()
    if host not in LOCAL:
        raise web.HTTPForbidden(text="只能在本机打开")
    if req.path.startswith("/api/"):
        if req.headers.get("Sec-Fetch-Site") in ("cross-site", "same-site"):
            raise web.HTTPForbidden(text="不允许跨站调用")
        origin = req.headers.get("Origin")
        if origin and origin != "null":
            from urllib.parse import urlparse
            if (urlparse(origin).hostname or "").lower() not in LOCAL:
                raise web.HTTPForbidden(text="不允许跨站调用")
    return await handler(req)


class GroupHome:
    def __init__(self, cfg, brain, ob, gc):
        self.cfg, self.brain, self.ob, self.gc = cfg, brain, ob, gc
        self.db = brain.db
        c = cfg.get("web") or {}
        self.enabled = bool(c.get("enabled", True))
        self.host = "127.0.0.1"
        self.port = int(c.get("port", 8768))
        self.runner: web.AppRunner | None = None
        self.started = time.time()
        self.nick = ""

    # ---------------- 启停 ----------------
    def app(self) -> web.Application:
        a = web.Application(middlewares=[local_only])
        r = a.router
        r.add_get("/", self.page)
        r.add_get("/api/state", self.state)
        r.add_get("/api/today", self.today)                   # 24.0 今天她做了什么、7 天趋势、后台问题、生效设置
        r.add_get("/tokens.css", self.tokens_css)
        r.add_get("/avatar/{kind}", self.avatar)
        r.add_get("/api/insight", self.insight)               # 17.0 只读：关系网、相处经验（每晚算好存着的）
        r.add_get("/api/agent", self.agent_view)               # 19.0 她查过什么：每次查了几轮、多久、简报
        r.add_get("/api/humane", self.humane)                 # 19.1 像人的几个指标
        r.add_get("/api/blind", self.blind_new)               # 20.0 盲测
        r.add_post("/api/blind", self.blind_answer)
        r.add_get("/api/messages", self.messages)
        r.add_get("/api/people", self.people)
        r.add_get("/api/person", self.person)
        r.add_post("/api/person/update", self.person_update)
        r.add_post("/api/person/forget", self.person_forget)
        r.add_post("/api/person/refresh", self.person_refresh)
        r.add_get("/api/her", self.her)
        r.add_post("/api/her/interests", self.her_interests)
        r.add_post("/api/her/self_notes", self.her_self_notes)
        r.add_post("/api/her/replan", self.her_replan)
        r.add_post("/api/group/notes", self.group_notes)
        r.add_post("/api/group/mute", self.group_mute)
        r.add_post("/api/group/say", self.group_say)
        r.add_post("/api/group/nudge", self.group_nudge)
        r.add_get("/api/settings", self.settings_get)
        r.add_post("/api/settings", self.settings_set)
        r.add_get("/api/logs", self.logs)
        r.add_get("/api/llm", self.llm_list)
        r.add_post("/api/llm/models", self.llm_models)
        r.add_post("/api/llm/test", self.llm_test)
        r.add_post("/api/llm/add", self.llm_add)
        r.add_post("/api/llm/remove", self.llm_remove)
        r.add_post("/api/restart", self.restart)
        r.add_get("/api/learned", self.learned)
        r.add_post("/api/learned/set", self.learned_set)
        r.add_get("/api/gate", self.gate_info)
        r.add_post("/api/learned/sticker", self.sticker_set)
        r.add_get("/api/sticker", self.sticker_file)
        r.add_post("/api/learned/fact", self.fact_set)
        r.add_post("/api/group/norms", self.group_norms)
        r.add_get("/api/pairs/next", self.pairs_next)
        r.add_post("/api/pairs/answer", self.pairs_answer)
        r.add_get("/api/label/next", self.label_next)          # 13.0 金标准集：这一刻她该不该开口
        r.add_post("/api/label/answer", self.label_answer)
        r.add_get("/api/calib/next", self.calib_next)          # 20.1 给她的话打分（评委校准）
        r.add_post("/api/calib/answer", self.calib_answer)
        r.add_get("/api/spot", self.spot_get)                  # 29.0 每天抽查：想说的 vs 发出的、查到的梗
        r.add_post("/api/spot", self.spot_post)
        r.add_get("/api/rules", self.rules_info)               # 21.1 规则登记：命中、拦对率、关掉
        r.add_post("/api/rules/judge", self.rules_judge)
        r.add_post("/api/rules/toggle", self.rules_toggle)
        r.add_get("/api/checks", self.checks)                  # 20.1 每周额外检查：评委、问答一致、讨好、说话统计、消融
        r.add_get("/api/ledger", self.ledger_list)             # 20.2 她查到的事实（账本）
        r.add_post("/api/ledger/delete", self.ledger_delete)
        r.add_get("/api/feed", self.feed_info)                 # 20.2 “刷到的”：兴趣表和候选
        r.add_post("/api/feed/remove", self.feed_remove)
        r.add_get("/api/label/batch", self.label_batch)        # 20.0 一屏判 10 条
        r.add_post("/api/label/answers", self.label_answers)
        r.add_get("/api/person/versions", self.person_versions)  # 13.0 画像的历史版本
        r.add_post("/api/person/restore", self.person_restore)
        r.add_post("/api/person/check", self.person_check)          # 16.0 画像抽查：这一项对 / 不对
        r.add_post("/api/lore", self.lore_set)
        r.add_post("/api/knowledge", self.knowledge_set)
        r.add_get("/api/life", self.life)                      # 她的日子
        r.add_post("/api/life/pref", self.life_pref)
        r.add_post("/api/life/plan", self.life_plan)
        r.add_post("/api/life/selfcore", self.life_selfcore)
        r.add_post("/api/life/proposal", self.life_proposal)
        r.add_post("/api/life/share", self.life_share)
        r.add_post("/api/life/birthday", self.life_birthday)
        r.add_post("/api/life/where", self.life_where)
        r.add_get("/api/eval", self.eval_get)                  # 4.3 每周评测
        r.add_post("/api/eval/run", self.eval_run)
        return a

    async def start(self):
        if not self.enabled:
            return
        self.runner = web.AppRunner(self.app(), access_log=None)
        await self.runner.setup()
        await web.TCPSite(self.runner, self.host, self.port).start()
        log.info("%s的小屋：http://%s:%d", self.brain.persona.name, self.host, self.port)

    async def stop(self):
        if self.runner:
            await self.runner.cleanup()

    # ---------------- 小工具 ----------------
    @staticmethod
    async def _json(req) -> dict:
        try:
            d = await req.json()
        except Exception:  # noqa: BLE001
            return {}
        return d if isinstance(d, dict) else {}

    def _q(self, sql: str, args=()) -> list:
        return self.db.conn.execute(sql, args).fetchall()

    def _day_start(self, days_ago: int = 0) -> float:
        now = self.brain.now()
        d = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days_ago)
        return d.timestamp()

    def _hour(self, ts: float) -> int:
        return datetime.fromtimestamp(ts, self.cfg.tz).hour

    def _gid(self, req, d: dict | None = None) -> int:
        v = (d or {}).get("gid") if d else req.query.get("gid")
        try:
            gid = int(v)
        except (TypeError, ValueError):
            raise web.HTTPBadRequest(text="缺少群号")
        if gid not in self.gc.groups:
            raise web.HTTPBadRequest(text="她不在这个群")
        return gid

    def _mentions(self, since: float, gid: int | None = None) -> int:
        """有人找她的次数（@她、回复她、戳她、叫她名字）。"""
        w, a = ("ts>=? AND role='user'", [since]) if gid is None else ("ts>=? AND role='user' AND group_id=?", [since, gid])
        direct = "(content LIKE '@你%' OR content LIKE '[回复你的%' OR content LIKE '[戳了戳你]%')"
        n = self._q(f"SELECT COUNT(*) n FROM qq_group_messages WHERE {w} AND {direct}", a)[0]["n"] or 0
        return n + sum(1 for r in self._q(f"SELECT content FROM qq_group_messages WHERE {w} AND NOT {direct}", a)
                       if self.gc._called(r["content"]))

    def _row_person(self, r) -> dict:
        from aigroup.memory.people import active_time, familiarity
        try:
            prof = json.loads(r["profile"]) if r["profile"] else {}
        except ValueError:
            prof = {}
        try:
            hours = json.loads(r["hours"]) if r["hours"] else []
        except ValueError:
            hours = []
        hours = (list(hours) + [0] * 24)[:24]
        return {"user_id": r["user_id"], "name": r["name"], "note": r["note"], "msgs": r["msgs"],
                "mentions": r["mentions"], "profile": prof, "hours": hours,
                "familiarity": familiarity(r["msgs"], r["mentions"]), "when": active_time(hours),
                "last_seen": r["last_seen"], "updated": r["updated"], "since_update": r["since_update"],
                "owner": int(r["user_id"]) == self.gc.owner_id,
                "aliases": self.gc.split_aliases(r["aliases"] if "aliases" in r.keys() else "")}

    # ---------------- 页面 ----------------
    async def page(self, req):
        return web.Response(text=PAGE.read_text(encoding="utf-8"), content_type="text/html",
                            headers={"Cache-Control": "no-store"})

    async def tokens_css(self, req):
        return web.Response(text=TOKENS.read_text(encoding="utf-8"), content_type="text/css",
                            headers={"Cache-Control": "no-cache"})

    async def avatar(self, req):
        """小满的立绘 / 头像：data/avatar/ 里的图（也是画她自己用的参考图）。"""
        kind = req.match_info["kind"]
        d = Path(self.cfg.root) / "data" / "avatar"
        if kind not in ("portrait", "avatar") or not d.is_dir():
            raise web.HTTPNotFound()
        files = sorted(f for f in d.iterdir() if f.is_file() and f.suffix.lower() in AVATAR_EXT)
        for want in ((kind,) if kind == "portrait" else ("avatar", "portrait")):
            for f in files:
                if want in f.stem.lower():
                    return web.FileResponse(f, headers={"Cache-Control": "max-age=600"})
        if files and kind == "portrait":
            return web.FileResponse(files[0], headers={"Cache-Control": "max-age=600"})
        raise web.HTTPNotFound()

    async def insight(self, req):
        """关系网（谁和谁常聊）和她和每个人相处的经验：只读出每晚整理好的结果，不现算。"""
        gid = self._gid(req)
        g = self.db.get(f"qq_relgraph_{gid}") or {}
        exp = []
        for r in self._q("SELECT k FROM kv WHERE k LIKE ?", (f"qq_relexp_{gid}_%",)):
            d = self.db.get(r["k"]) or {}
            try:
                uid = int(str(r["k"]).rsplit("_", 1)[1])
            except ValueError:
                continue
            if d.get("lines"):
                exp.append({"uid": uid, "name": d.get("name") or str(uid), "lines": list(d["lines"]), "ts": d.get("ts"),
                            "good": d.get("good", 0), "bad": d.get("bad", 0)})
        exp.sort(key=lambda x: -(x["ts"] or 0))
        specs = []
        for r in self._q("SELECT k FROM kv WHERE k LIKE ?", (f"qq_spec_{gid}_%",)):        # 19.1 相处说明书
            d = self.db.get(r["k"]) or {}
            try:
                uid = int(str(r["k"]).rsplit("_", 1)[1])
            except ValueError:
                continue
            if d.get("how"):
                specs.append({"uid": uid, "name": d.get("name") or str(uid), "how": list(d["how"]),
                              "tease_ok": list(d.get("tease_ok") or []), "tease_no": list(d.get("tease_no") or []),
                              "care": d.get("care") or "", "ts": d.get("ts")})
        specs.sort(key=lambda x: -(x["ts"] or 0))
        return web.json_response({"gid": gid, "pairs": g.get("pairs") or [], "pairs_ts": g.get("ts"), "experience": exp,
                                  "specs": specs})

    async def calib_next(self, req):
        from aigroup.eval.calib import next_items, scored
        gid = self._gid(req)
        return web.json_response({"items": next_items(self.db, gid, 5), "n": len(scored(self.db, 1000)),
                                  "calib": self.db.get("judge_calib") or {}})

    async def calib_answer(self, req):
        d = await self._json(req)
        from aigroup.eval.calib import answer, scored
        try:
            ok = answer(self.db, str(d["trace"]), int(d["score"]))
        except (KeyError, TypeError, ValueError):
            return web.json_response({"ok": False, "error": "参数不对"}, status=400)
        return web.json_response({"ok": ok, "n": len(scored(self.db, 1000))})

    async def checks(self, req):
        gid = self._gid(req)
        from aigroup.eval.ablate import NAMES, ablation_stats
        from aigroup.eval.traits import style_z
        nl = [x for x in (self.db.get("numcheck_log") or []) if float(x.get("ts") or 0) > time.time() - 7 * 86400]
        qa, pr = self.db.get("qa_last") or {}, self.db.get("pair_last") or {}
        return web.json_response({
            "calib": self.db.get("judge_calib") or {},
            "qa": {k: v for k, v in qa.items() if k != "answers"} if qa else {},
            "pairs": pr, "style": style_z(self.db, gid), "ablate": ablation_stats(self.db, gid=gid), "names": NAMES,
            "numcheck": {"bad": len(nl), "fixed": sum(1 for x in nl if x.get("fixed")), "recent": nl[-5:]},
            "rate": float(self.gc.gk(gid).get("ablate_rate", 0.1) or 0),
            "vector": self._vector(gid),
            "vector_prev": next((v for v in reversed(self.db.get("vector_hist") or []) if v.get("gid") == gid), {})})

    def _vector(self, gid):
        from aigroup.eval.vector import weekly_vector
        try:
            return weekly_vector(self.gc, gid) if gid is not None else {}
        except Exception as e:  # noqa: BLE001
            return {"error": str(e)[:100]}

    async def rules_info(self, req):
        """21.1 规则：每条命中多少、拦对率、建议；被拦下 / 改写的原句等你判。"""
        from aigroup.chat.rules import KEY_SAMPLES, known_rules, report
        off = list(self.gc.g.get("rules_off") or [])
        sm = [x for x in reversed(self.db.get(KEY_SAMPLES) or []) if x.get("ok") is None][:10]
        return web.json_response({"rules": report(self.db, known_rules()), "off": off, "samples": sm})

    async def rules_judge(self, req):
        d = await self._json(req)
        from aigroup.chat.rules import judge
        try:
            ok = judge(self.db, int(d["id"]), bool(d.get("ok")))
        except (KeyError, TypeError, ValueError):
            return web.json_response({"ok": False, "error": "参数不对"}, status=400)
        return web.json_response({"ok": ok})

    async def rules_toggle(self, req):
        d = await self._json(req)
        rule = str(d.get("rule") or "")[:20]
        if not rule:
            return web.json_response({"ok": False, "error": "参数不对"}, status=400)
        off = [x for x in (self.gc.g.get("rules_off") or []) if x != rule] + ([rule] if d.get("off") else [])
        over = dict(self.db.get("web_overrides") or {})
        over["rules_off"] = off
        self.gc.g["rules_off"] = off
        self.db.set("web_overrides", over)
        return web.json_response({"ok": True, "off": off})

    async def ledger_list(self, req):
        gid = self._gid(req)
        rows = self.gc.ledger().entries(gid, 120) if hasattr(self.gc, "ledger") else []
        return web.json_response({"items": rows})

    async def ledger_delete(self, req):
        d = await self._json(req)
        try:
            self.gc.ledger().delete(int(d["id"]))
        except (KeyError, TypeError, ValueError):
            return web.json_response({"ok": False, "error": "参数不对"}, status=400)
        return web.json_response({"ok": True})

    async def feed_info(self, req):
        gid = self._gid(req)
        gc = self.gc
        if not hasattr(gc, "feed_interests"):
            return web.json_response({"interests": [], "cands": []})
        return web.json_response({"interests": gc.feed_interests(gid), "cands": gc.feed_candidates(gid)[:8],
                                  "last": self.db.get(f"feed_last_{gid}") or 0})

    async def feed_remove(self, req):
        d = await self._json(req)
        gid = self._gid(req, d)
        t = str(d.get("topic") or "").strip()
        if not t:
            return web.json_response({"ok": False, "error": "参数不对"}, status=400)
        self.gc.remove_interest(gid, t)
        return web.json_response({"ok": True})

    async def humane(self, req):
        """19.1 像人的几个指标（这一周）：该说没说、不该说却说、搭话接住、当天重复、没人理还追发。"""
        gid = self._gid(req)
        return web.json_response(self.gc.humane_metrics(gid))

    async def agent_view(self, req):
        """19.0 “查资料”：开没开、今天查了几次、Tavily 这个月用了多少，最近每次查了什么（轮数、秒数、步骤、简报）。"""
        gid = self._gid(req)
        gc = self.gc
        g = gc.gk(gid) if gid else {}
        logs = [x for x in (self.db.get("agent_log") or []) if isinstance(x, dict) and (not gid or x.get("gid") == gid)]
        web_ = getattr(gc, "web", None)
        n_today = int(self.db.get(gc._agent_day_key(gid), 0) or 0) if gid and hasattr(gc, "_agent_day_key") else 0
        return web.json_response({
            "on": bool(gid and hasattr(gc, "agent_on") and gc.agent_on(gid)),
            "enabled": bool(g.get("agent", False)), "today": n_today, "per_day": int(g.get("agent_per_day", 60) or 60),
            "tavily_used": web_.tavily_used() if web_ is not None and hasattr(web_, "tavily_used") else 0,
            "tavily_out": bool(web_ is not None and hasattr(web_, "tavily_out") and web_.tavily_out()),
            "ds": (web_.ds_used() if web_ is not None and getattr(web_, "ds_key", "") else None),
            "reader": gc.toolbox().reader.layers() if hasattr(gc, "toolbox") else [],
            "tools": gc.toolbox().names(recall=True) if hasattr(gc, "toolbox") else [],
            "logs": [{"ts": x.get("ts"), "goal": x.get("goal"), "depth": x.get("depth"), "thinking": x.get("thinking"),
                      "rounds": x.get("rounds"), "seconds": x.get("seconds"), "forced": x.get("forced"),
                      "steps": (x.get("steps") or [])[:12], "brief": x.get("brief") or {}} for x in logs[:30]]})

    async def blind_new(self, req):
        """20.0 盲测：她说的混在群友说的里，你猜哪几句是她。"""
        gid = self._gid(req)
        return web.json_response(self.gc.blind_set(gid))

    async def blind_answer(self, req):
        d = await self._json(req)
        gid = self._gid(req, d)
        try:
            picks = [int(x) for x in d.get("picks") or []]
        except (TypeError, ValueError):
            return web.json_response({"ok": False, "error": "参数不对"}, status=400)
        return web.json_response(self.gc.blind_answer(gid, picks))

    def _wechat_status(self) -> dict:
        try:
            return self.ob.status() if hasattr(self.ob, "status") else {}
        except Exception as e:  # noqa: BLE001
            return {"error": str(e)}

    async def state(self, req):
        b, gc, p = self.brain, self.gc, self.brain.persona
        now = time.time()
        t0 = self._day_start()
        from aigroup.core.schedule import in_quiet
        activity = gc.her_activity()
        if self.ob.ready and not self.nick:
            try:
                self.nick = str((await self.ob.call("get_login_info")).get("nickname") or "")
            except Exception:  # noqa: BLE001
                pass
        groups = []
        for gid in sorted(gc.groups):
            st = gc._st(gid)
            name = st.name
            if not name and self.ob.ready:
                try:
                    name = await gc.group_name(gid)
                except Exception:  # noqa: BLE001
                    name = ""
            agg = self._q("SELECT COUNT(*) n, SUM(role='assistant') her, SUM(ts>=?) today, "
                          "SUM(ts>=? AND role='assistant') her_today, MAX(ts) last, "
                          "COUNT(DISTINCT CASE WHEN role='user' THEN user_id END) people "
                          "FROM qq_group_messages WHERE group_id=?", (t0, t0, gid))[0]
            last = self._q("SELECT name, role, content, ts FROM qq_group_messages WHERE group_id=? "
                           "ORDER BY id DESC LIMIT 1", (gid,))
            n10 = sum(1 for t in st.replies if now - t < 600)
            ge = gc.effect_stats(self._day_start(6), gid)
            her7 = self._q("SELECT COUNT(*) n, AVG(LENGTH(content)) len FROM qq_group_messages "
                           "WHERE group_id=? AND role='assistant' AND ts>=?", (gid, self._day_start(6)))[0]
            all7 = self._q("SELECT COUNT(*) n FROM qq_group_messages WHERE group_id=? AND ts>=?",
                           (gid, self._day_start(6)))[0]["n"] or 0
            groups.append({
                "id": gid, "name": name or str(gid), "total": agg["n"] or 0, "her": agg["her"] or 0,
                "today": agg["today"] or 0, "her_today": agg["her_today"] or 0, "last": agg["last"],
                "people": agg["people"] or 0, "muted": bool(self.db.get(f"qq_mute_{gid}")),
                "replies_10min": n10, "last_reply": st.last_reply_ts or None,
                "night": bool(self.db.get(f"qq_night_{gid}")),
                "last_msg": dict(last[0]) if last else None,
                "notes": self.db.get(f"qq_group_notes_{gid}") or "",
                # 2.2 两个群放一起比：她在哪个群说得多、说完有没有人接、心情、谁找她
                "effect_rate": ge["rate"], "effect_n": ge["n"], "effect_cold": ge["cold"],
                "her_7d": her7["n"] or 0, "all_7d": all7, "her_len": round(her7["len"] or 0, 1),
                "mood": (gc.mood.describe(gid).split("。")[0].replace("你的心情：", "") if gc.mood else ""),
                "mentions_today": self._mentions(t0, gid),
                "active_today": self._q("SELECT COUNT(DISTINCT user_id) n FROM qq_group_messages WHERE group_id=? "
                                        "AND ts>=? AND role='user'", (gid, t0))[0]["n"] or 0,
                "own_knobs": sorted(gc.gover.get(gid) or {}),
            })
        # 今天每个小时：群里说了多少 / 她说了多少
        hours_all, hours_her = [0] * 24, [0] * 24
        hours_by = {g: {"all": [0] * 24, "her": [0] * 24} for g in gc.groups}
        for r in self._q("SELECT ts, role, group_id FROM qq_group_messages WHERE ts>=?", (t0,)):
            h = self._hour(r["ts"])
            hours_all[h] += 1
            hb = hours_by.get(int(r["group_id"]))
            if hb:
                hb["all"][h] += 1
            if r["role"] == "assistant":
                hours_her[h] += 1
                if hb:
                    hb["her"][h] += 1
        # 最近 14 天
        days = []
        for i in range(13, -1, -1):
            a, z = self._day_start(i), self._day_start(i - 1) if i else now + 1
            r = self._q("SELECT COUNT(*) n, SUM(role='assistant') her FROM qq_group_messages WHERE ts>=? AND ts<?",
                        (a, z))[0]
            by = {str(x["group_id"]): [x["n"] or 0, x["her"] or 0] for x in self._q(
                "SELECT group_id, COUNT(*) n, SUM(role='assistant') her FROM qq_group_messages WHERE ts>=? AND ts<? "
                "GROUP BY group_id", (a, z))}
            day = datetime.fromtimestamp(a, self.cfg.tz)
            st = b.llm.stats(f"{day:%Y-%m-%d}") if hasattr(b.llm, "stats") else {}
            days.append({"day": f"{day:%m-%d}", "wd": "一二三四五六日"[day.weekday()], "msgs": r["n"] or 0,
                         "her": r["her"] or 0, "llm": st.get("calls", 0), "by": by})
        people_n = self._q("SELECT COUNT(*) n, SUM(profile!='') prof FROM qq_people")[0]
        active_today = self._q("SELECT COUNT(DISTINCT user_id) n FROM qq_group_messages WHERE ts>=? AND role='user'",
                               (t0,))[0]["n"]
        mentions_today = self._mentions(t0)
        both = self._q("SELECT COUNT(*) n FROM (SELECT user_id FROM qq_group_messages WHERE role='user' "
                       "GROUP BY user_id HAVING COUNT(DISTINCT group_id)>1)")[0]["n"] or 0
        recent_her = [dict(r) for r in self._q(
            "SELECT group_id, content, ts FROM qq_group_messages WHERE role='assistant' ORDER BY id DESC LIMIT 6")]
        llm_today = b.llm.stats() if hasattr(b.llm, "stats") else {}
        eff = gc.effect_stats(self._day_start(6))
        return web.json_response({
            "persona": {"name": p.name, "formal_name": p.formal_name, "age": p.age, "identity": p.identity,
                        "home": p.home, "look": p.look, "catchphrases": p.catchphrases, "birthday": p.birthday,
                        "file": self.cfg.persona_file},
            "now": now, "mood": b.mood, "activity": activity, "busy": gc.busy(activity) if activity else False,
            "quiet": in_quiet(b.now(), self.cfg.get("quiet_hours")), "quiet_hours": self.cfg.get("quiet_hours") or "",
            "active_hours": gc.g.get("active_hours"),
            "plan": self.db.get(f"life_{b.now():%Y-%m-%d}") or "",
            "qq": {"ready": bool(self.ob.ready), "self_id": getattr(self.ob, "self_wxid", "") or self.ob.self_id,
                   "nick": self.nick, "ws": ""},
            "wechat": self._wechat_status(),
            "health": gc.health_issues() if hasattr(gc, "health_issues") else {},
            "send_health": max(((gc.send_health(g) if hasattr(gc, "send_health") else {}) for g in gc.groups),
                               key=lambda d: int(d.get("streak") or 0), default={}),
            "channel": self.cfg.get("channel") or ("wechat" if hasattr(self.ob, "status") else "qq"),
            "owner_id": gc.owner_id, "uptime": now - self.started,
            "groups": groups, "hours": {"all": hours_all, "her": hours_her,
                                         "by": {str(k): v for k, v in hours_by.items()}}, "days": days,
            "kpi": {"today": sum(g["today"] for g in groups), "her_today": sum(g["her_today"] for g in groups),
                    "mentions_today": mentions_today, "active_today": active_today,
                    "people": people_n["n"] or 0, "profiled": people_n["prof"] or 0, "both_groups": both,
                    "llm_calls": llm_today.get("calls", 0), "llm_ok": llm_today.get("ok", 0),
                    "llm_limited": llm_today.get("limited", 0),
                    "effect_rate": eff["rate"], "effect_n": eff["n"], "effect_cold": eff["cold"]},
            "recent_her": recent_her,
        })

    # ---------------- 24.0 今天她做了什么 ----------------
    def _llm_day(self, day: str) -> dict:
        st = self.db.get(f"llm_stats_{day}")
        if not st and hasattr(self.brain.llm, "stats"):
            try:
                st = self.brain.llm.stats(day)
            except Exception:  # noqa: BLE001
                st = None
        return st if isinstance(st, dict) else {}

    @staticmethod
    def _llm_brief(st: dict) -> dict:
        hit, miss = int(st.get("tok_hit", 0) or 0), int(st.get("tok_miss", 0) or 0)
        return {"calls": int(st.get("calls", 0) or 0), "usd": round(float(st.get("usd", 0) or 0), 4),
                "cache_hit": round(hit / (hit + miss), 3) if hit + miss else None,
                "searches": int(st.get("ds_search", 0) or 0)}

    def _engaged(self, gid: int, a: float, z: float) -> dict:
        """她主动开口（没人叫她）的次数，和算完效果的里面被接住（score≥0.6）的比例。"""
        r = self._q("SELECT COUNT(*) n, SUM(done=1) done, SUM(done=1 AND score>=0.6) engaged FROM qq_reply_effect "
                    "WHERE group_id=? AND mentioned=0 AND ts>=? AND ts<?", (gid, a, z))[0]
        done = r["done"] or 0
        return {"n": r["n"] or 0, "done": done, "engaged": r["engaged"] or 0,
                "rate": round((r["engaged"] or 0) / done, 3) if done else None}

    def _effective(self, gid: int) -> list[dict]:
        """每个开关最终生效的值和它从哪来：和 GroupChat 里合并的顺序一样
        （默认 → config.yaml 的 group 段 → 小屋全局 web_overrides → 小屋里这个群单独调的）。"""
        dft = defaults_for(self.cfg.get("channel"))
        conf = chan_group(self.cfg)
        over = self.db.get("web_overrides") or {}
        over = over if isinstance(over, dict) else {}
        gover = self.gc.gover.get(gid) or {}
        eff = self.gc.gk(gid)
        out = []
        for k, (label, *_rest) in KNOBS.items():
            if k in gover:
                src, scope = "小屋", "这个群"
            elif k in over:
                src, scope = "小屋", "全局"
            elif k in conf:
                src, scope = "配置文件", "全局"
            else:
                src, scope = "默认", ""
            v, d = eff.get(k), dft.get(k)
            out.append({"key": k, "label": label, "value": v, "default": d, "source": src, "scope": scope,
                        "changed": json.dumps(v, ensure_ascii=False) != json.dumps(d, ensure_ascii=False)})
        return out

    async def today(self, req):
        gc = self.gc
        if req.query.get("gid"):
            gid = self._gid(req)
        elif gc.groups:
            gid = sorted(gc.groups)[0]
        else:
            raise web.HTTPBadRequest(text="还没配置群")
        now = time.time()
        t0 = self._day_start()
        summary = gc.actions_summary(gid, t0) if hasattr(gc, "actions_summary") else {
            "sent": 0, "failed": 0, "by_kind": {}, "why": {}}
        actions = gc.actions(gid, 0, 30) if hasattr(gc, "actions") else []
        shares_all = [x for x in (self.db.get("qq_shares") or []) if isinstance(x, dict) and x.get("gid") == gid]
        sh_today = [x for x in shares_all if float(x.get("ts") or 0) >= t0]
        by_kind: dict = {}
        for x in sh_today:
            by_kind[x.get("kind") or "?"] = by_kind.get(x.get("kind") or "?", 0) + 1
        idle_ts = float(self.db.get(f"qq_idle_{gid}") or 0) or None
        her_n = self._q("SELECT COUNT(*) n FROM qq_group_messages WHERE group_id=? AND role='assistant' AND ts>=?",
                        (gid, t0))[0]["n"] or 0
        today = {
            "day": f"{self.brain.now():%Y-%m-%d}",
            "dry_run": bool(gc.dry_run(gid)) if hasattr(gc, "dry_run") else False,
            "sends": summary, "actions": actions, "her_msgs": her_n,
            "llm": self._llm_brief(self._llm_day(f"{self.brain.now():%Y-%m-%d}")),
            "active": self._engaged(gid, t0, now + 1),
            "shares": {"n": sum(v for k, v in by_kind.items() if k != "skip"), "by_kind": by_kind,
                       "items": [{"ts": x.get("ts"), "kind": x.get("kind"), "text": x.get("text") or "",
                                  "topic": x.get("topic") or ""} for x in sh_today[-5:]]},
            "idle": {"last": idle_ts, "today": bool(idle_ts and idle_ts >= t0)},
        }
        trend = []
        for i in range(6, -1, -1):
            a, z = self._day_start(i), self._day_start(i - 1) if i else now + 1
            r = self._q("SELECT SUM(role='user') u, SUM(role='assistant') her FROM qq_group_messages "
                        "WHERE group_id=? AND ts>=? AND ts<?", (gid, a, z))[0]
            u, her = r["u"] or 0, r["her"] or 0
            day = datetime.fromtimestamp(a, self.cfg.tz)
            lb = self._llm_brief(self._llm_day(f"{day:%Y-%m-%d}"))
            trend.append({"day": f"{day:%m-%d}", "msgs": u, "her": her,
                          "her_share": round(her / (u + her), 3) if u + her else None,
                          "engaged_rate": self._engaged(gid, a, z)["rate"], "usd": lb["usd"],
                          "cache_hit": lb["cache_hit"], "searches": lb["searches"],
                          "shares": sum(1 for x in shares_all if a <= float(x.get("ts") or 0) < z
                                        and x.get("kind") != "skip")})
        return web.json_response({
            "gid": gid, "today": today, "trend": trend,
            "health": gc.health_issues() if hasattr(gc, "health_issues") else {},
            "effective": self._effective(gid),
        })

    # ---------------- 群聊记录 ----------------
    async def messages(self, req):
        gid = self._gid(req)
        limit = max(10, min(300, int(req.query.get("limit") or 120)))
        before = int(req.query.get("before") or 0)
        q = (req.query.get("q") or "").strip()[:40]
        sql = "SELECT id, ts, user_id, name, role, content FROM qq_group_messages WHERE group_id=?"
        args: list = [gid]
        if before:
            sql += " AND id<?"
            args.append(before)
        if q:
            sql += " AND (content LIKE ? OR name LIKE ?)"
            args += [f"%{q}%", f"%{q}%"]
        rows = [dict(r) for r in self._q(sql + " ORDER BY id DESC LIMIT ?", (*args, limit))]
        rows.reverse()
        st = self.gc._st(gid)
        return web.json_response({"messages": rows, "more": len(rows) == limit,
                                  "typing": bool(st.speaking), "thinking": bool(st.pending and not st.pending.done())})

    # ---------------- 群友 ----------------
    async def people(self, req):
        rows = self._q("SELECT * FROM qq_people WHERE user_id!=? ORDER BY last_seen DESC", (self.ob.self_id or 0,))
        out = [self._row_person(r) for r in rows]
        groups_of: dict[int, list[int]] = {}
        for r in self._q("SELECT DISTINCT user_id, group_id FROM qq_group_messages WHERE role='user'"):
            groups_of.setdefault(int(r["user_id"]), []).append(int(r["group_id"]))
        per: dict[int, dict] = {}                              # 2.2 每个群里她对他的了解（画像按群分开）
        for r in self._q("SELECT user_id, group_id, profile, note, msgs, last_seen, updated FROM qq_profiles"):
            if int(r["group_id"]) not in self.gc.groups:
                continue
            try:
                pr = json.loads(r["profile"]) if r["profile"] else {}
            except ValueError:
                pr = {}
            per.setdefault(int(r["user_id"]), {})[str(r["group_id"])] = {
                "msgs": r["msgs"], "last_seen": r["last_seen"], "updated": r["updated"],
                "note": pr.get("印象") or r["note"], "has": bool(pr),
                "brief": {k: pr[k] for k in ("称呼", "性格", "兴趣", "近况") if pr.get(k)}}
        for x in out:
            x["groups"] = sorted(set(groups_of.get(int(x["user_id"]), [])) | {int(g) for g in per.get(int(x["user_id"]), {})})
            x["per"] = per.get(int(x["user_id"]), {})
        return web.json_response({"people": out, "fields": PROFILE_KEYS,
                                  "profile_every": int(self.gc.g.get("profile_every", 15)),
                                  "checks": self.gc.profile_check_stats() if hasattr(self.gc, "profile_check_stats") else {}})

    async def person(self, req):
        try:
            uid = int(req.query.get("uid") or 0)
        except ValueError:
            raise web.HTTPBadRequest()
        rows = self._q("SELECT * FROM qq_people WHERE user_id=?", (uid,))
        if not rows:
            raise web.HTTPNotFound(text="没有这个人")
        x = self._row_person(rows[0])
        x["recent"] = [dict(r) for r in self._q(
            "SELECT group_id, ts, content FROM qq_group_messages WHERE user_id=? AND role='user' "
            "ORDER BY id DESC LIMIT 40", (uid,))]
        tot = self._q("SELECT COUNT(*) n FROM qq_group_messages WHERE role='user' AND content LIKE ?",
                      ("%@" + x["name"] + "%",))[0]["n"] if x["name"] else 0
        x["at_by_others"] = tot
        x["her_to"] = self._q("SELECT COUNT(*) n FROM qq_group_messages WHERE role='assistant' AND content LIKE ?",
                              ("%@" + x["name"] + "%",))[0]["n"] if x["name"] else 0
        x["per"] = self._person_groups(uid, x["name"])
        x["checks"] = [c for c in (self.db.get("profile_checks") or []) if c.get("uid") == uid]
        return web.json_response(x)

    async def person_check(self, req):
        """16.0 你看画像时给某一项点“对 / 不对”：算准确率；不对的那项下次整理画像时重写。"""
        d = await self._json(req)
        try:
            uid, gid = int(d.get("uid")), int(d.get("gid") or 0)
        except (TypeError, ValueError):
            return web.json_response({"ok": False, "error": "缺 uid"})
        field = str(d.get("field") or "")
        if field not in PROFILE_KEYS:
            return web.json_response({"ok": False, "error": "没有这一项"})
        self.gc.check_profile(uid, gid, field, bool(d.get("ok")))
        return web.json_response({"ok": True, "message": "记下了" if d.get("ok") else "记下了，下次整理他的画像时重写这一项"})

    def _partners(self, gid: int, uid: int, k: int = 5) -> list[dict]:
        """在这个群里他常和谁接话：最近 4000 条里相邻两句、不同的人、3 分钟内，算一次来回。"""
        rows = self._q("SELECT user_id, role, name, ts FROM (SELECT id, user_id, role, name, ts FROM qq_group_messages "
                       "WHERE group_id=? ORDER BY id DESC LIMIT 4000) ORDER BY id", (gid,))
        cnt: dict[int, int] = {}
        names: dict[int, str] = {}
        her = -1
        prev = None
        for r in rows:
            u = her if r["role"] == "assistant" else int(r["user_id"])
            names.setdefault(u, r["name"])
            if prev is not None and prev[0] != u and r["ts"] - prev[1] < 180:
                if u == uid:
                    cnt[prev[0]] = cnt.get(prev[0], 0) + 1
                elif prev[0] == uid:
                    cnt[u] = cnt.get(u, 0) + 1
            prev = (u, r["ts"])
        top = sorted(cnt.items(), key=lambda kv: -kv[1])[:k]
        return [{"uid": u, "name": self.brain.persona.name if u == her else names.get(u, str(u)), "n": n, "her": u == her}
                for u, n in top if n >= 2]

    def _person_groups(self, uid: int, name: str) -> list[dict]:
        out = []
        since = time.time() - 60 * 86400
        for gid in sorted(self.gc.groups):
            r = self._q("SELECT * FROM qq_profiles WHERE user_id=? AND group_id=?", (uid, gid))
            spoke = self._q("SELECT COUNT(*) n FROM qq_group_messages WHERE user_id=? AND group_id=? AND role='user'",
                            (uid, gid))[0]["n"] or 0
            if not r and not spoke:
                continue
            r = r[0] if r else None
            try:
                prof = json.loads(r["profile"]) if r and r["profile"] else {}
            except ValueError:
                prof = {}
            hours = [0] * 24
            for m in self._q("SELECT ts FROM qq_group_messages WHERE user_id=? AND group_id=? AND role='user' AND ts>=?",
                             (uid, gid, since)):
                hours[self._hour(m["ts"])] += 1
            from aigroup.memory.people import active_time
            out.append({
                "gid": gid, "name": self.gc._st(gid).name or str(gid), "profile": prof,
                "note": prof.get("印象") or (r["note"] if r else ""), "msgs": spoke,
                "last_seen": r["last_seen"] if r else None, "updated": r["updated"] if r else None,
                "since_update": r["since_update"] if r else 0, "hours": hours, "when": active_time(hours),
                "fp": self.gc.fingerprint_of(uid, gid) if hasattr(self.gc, "fingerprint_of") else "",
                "partners": self._partners(gid, uid),
                "found_her": self._q("SELECT COUNT(*) n FROM qq_group_messages WHERE user_id=? AND group_id=? "
                                     "AND role='user' AND (content LIKE '@你%' OR content LIKE '[回复你的%' "
                                     "OR content LIKE '[戳了戳你]%')", (uid, gid))[0]["n"] or 0,
                "her_to": self._q("SELECT COUNT(*) n FROM qq_group_messages WHERE group_id=? AND role='assistant' "
                                  "AND content LIKE ?", (gid, "%@" + name + "%"))[0]["n"] if name else 0,
            })
        return out

    async def person_update(self, req):
        d = await self._json(req)
        uid = int(d.get("uid") or 0)
        rows = self._q("SELECT profile, note FROM qq_people WHERE user_id=?", (uid,))
        if not rows:
            return web.json_response({"ok": False, "error": "没有这个人"})
        if "aliases" in d:                                   # 3.0.3：大家还怎么叫他（真名、外号）
            self.gc.set_aliases(uid, d.get("aliases"))
        prof_in = d.get("profile") or {}
        prof = {}
        for k in PROFILE_KEYS:
            v = prof_in.get(k)
            if k == "记得的事":
                items = v if isinstance(v, list) else str(v or "").split("\n")
                items = [str(i).strip()[:MEMORY_CHARS] for i in items if str(i).strip()][:PROFILE_MEMORIES]
                if items:
                    prof[k] = items
            else:
                s = str(v or "").strip()[:PROFILE_CHARS]
                if s:
                    prof[k] = s
        self.db.conn.execute("UPDATE qq_people SET profile=?, note=?, updated=? WHERE user_id=?",
                             (json.dumps(prof, ensure_ascii=False), str(prof.get("印象") or "")[:80], time.time(), uid))
        # 画像按群分开：页面传了 gid 就只改那个群的；"all" = 每个群都改成这份；都没给就用他最近说话的那个群
        gid = d.get("gid")
        if gid == "all":
            gids = [g for g in self.gc.groups if g in self.gc.groups_of(uid)]   # 只改他在的群（别把一个群的事写进另一个群）
        else:
            if not gid:
                last = self._q("SELECT group_id FROM qq_profiles WHERE user_id=? ORDER BY IFNULL(last_seen,0) DESC LIMIT 1",
                               (uid,))
                gid = last[0]["group_id"] if last else None
            gids = [int(gid)] if gid else []
        js = json.dumps(prof, ensure_ascii=False)
        for g in gids:
            self.gc.save_profile_version(uid, int(g), "web")             # 13.0 改之前存一份
            self.db.conn.execute(
                "INSERT INTO qq_profiles(user_id, group_id, profile, note, since_update, updated) VALUES (?,?,?,?,0,?) "
                "ON CONFLICT(user_id, group_id) DO UPDATE SET profile=excluded.profile, note=excluded.note, "
                "updated=excluded.updated", (uid, int(g), js, str(prof.get("印象") or "")[:80], time.time()))
        self.db.conn.commit()
        return web.json_response({"ok": True, "message": "改好了，她下次说话就按这个来"})

    async def person_forget(self, req):
        d = await self._json(req)
        uid = int(d.get("uid") or 0)
        gid = d.get("gid")
        if gid not in (None, "", "all"):                      # 2.2 只忘掉一个群里的
            self.db.conn.execute("UPDATE qq_profiles SET profile='', note='', since_update=0, updated=NULL "
                                 "WHERE user_id=? AND group_id=?", (uid, int(gid)))
            self.db.conn.commit()
            return web.json_response({"ok": True, "message": "这个群里的画像清空了（别的群的还在）"})
        self.db.conn.execute("UPDATE qq_people SET profile='', note='', since_update=0, updated=NULL WHERE user_id=?",
                             (uid,))
        self.db.conn.execute("UPDATE qq_profiles SET profile='', note='', since_update=0, updated=NULL WHERE user_id=?",
                             (uid,))
        self.db.conn.commit()
        return web.json_response({"ok": True, "message": "画像清空了：他再说几句话，她会重新认识他"})

    async def person_refresh(self, req):
        d = await self._json(req)
        uid = int(d.get("uid") or 0)
        rows = self._q("SELECT * FROM qq_people WHERE user_id=?", (uid,))
        if not rows:
            return web.json_response({"ok": False, "error": "没有这个人"})
        if not self._q("SELECT 1 FROM qq_group_messages WHERE user_id=? AND role='user' LIMIT 1", (uid,)):
            return web.json_response({"ok": False, "error": "他还没在群里说过话"})
        prow = self._q("SELECT p.user_id, p.group_id, p.profile, p.note, p.since_update, q.name, q.mentions, q.msgs "
                       "FROM qq_profiles p JOIN qq_people q ON q.user_id=p.user_id WHERE p.user_id=?", (uid,))
        if d.get("gid") not in (None, "", "all"):              # 2.2 只重新整理一个群里的
            prow = [r for r in prow if int(r["group_id"]) == int(d["gid"])]
        n = await self.gc.profile_people(prow) if prow else 0
        if not n:
            return web.json_response({"ok": False, "error": "这次没整理出来（大模型忙或正在整理别人），等会儿再试"})
        x = self._row_person(self._q("SELECT * FROM qq_people WHERE user_id=?", (uid,))[0])
        return web.json_response({"ok": True, "message": "重新整理好了", "person": x})

    # ---------------- 她自己 ----------------
    async def her(self, req):
        b, p = self.brain, self.brain.persona
        plans = []
        for i in range(0, 7):
            day = b.now() - timedelta(days=i)
            t = self.db.get(f"life_{day:%Y-%m-%d}")
            if t:
                plans.append({"day": f"{day:%m-%d}", "wd": "一二三四五六日"[day.weekday()], "today": i == 0, "plan": t})
        return web.json_response({
            "persona": {"name": p.name, "formal_name": p.formal_name, "age": p.age, "identity": p.identity,
                        "home": p.home, "look": p.look, "catchphrases": p.catchphrases, "birthday": p.birthday,
                        "body": p.body, "examples": len(p.examples), "warnings": p.warnings,
                        "file": self.cfg.persona_file, "aliases": sorted(self.gc.aliases)},
            "interests": self.gc.interests(), "self_notes": self.gc.self_notes(min(self.gc.groups)) if self.gc.groups else "",
            "self_notes_by": [{"gid": g, "name": self.gc._st(g).name or str(g), "text": self.gc.self_notes(g)}
                              for g in sorted(self.gc.groups)],
            "plans": plans, "mood": b.mood, "activity": self.gc.her_activity(),
        })

    async def her_interests(self, req):
        d = await self._json(req)
        words = d.get("words")
        if isinstance(words, str):
            words = words.replace("，", ",").replace("、", ",").replace("\n", ",").split(",")
        words = [w.strip()[:12] for w in (words or []) if str(w).strip() and len(str(w).strip()) >= 2]
        self.db.set("qq_interests", list(dict.fromkeys(words))[:40])
        return web.json_response({"ok": True, "message": "存好了：聊到这些她更容易插话"})

    async def her_self_notes(self, req):
        d = await self._json(req)
        gid = int(d.get("gid") or (min(self.gc.groups) if self.gc.groups else 0))   # 只改一个群的（按群分开存）
        if gid in self.gc.groups:
            self.db.set(f"qq_self_notes_{gid}", str(d.get("text") or "").strip()[:1200] or None)
        return web.json_response({"ok": True, "message": "存好了"})

    async def her_replan(self, req):
        b = self.brain
        self.db.set(f"life_{b.now():%Y-%m-%d}", None)
        self.db.set(f"life_{b.now():%Y-%m-%d}_tried", None)            # 手动点“重排”：不管今天失败过几次
        plan = await b.plan_life(b.now())
        return web.json_response({"ok": bool(plan), "plan": plan,
                                  "error": "" if plan else "没排出来（大模型调不通？看日志）"})

    # ---------------- 群 ----------------
    async def group_notes(self, req):
        d = await self._json(req)
        gid = self._gid(req, d)
        self.db.set(f"qq_group_notes_{gid}", str(d.get("text") or "").strip()[:1500] or None)
        return web.json_response({"ok": True, "message": "存好了"})

    async def group_mute(self, req):
        d = await self._json(req)
        gid = self._gid(req, d)
        muted = bool(d.get("muted"))
        self.db.set(f"qq_mute_{gid}", True if muted else None)
        return web.json_response({"ok": True, "muted": muted,
                                  "message": "这个群她先不说话了（消息照样记）" if muted else "她又可以在这个群说话了"})

    async def group_say(self, req):
        """替她在群里说一句（原样发出去，记成她说的）。"""
        d = await self._json(req)
        gid = self._gid(req, d)
        text = str(d.get("text") or "").strip()[:500]
        if not text:
            return web.json_response({"ok": False, "error": "写点什么"})
        if not self.ob.ready:
            return web.json_response({"ok": False, "error": "微信还没接上（微信开着吗？python run.py wxdiag 看看）"})
        from aigroup.onebot import text_seg
        try:
            mid = await self.ob.send_group(gid, [text_seg(text)])
        except Exception as e:  # noqa: BLE001
            return web.json_response({"ok": False, "error": f"没发出去：{e}"})
        self.gc._record(gid, self.ob.self_id or 0, self.brain.persona.name, text, mid, role="assistant")
        st = self.gc._st(gid)
        st.last_reply_ts = st.last_msg_ts = time.time()
        return web.json_response({"ok": True, "message": "发出去了"})

    async def group_nudge(self, req):
        """让她现在在这个群里自己说点什么（会走大模型、照人设说）。"""
        d = await self._json(req)
        gid = self._gid(req, d)
        if not self.ob.ready:
            return web.json_response({"ok": False, "error": "微信还没接上（微信开着吗？python run.py wxdiag 看看）"})
        st = self.gc._st(gid)
        if st.lock.locked():
            return web.json_response({"ok": False, "error": "她正在这个群里打字"})
        topic = str(d.get("topic") or "").strip()[:120]
        why = (f"你想在群里聊聊：{topic}。自然地开个头，一两句话" if topic else
               "你刷到群，想说点什么：可以接着最近的话题，或者分享你今天遇到的事。一两句话，自然一点")
        async with st.lock:
            try:
                ok = await self.gc.speak(gid, mentioned=False, owner_spoke=False, trigger=None, who="", why=why,
                                         must=True)        # 19.1 你在小屋里点的：不受“没人理不追发”“念头分”限制
            except Exception as e:  # noqa: BLE001
                return web.json_response({"ok": False, "error": f"没说出来：{e}"})
        return web.json_response({"ok": bool(ok), "message": "她说了" if ok else "",
                                  "error": "" if ok else "她想了想没说（和最近说过的太像？）"})

    # ---------------- 性格开关 ----------------
    def _knob_gid(self, v) -> int | None:
        """性格开关页：不给 / "all" = 全局；给了群号 = 只调这个群（盖在全局上面）。"""
        if v in (None, "", "all", 0, "0"):
            return None
        try:
            gid = int(v)
        except (TypeError, ValueError):
            raise web.HTTPBadRequest(text="群号不对")
        if gid not in self.gc.groups:
            raise web.HTTPBadRequest(text="她不在这个群")
        return gid

    async def settings_get(self, req):
        from aigroup.chat.settings import GLOBAL_ONLY, MAIN_KNOBS
        gid = self._knob_gid(req.query.get("gid"))
        over = self.db.get("web_overrides") or {}
        base = {**defaults_for(self.cfg.get('channel')), **chan_group(self.cfg)}
        gover = self.gc.gover.get(gid) or {} if gid is not None else {}
        items = []
        for k, (label, kind, lo, hi, step, hint) in KNOBS.items():
            if gid is None:
                items.append({"key": k, "label": label, "kind": kind, "min": lo, "max": hi, "step": step, "hint": hint,
                              "value": self.gc.g.get(k), "base": base.get(k), "changed": k in over,
                              "groups": [g for g in sorted(self.gc.groups) if k in (self.gc.gover.get(g) or {})],
                              "main": k in MAIN_KNOBS})
            else:
                items.append({"key": k, "label": label, "kind": kind, "min": lo, "max": hi, "step": step, "hint": hint,
                              "value": self.gc.gk(gid).get(k), "base": self.gc.g.get(k), "changed": k in gover,
                              "global_only": k in GLOBAL_ONLY, "main": k in MAIN_KNOBS})
        return web.json_response({"items": items, "gid": gid})

    async def settings_set(self, req):
        d = await self._json(req)
        gid = self._knob_gid(d.get("gid"))
        if gid is not None:
            return self._settings_set_group(gid, d)
        over = dict(self.db.get("web_overrides") or {})
        if d.get("reset"):
            base = {**defaults_for(self.cfg.get('channel')), **chan_group(self.cfg)}
            keys = [d["reset"]] if isinstance(d["reset"], str) else list(over)
            for k in keys:
                over.pop(k, None)
                if k in base:
                    self.gc.g[k] = base[k]
        for k, v in (d.get("values") or {}).items():
            cv = clean_knob(k, v)
            if cv is None:
                continue
            over[k] = cv
            self.gc.g[k] = cv
        self.db.set("web_overrides", over or None)
        return web.json_response({"ok": True, "message": "马上生效了（存在她的数据里，重启也还在）"})

    def _settings_set_group(self, gid: int, d: dict):
        from aigroup.chat.settings import GLOBAL_ONLY, group_key
        over = dict(self.gc.gover.get(gid) or {})
        if d.get("reset"):
            for k in ([d["reset"]] if isinstance(d["reset"], str) else list(over)):
                over.pop(k, None)
        for k, v in (d.get("values") or {}).items():
            if k in GLOBAL_ONLY:
                continue
            cv = clean_knob(k, v)
            if cv is not None:
                over[k] = cv
        if over:
            self.gc.gover[gid] = over
        else:
            self.gc.gover.pop(gid, None)
        self.db.set(group_key(gid), over or None)
        return web.json_response({"ok": True, "message": "只对这个群生效（别的群还是用全局的）"})

    # ---------------- 学到的（表达方式、黑话） ----------------
    async def learned(self, req):
        gid = self._gid(req)
        ex = [dict(r) for r in self._q(
            "SELECT id, situation, style, count, status, updated FROM qq_expressions WHERE group_id=? "
            "ORDER BY (status='no'), count DESC, updated DESC LIMIT 300", (gid,))]
        used = {e["id"] for e in self.gc.learner.expressions_for(gid, 1000, 0)}
        for e in ex:
            e["used"] = e["id"] in used
        jg = []
        for r in self._q("SELECT * FROM qq_jargon WHERE group_id=? ORDER BY (status='no'), count DESC LIMIT 300", (gid,)):
            x = dict(r)
            x["examples"] = json.loads(x["examples"] or "[]")
            x["used"] = x["status"] != "no" and bool(x["meaning"]) and (x["is_jargon"] == 1 or x["status"] == "ok")
            jg.append(x)
        st = []
        for r in self._q("SELECT id, key, path, seen, users, descr, emotion, usage, used, status, groups FROM qq_stickers "
                         "WHERE path!='' ORDER BY (status='no'), seen DESC LIMIT 80"):
            x = dict(r)
            if gid not in json.loads(x.pop("groups") or "[]"):
                continue
            x["users"] = len(json.loads(x["users"] or "[]"))
            st.append(x)
        names = self.gc.names(gid)
        facts = []
        for f in self.gc.memory.facts(gid, limit=60, valid_only=False):
            f["about"] = ("她自己" if f["subject_id"] == -1 else "群" if not f["subject_id"]
                          else names.get(str(f["subject_id"]), str(f["subject_id"])))
            facts.append(f)
        daily = []
        for i in range(1, 8):
            day = self.brain.now() - timedelta(days=i)
            rep = self.db.get(f"qq_daily_{gid}_{day:%Y-%m-%d}")
            if rep:
                daily.append(rep)
        lore = self.gc.lore.entries(gid) if self.gc.lore is not None else []
        return web.json_response({"expressions": ex, "jargon": jg, "learn": bool(self.gc.gk(gid).get("learn", True)),
                                  "lore": lore,
                                  "knowledge": self.gc.knowledge.entries(gid, 100) if getattr(self.gc, "knowledge", None) else [],
                                  "stickers": st, "facts": facts, "daily": daily,
                                  "norms": self.db.get(f"qq_group_norms_{gid}") or ""})

    async def sticker_set(self, req):
        d = await self._json(req)
        try:
            sid = int(d.get("id"))
        except (TypeError, ValueError):
            return web.json_response({"ok": False, "error": "参数不对"}, status=400)
        if d.get("status") in ("new", "ok", "no"):
            self.db.conn.execute("UPDATE qq_stickers SET status=? WHERE id=?", (d["status"], sid))
        for f in ("emotion", "usage"):
            if f in d:
                self.db.conn.execute(f"UPDATE qq_stickers SET {f}=? WHERE id=?", (str(d[f] or "").strip()[:30], sid))
        self.db.conn.commit()
        return web.json_response({"ok": True, "message": "改好了"})

    async def sticker_file(self, req):
        try:
            sid = int(req.query.get("id") or 0)
        except ValueError:
            raise web.HTTPBadRequest()
        rows = self._q("SELECT path FROM qq_stickers WHERE id=?", (sid,))
        if not rows or not rows[0]["path"]:
            raise web.HTTPNotFound()
        p = self.gc.stickers.dir / Path(rows[0]["path"]).name
        if not p.exists():
            raise web.HTTPNotFound()
        return web.FileResponse(p, headers={"Cache-Control": "max-age=86400"})

    async def fact_set(self, req):
        """记的事：标成过时 / 删掉。"""
        d = await self._json(req)
        try:
            fid = int(d.get("id"))
        except (TypeError, ValueError):
            return web.json_response({"ok": False, "error": "参数不对"}, status=400)
        if d.get("delete"):
            mem = getattr(self.gc, "memory", None)
            if mem is not None and hasattr(mem, "forget_fact"):
                mem.forget_fact(fid)                       # 24.0 留指纹：以后整理时不会再学回来
            else:
                self.db.conn.execute("DELETE FROM qq_facts WHERE id=?", (fid,))
            msg = "删掉了（以后也不会再记回来）"
        elif d.get("expire"):
            self.db.conn.execute("UPDATE qq_facts SET t_invalid=? WHERE id=?", (time.time(), fid))
            msg = "标成过时了：她不会再当现在的事提"
        else:
            self.db.conn.execute("UPDATE qq_facts SET t_invalid=NULL WHERE id=?", (fid,))
            msg = "恢复了"
        self.db.conn.commit()
        return web.json_response({"ok": True, "message": msg})

    async def group_norms(self, req):
        d = await self._json(req)
        gid = self._gid(req, d)
        self.db.set(f"qq_group_norms_{gid}", str(d.get("text") or "").strip()[:800] or None)
        return web.json_response({"ok": True, "message": "存好了"})

    async def learned_set(self, req):
        d = await self._json(req)
        kind = d.get("kind")
        table = {"expression": "qq_expressions", "jargon": "qq_jargon"}.get(kind)
        try:
            rid = int(d.get("id"))
        except (TypeError, ValueError):
            rid = 0
        if not table or not rid:
            return web.json_response({"ok": False, "error": "参数不对"}, status=400)
        if d.get("delete"):
            self.db.conn.execute(f"DELETE FROM {table} WHERE id=?", (rid,))
            self.db.conn.commit()
            return web.json_response({"ok": True, "message": "删掉了"})
        sets, args = [], []
        if d.get("status") in ("new", "ok", "no"):
            sets.append("status=?")
            args.append(d["status"])
        fields = {"expression": ("situation", "style"), "jargon": ("meaning",)}[kind]
        for f in fields:
            if f in d:
                v = str(d.get(f) or "").strip()[:40]
                if not v:
                    return web.json_response({"ok": False, "error": "不能是空的"})
                sets.append(f"{f}=?")
                args.append(v)
        if kind == "jargon" and d.get("status") == "ok":
            sets.append("is_jargon=1")
        if not sets:
            return web.json_response({"ok": False, "error": "没有要改的"})
        try:
            self.db.conn.execute(f"UPDATE {table} SET {', '.join(sets)}, updated=? WHERE id=?", (*args, time.time(), rid))
            self.db.conn.commit()
        except Exception as e:  # noqa: BLE001
            return web.json_response({"ok": False, "error": f"没改成：{e}"})
        msg = {"ok": "她会用这条", "no": "她不会再用这条", "new": "交给她自己判断"}.get(d.get("status"), "改好了")
        return web.json_response({"ok": True, "message": msg})

    async def gate_info(self, req):
        """发言判断：最近 1 小时的概率、阈值，她正在注意谁、精力、退避。"""
        gid = self._gid(req)
        from aigroup.chat.gate import REFIT_MIN, labeled_rows, talk_factor, threshold
        from aigroup.eval.replay import pair_stats
        gc = self.gc
        st = gc._st(gid)
        probs, spoke = gc.gate.window(gid)
        g = gc.gk(gid)
        factor = talk_factor(g.get("talk_curve"), f"{self.brain.now():%H:%M}")
        thr, keep = threshold(probs, min(1.0, float(g.get("talkativeness", 0.3)) * factor), spoke)
        names = gc.names(gid)
        recent = [dict(r) for r in self._q(
            "SELECT ts, p, threshold, passed, spoke, llm_skip, mentioned FROM qq_gate_log WHERE group_id=? "
            "ORDER BY id DESC LIMIT 30", (gid,))]
        return web.json_response({
            "threshold": thr, "keep": keep, "samples": len(probs), "spoke": spoke,
            "talkativeness": g.get("talkativeness"), "energy": round(st.energy.get(), 3),
            "backoff": round(st.backoff.waiting(), 1), "skips": st.backoff.n,
            "attention": [{"uid": u, "name": names.get(str(u), str(u)), "v": round(v, 3)} for u, v in st.attention.top()],
            "recent": recent, "factor": factor, "weights": gc.weights_src, "labeled": len(labeled_rows(self.db)),
            "refit_min": REFIT_MIN, "ignored": gc.ignored_count(gid),
            "wait": ({"name": names.get(str(st.wait["uid"]), str(st.wait["uid"])),
                      "left": round(st.wait["until"] - time.time())} if st.wait and st.wait["until"] > time.time() else None),
            "trace": gc.tracer.stats(self._day_start(6)),
            "mood": gc.mood.describe(gid).split("。")[0].replace("你的心情：", "") if gc.mood else "",
            "latency": {k: v for k, v in gc.latency_stats(gid).items() if k in ("n", "mean", "median")},
            "llm": {k: v for k, v in ((self.brain.llm.stats() if hasattr(self.brain.llm, "stats") else {}) or {}).items() if k in ("calls", "tok_hit", "tok_miss", "tok_out", "usd")},
            "detector": self.db.get("ai_detector_info") or {}, "calibration": {
                k: v for k, v in (self.db.get("quality_calibration") or {}).items() if k in ("n", "hard", "hard_rate")},
            "judge": self.db.get("judge_last") or {}, "refit": self.db.get("gate_refit_last") or {},
            "pairs": pair_stats(self.db)})

    # ---------------- 二选一 ----------------
    async def pairs_next(self, req):
        gid = self._gid(req)
        from aigroup.eval.replay import next_pair, pair_stats
        p = next_pair(self.db, gid)
        return web.json_response({"pair": p, "stats": pair_stats(self.db)})

    async def pairs_answer(self, req):
        d = await self._json(req)
        from aigroup.eval.replay import answer_pair, pair_stats
        try:
            answer_pair(self.db, str(d["trace"]), list(d.get("a") or []), list(d.get("b") or []), int(d.get("sent", -1)),
                        int(d["choice"]))
        except (KeyError, TypeError, ValueError):
            return web.json_response({"ok": False, "error": "参数不对"}, status=400)
        return web.json_response({"ok": True, "stats": pair_stats(self.db)})

    async def spot_get(self, req):
        gid = self._gid(req)
        if not hasattr(self.gc, "spot_items") or not self.gc.gk(gid).get("spot_check", True):
            return web.json_response({"says": [], "memes": [], "stats": {}, "off": True})
        return web.json_response(self.gc.spot_items(gid))

    async def spot_post(self, req):
        d = await self._json(req)
        try:
            ok = self.gc.spot_answer(self._gid(req, d), str(d["kind"]), int(d["ref"]),
                                     int(d["ok"]), str(d.get("note") or ""))
        except (KeyError, TypeError, ValueError):
            ok = False
        return web.json_response({"ok": ok}, status=200 if ok else 400)

    async def label_next(self, req):
        from aigroup.eval.label import next_moment, stats
        return web.json_response({"moment": next_moment(self.db, self._gid(req), self.gc.cfg.tz), "stats": stats(self.db)})

    async def label_answer(self, req):
        d = await self._json(req)
        from aigroup.eval.label import answer, stats
        try:
            lab = d.get("label")
            answer(self.db, int(d["id"]), None if lab in (None, "", "skip") else int(lab), str(d.get("how") or ""),
                   str(d.get("why0") or ""))
        except (KeyError, TypeError, ValueError):
            return web.json_response({"ok": False, "error": "参数不对"}, status=400)
        return web.json_response({"ok": True, "stats": stats(self.db)})

    async def label_batch(self, req):
        from aigroup.eval.label import batch, stats
        try:
            n = max(1, min(20, int(req.query.get("n") or 10)))
        except ValueError:
            n = 10
        return web.json_response({"items": batch(self.db, self._gid(req), self.gc.cfg.tz, n), "stats": stats(self.db)})

    async def label_answers(self, req):
        d = await self._json(req)
        from aigroup.eval.label import answer, stats
        done = 0
        try:
            for it in list(d.get("items") or [])[:40]:
                lab = it.get("label")
                answer(self.db, int(it["id"]), None if lab in (None, "", "skip") else int(lab), str(it.get("how") or ""),
                       str(it.get("why0") or ""))
                done += 1
        except (KeyError, TypeError, ValueError, AttributeError):
            return web.json_response({"ok": False, "error": "参数不对", "done": done}, status=400)
        return web.json_response({"ok": True, "done": done, "stats": stats(self.db)})

    async def person_versions(self, req):
        from aigroup.memory.style import profile_diff
        uid, gid = int(req.query.get("uid") or 0), int(req.query.get("gid") or 0)
        cur = self.gc.person(uid, gid) or {}
        now = dict(cur.get("profile") or {})
        out = []
        for v in self.gc.profile_versions(uid, gid):
            out.append({**v, "diff": profile_diff(v["profile"], now)})      # 从这一版到现在改了什么
            now = v["profile"]
        return web.json_response({"versions": out})

    async def person_restore(self, req):
        d = await self._json(req)
        try:
            uid, gid, vid = int(d["uid"]), int(d["gid"]), int(d["id"])
        except (KeyError, TypeError, ValueError):
            return web.json_response({"ok": False, "error": "参数不对"}, status=400)
        r = self._q("SELECT profile FROM qq_profile_versions WHERE id=? AND user_id=? AND group_id=?", (vid, uid, gid))
        if not r:
            return web.json_response({"ok": False, "error": "没有这一版"})
        self.gc._set_profile(uid, gid, json.loads(r[0]["profile"]), "web")
        self.db.conn.commit()
        return web.json_response({"ok": True, "message": "退回去了（现在这份也存了一版，想反悔还能再退）"})

    # ---------------- 群梗词条 ----------------
    async def knowledge_set(self, req):
        """她知道的事：删掉不对的、改一改、手动加一条“她该知道的事”。"""
        d = await self._json(req)
        kn = getattr(self.gc, "knowledge", None)
        if kn is None:
            return web.json_response({"ok": False, "error": "没有这一条"})
        split = lambda v: [k.strip() for k in str(v or "").replace("，", ",").replace("、", ",").split(",") if k.strip()]  # noqa: E731
        if d.get("add"):
            gid = self._gid(req, d)
            kid = kn.add(gid, str(d.get("topic") or ""), split(d.get("keys")), str(d.get("summary") or ""), "manual")
            return web.json_response({"ok": bool(kid), "message": "记下了：聊到关键词她会想起来" if kid else "",
                                      "error": "" if kid else "话题和内容都要写"})
        if not d.get("id"):
            return web.json_response({"ok": False, "error": "没有这一条"})
        if d.get("summary") is not None or d.get("topic") is not None:
            kn.update(int(d["id"]), summary=d.get("summary"), topic=d.get("topic"),
                      keys=split(d["keys"]) if d.get("keys") is not None else None, source="manual")
            return web.json_response({"ok": True, "message": "改好了"})
        kn.delete(int(d["id"]))
        return web.json_response({"ok": True, "message": "删掉了，她不再记得这个"})

    # ---------------- 她的日子 ----------------
    async def life(self, req):
        gid = int(req.query.get("gid") or 0) or None
        if gid is not None and gid not in self.gc.groups:
            gid = None
        wh = getattr(self.gc, "where", None)
        if wh is not None:
            try:
                await wh.prefetch([int(x["user_id"]) for x in wh.all()][:12])
            except Exception:  # noqa: BLE001
                pass
        snap = self.gc.life_snapshot(gid)
        snap["names"] = {str(g): self.gc._st(g).name or str(g) for g in sorted(self.gc.groups)}
        return web.json_response(snap)

    async def life_pref(self, req):
        d = await self._json(req)
        topic = str(d.get("topic") or "").strip()
        if not topic:
            return web.json_response({"ok": False, "error": "话题要写"})
        if d.get("delete"):
            prefs = self.gc.prefs()
            prefs.pop(topic, None)
            self.db.set("qq_prefs", prefs or None)
            return web.json_response({"ok": True, "message": "删掉了"})
        ok = self.gc.add_pref(topic, str(d.get("v") or ""), force=True)
        return web.json_response({"ok": ok, "message": "存好了：她以后都这么说" if ok else "", "error": "" if ok else "态度要写"})

    async def life_plan(self, req):
        d = await self._json(req)
        if d.get("id") and d.get("when"):                   # 19.0 改时间（还没提醒的）
            msg = self.gc.plan_reschedule(int(d["id"]), str(d["when"]))
            return web.json_response({"ok": not msg, "message": "改好了" if not msg else "", "error": msg})
        if d.get("id"):
            st = str(d.get("status") or "skip")
            if st not in ("open", "done", "skip", "cancelled"):
                return web.json_response({"ok": False, "error": "状态不对"})
            self.gc.set_plan(int(d["id"]), st)
            return web.json_response({"ok": True, "message": "改好了"})
        gid = self._gid(req, d)
        pid = self.gc.add_plan(gid, 0, str(d.get("who") or "大家"), str(d.get("what") or "").strip()[:40],
                               str(d.get("when") or ""), "remind" if d.get("kind") == "remind" else "ask")
        return web.json_response({"ok": bool(pid), "message": "记下了" if pid else "",
                                  "error": "" if pid else "时间写成 2026-10-24 20:00 这样（要在之后 45 天内），事情要写"})

    async def life_selfcore(self, req):
        d = await self._json(req)
        gid = self._gid(req, d)
        self.db.set(f"qq_selfcore_{gid}", str(d.get("text") or "").strip()[:500] or None)
        return web.json_response({"ok": True, "message": "存好了"})

    async def life_proposal(self, req):
        """14.0 她提的性格调整建议：采纳 / 不采纳（采纳只是记一笔，人格文件要人去改）。"""
        d = await self._json(req)
        out = list(self.db.get("persona_proposals") or [])
        hit = None
        for x in out:
            if x.get("id") == str(d.get("id")):
                x["status"] = "accepted" if d.get("accept") else "rejected"
                hit = x
        self.db.set("persona_proposals", out)
        if not hit:
            return web.json_response({"ok": False, "error": "找不到这条建议"})
        if hit.get("kind") == "signature":
            msg = (f"记下了：去人格文件里改写签名 {hit['facet']} 的 do / not（或者收窄触发词），重启后生效"
                   if d.get("accept") else "好，不改")
        else:
            msg = (f"记下了：去人格文件里把 {hit['facet']}（{hit['name']}）改成 {hit['to']}，重启后生效"
                   if d.get("accept") else "好，不改")
        return web.json_response({"ok": True, "message": msg})

    async def eval_get(self, req):
        return web.json_response(self.gc.eval_snapshot())

    async def eval_run(self, req):
        """小屋里点“现在评一次 / 和上一版比一比”：后台跑，跑完刷新就能看到。"""
        d = await self._json(req)
        gc = self.gc
        if getattr(gc, "_eval_running", False):
            return web.json_response({"ok": False, "error": "正在评，等一两分钟"})

        async def go():
            gc._eval_running = True
            try:
                if d.get("kind") == "compare":
                    await gc.compare_persona()
                elif d.get("kind") == "probe":
                    await gc.run_probes()
                else:
                    await gc.weekly_eval(force=True)
            except Exception as e:  # noqa: BLE001
                import logging
                logging.getLogger("aigroup.eval").info("评测出错：%s", e)
            finally:
                gc._eval_running = False
        gc._spawn(go())
        return web.json_response({"ok": True, "message": "开始评了，一两分钟后刷新看看"})

    async def life_where(self, req):
        d = await self._json(req)
        wh = getattr(self.gc, "where", None)
        try:
            uid = int(d.get("uid"))
        except (TypeError, ValueError):
            return web.json_response({"ok": False, "error": "选一个人"})
        if wh is None:
            return web.json_response({"ok": False, "error": "没开"})
        if d.get("delete"):
            wh.delete(uid, d.get("kind") or None)
            return web.json_response({"ok": True, "message": "删掉了"})
        got = await wh.set(uid, str(d.get("city") or ""), str(d.get("why") or ""), str(d.get("until") or ""), "manual")
        return web.json_response({"ok": bool(got), "message": f"记下了：{got['name']}（{got['admin']}）" if got else "",
                                  "error": "" if got else "没找到这个城市（写城市名，比如 珠海；国外的写英文名）"})

    async def life_birthday(self, req):
        d = await self._json(req)
        try:
            uid = int(d.get("uid"))
        except (TypeError, ValueError):
            return web.json_response({"ok": False, "error": "没有这个人"})
        ok = self.gc.set_birthday(uid, str(d.get("day") or ""))
        return web.json_response({"ok": ok, "message": ("记下了，那天她会在群里祝他" if d.get("day") else "删掉了") if ok else "",
                                  "error": "" if ok else "写成 月-日，比如 05-21"})

    async def life_share(self, req):
        """小屋里点“现在分享一次”：试试效果（照片要配了画图接口）。"""
        d = await self._json(req)
        gid = self._gid(req, d)
        if not getattr(self.ob, "ready", False):
            return web.json_response({"ok": False, "error": "还没连上，发不出去"})
        st = self.gc._st(gid)
        ok = await self.gc.share(gid, st, force_photo=bool(d.get("photo")))
        why = getattr(self.gc, "share_error", "")
        return web.json_response({"ok": ok, "message": ("发了一张照片，去群里看看" if d.get("photo") else "分享了一次，去群里看看") if ok else "",
                                  "error": "" if ok else (why or ("照片没发出去，看看日志" if d.get("photo")
                                                                  else "这次没刷到值得分享的，看看日志"))})

    async def lore_set(self, req):
        d = await self._json(req)
        lore = self.gc.lore
        if lore is None:
            return web.json_response({"ok": False, "error": "“群梗词条”在性格开关里关着"})
        if d.get("delete"):
            row = self.db.conn.execute("SELECT group_id, keys, content FROM qq_lore WHERE id=?", (int(d["id"]),)).fetchone()
            if row:                                         # 24.0 留指纹：每晚自动加梗时不会再加回来
                from aigroup.memory.facts import remember_forgotten
                try:
                    keys = json.loads(row["keys"] or "[]")
                except ValueError:
                    keys = []
                remember_forgotten(self.db, int(row["group_id"]), "lore", row["content"], keys)
            self.db.conn.execute("DELETE FROM qq_lore WHERE id=?", (int(d["id"]),))
            self.db.conn.commit()
            return web.json_response({"ok": True, "message": "删掉了（以后也不会自动加回来）"})
        if d.get("id"):
            if d.get("status") in ("new", "ok", "no"):
                self.db.conn.execute("UPDATE qq_lore SET status=? WHERE id=?", (d["status"], int(d["id"])))
                self.db.conn.commit()
            return web.json_response({"ok": True, "message": "改好了"})
        gid = self._gid(req, d)
        keys = [k for k in str(d.get("keys") or "").replace("，", ",").replace("、", ",").split(",") if k.strip()]
        rid = lore.add(gid, keys, str(d.get("content") or ""), cooldown=int(d.get("cooldown") or 20))
        return web.json_response({"ok": bool(rid), "message": "加好了" if rid else "",
                                  "error": "" if rid else "关键词和内容都要写"})

    # ---------------- 日志 ----------------
    async def logs(self, req):
        n = max(20, min(1000, int(req.query.get("n") or 300)))
        p = Path(self.cfg.root) / "data" / "logs" / "group.log"
        if not p.exists():
            return web.json_response({"lines": []})
        with open(p, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 300_000))
            data = f.read().decode("utf-8", errors="replace")
        return web.json_response({"lines": data.splitlines()[-n:]})

    # ---------------- 大模型 ----------------
    async def llm_list(self, req):
        from aigroup.llm.custom import load_custom
        custom = ((load_custom(self.cfg.root).get("llm") or {}).get("profiles") or {})
        llm = self.cfg.get("llm") or {}
        profs = [{"name": n, "model": p.get("model"), "base_url": p.get("base_url"), "custom": n in custom}
                 for n, p in (llm.get("profiles") or {}).items()]
        for n, p in custom.items():
            if not any(x["name"] == n for x in profs):
                profs.append({"name": n, "model": p.get("model"), "base_url": p.get("base_url"), "custom": True,
                              "pending": True})
        return web.json_response({"profiles": profs, "routes": dict(llm.get("routes") or {}),
                                  "stats": self.brain.llm.stats() if hasattr(self.brain.llm, "stats") else {}})

    async def llm_models(self, req):
        from aigroup.llm.custom import chat_models, list_models
        d = await self._json(req)
        models, url, via_proxy, err = await list_models(str(d.get("url") or ""), str(d.get("key") or ""),
                                                        str(self.cfg.get("proxy") or ""))
        return web.json_response({"ok": bool(models), "url": url, "via_proxy": via_proxy, "error": err,
                                  "models": chat_models(models) or models})

    async def llm_test(self, req):
        from aigroup.llm.custom import probe_model
        d = await self._json(req)
        ok, secs, text, extra = await probe_model(str(d.get("url") or ""), str(d.get("key") or ""),
                                                  str(d.get("model") or ""), str(self.cfg.get("proxy") or ""),
                                                  bool(d.get("via_proxy")))
        return web.json_response({"ok": ok, "secs": round(secs, 1), "text": text, "extra": extra})

    async def llm_add(self, req):
        from aigroup.llm.custom import save_profile
        d = await self._json(req)
        if not d.get("url") or not d.get("model"):
            return web.json_response({"ok": False, "error": "地址和模型都要有"})
        name, routes = save_profile(self.cfg, url=str(d["url"]), key=str(d.get("key") or ""), model=str(d["model"]),
                                    name=str(d.get("name") or ""), how=str(d.get("how") or "first"),
                                    roles_bg=("helper", "memory", "night"), via_proxy=bool(d.get("via_proxy")),
                                    extra=d.get("extra") or None, root=self.cfg.root)
        return web.json_response({"ok": True, "name": name, "routes": routes,
                                  "message": f"加好了：{name}。点“重启生效”或者下次启动时生效"})

    async def llm_remove(self, req):
        from aigroup.llm.custom import remove_profile
        d = await self._json(req)
        ok = remove_profile(str(d.get("name") or ""), self.cfg.root)
        return web.json_response({"ok": ok, "message": "删掉了，重启后生效" if ok else "只能删自己加的接口"})

    async def restart(self, req):
        """和托盘里的“重启”一样：往 data/tray_cmd 写 restart，程序收尾后由托盘 / scripts/start_console.bat 重新打开。"""
        try:
            (Path(self.cfg.root) / "data" / "tray_cmd").write_text("restart", encoding="utf-8")
        except OSError as e:
            return web.json_response({"ok": False, "error": str(e)})
        return web.json_response({"ok": True, "message": "正在重启，十几秒后刷新页面"})
