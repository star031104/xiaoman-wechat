"""31.0 五件事：看视频用宫格图、记事核对原话、主动说话看有没有人接、困意用公式、安排里的打算不能说成做过了。"""
import shutil
import subprocess
import time
from datetime import datetime, timedelta

import pytest

from conftest import FRIEND, FRIEND2, GROUP, _gc


# ---------------- 1. 宫格图 ----------------
def test_find_cuts_and_plan_cells():
    from aigroup.net import storyboard as sb
    t = [i / 2 for i in range(120)]                      # 60 秒，每秒 2 帧
    diff = [2.0] * 120
    for k in (80, 84, 88, 92, 96, 100):                  # 40~50 秒快剪
        diff[k] = 60.0
    diff[20] = 50.0                                      # 10 秒一个切点
    cuts = sb.find_cuts(t, diff)
    assert 10.0 in cuts and 40.0 in cuts and len(cuts) == 7
    cells = sb.plan_cells(60, cuts, 8)
    assert len(cells) == 8 and cells[0][0] == 0 and cells[-1][1] == 60
    fast = [b - a for a, b in cells if a >= 38 and b <= 52]
    slow = [b - a for a, b in cells if b <= 38]
    assert fast and slow and max(fast) < max(slow)       # 快剪的地方格子短
    # 画面几乎不动：格数减半
    assert len(sb.plan_cells(60, [], 8, motion_p90=1.0)) == 4


def test_cells_follow_talk_density():
    from aigroup.net import storyboard as sb
    lines = "[00:00] 嗯\n[00:20] " + "说了很多很多话" * 20 + "\n[00:40] 啊"
    cells = sb.plan_cells(60, [], 6, sb.talk_from_lines(lines))
    mid = [b - a for a, b in cells if a >= 18 and b <= 42]
    assert mid and max(mid) < 60 / 6                    # 话密的那 20 秒格子比平均短
    assert "说了很多" in sb.lines_in(lines, 25, 30)


def test_pick_moment_prefers_sharp_and_avoids_cut():
    from aigroup.net import storyboard as sb
    t = [i / 2 for i in range(40)]
    sharp = [10.0] * 40
    sharp[int(7.5 * 2)] = 99.0                          # 7.5 秒那帧最清楚
    assert sb.pick_moment((0.0, 10.0), t, sharp, []) == 7.5
    assert sb.pick_moment((0.0, 10.0), t, sharp, [7.3]) != 7.5        # 刚切完 0.2 秒：转场，不选


def test_compose_grid_jpeg():
    from io import BytesIO

    from PIL import Image

    from aigroup.net import storyboard as sb
    shots = []
    for k in range(5):
        b = BytesIO()
        Image.new("RGB", (480, 270), (40 * k, 100, 200)).save(b, "JPEG")
        shots.append((k + 1, b.getvalue(), f"00:{k:02d}"))
    out = sb.compose(shots)
    im = Image.open(BytesIO(out))
    assert out[:2] == b"\xff\xd8" and im.width > 3 * sb.CELL_W and im.height > 2 * sb.CELL_H


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="没有 ffmpeg")
def test_scan_finds_color_cuts(tmp_path):
    """同样亮度换颜色（红 → 绿 → 蓝）也要找得到切点。"""
    import asyncio

    from aigroup.net import storyboard as sb
    src = tmp_path / "v.mp4"
    parts = []
    for c in ("0xC03030", "0x30A030", "0x3030C0"):
        parts += ["-f", "lavfi", "-i", f"color=c={c}:s=160x90:d=2:r=10"]
    cmd = ["ffmpeg", "-loglevel", "error", "-y", *parts, "-filter_complex", "[0][1][2]concat=n=3:v=1:a=0", str(src)]
    subprocess.run(cmd, check=True, timeout=60)
    sc = asyncio.run(sb.scan(str(src), 6))
    assert sc and len(sc["t"]) >= 10
    cuts = sb.find_cuts(sc["t"], sc["diff"])
    assert any(abs(c - 2) <= 0.6 for c in cuts) and any(abs(c - 4) <= 0.6 for c in cuts)


def test_zoom_times():
    from aigroup.net import storyboard as sb
    ts = sb.zoom_times(100, 300)
    assert len(ts) == 4 and 97 <= ts[0] < ts[-1] <= 103
    assert sb.zoom_times(1, 300)[0] >= 0


