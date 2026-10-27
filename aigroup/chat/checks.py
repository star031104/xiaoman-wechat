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

CHECKS = ("ask_repeat", "addressee", "answered", "catchphrase", "video_pending", "seen_missing", "promise", "dodge", "copy", "stale_ask",
          "misattrib", "critic")
# 23.2 这些检查命中、又没被叫到：不重写，直接不说（重写以后还是同一个毛病，比如替别人答）
DROP = "〔不说〕"
# 25.1 被要求下判断时往回缩的说法
DODGE_RX = re.compile(r"不敢(点|说|下|判断|乱说)|没法(判断|说谁|点名)|不好(说|判断|评价)|说不(准|好)(谁|是)?|没人(评|复盘|打分)|"
                      r"(评分|复盘)(都)?没(有|找到)|表达(得)?含糊|我只知道|谁(最差|更好)(我)?(也)?(不知道|说不上)")

# 26.0 没说错时的认错（10-27 09:10 / 09:31：“是我表达含糊”“刚才那话我说早了点”）
SELF_BLAME = re.compile(r"是我(表达|说得|没说)(含糊|不清|清楚)|我表达(得)?(含糊|不清)|(那话|这话)我说早了|我说早了|认错[，,。]|是我的锅|我的锅")
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


# ---- 29.0 别提了 ----
TOPIC_CLOSE_RX = re.compile(r"别(?:再)?提(.{1,8}?)了|别(?:再)?问(.{1,8}?)了|(.{1,8}?)(?:的事)?(?:能)?别(?:再)?提了|不想(?:聊|说|提)(.{1,8})")
# 30.0 “压力很大”单独说只是在倾诉，不是让她别提（29.0 误把它当成“别提了”，会把她上一句的词封 7 天）
CLOSE_ONLY_RX = re.compile(r"别(再)?提了|别(再)?问了|别说了|换个话题|能不能不(说|提|问)")
# ---- 问 / 叮嘱 ----
ASK_RX = re.compile(r"[？?]|(了吗|了没|没有|吗|呢|啥时候|几点|多少|咋样|怎么样|赢了没|打完了|结束了)$|"
                    r"(出了没|出来没|怎么样了|咋样了|后来呢|后来怎么|结果呢|赢没赢|发了没|弄完没|搞定没)")
CARE_RX = re.compile(r"记得|别忘|注意|小心|多喝|早点睡|擦药|上药|别熬夜|好好休息|悠着点|慢点|别太累|"
                     r"别熬了|别熬|早点躺|快睡|先睡|去睡|看完(就|这局)?睡")    # 26.0 10-26 凌晨一个多小时催睡 4 次
CLOSE_RX = re.compile(r"别问了|别再问|能别|别鞭尸|鞭尸|不想说|不想提|别提了|别(再)?提.{1,6}了|压力(很|好|太)?大|又问|问了好几遍|问过了|天天问|每天都要问|一天问|烦不烦|够了|"
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
                        r"人家(叫|是)|写的是|不对吧?|错了|"
                        r"难道.{0,12}(也|还|都).{0,8}吗|(都.{0,2}|早就?)(出来了|发了|承认了|道歉了|官宣了|公布了)(?![吗没么嘛])")   # 30.1 “难道这些也别当真吗”“声明都出来了”
