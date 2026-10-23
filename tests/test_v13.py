"""13.0：看图 / 画图 / 向量走接口、图上叠中文字、整理的输出更宽松地解析。"""
import asyncio
import io

import pytest

from conftest import GROUP, _gc


class _Resp:
    def __init__(self, data, content=b""):
        self._d, self.content = data, content

    def raise_for_status(self):
        pass

    def json(self):
        return self._d


def _png(w=200, h=120):
    from PIL import Image
    b = io.BytesIO()
    Image.new("RGB", (w, h), "skyblue").save(b, "PNG")
    return b.getvalue()


def test_image_ref_sends_refs_without_size(cfg, brain, monkeypatch, tmp_path):
    import httpx

    from aigroup.net.photo import PhotoMaker
    cfg["llm"]["profiles"] = {"z": {"base_url": "https://x/v1", "api_key": "k", "model": "Tongyi-MAI/Z-Image-Turbo"},
                              "e": {"base_url": "https://x/v1", "api_key": "k", "model": "Qwen/Qwen-Image-Edit-2509",
                                    "image_price": 0.04}}
    cfg["llm"]["routes"] = {**cfg["llm"]["routes"], "image": ["z"], "image_ref": ["e"]}
    sent = []

    class C:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None, json=None):
            sent.append(json)
            import base64
            return _Resp({"images": [{"b64_json": base64.b64encode(_png()).decode()}]})
    monkeypatch.setattr(httpx, "AsyncClient", C)
    pm = PhotoMaker(cfg, brain.db, brain.persona, per_day=3)
    assert not pm.can_draw_self                       # 还没有参考图
    (tmp_path / "data" / "avatar").mkdir(parents=True)
    (tmp_path / "data" / "avatar" / "a.png").write_bytes(_png())
    (tmp_path / "data" / "avatar" / "b.png").write_bytes(_png())
    assert pm.can_draw_self
    out = asyncio.run(pm.make("在图书馆看书", with_self=True))
    assert out and sent[-1]["model"].endswith("Edit-2509")
    assert sent[-1]["image"].startswith("data:image/png;base64,") and "image2" in sent[-1]
    assert "image_size" not in sent[-1] and "不要出现任何文字" in sent[-1]["prompt"]
    asyncio.run(pm.make("窗外的晚霞"))
    assert sent[-1]["model"] == "Tongyi-MAI/Z-Image-Turbo" and sent[-1]["image_size"]
    assert pm.remaining_today() == 1


def test_overlay_text_needs_font(monkeypatch):
    from aigroup.net import overlay
    monkeypatch.setattr(overlay, "find_font", lambda prefer="": None)
    assert overlay.overlay_text(_png(), "生日快乐") is None          # 没字体：调用方发原图
    import glob
    fonts = glob.glob("/usr/share/fonts/**/*.tt[fc]", recursive=True)
    if not fonts:
        pytest.skip("这台机器没有字体")
    monkeypatch.setattr(overlay, "find_font", lambda prefer="": fonts[0])
    out = overlay.overlay_text(_png(400, 300), "生日快乐呀")
    from PIL import Image
    assert out and Image.open(io.BytesIO(out)).size == (400, 300)


def test_api_embedder(cfg, brain, monkeypatch):
    import httpx

    from aigroup.memory.facts import APIEmbedder, load_embedder
    cfg["llm"]["profiles"] = {"emb": {"base_url": "https://x/v1", "api_key": "k", "model": "BAAI/bge-m3"}}
    cfg["llm"]["routes"] = {**cfg["llm"]["routes"], "embed": ["emb"]}
    calls = []

    class C:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, headers=None, json=None):
            calls.append(json)
            return _Resp({"data": [{"index": i, "embedding": [1.0, float(i), 0.0]} for i, _ in enumerate(json["input"])]})
    monkeypatch.setattr(httpx, "Client", C)
    e = load_embedder("", cfg)
    assert isinstance(e, APIEmbedder)
    v = e.encode(["a", "b"])
    assert v.shape == (2, 3) and abs(float((v[1] ** 2).sum()) - 1) < 1e-5
    e.encode(["猫"])
    e.encode(["猫"])
    assert len(calls) == 2                               # 同一个查询句只算一次
    gc, ob = _gc(cfg, brain)
    assert isinstance(gc.memory.embedder, APIEmbedder)
    gc._record(GROUP, 88888, "小王", "我家的橘猫叫团子", 1)
    assert gc.memory.embed_pending(10) == 1


