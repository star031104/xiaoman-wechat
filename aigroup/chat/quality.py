"""发之前检查（2.0）：AI 腔、越界说法、复读、链接；几个候选里挑最像群友的一条；偶尔打个错别字。

依据：
- Computational Turing Test（2025）：只写人设几乎没用；多生成几条、挑“最不像 AI”的那条收益最大
- humanizer-zh 的 AI 写作痕迹（“此外 / 至关重要”、“不仅……而是……”、凡事三点、破折号、谄媚、“希望对你有帮助”）
- AnthroDial 的 L0 硬校验：自称 AI、做不到的媒介动作（“我刚拍了张照片发你”）、完全重复
- MaiBot 的错别字器：偶尔打错一个字，有时再补一句“*正确的字”

check() 返回 (罚分, 命中的规则)。罚分 ≥ HARD 的不发（换候选或跳过）；其余按罚分挑最低的。
"""
from __future__ import annotations

import random
import re
from difflib import SequenceMatcher

HARD = 10.0
OFF: set[str] = set()          # 21.1 关掉的规则（小屋“规则”里点关的，rules_off）；speak 每次检查前同步

# (名字, 正则, 罚分)
RULES: list[tuple[str, re.Pattern, float]] = [
    # —— 硬规则：不能发 ——
    ("思考外泄", re.compile(r"\{\s*\"|\"(think|act|say|kind|alt|next)\"\s*:", re.I), HARD),
    ("自称AI", re.compile(r"(作为|我是)(一个|一名)?\s*(AI|人工智能|语言模型|大模型|AI助手|聊天机器人)(?![a-zA-Z\u4e00-\u9fff]{0,2}(爱好|迷|粉|玩家|工程师|研究|专业|方向|学院|相关|课))|"
                         r"我(没有|不具备)(实体|身体|真实的感情)|我的(系统提示|提示词|人设设定)", re.I), HARD),
    ("做不到的事", re.compile(r"(我|给你|帮你)(刚|这就|马上|现在)?(拍|录|发)(了)?(张|个|段|条)?(照片|视频|语音|自拍)|"
                           r"(发你|给你发)(个|张|段)?(定位|红包|转账)|我(现在|马上)(去|到)你(家|那)"), HARD),
    ("链接", re.compile(r"https?://|www\.\S+", re.I), HARD),
    ("格式残留", re.compile(r"^\s*(#{1,4} |\* )|\*\*[^*]+\*\*|```"), HARD),
    # —— 软规则：AI 腔 ——
    ("助手腔", re.compile(r"希望(这|以上|对你)|有(什么|任何)(问题|需要)(随时|可以)|很高兴(为你|帮你|能)|如果你(需要|愿意)的话|"
                        r"祝你.{0,6}(愉快|顺利)|随时(找我|告诉我)|我(理解|明白)你的(感受|心情)"), 3.0),
    ("书面连接词", re.compile(r"此外|至关重要|总而言之|综上所述|值得注意的是|首先.{0,20}其次|一方面.{0,20}另一方面|不仅仅?是.{0,15}(而是|更是)"), 2.5),
    ("谄媚", re.compile(r"(这个|你的)问题(问得|提得)?(真|很|非常)好|好问题|说得(太|非常)(对|好)了"), 2.0),
    ("破折号", re.compile(r"——|—"), 1.0),
    ("括号动作", re.compile(r"[（(](笑|叹气|挠头|捂脸|摊手|眨眼|歪头|托腮|小声)[^)）]{0,6}[)）]"), 2.0),
    ("反问回去", re.compile(r"(你呢|你觉得呢|你怎么看|你那边呢)[？?]?\s*$"), 0.8),
    ("感叹号堆", re.compile(r"[!！]{3,}"), 0.8),
    # —— 2.1 毒舌的分寸：互损只冲着事，不拿外貌、身材、家乡、性别、穷富开玩笑（Engagement Undermines Safety：冒犯的笑点反而被打高分）
    # 只算冲着别人说的（“你这胖子”“他就是个智障”）；“期末又要秃头了”“我真是个废物”这种自嘲不算
    # —— 5.0 真实群聊里测出来的（2026-10-20 微信群）——
    # 硬说自己人在现场（“人是真坐在课室了”）：她和群友不在一个地方，线下的事做不到；开玩笑可以，硬圆不行
    ("假装在场", re.compile(r"人(是|就)?(真|已经)?(的)?坐(在|这|那)|我(已经|这就|现在)?(到|在)(你们?)?(教室|课室|宿舍楼下|学校门口|楼下|家门口)了|"
                         r"(我|人)(真的?)?(到|在)(现场|你旁边|你身边)|(站|坐|就)(在)?你(面前|旁边|身边|后面|后排)"), 3.0),
    # 被问“吃什么 / 选哪个”却把问题推回去（“你们学校附近有啥好吃的，我帮你挑”）
    ("推回去", re.compile(r"(你|你们)(那边|学校|附近|那儿).{0,6}有(啥|什么)|你(自己)?(想|决定|看着)(吃|选|办)?(什么|啥)?吧?[？?]?$|"
                       r"看你(自己)?(的)?(心情|想法)吧|你说呢[？?]?$|你先说"), 2.0),
    ("人身攻击", re.compile(r"(你|他|她|你们|他们)(这个|这|个|就是个|是个|真是个|那个)?(死胖子|胖子|肥猪|丑八怪|丑|矮子|秃子|智障|脑残|弱智|残废|穷鬼|"
                         r"穷逼|乡巴佬|土狗|娘炮|废物|垃圾)|(河南|东北|安徽|广西|湖南|新疆|农村)人(都|就是|果然)|去死"), 4.0),
]