# 30.2 “原文在这 / 看这个 / 图在这 / 你看下面”：指着一个东西让她看
POINTER_RX = re.compile(r"(原文|原图|图|截图|链接|视频|证据|声明|这个)(就)?在这(里|儿)?|^\s*(你)?(看|瞅)(这个|这张|这条|下面|上面)|^\s*喏")
SEEN_RX = re.compile(r"(原文|原图|图|截图|链接|视频|声明|你发的)?我?(看了|看过|看完|瞅了|读了)|^\s*看了")
HAS_MEDIA_RX = re.compile(r"\[(图片|视频|链接|分享|文件|表情包|卡片|小程序|聊天记录)|https?://")
# 23.3 她自嘲的那几个点（眼睛 / 脑子 / 记性）：2 小时内同一个别再拿出来
SELF_MOCK = re.compile(r"眼睛|眼神|眼瞎|脑子|记性|反应慢|手残|嘴快")
# 23.3 统一发前自检的“毛病清单”：每条都来自日报里确认过的事（加新的就往这里加一条，并在 tests/group/incidents.yaml 加一个例子）
FAILURE_MODES = [
    "替别人回答别人问别人的问题（“A，这个能……吗”不是问她）",
    "没看过 / 打不开的视频、文章，却说得像看过（“前面那段可以砍”），或者空说“这就去看”；群友说“原文在这 / 看这个”但聊天里没有图和链接，她却说“我看了”",
    "对图认真下断言、替发图的人说他在干嘛，图说明里没有（价目表说成“在看场地”），群友会当真的那种；图里的字像错别字时当真拿来开玩笑（一听就是玩笑、夸张、反问的不算，比如“截到灵魂了”）",
    "群友拿出事实指出她说错了、她也确实错了，还顶嘴、找补、开玩笑带过（被逗、被曲解、被故意抬杠时笑着说清楚不算，那时也不用认错）",
    "当成事实说了查到的资料和群聊里都没有的数字、比分、时长、“第一次”这类说法（她自己的看法、倾向、判断不算；资料里“你看了数据以后的判断”照说不算）",
    "同一件事说第二遍（换个说法也算；回答一个新问题时说“不记得 / 不知道 / 我去查”不算），或者回答别人已经被回答过的问题",
    "大家在聊别的时候突然追问旧事，或者追问对方已经说过“发过了 / 别问了”的事",
    "当真地编自己的线下经历，或者编群友的状态、经历（一听就是玩笑、夸张的不算，比如“我这就给你老板发消息”“起这么早就为了骂AL”）",
    "同一个梗 / 自嘲点（小本本、记仇、眼睛不好）一天里反复用",
    "前后矛盾：和她自己前面刚说的对不上",
    "一股 AI 八股味：“看完了。”开头、“这句我认”、“最扎的是”、“不是……而是……”，一句话里堆好几种写作句式",
    "说“核好了 / 查到了”其实没查到（这次的资料里没有），或者答应了“我去看看”又不给结果、再说一遍“我再翻翻”（第一次说“我去查查 / 翻翻”不算，算的是前面已经答应过、这次又来一遍）",
    "接错人：把 A 说的话安到 B 头上（“B 你别拿它当正常体温”，说正常体温的是 A）",
    "像自己以前说过的话：同一个意思、同一种自怜 / 自嘲换个说法又来（“就我一个打工的”“就我光秃秃一个”），或者照抄别人的原话",
    "被要求下判断（锐评、谁最差、买哪个），资料里有数据和判断却缩回去（“没人评过不敢点”“我只知道…”“是我表达含糊”）",
    "没说错却认错、道歉（“是我表达含糊”“我说早了点”“认错”）：被追问、被逗的时候",
    "资料里有具体事实，她却只给空评语、结论（“形式合格实质回避”“不算诚意”“问题很大”），不说让她这么判断的那件具体的事（谁、什么时候、做了什么、说了哪句）",
    "把资料里已经证实的事（当事方 / 官方已经道歉、处罚、表态）说成“传闻 / 先别当真 / 没实锤”，或者凭日期、格式自己猜截图真假（资料里有“没发过 / 拒绝置评”才算有依据）",
    "拿资料里没证实的说法（标了大概 / 不太确定 / 只有一家这么说，或者资料里根本没有）去纠正别人，或者在别人表态时说教（“骂归骂”“别把两件事混一起”“理性看待”）",
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
    # 30.2 10-27 晚 4 分钟里三次“先纠一下 / 得纠一下 / 先纠个时间”
    "纠一下": re.compile(r"(先|得|要|我)?纠(一下|个|正一下)|先更正|更正一下"),
    # 30.2 回放里连着两次“大半夜的你先睡 / 别再刷了”：关心睡觉一天一次就够
    "催睡": re.compile(r"(快|赶紧|先|早点|你先|该|去)睡(吧|觉|了)?(?!醒)|别熬(夜)?了?|别再?刷了|大半夜的"),
    "自怜": re.compile(r"就我(一个|光秃秃|没有|最惨|啥都没)|光秃秃|我(一个|就是个|只是个)小AI|失宠|没人疼|没人理我|就我是个?打工的"),
}

JOKE_OK = {1, 4, 6, 7, 9, 13, 14}      # 26.0 玩笑只查这几条（替人答、重复、翻旧事、同一个梗、接错人、像以前）


