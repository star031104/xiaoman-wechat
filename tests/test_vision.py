"""38.2 看图 2.0：结构化结果、补原图、补看离线的图、被问到再细看、聊天截图分左右、发前检查。"""
import asyncio
import io
import json

from conftest import GROUP, _gc, _run

JS = json.dumps({"kind": "聊天截图", "gist": "两个人聊煎鸡翅", "sure": ["有人问为什么煎三个"],
                 "unsure": ["像是在家里"], "text": ["为什么煎三个鸡翅", "根本不存在的字"],
                 "people": [{"name": "小王", "conf": "高"}], "sensitive": "", "ask_ok": True}, ensure_ascii=False)


def _ev(mid, url="http://x/a.png", uid=88888, name="小王", text=None, reply=None):
    segs = []
    if reply:
        segs.append({"type": "reply", "data": {"id": str(reply)}})
    segs.append({"type": "image", "data": {"url": url, "file": "a"}} if text is None
                else {"type": "text", "data": {"text": text}})
    return {"post_type": "message", "message_type": "group", "group_id": GROUP, "user_id": uid, "message_id": mid,
            "sender": {"card": name}, "message": segs}


def test_clean_info_keeps_only_ocr_text_and_named_people():
    from aigroup.chat.vision import clean_info, make_line
    info = clean_info(json.loads(JS), ocr="为什么煎三个鸡翅 / 一人一个吗", names=["小王", "阿杰"])
    assert info["kind"] == "聊天截图" and info["text"] == ["为什么煎三个鸡翅"]
    assert "people" not in info                               # 图上没写“小王”
    info2 = clean_info({"kind": "截图", "gist": "这是一张价目表，看不出是干嘛的", "people": ["小王"]},
                       ocr="小王 的 价目表", names=["小王"])
    assert info2["kind"] == "网页截图" and info2["gist"] == "价目表" and info2["people"][0]["name"] == "小王"
    t = clean_info(json.loads(JS), ocr="为什么煎三个鸡翅", thumb=True)
    assert "text" not in t and t["thumb"]
    assert "缩略图" in make_line(t, "thumb") and make_line({}, "missing") == "原图还没加载出来"
    assert make_line(info, "ok").startswith("聊天截图：两个人聊煎鸡翅「为什么煎三个鸡翅」")
    s = clean_info({"kind": "照片", "gist": "一份工作文件", "sensitive": "工作文件"})
    assert s["sensitive"] == "公司内部文件" and s["ask_ok"] is False
    assert "sensitive" not in clean_info({"kind": "照片", "gist": "街景", "sensitive": "陌生人的脸"})


def test_on_event_structured_and_detail(cfg, brain, monkeypatch):
    from aigroup.chat import ingest
    gc, ob = _gc(cfg, brain)
    brain.llm.vision = True
    monkeypatch.setattr(ingest, "fetch_bytes", lambda url, **k: asyncio.sleep(0, b"\x89PNGfake"))
    seen = []

    async def d(b64, hint="", prompt=""):
        seen.append(prompt)
        return "```json\n" + JS + "\n```"
    brain.llm.describe_image = d
    gc._record(GROUP, 88888, "小王", "我今天做了鸡翅", 1)
    _run(gc, _ev(2))
    row = brain.db.conn.execute("SELECT id, content FROM qq_group_messages ORDER BY id DESC LIMIT 1").fetchone()
    assert row["content"].startswith("[图片：聊天截图：两个人聊煎鸡翅]")       # OCR 没认出字：模型抄的字一个也不放
    assert "我今天做了鸡翅" in seen[0] and "发图的人" in seen[0]                 # 带上了他前面配的话
    rec = gc.img_record(GROUP, row["id"])
    assert rec["status"] == "ok" and rec["info"]["gist"] == "两个人聊煎鸡翅"
    det = gc.image_detail(GROUP, row["id"])
    assert "能确定的：有人问为什么煎三个" in det and "拿不准" in det
    blk = gc.image_block(GROUP)
    assert "能确定的" in blk


