"""人设文件：persona/xxx.md

顶部 --- 之间是基础信息（YAML），下面是自由描述（她是谁、性格、说话方式、底线……），
最后可以有“# 对话示例”。字段说明见 persona/template.md 和 PERSONA.md；只有 name 是必填的，其他都有默认值。

    name           名字（必填）
    call_name      她怎么叫你（默认“你”）
    formal_name    装正经时的称呼（可选，用来检查“别叫太多”）
    user_name      在她的世界里你是谁（默认同 call_name；提示词里“和你在意的人（xx）聊天”）
    gender         她的性别 female / male / other（决定代词：她 / 他 / TA）
    user_gender    你的性别 male / female / other
    age, birthday  年龄、生日（MM-DD；不填生日就自动挑一个）
    identity       一句话身份：大学一年级学生 / 出版社插画编辑 / 自由职业画师……
    life           student（按校历：学期、考试周、寒暑假）/ worker（按工作日、节假日）/ free（自由安排）
    school / work  校历 / 上班时间（可选，覆盖默认）
    home           “家就在你家隔壁”这类和你的关系距离（放假回家时会用到）
    look           外貌描述（她发自拍时画图用；不写就不画人像，只拍景物）
    catchphrases   她的口头禅（用太多时会提醒她换个说法）
    interests      她感兴趣的话题（群里聊到这些更想插话；不写就从人设里自动整理）
    hold_lines     网不好时先说的话（不经过大模型）
    greeting       第一次 /start 时她说的话
    ai_open        true = 大家都知道她是你做的 AI（被问到就大方承认，不装真人）；默认 false
    gaps           19.1 她不懂、也不太关心的领域（被问到就说不懂、敷衍一句，不去查、不装懂），比如 [车, 股票]
    traits         14.0 结构化人格文件（和人设放同一个文件夹，比如 local_xiaoman_wx.traits.yaml），见 aigroup/traits.py
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

EX_HEAD = re.compile(r"^#\s*对话示例\s*$", re.M)
PRONOUN = {"female": "她", "male": "他", "other": "TA"}
GENDER_ALIASES = {"女": "female", "女生": "female", "female": "female", "f": "female", "girl": "female",
                  "男": "male", "男生": "male", "male": "male", "m": "male", "boy": "male",
                  "其他": "other", "other": "other", "x": "other", "nb": "other"}
LIFE_ALIASES = {"student": "student", "学生": "student", "worker": "worker", "上班": "worker", "上班族": "worker",
                "work": "worker", "free": "free", "自由": "free", "自由职业": "free"}
KNOWN_KEYS = {"name", "call_name", "formal_name", "user_name", "gender", "user_gender", "age", "birthday", "identity",
              "life", "school", "work", "home", "look", "voice", "catchphrases", "interests", "hold_lines", "greeting", "id", "ai_open", "traits", "gaps"}


@dataclass
class Example:
    scene: str
    keywords: list[str]
    turns: list[tuple[str, str]]          # [(“他”/名字, 内容)]

    def render(self, name: str) -> str:
        return "\n".join(f"{who}：{text}" for who, text in self.turns)


@dataclass
class Persona:
    name: str
    call_name: str
    body: str
    examples: list[Example] = field(default_factory=list)
    formal_name: str = ""
    user_name: str = ""
    gender: str = "female"
    user_gender: str = "male"
    age: int | None = None
    birthday: str = ""
    identity: str = ""
    life: str = "free"
    school: dict = field(default_factory=dict)
    work: dict = field(default_factory=dict)
    home: str = ""
    look: str = ""
    voice: dict = field(default_factory=dict)
    catchphrases: list[str] = field(default_factory=list)
    interests: list[str] = field(default_factory=list)
    hold_lines: list[str] = field(default_factory=list)
    greeting: str = ""
    ai_open: bool = False
    gaps: list[str] = field(default_factory=list)
    traits: object = None                 # aigroup.traits.Traits（14.0）；没写 traits 就是 None
    path: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def ta(self) -> str:
        """她 / 他 / TA"""
        return PRONOUN.get(self.gender, "她")

    @property
    def user_ta(self) -> str:
        return PRONOUN.get(self.user_gender, "他")

    @property
    def default_pronouns(self) -> bool:
        return self.gender == "female" and self.user_gender == "male"

    def update_from(self, other: "Persona"):
        """热更新：原地改字段（各模块拿着的都是同一个对象，改完马上生效）。"""
        for k in self.__dataclass_fields__:
            setattr(self, k, getattr(other, k))


def parse_examples(text: str) -> list[Example]:
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    out = []
    for block in re.split(r"^##\s+", text, flags=re.M)[1:]:
        head, _, rest = block.partition("\n")
        scene, _, kws = head.partition("|")
        turns = []
        for line in rest.splitlines():
            m = re.match(r"^\s*([^：:\s]{1,8})[：:]\s*(.+)$", line)
            if m:
                turns.append((m.group(1), m.group(2).strip()))
        if turns:
            out.append(Example(scene.strip(), kws.split(), turns))
    return out


def _norm_md(v) -> str:
    from aigroup.core.util import norm_md
    return norm_md(v) or ""


def split_front(text: str) -> tuple[dict, str, list[str]]:
    """返回 (frontmatter, 正文, 警告)。"""
    warnings: list[str] = []
    meta: dict = {}
    body = text
    if text.lstrip("﻿").startswith("---"):
        parts = re.split(r"^---[ \t\r]*$", text.lstrip("﻿"), maxsplit=2, flags=re.M)
        if len(parts) >= 3 and not parts[0].strip():
            try:
                meta = yaml.safe_load(parts[1]) or {}
                if not isinstance(meta, dict):
                    warnings.append("顶部 --- 之间要写成“字段: 值”的格式")
                    meta = {}
            except yaml.YAMLError as e:
                warnings.append(f"顶部 --- 之间的格式有错：{str(e).splitlines()[0]}")
                meta = {}
            body = parts[2]
    else:
        warnings.append("文件开头没有 --- 包起来的基础信息（至少要写 name）")
    return meta, body, warnings


def parse_persona(text: str, path: str = "") -> Persona:
    meta, body, warnings = split_front(text)
    examples = []
    m = EX_HEAD.search(body)
    if m:                              # 对话示例单独存，不整段进提示词
        nxt = re.search(r"^#\s+\S", body[m.end():], re.M)
        end = m.end() + nxt.start() if nxt else len(body)
        examples = parse_examples(body[m.end():end])
        body = body[:m.start()] + body[end:]
    unknown = [k for k in meta if k not in KNOWN_KEYS]
    if unknown:
        warnings.append("不认识的字段（会被忽略）：" + "、".join(map(str, unknown)))
    name = str(meta.get("name") or "").strip()
    if not name:
        warnings.append("没写 name（她叫什么）")
        name = "她"
    gender = GENDER_ALIASES.get(str(meta.get("gender") or "female").strip().lower(), None)
    if gender is None:
        warnings.append("gender 只能是 female / male / other")
        gender = "female"
    ugender = GENDER_ALIASES.get(str(meta.get("user_gender") or "male").strip().lower(), None)
    if ugender is None:
        warnings.append("user_gender 只能是 male / female / other")
        ugender = "male"
    life = LIFE_ALIASES.get(str(meta.get("life") or "").strip().lower(), "")
    if meta.get("life") and not life:
        warnings.append("life 只能是 student / worker / free")
    if not life:
        life = "student" if re.search(r"大学|大一|大二|大三|大四|高中|学生|研究生", str(meta.get("identity") or "") + body[:400]) \
            else "free"
    age = meta.get("age")
    try:
        age = int(age) if age not in (None, "") else None
        if age is not None and age > 200:
            raise ValueError
    except (TypeError, ValueError, OverflowError):
        warnings.append("age 要写数字")
        age = None
    if age is not None and age < 18:
        warnings.append("角色要是成年人（age 至少 18）")
        age = 18
    bday = _norm_md(meta.get("birthday")) if meta.get("birthday") else ""
    if meta.get("birthday") and not bday:
        warnings.append("birthday 要写成 MM-DD，比如 03-15")
    lst = lambda v: [str(x).strip() for x in (v or []) if str(x).strip()] if isinstance(v, list) else (  # noqa: E731
        [str(v).strip()] if v else [])
    voice = meta.get("voice") if isinstance(meta.get("voice"), dict) else {}
    call = str(meta.get("call_name") or "你").strip()
    body = body.strip()
    traits = None
    if meta.get("traits"):
        from aigroup.traits import load_traits
        tp = Path(path).parent / str(meta["traits"]) if path else Path(str(meta["traits"]))
        traits = load_traits(tp)
        warnings += [f"人格文件：{w}" for w in traits.warnings]
        core = traits.render_core()
        if core.strip() != "# 你的人格（说话做事都从这里出发）":
            body = core + "\n\n" + body          # 渲染好的人格放正文最前面：人设版本、盲比、评测都会带上它
    if len(body) < 80:
        warnings.append("正文太短：多写几句她是谁、什么性格、怎么说话，她才像她")
    if len(body) > 6000:
        warnings.append(f"正文有 {len(body)} 字，有点长（太长的人设模型会记不住重点，建议 5000 字以内，细节放进对话示例和人格文件）")
    if not examples:
        warnings.append("没有“# 对话示例”：写几段她平时怎么说话，效果会好很多")
    return Persona(
        name=name, call_name=call, body=body, examples=examples,
        formal_name=str(meta.get("formal_name") or "").strip(),
        user_name=str(meta.get("user_name") or "").strip(),
        gender=gender, user_gender=ugender, age=age, birthday=bday,
        identity=str(meta.get("identity") or "").strip(), life=life,
        school=meta.get("school") if isinstance(meta.get("school"), dict) else {},
        work=meta.get("work") if isinstance(meta.get("work"), dict) else {},
        home=str(meta.get("home") or "").strip(), look=str(meta.get("look") or "").strip(),
        voice=voice, catchphrases=lst(meta.get("catchphrases")), interests=lst(meta.get("interests")), hold_lines=lst(meta.get("hold_lines")),
        greeting=str(meta.get("greeting") or "").strip(), path=str(path), warnings=warnings,
        ai_open=str(meta.get("ai_open") or "").strip().lower() in ("true", "1", "yes", "是"),
        gaps=lst(meta.get("gaps")), traits=traits,
    )


def load_persona(path: str | Path) -> Persona:
    text = Path(path).read_text(encoding="utf-8")
    return parse_persona(text, str(path))


def pronoun_note(p: Persona) -> str:
    """提示词里默认用“她”指角色、“他”指用户；性别不是默认时，告诉模型实际该用哪个代词。"""
    if p.default_pronouns:
        return ""
    words = {"female": "女生", "male": "男生", "other": "不想被区分性别的人"}
    user = p.user_name or p.call_name or "用户"
    return (f"（说明：下面的说明文字默认用“她”指{p.name}、用“他”指{user}。实际上{p.name}是{words[p.gender]}，"
            f"{user}是{words[p.user_gender]}：你写出来的话、日记、记忆里，称呼{p.name}用“{p.ta}”，"
            f"称呼{user}用“{p.user_ta}”。）")
