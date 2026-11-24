"""38.1 记忆复核的命令行（每天 18 点 Claude 的定时任务用；人也可以手动用）。不调任何模型。

  python scripts/mem_review.py status                       复核到哪了、还剩多少条、事实里临时 / 复核过的各几条
  python scripts/mem_review.py export [--from ID] [--to ID] [--max 1500] [--out 文件]
                                                            导出要复核的那段（默认：上次复核到的之后）
  python scripts/mem_review.py apply 操作单.json [--dry]     校验并写入（先备份数据库；--dry 只校验不写）
  python scripts/mem_review.py undo RUN                     把某一次复核整个退回
  python scripts/mem_review.py check                        复核后的体检：临时的还剩几条、琐碎的占比、重复的、超长的、看图统计
  python scripts/mem_review.py image ID                     38.2 把第 ID 张图（qq_images）存成 jpg 看（微信的图在 attach 目录，可能要 sudo）

--home 默认 home/wechat；群只有一个时自动认，不然用 --gid。操作单的格式见 docs/记忆复核.md。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def open_db(home: Path):
    from aigroup.core.store import Store
    db = Store(home / "data" / "group.db")
    db.conn.execute("PRAGMA busy_timeout=30000")          # 服务在跑、也在写：等一等别报“被锁”
    return db


def tz_of(home: Path):
    try:
        from aigroup.core.config import load_config
        return load_config(home / "config.yaml")["tz"]
    except Exception:  # noqa: BLE001
        from zoneinfo import ZoneInfo
        return ZoneInfo("Asia/Shanghai")


def pick_gid(db, gid):
    if gid:
        return int(gid)
    gs = [r[0] for r in db.conn.execute("SELECT DISTINCT group_id FROM qq_group_messages")]
    if len(gs) != 1:
        sys.exit(f"有 {len(gs)} 个群，用 --gid 指定：{gs}")
    return int(gs[0])


def her_name(home: Path) -> str:
    try:
        from aigroup.core.config import load_config
        c = load_config(home / "config.yaml")
        return str((c.get("persona") or {}).get("name") or "小满")
    except Exception:  # noqa: BLE001
        return "小满"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["status", "export", "apply", "undo", "check", "image"])
    ap.add_argument("arg", nargs="?")
    ap.add_argument("--home", default="home/wechat")
    ap.add_argument("--gid", type=int)
    ap.add_argument("--from", dest="from_id", type=int)
    ap.add_argument("--to", dest="to_id", type=int)
    ap.add_argument("--max", type=int, default=1500)
    ap.add_argument("--out")
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()
    home = (ROOT / a.home) if not Path(a.home).is_absolute() else Path(a.home)
    db = open_db(home)
    tz = tz_of(home)
    gid = pick_gid(db, a.gid)
    from aigroup.memory import review as R
    from aigroup.memory.facts import Memory
    mem = Memory(db, tz)
    if a.cmd == "status":
        print(json.dumps(R.status(db, gid, tz), ensure_ascii=False, indent=1))
    elif a.cmd == "export":
        data = R.export(db, gid, tz, from_id=a.from_id, to_id=a.to_id, max_msgs=a.max, her="她")
        out = Path(a.out) if a.out else home / "data" / "mem_review" / f"export-{datetime.now(tz):%Y%m%d-%H%M%S}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        out.with_suffix(".txt").write_text(R.render_text(data), encoding="utf-8")
        print(json.dumps({"file": str(out), "text": str(out.with_suffix(".txt")), "from_id": data["from_id"], "to_id": data["to_id"], "messages": len(data["messages"]),
                          "facts": len(data["facts"]), "more": data["more"]}, ensure_ascii=False))
    elif a.cmd == "apply":
        sheet = json.loads(Path(a.arg).read_text(encoding="utf-8"))
        if not a.dry:
            bk = home / "data" / "backup" / f"before-mem-review-{datetime.now(tz):%Y%m%d-%H%M%S}.db"
            bk.parent.mkdir(parents=True, exist_ok=True)
            src = sqlite3.connect(str(home / "data" / "group.db"))
            with sqlite3.connect(str(bk)) as dst:
                src.backup(dst)
            src.close()
            olds = sorted(bk.parent.glob("before-mem-review-*.db"))
            for x in olds[:-14]:                                  # 只留最近 14 份
                x.unlink()
        ap_ = R.Applier(db, mem, gid, tz, dry=a.dry, note=str(sheet.get("note") or ""))
        res = ap_.run(sheet)
        print(json.dumps(res, ensure_ascii=False, indent=1))
    elif a.cmd == "undo":
        print(json.dumps({"undone": R.undo(db, int(a.arg))}, ensure_ascii=False))
    elif a.cmd == "check":
        now = time.time()
        rows = [dict(r) for r in db.conn.execute("SELECT * FROM qq_facts WHERE group_id=? AND (t_invalid IS NULL OR t_invalid>?)",
                                                 (gid, now))]
        from difflib import SequenceMatcher
        dups = []
        for i, x in enumerate(rows):
            for y in rows[i + 1:]:
                if x["subject_id"] == y["subject_id"] and SequenceMatcher(None, x["text"], y["text"]).ratio() >= 0.7:
                    dups.append([x["id"], y["id"]])
        by_imp = {}
        for r in rows:
            by_imp[r.get("importance") or 2] = by_imp.get(r.get("importance") or 2, 0) + 1
        print(json.dumps({"valid": len(rows), "provisional": sum(1 for r in rows if not r.get("review")),
                          "importance": by_imp, "near_dups": dups[:20],
                          "no_quote": sum(1 for r in rows if r.get("review") and not r.get("quote")),
                          "images_7d": image_stats(db, gid), "her_scores": score_stats(db, gid)},
                         ensure_ascii=False, indent=1))
    elif a.cmd == "image":
        out = save_image(db, home, int(a.arg))
        print(json.dumps({"file": str(out) if out else None}, ensure_ascii=False))


def score_stats(db, gid: int) -> dict:
    """38.3 复核时给她打的分：最近 3 天 / 之前 7 天的均分、≤2 分占比、常见毛病、挑句子挑对的比例。"""
    now = time.time()

    def part(a, b):
        rows = db.conn.execute("SELECT score, tags, pick FROM qq_human_score WHERE group_id=? AND src='claude-review' AND ts>? AND ts<=?",
                               (gid, now - a * 86400, now - b * 86400)).fetchall()
        if not rows:
            return {"n": 0}
        tags: dict = {}
        for r in rows:
            for t in [x for x in str(r["tags"] or "").split("、") if x]:
                tags[t] = tags.get(t, 0) + 1
        picks = [r["pick"] for r in rows if r["pick"]]
        return {"n": len(rows), "avg": round(sum(r["score"] for r in rows) / len(rows), 2),
                "low": round(sum(1 for r in rows if r["score"] <= 2) / len(rows), 2),
                "tags": dict(sorted(tags.items(), key=lambda x: -x[1])[:8]),
                "pick_right": round(picks.count("对") / len(picks), 2) if picks else None}
    try:
        return {"last3d": part(3, 0), "prev7d": part(10, 3)}
    except Exception:  # noqa: BLE001
        return {}


def image_stats(db, gid: int) -> dict:
    """38.2 最近 7 天的看图：几张、一开始是缩略图 / 没加载出来的、补到原图的、被问后细看的。"""
    from aigroup.chat.vision import ensure_schema
    ensure_schema(db.conn)
    now = time.time()
    rows = db.conn.execute("SELECT status, recovered, deep FROM qq_images WHERE group_id=? AND ts>? AND msg_row IS NOT NULL",
                           (gid, now - 7 * 86400)).fetchall()
    bad = sum(1 for r in rows if r["status"] != "ok" or r["recovered"])
    rec = sum(1 for r in rows if r["recovered"])
    return {"images": len(rows), "thumb_or_missing": bad, "recovered": rec,
            "still_bad": sum(1 for r in rows if r["status"] != "ok"),
            "deep_looks": sum(len(json.loads(r["deep"] or "[]")) for r in rows)}


def save_image(db, home: Path, img_id: int) -> Path | None:
    """38.2 qq_images 里第 img_id 张图 → data/mem_review/img-<id>.jpg（微信本地的图要解开）。"""
    r = db.conn.execute("SELECT url FROM qq_images WHERE id=?", (img_id,)).fetchone()
    if not r:
        return None
    url = str(r["url"] or "")
    data = None
    if url.startswith("wximg:"):
        import yaml
        from aigroup.wechat.wximg import ImageStore
        _, chat, name, ts = url.split(":", 3)
        cfg = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8")) or {}
        root = Path(str((cfg.get("wechat") or {}).get("data_dir") or ""))
        for acc in sorted(root.glob("wxid_*")):
            st = ImageStore(acc, acc.name, root, None)
            try:
                data = st.load(chat, name, float(ts or 0), True)
            except PermissionError:
                print("没权限读微信的图：用 sudo 跑这条命令")
                return None
            if data:
                break
    elif url.startswith("http"):
        import urllib.request
        data = urllib.request.urlopen(url, timeout=15).read()            # noqa: S310
    if not data:
        return None
    out = home / "data" / "mem_review" / f"img-{img_id}.jpg"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    return out


if __name__ == "__main__":
    main()
