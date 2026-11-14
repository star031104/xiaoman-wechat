"""36.6：“？”只算紧跟她的那个、在问她不破刷屏硬刹车、自检 / 规则看线上人设、发版记录记真提交号、微信语音（听 / 回）。"""
import asyncio
import base64
import io
import subprocess
import time
import types
import wave
from datetime import datetime

from conftest import FRIEND, FRIEND2, GROUP, SELF, _gc


def _wav(sec=0.2, rate=16000) -> bytes:
    b = io.BytesIO()
    with wave.open(b, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\0\0" * int(sec * rate))
    return b.getvalue()


# ---------- 一1 “？” ----------
def test_question_mark_right_after_her_is_weak_annoyed(cfg, brain):
    gc, _ = _gc(cfg, brain, room_rules=True, social_read="on")
    now = time.time()
    gc._record(GROUP, SELF, "小满", "怪我嘴快，扎完给揉揉", 1, role="assistant", ts=now - 6)
    gc._record(GROUP, FRIEND, "小王", "？", 2, ts=now - 1)
    d = gc.room_rules(GROUP, {}, now)
    assert d["about_her"] == "annoyed" and d.get("src") == "rule"
    skip, _ = gc.room_verdict(GROUP, d, mentioned=False, guess="", now=now)
    assert skip and not gc.hushed(GROUP, now)                  # 只这一轮不接，不进安静期


def test_question_mark_to_someone_else_not_annoyed(cfg, brain):
    gc, _ = _gc(cfg, brain, room_rules=True)
    now = time.time()
    gc._record(GROUP, SELF, "小满", "今晚早点睡", 1, role="assistant", ts=now - 20)
    gc._record(GROUP, FRIEND2, "老张", "明天几点集合", 2, ts=now - 10)
    gc._record(GROUP, FRIEND, "小王", "？", 3, ts=now - 5)       # 中间有别人说话：回的是老张
    assert gc.room_rules(GROUP, {}, now).get("about_her", "none") in ("", "none")
    gc2, _ = _gc(cfg, brain, room_rules=True)
    gc2._record(GROUP, SELF, "小满", "今晚早点睡", 1, role="assistant", ts=now - 20)
    gc2._record(GROUP, FRIEND, "小王", "[回复老张的「明天几点」] ？", 2, ts=now - 5)
    assert gc2.room_rules(GROUP, {}, now).get("about_her", "none") in ("", "none")


# ---------- 一2 在问她也过硬刹车 ----------
def test_asked_respects_burst_max():
    from aigroup.chat.plan import decide
    from aigroup.chat.settings import DEFAULTS
    g = dict(DEFAULTS)
    now = time.time()
    ok, _ = decide(mentioned=False, now=now, last_reply_ts=now - 5, replies_10min=4, quiet=False, g=g, p=0.0, thr=0.5,
                   burst=2, asked=True)
    assert ok
    kw = dict(mentioned=False, now=now, last_reply_ts=now - 5, replies_10min=5, quiet=False, g=g, p=0.0, thr=0.5,
              burst=int(g.get("burst_max", 5)), asked=True)
    assert not decide(**kw, asked_weak=True)[0]          # 只靠短问句认出的：过硬刹车
    assert decide(**kw)[0]                               # 带问号的照旧不拦（30.1）


def test_asks_her_strength():
    from aigroup.chat.plan import asks_her
    assert asks_her("你觉得谁能赢？") == "strong" and asks_her("你买啥了") == "weak" and asks_her("好的") == ""


# ---------- 三1 线上人设 ----------
def test_persona_text_includes_v2(cfg, brain):
    gc, _ = _gc(cfg, brain)
    gc.v2_persona = lambda: types.SimpleNamespace(body="她爱存歌单", identity="大学生")
    t = gc._persona_text()
    assert "她爱存歌单" in t and (brain.persona.body or "")[:10] in t


# ---------- 三2 发版记录 ----------
def test_gate_pending_used_on_start(tmp_path):
    from aigroup.core.releases import note_deploy
    root = tmp_path
    subprocess.run(["git", "init", "-q"], cwd=root)
    (root / "CHANGELOG.md").write_text("# 更新记录\n\n## 36.6 — x\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=root)
    subprocess.run(["git", "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-qm", "x"], cwd=root)
    (root / "deploy").mkdir()
    (root / "deploy" / "releases.log").write_text("2026-11-14T19:30 36.5 aeef540 上线\n", encoding="utf-8")
    (root / "deploy" / ".gate_pending").write_text("36.6 hotfix\n", encoding="utf-8")
    line = note_deploy(root, datetime(2026, 11, 15, 1, 0))
    assert line.startswith("2026-11-15T01:00 36.6 ") and line.strip().endswith("上线 hotfix")
    assert not (root / "deploy" / ".gate_pending").exists()


# ---------- 六 语音 ----------
def test_silk_to_wav_rejects_non_silk():
    from aigroup.net.voice import silk_to_wav
    assert silk_to_wav(b"not silk") is None


def test_hearing_prefers_deepseek_for_wav(monkeypatch):
    from aigroup.net.voice import Hearing
    cfg = {"llm": {"profiles": {"d": {"base_url": "https://api.deepseek.com/v1", "api_key": "k"}}}, "net": {}}
    h = Hearing(cfg)
    assert h.backend == "deepseek" and h.ready

    async def fake(self, wav):
        return "今天赢了"
    monkeypatch.setattr(Hearing, "hear_deepseek", fake)
    assert asyncio.run(h.hear(_wav())) == ("今天赢了", [])


def test_voice_deepseek_backend(monkeypatch):
    from aigroup.net.voice import Voice
    cfg = {"llm": {"profiles": {"d": {"base_url": "https://api.deepseek.com/v1", "api_key": "k"}}}, "net": {}}
    v = Voice(cfg)
    assert v.backend == "deepseek" and v.ready and v.voice_id == "stella"

    async def fake(self, text):
        return _wav()
    monkeypatch.setattr(Voice, "synth_deepseek", fake)
    assert asyncio.run(v.synth("说句话听听"))[:4] == b"RIFF"


def test_wechat_render_voice_as_file(tmp_path):
    from aigroup.wechat.bot import WeChatBot
    fake = types.SimpleNamespace(chat_of=lambda g: "x@chatroom", opts={"outbox": str(tmp_path)})
    text, files = WeChatBot.render(fake, 1, [{"type": "record", "data": {"file": "base64://" + base64.b64encode(_wav()).decode()}}])
    assert text == "" and len(files) == 1
    p = __import__("pathlib").Path(files[0])
    assert p.parent.name.startswith("aiwx_") and "语音" in p.name and p.stat().st_size > 0


def test_wechat_voice_event():
    from aigroup.wechat.bot import WeChatBot
    from aigroup.wechat.wxdb import WxMsg
    m = WxMsg(chat="g@chatroom", local_id=7, server_id=99, seq=1, ts=100, type=34, sub=0, sender="wxid_a", text="[语音]")
    fake = types.SimpleNamespace(gid=lambda c: 1, ids=types.SimpleNamespace(id=lambda u: 5), self_id=2, self_wxid="me",
                                 data=types.SimpleNamespace(display=lambda u, c: "阿杰", contacts={}))
    ev = WeChatBot.to_event(fake, m)
    seg = [s for s in ev["message"] if s["type"] == "record"]
    assert seg and seg[0]["data"]["file"] == "wxvoice:g@chatroom:99:7:100"