def test_look_caches_and_uses_group_prompt(cfg, brain, monkeypatch):
    gc, ob = _gc(cfg, brain)
    from aigroup.chat import ingest
    monkeypatch.setattr(ingest, "fetch_bytes", lambda url, **k: asyncio.sleep(0, b"IMG"))
    seen = []

    async def d(b64, hint="", prompt=""):
        seen.append(prompt)
        return "截图：「明天交作业」，像在说小王（把握低）"
    brain.llm.describe_image = d
    a = asyncio.run(gc._look("http://x/1.png", "阿杰", GROUP))
    b = asyncio.run(gc._look("http://x/2.png", "阿杰", GROUP))
    assert a == b and len(seen) == 1 and "阿杰发的" in seen[0] and "把握" in seen[0]


# ---------------- 13.0 开口 ----------------
def test_quick_judge(cfg, brain):
    from conftest import FakeLLM

    class D(FakeLLM):
        async def chat(self, messages, **kw):
            if "动作只能选一个" in messages[-1]["content"]:          # 20.0：理由 + 动作；老的一个字也还认
                assert kw.get("role") == "gate"
                return "说"
            return await super().chat(messages, **kw)
    brain.llm = D()
    gc, ob = _gc(cfg, brain)
    gc._record(GROUP, 88888, "小王", "有人知道这个怎么弄吗", 1)
    assert asyncio.run(gc.quick_judge(GROUP)) == "say"


def test_unprompted_daily_cap(cfg, brain):
    import time
    gc, ob = _gc(cfg, brain, unprompted_per_day=2)
    for _ in range(2):
        brain.db.conn.execute("INSERT INTO qq_reply_effect(ts, group_id, mentioned) VALUES (?,?,0)", (time.time(), GROUP))
    brain.db.conn.commit()
    gc._record(GROUP, 88888, "小王", "今天好热", 1)
    n = len(brain.llm.group_calls)
    assert asyncio.run(gc.speak(GROUP, mentioned=False, owner_spoke=False, trigger=None, who="")) is False
    assert len(brain.llm.group_calls) == n and not ob.sent
    assert asyncio.run(gc.speak(GROUP, mentioned=True, owner_spoke=False, trigger=None, who="小王")) is True


def test_rethink_when_new_messages_arrive(cfg, brain):
    from conftest import FakeLLM, as_plan

    class D(FakeLLM):
        async def chat(self, messages, **kw):
            last = messages[-1]["content"]
            if "最近的群聊记录" in last:
                self.group_calls.append(messages)
                if len(self.group_calls) == 1:                  # 想的时候群里又来了两句，其中一句叫她
                    gc._record(GROUP, 88888, "小王", "满满你说呢", 2, to_her=True)
                    gc._record(GROUP, 66666, "阿杰", "对啊", 3)
                    gc._st(GROUP).batch_mentioned = True
                    return as_plan("我觉得都行")
                assert "满满你说呢" in last
                return as_plan("那就周六吧")
            return await super().chat(messages, **kw)
    brain.llm = D()
    gc, ob = _gc(cfg, brain, rethink_max=2)          # 23.0 默认不重想了；开着时照旧
    gc._record(GROUP, 88888, "小王", "周六还是周日", 1)
    assert asyncio.run(gc.speak(GROUP, mentioned=False, owner_spoke=False, trigger=None, who="")) is True
    assert len(brain.llm.group_calls) == 2
    sent = [s for _, segs in ob.sent for s in segs if s["type"] == "text"]
    assert sent and "周六" in sent[-1]["data"]["text"] and not gc._st(GROUP).batch_mentioned