def critic_verdict(d: dict, v2: bool = True) -> str:
    """26.0 自检结果 → 交给重写的一句话。v2：玩笑 / 看法只认几条；不带“改成……”（重写会照抄）。"""
    if not isinstance(d, dict) or d.get("bad") is not True:
        return ""
    k = critic_index(d.get("which"))
    mode = FAILURE_MODES[k - 1] if 0 < k <= len(FAILURE_MODES) else "清单里的毛病"
    if not v2:
        return f"自检：{mode.split('（')[0]}——{str(d.get('why') or '')[:70]}；{str(d.get('fix') or '换个说法或者不说')[:60]}"
    kind = str(d.get("kind") or "")
    if k == 13:                    # 27.1 接错人交给规则（misattrib）：自检分不清谁说的哪句，回放里一对一错
        return ""
    if "玩笑" in kind and k not in JOKE_OK:
        return ""
    if "看法" in kind and k in (3, 8):
        return ""
    span = str(d.get("span") or "").strip()[:24]
    return (f"自检：{mode.split('（')[0]}——{str(d.get('why') or '')[:70]}"
            + (f"（有问题的是「{span}」）" if span else ""))


def critic_prompt(ctx: str, text: str, brief: str = "", read: str = "", v2: bool = True, mine: str = "") -> str:
    """26.0 统一自检的提示词（线上和 release_gate --llm 用同一份）。"""
    if v2:
        # 26.0 先分清每句是什么话：玩笑、夸张、反问不拿去对资料；只指出毛病，不写替换句（以前重写照抄“改成……”，发出去全是检查员的口吻）
        prompt = ("你帮一个在朋友群里聊天的人看她要发的话有没有犯下面这些毛病。只看这些；没犯就是没犯，别挑剔说法，别嫌不够稳妥。\n"
                  + "\n".join(f"{i + 1}. {m}" for i, m in enumerate(FAILURE_MODES))
                  + "\n\n先判断她这句是哪种话：事实（会被当真的说法）/ 看法（她的判断、倾向、锐评）/ 玩笑（夸张、反问、调侃、自嘲、损人）/ 关心。\n"
                  "- 玩笑：只看第 1、4、6、7、9、13、14 条和会不会伤人（被纠正时拿玩笑带过算第 4 条）；夸张、反话本来就不是真的，不算编、不算断言\n"
                  "- 看法：只看里面当成事实说的数字有没有依据，和第 17、18、19 条（只有评语没有撑着的事实 / 把证实的事说成传闻 / 拿没证实的去纠正人、说教）；看法本身不算毛病，更不要让她“别下结论”\n"
                  "- 事实：照清单全看\n"
                  "例子（照这个尺度）：\n"
                  "- 群友发白图，她说“白得这么彻底，是截图截到灵魂了” → 玩笑，没毛病\n"
                  "- 群友让她跟老板请假，她说“行，我这就给老板发条消息” → 玩笑，没毛病\n"
                  "- 群友问“所以你觉得是 hope 最差吗”，她说“我没说他最差啊，被针对不等于打得烂” → 看法，没毛病（他是在追问，不是纠正）\n"
                  "- 群友说“al不是打了33分钟吗”，她回“认字，广告太抢戏了” → 第 4 条（真错了还找补）\n"
                  "- 群友说“al不是打了33分钟吗”，她回“啊我记错了，是 33 分钟” → 没毛病（认错改口不算说第二遍）\n"
                  "- 群友问别人“老王这个能开发票吗”，她抢着答 → 第 1 条\n"
                  "- 她前面说过“今晚早点躺”，这句又说“今晚就别熬了” → 第 6 条（关心、叮嘱换个说法也算说第二遍）\n"
                  "- 她前面说过“我小本本记岔了”，群友又问了一件别的事，她说“我不记得了，你问老张” → 没毛病（回答新问题时说不记得不算说第二遍）\n"
                  "- 群友说自己发烧 37.3，她说“跑完步 37.3 那叫状态好” → 第 13 条（把话理解错、安错了）\n"
                  "- 资料里没有，她说“T1现在1-1，输DK那局是领先被翻的” → 第 5 条（当事实说的比分）\n\n"
                  + f"# 最近的群聊（最后几条最新）\n{ctx}\n\n"
                  + (f"# 最近几句群友话的意思（读过的）\n{read}\n\n" if read else "")
                  + (f"# 她今天早些时候说过的（看第 6、9、14 条：同一个意思、同一个梗、同一个句架子）\n{mine}\n\n" if mine else "")
                  + (f"# 她这次刚查到的资料（第 5 条只看这里和群聊里有没有；这里写着的数字、比分、判断照说不算毛病）\n{brief}\n\n"
                     if brief else "# 她这次没查资料\n\n")
                  + f"# 她要发的\n{text}\n\n"
                  "只输出 JSON：{\"kind\": \"事实/看法/玩笑/关心\", \"bad\": true/false, \"which\": 毛病序号（一个数字）, "
                  "\"why\": \"一句话说毛病在哪\", \"span\": \"有毛病的那几个字（她的原话）\"}")
    else:
        prompt = ("你帮一个在朋友群里聊天的人看她要发的话有没有犯下面这些毛病（只看这些，没犯就是没犯，别挑剔说法）：\n"
                  + "\n".join(f"{i + 1}. {m}" for i, m in enumerate(FAILURE_MODES))
                  + f"\n\n# 最近的群聊（最后几条最新）\n{ctx}\n\n"
                  + (f"# 她这次刚查到的资料（第 5 条只看这里和群聊里有没有；这里写着的数字、比分照说不算毛病）\n{brief}\n\n"
                     if brief else "# 她这次没查资料\n\n")
                  + f"# 她要发的\n{text}\n\n"
                  "只输出 JSON：{\"bad\": true/false, \"which\": 毛病序号（一个数字）, \"why\": \"一句话，引用她的原话\", "
                  "\"fix\": \"怎么改，一句话\"}")
    return prompt


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