def test_thumb_retry_recovers(cfg, brain):
    gc, ob = _gc(cfg, brain, img_retry_secs=[0, 0])
    brain.llm.vision = True
    state = {"n": 0}

    async def fetch_image(url, wait=8.0, wait_full=25.0):
        state["n"] += 1
        ob.last_was_thumb = state["n"] == 1
        return b"THUMB" if state["n"] == 1 else b"FULLIMG"
    ob.fetch_image = fetch_image
    prompts = []

    async def d(b64, hint="", prompt=""):
        prompts.append(prompt)
        return json.dumps({"kind": "图表", "gist": "K 线图，最后一根大阴线"}, ensure_ascii=False)
    brain.llm.describe_image = d
    _run(gc, _ev(5, url="wximg:g@chatroom:" + "a" * 32 + ":1"))
    row = brain.db.conn.execute("SELECT id, content FROM qq_group_messages ORDER BY id DESC LIMIT 1").fetchone()
    rec = gc.img_record(GROUP, row["id"])
    assert "缩略图" in prompts[0] and len(prompts) == 2
    assert rec["status"] == "ok" and rec["recovered"] == 1 and rec["tries"] == 1
    assert row["content"] == "[图片：图表：K 线图，最后一根大阴线]"
    st = gc.image_stats(GROUP)
    assert st["recovered"] == 1 and st["recover_rate"] == 1.0


def test_missing_then_backfill(cfg, brain):
    gc, ob = _gc(cfg, brain, img_retry_secs=[0])
    brain.llm.vision = True

    async def fetch_image(url, wait=8.0, wait_full=25.0):
        ob.last_was_thumb = False
        return None
    ob.fetch_image = fetch_image

    async def d(b64, hint="", prompt=""):
        return json.dumps({"kind": "照片", "gist": "一碗面"}, ensure_ascii=False)
    brain.llm.describe_image = d
    _run(gc, _ev(6, url="wximg:g@chatroom:" + "b" * 32 + ":1"))
    row = brain.db.conn.execute("SELECT id, content FROM qq_group_messages ORDER BY id DESC LIMIT 1").fetchone()
    assert row["content"] == "[图片：原图还没加载出来]"
    assert "还没加载出来" in gc.image_detail(GROUP, row["id"])

    async def ok(url, wait=8.0, wait_full=25.0):
        ob.last_was_thumb = False
        return b"PIC"
    ob.fetch_image = ok
    rid = gc._record(GROUP, 88888, "小王", "[图片]", 7)            # 离线时补进来的
    asyncio.run(gc.img_backfill(GROUP, [(rid, "wximg:g@chatroom:" + "c" * 32 + ":1", "小王", 88888, 0)]))
    assert brain.db.conn.execute("SELECT content FROM qq_group_messages WHERE id=?", (rid,)).fetchone()[0] == "[图片：照片：一碗面]"


def test_catch_up_queues_images(cfg, brain):
    import time
    gc, ob = _gc(cfg, brain)
    brain.llm.vision = True
    got = []

    async def fake_backfill(gid, items):
        got.extend(items)
    gc.img_backfill = fake_backfill
    now = time.time()

    async def call(action, **kw):
        if action == "get_group_msg_history":
            return {"messages": [{"message_id": 50, "user_id": 88888, "time": now - 60, "sender": {"card": "小王"},
                                  "message": [{"type": "image", "data": {"url": "wximg:g:" + "d" * 32 + ":1"}}]}]}
        return {}
    ob.call = call

    async def go():
        await gc._catch_up_group(GROUP)
        await asyncio.gather(*list(gc._bg), return_exceptions=True)
    asyncio.run(go())
    assert len(got) == 1 and got[0][1].startswith("wximg:")


def _png() -> bytes:
    from PIL import Image
    b = io.BytesIO()
    Image.new("RGB", (40, 40), "white").save(b, "PNG")
    return b.getvalue()