def test_idle_aborts_when_group_wakes_up(cfg, brain):
    from conftest import FakeLLM, as_plan

    class D(FakeLLM):
        async def chat(self, messages, **kw):
            if "最近的群聊记录" in messages[-1]["content"]:
                gc._record(GROUP, 88888, "小王", "来了来了", 2)
                gc._record(GROUP, 66666, "阿杰", "开黑", 3)
                return as_plan("好安静啊")
            return await super().chat(messages, **kw)
    brain.llm = D()
    gc, ob = _gc(cfg, brain)
    gc._record(GROUP, 88888, "小王", "晚安", 1)
    assert asyncio.run(gc.speak(GROUP, mentioned=False, owner_spoke=False, trigger=None, who="",
                                why="群里已经安静了 3 个多小时")) is False
    assert not ob.sent


def test_transcript_marks_old_lines(cfg, brain):
    import time
    gc, ob = _gc(cfg, brain)
    gc._record(GROUP, 88888, "小王", "早", 1, ts=time.time() - 2 * 86400)
    gc._record(GROUP, 88888, "小王", "在吗", 2, ts=time.time() - 2 * 3600)
    t = gc.transcript(GROUP, 10)
    assert "过了" in t and "以前的了" in t
    import re
    assert re.search(r"\[\d\d-\d\d \d\d:\d\d\] 小王", t)          # 不是今天的带日期


# ---------------- 13.0 语音 ----------------
def test_parse_sensevoice():
    from aigroup.net.voice import parse_sensevoice, voice_text
    t, tags = parse_sensevoice("😀卧槽今天赢了😊")
    assert t == "卧槽今天赢了" and tags == ["带笑", "听起来挺开心"]
    assert voice_text(t, tags, 8.2) == "[语音 8 秒，带笑，听起来挺开心：卧槽今天赢了]"
    assert voice_text("", [], None) == "[语音，没听清]"


def test_voice_message_heard(cfg, brain):
    from conftest import _run
    gc, ob = _gc(cfg, brain)

    class H:
        ready = True

        async def fetch_qq(self, ob, file, url):
            return b"MP3"

        async def hear(self, data):
            return "周六去爬山吗", ["听起来挺开心"]
    gc.hearing = H()
    ev = {"post_type": "message", "message_type": "group", "group_id": GROUP, "user_id": 88888, "message_id": 5,
          "sender": {"card": "小王"}, "message": [{"type": "record", "data": {"file": "a.amr"}}]}
    _run(gc, ev)
    row = brain.db.conn.execute("SELECT content FROM qq_group_messages WHERE qq_msg_id=5").fetchone()
    assert "周六去爬山吗" in row["content"] and "开心" in row["content"]


def test_voice_reply_when_asked(cfg, brain):
    gc, ob = _gc(cfg, brain, reply="来啦来啦")

    class V:
        ready, per_day = True, 2

        async def synth(self, text):
            return b"RIFFxxxxWAVE"
    gc.voice = V()
    gc._record(GROUP, 88888, "小王", "满满发个语音听听", 1, to_her=True)
    asyncio.run(gc.speak(GROUP, mentioned=True, owner_spoke=False, trigger=1, who="小王", query="满满发个语音听听"))
    assert any(s["type"] == "record" for _, segs in ob.sent for s in segs)
    ob.sent.clear()
    asyncio.run(gc.speak(GROUP, mentioned=True, owner_spoke=False, trigger=1, who="小王", query="满满你好"))
    assert not any(s["type"] == "record" for _, segs in ob.sent for s in segs)


# ---------------- 13.0 更懂人 ----------------
def test_fingerprint_and_audit():
    import time as _t

    from aigroup.memory.style import audit, fingerprint, fingerprint_line
    now = _t.time()
    rows = [(now - i * 60, "[表情包]" if i % 4 == 0 else "哈哈哈好" if i % 3 == 0 else "今天吃啥") for i in range(40)]
    fp = fingerprint(rows)
    assert fp["n"] == 40 and fp["sticker"] >= 0.2 and fp["avg_len"] < 6
    line = fingerprint_line(fp)
    assert "爱发表情包" in line and "很短" in line
    assert fingerprint(rows[:10]) == {}
    her = ["这个问题其实可以从好几个方面来看，首先是时间安排其次是预算，希望对你有帮助"] * 4 + ["好的你呢？"] * 5
    notes = audit(her, ["好", "哈哈", "走起"] * 10, ["好的"])
    assert any("说短一点" in n for n in notes) and any("客服" in n for n in notes)
    assert audit(her[:3], [], []) == []


