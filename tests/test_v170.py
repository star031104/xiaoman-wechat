"""17.0 小屋 B 版：共用的 tokens.css、立绘 / 头像路由、只读的关系网和相处经验、性格开关还是 <label>。"""
import asyncio
import re

from aiohttp.test_utils import TestClient, TestServer

from aigroup.chat.group import GroupChat
from aigroup.web.server import GroupHome

from conftest import FRIEND, GROUP, OB, OWNER, ProfileLLM


def _app(cfg, brain):
    brain.llm = ProfileLLM()
    ob = OB()
    gc = GroupChat(cfg, brain, ob, OWNER, [GROUP], ["满满"])
    return GroupHome(cfg, brain, ob, gc)


def test_tokens_avatar_insight(cfg, brain):
    home = _app(cfg, brain)
    av = cfg.root / "data" / "avatar"

    async def go():
        async with TestClient(TestServer(home.app())) as c:
            r = await c.get("/tokens.css")
            css = await r.text()
            assert r.status == 200 and "#6A4FB8" in css and 'data-tod="night"' in css
            assert (await c.get("/avatar/portrait")).status == 404           # 还没放图
            av.mkdir(parents=True, exist_ok=True)
            (av / "xiaoman-portrait.webp").write_bytes(b"RIFF0000WEBP")
            assert (await c.get("/avatar/portrait")).status == 200
            assert (await c.get("/avatar/avatar")).status == 200             # 没有头像就用立绘
            assert (await c.get("/avatar/..%2F..%2Fconfig.yaml")).status == 404
            brain.db.set(f"qq_relgraph_{GROUP}", {"ts": 1.0, "pairs": [{"a": "小李", "b": "阿周", "score": 4}]})
            brain.db.set(f"qq_relexp_{GROUP}_{FRIEND}", {"ts": 2.0, "name": "小李", "lines": ["他吃损"], "good": 5, "bad": 1})
            d = await (await c.get(f"/api/insight?gid={GROUP}")).json()
            assert d["pairs"][0]["a"] == "小李" and d["experience"][0]["uid"] == FRIEND
            assert d["experience"][0]["lines"] == ["他吃损"]
            page = await (await c.get("/")).text()
            assert 'href="tokens.css"' in page and 'src="avatar/portrait"' in page
    asyncio.run(go())


def test_page_paths_relative_and_switch_label(cfg, brain):
    from pathlib import Path
    html = (Path(__file__).resolve().parents[1] / "aigroup" / "web" / "page.html").read_text(encoding="utf-8")
    assert not re.search(r"""(src|href)=["']/(api|files)/""", html)          # nginx 占着 /api/、/files/
    assert not re.search(r"""fetch\(\s*["'`]/""", html)
    assert '<label class="switch"' in html                                    # 13.2：点开关整块是 label
    for need in ('id="labCard"', 'data-lab="1"', 'data-ck=', 'id="relPairs"', 'id="relExp"', 'id="wbseg"', 'id="lfProps"',
                 'id="factList"', 'id="evProbe"'):
        assert need in html, need
