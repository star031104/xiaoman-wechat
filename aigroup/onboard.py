"""5.0 进群前的准备：让她第一天就认识大家（python run.py onboard）。

三步，都可以单独跑：
1. 导出群里以前的聊天记录（只读）
   `wxexport`：从电脑上开着的某个号（一般是你的大号）读本地库，存成 data/export/<时间>/<群名>.txt
2. `seedgen [导出目录]`：用你配的大模型把这些记录整理成一份“认识大家”的资料草稿 data/seed_draft.yaml——
   每个群在聊什么、不成文的规矩、群梗；每个常说话的人的画像（大家怎么叫他、性格、说话风格、兴趣、近况、别碰的话题……）；
   群友之间几乎不叫名字的群，顺手把“叫人名字”关掉。只记聊天里看得出来的，不记电话、住址、证件号这类隐私。
3. 你打开草稿看一遍、改一改（删掉不想让她知道的），再 `seed data/seed_draft.yaml` 灌进去，重启她。

导进去的人都算“事先听说的”：真正和她说上几句话以前，她不会把听来的近况、私事说出来（chat/prompt.py 的“刚认识”）。
一次整理大约 = 群数 + 常说话的人数 次大模型调用（每次几千字），DeepSeek 一共几分钱。
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import datetime
from pathlib import Path

import yaml

from aigroup.core.config import ROOT, load_config
from aigroup.core.util import ask, say, yes

LINE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}) ([^：]{1,40})：(.*)$")
MEMBER_RE = re.compile(r"^- (.*?)｜微信昵称 (.*?)｜备注 (.*?)｜(\S+)｜说了 (\d+) 条$")
NOISE = re.compile(r"^\[(图片|表情|表情包|动画表情|语音|视频|文件|链接|分享|小程序|位置|名片|红包|转账|拍一拍)[^\]]*\]$")

GROUP_PROMPT = """下面是一个{chan}「{name}」以前的聊天记录（节选，按时间）。你要帮一个新来的群友（{her}）提前了解这个群。
只根据记录里看得出来的写，别编；不写电话、住址、证件号、具体的钱数这类隐私。
{her} 自己说的话、大家讨论 {her} 的事（她是谁做的、是不是机器人、怎么测试她）都不要写。只输出 JSON：
{{"notes": ["这个群最近在聊的事、约过的活动、反复出现的话题（每条 25 字以内，最多 10 条）"],
  "norms": ["这个群不成文的规矩和说话习惯（比如：大家互称 X 总 / 一句话拆好几条发 / 爱用某个表情 / 深夜最活跃；每条 25 字以内，最多 6 条）"],
  "lore": [{{"keys": ["梗的关键词"], "text": "这个梗是什么意思、怎么来的（40 字以内）"}}]}}
lore 只写真的反复出现、外人看不懂的梗，最多 8 条，没有就空着。

# 群成员（说话多的在前）
{members}

# 聊天记录
{log}"""

PERSON_PROMPT = """下面是{chan}「{group}」里「{who}」（{ids}）说过的话（节选，带一点上下文），以及群里别人怎么叫他。
你要帮一个新来的群友（{her}）提前了解他。只根据记录里看得出来的写，看不出来的字段就写空字符串；别编；
不写电话、住址、证件号、具体的钱数、身体隐私。只输出 JSON：
{{"aliases": ["群里大家怎么叫他（外号、简称；最多 4 个；没有就空）"],
  "印象": "一句话印象（25 字以内）", "称呼": "大家平时怎么叫他", "性格": "", "说话风格": "（句子长短、爱用的表情、口头禅的用法）",
  "口头禅": "（用 / 隔开）", "兴趣": "", "近况": "（最近在忙什么，带上大概时间）", "群里关系": "（和群里谁走得近、谁常互损）",
  "怎么聊": "（新来的人和他聊天要注意什么）", "别碰": "（他明显不爱被提的话题；没有就空）",
  "记得的事": ["值得记住的小事，每条 20 字以内，最多 5 条"]}}

# 群里提到他的话
{mentions}