def test_profile_versions_and_restore(cfg, brain):
    gc, ob = _gc(cfg, brain)
    gc._record(GROUP, 88888, "小王", "hi", 1)
    gc._set_profile(88888, GROUP, {"印象": "爱吐槽"}, "digest")
    gc._set_profile(88888, GROUP, {"印象": "爱吐槽，在考研", "看法": "支持 AG"}, "digest")
    vs = gc.profile_versions(88888, GROUP)
    assert len(vs) == 1 and vs[0]["profile"] == {"印象": "爱吐槽"}
    from aigroup.memory.style import profile_diff
    assert profile_diff({"印象": "a"}, {"印象": "b", "看法": "c"}) == ["印象：a → b", "+看法：c"]
    from aigroup.memory.people import profile_line
    assert "看法：支持 AG" in profile_line("小王", {"看法": "支持 AG"}, "很熟", "")


def test_label_moment(cfg, brain):
    from aigroup.eval.label import answer, next_moment, stats
    gc, ob = _gc(cfg, brain)
    gc._record(GROUP, 88888, "小王", "有人打球吗", 1)
    gid = gc.gate.add(GROUP, False, 0.3, 0.35, False, {"question": 1.0}, judge="skip")
    m = next_moment(brain.db, GROUP)
    assert m["id"] == gid and "有人打球吗" in m["context"][-1] and "没开口" in m["did"]
    assert any("不说" in x for x in m["steps"]) and any("有人在问" in x for x in m["steps"])     # 20.0 决定链
    answer(brain.db, gid, 1)
    assert stats(brain.db)["n"] == 1 and next_moment(brain.db, GROUP) is None
    from aigroup.chat.gate import labeled_rows
    assert any(y == 1.0 and w == 2.0 for _, y, w in labeled_rows(brain.db))


def test_style_block_in_prompt(cfg, brain):
    gc, ob = _gc(cfg, brain)
    brain.db.set(f"qq_style_note_{GROUP}", {"day": "x", "notes": ["说短一点"]})
    m = asyncio.run(gc.build_messages(GROUP, False, False, ""))
    assert "说短一点" in m[0]["content"]


def test_cheer_before_exam(cfg, brain):
    import time as _t
    gc, ob = _gc(cfg, brain, reply="明天考试冲冲冲")
    gc._record(GROUP, 88888, "小王", "我明天考试", 1)
    brain.db.conn.execute("INSERT INTO qq_plans(group_id, user_id, who, what, due, all_day, kind, created) "
                          "VALUES (?,?,?,?,?,?,?,?)", (GROUP, 88888, "小王", "考试", _t.time() + 6 * 3600, 0, "ask", _t.time()))
    brain.db.conn.commit()
    hour = gc.brain.now().hour
    ok = asyncio.run(gc.plans_tick(GROUP, gc._st(GROUP)))
    if 8 <= hour < 23:
        assert ok and ob.sent and "打个气" in brain.llm.group_calls[-1][-1]["content"]
        assert asyncio.run(gc.plans_tick(GROUP, gc._st(GROUP))) is False          # 一件事只打一次气


# ---------------- 13.0 隐私 ----------------
def test_mask_pii_on_record(cfg, brain):
    from aigroup.core.safety import mask_pii
    assert mask_pii("打我电话13812345678") == "打我电话[手机号]"
    assert mask_pii("验证码是 482913 别告诉别人") == "验证码[已隐藏] 别告诉别人"
    assert mask_pii("@10003 群号123456789 2026-10-21") == "@10003 群号123456789 2026-10-21"
    gc, ob = _gc(cfg, brain)
    gc._record(GROUP, 88888, "小王", "我手机 13812345678 身份证440106199901011234", 1)
    row = brain.db.conn.execute("SELECT content FROM qq_group_messages").fetchone()
    assert "138" not in row["content"] and "[身份证号]" in row["content"]


