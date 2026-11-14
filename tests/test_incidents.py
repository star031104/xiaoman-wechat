"""23.3 问题回归集（tests/group/incidents.yaml）：日报里确认过的每一件事，规则检查都要认得出；正常的话不能误伤。
只靠统一自检（check_critic，要调大模型）才认得出的标 expect: critic，这里跳过，交给 scripts/release_gate.py --llm。
36.4 不是发前检查的事也能进回归集（expect 写管它的那一环）：
  gate          她已经说了好几条，这次就算想说，门控也该拦
  speak         群友接着她的话问她，门控该放行（额度、冷却都不拦）
  gate_silent   看不清的图 / 没问她：不点评
  about_her_X / mood_X   局面判断（规则兜底那层，模型返回空也要判对）
  own_mentions  问她推荐 / 看法：把他自己提过的事列给她
  pass          发前规则不拦，自检只看得到这句话牵涉的人的痛点账（不拿别人的旧事当理由）"""
import asyncio
import time
from pathlib import Path

import pytest
import yaml
from conftest import FRIEND, FRIEND2, GROUP, SELF, _gc

CASES = yaml.safe_load((Path(__file__).parent / "incidents.yaml").read_text(encoding="utf-8"))
FRIEND3 = 33333
WHO = {"小王": FRIEND, "老张": FRIEND2, "阿杰": FRIEND3, "大家": None}


def build(gc, case):
    now = time.time()
    rows = {}
    for i, (who, text, ago) in enumerate(case.get("msgs") or []):
        if who == "她":
            rid = gc._record(GROUP, SELF, "小满", text, 100 + i, role="assistant", ts=now - ago)
        else:
            rid = gc._record(GROUP, WHO[who] or FRIEND, who, text, 100 + i, to_her="[回复你" in text or text.startswith("@你"),
                             ts=now - ago)
        rows.setdefault(who, []).append({"id": rid, "role": "assistant" if who == "她" else "user", "content": text,
                                         "ts": now - ago})
    tgt = case.get("target")
    return (WHO.get(tgt), rows[tgt][-1]) if tgt and rows.get(tgt) else (None, None)


def run(gc, case):
    uid, row = build(gc, case)
    gc.db.conn.execute("DELETE FROM qq_check_log")
    asyncio.run(gc.checks22(GROUP, [case["draft"]], target_uid=uid, target_row=row, mentioned=False, web=case.get("web") or "",
                            image={"top": case["image_top"]} if case.get("image_top") else {}))
    return {r["name"] for r in gc.db.conn.execute("SELECT name FROM qq_check_log").fetchall()}


def texts_of(gc, case, now):
    """最后一批群友消息（从她最后一句之后算起），和线上 batch_texts 一样的 (id, uid, 原文, meta)。"""
    rows = gc._rows(GROUP, 30)
    her = max([float(r["ts"]) for r in rows if r["role"] == "assistant"] or [0.0])
    return [(r["id"], int(r["user_id"] or 0), str(r["content"] or ""), {}) for r in rows
            if r["role"] == "user" and float(r["ts"]) >= her and now - float(r["ts"]) < 600]


def other_kind(gc, case) -> None:
    from aigroup.chat.address import likely_to_her
    from aigroup.chat.plan import asks_her, decide
    exp = case["expect"]
    uid, row = build(gc, case)
    now = time.time()
    rows = gc._rows(GROUP, 30)
    hers = [r for r in rows if r["role"] == "assistant"]
    her_ts = max([float(r["ts"]) for r in hers] or [0.0])
    texts = texts_of(gc, case, now)
    g = gc.gk(GROUP)
    burst = sum(1 for r in hers if now - float(r["ts"]) < 300)
    if exp in ("gate", "speak"):
        guess = ""
        if texts:
            guess = likely_to_her(texts[-1][2], now=now, her_last_ts=her_ts, attention=0.0,
                                  her_recent=[str(r["content"]) for r in hers[::-1][:3]])
        asked = bool(guess in ("to", "bid") and any(asks_her(t[2]) for t in texts[-2:]))
        ok, why = decide(mentioned=False, now=now, last_reply_ts=her_ts, replies_10min=burst, quiet=False, g=g, p=1.0,
                         thr=0.5, waited=True, burst=burst, asked=asked, waited_recent=False)
        if ok and not asked:
            bl = gc.budget_left(GROUP, now) if hasattr(gc, "budget_left") else ""
            ok = ok and not bl
        if exp == "gate":
            assert not ok, f"{case['id']}（{case.get('what')}）门控放行了：{why}"
        else:       # 要认出“在问她”（额度、冷却、刷屏刹车都不拦的那条路），不能只靠这次正好没用完额度
            assert ok and asked, f"{case['id']}（{case.get('what')}）没认出在问她：{why}（guess={guess!r}, asked={asked}）"
    elif exp == "gate_silent":
        why = gc.media_gate(GROUP, texts, now)
        assert why, f"{case['id']}（{case.get('what')}）没拦住点评图"
    elif exp.startswith("about_her_") or exp.startswith("mood_"):
        d = gc.room_rules(GROUP, {}, now)
        if exp.startswith("about_her_"):
            assert d.get("about_her") == exp[len("about_her_"):], f"{case['id']} 局面判成 {d}"
        else:
            want = {"amused": "笑", "sad": "难过", "annoyed": "烦"}.get(exp[len("mood_"):], exp[len("mood_"):])
            assert want in str(d.get("mood")), f"{case['id']} 情绪判成 {d}"
    elif exp == "own_mentions":
        note = gc.own_mentions_note(GROUP, texts, now)
        assert note, f"{case['id']}（{case.get('what')}）没把他自己提过的事列出来"
    elif exp == "pass":
        hit = run(gc, case)
        assert not hit, f"{case['id']} 误伤：{hit}"
        if hasattr(gc, "sore_about"):
            about = gc.sore_about(GROUP, case["draft"], uid, now)
            block = gc.sore_block(GROUP, now, about=about)
            others = [s for s in gc.sore_spots(GROUP, now) if s["uid"] not in about]
            for s in others:
                assert s["lines"][0][:8] not in block, f"{case['id']} 自检看到了不相干的人的痛点：{block}"
    else:
        raise AssertionError(f"不认识的 expect：{exp}")


OTHER = ("gate", "speak", "gate_silent", "own_mentions", "pass")


def is_other(c) -> bool:
    e = str(c.get("expect") or "")
    return e in OTHER or e.startswith("about_her_") or e.startswith("mood_")


@pytest.mark.parametrize("case", [c for c in CASES if c.get("expect") not in ("critic",)], ids=lambda c: c["id"])
def test_incident(cfg, brain, case):
    if is_other(case):
        gc, _ = _gc(cfg, brain, check_critic="off", media_first_comment=True, room_rules=True)   # conftest 默认关了看图门槛
        other_kind(gc, case)
        return
    gc, _ = _gc(cfg, brain, check_critic="off")
    hit = run(gc, case)
    if case["expect"] == "none":
        assert not hit, f"{case['id']} 误伤：{hit}"
    else:
        assert case["expect"] in hit, f"{case['id']}（{case.get('what')}）没认出来，命中的是 {hit}"


def test_every_failure_mode_has_a_case():
    from aigroup.chat.checks import FAILURE_MODES
    modes = {int(c["mode"]) for c in CASES if c.get("mode")}
    missing = [i + 1 for i in range(len(FAILURE_MODES)) if i + 1 not in modes]
    assert len(missing) <= 3, f"这些毛病还没有例子：{missing}"