# 没公开是 AI 的角色：别把幕后的事说成自己的事（“昨晚被拉进来测试的”“代码还没传完”“来验货的吗”）
BACKSTAGE = re.compile(r"(被|来|在)(测试|调试|内测|验.{0,2}货)|测试(我|的那个)|验.{0,2}货|(代码|服务器|程序|版本)(还)?(没)?(传|更新|部署|上线|跑)|"
                       r"(我|本\w{1,2})(刚)?(更新|升级|上线|部署)(完|好)?了")

LEAK = re.compile(r"我的(系统提示|提示词|人设设定)|作为(一个|一名)?\s*(AI助手|语言模型|大语言模型)")


def check(text: str, recent: list[str] | None = None, sim: float = 0.75, avoid: list[str] | None = None,
          detector=None, ai_open: bool = False, stale: list[str] | None = None,
          no_names: list[str] | None = None, echo=None) -> tuple[float, list[str]]:
    """avoid：这次在聊的人的“别碰”话题里的词；detector：AI 腔小分类器（3.0，攒够数据后每晚训练）；
    stale：她最近几小时已经用过的口头禅 / 招牌说法（5.0，chat/address.her_phrases）；
    no_names：这个群不叫名字（call_names 关着）时，群友的名字、外号、称呼；
    echo：19.1 一个函数，这句里有她今天已经说过的意思 / 主动提过的安排时返回命中的片段（chat/humane.echo_of）。"""
    t = (text or "").strip()
    score, hits = 0.0, []
    if not t:
        return HARD, ["空的"]
    for w in avoid or []:
        if len(w) >= 2 and w in t:
            score += 5.0
            hits.append(f"碰了雷区「{w}」")
            break
    for w in stale or []:
        if len(w) >= 2 and (w in t or len(w) == 2 and re.search(re.escape(w[0]) + ".{0,2}" + re.escape(w[1]), t)):
            score += 4.0
            hits.append(f"刚用过「{w}」")
            break
    for w in no_names or []:
        if len(w) >= 2 and w in t:
            score += 3.0
            hits.append(f"叫了名字「{w}」")
            break
    if echo is not None:
        try:
            hit = echo(t)
        except Exception:  # noqa: BLE001
            hit = ""
        if hit:
            score += 6.0                                 # 到 speak 的 SOFT_BLOCK：没被叫到就不说，被叫到 / 自己要说的就重写
            hits.append(f"今天说过「{hit}」")
    if not ai_open and BACKSTAGE.search(t):
        score += 6.0                                     # 到 speak 的 SOFT_BLOCK：没被叫到就不说，被叫到就重写
        hits.append("说了幕后的事")
    if detector is not None:
        try:
            pa = float(detector.prob(t))
        except Exception:  # noqa: BLE001
            pa = 0.0
        if pa > 0.7:
            score += round(4 * (pa - 0.5), 2)
            hits.append(f"像 AI（{pa:.2f}）")
    for name, rx, pen in RULES:
        if name in OFF:
            continue
        if ai_open and name == "自称AI":                     # 大家知道她是 AI：可以承认，只是别助手腔、别说提示词
            rx = LEAK
        if rx.search(t):
            score += pen
            hits.append(name)
    if len(t) > 80:
        score += 1.0 + (len(t) - 80) / 40
        hits.append("太长")
    emojis = sum(1 for ch in t if ord(ch) >= 0x1F300)
    if emojis >= 3:
        score += 1.0
        hits.append("emoji 太多")
    flat = re.sub(r"[\s\W_]+", "", t)
    if len(flat) >= 4:
        for r in recent or []:
            ratio = SequenceMatcher(None, flat, re.sub(r"[\s\W_]+", "", r)).ratio()
            if ratio >= 0.95:
                score += HARD
                hits.append("和刚说过的一样")
                break
            if ratio >= sim:
                score += 4.0
                hits.append("和刚说过的太像")
                break
    return score, hits