def test_forget_person_and_group(cfg, brain):
    from aigroup.memory.forget import forget_group, forget_person
    gc, ob = _gc(cfg, brain)
    gc._record(GROUP, 88888, "小王", "我养了猫", 1)
    gc._record(GROUP, 66666, "阿杰", "我也是", 2)
    gc._set_profile(88888, GROUP, {"印象": "爱猫"})
    gc.memory.add_fact(GROUP, "小王养了一只猫", subject=88888)
    brain.db.set(f"qq_fp_{GROUP}_88888", {"line": "x"})
    n = forget_person(brain.db, 88888)
    assert n["qq_group_messages"] == 1 and n["qq_facts"] == 1 and n["kv"] >= 1
    assert brain.db.conn.execute("SELECT COUNT(*) FROM qq_group_messages WHERE user_id=88888").fetchone()[0] == 0
    assert brain.db.conn.execute("SELECT COUNT(*) FROM qq_group_messages").fetchone()[0] == 1
    brain.db.set(f"qq_group_notes_{GROUP}", "x")
    forget_group(brain.db, GROUP)
    assert brain.db.conn.execute("SELECT COUNT(*) FROM qq_group_messages").fetchone()[0] == 0
    assert brain.db.get(f"qq_group_notes_{GROUP}") is None


def test_console_hides_chat_text(capsys):
    import logging

    from aigroup.app import _NoChatText
    rec = logging.LogRecord("aigroup.text.group", logging.INFO, "", 0, "原文：%s", ("秘密",), None)
    assert _NoChatText().filter(rec) is False
    rec2 = logging.LogRecord("aigroup.group", logging.INFO, "", 0, "整理", (), None)
    assert _NoChatText().filter(rec2) is True


def test_privacy_scan_rules(tmp_path):
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location("ps", Path(__file__).resolve().parents[1] / "scripts" / "privacy_scan.py")
    ps = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ps)
    ps.ROOT = tmp_path
    f = tmp_path / "a.py"
    f.write_text("k = 'ghp_" + "a" * 36 + "'\nroom = '12345678@chatroom'\nip = '129.1.2.3'\nok = '8.8.8.8'\n"  # privacy: ok
                 "x = '老张'  # privacy: ok\ny = '老张'\n", encoding="utf-8")
    hits = ps.scan([f], ["老张"])
    assert any("GitHub" in h for h in hits) and any("IP" in h for h in hits)
    assert not any("chatroom" in h for h in hits) and sum("私人词" in h for h in hits) == 1


def test_review_fixes(cfg, brain):
    from aigroup.core.safety import mask_pii
    assert mask_pii("联系我test@qq.com就行") == "联系我[邮箱]就行"
    assert mask_pii("我说@小明.你看看这个") == "我说@小明.你看看这个"
    assert mask_pii("4000 5000 6000 次") == "4000 5000 6000 次"
    assert mask_pii("bilibili.com/video/av4012345678901234") == "bilibili.com/video/av4012345678901234"
    assert mask_pii("卡 6222021234567890 好") == "卡 [银行卡号] 好"
    assert "123" not in mask_pii("密码是password123")
    from aigroup.eval.label import answer
    gc, ob = _gc(cfg, brain)
    gid = gc.gate.add(GROUP, False, 0.3, 0.35, False, {"question": 1.0})
    answer(brain.db, gid, None)                                # “看不出来”不能当成标签
    from aigroup.chat.gate import labeled_rows
    assert not [r for r in labeled_rows(brain.db) if r[1] < 0]
    from aigroup.memory.digest import normalize_digest_lines
    assert normalize_digest_lines("3. 群|周六唱K\n4. 群|x") == ["群|周六唱K", "群|x"]


def test_must_bypasses_cap(cfg, brain):
    import time
    gc, ob = _gc(cfg, brain, unprompted_per_day=1)
    brain.db.conn.execute("INSERT INTO qq_reply_effect(ts, group_id, mentioned) VALUES (?,?,0)", (time.time(), GROUP))
    brain.db.conn.commit()
    gc._record(GROUP, 88888, "小王", "hi", 1)
    assert asyncio.run(gc.speak(GROUP, mentioned=False, owner_spoke=False, trigger=None, who="小王",
                                why="今天是小王的生日", must=True)) is True