def test_ask_triggers_zoom(cfg, brain, monkeypatch):
    from aigroup.chat import ingest
    gc, ob = _gc(cfg, brain)
    brain.llm.vision = True
    png = _png()
    monkeypatch.setattr(ingest, "fetch_bytes", lambda url, **k: asyncio.sleep(0, png))

    async def d(b64, hint="", prompt=""):
        return json.dumps({"kind": "照片", "gist": "一座白色的塔"}, ensure_ascii=False)
    brain.llm.describe_image = d
    calls = []

    async def chat(messages, role=None, **kw):
        calls.append((role, messages))
        return json.dumps({"answer": "塔上写着「广州塔」", "sure": True, "seen": ["广州塔"]}, ensure_ascii=False)
    brain.llm.chat = chat
    gc._record(GROUP, 88888, "小王", "[图片：照片：一座白色的塔]", 10)
    rid = brain.db.conn.execute("SELECT MAX(id) FROM qq_group_messages").fetchone()[0]
    gc.after_image(GROUP, rid, {"status": "ok", "line": "照片：一座白色的塔", "info": {"kind": "照片", "gist": "一座白色的塔"},
                                "hash": "h1"}, "http://x/t.png", "小王")
    gc._record(GROUP, 66666, "阿杰", "这是哪啊", 11)
    assert gc._ask_target(GROUP)[1]["id"] == rid
    assert asyncio.run(gc.image_zoom(GROUP)) is True
    assert calls and calls[0][0] == "vision" and "这是哪啊" in calls[0][1][0]["content"][0]["text"]
    det = gc.image_detail(GROUP, rid)
    assert "你又放大细看了一次：塔上写着「广州塔」（有把握）" in det
    assert asyncio.run(gc.image_zoom(GROUP)) is False                 # 同一个问题不再看第二次
    gc._record(GROUP, 66666, "阿杰", "哈哈哈", 12)
    gc._record(GROUP, 66666, "阿杰", "笑死", 13)
    gc._record(GROUP, 66666, "阿杰", "无语", 14)
    gc._record(GROUP, 66666, "阿杰", "这是谁", 15)                       # 图已经是 4 句以前了：不算在问图
    assert gc._ask_target(GROUP) is None


def test_reply_to_image_is_ask(cfg, brain):
    gc, ob = _gc(cfg, brain)
    gc._record(GROUP, 88888, "小王", "[图片：照片：一杯咖啡]", 20)
    for i in range(4):
        gc._record(GROUP, 66666, "阿杰", f"别的话{i}", 21 + i)
    gc._record(GROUP, 66666, "阿杰", "[回复别人的「[图片：照片：一杯咖啡]」] 多少钱", 30, reply_to=20)
    hit = gc._ask_target(GROUP)
    assert hit and "一杯咖啡" in hit[1]["content"]


def test_img_unseen_and_sensitive(cfg, brain):
    gc, ob = _gc(cfg, brain)
    rid = gc._record(GROUP, 88888, "小王", "[图片：聊天截图：工作沟通]", 40)
    gc._img_save(GROUP, {"status": "ok", "line": "聊天截图：工作沟通", "hash": "x",
                         "info": {"kind": "聊天截图", "gist": "工作沟通", "text": ["其中有异议的"], "sensitive": "工作文件"},
                         "ocr": "其中有异议的 / 生产8240kg一浴酶 / 全部胀气了"}, rid, "u", "小王")
    w = gc._img_unseen(GROUP, "一浴酶是啥意思")
    assert "一浴酶" in w and "没对上" in w
    assert gc._img_unseen(GROUP, "看起来是在讨论工作") == ""
    assert "工作文件" in gc._img_sensitive(GROUP, "8240kg一浴酶也太多了")
    assert gc._img_sensitive(GROUP, "辛苦了，周末好好休息") == ""


