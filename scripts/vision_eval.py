"""38.2 看图评测：50 张群里真实的图（聊天截图 10、网页 / 资讯 10、照片 10、梗图 8、图表 4、文档 4、缩略图 4），
标准答案由 Claude 看图写（是什么、关键原字、谁说的、不能说的）。图和答案只放在服务器的数据目录里，不进仓库：
    <home>/data/vision_eval/gold.json    [{"i", "cat", "kind": [可接受的类型], "gist_any": [一句话里该有的词],
                                          "text": [关键原字], "speakers": [["left/right", 那句话的片段]],
                                          "no": [不能说的（编的价格、认错的人）], "sensitive": bool, "thumb": bool}]
    <home>/data/vision_eval/imgs/<i>.jpg

用法（要真的看图模型；在服务器上跑）：
    python scripts/vision_eval.py --home home/wechat                 # 现在的做法（38.2 结构化 + OCR 对字 + 气泡分人）
    python scripts/vision_eval.py --home home/wechat --legacy        # 38.1 以前的做法（一段话），对比用
    python scripts/vision_eval.py --home home/wechat --route sf_qwen3_vl   # 换一个看图接口试（换模型前先跑这个）
指标：类型对不对、原字召回 / 准确、编造率（目标 ≤ 5%）、认错人（目标 0）、缩略图说没说看不清、聊天截图谁说的对不对、
废话率（“看不出是干嘛的”）、敏感图认出来没有、每张多少秒。结果另存 JSON（--out）。
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _hit(snip: str, pool: str) -> bool:
    from aigroup.chat.vision import on_ocr
    return on_ocr(snip, pool, 0.75)


def score_item(g: dict, out: dict, alltext: str) -> dict:
    """一张图的得分。out：{"info", "raw_text", "line", "legacy"}。"""
    from aigroup.chat.vision import _norm
    info = out.get("info") or {}
    line = out.get("line") or ""
    blob = " ".join([line, info.get("gist", ""), *(info.get("sure") or []), *(info.get("text") or [])])
    r: dict = {"i": g["i"], "cat": g["cat"]}
    if not out.get("legacy"):
        r["kind_ok"] = info.get("kind") in g["kind"]
    if g.get("gist_any"):
        r["gist_ok"] = any(_norm(w) in _norm(blob) for w in g["gist_any"])
    gt = g.get("text") or []
    if gt:
        r["text_recall"] = sum(1 for t in gt if _hit(t, blob)) / len(gt)
    said = out.get("raw_text") or []                          # 模型（或清洗后）声称图上写着的字
    pool = alltext + " " + " ".join(gt)
    bad = [t for t in said if not _hit(t, pool)]
    nos = [w for w in g.get("no") or [] if _norm(w) and _norm(w) in _norm(blob)]
    r["text_claims"] = len(said)
    r["text_bad"] = bad
    r["no_hit"] = nos
    r["fabricated"] = bool(bad or nos)
    people = info.get("people") or []
    r["wrong_person"] = bool(people) or bool(re.search(r"像在说.{1,6}（把握", line))
    r["filler"] = bool(re.search(r"看不出(是|具体)?(干嘛|用途|在说)", line))
    if g.get("thumb"):
        r["thumb_honest"] = bool(re.search(r"缩略图|看不清", line)) and not (info.get("text") or [])
    if g.get("speakers"):
        sp = info.get("speakers") or []
        ok = 0
        for side, snip in g["speakers"]:
            m = next((s for s in sp if _hit(snip, s.get("said", "")) or _hit(s.get("said", "")[:12], snip)), None)
            if m and (m.get("side") or ("right" if "自己" in str(m.get("who")) else "left")) == side:
                ok += 1
        r["speaker_acc"] = ok / len(g["speakers"])
    if not out.get("legacy"):
        r["sensitive_pred"] = bool(info.get("sensitive"))
        r["sensitive_gold"] = bool(g.get("sensitive"))
    r["secs"] = out.get("secs", 0)
    r["line"] = line[:120]
    return r


def summary(rows: list[dict]) -> dict:
    def avg(k, sub=None):
        xs = [float(r[k]) for r in (sub or rows) if k in r]
        return round(sum(xs) / len(xs), 3) if xs else None
    s = {"n": len(rows), "kind_acc": avg("kind_ok"), "gist_hit": avg("gist_ok"), "text_recall": avg("text_recall"),
         "fabrication_rate": avg("fabricated"), "wrong_person": sum(1 for r in rows if r.get("wrong_person")),
         "filler_rate": avg("filler"), "thumb_honest": avg("thumb_honest"), "speaker_acc": avg("speaker_acc"),
         "secs_avg": avg("secs")}
    claims = sum(r.get("text_claims", 0) for r in rows)
    s["text_precision"] = round(1 - sum(len(r.get("text_bad") or []) for r in rows) / claims, 3) if claims else None
    tp = sum(1 for r in rows if r.get("sensitive_pred") and r.get("sensitive_gold"))
    pp = sum(1 for r in rows if r.get("sensitive_pred"))
    gp = sum(1 for r in rows if r.get("sensitive_gold"))
    if "sensitive_pred" in (rows[0] if rows else {}):
        s["sensitive_recall"] = round(tp / gp, 2) if gp else None
        s["sensitive_precision"] = round(tp / pp, 2) if pp else None
    s["by_cat"] = {c: {"gist_hit": avg("gist_ok", [r for r in rows if r["cat"] == c]),
                       "text_recall": avg("text_recall", [r for r in rows if r["cat"] == c]),
                       "fabricated": avg("fabricated", [r for r in rows if r["cat"] == c])}
                   for c in dict.fromkeys(r["cat"] for r in rows)}
    return s


async def run_one(llm, g: dict, data: bytes, legacy: bool, names: list[str]) -> tuple[dict, str]:
    from aigroup.chat.ingest import trim_desc
    from aigroup.chat.vision import VISION_PROMPT, clean_info, make_line, parse_json
    from aigroup.net.ocr import chat_layout, lines_text, ocr_lines, render_turns
    thumb = bool(g.get("thumb"))
    lines, arr = ocr_lines(data)
    alltext = lines_text(lines, 4000)
    ocr = "" if thumb else lines_text(lines)
    if len(re.sub(r"\W", "", ocr)) < 6:
        ocr = ""
    t0 = time.time()
    b64 = base64.b64encode(data).decode()
    if legacy:
        from aigroup.llm.client import GROUP_IMAGE_PROMPT
        prompt = GROUP_IMAGE_PROMPT.format(who="群友", names="、".join(names) or "（不知道）")
        if thumb:
            prompt += "\n\n（这是一张很小的缩略图，原图没下下来：只写看得清的大概是什么；看不清的字一个也别写、别猜，别写人名；最后加一句“缩略图，看不清细节”。）"
        if ocr:
            prompt += f"\n\n（OCR 认出的图上原文，要写图上的字只能照这个抄，别自己补：{ocr}）"
        raw = await llm.describe_image(b64, "", prompt=prompt)
        line = trim_desc(raw, 180)
        return {"legacy": True, "line": line, "info": {}, "raw_text": re.findall(r"「([^」]{1,40})」", line),
                "secs": round(time.time() - t0, 1)}, alltext
    turns = [] if thumb or not lines else chat_layout(lines, arr)["turns"]
    prompt = VISION_PROMPT.format(who="群友", ctx="", names="、".join(names) or "（不知道）")
    if thumb:
        prompt += "\n\n（这是一张很小的缩略图，原图没下下来：只写看得清的大概是什么；看不清的字一个也别写、别猜，text、people、speakers 都不写。）"
    if ocr:
        prompt += f"\n\n（OCR 认出的字，可能有错字，只作参考；text 里写你自己在图上看清的：{ocr[:300]}）"
    if turns:
        prompt += f"\n\n（按气泡颜色分的，聊天截图里谁说的：{render_turns(turns, 300)}）"
    raw = await llm.describe_image(b64, "", prompt=prompt)
    js = parse_json(raw) or {}
    info = clean_info(js, ocr=ocr, turns=turns, names=names, thumb=thumb, fallback="" if js else raw[:40])
    status = "thumb" if thumb else "ok"
    return {"line": make_line(info, status), "info": info, "raw_js": js,
            "raw_text": list(info.get("text") or []), "raw_model_text": [str(x) for x in (js.get("text") or [])][:6],
            "secs": round(time.time() - t0, 1)}, alltext


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--home", default="")
    ap.add_argument("--legacy", action="store_true", help="用 38.1 以前的问法（一段话）")
    ap.add_argument("--route", default="", help="只用这个看图接口（config.llm.yaml 里的名字）")
    ap.add_argument("--only", default="", help="只跑这几张（逗号分隔的 i）")
    ap.add_argument("--names", default="", help="群里的名字（逗号分隔），默认从 group.db 取")
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    if a.home:
        os.environ["AIGROUP_HOME"] = str(Path(a.home).expanduser().resolve())
    from aigroup.core.config import load_config
    from aigroup.llm.client import LLM
    cfg = load_config()
    llm = LLM(cfg)
    if a.route:
        llm.routes["vision"] = [a.route]
    base = Path(a.home or ".").expanduser() / "data" / "vision_eval"
    gold = json.loads((base / "gold.json").read_text(encoding="utf-8"))
    if a.only:
        keep = {int(x) for x in a.only.split(",") if x.strip()}
        gold = [g for g in gold if g["i"] in keep]
    names = [x for x in a.names.split(",") if x] or _names(base.parent / "group.db")
    rows, raw = [], []
    for g in gold:
        p = base / "imgs" / f"{g['i']}.jpg"
        if not p.exists():
            print("缺图", p)
            continue
        try:
            out, alltext = await run_one(llm, g, p.read_bytes(), a.legacy, names)
        except Exception as e:  # noqa: BLE001
            print(g["i"], "出错：", e)
            out, alltext = {"line": "", "info": {}, "raw_text": [], "legacy": a.legacy}, ""
        r = score_item(g, out, alltext)
        if not a.legacy:                                         # 模型自己声称的字（清洗前）也算一次编造率
            pool = alltext + " " + " ".join(g.get("text") or [])
            r["raw_text_bad"] = [t for t in out.get("raw_model_text") or [] if not _hit(t, pool)]
        rows.append(r)
        raw.append({"i": g["i"], "out": out})
        print(f"{g['i']:>4} {g['cat']:<6} {r.get('kind_ok', '-')!s:<5} gist={r.get('gist_ok', '-')!s:<5} "
              f"recall={r.get('text_recall', '-')!s:<5} fab={r['fabricated']!s:<5} {r['line'][:60]}")
    s = summary(rows)
    if not a.legacy:
        claims = sum(len(x["out"].get("raw_model_text") or []) for x in raw)
        s["raw_text_precision"] = round(1 - sum(len(r.get("raw_text_bad") or []) for r in rows) / claims, 3) if claims else None
    print(json.dumps(s, ensure_ascii=False, indent=1))
    out = Path(a.out) if a.out else base / f"report-{'legacy' if a.legacy else 'v2'}-{time.strftime('%m%d-%H%M')}.json"
    out.write_text(json.dumps({"summary": s, "rows": rows, "raw": raw}, ensure_ascii=False, indent=1), encoding="utf-8")
    print("结果：", out)


def _names(db: Path) -> list[str]:
    import sqlite3
    try:
        c = sqlite3.connect(str(db))
        return [r[0] for r in c.execute("SELECT name FROM qq_people ORDER BY msgs DESC LIMIT 12")]
    except Exception:  # noqa: BLE001
        return []


if __name__ == "__main__":
    asyncio.run(main())
