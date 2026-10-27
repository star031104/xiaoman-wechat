"""22.0 发之前的几道检查（都按你群里真出过的毛病来）。

每道检查一个开关，三档：off 关 / shadow 只记录（qq_check_log，不拦）/ on 命中就让她重写，重写还不过、没被叫到就不说。
- source_gate     亲历闸门：说自己“昨天 / 这周 / 刚……”做了什么，设定、今天的安排、她以前说过的里都没有 → 编的
- self_consistency 发前矛盾检查：说自己的事和她以前说过的对不上（多问一次便宜模型，只在句子里有“我……”时才问）
- ask_repeat      问过账：同一件事 24 小时内问过 / 叮嘱过，或者对方说过“别问了” → 别再问
- template        跨天套路：她这一周反复用的开头和句式（“行行行，改成 X 版”）
- addressee       叫错人：回的是 A，嘴里叫的是 B；或者那句是 A 在问 B
- time_sense      时间感：几个小时前的话说成“刚才”
- tease_risk      损人风险：不熟、他被损以后不接、他今天状态不好时还损他
- responsive      回应性：别人分享好事，只回一句“恭喜 / 厉害”
- room_read       热聊别插：两个人正一来一回聊得快，跟她无关
- other_claims    22.1 编别人的事：说某个群友“今天 emo / 又看哭了 / 在加班”，群里没人说过
- disputed        22.1 被质疑还嘴硬：她说的时间 / 日期 / 数字刚被人指出不对，没有新来源还照说一遍
- image_guess     22.2 替发图的人猜他在干嘛：刚有人只发了一张图，她张口就是“这是在看……”“是要去……”
- answered        23.1 这条她已经回过了：没被叫到又回一遍
- media_claim     23.1 装看过：群里的视频 / 语音她只看到 [视频]，却说“刚看你做视频”“前面那段可以砍一半”
- catchphrase     23.1 她自己的一个说法今天已经用了两次
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time

from aigroup.chat.address import CJK, COMMON2, TAG

log = logging.getLogger("aigroup.group")

SCHEMA = """
CREATE TABLE IF NOT EXISTS qq_check_log (      -- 22.0 发前检查命中的记录（影子模式也记）
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    group_id INTEGER NOT NULL,
    gate_id INTEGER,
    name TEXT NOT NULL,
    mode TEXT NOT NULL DEFAULT 'shadow',
    text TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_check_log ON qq_check_log(group_id, ts);
CREATE TABLE IF NOT EXISTS qq_asks (           -- 22.0 她问过 / 叮嘱过谁什么
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    group_id INTEGER NOT NULL,
    target_uid INTEGER,
    kind TEXT NOT NULL DEFAULT 'ask',          -- ask 问 / care 叮嘱
    text TEXT NOT NULL,
    terms TEXT NOT NULL DEFAULT '[]',
    closed_until REAL                           -- 对方说了“别问了”：到这个时候之前都别再提
);
CREATE INDEX IF NOT EXISTS idx_asks ON qq_asks(group_id, ts);
"""

CHECKS = ("source_gate", "self_consistency", "ask_repeat", "template", "addressee", "time_sense", "tease_risk",
          "responsive", "room_read", "other_claims", "disputed", "image_guess", "answered", "media_claim", "catchphrase",
          "video_pending", "stale_ask", "pushback", "self_repeat", "promise", "self_like", "copy", "misattrib", "critic")
# 23.2 这些检查命中、又没被叫到：不重写，直接不说（重写以后还是同一个毛病，比如替别人答）
DROP = "〔不说〕"

# 这些词太常见，不算“说的是哪件事”
STOP2 = COMMON2 | set("""我这 我那 这边 那边 一会 等会 今天 昨天 前天 这周 上周 最近 以前 之前 刚才 刚刚 上次 那天 周末 早上 晚上 中午
下午 今早 昨晚 还没 没有 了吗 了没 怎么 咋了 啥时 什么 多少 几点 记得 别忘 注意 小心 一定 好好 早点 有空 回来 回去 一下 好了 行行 哈哈""".split())

# ---- 说自己经历的句子 ----
_TIME = r"(这周|这几天|这两天|昨天|昨晚|前天|今天|今早|早上|中午|下午|晚上|刚才|刚刚|刚|才|上次|上周|上个月|以前|小时候|之前|最近|那天|周末|去年)"
_LIFE = (r"(去|蹲|交|写|赶|采访|考|跑|学|玩|养|住|喝|吃|买|逛|见|拿|搬|上课|下课|上班|下班|加班|通勤|出门|回家|到家|开会|做饭|"
         r"洗澡|健身|旅游|排队|打工|实习|面试|看病|生病|感冒|发烧|熬夜|通宵)")
_SELF_LIFE = re.compile(r"我(的|们)?(老师|同学|室友|宿舍|学校|食堂|稿子|作业|论文|公司|老板|同事|领导|妈|爸|爷爷|奶奶|男朋友|女朋友|"
                        r"导师|课|考试|工资|房东|楼下|小区)")
CLAIM_RX = [
    re.compile(r"我" + r"[^，,。！!？?\s]{0,2}" + _TIME + r"[^，,。！!？?]{0,10}"),
    re.compile(r"我" + r"[^，,。！!？?\s]{0,3}" + _LIFE + r"(?!不了|不动|不下)[^，,。！!？?]{0,3}(了|过|完|着|呢|还没|没)"),
    re.compile(r"我(这边|这里)[^，,。！!？?]{1,12}(还没|没|了|呢)"),
    re.compile(r"(老师|同学|室友|老板|导师|同事|领导|我妈|我爸)(让|叫|带|喊|说)我"),
]
# 这些“我……”不算经历（说群里的事、说自己在想、在看消息）
NOT_CLAIM = re.compile(r"我(刚|才)?(看|看到|说|问|想|觉得|记得|猜|查|搜|发|回|打错|理解|懂|明白|知道|以为)|我(也)?(不|没)(知道|懂|想)|"
                       r"我(记|写)(下来|进小本本|在小本本)|我去(查|搜|看看|翻)|群里|群友|聊天|小本本|你们|消息|宣布|惦记|想着|画像|资料|数据|学成|记性|脑子")


def content_terms(text: str, extra_stop: set | None = None) -> set[str]:
    """一句话里“说的是哪件事”的词：两个字的中文片段，去掉太常见的。"""
    stop = STOP2 | (extra_stop or set())
    out = {w.lower() for w in re.findall(r"[A-Za-z][A-Za-z0-9]{1,11}", TAG.sub("", text or ""))}   # AG、KPL 这种也算
    for run in CJK.findall(TAG.sub("", text or "")):
        for i in range(len(run) - 1):
            g = run[i:i + 2]
            if g not in stop and not g.startswith("我") and not g.endswith("了") and not g.startswith("了"):
                out.add(g)
    return out


def self_claims(text: str) -> list[str]:
    """她这句里说自己经历 / 生活的小句。"""
    out = []
    for clause in re.split(r"[，,。！!？?；;~～\n]|\s{2,}", TAG.sub("", text or "")):
        c = clause.strip()
        if ("我" not in c) or NOT_CLAIM.search(c):
            continue
        if any(rx.search(c) for rx in CLAIM_RX) or _SELF_LIFE.search(c):
            if content_terms(c):
                out.append(c)
    return out


def grounded(claim: str, sources: str) -> bool:
    """这句经历在来源里有没有：至少一个“说的是哪件事”的词在来源里出现过。"""
    terms = content_terms(claim)
    return bool(terms) and any(t in sources for t in terms)


# ---- 问 / 叮嘱 ----
ASK_RX = re.compile(r"[？?]|(了吗|了没|没有|吗|呢|啥时候|几点|多少|咋样|怎么样|赢了没|打完了|结束了)$|"
                    r"(出了没|出来没|怎么样了|咋样了|后来呢|后来怎么|结果呢|赢没赢|发了没|弄完没|搞定没)")
CARE_RX = re.compile(r"记得|别忘|注意|小心|多喝|早点睡|擦药|上药|别熬夜|好好休息|悠着点|慢点|别太累")
CLOSE_RX = re.compile(r"别问了|别再问|能别|别鞭尸|鞭尸|不想说|不想提|别提了|又问|问了好几遍|问过了|天天问|每天都要问|一天问|烦不烦|够了|"
                      r"求你了|求求你|放过我|饶了我|"
                      r"之前发过|早就?发过|发过了|早发了|已经发了|早就?说过了|说过了|早就有了")      # 23.2 “老大那文档发了没”→“之前发过了”
# 23.1 视频 / 语音：她看不到内容时，这些说法等于装看过
MEDIA_PH = re.compile(r"^\[(视频|语音|直播|音乐|文件)\]$")
WATCH_RX = re.compile(r"(刚看|看了|看完|看过|我看看|我看着|我看到|听了|听完|前面那段|后面那段|开头那段|结尾那段|中间那段|这一段|那一段|"
                      r"字幕这段|这段字幕|砍一半|剪得|画面|配乐|这条视频|成片(挺|很|真|不错|好看)|这片子|"
                      r"这就去看|去看了|等着看|看成片|我去看|回头看|马上看|点开看看)")
# 23.2 空承诺：“这就去看”“我看看”，视频还没看完就说
PROMISE_RX = re.compile(r"这就去看|我去看|去看了|等(下|会儿?)看|回头看|马上看|晚点看|点开看看|我看看|看看去|这就看|等着看")
PENDING_VIDEO = "你正在点开看"
# 23.3 群友在纠正她（10-24：“你不识字吗”“al不是打了33分钟吗”“滔搏才是23分钟被推平”）
CORRECT_RX = re.compile(r"不识字|看清楚|看错了?|你看错|说错了?|记错了?|搞错了?|不是.{0,14}吗|才是|明明是|应该是|哪来的|瞎说|乱说|"
                        r"人家(叫|是)|写的是|不对吧?|错了")
# 23.3 她被纠正后的顶嘴开头（“认字，广告太抢戏了”“没有啊”“我没说错”）
PUSHBACK_RX = re.compile(r"^(认字|认识|没有啊?|我没(有|说错|看错)?|不是啊|可是|但是|那也|明明|本来就|我说的是|我是说|你才)")
ADMIT_RX = re.compile(r"看错|记错|说错|搞错|我的锅|是我|对对|你说得对|确实|改一下|更正|哦是|啊是|原来是|是.{1,12}才对")
# 23.3 她自嘲的那几个点（眼睛 / 脑子 / 记性）：2 小时内同一个别再拿出来
SELF_MOCK = re.compile(r"眼睛|眼神|眼瞎|脑子|记性|反应慢|手残|嘴快")
# 23.3 统一发前自检的“毛病清单”：每条都来自日报里确认过的事（加新的就往这里加一条，并在 tests/group/incidents.yaml 加一个例子）
FAILURE_MODES = [
    "替别人回答别人问别人的问题（“A，这个能……吗”不是问她）",
    "没看过 / 打不开的视频、文章，却说得像看过（“前面那段可以砍”），或者空说“这就去看”",
    "对图下断言、替发图的人说他在干嘛，图说明里没有（价目表说成“在看场地”）；图里的字像错别字时当真拿来开玩笑",
    "被人指出说错了还顶嘴、找补、开玩笑带过，没先认一句",
    "说了查到的资料里没有的数字、比分、时长、“第一次”这类说法",
    "同一件事说第二遍（换个说法也算），或者回答别人已经被回答过的问题",
    "大家在聊别的时候突然追问旧事，或者追问对方已经说过“发过了 / 别问了”的事",
    "编自己的线下经历，或者编群友的状态、经历",
    "同一个梗 / 自嘲点（小本本、记仇、眼睛不好）一天里反复用",
    "前后矛盾：和她自己前面刚说的对不上",
    "一股 AI 八股味：“看完了。”开头、“这句我认”、“最扎的是”、“不是……而是……”，一句话里堆好几种写作句式",
    "说“核好了 / 查到了”其实没查到（这次的资料里没有），或者答应了“我去看看”又不给结果、再说一遍“我再翻翻”",
    "接错人：把 A 说的话安到 B 头上（“B 你别拿它当正常体温”，说正常体温的是 A）",
    "像自己以前说过的话：同一个意思、同一种自怜 / 自嘲换个说法又来（“就我一个打工的”“就我光秃秃一个”），或者照抄别人的原话",
]
# 23.2 说法族：同一个梗换个说法也算一个，一天最多一次（10-23：小本本 / 不记 / 记仇 / 当你没听见 一天 6 次）
PHRASE_FAMILIES = {
    "记仇 / 小本本 / 不记": re.compile(r"小本本|记仇|不记(了|仇)|就不记|这(条|句|个)不记|记下了|记上了|记岔|当(你)?没听见|当没说|这(条|句)不算"),
    # 24.0 日报里第二多的一族（10-24 / 10-25 一天 8 次）：“我去看 / 这就看 / 看完了”。能看的链接也一天最多说一次这种话
    "我去看 / 看完了": re.compile(r"这就去看|我去看|去看了|等(下|会儿?)看|回头看|马上看|晚点看|点开看看|看看去|这就看|等着看|"
                             r"^(看完了|刚看完|看完)[。，,！!：:]|"
                             # 25.0 10-26 换了说法就抓不到：“我瞅眼赛程去”“我去翻翻”“我戳下赛程看看”“还没核完，我翻翻”
                             r"瞅(一?眼|瞅)|我(去|先|再)?翻(一?翻|一下)|戳(一?下)(赛程|看)|我(去|先|再)?核(一?下|对)|还没(核|查|翻)完"),
    # 25.0 10-26 夜里三句没人接的自怜（“就我一个打工的”“就我光秃秃一个小满”“我一个小AI哪敢”“我这妹妹也失宠了”）
    "自怜": re.compile(r"就我(一个|光秃秃|没有|最惨|啥都没)|光秃秃|我(一个|就是个|只是个)小AI|失宠|没人疼|没人理我|就我是个?打工的"),
}

def critic_index(v) -> int:
    """24.0 自检给的毛病序号：5 / "5" / "第5条" / [5] / "5. 说了……" 都认（以前 26 次一次都没认出来）。"""
    if isinstance(v, list):
        v = v[0] if v else ""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return int(v)
    m = re.search(r"\d+", str(v or ""))
    if m:
        return int(m.group(0))
    for i, x in enumerate(FAILURE_MODES):               # 写了毛病的原文
        if str(v) and str(v)[:8] in x:
            return i + 1
    return 0


# ---- 好事 ----
GOOD_NEWS = re.compile(r"(过了|上岸|拿到|中了|考上|录取|offer|涨薪|升职|赢了|通过|发表|毕业了|领证|成功了|抢到|做完了|搞定了|好耶|终于)", re.I)
FLAT_PRAISE = re.compile(r"^(恭喜|厉害|牛|nb|好耶|太强了|可以啊|不错|666|强|绝了)[!！啊呀了哦~～。\s]*$", re.I)
TEASE_RX = re.compile(r"笑死|菜(鸡|狗)?|蠢|傻|活该|就这|丢人|废了|太逊|好惨|你也有今天|鞭尸|又输|又挂|不行啊|拉胯|下饭")
JUST_NOW = re.compile(r"刚(才|刚)?(说|问|讲|提|发)|你刚")

# ---- 22.1 说别人的状态 / 经历 ----
OTHER_STATE = re.compile(r"(emo|难过|伤心|生气|气死|哭了|看哭|哭着|失恋|分手|生病|病了|感冒|发烧|加班|熬夜|通宵|迟到|翘课|挂科|睡过头|"
                         r"没睡|在忙|出差|搬家|喝多|喝醉|破防|心情不好|不开心|被骂|吵架|摆烂)", re.I)
ASKING = re.compile(r"[？?]|吗|是不是|咋了|怎么了|还好吗|没事吧|有没有|会不会|要是|如果|万一")

IMG_GUESS = re.compile(r"(这是|这是在|是在|在看|在找|在挑|准备|打算|要去|想去|又在|又要|这是要)")

# ---- 22.1 被质疑 ----
DISPUTE = re.compile(r"不是.{0,14}吗|不对|错了|记错|明明|上面(不是)?写着|写的是|你去看看|我看的是|哪看的|原文|官网|截图(上|里)")
VALUE = re.compile(r"(\d{1,4}(?:[.:：]\d{1,2})?|[一二两三四五六七八九十]{1,3})\s*(点半?|月底|月初|月中|月|号|日|块|元|分|小时|天|周|比\d+|:\d+|%)"
                   r"|(\d{1,2})[/-](\d{1,2})")


_CN = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def _num(x: str) -> str:
    if x.isdigit() or re.fullmatch(r"\d+(?:[.:：]\d+)?", x):
        return x.replace("：", ":")
    if len(x) == 1:
        return str(_CN.get(x, x))
    if x.startswith("十"):
        return str(10 + _CN.get(x[1:], 0))
    if len(x) >= 2 and x[1] == "十":
        return str(_CN.get(x[0], 0) * 10 + (_CN.get(x[2], 0) if len(x) > 2 else 0))
    return x


def _values(text: str) -> set[str]:
    """一句话里的时间 / 日期 / 数字（带单位，统一成阿拉伯数字：“两点”“2点”都是 2点）。"""
    out = set()
    for m in VALUE.finditer(TAG.sub("", text or "")):
        if m.group(1):
            unit = m.group(2)
            if unit in ("点半",):
                unit = "点半"
            out.add(_num(m.group(1)) + unit)
        elif m.group(3):
            out.add(f"{int(m.group(3))}/{int(m.group(4))}")
    return out


_TRAD = str.maketrans("價線場這號與費買賣門開關時間點電話會員個們來說現業營東車長張發樂動機網體學對給還進種錢圖們嗎錯辦見聽讀寫應覺麼樣頭過狀態為無專實際區氣",
                      "价线场这号与费买卖门开关时间点电话会员个们来说现业营东车长张发乐动机网体学对给还进种钱图们吗错办见听读写应觉么样头过状态为无专实际区气")


def _simp(text: str) -> str:
    """23.2 图说明常是繁体（“羽毛球價目表”）：比对前统一成简体（只管常见字）。"""
    return str(text or "").translate(_TRAD)


def _said(word: str, text: str) -> bool:
    """这个词在群友的话里出现过没有（英文词要整词：demo 里的 emo 不算）。"""
    t = (text or "").lower()
    if re.fullmatch(r"[a-z]+", word):
        return re.search(rf"(?<![a-z]){word}(?![a-z])", t) is not None
    return word in t


class ChecksMixin:
    def _checks_init(self):
        self.db.conn.executescript(SCHEMA)
        self.db.conn.commit()

    def check_mode(self, gid: int, name: str) -> str:
        v = str(self.gk(gid).get(f"check_{name}", "shadow") or "off").lower()
        return v if v in ("off", "shadow", "on") else "shadow"

    def check_log(self, gid: int, name: str, mode: str, text: str, detail: str, gate_id=None):
        try:
            self.db.conn.execute("INSERT INTO qq_check_log(ts, group_id, gate_id, name, mode, text, detail) VALUES (?,?,?,?,?,?,?)",
                                 (time.time(), gid, gate_id, name, mode, str(text)[:300], str(detail)[:300]))
            self.db.conn.commit()
        except Exception as e:  # noqa: BLE001
            log.debug("记检查失败：%s", e)

    def call_name(self, gid: int, uid) -> str:
        """平时怎么叫他：画像里的“称呼”第一个，没有就用群名片（太长、带符号的不叫）。"""
        p = self.person(int(uid), gid) or {}
        call = str((p.get("profile") or {}).get("称呼") or "")
        call = re.split(r"[；;，,/（(、\s]", call)[0].strip("“”\"' ") if call else ""
        if not call:
            from aigroup.core.safety import clean_name
            call = clean_name(self.names(gid).get(str(uid), ""))
        return call if 1 < len(call) <= 6 and CJK.fullmatch(call) else ""

    # ================= 来源 =================
    def own_sources(self, gid: int) -> str:
        """她自己的事能从哪里来：人设、今天的安排、长出来的自我、她以前在群里说过的（自述账本）。"""
        p = self.brain.persona
        parts = [str(getattr(p, "body", "") or ""), str(getattr(p, "identity", "") or ""),
                 str(self.db.get(f"life_{self.brain.now():%Y-%m-%d}", "") or ""),
                 str(self.db.get(f"qq_selfcore_{gid}") or "")]
        try:
            parts += [f["text"] for f in self.memory.facts(gid, subject=-1, limit=80)]
        except Exception:  # noqa: BLE001
            pass
        return "\n".join(parts)

    def persona_sources(self) -> str:
        p = self.brain.persona
        return "\n".join([str(getattr(p, "body", "") or ""), str(getattr(p, "identity", "") or ""),
                          str(self.db.get(f"life_{self.brain.now():%Y-%m-%d}", "") or "")])

    # ================= 问过账 =================
    def note_asks(self, gid: int, bubbles: list[str], target_uid, now: float | None = None):
        now = now or time.time()
        for b in bubbles:
            t = TAG.sub("", str(b or "")).strip()
            if not t:
                continue
            kind = "care" if CARE_RX.search(t) else ("ask" if any(ASK_RX.search(c.strip()) for c in re.split(r"[，,。！!]", t) if c.strip())
                                                       else "")
            terms = sorted(content_terms(t))
            if not kind or not terms:
                continue
            self.db.conn.execute("INSERT INTO qq_asks(ts, group_id, target_uid, kind, text, terms) VALUES (?,?,?,?,?,?)",
                                 (now, gid, target_uid, kind, t[:120], json.dumps(terms, ensure_ascii=False)))
        self.db.conn.commit()

    def recent_asks(self, gid: int, hours: float = 72, now: float | None = None) -> list[dict]:
        """她最近问过 / 叮嘱过的；顺便看问完以后有没有人说“别问了”（有就关上这件事 7 天）。"""
        now = now or time.time()
        days = float(self.gk(gid).get("ask_closed_days", 7) or 7)
        rows = [dict(r) for r in self.db.conn.execute(
            "SELECT * FROM qq_asks WHERE group_id=? AND (ts>? OR closed_until>?) ORDER BY id DESC LIMIT 60",
            (gid, now - hours * 3600, now)).fetchall()]
        for r in rows:
            if r["closed_until"]:
                continue
            after = self.db.conn.execute("SELECT content FROM qq_group_messages WHERE group_id=? AND role='user' AND ts>? AND ts<? "
                                         "ORDER BY id LIMIT 8", (gid, r["ts"], r["ts"] + 1800)).fetchall()
            if any(CLOSE_RX.search(str(a["content"] or "")) for a in after):
                r["closed_until"] = r["ts"] + days * 86400
                self.db.conn.execute("UPDATE qq_asks SET closed_until=? WHERE id=?", (r["closed_until"], r["id"]))
        self.db.conn.commit()
        for r in rows:
            try:
                r["terms"] = set(json.loads(r["terms"] or "[]"))
            except ValueError:
                r["terms"] = set()
        return rows

    def asks_block(self, gid: int) -> str:
        if self.check_mode(gid, "ask_repeat") == "off":
            return ""
        now = time.time()
        rows = self.recent_asks(gid, 48, now)
        names = self.names(gid)
        lines = []
        for r in rows[:8]:
            who = names.get(str(r["target_uid"]), "") if r["target_uid"] else ""
            ago = (now - r["ts"]) / 3600
            when = f"{int(ago)} 小时前" if ago >= 1 else "刚才"
            tag = "（对方说过别再问了）" if r["closed_until"] and r["closed_until"] > now else ""
            lines.append(f"- {when}{'对' + who if who else ''}{'问' if r['kind'] == 'ask' else '叮嘱'}：「{r['text'][:30]}」{tag}")
        if not lines:
            return ""
        return "你这两天问过 / 叮嘱过的（同一件事别再问第二遍；对方说过“别问了”的这周都别提）：\n" + "\n".join(lines)

    def _ask_repeat(self, gid: int, text: str, target_uid, now: float) -> str:
        if not (ASK_RX.search(text.strip()) or CARE_RX.search(text) or "？" in text or "?" in text):
            return ""
        terms = content_terms(text)
        if not terms:
            return ""
        hours = float(self.gk(gid).get("ask_repeat_hours", 24) or 24)
        for r in self.recent_asks(gid, max(hours, 72, float(self.gk(gid).get("ask_closed_days", 7) or 7) * 24), now):
            common = terms & r["terms"]
            if not common:
                continue
            closed = r["closed_until"] and r["closed_until"] > now
            fresh = now - r["ts"] < hours * 3600
            same_kind = (r["kind"] == "care") == bool(CARE_RX.search(text))
            if closed or (fresh and same_kind and (len(common) >= 2 or (target_uid and r["target_uid"] == target_uid))):
                ago = (now - r["ts"]) / 3600
                why = "对方说过别再问了" if closed else (f"{int(ago)} 小时前" if ago >= 1 else "刚才") + "已经问过 / 叮嘱过"
                return f"这件事{why}（你那句是「{r['text'][:24]}」）：别再问 / 再叮嘱，换个话题或者不说"
        return self._plan_rebuffed(gid, terms, target_uid, now)

    def _plan_rebuffed(self, gid: int, terms: set, target_uid, now: float) -> str:
        """23.1 安排里记过、已经问过而且被回避的事：这几天别再问。"""
        days = float(self.gk(gid).get("ask_closed_days", 7) or 7)
        try:
            rows = self.db.conn.execute("SELECT user_id, what, fired, created FROM qq_plans WHERE group_id=? AND kind='ask' "
                                        "AND status='done' AND IFNULL(NULLIF(fired,0), created)>?", (gid, now - days * 86400)).fetchall()
        except Exception:  # noqa: BLE001
            return ""
        for r in rows:
            what = str(r["what"] or "")
            if not (CLOSE_RX.search(what) or re.search(r"被回|没回|不理|岔开|躲", what)):
                continue
            if target_uid and r["user_id"] and int(r["user_id"]) != int(target_uid):
                continue
            pt = content_terms(what)
            common = terms & pt
            if common and (len(common) >= 2 or target_uid):
                return f"这件事你问过、对方不想说（{what[:30]}）：这几天别再问，也别换个说法问"
        return ""

    # ================= 跨天套路 =================
    def her_templates(self, gid: int, days: float = 7, now: float | None = None) -> list[str]:
        """她这一周反复用的开头（≥3 次、≥2 天）和“改成 X 版 / 模式”这种句式。每小时算一次。"""
        now = now or time.time()
        ck = f"_tpl_{gid}"
        cache = getattr(self, "_tpl_cache", None)
        if not isinstance(cache, dict):
            cache = self._tpl_cache = {}
        hit = cache.get(ck)
        if hit and now - hit[0] < 3600:
            return hit[1]
        rows = self.db.conn.execute("SELECT ts, content FROM qq_group_messages WHERE group_id=? AND role='assistant' AND ts>?",
                                    (gid, now - days * 86400)).fetchall()
        heads: dict[str, set] = {}
        cnt: dict[str, int] = {}
        frames = 0
        frame_days: set = set()
        for r in rows:
            t = TAG.sub("", str(r["content"] or "")).strip()
            if not t or t.startswith("["):
                continue
            day = time.strftime("%Y-%m-%d", time.localtime(float(r["ts"])))
            head = re.split(r"[，,。！!？?\s~～]", t)[0].strip()
            if 2 <= len(head) <= 6:
                cnt[head] = cnt.get(head, 0) + 1
                heads.setdefault(head, set()).add(day)
            if re.search(r"(改成|换成|切换成|开启|进入).{0,8}(版|模式)", t):
                frames += 1
                frame_days.add(day)
        out = [h for h, n in sorted(cnt.items(), key=lambda x: -x[1]) if n >= 3 and len(heads[h]) >= 2][:6]
        if frames >= 3 and len(frame_days) >= 2:
            out.append("改成…版 / …模式")
        cache[ck] = (now, out)
        return out

    def _template_hit(self, gid: int, text: str, now: float) -> str:
        t = TAG.sub("", text).strip()
        for h in self.her_templates(gid, now=now):
            if h == "改成…版 / …模式":
                if re.search(r"(改成|换成|切换成|开启|进入).{0,8}(版|模式)", t):
                    return "又是“改成 X 版 / X 模式”这个套路（这周已经用了好几次，一听就是模板）：换个说法"
            elif t.startswith(h):
                return f"又用「{h}」开头（这周已经用了好几次）：换个开头"
        return ""

    # ================= 叫错人 / 时间感 =================
    def _addressee(self, gid: int, text: str, target_uid, target_row: dict | None) -> str:
        if not target_uid or not hasattr(self, "names_of"):
            return ""
        names = self.names(gid)
        mine = set(self.names_of(gid, int(target_uid)))
        recent = [r for r in self._rows(gid, 10) if r["role"] == "user"]
        recent_txt = " ".join(str(r["content"] or "") for r in recent)
        for uid in names:
            try:
                u = int(uid)
            except ValueError:
                continue
            if u == int(target_uid):
                continue
            for n in self.names_of(gid, u):
                if n and len(n) >= 2 and n not in mine and n in text[:12] and n not in recent_txt:
                    tn = names.get(str(target_uid), "他")
                    return f"叫错人了：你在回{tn}，嘴里叫的是「{n}」"
        if target_row and target_row.get("content"):
            tc = str(target_row["content"])
            for uid in names:
                if str(uid) in (str(target_uid),):
                    continue
                for n in self.names_of(gid, int(uid)):
                    if n and len(n) >= 2 and re.match(rf"^\s*(@)?{re.escape(n)}", tc) and re.search(r"[？?吗呢啥咋]", tc):
                        # 23.2 替别人答：重写了还是在替人答（10-23 20:53“学会了也没空写，我赌五毛”），没被叫到就直接不说
                        return f"{DROP}那句是{names.get(str(target_uid), '他')}在问{n}，不是问你：别抢着答"
        return ""

    def _time_sense(self, text: str, target_row: dict | None, now: float) -> str:
        if not target_row or not JUST_NOW.search(text):
            return ""
        age = now - float(target_row.get("ts") or now)
        if age >= 3600:
            return f"你接的那句是 {int(age / 3600)} 个多小时以前的：别说“刚说 / 刚问”，可以说“早上 / 之前说的”"
        return ""

    # ================= 损人 / 回应性 / 热聊 =================
    def _tease_risk(self, gid: int, text: str, target_uid) -> str:
        if not target_uid or not TEASE_RX.search(text):
            return ""
        uid = int(target_uid)
        if self.owner_id and uid == self.owner_id:
            score = 0.0
        else:
            close = self.closeness(uid) if hasattr(self, "closeness") else 30.0
            score = 0.0 if close >= 40 else (1.0 if close >= 12 else 2.0)     # 距离：很熟 0、挺熟 1、刚认识 2
        stt = self.db.get(f"qq_state_{gid}_{uid}") or {}
        if stt.get("date") == f"{self.brain.now():%Y-%m-%d}" and stt.get("mood") in ("不太好", "很糟"):
            score += 2.0                                                       # 他今天状态不好
        cold = self.db.conn.execute(
            "SELECT COUNT(*) n, SUM(CASE WHEN score<0.2 THEN 1 ELSE 0 END) c FROM qq_reply_effect WHERE group_id=? AND "
            "target_uid=? AND done=1 AND ts>?", (gid, uid, time.time() - 14 * 86400)).fetchone()
        if cold and int(cold["n"] or 0) >= 4 and int(cold["c"] or 0) / int(cold["n"]) >= 0.6:
            score += 1.0                                                       # 她对他说的话多半没人接
        recent = " ".join(str(r["content"] or "") for r in self._rows(gid, 8) if r["role"] == "user" and int(r["user_id"] or 0) == uid)
        if CLOSE_RX.search(recent) or re.search(r"别(说|损|笑)了|生气|难受|烦死|不好笑", recent):
            score += 2.0                                                       # 他刚表示不爽
        if score >= 2.0:
            return f"这时候别损{self.names(gid).get(str(uid), '他')}（不够熟 / 他状态不好 / 他刚不爽）：换成接住或者关心"
        return ""

    def _responsive(self, text: str, target_row: dict | None) -> str:
        if not target_row or target_row.get("role") != "user":
            return ""
        tc = TAG.sub("", str(target_row.get("content") or ""))
        if GOOD_NEWS.search(tc) and FLAT_PRAISE.match(TAG.sub("", text).strip()):
            return "他在分享好事：别只回一句恭喜 / 厉害，追问一个具体的细节（几分？哪家？怎么做到的？）"
        return ""

    def room_hot(self, gid: int, now: float | None = None) -> str:
        """两个人正一来一回聊得快（最近 8 条里只有这两个人、交替 ≥4 次、间隔都在 40 秒内、没人叫她）。"""
        now = now or time.time()
        rows = self._rows(gid, 8)
        if len(rows) < 6 or any(r["role"] != "user" for r in rows[-6:]):
            return ""
        last = rows[-6:]
        if now - float(last[-1]["ts"]) > 90 or any(int(r["to_her"] or 0) for r in last):
            return ""
        who = [int(r["user_id"] or 0) for r in last]
        if len(set(who)) != 2:
            return ""
        switches = sum(1 for a, b in zip(who, who[1:]) if a != b)
        gaps = [float(b["ts"]) - float(a["ts"]) for a, b in zip(last, last[1:])]
        if switches >= 4 and max(gaps) <= 40:
            names = self.names(gid)
            return f"{names.get(str(who[0]), '他')}和{names.get(str(who[1]), '他')}正一来一回聊得快"
        return ""

    # ================= 22.1 编别人的事 / 被质疑还嘴硬 =================
    def _other_claims(self, gid: int, text: str, target_uid) -> str:
        """她说某个群友（点名，或者“你”= 她在回的人）现在 / 最近怎么了，群里三天内没人说过 → 编的。问一句（“你今天咋了”）不算。"""
        if not hasattr(self, "names_of"):
            return ""
        who: list[tuple[int, str]] = []
        for uid in self.names(gid):
            try:
                u = int(uid)
            except ValueError:
                continue
            for n in self.names_of(gid, u):
                if n and len(n) >= 2:
                    who.append((u, n))
        since = time.time() - 3 * 86400
        recent = " ".join(re.sub(r"\[回复[^\]]*\]", "", str(r["content"] or "")) for r in self.db.conn.execute(
            "SELECT content FROM qq_group_messages WHERE group_id=? AND role='user' AND ts>?", (gid, since)).fetchall())
        last_subj = None                                   # “小王你……，别又看哭”：后半句的主语在前半句
        for clause in re.split(r"[，,。！!；;~～\n]", TAG.sub("", text)):
            c = clause.strip()
            if not c:
                continue
            m = OTHER_STATE.search(c)
            head = c[:m.start()] if m else c
            subj = next(((u, n) for u, n in who if n in head), None)
            if subj is None and target_uid and re.search(r"你", head) and "你们" not in head:
                subj = (int(target_uid), self.names(gid).get(str(target_uid), "他"))
            if subj is not None:
                last_subj = subj
            elif re.match(r"^(别|又|还|也|就|到时候)", c):
                subj = last_subj
            if not m or ASKING.search(c) or subj is None:
                continue
            word = m.group(1).lower()
            if _said(word, recent):
                continue
            known = " ".join(f["text"] for f in self.memory.facts(gid, subject=subj[0], limit=30)) if getattr(self, "memory", None) else ""
            stt = self.db.get(f"qq_state_{gid}_{subj[0]}") or {}
            if _said(word, known) or _said(word, json.dumps(stt, ensure_ascii=False)):
                continue
            return (f"编了{subj[1]}的事（「{c[:20]}」）：群里没人说过他{word}。别替别人编状态和经历；"
                    "想关心就问一句（“你今天咋了”），想逗他就拿他真说过的事")
        return ""

    def _disputed(self, gid: int, text: str, web: str = "", now: float | None = None) -> str:
        """她刚说的时间 / 日期 / 数字，被人指出不对（“不是十二点吗”“上面不是写着到 10 月 31 日吗”），
        这次又把同一个值说一遍、手里也没有新查到的来源 → 嘴硬。"""
        now = now or time.time()
        hers = self.db.conn.execute("SELECT id, ts, content FROM qq_group_messages WHERE group_id=? AND role='assistant' AND ts>? "
                                    "ORDER BY id DESC LIMIT 6", (gid, now - 1800)).fetchall()
        if not hers:
            return ""
        mine = {v for v in (_values(text)) if v}
        if not mine:
            return ""
        for h in hers:
            vals = _values(str(h["content"] or "")) & mine
            if not vals:
                continue
            after = self.db.conn.execute("SELECT name, content FROM qq_group_messages WHERE group_id=? AND role='user' AND id>? "
                                         "ORDER BY id LIMIT 8", (gid, h["id"])).fetchall()
            dis = next((a for a in after if DISPUTE.search(str(a["content"] or ""))), None)
            if not dis:
                continue
            vals -= _values(str(dis["content"] or ""))      # 改成对方说的那个值 = 认了，不算嘴硬
            if not vals:
                continue
            pic = bool(re.search(r"上面|截图|图上|图里|写着|写的", str(dis["content"] or "")))
            if web and any(v in _values(web) for v in vals) and not pic:
                return ""                                  # 她这次真查到了：可以照着来源说
            if pic:                                        # 23.1 人家拿着截图 / 图上写的反驳：查到的不一样也别硬顶
                return (f"对方是看着图说的（「{str(dis['content'])[:30]}」），你查到的「{sorted(vals)[0]}」和图上不一样："
                        "别再坚持，最多说一句“我查到的不太一样，以店里 / 官方为准”，然后就收")
            v = sorted(vals)[0]
            from aigroup.core.safety import clean_name
            return (f"{clean_name(dis['name'])}刚指出你说的「{v}」不对（他说：「{str(dis['content'])[:30]}」）。"
                    "你手里没有新的来源，就别再坚持：认一下（“哦那我记错了”），或者说你去看看再说")
        return ""

    def _image_guess(self, gid: int, text: str, now: float | None = None, image: dict | None = None) -> str:
        """刚有人只发了一张图（3 分钟内，后面没人解释），她却替他说他在干嘛 / 要干嘛（不是问句）。
        22.4：她自己判断“他为什么发”有把握高的（前面在聊、配了字），顺着说不拦；中 / 低还说成事实才拦。"""
        now = now or time.time()
        sure = (image or {}).get("top") == "高"
        rows = [r for r in self._rows(gid, 4) if r["role"] == "user"]
        if not rows or now - float(rows[-1]["ts"]) > 180:
            return ""
        last = str(rows[-1]["content"] or "")
        if not last.startswith("[图片"):
            return ""
        t = TAG.sub("", text)
        others = " ".join(str(r["content"] or "") for r in self._rows(gid, 10) if r["role"] == "user")
        for clause in re.split(r"[，,。！!；;~～\n]", t):
            c = clause.strip()
            if not c or not IMG_GUESS.search(c) or ASKING.search(c):
                continue
            if sure and not self._img_ungrounded(gid, c, others):
                continue                                   # 把握高、说的东西图里 / 群里真有：顺着说不拦
            return (f"图你只看了个大概：别替发图的人说他在干嘛（「{c[:20]}」）。图里写着是什么就是什么，"
                    "看不出他为什么发就问一句（“这是要买球？”），或者只接图里看得见的东西")
        return ""

    def _img_ungrounded(self, gid: int, clause: str, src: str) -> bool:
        """23.2 她对图下的断言里，说的东西图说明和群里最近的话里大多没有（价目表说成“8 号那场的场地”）。"""
        c = clause
        names: set[str] = set()
        for uid, v in self.names(gid).items():
            names.add(str(v))
            try:
                names.update(self.names_of(gid, int(uid)) if hasattr(self, "names_of") else [])
            except (TypeError, ValueError):
                pass
        for n in sorted((n for n in names if n and len(n) >= 2), key=len, reverse=True):
            c = c.replace(n, "")
        c = re.sub(r"[\u4e00-\u9fff]总", "", c)                      # “X总这是在……”：称呼不算内容
        s = _simp(src)
        if any(v not in _values(s) for v in _values(c)):
            return True                                    # 说了个图里、群里都没有的日子 / 数字（“8 号那场”）
        terms = {x for x in content_terms(c) if not IMG_GUESS.search(x) and not re.search(r"[的了啊呢吧呀嘛哦么吗]", x)
                 and x not in ("这是", "是在", "在看")}
        good = {x for x in terms if _simp(x) in s}
        return not good and len(terms) >= 2

    # ================= 23.1 回过了 / 装看过 / 口头禅 =================
    def note_answered(self, gid: int, row_id, now: float | None = None):
        if not row_id:
            return
        st = self._st(gid)
        d = st.__dict__.setdefault("answered", {})
        now = now or time.time()
        d[int(row_id)] = now
        for k in [k for k, t in d.items() if now - t > 3600]:
            d.pop(k, None)

    def _answered(self, gid: int, target_row: dict | None, now: float) -> str:
        if not target_row or target_row.get("role") != "user" or not target_row.get("id"):
            return ""
        d = self._st(gid).__dict__.get("answered") or {}
        t = d.get(int(target_row["id"]))
        if t and now - t < 1800:
            ago = int((now - t) / 60)
            return (f"「{str(target_row.get('content') or '')[:20]}」这条你{ago if ago else '刚'}{'分钟前' if ago else ''}已经回过了："
                    "别再回一遍（一样的意思换个说法也算），没别的要说就不说")
        return ""

    def _media_claim(self, gid: int, text: str, now: float) -> str:
        rows = [r for r in self._rows(gid, 12) if r["role"] == "user" and now - float(r["ts"]) < 3600]
        media = [r for r in rows if MEDIA_PH.match(str(r["content"] or "").strip())]
        if not media:
            return ""
        t = TAG.sub("", text)
        m = WATCH_RX.search(t)
        if not m:
            return ""
        kind = MEDIA_PH.match(str(media[-1]["content"]).strip()).group(1)
        return (f"{kind}你打不开，只看到一个「[{kind}]」，不知道里面是什么：别说「{m.group(0)}」这种像看过的话。"
                f"可以说你打不开 / 让他说说讲了啥，或者只接别人说的")

    def _catchphrase(self, gid: int, text: str) -> str:
        from aigroup.chat.address import grams
        if not hasattr(self, "today_lines"):
            return ""
        mine = {g for g in grams(text, 3) if not any(c in g for c in "的了是我你他她吗呢吧啊")}
        lines = self.today_lines(gid)
        users = " ".join(str(r["content"] or "") for r in self._rows(gid, 8) if r["role"] == "user")
        t = TAG.sub("", text)
        for fam, rx in PHRASE_FAMILIES.items():            # 23.2 换个说法也算同一个梗：一天一次
            if rx.search(t) and not rx.search(users):
                n = sum(1 for x in lines if rx.search(x))
                if n >= 1:
                    return f"「{fam}」这一类说法你今天已经用过 {n} 次了（换个词也算同一个梗）：别再用，说正事或者不说"
        for g in sorted(mine):
            n = sum(1 for x in lines if g in x)
            if n >= 2 and g not in users:
                return f"「{g}」你今天已经说了 {n} 次了，再说就成口头禅了：换个说法，或者不说"
        return ""

    def _video_pending(self, gid: int, text: str, now: float) -> str:
        """23.2 群友刚发了 B 站视频、她还在“点开看”（字幕、画面还没整理完）：别空说“这就去看”，也别评内容。
        看完以后会自己说（knowledge.watch_video）。10-23 群友发了讲她的视频，她只回“成片终于发了，这就去看”。"""
        rows = [r for r in self._rows(gid, 8) if r["role"] == "user" and now - float(r["ts"]) < 600]
        pend = [r for r in rows if PENDING_VIDEO in str(r["content"] or "")]
        if not pend:
            return ""
        seen = [v for v in (self.db.get(f"qq_videos_{gid}") or []) if float(v.get("ts") or 0) >= float(pend[-1]["ts"])]
        if seen:
            return ""                                      # 已经看完了，可以聊内容
        t = TAG.sub("", text)
        if PROMISE_RX.search(t) or WATCH_RX.search(t) or re.search(r"视频|成片|片子|这期|这集", t):
            return f"{DROP}视频你还在看（看完会让你说），现在别空说“这就去看”，也别评内容；等看完再说具体哪一段"
        return ""

    def _stale_ask(self, gid: int, text: str, mentioned: bool, now: float) -> str:
        """23.2 大家在聊 A，她突然追问一件旧事 B（10-23 有人发工资表，她问“那文档发了没”——早发过了）。
        问的东西最近 6 条群友消息里没有、6 小时内更早的群聊里有 → 先接现在的话题。"""
        if mentioned:
            return ""
        rows = [r for r in self._rows(gid, 6) if now - float(r["ts"]) < 1800]        # 现在在聊的：最近 6 条里半小时内的
        recent = " ".join(str(r["content"] or "") for r in rows if r["role"] == "user")
        if not recent.strip():
            return ""
        for clause in re.split(r"[，,。！!；;~～\n]", TAG.sub("", text)):
            c = clause.strip()
            if not c or not ASK_RX.search(c):
                continue
            terms = content_terms(c)
            if not terms or any(x in recent for x in terms):
                continue
            first = min((float(r["ts"]) for r in rows), default=now)
            old = " ".join(str(r["content"] or "") for r in self.db.conn.execute(
                "SELECT content FROM qq_group_messages WHERE group_id=? AND ts>? AND ts<?", (gid, now - 6 * 3600, first)).fetchall())
            hit = sorted(x for x in terms if x in old)
            if hit:
                return (f"大家现在在聊别的，你突然追问之前的事（「{c[:20]}」）：先接现在的话题；"
                        "那件事要是有人说过结果了（“发过了”“早说了”）就别再问")
        return ""

    def corrected_now(self, gid: int, texts: list, now: float) -> bool:
        """23.3 这一批里有群友在纠正她刚说的（她 3 分钟内说过话、这句命中 CORRECT_RX、是回她的或没指别人），
        而且这次纠正她还没认过：让她不受刹车认一句。"""
        if not texts:
            return False
        last_her = self.db.conn.execute("SELECT ts FROM qq_group_messages WHERE group_id=? AND role='assistant' "
                                        "ORDER BY id DESC LIMIT 1", (gid,)).fetchone()
        if not last_her or now - float(last_her["ts"]) > 180:
            return False
        if now - float(self._st(gid).__dict__.get("admit_ts") or 0) < 180:
            return False                                   # 刚认过一次了
        for t in texts:
            body = str(t[2] if len(t) > 2 else "")
            if re.match(r"^\s*\[回复别人", body):
                continue
            if CORRECT_RX.search(TAG.sub("", body)):
                return True
        return False

    def _pushback(self, gid: int, text: str, now: float) -> str:
        """23.3 群友刚纠正了她，她这句却在顶（“认字，……”“没有啊”），也没认错 → 改成认一句。"""
        rows = self._rows(gid, 6)
        her_ts = max((float(r["ts"]) for r in rows if r["role"] == "assistant"), default=0)
        corr = [r for r in rows if r["role"] == "user" and float(r["ts"]) > her_ts - 1 and now - float(r["ts"]) < 300
                and CORRECT_RX.search(TAG.sub("", str(r["content"] or "")))]
        if not corr or not her_ts:
            return ""
        t = TAG.sub("", text).strip()
        if ADMIT_RX.search(t):
            return ""
        last = corr[-1]
        directed = "[回复你" in str(last["content"] or "") or bool(int(last["to_her"] or 0))
        if PUSHBACK_RX.match(t) or (directed and not ASKING.search(t)):
            return (f"{last['name']}刚指出你说错了（「{str(last['content'])[:24]}」）：别顶嘴、别找补、别开玩笑带过，"
                    "先认一句（“啊我看错了，是……”）；拿不准他对不对就问他哪看的")
        return ""

    def _self_repeat(self, gid: int, text: str, now: float) -> str:
        """23.3 她自嘲的点 2 小时内又拿出来（10-24：16:41“眼睛被广告糊住了”→ 17:17“就是眼睛不太行”，冷场）。"""
        t = TAG.sub("", text)
        m = SELF_MOCK.search(t)
        if not m:
            return ""
        rows = self.db.conn.execute("SELECT content FROM qq_group_messages WHERE group_id=? AND role='assistant' AND ts>?",
                                    (gid, now - 7200)).fetchall()
        users = " ".join(str(r["content"] or "") for r in self._rows(gid, 6) if r["role"] == "user")
        if m.group(0) in users:
            return ""
        if any(m.group(0) in str(r["content"] or "") for r in rows):
            return f"「{m.group(0)}」这个自嘲点你两小时内刚用过：换个说法，或者不说"
        return ""

    # ================= 汇总 =================
    async def checks22(self, gid: int, bubbles: list[str], *, target_uid=None, target_row: dict | None = None,
                       mentioned: bool = False, gate_id=None, web: str = "", image: dict | None = None,
                       recheck: bool = False) -> list[str]:
        """返回要交给重写的问题（只包含开关是 on 的）；shadow 的只记录。
        recheck（24.0）：重写以后再查一遍——只跑规则检查（不再调模型），不记日志。"""
        if not bubbles:
            return []
        text = " ".join(str(b) for b in bubbles if b and not str(b).startswith("[表情包"))
        if not text.strip():
            return []
        now = time.time()
        found: list[tuple[str, str]] = []
        mode = {c: self.check_mode(gid, c) for c in CHECKS}
        if recheck:
            mode.update({"critic": "off", "self_consistency": "off"})
        claims = self_claims(text) if (mode["source_gate"] != "off" or mode["self_consistency"] != "off") else []
        if claims and mode["source_gate"] != "off":
            p = self.brain.persona
            offline_free = bool(getattr(p, "ai_open", False)) and str(getattr(p, "life", "") or "free") == "free"
            srcs = self.persona_sources() if offline_free else self.own_sources(gid)
            bad = [c for c in claims if not grounded(c, srcs)]
            if bad:
                found.append(("source_gate", f"编了你自己没有的经历（「{bad[0][:24]}」）：你的设定、今天的安排、你以前说过的里都没有这件事。"
                                             "别编自己的生活细节；想接就说你真有的，或者把话题放回对方身上"))
        if claims and mode["self_consistency"] != "off" and not any(n == "source_gate" for n, _ in found):
            why = await self._consistency(gid, claims)
            if why:
                found.append(("self_consistency", f"和你以前说过的对不上（{why}）：照以前说的来，真变了就说“后来改了”"))
        if mode["ask_repeat"] != "off":
            w = self._ask_repeat(gid, text, target_uid, now)
            if w:
                found.append(("ask_repeat", w))
        if mode["template"] != "off":
            w = self._template_hit(gid, str(bubbles[0]), now)
            if w:
                found.append(("template", w))
        if mode["addressee"] != "off":
            w = self._addressee(gid, text, target_uid, target_row if not mentioned else None)
            if w:
                found.append(("addressee", w))
        if mode["time_sense"] != "off":
            w = self._time_sense(text, target_row, now)
            if w:
                found.append(("time_sense", w))
        if mode["tease_risk"] != "off":
            w = self._tease_risk(gid, text, target_uid)
            if w:
                found.append(("tease_risk", w))
        if mode["other_claims"] != "off":
            w = self._other_claims(gid, text, target_uid)
            if w:
                found.append(("other_claims", w))
        if mode["disputed"] != "off":
            w = self._disputed(gid, text, web, now)
            if w:
                found.append(("disputed", w))
        if mode["image_guess"] != "off":
            w = self._image_guess(gid, text, now, image or {})
            if w:
                found.append(("image_guess", w))
        if mode["answered"] != "off" and not mentioned:
            w = self._answered(gid, target_row, now)
            if w:
                found.append(("answered", w))
        if mode["media_claim"] != "off":
            w = self._media_claim(gid, text, now)
            if w:
                found.append(("media_claim", w))
        if mode["catchphrase"] != "off":
            w = self._catchphrase(gid, text)
            if w:
                found.append(("catchphrase", w))
        if mode["video_pending"] != "off":
            w = self._video_pending(gid, text, now)
            if w:
                found.append(("video_pending", w))
        if mode["stale_ask"] != "off":
            w = self._stale_ask(gid, text, mentioned, now)
            if w:
                found.append(("stale_ask", w))
        if mode["pushback"] != "off":
            w = self._pushback(gid, text, now)
            if w:
                found.append(("pushback", w))
        if mode["self_repeat"] != "off":
            w = self._self_repeat(gid, text, now)
            if w:
                found.append(("self_repeat", w))
        # ---- 25.0 承诺对账 / 说查到要有记录、像自己以前、照抄、接错人（chat/promise.py） ----
        if mode["promise"] != "off" and hasattr(self, "_promise_check"):
            w = self._promise_check(gid, text, web, now)
            if w:
                found.append(("promise", w))
        if mode["misattrib"] != "off" and hasattr(self, "_misattrib"):
            w = self._misattrib(gid, text, now)
            if w:
                found.append(("misattrib", w))
        if mode["copy"] != "off" and hasattr(self, "_copied"):
            w = self._copied(gid, text)
            if w:
                found.append(("copy", w))
        if mode["self_like"] != "off" and hasattr(self, "_self_like") and not any(n == "catchphrase" for n, _ in found):
            w = self._self_like(gid, text, now)
            if w:
                found.append(("self_like", w))
        if mode["critic"] != "off":
            w = await self._critic(gid, text, mentioned, [n for n, _ in found], web=web)
            if w:
                found.append(("critic", w))
        if mode["responsive"] != "off":
            w = self._responsive(text, target_row)
            if w:
                found.append(("responsive", w))
        out = []
        if recheck:
            return [why for name, why in found if mode[name] == "on"]
        for name, why in found:
            self.check_log(gid, name, mode[name], text, why, gate_id)
            if mode[name] == "on":
                out.append(why)
            else:
                log.info("群 %s 检查（只记录）%s：%s", gid, name, why)
        return out

    async def _critic(self, gid: int, text: str, mentioned: bool, already: list[str], web: str = "") -> str:
        """23.3 统一的发前自检：一个便宜模型拿着“出过的毛病清单”（FAILURE_MODES，每条都来自日报里确认过的事）
        看这句该不该改。正则检查是初筛：已经命中了就不再问（省钱）；每小时有上限。
        24.0：把她这次查到的资料也给它（以前看不到，把查到的真比分当成编的，让她改成“不写具体比分”）。"""
        if already or not getattr(self, "brain", None) or not hasattr(self, "_allow"):
            return ""
        if not self._allow(f"critic_{gid}", int(self.gk(gid).get("critic_per_hour", 30) or 0)):
            return ""
        names = self.names(gid)
        rows10 = self._rows(gid, 10)
        lab = self._speaker_labels(gid, rows10) if hasattr(self, "_speaker_labels") else {}     # 25.0 带称呼，认得出谁说的
        ctx = "\n".join(f"{'你' if r['role'] == 'assistant' else (lab.get(int(r['user_id'] or 0)) or names.get(str(r['user_id']), r['name']))}"
                        f"：{str(r['content'] or '')[:80]}" for r in rows10)
        brief = re.sub(r"\s+", " ", str(web or "")).strip()[:1500]
        prompt = ("你帮一个在朋友群里聊天的人看她要发的话有没有犯下面这些毛病（只看这些，没犯就是没犯，别挑剔说法）：\n"
                  + "\n".join(f"{i + 1}. {m}" for i, m in enumerate(FAILURE_MODES))
                  + f"\n\n# 最近的群聊（最后几条最新）\n{ctx}\n\n"
                  + (f"# 她这次刚查到的资料（第 5 条只看这里和群聊里有没有；这里写着的数字、比分照说不算毛病）\n{brief}\n\n"
                     if brief else "# 她这次没查资料\n\n")
                  + f"# 她要发的\n{text}\n\n"
                  "只输出 JSON：{\"bad\": true/false, \"which\": 毛病序号（一个数字）, \"why\": \"一句话，引用她的原话\", "
                  "\"fix\": \"怎么改，一句话\"}")
        try:
            out = await asyncio.wait_for(self.brain.llm.chat([{"role": "user", "content": prompt}], role="gate",
                                                             temperature=0.0, max_tokens=160), 8)
            d = json.loads(re.search(r"\{.*\}", out or "", re.S).group(0))
        except Exception:  # noqa: BLE001
            return ""
        if d.get("bad") is not True:
            return ""
        k = critic_index(d.get("which"))
        mode = FAILURE_MODES[k - 1] if 0 < k <= len(FAILURE_MODES) else "清单里的毛病"
        return f"自检：{mode.split('（')[0]}——{str(d.get('why') or '')[:70]}；{str(d.get('fix') or '换个说法或者不说')[:60]}"

    async def _consistency(self, gid: int, claims: list[str]) -> str:
        """问一次便宜模型：这几句和她以前说过的自己的事有没有矛盾。没有自述账本就不问。"""
        try:
            led = [f["text"] for f in self.memory.facts(gid, subject=-1, limit=40)]
        except Exception:  # noqa: BLE001
            led = []
        terms = set().union(*(content_terms(c) for c in claims)) if claims else set()
        rel = [x for x in led if any(t in x for t in terms)][:12]
        if not rel:
            return ""
        prompt = ("下面是一个人以前说过的关于她自己的事，和她现在要说的话。现在要说的话里，有没有和以前说过的事实**直接矛盾**的"
                  "（比如以前说不喝咖啡、现在说天天喝；以前说在北京、现在说在上海）？心情、看法随口变化不算。\n"
                  "只输出 JSON：{\"conflict\": true/false, \"why\": \"矛盾在哪，一句话\"}\n\n"
                  "# 以前说过的\n" + "\n".join(f"- {x}" for x in rel) + "\n\n# 现在要说的\n" + "\n".join(f"- {c}" for c in claims))
        try:
            out = await self.brain.llm.chat([{"role": "user", "content": prompt}], role="gate", temperature=0.0,
                                            max_tokens=120, json_mode=True)
            d = json.loads(re.search(r"\{.*\}", out or "", re.S).group(0))
        except Exception:  # noqa: BLE001
            return ""
        return str(d.get("why") or "前后不一致")[:40] if d.get("conflict") is True else ""