# ---------------- 13.1 微信专用 ----------------
def test_wx_image_decrypt(tmp_path):
    import hashlib
    import io

    from cryptography.hazmat.primitives import padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from PIL import Image

    from aigroup.wechat.wximg import ImageStore, derive, packed_image_name
    b = io.BytesIO()
    Image.new("RGB", (60, 40), "red").save(b, "JPEG")
    jpg = b.getvalue()
    code, wxid = 3941000000, "wxid_abc123"
    key, xk = derive(code, wxid + "_b929")
    assert key == hashlib.md5(f"{code}{wxid}".encode()).hexdigest()[:16].encode()
    head, tail = jpg[:1024], jpg[1024:]
    pd = padding.PKCS7(128).padder()
    enc = Cipher(algorithms.AES(key), modes.ECB()).encryptor().update(pd.update(head[:1024]) + pd.finalize())
    enc = enc[:1024] if len(head) == 1024 else enc
    dat = (b"\x07\x08V2\x08\x07" + len(enc).to_bytes(4, "little") + len(tail).to_bytes(4, "little") + b"\x01"
           + enc + b"\x00" * 16 + bytes(x ^ xk for x in tail))
    chat, name = "123@chatroom", "a" * 32
    acc = tmp_path / "data" / (wxid + "_b929")
    img_dir = acc / "msg" / "attach" / hashlib.md5(chat.encode()).hexdigest() / "2026-10" / "Img"
    img_dir.mkdir(parents=True)
    (img_dir / f"{name}_t.dat").write_bytes(dat)
    kv = tmp_path / "xwechat" / "net" / "kvcomm"
    kv.mkdir(parents=True)
    (kv / f"key_{code}_1_2.statistic").write_text("")
    st = ImageStore(acc, acc.name, tmp_path / "data")
    import time as _t
    out = st.load(chat, name, _t.mktime((2026, 10, 20, 12, 0, 0, 0, 0, -1)))
    assert out and out[:2] == b"\xff\xd8" and Image.open(io.BytesIO(out)).size == (60, 40)
    assert packed_image_name(b'\x08\x06\x1a"" ' + name.encode() + b"X\x00") == name


def test_stale_batch_and_hours(cfg, brain):
    import time as _t
    gc, ob = _gc(cfg, brain, unprompted_hours="")
    gc._record(GROUP, 88888, "小王", "晚安", 1, ts=_t.time() - 3 * 3600)
    assert gc._quiet_for_unprompted(GROUP, _t.time()) is True
    gc._record(GROUP, 88888, "小王", "又来了", 2)
    assert gc._quiet_for_unprompted(GROUP, _t.time()) is False
    h = gc.brain.now().hour
    gc.g["unprompted_hours"] = f"{(h + 2) % 24:02d}:00-{(h + 3) % 24:02d}:00"
    gc.gover.pop(GROUP, None)
    assert gc._quiet_for_unprompted(GROUP, _t.time()) is True
    gc.g["unprompted_hours"] = f"{(h - 1) % 24:02d}:00-{(h + 1) % 24:02d}:00"
    assert gc._quiet_for_unprompted(GROUP, _t.time()) is False


def test_sticker_described_once(cfg, brain, monkeypatch):
    from aigroup.chat import ingest
    from conftest import _run
    gc, ob = _gc(cfg, brain)
    brain.llm.vision = True
    monkeypatch.setattr(ingest, "fetch_bytes", lambda url, **k: asyncio.sleep(0, b"\\x89PNGxx"))
    calls = []

    async def d(b64, hint="", prompt=""):
        calls.append(prompt)
        return "翻白眼，无语"
    brain.llm.describe_image = d
    for i in (1, 2):
        ev = {"post_type": "message", "message_type": "group", "group_id": GROUP, "user_id": 88888, "message_id": i,
              "sender": {"card": "小王"}, "message": [{"type": "image", "data": {"url": "http://x/s.gif", "file": "abc",
                                                                                "sub_type": 1}}]}
        _run(gc, ev)
    rows = [r["content"] for r in brain.db.conn.execute("SELECT content FROM qq_group_messages")]
    assert rows == ["[表情包：翻白眼，无语]"] * 2 and len(calls) == 1
