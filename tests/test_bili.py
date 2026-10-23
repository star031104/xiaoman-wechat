"""4.4 B 站视频点开看：wbi 签名、弹幕热段、字幕合并、整个流程（假的网络）。"""
import asyncio
import json

from conftest import FRIEND, GROUP, _gc, _msg, _run


def test_wbi_sign_known_vector():
    from aigroup.net.bili import mixin_key, wbi_sign
    img, sub = "7cd084941338484aae1ad9425b84077c", "4932caff0ff746eab6f01bf08b70ac45"
    assert mixin_key(img, sub) == "ea1db124af3c7062474693fa704f4ff8"
    p = wbi_sign({"foo": "114", "bar": "514", "zab": 1919810}, img, sub, ts=1702204169)
    assert p["w_rid"] == "8f6f2b5b3d485fe1886cec6a0be8c5d4"


def test_danmaku_and_subtitles():
    from aigroup.net.bili import hot_moments, parse_danmaku, subtitle_lines
    xml = "".join(f'<d p="{t},1,25,16777215,0,0,0,0">{w}</d>' for t, w in
                  [(5, "前排")] * 3 + [(190 + i % 10, "哈哈哈哈哈哈") for i in range(12)] + [(195, "名场面")] * 4
                  + [(400, "泪目")] * 5)
    dm = parse_danmaku(xml)
    hm = hot_moments(dm)
    assert hm[0]["t"] == "03:00" or hm[1]["t"] == "03:00"
    assert any("哈哈哈" in m["top"] for m in hm)
    sub = subtitle_lines([{"from": i * 5, "content": f"第{i}句"} for i in range(10)])
    assert sub.startswith("[00:00] 第0句") and "[00:20]" in sub


def _fake_fetch():
    pages = {
        "web-interface/view": {"data": {"aid": 1, "cid": 2, "title": "七日世界禁忌岛实测", "owner": {"name": "某UP"},
                                        "duration": 600, "tname": "游戏", "desc": "带你看看新剧本", "stat": {"view": 9}}},
        "web-interface/nav": {"data": {"wbi_img": {"img_url": "https://i0/7cd084941338484aae1ad9425b84077c.png",
                                                    "sub_url": "https://i0/4932caff0ff746eab6f01bf08b70ac45.png"}}},
        "player/wbi/v2": {"data": {"subtitle": {"subtitles": [{"lan": "ai-zh", "subtitle_url": "//sub/x.json"}]}}},
        "sub/x.json": {"body": [{"from": 0, "content": "今天来试试禁忌岛"}, {"from": 190, "content": "这船开不动啊"}]},
        "v2/reply": {"data": {"replies": [{"content": {"message": "船那段笑死"}, "like": 99}]}},
    }

    async def fetch(url, headers=None):
        if "dm/list.so" in url:
            return ("".join(f'<d p="{190 + i},1">笑死</d>' for i in range(10))).encode()
        for k, v in pages.items():
            if k in url:
                return json.dumps(v).encode()
        raise RuntimeError("没有")
    return fetch


def test_watch_video_flow(cfg, brain):
    gc, ob = _gc(cfg, brain, reply="船那段太好笑了")
    gc.links.fetch_bytes = _fake_fetch()
    seen = {}

    async def js(messages, default=None, role="helper"):
        seen["prompt"] = messages[-1]["content"]
        return {"gist": "博主试玩禁忌岛，造船出海", "points": ["03:10 船开不动"], "highlights": "03:10 船开不动，弹幕刷笑死",
                "vibe": "都在笑那艘船", "keys": ["禁忌岛"], "seen": "full"}
    brain.llm.json = js
    _run(gc, _msg(FRIEND, "小王", "看这个", 1))
    res = asyncio.run(gc.watch_video(GROUP, {"bv": "BV1xx411c7mD", "ok": True}, to_her=True, who="小王"))
    assert "这船开不动" in seen["prompt"] and "笑死" in seen["prompt"] and "船那段笑死" in seen["prompt"]
    assert res["subtitle"] and "船开不动" in res["block"]
    assert any(e["source"] == "video" for e in gc.knowledge.entries(GROUP))
    assert "群里最近分享的视频" in gc.videos_block(GROUP)
    assert "刚看完" in brain.llm.group_calls[-1][-1]["content"]                # 发给她看的：看完说两句
    msgs = asyncio.run(gc.build_messages(GROUP, False, False, "小王"))
    assert "船开不动" in msgs[1]["content"]
    n = len(brain.llm.group_calls)
    asyncio.run(gc.watch_video(GROUP, {"bv": "BV1xx411c7mD", "ok": True}))   # 看过的不再看、没叫她不说
    assert len(brain.llm.group_calls) == n


def test_links_queue_videos(cfg, brain):
    gc, ob = _gc(cfg, brain)
    from aigroup.net.links import LinkReader
    lr = LinkReader(cfg)

    async def read(u):
        return {"ok": True, "kind": "B站视频", "title": "猫", "bv": "BV1xx411c7mD", "site": "B站"}
    lr.read = read
    d = asyncio.run(lr.enrich("https://www.bilibili.com/video/BV1xx411c7mD"))
    assert "正在点开看" in d
    assert [v["bv"] for v in lr.take_videos()] == ["BV1xx411c7mD"] and lr.take_videos() == []


def test_noise_danmaku_filtered():
    from aigroup.net.bili import parse_danmaku
    xml = "".join(f'<d p="1,1">{w}</d>' for w in ["刚刚", "三小时前", "前排", '["0.05","0.1"]', "？？", "笑死我了", "1"])
    assert [t for _, t in parse_danmaku(xml)] == ["笑死我了"]


def test_asr_used_when_no_subtitle(cfg, brain, monkeypatch):
    """没字幕：把音频转成文字再整理（假的语音识别；没有 ffmpeg 时整段转）。"""
    from aigroup.net import bili as B
    fetch = _fake_fetch()

    async def fetch2(url, headers=None):
        if "player/wbi/v2" in url:
            return json.dumps({"data": {"subtitle": {"subtitles": []}}}).encode()
        if "playurl" in url:
            return json.dumps({"data": {"dash": {"audio": [{"baseUrl": "https://cdn/a.m4s", "bandwidth": 6}]}}}).encode()
        if "cdn/a.m4s" in url:
            return b"\x00" * 5000
        return await fetch(url, headers)
    asr = B.ASR({"llm": {"profiles": {"sf": {"base_url": "https://api.siliconflow.cn/v1", "api_key": "k"}}}})
    assert asr.enabled and asr.key == "k"

    async def one(data, name="a.mp3", mime="audio/mpeg"):
        return "兄弟们今天试试禁忌岛🎼"
    asr.one = one
    monkeypatch.setattr(B.shutil, "which", lambda x: None)
    w = asyncio.run(B.Bili(fetch2, None, asr).watch("BV1xx411c7mD"))
    assert w["subtitle"] == "" and w["heard"].startswith("[全程] 兄弟们")
    assert not B.ASR({"llm": {"profiles": {}}}).enabled