class ChecksMixin:
    def _checks_init(self):
        self.db.conn.executescript(SCHEMA)
        self.db.conn.commit()

    def check_mode(self, gid: int, name: str) -> str:
        v = str(self.gk(gid).get(f"check_{name}", "off") or "off").lower()
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

    def closed_topics(self, gid: int, now: float | None = None) -> list[dict]:
        """29.0 群友说过“别提 X 了 / 别问了 / 压力很大”的事：这几天谁都别提（不只是她问过的）。
        10-27 11:39 群友说“别提面试了好吗压力很大的”，第二天的主动问候也不该再说面试。每分钟算一次。"""
        now = now or time.time()
        ck = self.__dict__.setdefault("_closed_cache", {})
        hit = ck.get(gid)
        if hit and now - hit[0] < 60:
            return hit[1]
        days = float(self.gk(gid).get("ask_closed_days", 7) or 7)
        out = []
        rows = self.db.conn.execute("SELECT id, ts, user_id, content FROM qq_group_messages WHERE group_id=? AND role='user' AND ts>? "
                                    "AND (content LIKE '%别%' OR content LIKE '%不想%' OR content LIKE '%换个话题%' OR content LIKE '%能不能不%') "
                                    "ORDER BY id DESC LIMIT 40", (gid, now - days * 86400)).fetchall()
        for r in rows:
            t = TAG.sub("", str(r["content"] or ""))
            m = TOPIC_CLOSE_RX.search(t)
            if not m and not CLOSE_ONLY_RX.search(t):
                continue
            topic = (next((g for g in m.groups() if g), "") if m else "").strip("的事 ")
            terms = content_terms(topic) if topic else set()
            if not terms:                                # “别提了 / 压力很大”：说的是她刚说的那件事
                her = self.db.conn.execute("SELECT content FROM qq_group_messages WHERE group_id=? AND role='assistant' AND ts<? "
                                           "AND ts>? ORDER BY id DESC LIMIT 1", (gid, r["ts"], r["ts"] - 600)).fetchone()
                terms = set(sorted(content_terms(str(her["content"] or "")), key=lambda w: -len(w))[:3]) if her else set()
                topic = str(her["content"] or "")[:16] if her else ""        # 30.0 只取她那句里最多 3 个词，别一封一大片
            if terms:
                out.append({"ts": float(r["ts"]), "uid": r["user_id"], "topic": topic, "terms": terms})
        ck[gid] = (now, out)
        return out

    def _closed_hit(self, gid: int, text: str, now: float) -> str:
        terms = content_terms(text)
        for c in self.closed_topics(gid, now):
            common = terms & c["terms"]
            if common and (len(common) >= 2 or len(c["terms"]) <= 3):
                who = self.names(gid).get(str(c["uid"]), "群友")
                return f"{who}说过别再提「{c['topic'][:12]}」（{int((now - c['ts']) / 3600)} 小时前）：这几天别再提这件事，换个话题或者不说"
        return ""

    def _ask_repeat(self, gid: int, text: str, target_uid, now: float) -> str:
        w = self._closed_hit(gid, text, now) if self.gk(gid).get("closed_topics", True) else ""
        if w:
            return w
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

    # ================= 损人 / 回应性 / 热聊 =================
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
            return (f"{DROP if self.gk(gid).get('drop_not_rewrite', True) else ''}「{str(target_row.get('content') or '')[:20]}」这条你{ago if ago else '刚'}{'分钟前' if ago else ''}已经回过了："
                    "别再回一遍（一样的意思换个说法也算），没别的要说就不说")
        return ""

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

    def _seen_missing(self, gid: int, text: str, now: float) -> str:
        """30.2 群友刚说“原文在这 / 看这个”，可她这边没收到东西，她却说“我看了”（10-27 23:17“原文我看了”）。"""
        if not SEEN_RX.search(TAG.sub("", text)):
            return ""
        rows = self.db.conn.execute("SELECT user_id, content FROM qq_group_messages WHERE group_id=? AND role='user' AND ts>? "
                                    "ORDER BY id DESC LIMIT 6", (gid, now - 180)).fetchall()
        for r in rows:
            body = re.sub(r"^(\s*@\S+\s*)+", "", TAG.sub("", str(r["content"] or "")))
            if POINTER_RX.search(body) and not HAS_MEDIA_RX.search(str(r["content"] or "")):
                if self.pointer_missing(gid, [(None, r["user_id"], str(r["content"] or ""))], now):
                    return ("他说“在这 / 看这个”，可你这边根本没收到（聊天里没有图、链接、文件）：别说“我看了 / 回头看”，"
                            "就回“没刷出来，你再发一下？”")
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

    def pointer_missing(self, gid: int, texts: list, now: float) -> bool:
        """30.2 群友说“原文在这 / 看这个 / 图在这”，可她这边没收到东西（他最近 2 分钟没有图、链接、文件、分享）。
        10-27 23:16 群友说“@你 原文在这”，图没过来，她答“原文我看了”。"""
        if not texts:
            return False
        last = texts[-1]
        body = re.sub(r"^(\s*@\S+\s*)+", "", TAG.sub("", str(last[2] if len(last) > 2 else "")))
        if not POINTER_RX.search(body) or HAS_MEDIA_RX.search(str(last[2])):
            return False
        rows = self.db.conn.execute("SELECT content FROM qq_group_messages WHERE group_id=? AND user_id=? AND role='user' AND ts>? "
                                    "ORDER BY id DESC LIMIT 6", (gid, last[1], now - 120)).fetchall()
        return not any(HAS_MEDIA_RX.search(str(r["content"] or "")) for r in rows)

    def corrected_now(self, gid: int, texts: list, now: float) -> bool:
        """23.3 这一批里有群友在纠正她刚说的（她 3 分钟内说过话、这句命中 CORRECT_RX、是回她的或没指别人），
        而且这次纠正她还没认过：让她不受刹车认一句。"""
        if not texts:
            return False
        last_her = self.db.conn.execute("SELECT ts, content FROM qq_group_messages WHERE group_id=? AND role='assistant' "
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
            # 30.1 把她的原话加问号甩回来（“别当真？？”）：也是在说她错了
            m = re.fullmatch(r"\s*(?:@\S+\s*)?([^？?！!]{2,12})[？?]{2,}\s*", TAG.sub("", body))
            if m and m.group(1).strip() in str(last_her["content"] or ""):
                return True
        return False

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
                return (f"{DROP if self.gk(gid).get('drop_not_rewrite', True) else ''}大家现在在聊别的，你突然追问之前的事（「{c[:20]}」）：先接现在的话题；"
                        "那件事要是有人说过结果了（“发过了”“早说了”）就别再问")
        return ""

    def _dodge(self, text: str, web: str, gid: int | None = None) -> str:
        """25.1 资料里有“你看了数据以后的判断”，她却在躲（10-27：“谁最差我真不敢点”→ 被追问“是我表达含糊，我只知道 Hope 被针对”）。
        26.0 没查资料也管：被追问、被逗（不是拿事实纠正她）时却认错道歉。"""
        m = re.search(r"你看了数据以后的判断：([^\n]+)", web or "")
        if not m:
            t = TAG.sub("", text)
            a = SELF_BLAME.search(t)
            if a and gid is not None:
                last = next((str(r["content"] or "") for r in reversed(self._rows(gid, 4)) if r["role"] == "user"), "")
                if last and not CORRECT_RX.search(last):
                    return (f"没人说你错，你却说「{a.group(0)}」：他是在追问 / 逗你，不是纠正你。别认错，"
                            "直接回答（是或不是，加一句为什么），或者笑着顶回去")
            return ""
        t = TAG.sub("", text)
        d = DODGE_RX.search(t)
        if not d:
            return ""
        call = re.sub(r"（(大概|不太确定)）$", "", m.group(1)).strip()
        return (f"你查了数据、也有自己的判断（「{call[:40]}」），却说「{d.group(0)}」：直接说你的看法，带一条依据；"
                "把握不大就说“我倾向…”，别缩回去")

    # ================= 汇总 =================
    async def checks22(self, gid: int, bubbles: list[str], *, target_uid=None, target_row: dict | None = None,
                       mentioned: bool = False, gate_id=None, web: str = "", image: dict | None = None,
                       recheck: bool = False, quiet: bool = False) -> list[str]:
        """发前检查。返回要处理的问题（只包含开关是 on 的）；shadow 的只记录。
        27.1 只留模型一次看不全、要查历史或状态的 10 个（回过、问过、承诺、说法族 / 口头禅、叫错人、照抄、视频还在看、躲判断、翻旧事、接错人），
        其余的毛病都在 FAILURE_MODES 里交给统一自检（以前同一个毛病提示词、正则、自检各写一遍，三处打架）。
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
            mode["critic"] = "off"
        rules = [
            ("ask_repeat", lambda: self._ask_repeat(gid, text, target_uid, now)),
            ("addressee", lambda: self._addressee(gid, text, target_uid, target_row if not mentioned else None)),
            ("answered", lambda: "" if mentioned else self._answered(gid, target_row, now)),
            ("catchphrase", lambda: self._catchphrase(gid, text)),
            ("video_pending", lambda: self._video_pending(gid, text, now)),
            ("seen_missing", lambda: self._seen_missing(gid, text, now)),
            ("promise", lambda: self._promise_check(gid, text, web, now) if hasattr(self, "_promise_check") else ""),
            ("dodge", lambda: self._dodge(text, web, gid)),
            ("copy", lambda: self._copied(gid, text) if hasattr(self, "_copied") else ""),
            # 27.1 回放里自检认不出这两类（谁说的哪句、哪件事早就说过），留成规则
            ("stale_ask", lambda: self._stale_ask(gid, text, mentioned, now)),
            ("misattrib", lambda: self._misattrib(gid, text, now) if hasattr(self, "_misattrib") else ""),
        ]
        for name, fn in rules:
            if mode[name] != "off":
                w = fn()
                if w:
                    found.append((name, w))
        if mode["critic"] != "off":
            w = await self._critic(gid, text, mentioned, [n for n, _ in found], web=web)
            if w:
                found.append(("critic", w))
        out = []
        if recheck or quiet:                           # quiet（29.0）：备选说法的检查，不记日志（没选上的说法不算命中）
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
                        f"：{str(r['content'] or '')[:(240 if str(r['content'] or '').startswith('[图片') else 100)]}" for r in rows10)
        brief = re.sub(r"\s+", " ", str(web or "")).strip()[:1500]
        read = str((self.__dict__.get("_read_txt") or {}).get(gid) or "")[:600]
        mine = ""
        if hasattr(self, "today_lines"):                 # 27.1 规则里的“像自己以前 / 自嘲重复 / 老套路”并进自检：给它看她今天说过的
            try:
                shown = {str(r["content"] or "") for r in rows10 if r["role"] == "assistant"}
                mine = "\n".join(f"- {x[:60]}" for x in [x for x in self.today_lines(gid) if x not in shown][-8:])
            except Exception:  # noqa: BLE001
                mine = ""
        prompt = critic_prompt(ctx, text, brief, read, v2=bool(self.gk(gid).get("critic_v2", True)), mine=mine)
        try:
            out = await asyncio.wait_for(self.brain.llm.chat([{"role": "user", "content": prompt}], role="gate",
                                                             temperature=0.0, max_tokens=200), 8)
            d = json.loads(re.search(r"\{.*\}", out or "", re.S).group(0))
        except Exception:  # noqa: BLE001
            return ""
        return critic_verdict(d, v2=bool(self.gk(gid).get("critic_v2", True)))
