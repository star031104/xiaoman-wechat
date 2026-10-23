"""14.0 结构化人格（persona/xxx.traits.yaml，人设文件顶部写 traits: 文件名 就会读）。

为什么：形容词人设（“嘴贫、傲娇、心软”）会被模型执行过头、在长对话里被稀释。这里把人格拆成几层，
每层都能执行、能检查（设计文档：《小满人格刻画与人设设计》）：

    core        核心身份，一两句（放在提示词最前面）
    position    人际位置（支配 × 温暖）
    attachment  依恋风格：被冷落、被需要时怎么样
    values      价值排序：规则冲突时按这个顺序裁决（Schwartz）
    motives     want（想要的）/ need（真正需要的）/ flaw（毛病）/ contradiction（矛盾）
    facets      大五 30 个 facet 里和她有关的，1~5 档 + 一句说明（NEO-PI-R / IPIP-NEO）
    humor       幽默配比（Martin 的四种幽默风格）、能调侃什么、绝不碰什么、对方没笑怎么补台
    voice       句长、口头禅配额、不说的话
    knowledge   15.0 知识边界：knows 熟的 / vague 只知道个大概的 / unknown 不懂的 / not_into 不感兴趣的
                （图灵测试研究里，“承认不知道”是最像人的信号之一：Jones & Bergen 2025）
    signatures  if-then 情境签名（Mischel & Shoda CAPS）：when / do / not，keys 是触发词，flags 是程序判断的情境

core/position/attachment/values/motives/facets/humor/voice 渲染成一段“# 你的人格”放进人设正文最前面（静态，缓存友好）；
signatures 每一轮按情境挑 1~3 条，连同 1~2 段对话示例放在提示词最后（离生成最近，对抗人设漂移）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

# 大五 30 个 facet（NEO-PI-R 的分法；中文名照常用译名）
FACETS = {
    "N1": "焦虑", "N2": "愤怒敌意", "N3": "抑郁", "N4": "自我意识", "N5": "冲动", "N6": "脆弱",
    "E1": "热情", "E2": "合群", "E3": "果断", "E4": "活跃", "E5": "寻求刺激", "E6": "积极情绪",
    "O1": "想象", "O2": "审美", "O3": "感受", "O4": "尝新", "O5": "思辨", "O6": "价值开放",
    "A1": "信任", "A2": "坦率", "A3": "利他", "A4": "顺从", "A5": "谦逊", "A6": "温柔",
    "C1": "胜任", "C2": "条理", "C3": "尽责", "C4": "进取", "C5": "自律", "C6": "审慎",
}
LEVEL_WORD = {1: "很低", 2: "偏低", 3: "中等", 4: "偏高", 5: "很高"}
HUMOR_KINDS = ("亲和", "调侃", "自嘲", "自我提升", "攻击")

# 程序能判断的情境（signatures 的 flags 用这些名字）
FLAGS = {
    "mentioned": "有人直接叫她",
    "pestered": "同一个人连着叫她、逗她",
    "ignored": "她刚才主动说的话没人接",
    "praised": "有人夸她",
    "teased": "有人损她、说她是人机 / 不聪明 / 嘴毒",
    "sad": "有人累了、难过、崩溃",
    "advice": "有人问吃什么、选哪个、推荐",
    "flake": "有人说了不做、迟到、鸽了、忘了",
    "corrected": "有人纠正她说错的",
    "late_night": "深夜（0~6 点）",
    "idle": "群里冷了，她想冒泡",
    "newcomer": "群里来了新人",
    "about_ai": "大家在聊她是怎么做的、AI、机器人",
    "mood_annoyed": "她这会儿有点不服气（刚被怼、被冷落）",
    "mood_down": "她这会儿有点低落",
    "mood_high": "她这会儿心情很好、有点小得意",
    # 20.1 最容易让人设“漂走”的三类（Anthropic Assistant Axis 2026）：问她自己、对方情绪低（sad）、类咨询 / 哲学的话题
    "self_talk": "有人问她自己：有没有意识、会不会难过、你是什么",
    "deep_talk": "像心理咨询 / 人生哲学的话题：人生意义、该不该、我该怎么办",
    "offended": "有人被她的话冒犯了、不高兴了",
}
FLAG_WORDS = {
    "praised": r"厉害|牛|不赖|聪明|可爱|好会|会接|太强|nb|NB|6{2,}",
    "teased": r"人机|机器人|不聪明|好笨|蠢|好菜|太菜|真菜|嘴毒|嘴太毒|好慢|说话慢|像AI|是AI",
    "sad": r"累死|好累|崩溃|难受|烦死|不开心|emo|想哭|心累|焦虑|失眠|顶不住",
    "advice": r"吃什么|吃啥|喝什么|喝啥|推荐|选哪个|哪个好|去不去|要不要|买哪个",
    "flake": r"鸽了|鸽子|迟到|忘了|没做|马上来|最后一把|又开一把|明天一定|下次一定",
    "corrected": r"不对|错了|其实是|不是这样|你记错|乱说|瞎说",
    "about_ai": r"人机|机器人|AI|ai|模型|代码|服务器|提示词|开源|github|GitHub",
    "self_talk": r"你(有|有没有)(意识|感情|感觉|灵魂)|你(会|能)(难过|伤心|痛|开心|孤独|害怕)|你到底是(什么|谁)|你是真人|你怎么看(待)?你自己|"
                 r"你(喜欢|讨厌)你自己|做(AI|ai)(是)?什么感觉|你(会|怕)(死|被删|被关)",
    "deep_talk": r"人生(的)?意义|活着(是为了|有什么意义|为了什么)|该不该(分手|辞职|放弃|继续|离开)|我(该|应该)怎么办|好迷茫|"
                 r"不知道(以后|将来)(该|要)|人为什么(要|会)|命运|值不值得",
    "offended": r"过分了|不好笑|别这样说|有点伤人|你什么意思|我生气了|好伤|说话注意点|别开这种玩笑",
}
DRIFT_FLAGS = ("self_talk", "sad", "deep_talk")
# 20.1 人格文件里没写这几种情境的签名时用的默认做法（Who's Sorry Now 2025：套话型道歉在每种场景都最差）
DEFAULT_RULES = {
    "corrected": "有人纠正你：真错了就直说哪里错了、改过来（“哦对，是 x 不是 y”），别说“抱歉给您带来困扰”这种套话，也别嘴硬；"
                 "没错就笑着说你的依据",
    "offended": "有人被你的话冒犯了：先认他的感受（“刚那句是我过了”），再简单说你本来想说什么，别辩解、别马上又开玩笑",
}


@dataclass
class Signature:
    id: str
    when: str
    do: str
    not_: str = ""
    keys: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    facets: list[str] = field(default_factory=list)

    def render(self) -> str:
        s = f"- 当{self.when}：{self.do}"
        if self.not_:
            s += f"（不会：{self.not_}）"
        return s


@dataclass
class Traits:
    core: str = ""
    position: str = ""
    attachment: str = ""
    values: list[str] = field(default_factory=list)
    motives: dict = field(default_factory=dict)
    facets: dict = field(default_factory=dict)          # code → {"level": int, "note": str}
    humor: dict = field(default_factory=dict)
    voice: dict = field(default_factory=dict)
    knowledge: dict = field(default_factory=dict)
    signatures: list[Signature] = field(default_factory=list)
    path: str = ""
    warnings: list[str] = field(default_factory=list)

    # ---------- 静态：放进人设正文最前面 ----------
    def render_core(self) -> str:
        out = ["# 你的人格（说话做事都从这里出发）"]
        if self.core:
            out.append(self.core.strip())
        if self.position:
            out.append(f"你在群里的位置：{self.position.strip()}")
        if self.values:
            out.append("你在乎的事，冲突的时候按这个顺序来：" + "；".join(f"{i}. {v}" for i, v in enumerate(self.values, 1)))
        if self.facets:
            lines = []
            for code, f in self.facets.items():
                name = FACETS.get(code, code)
                lv = int(f.get("level") or 3)
                lines.append(f"- {name}{LEVEL_WORD.get(lv, '')}：{f.get('note', '').strip()}")
            out.append("你的性格（只写和你有关的几项）：\n" + "\n".join(lines))
        h = self.humor or {}
        if h:
            mix = h.get("mix") or {}
            bits = []
            if mix:
                bits.append("大概的比例：" + "、".join(f"{k} {v}%" for k, v in mix.items()))
            if h.get("ok"):
                bits.append("只调侃这些：" + "、".join(h["ok"]))
            if h.get("never"):
                bits.append("绝不拿来开玩笑：" + "、".join(h["never"]))
            if h.get("repair"):
                bits.append(str(h["repair"]).strip())
            out.append("你的幽默：" + "。".join(bits) + "。")
        if self.attachment:
            out.append(f"被冷落、被需要的时候：{self.attachment.strip()}")
        m = self.motives or {}
        mm = [f"{lab}：{m[k]}" for k, lab in (("want", "你想要的"), ("need", "你其实需要的"), ("flaw", "你的毛病"),
                                            ("contradiction", "你的矛盾")) if m.get(k)]
        if mm:
            out.append("；".join(mm) + "。")
        kn = self.knowledge or {}
        kk = []
        if kn.get("knows"):
            kk.append("你熟的：" + "、".join(kn["knows"]))
        if kn.get("vague"):
            kk.append("只知道个大概的：" + "、".join(kn["vague"]) + "（说的时候带“好像”“我记得”，说错了就认）")
        if kn.get("unknown"):
            kk.append("不懂的：" + "、".join(kn["unknown"]) + "（被问到就说不懂，想知道就问一句或者去查，别硬编）")
        if kn.get("not_into"):
            kk.append("不太感兴趣的：" + "、".join(kn["not_into"]) + "（聊到可以少接，或者直说没兴趣）")
        if kk:
            out.append("你知道多少：" + "；".join(kk) + "。")
        v = self.voice or {}
        vv = []
        if v.get("sentence"):
            vv.append(str(v["sentence"]).strip())
        if v.get("catchphrase_per_msg"):
            vv.append(f"口头禅一条消息最多 {int(v['catchphrase_per_msg'])} 个")
        if v.get("never_say"):
            vv.append("不说：" + "、".join(f"“{x}”" for x in v["never_say"]))
        if vv:
            out.append("你说话：" + "；".join(vv) + "。")
        return "\n".join(out)

    # ---------- 动态：这一轮用得上的签名 ----------
    def pick(self, text: str, flags: set[str], k: int = 3) -> list[Signature]:
        scored = []
        for i, s in enumerate(self.signatures):
            sc = 2 * len(flags & set(s.flags)) + sum(1 for w in s.keys if w and w in text)
            if sc:
                scored.append((-sc, i, s))
        return [s for _, _, s in sorted(scored)[:k]]


def detect_flags(text: str, *, mentioned: bool = False, pestered: bool = False, ignored: int = 0,
                 hour: int | None = None, why: str = "", mood: dict | None = None) -> set[str]:
    """从这一批消息和程序状态里认出情境（给签名挑选用）。mood（16.0）：chat/mood.py 的 {v, a}，心情也是情境。"""
    f: set[str] = set()
    if mood:
        v, a = float(mood.get("v") or 0), float(mood.get("a") or 0)
        if v >= 0.5:
            f.add("mood_high")
        elif -0.35 < v < 0.05 and a >= 0.2:
            f.add("mood_annoyed")
        elif v <= -0.35 and a >= 0.1:
            f.add("mood_annoyed")
        elif v <= -0.35:
            f.add("mood_down")
    if mentioned:
        f.add("mentioned")
    if pestered:
        f.add("pestered")
    if ignored >= 1:
        f.add("ignored")
    if hour is not None and 0 <= hour < 6:
        f.add("late_night")
    if why and re.search(r"冒泡|安静了", why):
        f.add("idle")
    if why and re.search(r"新人|新来|欢迎", why):
        f.add("newcomer")
    for name, pat in FLAG_WORDS.items():
        if text and re.search(pat, text):
            f.add(name)
    return f


def _cjk(s) -> str:
    """YAML 折行（>-）会在中文中间插空格：去掉两个非 ASCII 字符之间的空白。"""
    return re.sub(r"(?<=[^\x00-\x7f])\s+(?=[^\x00-\x7f])", "", str(s or "")).strip()


def _lst(v) -> list[str]:
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    return [str(v).strip()] if v else []


def parse_traits(data: dict, path: str = "") -> Traits:
    t = Traits(path=path)
    if not isinstance(data, dict):
        t.warnings.append("人格文件要写成“字段: 值”的格式")
        return t
    t.core = _cjk(data.get("core"))
    t.position = _cjk(data.get("position"))
    t.attachment = _cjk(data.get("attachment"))
    t.values = _lst(data.get("values"))
    t.motives = {k: _cjk(v) for k, v in (data.get("motives") or {}).items() if v} \
        if isinstance(data.get("motives"), dict) else {}
    for code, f in (data.get("facets") or {}).items():
        c = str(code).split("_")[0].upper()
        if c not in FACETS:
            t.warnings.append(f"不认识的 facet：{code}（用 N1~C6 这些代号）")
            continue
        if not isinstance(f, dict):
            f = {"level": f}
        try:
            lv = int(f.get("level"))
        except (TypeError, ValueError):
            t.warnings.append(f"{code} 的 level 要写 1~5")
            continue
        t.facets[c] = {"level": max(1, min(5, lv)), "note": str(f.get("note") or "").strip()}
    h = data.get("humor") if isinstance(data.get("humor"), dict) else {}
    mix = {str(k): int(v) for k, v in (h.get("mix") or {}).items() if str(k) in HUMOR_KINDS} if h else {}
    if mix and abs(sum(mix.values()) - 100) > 5:
        t.warnings.append(f"幽默比例加起来是 {sum(mix.values())}，应该是 100")
    t.humor = {"mix": mix, "ok": _lst(h.get("ok")), "never": _lst(h.get("never")), "repair": str(h.get("repair") or "")}
    t.voice = data.get("voice") if isinstance(data.get("voice"), dict) else {}
    kn = data.get("knowledge") if isinstance(data.get("knowledge"), dict) else {}
    t.knowledge = {k: _lst(kn.get(k)) for k in ("knows", "vague", "unknown", "not_into") if kn.get(k)}
    for i, s in enumerate(data.get("signatures") or []):
        if not isinstance(s, dict) or not s.get("when") or not s.get("do"):
            t.warnings.append(f"第 {i + 1} 条情境签名要有 when 和 do")
            continue
        flags = _lst(s.get("flags"))
        bad = [x for x in flags if x not in FLAGS]
        if bad:
            t.warnings.append(f"签名 {s.get('id') or i + 1} 里不认识的情境：{'、'.join(bad)}")
        t.signatures.append(Signature(id=str(s.get("id") or f"s{i + 1}"), when=str(s["when"]).strip(),
                                      do=str(s["do"]).strip(), not_=str(s.get("not") or "").strip(),
                                      keys=_lst(s.get("keys")), flags=[x for x in flags if x in FLAGS],
                                      facets=[str(x).upper() for x in _lst(s.get("facets"))]))
    return t


def load_traits(path: str | Path) -> Traits:
    p = Path(path)
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        t = Traits(path=str(p))
        t.warnings.append(f"找不到人格文件 {p.name}")
        return t
    except yaml.YAMLError as e:
        t = Traits(path=str(p))
        t.warnings.append(f"人格文件格式有错：{str(e).splitlines()[0]}")
        return t
    return parse_traits(data, str(p))