# ---------------- 2. 记事核对原话 ----------------
def _rows():
    return [{"id": 1, "role": "user", "user_id": FRIEND, "content": "我下周开始在腾讯实习了"},
            {"id": 2, "role": "user", "user_id": FRIEND2, "content": "听说小王他打算考研"},
            {"id": 3, "role": "user", "user_id": FRIEND, "content": "我打算下个月去成都玩"},
            {"id": 4, "role": "user", "user_id": FRIEND, "content": "今天有点感冒"},
            {"id": 5, "role": "user", "user_id": FRIEND, "content": "我一点都不喜欢香菜"},
            {"id": 6, "role": "user", "user_id": FRIEND, "content": "假如我是富婆我就天天喝奶茶"}]


def test_vet_fact_rules():
    from aigroup.memory.facts import vet_fact
    rows = _rows()
    v = vet_fact("在腾讯实习", "在腾讯实习了", FRIEND, "self", rows)
    assert v["row"]["id"] == 1 and v["src"] == "self" and not v["why"]
    v = vet_fact("在阿里实习", "我在阿里实习", FRIEND, "self", rows)      # 原话里没有
    assert v["row"] is None and v["src"] == "guess"
    v = vet_fact("在准备考研", "他打算考研", FRIEND, "self", rows)        # 别人转述的
    assert v["src"] == "other"
    v = vet_fact("下个月去成都玩", "下个月去成都玩", FRIEND, "self", rows)
    assert v["until_days"] == 14                                        # 打算：两周后过期
    v = vet_fact("感冒了", "今天有点感冒", FRIEND, "self", rows)
    assert v["until_days"] == 3                                         # 临时状态
    v = vet_fact("喜欢香菜", "不喜欢香菜", FRIEND, "self", rows)          # 否定对不上
    assert v["src"] == "guess"
    v = vet_fact("想天天喝奶茶", "天天喝奶茶", FRIEND, "self", rows)       # 假设
    assert v["src"] == "guess"


def test_single_value_attr_replaces_old(cfg, brain):
    gc, _ = _gc(cfg, brain)
    m = gc.memory
    a = m.add_fact(GROUP, "在腾讯实习", subject=FRIEND, source="self")
    b = m.add_fact(GROUP, "喜欢吃火锅", subject=FRIEND, source="self")
    c = m.add_fact(GROUP, "在字节跳动实习", subject=FRIEND, source="self")
    assert a and b and c
    live = {f["id"] for f in m.facts(GROUP, subject=FRIEND)}
    assert c in live and b in live and a not in live                   # 实习换了：旧的失效；喜好不受影响
    m.add_fact(GROUP, "好像在美团实习", subject=FRIEND, source="guess")    # 猜的不顶掉真的
    assert c in {f["id"] for f in m.facts(GROUP, subject=FRIEND)}


def test_digest_uses_quote(cfg, brain):
    """整理时带了原话：对得上的挂门牌、来源照原话判；对不上的只能算猜。"""
    import asyncio
    gc, _ = _gc(cfg, brain)
    now = time.time()
    for i, (uid, txt) in enumerate([(FRIEND, "我下周开始在腾讯实习了"), (FRIEND2, "听说小王他打算考研")]):
        gc._record(GROUP, uid, "小王" if uid == FRIEND else "阿杰", txt, 100 + i, ts=now - 60 + i)
    out = (f"事|{FRIEND}|下周开始在腾讯实习|-|自己|-|下周开始在腾讯实习了\n"
           f"事|{FRIEND}|在准备考研|-|自己|-|他打算考研\n"
           f"事|{FRIEND}|养了一只猫|-|自己|-|我养了只猫")

    async def chat(messages, **kw):
        return out
    brain.llm.chat = chat
    asyncio.run(gc._digest_one(GROUP))
    fs = {f["text"]: f for f in gc.memory.facts(GROUP, subject=FRIEND)}
    assert fs["下周开始在腾讯实习"]["source"] == "self" and fs["下周开始在腾讯实习"]["msg_id"]
    assert fs["在准备考研"]["source"] == "other"
    assert fs["养了一只猫"]["source"] == "guess" and not fs["养了一只猫"]["msg_id"]


# ---------------- 3. 主动说话看有没有人接 ----------------
def test_reach_weight(cfg, brain):
    from aigroup.chat.feedback import reach_gap_mult, reach_step
    assert reach_step(0.7, 1.0) == pytest.approx(0.85) and reach_step(0.7, 0.0) == pytest.approx(0.5)
    assert reach_step(0.25, 0.0) == 0.2 and reach_step(0.95, 1.0) == 1.0 and reach_step(0.7, 0.2) == 0.7
    assert reach_gap_mult(0) == 1.0 and reach_gap_mult(2) == pytest.approx(1.7) and reach_gap_mult(9) == 2.2
    gc, _ = _gc(cfg, brain)
    now = time.time()
    for _ in range(3):
        gc._reach_update(GROUP, 0.0, now)
    w, streak = gc.reach(GROUP, now)
    assert w == pytest.approx(0.2) and streak == 3
    assert gc.reach_factor(GROUP, now) == pytest.approx(0.2 / 0.7, abs=1e-3)
    w2, _ = gc.reach(GROUP, now + 24 * 3600)                             # 一天没新反馈：往 0.7 回一半
    assert w2 == pytest.approx(0.45, abs=0.01)
    gc._reach_update(GROUP, 1.0, now)
    assert gc.reach(GROUP, now)[1] == 0
    gc2, _ = _gc(cfg, brain, reach_gate=False)
    assert gc2.reach_factor(GROUP, now) == 1.0