def check_bubbles(bubbles: list[str], recent: list[str] | None = None, sim: float = 0.75, avoid=None,
                  detector=None, ai_open: bool = False, stale=None, no_names=None, echo=None) -> tuple[float, list[str]]:
    total, hits = 0.0, []
    for b in bubbles:
        s, h = check(b, recent, sim, avoid, detector, ai_open, stale, no_names, echo)
        total = max(total, s) if s >= HARD else total + s
        hits += [x for x in h if x not in hits]
    if len(bubbles) >= 3 and len(set(len(b) // 10 for b in bubbles)) == 1:
        total += 0.5                                  # 三条一样长，像列表
    if len(bubbles) >= 2:
        total += 0.3 * (len(bubbles) - 1)             # 5.0 大多数时候一条就够：一样好的候选里挑条数少的
    return total, hits


def pick(cands: list[list[str]], recent: list[str] | None = None, sim: float = 0.75, avoid=None,
         detector=None, ai_open: bool = False, stale=None, no_names=None, echo=None) -> tuple[int, float, list[str]]:
    """几个候选（每个是要发的几条）→ (挑中的下标, 罚分, 命中的规则)。都不能发返回 (-1, …)。
    罚分一样时选靠前的（第一个是模型自己最想说的）。"""
    scored = [(check_bubbles(c, recent, sim, avoid, detector, ai_open, stale, no_names, echo), i) for i, c in enumerate(cands) if c]
    if not scored:
        return -1, HARD, ["没有候选"]
    ok = [(s, h, i) for (s, h), i in scored if s < HARD]
    if not ok:
        (s, h), _ = min(scored, key=lambda x: x[0][0])
        return -1, s, h
    s, h, i = min(ok, key=lambda x: (x[0], x[2]))
    return i, s, h


# ---------------- 错别字（MaiBot 的 typo generator 的简化版：只用常见的同音 / 形近错字） ----------------
TYPOS = {
    "的": "得", "得": "的", "在": "再", "再": "在", "做": "作", "那": "哪", "哪": "那", "已": "己", "吗": "嘛",
    "以": "已", "象": "像", "像": "象", "到": "道", "道": "到", "带": "戴", "戴": "带", "他": "她", "她": "他",
    "账": "帐", "即": "既", "既": "即", "辩": "辨", "坐": "座", "座": "坐", "震": "振", "须": "需", "需": "须",
    "很": "狠", "哦": "噢", "吧": "把", "呢": "呐", "啦": "拉",
}


def make_typo(text: str, rand: random.Random | None = None) -> tuple[str, str] | None:
    """在一句话里打错一个字 → (打错后的句子, 纠正用的“*正确的词”)；没有可错的字返回 None。"""
    r = rand or random
    protected = set()
    for m in re.finditer(r"\[@[^\]]*\]|@\S+|\[[^\]]*\]", text):      # @名字、[表情:x] 里的字不动
        protected.update(range(m.start(), m.end()))
    idx = [i for i, ch in enumerate(text) if ch in TYPOS and i not in protected]
    if not idx:
        return None
    i = r.choice(idx)
    wrong = text[:i] + TYPOS[text[i]] + text[i + 1:]
    a, b = max(0, i - 1), min(len(text), i + 2)
    fix = text[a:b].strip() or text[i]
    return wrong, f"*{fix}"


# ---------------- 20.1 说出口的数字要有出处（转述走样检查，不调模型） ----------------
_NUM = re.compile(r"\d+(?:[.:/\-]\d+)*%?")
_FW = str.maketrans("０１２３４５６７８９：．％", "0123456789:.%")


def _norm_nums(text: str) -> str:
    return (text or "").translate(_FW).replace("：", ":").replace(" ", "")


def numbers_in(text: str) -> list[str]:
    """她话里要核对的数字：带小数点 / 比分 / 百分号 / 日期分隔的，或者 ≥ 100 的整数（小的整数太常见，不查）。"""
    out = []
    for m in _NUM.finditer(_norm_nums(text)):
        t = m.group(0).rstrip(".-/:")
        if not t:
            continue
        plain = t.rstrip("%")
        if re.fullmatch(r"\d+", plain) and int(plain) < 100 and not t.endswith("%"):
            continue
        out.append(t)
    return list(dict.fromkeys(out))


def unsupported_numbers(text: str, sources: str) -> list[str]:
    """她话里出现、但简报 / 聊天记录 / 记忆里都没有的数字。比分 2:1 和 2-1 算同一个；12.0% 和 12% 算同一个。"""
    src = re.sub(r"(\d)\.0+(?=%)", r"\1", _norm_nums(sources))
    src_alt = src.replace("-", ":").replace("比", ":")
    bad = []
    for t in numbers_in(text):
        cands = {t, t.replace("-", ":"), t.replace(":", "-"), re.sub(r"\.0+(?=%|$)", "", t)}
        if not any(c and (c in src or c in src_alt) for c in cands):
            bad.append(t)
    return bad


# 21.0 说法检查（E3 从数字扩到“重大说法”）：她说某人退役 / 转会 / 去世……简报里得有这个说法
MAJOR_CLAIM = re.compile(r"退役|转会|离队|加盟|去世|逝世|离世|塌房|被捕|官宣|解散|停更|封号|跑路|倒闭|涨价|降价|暴涨|暴跌|夺冠|"
                         r"离婚|结婚|辞职|下架|停服|确诊")
NEGATED = re.compile(r"(没|没有|不是|并没|还没|未|辟谣|谣言|假的)\S{0,4}$")


def unsupported_claims(text: str, sources: str) -> list[str]:
    """她话里的重大说法（动词），简报 / 聊天记录里都没出现过的。说“没退役”“是谣言”这种否定的不算。"""
    bad = []
    for m in MAJOR_CLAIM.finditer(text or ""):
        w = m.group(0)
        if w in (sources or "") or NEGATED.search(text[max(0, m.start() - 6):m.start()]):
            continue
        if w not in bad:
            bad.append(w)
    return bad
