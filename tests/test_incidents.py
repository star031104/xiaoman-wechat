"""23.3 问题回归集（tests/group/incidents.yaml）：日报里确认过的每一件事，规则检查都要认得出；正常的话不能误伤。
只靠统一自检（check_critic，要调大模型）才认得出的标 expect: critic，这里跳过，交给 scripts/release_gate.py --llm。"""
import asyncio
import time
from pathlib import Path

import pytest
import yaml
from conftest import FRIEND, FRIEND2, GROUP, SELF, _gc

CASES = yaml.safe_load((Path(__file__).parent / "incidents.yaml").read_text(encoding="utf-8"))
WHO = {"小王": FRIEND, "老张": FRIEND2}


def build(gc, case):
    now = time.time()
    rows = {}
    for i, (who, text, ago) in enumerate(case.get("msgs") or []):
        if who == "她":
            rid = gc._record(GROUP, SELF, "小满", text, 100 + i, role="assistant", ts=now - ago)
        else:
            rid = gc._record(GROUP, WHO[who], who, text, 100 + i, to_her="[回复你" in text or text.startswith("@你"),
                             ts=now - ago)
        rows.setdefault(who, []).append({"id": rid, "role": "assistant" if who == "她" else "user", "content": text,
                                         "ts": now - ago})
    tgt = case.get("target")
    return (WHO.get(tgt), rows[tgt][-1]) if tgt else (None, None)


def run(gc, case):
    uid, row = build(gc, case)
    gc.db.conn.execute("DELETE FROM qq_check_log")
    asyncio.run(gc.checks22(GROUP, [case["draft"]], target_uid=uid, target_row=row, mentioned=False, web=case.get("web") or "",
                            image={"top": case["image_top"]} if case.get("image_top") else {}))
    return {r["name"] for r in gc.db.conn.execute("SELECT name FROM qq_check_log").fetchall()}


@pytest.mark.parametrize("case", [c for c in CASES if c.get("expect") not in ("critic",)], ids=lambda c: c["id"])
def test_incident(cfg, brain, case):
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