def _bubbles() -> bytes:
    from PIL import Image, ImageDraw, ImageFont
    try:
        font = ImageFont.load_default(size=36)
    except TypeError:
        import pytest
        pytest.skip("Pillow 太旧")
    im = Image.new("RGB", (900, 700), (237, 237, 237))
    dr = ImageDraw.Draw(im)
    dr.text((40, 30), "Alice", fill=(120, 120, 120), font=ImageFont.load_default(size=26))
    for y, txt, right in ((70, "hello there", False), (190, "see you soon", True), (310, "ok bye now", False),
                          (430, "good night", True)):
        w = 320
        x0 = 900 - 40 - w if right else 40
        dr.rectangle((x0, y, x0 + w, y + 80), fill=(160, 236, 112) if right else (255, 255, 255))
        dr.text((x0 + 30, y + 20), txt, fill="black", font=font)
    b = io.BytesIO()
    im.save(b, "PNG")
    return b.getvalue()


def test_chat_layout_bubbles():
    import pytest

    from aigroup.net import ocr
    if ocr._get() is None:
        pytest.skip("没装 rapidocr")
    lines, arr = ocr.ocr_lines(_bubbles())
    lay = ocr.chat_layout(lines, arr)
    assert lay["chat"]
    sides = {t["text"].lower().replace(" ", ""): t["side"] for t in lay["turns"]}
    assert sides.get("hellothere") == "left" and sides.get("seeyousoon") == "right"
    assert sides.get("goodnight") == "right" and sides.get("okbyenow") == "left"
    assert "右边（截图的人自己）" in ocr.render_turns(lay["turns"])


def test_table_is_not_chat():
    from aigroup.net import ocr
    lines = [{"x0": 0.05, "x1": 0.3, "y0": 0.1 * i, "y1": 0.1 * i + 0.03, "text": f"学校{i}"} for i in range(1, 6)] + \
            [{"x0": 0.7, "x1": 0.9, "y0": 0.1 * i, "y1": 0.1 * i + 0.03, "text": f"{i}000"} for i in range(1, 6)]
    import numpy as np
    arr = np.full((600, 600, 3), 255, dtype=np.uint8)
    assert ocr.chat_layout(lines, arr)["chat"] is False


def test_review_exports_and_fixes_images(cfg, brain):
    from aigroup.memory import review as R
    gc, ob = _gc(cfg, brain)
    rid = gc._record(GROUP, 88888, "小王", "[图片：其他：看不出是干嘛的]", 60)
    iid = gc._img_save(GROUP, {"status": "ok", "line": "看不出是干嘛的", "hash": "q",
                               "info": {"kind": "其他", "gist": ""}, "ocr": "收盘 3021.5 / 大阴线"}, rid, "u", "小王")
    d = R.export(gc.db, GROUP, gc.cfg.tz)
    assert d["images"][0]["id"] == iid and "看图" in R.render_text(d)
    ap = R.Applier(gc.db, gc.memory, GROUP, gc.cfg.tz)
    res = ap.run({"gid": GROUP, "images": [{"id": iid, "kind": "图表", "gist": "K 线图，最后一根大阴线", "text": ["收盘 3021.5"]},
                                           {"id": iid, "text": ["根本没有的字"]}]})
    assert res["ok"] == 1 and "OCR 里没有" in res["rejected"][0]["why"]
    c = brain.db.conn.execute("SELECT content FROM qq_group_messages WHERE id=?", (rid,)).fetchone()[0]
    assert c == "[图片：图表：K 线图，最后一根大阴线「收盘 3021.5」]"
    R.undo(gc.db, res["run"])
    c = brain.db.conn.execute("SELECT content FROM qq_group_messages WHERE id=?", (rid,)).fetchone()[0]
    assert c == "[图片：其他：看不出是干嘛的]"


def test_sensitive_line_has_no_quotes():
    from aigroup.chat.vision import clean_info, make_line
    info = clean_info({"kind": "网页截图", "gist": "证件列表页", "text": ["C46199943"], "sensitive": "护照"}, ocr="C46199943")
    assert info["sensitive"] == "证件" and make_line(info, "ok") == "网页截图：证件列表页"
