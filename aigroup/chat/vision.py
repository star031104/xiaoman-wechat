"""38.2 看图 2.0：拿到原图、结构化地记、带上下文看、被问到再细看、聊天截图分清谁说的。

- _see：看一张图 → {status, line, info, ...}。status：ok（原图）/ thumb（只有缩略图）/ missing（图还没加载出来）/ fail（看图接口没回）。
  先 OCR（字和位置）→ 聊天截图按气泡分左右（net/ocr.chat_layout）→ 看图模型按 JSON 写（kind / gist / sure / unsure /
  text / speakers / people / sensitive / ask_ok）→ 清洗：图上的字只留和 OCR 对得上的、名字只留图上写着的、去掉占位话。
  消息里只放一行（[图片：类型：一句话「原字」]），细节存 qq_images，提示词里的“刚有人发了图”再展开。
- 补原图：缩略图 / 没加载出来的图，按 10 秒、30 秒、2 分钟、10 分钟再去拿；拿到原图就重看一遍，把消息里的描述换掉。
  她不在时补进来的图（catch_up）以前不看，只剩一个 [图片]：现在补完消息后在后台看最近几张。
- image_zoom：有人回复这张图、或者图发出来 3 句内问“这是哪 / 谁 / 啥 / 多少 / 真的假的 / 你看看”：拿原图 + 问题 + 前后聊天
  再看一次，字小的按 OCR 框裁出来放大一起给；最多等 15 秒，没看完先让她说“我放大看看”，看完再接。
- 发前检查：_img_unseen（引了图上 OCR 认的、看图没对上的字）、_img_sensitive（别人家里人 / 工作文件 / 证件这种图去点评细节）。
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import logging
import re
import time
from difflib import SequenceMatcher

log = logging.getLogger("aigroup.group")
tlog = logging.getLogger("aigroup.text.group")

SCHEMA = """
CREATE TABLE IF NOT EXISTS qq_images (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    group_id INTEGER NOT NULL,
    msg_row INTEGER,                         -- qq_group_messages.id
    url TEXT NOT NULL DEFAULT '',
    who TEXT NOT NULL DEFAULT '',
    hash TEXT NOT NULL DEFAULT '',           -- 图片内容的 md5 前 16 位（同一张图只看一次）
    status TEXT NOT NULL DEFAULT '',         -- ok / thumb / missing / fail
    kind TEXT NOT NULL DEFAULT '',
    gist TEXT NOT NULL DEFAULT '',
    line TEXT NOT NULL DEFAULT '',           -- 写进消息里的那一行
    ocr TEXT NOT NULL DEFAULT '',
    layout TEXT NOT NULL DEFAULT '',         -- 聊天截图按气泡分的谁说了什么（JSON）
    info TEXT NOT NULL DEFAULT '{}',         -- 清洗后的结构化结果（JSON）
    model TEXT NOT NULL DEFAULT '',
    tries INTEGER NOT NULL DEFAULT 0,        -- 补原图试了几次
    recovered INTEGER NOT NULL DEFAULT 0,    -- 1 = 一开始是缩略图 / 没加载出来，后来补到了原图
    deep TEXT NOT NULL DEFAULT '[]',         -- 被问到后细看的结果 [{q, a, sure, ts}]
    updated REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_qq_images_msg ON qq_images(group_id, msg_row);
CREATE INDEX IF NOT EXISTS idx_qq_images_hash ON qq_images(hash);
"""

KINDS = ("照片", "聊天截图", "网页截图", "游戏截图", "梗图", "表情包", "图表", "文档", "商品", "其他")
KIND_ALIAS = {"截图": "网页截图", "手机截图": "网页截图", "软件截图": "网页截图", "界面截图": "网页截图",
              "app截图": "网页截图", "社交平台截图": "网页截图", "帖子截图": "网页截图", "群聊截图": "聊天截图",
              "聊天记录": "聊天截图", "k线图": "图表", "K线图": "图表", "表格": "图表", "海报": "其他",
              "风景": "照片", "自拍": "照片", "实拍": "照片", "表情": "表情包", "梗": "梗图", "文件": "文档"}
FILLER_RX = re.compile(r"(看不出(具体)?(是)?(干嘛|在说|用途|什么|谁)[^，。；]*|(图里|图中)?(没有|看不清)(能看清的|可辨认的|清晰的)?"
                       r"(任何)?(文字|字|数字)[^，。；]*|看不出是在说群里[^，。；]*|一般(拿来|用来)[^，。；]*|"
                       r"数字(也)?看不清[^，。；]*)[，。；]?")
RETRY_SECS = (10, 30, 120, 600)
SENSITIVE = ("证件", "银行卡", "病历", "住址", "家人的私事", "公司内部文件")
SENS_ALIAS = {"身份证": "证件", "护照": "证件", "通行证": "证件", "地址": "住址", "家庭住址": "住址", "家人": "家人的私事",
              "工作文件": "公司内部文件", "内部文件": "公司内部文件", "医疗": "病历", "病例": "病历", "银行卡号": "银行卡"}
ASK_RX = re.compile(r"这是|这啥|这个是|是哪|在哪|哪里|哪儿|哪家|哪个|是谁|谁啊|谁呀|啥|什么|多少|几[个块点号岁钱]|真的假的|"
                    r"真的吗|是真的|你看看|看看这|看得清|看清|写的(是)?|什么意思|认识吗|认得")
WAIT_RX = re.compile(r"放大|细看|仔细看|我看看|我瞅瞅|等我看")
UNCLEAR_WORDS = "原图还没加载出来"

VISION_PROMPT = (
    "这是群聊里{who}发的一张图。{ctx}\n"
    "只输出一个 JSON（没有的字段就不写，不要写“看不出”“没有”“无”这种占位话）：\n"
    '{{"kind": "照片 / 聊天截图 / 网页截图 / 游戏截图 / 梗图 / 表情包 / 图表 / 文档 / 商品 / 其他（选一个）",\n'
    ' "gist": "它是什么，一句话，40 字以内，具体（“店里贴的羽毛球价目表”“K 线图，最后一根大阴线”）",\n'
    ' "sure": ["图上能确定的细节，最多 3 条，每条 20 字以内"],\n'
    ' "unsure": ["看不清、拿不准的，最多 2 条"],\n'
    ' "text": ["你在图上亲眼看清的关键原字，最多 6 处，照原样抄（繁体写繁体），看不清的别写"],\n'
    ' "speakers": [{{"who": "左边 / 右边 / 图上写的名字", "said": "他说的一句"}}],\n'
    ' "people": [{{"name": "群里的谁", "conf": "高 / 低", "why": "图上哪里写着他的名字"}}],\n'
    ' "sensitive": "只有图里露出了这些才写是哪个：证件 / 银行卡 / 病历 / 住址 / 家人的私事 / 公司内部文件；'
    '招聘、学习资料、网上的新闻和帖子、路人、普通聊天都不算，不算就不写这个字段",\n'
    ' "ask_ok": true}}\n'
    "规则：\n"
    "- 只写图上看得到的；不写发图的人想干嘛、在干嘛、要去哪；图里没有日期就别写日期。\n"
    "- 牌子、人名、地名：图上写着才写。people 只在图上写着群友的名字 / 群名片时才写，写把握；"
    "群里有：{names}。光看长相、看内容猜的不写。\n"
    "- 聊天截图：右边（绿色 / 蓝色气泡）是截图的人自己说的，左边是别人说的，speakers 按这个写，别把左边的话算成发图的人说的。\n"
    "- sensitive 的图 ask_ok 写 false。"
)

DEEP_PROMPT = (
    "群聊里{who}发了一张图，现在有人问：「{q}」。\n前后在聊：\n{ctx}\n"
    "第一次看的印象：{gist}\n{ocr}"
    "第一张是原图{crops}。只回答这个问题，只说图上看得到的；看不清就说看不清，别猜牌子、人名、地点。\n"
    '只输出 JSON：{{"answer": "一两句话的答案", "sure": true/false, "seen": ["支撑答案的图上原字或细节，最多 3 条"]}}'
)


def ensure_schema(conn):
    conn.executescript(SCHEMA)


def _norm(s: str) -> str:
    return re.sub(r"[\s\W_]+", "", str(s or "")).lower()


def on_ocr(item: str, ocr: str, ratio: float = 0.8) -> bool:
    """看图模型抄的这处字，OCR 也认出来了（允许一两个字的出入）。"""
    a, b = _norm(item), _norm(ocr)
    if not a or not b:
        return False
    if a in b:
        return True
    if len(a) < 4:
        return False
    n = len(a)
    best = 0.0
    for i in range(0, max(1, len(b) - n + 1)):
        r = SequenceMatcher(None, a, b[i:i + n]).ratio()
        if r > best:
            best = r
            if best >= ratio:
                return True
    return False


def parse_json(raw: str) -> dict | None:
    s = str(raw or "").strip()
    s = re.sub(r"^```(json)?|```$", "", s, flags=re.M).strip()
    m = re.search(r"\{.*\}", s, re.S)
    if not m:
        return None
    try:
        v = json.loads(m.group(0))
    except ValueError:
        try:
            v = json.loads(re.sub(r",\s*([}\]])", r"\1", m.group(0)))
        except ValueError:
            return None
    return v if isinstance(v, dict) else None


def _strs(v, n: int, cut: int = 40) -> list[str]:
    if isinstance(v, str):
        v = [v]
    out = []
    for x in v or []:
        x = FILLER_RX.sub("", str(x or "")).strip(" ，。；、")
        if x and x not in out and not re.fullmatch(r"(无|没有|不清楚|看不清|未知|-)", x):
            out.append(x[:cut])
    return out[:n]


def clean_info(raw: dict | None, *, ocr: str = "", turns: list | None = None, names: list[str] | None = None,
               thumb: bool = False, fallback: str = "") -> dict:
    """看图模型的 JSON → 能放心用的结构化结果。"""
    raw = raw or {}
    kind = str(raw.get("kind") or "").strip().split("/")[0].strip()
    kind = KIND_ALIAS.get(kind, kind)
    if kind not in KINDS:
        kind = next((k for k in KINDS if k in str(raw.get("kind") or "")), "其他")
    gist = FILLER_RX.sub("", str(raw.get("gist") or fallback or "")).strip(" ，。；、")
    gist = re.sub(r"^这是(一张|张)?", "", gist).strip()[:40]
    info: dict = {"kind": kind, "gist": gist}
    sure = _strs(raw.get("sure"), 3, 30)
    unsure = _strs(raw.get("unsure"), 2, 30)
    texts = [] if thumb else [t for t in _strs(raw.get("text"), 6, 40) if on_ocr(t, ocr)]
    if sure:
        info["sure"] = sure
    if unsure:
        info["unsure"] = unsure
    if texts:
        info["text"] = texts
    if kind != "聊天截图":
        turns = []                                               # 不是聊天截图：按气泡分的不算（卡片、表格也有色块）
    if turns:
        info["speakers"] = [{"who": ("截图的人自己" if t["side"] == "right" else (t.get("who") or "对方")),
                             "side": t["side"], "said": t["text"][:60]} for t in turns[:12]]
    elif isinstance(raw.get("speakers"), list) and not thumb and kind == "聊天截图":
        sp = []
        for s in raw["speakers"][:8]:
            if isinstance(s, dict) and str(s.get("said") or "").strip() and on_ocr(str(s["said"])[:20], ocr):
                who = str(s.get("who") or "")[:12]
                side = "right" if re.search(r"右|自己|截图的人", who) else ("left" if re.search(r"左|对方", who) else "")
                sp.append({"who": "截图的人自己" if side == "right" else (who or "对方"), "said": str(s["said"])[:60],
                           **({"side": side} if side else {})})
        if sp:
            info["speakers"] = sp
    people = []
    for p in raw.get("people") or [] if isinstance(raw.get("people"), list) else []:
        nm = str((p or {}).get("name") if isinstance(p, dict) else p or "").strip()
        if nm and ocr and nm in ocr and (not names or nm in names):       # 图上写着这个名字才算
            people.append({"name": nm, "conf": str((p or {}).get("conf") or "低")[:2] if isinstance(p, dict) else "低"})
    if people:
        info["people"] = people[:3]
    sens = raw.get("sensitive")
    sens = [x for x in _strs(sens, 3, 12) if isinstance(sens, (list, str))] if sens else []
    sens = [SENS_ALIAS.get(x, x) for x in sens]
    sens = [x for x in dict.fromkeys(sens) if x in SENSITIVE]          # 只认这几类（模型爱把路人、普通聊天也算上）
    if sens:
        info["sensitive"] = "、".join(sens)
    ask = raw.get("ask_ok")
    info["ask_ok"] = bool(ask if isinstance(ask, bool) else str(ask).lower() not in ("false", "否", "0")) and not info.get("sensitive")
    if thumb:
        info["thumb"] = True
    return info


def make_line(info: dict, status: str) -> str:
    """写进消息里的那一行（≤ 90 字左右）。"""
    if status == "missing":
        return UNCLEAR_WORDS
    if status == "fail" and not info.get("gist"):
        return ""
    kind = info.get("kind") or "其他"
    gist = info.get("gist") or ""
    line = f"{kind}：{gist}" if kind != "其他" else gist
    tx = [t for t in info.get("text") or [] if len(t) <= 16][:3]
    if tx and status != "thumb" and not info.get("sensitive"):          # 证件、病历这种图：原字（号码）不进聊天记录
        q = "".join(f"「{t}」" for t in tx)
        if len(line) + len(q) <= 88:
            line += q
    if status == "thumb":
        line += "（缩略图，原图还没加载出来，看不清细节）"
    return line.replace("[", "（").replace("]", "）")


class VisionMixin:
    # ---------------- 看一张图 ----------------
    def _img_ctx(self, gid: int | None, uid=None, before_ts: float | None = None, after: bool = False) -> str:
        """发图的人前后 2 句 + 群里最近在聊的 3 句（给看图模型知道这张图是拿来说什么的）。"""
        if gid is None:
            return ""
        now = before_ts or time.time()
        try:
            rows = self.db.conn.execute(
                "SELECT ts, user_id, name, role, content FROM qq_group_messages WHERE group_id=? AND ts<=? AND ts>? "
                "ORDER BY id DESC LIMIT 8", (gid, now + (120 if after else 0), now - 600)).fetchall()
        except Exception:  # noqa: BLE001
            return ""
        rows = list(reversed(rows))
        if not rows:
            return ""
        lines = []
        for r in rows[-6:]:
            c = str(r["content"] or "")
            if c.startswith("[图片") or not c.strip():
                continue
            who = "她" if r["role"] == "assistant" else (r["name"] or "")
            mark = "（发图的人）" if uid is not None and str(r["user_id"]) == str(uid) else ""
            lines.append(f"{who}{mark}：{c[:50]}")
        if not lines:
            return ""
        return "前后的聊天（发图的人配的话算图的一部分，但别替他猜意图）：\n" + "\n".join(lines[-5:])

    async def _fetch_img(self, url: str, quick: bool = False) -> tuple[bytes | None, bool]:
        from aigroup.onebot import fetch_bytes
        if str(url).startswith("wximg:") and hasattr(self.ob, "fetch_image"):
            data = await (self.ob.fetch_image(url, wait=0.5, wait_full=0.5) if quick else self.ob.fetch_image(url))
            return data, bool(getattr(self.ob, "last_was_thumb", False))
        try:
            import aigroup.chat.ingest as ing                 # 测试里会换掉 ingest.fetch_bytes
            return await ing.fetch_bytes(url), False
        except Exception:  # noqa: BLE001
            return await fetch_bytes(url), False

    async def _see(self, url: str, who: str = "群友", gid: int | None = None, uid=None, ctx: str | None = None,
                   quick: bool = False) -> dict:
        """看一张图。返回 {status, line, info, hash, ocr, layout, thumb, model}。"""
        ensure_schema(self.db.conn)
        data, thumb = await self._fetch_img(url, quick)
        if not data:
            return {"status": "missing", "line": UNCLEAR_WORDS, "info": {}, "hash": "", "ocr": "", "layout": [],
                    "thumb": False, "model": ""}
        h = hashlib.md5(data).hexdigest()[:16]
        old = self.db.conn.execute("SELECT * FROM qq_images WHERE hash=? AND status IN ('ok','thumb') AND line!='' "
                                   "ORDER BY id DESC LIMIT 1", (h,)).fetchone()
        if old:                                                 # 同一张图（转发、复读）只看一次
            return {"status": old["status"], "line": old["line"], "info": json.loads(old["info"] or "{}"), "hash": h,
                    "ocr": old["ocr"], "layout": json.loads(old["layout"] or "[]"), "thumb": old["status"] == "thumb",
                    "model": old["model"], "cached": True}
        legacy = self.db.get("qq_img_" + h)                     # 38.2 以前的缓存（一段文字）
        if legacy and not thumb:
            info = {"kind": "其他", "gist": trim(str(legacy), 40), "ask_ok": True}
            return {"status": "ok", "line": str(legacy)[:100], "info": info, "hash": h, "ocr": "", "layout": [],
                    "thumb": False, "model": "", "cached": True}
        if len(data) > 1_200_000:                               # 太大的图先缩一下
            try:
                from PIL import Image
                im = Image.open(io.BytesIO(data)).convert("RGB")
                im.thumbnail((1600, 1600))
                b = io.BytesIO()
                im.save(b, "JPEG", quality=85)
                data = b.getvalue()
            except Exception:  # noqa: BLE001
                pass
        names = self.member_names(gid)[:12] if gid is not None and hasattr(self, "member_names") else []
        lines, turns, ocr = [], [], ""
        if not thumb and self.gk(gid).get("ocr", True):
            try:
                from aigroup.net.ocr import chat_layout, lines_text, ocr_lines
                lines, arr = await asyncio.wait_for(asyncio.to_thread(ocr_lines, data), 12)
                ocr = lines_text(lines)
                if len(re.sub(r"\W", "", ocr)) < 6:
                    ocr = ""
                turns = chat_layout(lines, arr)["turns"] if lines else []
            except Exception as e:  # noqa: BLE001
                log.info("OCR 没跑完：%s", e)
        if ctx is None:
            ctx = self._img_ctx(gid, uid)
        prompt = VISION_PROMPT.format(who=who, ctx=("\n" + ctx) if ctx else "", names="、".join(names) or "（不知道）")
        if thumb:
            prompt += ("\n\n（这是一张很小的缩略图，原图没下下来：只写看得清的大概是什么；看不清的字一个也别写、别猜，"
                       "text、people、speakers 都不写。）")
        if ocr:
            prompt += f"\n\n（OCR 认出的字，可能有错字，只作参考；text 里写你自己在图上看清的：{ocr[:300]}）"
        if turns:
            from aigroup.net.ocr import render_turns
            prompt += f"\n\n（按气泡颜色分的，聊天截图里谁说的：{render_turns(turns, 300)}）"
        model = ""
        try:
            raw = await self.brain.llm.describe_image(base64.b64encode(data).decode(), "", prompt=prompt)
            routes = getattr(self.brain.llm, "routes", None) or {}
            model = str((routes.get("vision") or [""])[0]) if isinstance(routes, dict) else ""
        except Exception as e:  # noqa: BLE001
            log.info("群图片没看清：%s", e)
            return {"status": "fail", "line": "", "info": {}, "hash": h, "ocr": ocr, "layout": turns, "thumb": thumb,
                    "model": ""}
        js = parse_json(raw)
        info = clean_info(js, ocr=ocr, turns=turns, names=names, thumb=thumb,
                          fallback="" if js else trim(raw, 40))
        if not js and ocr and not thumb:                        # 模型没按 JSON 写：至少留 OCR 认出来又被它写到的字
            info["text"] = [t for t in re.findall(r"「([^」]{1,30})」", str(raw)) if on_ocr(t, ocr)][:4]
            if not info["text"]:
                info.pop("text")
        status = "thumb" if thumb else "ok"
        return {"status": status, "line": make_line(info, status), "info": info, "hash": h, "ocr": ocr,
                "layout": turns, "thumb": thumb, "model": model}

    async def _look(self, url: str, who: str = "群友", gid: int | None = None) -> str:
        """老接口：只要写进消息里的那一行。"""
        rec = await self._see(url, who, gid)
        if rec.get("status") in ("ok", "thumb") and not rec.get("cached"):
            self._img_save(gid or 0, rec, None, url, who)
        return rec["line"] if rec.get("status") != "missing" else ""

    def _img_save(self, gid: int, rec: dict, msg_row: int | None, url: str, who: str) -> int:
        ensure_schema(self.db.conn)
        now = time.time()
        cur = self.db.conn.execute(
            "INSERT INTO qq_images(ts, group_id, msg_row, url, who, hash, status, kind, gist, line, ocr, layout, info, model, "
            "updated) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (now, gid, msg_row, url, who, rec.get("hash") or "", rec.get("status") or "", (rec.get("info") or {}).get("kind", ""),
             (rec.get("info") or {}).get("gist", ""), rec.get("line") or "", rec.get("ocr") or "",
             json.dumps(rec.get("layout") or [], ensure_ascii=False), json.dumps(rec.get("info") or {}, ensure_ascii=False),
             rec.get("model") or "", now))
        self.db.conn.commit()
        return int(cur.lastrowid)

    def img_record(self, gid: int, msg_row: int) -> dict | None:
        try:
            ensure_schema(self.db.conn)
            r = self.db.conn.execute("SELECT * FROM qq_images WHERE group_id=? AND msg_row=? ORDER BY id DESC LIMIT 1",
                                     (gid, msg_row)).fetchone()
        except Exception:  # noqa: BLE001
            return None
        if not r:
            return None
        d = dict(r)
        for k, dflt in (("info", {}), ("layout", []), ("deep", [])):
            try:
                d[k] = json.loads(d[k] or "") if d[k] else dflt
            except ValueError:
                d[k] = dflt
        return d

    def _img_set_msg(self, msg_row: int, line: str):
        """把消息里的 [图片…] 换成新看的这一行。"""
        r = self.db.conn.execute("SELECT content FROM qq_group_messages WHERE id=?", (msg_row,)).fetchone()
        if not r:
            return
        from aigroup.core.safety import clean_member_text
        new = re.sub(r"\[图片(?:[：:][^\]]*)?\]", f"[图片：{clean_member_text(line, 100)}]" if line else "[图片]",
                     str(r["content"] or ""), count=1)
        self.db.conn.execute("UPDATE qq_group_messages SET content=? WHERE id=?", (new[:500], msg_row))
        self.db.conn.commit()

    def after_image(self, gid: int, msg_row: int, rec: dict, url: str, who: str, uid=None):
        """on_event 记完消息以后：存 qq_images，缩略图 / 没加载出来的排队补原图。"""
        try:
            img_id = self._img_save(gid, rec, msg_row, url, who)
        except Exception as e:  # noqa: BLE001
            log.info("看图结果没存上：%s", e)
            return
        if rec.get("status") in ("thumb", "missing", "fail") and str(url).startswith("wximg:") \
                and self.gk(gid).get("img_retry", True):
            self._spawn(self._img_retry(gid, img_id, uid))

    async def _img_retry(self, gid: int, img_id: int, uid=None):
        """缩略图 / 没加载出来：10 秒、30 秒、2 分钟、10 分钟各去拿一次原图；拿到就重看、把消息里的描述换掉。"""
        t0 = time.time()
        secs = [float(x) for x in (self.gk(gid).get("img_retry_secs") or RETRY_SECS)]
        for n, s in enumerate(secs, 1):
            await asyncio.sleep(max(0.0, t0 + s - time.time()))
            r = self.db.conn.execute("SELECT * FROM qq_images WHERE id=?", (img_id,)).fetchone()
            if not r or r["status"] == "ok":
                return
            data, thumb = await self._fetch_img(r["url"], quick=True)
            self.db.conn.execute("UPDATE qq_images SET tries=? WHERE id=?", (n, img_id))
            self.db.conn.commit()
            if not data or (thumb and r["status"] == "thumb"):
                continue
            ctx = self._img_ctx(gid, uid, before_ts=float(r["ts"]), after=True)
            rec = await self._see(r["url"], r["who"], gid, uid=uid, ctx=ctx, quick=True)
            if rec["status"] not in ("ok", "thumb") or (rec["status"] == "thumb" and r["status"] == "thumb"):
                continue
            info = rec.get("info") or {}
            self.db.conn.execute(
                "UPDATE qq_images SET status=?, hash=?, kind=?, gist=?, line=?, ocr=?, layout=?, info=?, model=?, recovered=?, "
                "updated=? WHERE id=?",
                (rec["status"], rec.get("hash") or "", info.get("kind", ""), info.get("gist", ""), rec["line"], rec.get("ocr") or "",
                 json.dumps(rec.get("layout") or [], ensure_ascii=False), json.dumps(info, ensure_ascii=False),
                 rec.get("model") or "", int(rec["status"] == "ok"), time.time(), img_id))
            self.db.conn.commit()
            if r["msg_row"]:
                self._img_set_msg(int(r["msg_row"]), rec["line"])
            log.info("群 %s：第 %d 次补到了%s（%s）：%s", gid, n, "原图" if rec["status"] == "ok" else "缩略图",
                     r["status"], rec["line"][:40])
            if rec["status"] == "ok":
                return

    async def img_backfill(self, gid: int, items: list[tuple]):
        """catch_up 补进来的图（她不在时发的）：后台看最近几张，把 [图片] 换成描述。items：(msg_row, url, name, uid, ts)。"""
        for row, url, name, uid, ts in items:
            try:
                ctx = self._img_ctx(gid, uid, before_ts=float(ts), after=True)
                rec = await self._see(url, name, gid, uid=uid, ctx=ctx, quick=True)
            except Exception as e:  # noqa: BLE001
                log.info("群 %s：补看图没成：%s", gid, e)
                continue
            if rec.get("line"):
                self._img_set_msg(row, rec["line"])
            self.after_image(gid, row, rec, url, name, uid)
        if items:
            log.info("群 %s：她不在时的 %d 张图补看了", gid, len(items))

    # ---------------- 被问到再细看 ----------------
    def _ask_target(self, gid: int, now: float | None = None) -> tuple[dict, dict] | None:
        """她上次说话以后，有人在问刚才那张图：返回 (问的那条, 图那条)。"""
        now = now or time.time()
        rows = [dict(r) for r in self._rows(gid, 10)]
        last_her = max((i for i, r in enumerate(rows) if r["role"] == "assistant"), default=-1)
        for qi in range(len(rows) - 1, last_her, -1):
            q = rows[qi]
            body = re.sub(r"^\[回复[^\]]*\]\s*|^@你\s*", "", str(q["content"] or ""))
            if q["role"] != "user" or now - float(q["ts"]) > 600 or body.startswith("[图片") or not ASK_RX.search(body):
                continue
            if q.get("reply_to"):
                r = self.db.conn.execute("SELECT * FROM qq_group_messages WHERE group_id=? AND qq_msg_id=? ORDER BY id DESC LIMIT 1",
                                         (gid, q["reply_to"])).fetchone()
                if r and str(r["content"] or "").lstrip().startswith("[图片"):
                    return q, dict(r)
            users = [r for r in rows[:qi] if r["role"] == "user"][-3:]
            for r in reversed(users):
                if str(r["content"] or "").startswith("[图片") and float(q["ts"]) - float(r["ts"]) < 600:
                    return q, r
            return None
        return None

    async def image_zoom(self, gid: int, wait_note: bool = True) -> bool:
        """有人在问刚才那张图：原图 + 问题 + 前后聊天再看一次（字小的裁出来放大）。结果追加进 qq_images.deep。"""
        if not self.gk(gid).get("image_zoom", True) or not getattr(self.brain.llm, "vision", False):
            return False
        hit = self._ask_target(gid)
        if not hit:
            return False
        q, img_row = hit
        rec = self.img_record(gid, int(img_row["id"]))
        if not rec or rec["status"] == "missing" or not rec.get("url"):
            return False
        qtext = re.sub(r"^\[回复[^\]]*\]\s*|^@你\s*", "", str(q["content"] or ""))[:60]
        if any(d.get("q") == qtext for d in rec["deep"]) or len(rec["deep"]) >= 2:
            return False
        if not self._allow(f"img_zoom_{gid}", int(self.gk(gid).get("image_zoom_per_hour", 6) or 0)):
            return False
        self.__dict__.setdefault("_img_zooming", {})[gid] = time.time()
        try:
            data, thumb = await self._fetch_img(rec["url"], quick=True)
            if not data:
                return False
            crops: list[bytes] = []
            lines: list = []
            if not thumb:
                try:
                    from aigroup.net.ocr import crops_for, ocr_lines
                    lines, _ = await asyncio.wait_for(asyncio.to_thread(ocr_lines, data), 12)
                    words = [w for w in re.findall(r"[A-Za-z0-9]{2,}|[一-鿿]{2,}", qtext) if not ASK_RX.fullmatch(w)]
                    small = lines and sorted(d["y1"] - d["y0"] for d in lines)[len(lines) // 2] < 0.025
                    if lines and (small or any(w in d["text"] for d in lines for w in words)):
                        crops = await asyncio.to_thread(crops_for, data, lines, words, 2)
                except Exception as e:  # noqa: BLE001
                    log.info("细看时 OCR / 裁图没成：%s", e)
            from aigroup.llm.client import image_payload
            ctx = self._img_ctx(gid, img_row.get("user_id"), before_ts=float(q["ts"]))
            prompt = DEEP_PROMPT.format(who=rec.get("who") or "群友", q=qtext, ctx=ctx or "（没有）",
                                        gist=rec.get("line") or "（没有）",
                                        ocr=(f"OCR 认出的字（可能有错）：{rec.get('ocr')[:300]}\n" if rec.get("ocr") else ""),
                                        crops=f"，后面 {len(crops)} 张是按字的位置裁出来放大的" if crops else "")
            if thumb:
                prompt += "\n（这还是缩略图，原图没下下来：看不清就直说。）"
            content = [{"type": "text", "text": prompt}]
            for b in [data, *crops]:
                b64, mime = image_payload(base64.b64encode(b).decode())
                content.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})
            raw = await self.brain.llm.chat([{"role": "user", "content": content}], role="vision", temperature=0.2,
                                            max_tokens=500)
            js = parse_json(raw) or {"answer": trim(raw, 80), "sure": False}
            ocr = " ".join(d["text"] for d in lines) if lines else (rec.get("ocr") or "")
            seen = [s for s in _strs(js.get("seen"), 3, 40) if thumb is False and (on_ocr(s, ocr) or not re.search(r"「", s))]
            item = {"q": qtext, "a": str(js.get("answer") or "")[:120], "sure": bool(js.get("sure")) and not thumb,
                    "seen": seen, "crops": len(crops), "ts": time.time()}
            if not item["a"]:
                return False
            deep = rec["deep"] + [item]
            self.db.conn.execute("UPDATE qq_images SET deep=?, updated=? WHERE id=?",
                                 (json.dumps(deep, ensure_ascii=False), time.time(), rec["id"]))
            self.db.conn.commit()
            tlog.info("群 %s：被问「%s」，又细看了一次（裁了 %d 块）：%s", gid, qtext[:20], len(crops), item["a"][:40])
            return True
        except Exception as e:  # noqa: BLE001
            log.info("群 %s：细看没成：%s", gid, e)
            return False
        finally:
            self.__dict__.setdefault("_img_zooming", {}).pop(gid, None)

    async def image_zoom_wait(self, gid: int, who: str, timeout: float = 15.0):
        """_prepare 里用：最多等 15 秒；没看完让她先说“我放大看看”，看完了再回来接一句。"""
        task = asyncio.ensure_future(self.image_zoom(gid))
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout)
            return
        except asyncio.TimeoutError:
            self.__dict__.setdefault("_img_late", {})[gid] = time.time()
            log.info("群 %s：细看图超过 %.0f 秒，先让她说放大看看", gid, timeout)
        except Exception:  # noqa: BLE001
            return

        async def later():
            try:
                ok = await task
            except Exception:  # noqa: BLE001
                ok = False
            self.__dict__.setdefault("_img_late", {}).pop(gid, None)
            if not ok:
                return
            mine = self.db.conn.execute("SELECT content, ts FROM qq_group_messages WHERE group_id=? AND role='assistant' "
                                        "ORDER BY id DESC LIMIT 1", (gid,)).fetchone()
            if not mine or time.time() - float(mine["ts"]) > 300 or not WAIT_RX.search(str(mine["content"] or "")):
                return
            st = self._st(gid)
            async with st.lock:
                await self.speak(gid, mentioned=True, owner_spoke=False, trigger=None, who=who,
                                 why="你刚说了放大看看那张图，现在看完了（结果在“刚有人发了图”里）：接着把看到的告诉他，一两句；看不清就直说")
        self._spawn(later())

    # ---------------- 提示词里的细节 ----------------
    def image_detail(self, gid: int, msg_row: int) -> str:
        """“刚有人发了图”那段里的细节：确定的 / 拿不准的 / 能引的原字 / 截图里谁说的 / 敏感 / 缩略图 / 细看的结果。"""
        rec = self.img_record(gid, msg_row)
        if not rec:
            return ""
        info = rec["info"] or {}
        out = []
        st = rec["status"]
        if st == "missing":
            out.append("- 这张图还没加载出来，你没看到：别说得像看过，可以说“图没刷出来”或者让他说说是啥")
        elif st == "thumb":
            out.append("- 只拿到了缩略图（原图还没加载出来）：照实说看不清，别认牌子、字、人")
        elif st == "fail":
            out.append("- 这张图你没看成：别说得像看过")
        if info.get("sure"):
            out.append("- 能确定的：" + "；".join(info["sure"]))
        if info.get("unsure"):
            out.append("- 拿不准的（别当真说）：" + "；".join(info["unsure"]))
        if info.get("text") and not info.get("sensitive"):
            out.append("- 图上的字（看图和 OCR 都认出来的，要引图上的字只能引这几处）：" + "".join(f"「{t}」" for t in info["text"]))
        elif rec.get("ocr"):
            out.append("- 图上的字只有机器认的、没对上：别照着念原字")
        sp = info.get("speakers") or []
        if sp:
            body = "／".join(f"{s.get('who') or '?'}：{s.get('said', '')[:30]}" for s in sp[:6])
            out.append(f"- 截图里谁说的（右边是发图的人自己，左边是别人；左边的话不是发图的人说的）：{body}")
        if info.get("people"):
            out.append("- 图上写着名字的群友：" + "、".join(f"{p['name']}（把握{p.get('conf', '低')}）" for p in info["people"]))
        if info.get("sensitive"):
            out.append(f"- 这张图沾上了「{info['sensitive']}」：只接他的情绪 / 他配的话，别点评、别复述图里的内容")
        elif info.get("ask_ok") is False:
            out.append("- 这张图不适合追着聊细节")
        for d in rec.get("deep") or []:
            out.append(f"- 有人问「{d['q']}」，你又放大细看了一次：{d['a']}（{'有把握' if d.get('sure') else '拿不准，说的时候带上“好像”'}）")
        if (self.__dict__.get("_img_late") or {}).get(gid) or (self.__dict__.get("_img_zooming") or {}).get(gid):
            out.append("- 你正在放大细看这张图，还没看完：可以先说一句“等下我放大看看”，别现在就下结论")
        return "\n".join(out)

    # ---------------- 发前检查 ----------------
    def _recent_imgs(self, gid: int, now: float, mins: float = 30) -> list[dict]:
        try:
            ensure_schema(self.db.conn)
            rows = self.db.conn.execute("SELECT msg_row FROM qq_images WHERE group_id=? AND ts>? AND msg_row IS NOT NULL "
                                        "ORDER BY id DESC LIMIT 4", (gid, now - mins * 60)).fetchall()
        except Exception:  # noqa: BLE001
            return []
        return [x for x in (self.img_record(gid, int(r["msg_row"])) for r in rows) if x]

    def _img_unseen(self, gid: int, text: str, now: float | None = None) -> str:
        """引了图上 OCR 认的、看图没对上的字（“一浴酶全胀气”这种错字）。"""
        now = now or time.time()
        t = _norm(text)
        if len(t) < 3:
            return ""
        chat = _norm(" ".join(re.sub(r"\[图片[^\]]*\]", "", str(r["content"] or "")) for r in self._rows(gid, 12)))
        for rec in self._recent_imgs(gid, now):
            ocr = rec.get("ocr") or ""
            if not ocr:
                continue
            info = rec["info"] or {}
            ok = _norm(" ".join([*(info.get("text") or []), *(info.get("sure") or []), info.get("gist", ""),
                                 *(d.get("a", "") for d in rec.get("deep") or []), *(s for d in rec.get("deep") or []
                                                                                       for s in d.get("seen") or [])]))
            for seg in ocr.split(" / "):
                s = _norm(seg)
                if len(s) < 3 or on_ocr(seg, " ".join(info.get("text") or [])):
                    continue
                for i in range(len(s) - 2):
                    w = s[i:i + 3]
                    if re.fullmatch(r"[一-鿿]{3}", w) and w in t and w not in ok and w not in chat:
                        return (f"「{w}」是图上机器认的字，看图时没对上、可能认错：别照着念，"
                                "要说就说大概意思，或者问他一句写的是啥")
        return ""

    def _img_sensitive(self, gid: int, text: str, now: float | None = None) -> str:
        """别人家里人 / 工作文件 / 证件这种图：她在点评、复述图里的细节。"""
        now = now or time.time()
        t = _norm(text)
        for rec in self._recent_imgs(gid, now, 15):
            info = rec["info"] or {}
            if not info.get("sensitive"):
                continue
            pool = _norm(" ".join([rec.get("ocr") or "", *(info.get("text") or []), *(info.get("sure") or [])]))
            for i in range(max(0, len(pool) - 3)):
                w = pool[i:i + 4]
                if re.search(r"[一-鿿]{2}", w) and w in t:
                    return (f"那张图沾上了「{info['sensitive']}」，你在说图里的细节（「{w}」）：只接他的情绪或者他配的话，"
                            "别点评、别复述图里的内容")
        return ""

    # ---------------- 统计（周报 / 复核用） ----------------
    def image_stats(self, gid: int, days: float = 7) -> dict:
        ensure_schema(self.db.conn)
        rows = self.db.conn.execute("SELECT status, recovered, deep FROM qq_images WHERE group_id=? AND ts>? AND msg_row IS NOT NULL",
                                    (gid, time.time() - days * 86400)).fetchall()
        n = len(rows)
        first_bad = sum(1 for r in rows if r["status"] != "ok" or r["recovered"])
        rec = sum(1 for r in rows if r["recovered"])
        deep = sum(len(json.loads(r["deep"] or "[]")) for r in rows)
        return {"images": n, "thumb_or_missing": first_bad, "recovered": rec,
                "thumb_rate": round(first_bad / n, 2) if n else 0.0,
                "recover_rate": round(rec / first_bad, 2) if first_bad else 0.0,
                "still_bad": sum(1 for r in rows if r["status"] != "ok"), "deep_looks": deep}


def trim(s: str, n: int) -> str:
    s = re.sub(r"\s+", " ", str(s or "")).strip()
    return s[:n]
