"""17.1 发图按图片发（不是文件）；看图前把图换成接口认的格式，认不出的不发给接口，也不让聊天模型冷却。"""
import base64
import io

import pytest
from PIL import Image

from aigroup.llm.client import image_payload
from aigroup.wechat.linux import XSender


def _img(fmt):
    b = io.BytesIO()
    Image.new("RGB", (8, 8), (106, 79, 184)).save(b, fmt)
    return b.getvalue()


def test_image_payload_formats():
    for fmt, mime in (("PNG", "image/png"), ("JPEG", "image/jpeg"), ("WEBP", "image/webp")):
        b64 = base64.b64encode(_img(fmt)).decode()
        assert image_payload(b64) == (b64, mime)
    out, mime = image_payload(base64.b64encode(_img("GIF")).decode())       # GIF 转成第一帧 PNG
    assert mime == "image/png" and base64.b64decode(out)[:4] == b"\x89PNG"
    with pytest.raises(ValueError):
        image_payload(base64.b64encode(b"wxgf\x00\x01garbage").decode())    # 微信自己的格式：不发给接口


def test_vision_400_does_not_cool_chat_model():
    from aigroup.llm import client as C
    src = open(C.__file__, encoding="utf-8").read()
    assert 'bad_img = role == "vision" and "400" in str(e)' in src


def test_paste_image_as_picture(tmp_path):
    calls = []
    s = XSender({})
    s._x = lambda *a, data=None: calls.append((a, data))
    p = tmp_path / "aiwx_1.png"
    p.write_bytes(_img("PNG"))
    s._clip_files([str(p)])
    assert calls[-1][0][:4] == ("xclip", "-selection", "clipboard", "-t") and calls[-1][0][4] == "image/png"
    assert calls[-1][1] == p.read_bytes()
    s.paste_images = False                                                  # 关掉就照旧按文件发
    s._clip_files([str(p)])
    assert "text/uri-list" in calls[-1][0] and calls[-1][1].startswith(b"file://")
    q = tmp_path / "a.txt"
    q.write_text("x")
    s.paste_images = True
    s._clip_files([str(q)])                                                 # 不是图：还是文件
    assert "text/uri-list" in calls[-1][0]


def test_truncated_sticker_not_saved_or_sent(tmp_path):
    from aigroup.core.store import Store
    from aigroup.memory.stickers import Stickers, image_ok
    full = _img("PNG")
    assert image_ok(full) and not image_ok(full[: len(full) // 2]) and not image_ok(b"nope")
    big = io.BytesIO()
    Image.effect_noise((300, 300), 60).convert("RGB").save(big, "PNG")
    assert not image_ok(big.getvalue()[:3412])                              # 线上那张：只下了开头 3KB
    db = Store(tmp_path / "g.db")
    st = Stickers(db, tmp_path)
    db.conn.execute("INSERT INTO qq_stickers(key, url, groups, users, seen, contexts, created, updated, emotion) "
                    "VALUES ('k1', 'u', '[1]', '[]', 3, '[]', 0, 0, '疑惑')")
    assert st.save_file("k1", big.getvalue()[:3412]) == ""                  # 坏的不存
    p = tmp_path / "data" / "stickers" / "k1.png"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(big.getvalue()[:3412])                                    # 以前存坏的
    db.conn.execute("UPDATE qq_stickers SET path='k1.png' WHERE key='k1'")
    assert st.choose(1, "疑惑") is None and not p.exists()
    assert db.conn.execute("SELECT path FROM qq_stickers WHERE key='k1'").fetchone()["path"] == ""
    assert st.save_file("k1", big.getvalue()) == "k1.png" and st.choose(1, "疑惑")["file"].exists()


def test_fetch_bytes_reads_to_end():
    import asyncio

    from aiohttp import web
    from aiohttp.test_utils import TestServer

    from aigroup.onebot import fetch_bytes
    payload = bytes(range(256)) * 4000                                     # 1MB，分好几块到

    async def h(req):
        resp = web.StreamResponse(headers={"Content-Length": str(len(payload))})
        await resp.prepare(req)
        for i in range(0, len(payload), 30000):
            await resp.write(payload[i:i + 30000])
            await asyncio.sleep(0)
        return resp

    async def go():
        app = web.Application()
        app.router.add_get("/a", h)
        async with TestServer(app) as srv:
            got = await fetch_bytes(str(srv.make_url("/a")))
            small = await fetch_bytes(str(srv.make_url("/a")), limit=1000)
        return got, small
    got, small = asyncio.run(go())
    assert got == payload and small is None