def test_evaluate_effects_updates_reach(cfg, brain):
    gc, _ = _gc(cfg, brain)
    now = time.time()
    gc.db.conn.execute("INSERT INTO qq_reply_effect(ts, group_id, mentioned, her_ids, text) VALUES (?,?,?,?,?)",
                       (now - 2000, GROUP, 0, "[]", "有人看今晚的比赛吗"))
    gc.db.conn.commit()
    gc.evaluate_effects(now)
    w, streak = gc.reach(GROUP, now)
    assert w == pytest.approx(0.5) and streak == 1


# ---------------- 4. 困意 ----------------
def test_sleep_cycle():
    from zoneinfo import ZoneInfo

    from aigroup.chat.body import VERY_SLEEPY, body_line, sleep_cycle
    tz = ZoneInfo("Asia/Shanghai")
    now = datetime(2026, 10, 30, 15, 0, tzinfo=tz)
    c = sleep_cycle(now, [])
    assert c["slept_min"] == 450 and c["awake_h"] == 7.0 and c["pressure"] < 45
    assert "困" not in body_line(now, c)
    late = (now.replace(hour=2, minute=40)).timestamp()                   # 昨晚聊到 2:40
    c2 = sleep_cycle(now.replace(hour=21), [late])
    assert c2["slept_min"] == 320 and c2["pressure"] >= VERY_SLEEPY
    assert "困得不行" in body_line(now.replace(hour=21), c2)
    early = now.replace(hour=6, minute=30).timestamp()                     # 今天 6:30 就开口了
    c3 = sleep_cycle(now.replace(hour=7), [early])
    assert c3["wake"].hour == 6 and "刚起床" in body_line(now.replace(hour=7), c3)
    mid = sleep_cycle(now.replace(day=31, hour=1, minute=0), [])          # 凌晨 1 点还没睡：算前一天醒的
    assert mid["awake_h"] == 17.0 and mid["pressure"] >= VERY_SLEEPY
    assert "饿" in body_line(now.replace(hour=12), sleep_cycle(now.replace(hour=12), []))


def test_body_state_in_prompt(cfg, brain):
    gc, _ = _gc(cfg, brain)
    s = gc.body_state(GROUP)
    assert isinstance(s, str) and s
    gc2, _ = _gc(cfg, brain, body_clock=False)
    assert gc2.body_state(GROUP) == brain.mood


# ---------------- 5. 安排里的打算不能说成做过了 ----------------
def test_life_done(cfg, brain):
    gc, _ = _gc(cfg, brain)
    fixed = datetime.now(cfg.tz).replace(hour=15, minute=10)
    brain.now = lambda: fixed
    now = brain.now()
    past = (now - timedelta(hours=3)).replace(minute=0)
    later = (now + timedelta(hours=2)).replace(minute=0)
    plan = (f"{past:%H:%M}-{(past + timedelta(hours=1)):%H:%M} 想去那家火锅店排个号\n"
            f"{past:%H:%M}-{(past + timedelta(minutes=50)):%H:%M} 在宿舍改简历\n"
            f"{later:%H:%M}-{(later + timedelta(hours=1)):%H:%M} 去体育馆打羽毛球")
    gc.db.set(f"life_{now:%Y-%m-%d}", plan)
    assert gc._life_done(GROUP, "我刚去吃了火锅，排了好久")             # 安排里只是“想去”
    assert gc._life_done(GROUP, "我刚去体育馆打完羽毛球，累死")                   # 还没到点
    assert not gc._life_done(GROUP, "我想去吃火锅")                     # 说的就是打算
    assert not gc._life_done(GROUP, "我刚在宿舍改完简历")                       # 已经过了的安排：可以说
    gc._record(GROUP, FRIEND, "小王", "你不是中午去吃火锅了吗", 501, ts=fixed.timestamp() - 600)
    assert not gc._life_done(GROUP, "我刚去吃了火锅，排了好久")         # 群里今天聊过（有来源）就不拦