# 他说的话
{said}"""


# ---------------- 读导出的文本 ----------------
def parse_export(path: Path) -> dict:
    """data/export/<时间>/<群名>.txt → {name, members: [{name, nick, remark, id, n}], lines: [(ts, who, text)]}"""
    name, members, lines, sec = path.stem, [], [], ""
    for ln in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if ln.startswith("# ") and not lines and not members:
            name = ln[2:].strip()
        elif ln.startswith("## "):
            sec = ln[3:]
        elif sec.startswith("成员"):
            m = MEMBER_RE.match(ln.strip())
            if m:
                members.append({"name": m.group(1), "nick": m.group(2), "remark": m.group(3), "id": m.group(4),
                                "n": int(m.group(5))})
        elif sec.startswith("聊天记录"):
            m = LINE_RE.match(ln)
            if m:
                try:
                    ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M").timestamp()
                except ValueError:
                    continue
                lines.append((ts, m.group(2), m.group(3).replace(" ⏎ ", " ")))
    return {"name": name, "members": members, "lines": lines}


def names_habit(lines: list[tuple], names: list[str]) -> float:
    """群友之间叫名字的比例（不算 @）：小于 0.04 就当这个群“平时不喊名字”。"""
    texts = [t for _, w, t in lines if w not in ("系统",)][-600:]
    if len(texts) < 100:
        return 1.0
    ns = [n for n in names if len(n) >= 2]
    hit = sum(1 for t in texts if any(n in re.sub(r"@\S+", "", t) for n in ns))
    return hit / len(texts)


def _clip(rows: list[str], limit: int) -> str:
    out, size = [], 0
    for r in reversed(rows):                      # 从最近的往前取
        if size + len(r) > limit:
            break
        out.append(r)
        size += len(r) + 1
    return "\n".join(reversed(out))


def _fmt(ts: float, who: str, text: str) -> str:
    return f"{time.strftime('%m-%d %H:%M', time.localtime(ts))} {who}：{text[:160]}"


# ---------------- 整理成种子 ----------------
async def build_seed(cfg, exports: list[dict], llm, her: str, max_people: int = 15, progress=print,
                     her_names: list[str] | None = None) -> dict:
    chan = "QQ 群" if cfg.channel == "qq" else "微信群"
    key = "qq" if cfg.channel == "qq" else "wxid"
    groups = []
    for ex in exports:
        lines = [(ts, w, t) for ts, w, t in ex["lines"] if t.strip() and not NOISE.match(t.strip())]
        if not lines:
            progress(f"「{ex['name']}」没有能用的聊天记录，跳过")
            continue
        mem = sorted(ex["members"], key=lambda m: -m["n"])
        mem_txt = "\n".join(f"- {m['name']}（说了 {m['n']} 条" + (f"，昵称 {m['nick']}" if m["nick"] and m["nick"] != m["name"] else "")
                            + "）" for m in mem[:40])
        progress(f"「{ex['name']}」：{len(lines)} 条，整理群里的事……")
        g = await llm.json([{"role": "user", "content": GROUP_PROMPT.format(
            chan=chan, name=ex["name"], her=her, members=mem_txt,
            log=_clip([_fmt(*x) for x in lines], 14000))}], default={}, role="memory") or {}
        if not isinstance(g, dict):
            g = {}
        entry: dict = {"name": ex["name"]}
        if cfg.channel == "qq":
            entry["gid"] = ex.get("gid") or ""
        for k in ("notes", "norms"):
            items = [str(x).strip() for x in (g.get(k) or []) if str(x).strip()]
            if items:
                entry[k] = "\n".join(f"- {x}" for x in items[:10])
        lore = [{"keys": [str(k) for k in (x.get("keys") or []) if str(k).strip()][:4], "text": str(x.get("text") or "")[:80]}
                for x in (g.get("lore") or []) if isinstance(x, dict) and x.get("text")]
        if lore:
            entry["lore"] = [x for x in lore if x["keys"]][:8]
        people = []
        mine = {her, *(her_names or [])}                # 她自己（已经在群里的话）不用认识
        talkers = [m for m in mem if m["n"] >= 5 and m["name"] not in ("系统",) and m["name"] not in mine
                   and m["nick"] not in mine][:max_people]
        all_names = [m["name"] for m in mem] + [m["nick"] for m in mem if m["nick"]]
        for i, m in enumerate(talkers, 1):
            who = m["name"]
            said, ctx = [], []
            for j, (ts, w, t) in enumerate(lines):
                if w == who:
                    said.append(_fmt(ts, w, t))
                elif who in t or (m["nick"] and m["nick"] in t):
                    ctx.append(_fmt(ts, w, t))
            progress(f"  {i}/{len(talkers)} {who}（{len(said)} 句）")
            p = await llm.json([{"role": "user", "content": PERSON_PROMPT.format(
                chan=chan, group=ex["name"], who=who, her=her,
                ids=f"昵称 {m['nick']}" if m["nick"] else "", mentions=_clip(ctx, 2500) or "（没有）",
                said=_clip(said, 6000))}], default={}, role="memory") or {}
            if not isinstance(p, dict):
                continue
            aliases = [str(a).strip() for a in (p.pop("aliases", None) or []) if str(a).strip() and str(a).strip() != who][:4]
            prof = {k: v for k, v in p.items() if v not in ("", None, [], {})}
            if not prof:
                continue
            person = {key: m["id"], "name": who if who != "我" else (m["nick"] or who), "profile": prof}
            if aliases:
                person["aliases"] = aliases
            people.append(person)
        if people:
            entry["people"] = people
        ratio = names_habit(lines, all_names + [a for p in people for a in p.get("aliases", [])])
        if ratio < 0.04:
            entry["knobs"] = {"call_names": False}
            progress(f"  这个群几乎不叫名字（{ratio:.0%}）：给她关掉“叫人名字”")
        groups.append(entry)
    return {"groups": groups}


def latest_export(root: Path = ROOT) -> Path | None:
    d = root / "data" / "export"
    subs = sorted((p for p in d.iterdir() if p.is_dir()), key=lambda p: p.name) if d.exists() else []
    return subs[-1] if subs else None


def seedgen_cmd(argv: list[str]) -> int:
    cfg = load_config()
    src = Path(argv[0]) if argv and not argv[0].startswith("--") else latest_export()
    if src and not src.is_absolute():
        src = ROOT / src
    if not src or not src.exists():
        say(False, "没找到导出的聊天记录：先跑 wxexport")
        return 1
    files = sorted(src.glob("*.txt")) if src.is_dir() else [src]
    exports = []
    for f in files:
        ex = parse_export(f)
        meta = f.with_suffix(".json")
        if meta.exists():
            try:
                ex.update(json.loads(meta.read_text(encoding="utf-8")))
            except ValueError:
                pass
        exports.append(ex)
    if not exports:
        say(False, f"{src} 里没有导出的 .txt")
        return 1
    from aigroup.brain import GroupBrain
    brain = GroupBrain(cfg)
    if not brain.llm._chain("memory") and not brain.llm._chain("chat"):
        say(False, "还没配大模型接口：先跑 llm 加一个")
        return 1
    print(f"用大模型整理 {len(exports)} 个群的聊天记录（{src}）……")
    p = brain.persona
    seed = asyncio.run(build_seed(cfg, exports, brain.llm, p.name,
                                  her_names=[x for x in (getattr(p, "formal_name", ""), *((cfg.get(cfg.channel) or {}).get("aliases") or [])) if x]))
    out = ROOT / "data" / "seed_draft.yaml"
    out.parent.mkdir(parents=True, exist_ok=True)
    head = ("# 进群前的准备：她会“事先知道”的事（seedgen 整理的草稿）。\n"
            "# 看一遍、改一改：删掉不想让她知道的，改掉写错的。改好以后：python run.py seed data/seed_draft.yaml\n\n")
    out.write_text(head + yaml.safe_dump(seed, allow_unicode=True, sort_keys=False, width=200), encoding="utf-8")
    n = sum(len(g.get("people") or []) for g in seed["groups"])
    say(True, f"草稿写好了：{len(seed['groups'])} 个群、{n} 个人 → {out}")
    return 0


# ---------------- 一条龙 ----------------
def onboard_cmd(argv: list[str]) -> int:
    print("进群前的准备：先导出群里以前的聊天记录，再用大模型整理成“认识大家”的资料，你看过以后灌给她。\n")
    print("第 1 步：导出聊天记录（从电脑上开着的某个号读，一般选你的大号；要导哪个号，那个微信就得开着、登录着）")
    if yes("现在导出吗（已经导过就选 n）", True):
        from aigroup.wechat import diag
        if diag.export_cmd(argv):
            return 1
    print("\n第 2 步：用大模型整理（大约群数 + 常说话的人数次调用）")
    if seedgen_cmd([]):
        return 1
    draft = ROOT / "data" / "seed_draft.yaml"
    print(f"\n第 3 步：打开 {draft} 看一遍，删掉不想让她知道的、改掉写错的。")
    try:
        import os
        import sys
        if sys.platform == "win32":
            os.startfile(str(draft))              # noqa: S606
    except OSError:
        pass
    ask("改好了按回车继续（不想导入就 Ctrl+C）", "")
    from aigroup.seed import main as seed_main
    return seed_main([str(draft)])
