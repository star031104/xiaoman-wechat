"""群聊的所有可调设置，集中在这一个文件。

- DEFAULTS：每个设置的默认值（config.yaml 的 wechat.group 里写了的覆盖它；小屋里调过的再覆盖）
- KNOBS：其中哪些能在小屋“性格开关”页拖着调（标签、类型、范围、说明）
- DEAD_KNOBS：以前有、现在不用了的开关（启动时从小屋保存的设置里清掉）

加一个新设置：在 DEFAULTS 里加一行（带注释）；想在小屋里能调，再在 KNOBS 里加一行；代码里用 self.g["名字"] 读。
"""
from __future__ import annotations

DEFAULTS = {
    "merge_wait_seconds": 2,       # 等连发说完再判断（同一个人还在发会自动顺延）
    "instant": True,               # 23.0 看到就处理：不装作没看到、不模拟打字（多条之间留 0.5~1 秒）；False 回到按 read_delay / notice_delay / 打字速度等
    "context_messages": 30,        # 带给模型的最近群消息条数
    "talkativeness": 0.3,          # 话痨度 0~1：最近 1 小时里，把最想说的这么多“份额”留给她（chat/gate.py）
    "cold_minutes": 15,            # 群里沉默超过这么久后的第一句：不太想接
    "cooldown_seconds": 60,        # 没被叫到时两次开口的最小间隔
    "max_per_10min": 6,            # 每个群 10 分钟最多说几轮（被叫到时放宽到两倍）
    "max_bubbles": 2,
    "typing_chars_per_sec": 6,     # 打字速度（每秒几个字）
    "impression_every": 40,        # 每个群每攒这么多条群友消息，整理一次印象、群梗，顺便学说话方式
    "profile_every": 15,           # 某个人又说了这么多句，就更新一次她心里这个人的画像
    "route": "chat",               # 用哪条模型路线（llm.routes 里的名字）
    "see_images": True,            # 群里的图片要不要让看图模型看（13.0 默认开：deepseek-flash 看一张不到 1 厘钱；没配 vision 就不看）
    "owner_note": "把你拉进群的老朋友，爱和你互损",   # 她怎么看你（wechat.owner 那个人）
    "learn": True,                 # 跟着群里学说话方式和黑话（memory/learn.py）
    "plan_json": True,             # 先想再说：大模型先写想法、选回哪条、说多长，再写正文
    # 群里安静太久，她自己冒个泡（只在 active_hours 里、免打扰时段外；她上一句就是群里最后一句时不说）
    "idle_hours": 3,               # 群里安静超过这么久才考虑
    "idle_chance": 0.25,           # 满足条件后每小时冒泡的概率
    "idle_gap_hours": 6,           # 两次冒泡至少隔多久
    "active_hours": "10:00-23:30",
    # 像真人（秒数都会乘 pace；pace: 0 就是全部立刻，测试用）
    "pace": 1.0,
    "read_delay": [2, 12],         # 被叫到、她有空：几秒后看到
    "notice_delay": [5, 40],       # 没人叫她、她想插话：刷到群消息要多久
    "busy_delay_minutes": [1, 2],  # 她正忙（上课 / 上班……）时被叫到：过几分钟偷空回
    "repeat_chance": 0.5,          # 群里在复读时她跟着复读的概率
    "react_chance": 0.0,           # 不说话时给那条消息点个表情的概率（微信没有这个功能，保持 0）
    "welcome_chance": 0.7,         # 新人进群欢迎一下
    "night_catch_up": True,        # 免打扰时段被叫到：第二天早上补一句
    "similar_threshold": 0.75,     # 和她最近说过的话这么像就不发
    # —— 2.0 ——
    "talk_curve": {"09:00-11:59": 0.8, "12:00-13:59": 1.1, "14:00-17:59": 0.9, "18:00-23:59": 1.2},  # 按时段调话量
    "quality_check": True,         # 发之前检查 AI 腔、越界说法、复读（chat/quality.py）
    "candidates": 2,               # 让大模型多写几种说法，挑最像群友的（1 = 不多写）
    "typo_rate": 0.03,             # 偶尔打错一个字
    "typo_fix_chance": 0.5,        # 打错后补一句“*正确的字”
    "stickers": True,              # 跟着群友存表情包、偶尔发
    "memory_recall": True,         # 聊天时想起以前群里的事（memory/facts.py）
    "profile_deep": True,          # 3.1 每天一次：这一周说话最多的几个人，拿一周的话仔细回想一遍画像
    "profile_deep_people": 5,      # 每天仔细回想几个人
    "profile_detail": 4,           # 提示词里最多写几个人的详细画像（其他人一句话）
    "wait_max_seconds": 600,       # “等某人开口再说”最多等多久
    "trace": True,                 # 每次问大模型都存一份，可以回放（chat/trace.py、eval/replay.py）
    "explore_rate": 0.02,          # 本该拦下的批次里随机放行这么多（给重新拟合纠偏；0 = 关）
    # —— 2.2 ——
    "lore": True,                  # 群梗词条（聊到关键词才告诉她这个梗的意思，带冷却）
    "mood": True,                  # 持续的心情（被夸、被怼、没人理会影响一阵子）
    "human_latency": True,         # 按这个群群友自己的回复间隔来定她“看到消息”要多久
    "poke_back_chance": 0.0,       # 被拍了，拍回去的概率（微信版做不到，保持 0）
    "update_signature": False,     # 按今天的安排改签名（微信版做不到，保持关）
    # —— 3.0 ——
    "catch_up": True,              # 接上微信后补上她不在时群里说的话（重启、关机、微信没开期间；直接从本地库里读）
    "catch_up_count": 200,         # 每个群往回最多补多少条
    "people_aliases": {},          # 3.0.3 群友的别名（真名、外号）：{"群名片或昵称或微信号": "别名、别名"}；这里新加的启动时补进去，小屋里能改
    "memory_embed_model": "",      # 本机的向量模型（要装 sentence-transformers）；配了 llm.routes.embed（比如硅基流动免费的 BAAI/bge-m3）就走接口，不用这个
    # —— 联网（net/）——
    "read_links": True,            # 群友发的链接 / 分享卡片点开看（标题、简介、正文开头），不用假装看过
    "web_search": True,            # 被问到她可能不知道的事（是什么 / 讲了什么 / 最新……）先搜一搜
    "search_per_hour": 12,         # 每小时最多搜几次（所有群加起来）
    "agent": False,                # 19.0 回话前先想一想：要不要上网查（会多轮搜、读网页、交简报）、要不要设提醒（工具调用；要 DeepSeek 官方接口）
    "agent_per_day": 60,           # 每个群每天最多查几次资料（超过了退回只搜一次）
    "agent_quick_rounds": 2,       # 快查最多几轮
    "agent_deep_rounds": 4,        # 深查最多几轮
    "agent_quick_seconds": 15,     # 快查最多多少秒（到了就交卷）
    "agent_deep_seconds": 40,      # 深查最多多少秒
    "agent_deep_thinking": True,   # 深查打开思考模式（更准、慢一倍）
    "agent_filler": True,          # 被问到、要深查时先说一句“等我看看”（每个群 10 分钟最多一次）
    "agent_filler_after": 10,      # 23.3 查了这么多秒还没查完才说“等我看看”（查得快就直接给答案，别连着发）
    "remember_web": True,          # 搜到 / 读到的要点记下来，以后聊到关键词想起来，常用到的变成她的兴趣
    "weekly_reflect": True,        # 每周一回想这一周，写几句“这一周的我”放进提示词（人物跟着经历慢慢变）
    # —— 4.2 人物慢慢长（chat/grow.py）——
    "monthly_self": True,          # 每月整理一次“我是怎样的人”（把每周的回想沉淀成稳定的性格和口味）
    "plans": True,                 # 记住群里约好的事：到点提醒大家、事后问结果
    "plans_per_day": 3,            # 每个群每天最多提醒 / 问几次
    "fix_knowledge": True,         # 有人说“不对 / 其实是……”时，对一下她记的知识，对的就改
    "share_per_day": 1,            # 每个群每天最多主动分享几次刷到的东西（0 = 关）
    "share_min_score": 13,         # 24.0 分享评分卡（新鲜/相关/有意思/可信/能聊 各 0~4）到这个分才分享
    "share_chance": 0.7,           # 一天里会分享的概率（24.0：每天随机定一个时刻，到点再看群里静不静）
    "share_fresh_days": 3,         # 13.2 只分享这么多天以内发生的事（看不出日期的不分享）
    "photo_share_chance": 0.3,     # 分享时是一张自己日常照片的概率（要配了画图接口 routes.image）
    "photo_per_day": 1,            # 每天最多发几张照片（所有群加起来）
    "watch_videos": True,          # 4.4 群友发的 B 站视频点开“看”：字幕、弹幕、热评整理成要点（每小时最多 10 个）
    "body_clock": True,            # 31.0 困不困按“醒了多久 + 昨晚少睡多少”算（她真在群里说话的时间），不再只看钟点
    "wake_time": "08:00",          # 31.0 平时几点起（今天更早开口就按开口时间）
    "bed_time": "00:30",           # 31.0 平时几点睡（昨晚更晚还在说话就按最后一句）
    "reach_gate": True,            # 31.0 她主动说的话有没有人接 → 权重 0.2~1，乘进插话 / 冒泡 / 分享；连着没人接拉长冒泡间隔
    "video_grid": True,            # 31.0 看视频画面用一张宫格图（按切点分格、挑最清楚的帧、烧编号）；关掉 = 回到均匀截 8 张
    "video_frames": 8,             # 15.0 看 B 站视频时截几张画面给看图模型看（0 = 不看画面；要装 ffmpeg、配看图模型）
    "weather": True,               # 4.4 记住群友在哪个城市（出差、旅游、老家），提示词里带上那边的天气
    "weather_care": True,          # 4.4 他在外地、那边要下大雨 / 太热太冷：提醒一句（每个群每天最多一次）
    "weekly_eval": True,           # 4.3 每周一拿真实聊天评一次分；人设改过就和上一版比一比（chat/../eval/weekly.py）
    "celebrate": True,             # 4.3 群友生日（小屋“她的日子”里填）、节日：当天在群里说一声
    # —— 5.0 真实群聊里测出来的 ——
    "call_names": True,            # 叫人用称呼；关掉 = 这个群不叫任何人的名字 / 外号，直接说、要回谁就引用他那条
    "call_names_auto": True,       # 没专门设 call_names 的群：群友之间很少叫名字（最近 300 句里不到 4%），她也不叫
    "group_note": "",              # 关于这个群你想特意交代她的话（每个群单独写，比如“大家互称 X 总”“别提考研”）
    "guess_to_her": True,          # 没 @ 她、但她刚说完话，同一个人接着说“你……”/ 接她的词：当成在跟她说
    "merge_bubbles": True,         # 两条都很短、或者最近老是分两条发：合成一条发（大多数时候一条）
    "stale_hours": 3,              # 她自己的口头禅、招牌梗这么多小时内不重复
    "pending_reply_minutes": 20,   # 重启前有人 @ 她还没回：重启后这么多分钟以内的补回
    # —— 13.0 开口更像人 ——
    "gate_judge": True,            # 本地门控拿不准（差一点到阈值）时，问一下便宜模型“说 / 等 / 不”（只回一个字）
    "gate_judge_band": 0.25,       # 离阈值这么近算“拿不准”
    "gate_judge_per_hour": 20,     # 每个群每小时最多问几次
    "unprompted_per_day": 40,      # 每个群每天主动开口（插话、冒泡、分享，不算被叫到）最多几次；0 = 不限
    "rethink_max": 0,              # 想好要说的话、还没发出去时群里又来了新消息：重新想（最多几次）
    "rethink_min_new": 2,          # 新来了这么多条（或者有人叫她）才重新想
    "hear_voice": True,            # 群友发的语音转成文字（带情绪、笑声）再给她看（QQ；默认用硅基流动免费的 SenseVoice）
    "voice_reply": True,           # 有人让她“发语音 / 说句话听听”时发一条语音（要配 net.tts，QQ 才能发）
    "plans_cheer": True,           # 群友自己的事（考试、面试、比赛）开始前几个小时给他打个气（每件事一次）
    "birthday_card": True,         # 群友生日：祝完再发一张生日卡（要配画图接口；字是程序叠上去的）
    "see_stickers": True,          # 13.1 群友发的表情包让看图模型看一眼是什么意思（同一张只看一次，每群每小时最多 30 张）
    "unprompted_hours": "",        # 13.1 主动开口（插话、分享，不算被叫到、冒泡另有 active_hours）只在这个时段里；空 = 不限
    "signatures": True,            # 14.0 每轮按情境挑 1~3 条人格签名（if-then）放在提示词最后（人设里写了 traits 才有）
    "signature_k": 3,              # 最多挑几条
    "persona_examples": True,      # 14.0 按关键词挑 1~2 段人设里的对话示例放进提示词
    "example_k": 2,
    "hold_thoughts": True,         # 15.0 想说但时机不对的话先留着，聊到相关的或群冷了再拿出来（最多 5 句，6 小时过期）
    "rel_experience": True,        # 16.0 每晚给常聊的几个人写“和他相处的经验”（看她的话有没有人接）
    "absent_hours": 2,             # 16.0 隔了这么多小时回来算“刚回来”：告诉她他不在时群里聊了什么
    "state_judge": True,           # 16.0 群友今天的状态：关键词命中后让便宜模型判断是真的还是玩笑
    # —— 19.1 更像人（chat/humane.py）——
    "bids": True,                  # 别人接她的话（她点过的人随后第一句、接了她说过的词）：当成在跟她说，接一句
    "bid_hours": 3,                # 她说完多少小时内算“在接她的话”
    "open_question_minutes": 10,   # 21.0 她问了大家一句（中午吃啥），这么多分钟内有人说的第一句算在回答她
    "no_repeat_today": True,       # 今天说过的意思、梗、安排里的事不再主动说第二遍（提示词 + 发之前查）
    "no_monologue": True,          # 她上一句还没人接：不再主动追一句（冒泡、分享、天气这些）
    "lonely_hours": "01:00-07:00", # 这段时间群里 20 分钟没人说话：不主动开口（空着 = 不管）
    "pitch_queue": True,           # 21.0 她觉得“再等等”的话先放着，过一会儿 / 群里停下来再判一次
    "pitch_min_seconds": 60,       # 至少等这么久
    "pitch_max_seconds": 240,      # 最多等这么久，过了就丢
    "pitch_quiet_seconds": 40,     # 群里这么久没人说话算“停下来了”
    "pitch_max_new": 8,            # 等的时候群里又说了这么多句：话题已经走了，丢掉
    "borderline_judge": False,     # 21.0 门控刚过阈值一点点、没人叫她：再问一下便宜模型
    "borderline_band": 0.1,        # 离阈值多近算“擦边过”
    "person_card": True,           # 21.0 在聊的人合成一张人卡（≤120 字），代替“今天状态 / 刚回来 / 相处经验 / 相处说明书”四段
    "listener_mode": "off",        # 27.0 默认关（影子模式只记录、每次多一次调用；标注不够它永远接管不了）。       # 21.1 听者：auto 先只记、和你的标注一致率够了自动接管；on 直接管；shadow 只记；off 不用
    "listener_agree": 0.7,         # auto 接管要的一致率
    "listener_min_each": 15,
    "listener_margin": 0.08,       # 或者：听者比她自己现在的做法和你一致率高这么多（且 ≥ 0.6）也接管       # auto 接管前“该接”“时机不对”各要攒这么多条你的标注
    "listener_panel": True,        # 配了 llm.routes.listener2 时两个模型一起判，意见不一就等等
    "move_cooldown": True,         # 21.1 同一种接法半小时里用过 2 次就提醒她换一种
    "move_cooldown_minutes": 30,
    "length_match": True,          # 21.1 长度靠拢群友：告诉她群里一条一般几个字，主动说太长就换短的
    "length_max_ratio": 3,         # 主动说时一条超过群友中位长度的几倍算太长
    "talk_control": True,          # 21.1 群里有人冲她说“少说点 / 多说点”：话痨度 ×0.5 / ×1.5
    "talk_control_hours": 3,       # 管多久（群主说的管到明早）
    "share_target": 0.8,           # 21.1 她的消息份额超过“平均份额 × 这个”就收紧（0 = 不管）
    "mention_read_cap": 20,        # 21.1 被叫到“看到消息”最多拖几秒（在忙除外）
    "offline_note_minutes": 30,    # 21.1 被叫到隔了这么久才回：带一句“刚掉线了 / 才看到”
    "agent_mention_quick_cap": 12, # 21.1 被叫到时快查最多几秒（到点交卷，没覆盖的写进“没查到”）
    "agent_mention_deep_cap": 25,  # 被叫到时深查最多几秒
    "agent_mention_judge_cap": 40, # 25.1 被叫到、要她下判断 / 推荐买什么时最多几秒（要拿原始数据，25 秒常常拿不到；10 秒时先回一句）
    "agent_source_hints": True,    # 25.1 给研究员“常去的数据站”（按问题里的领域挑几行；net.source_hints 可以加）
    "agent_stop_on_covered": True, # 21.1 研究员写的“要证实”的都有资料了就交卷（HALT），不等到时间到
    "agent_verify_cites": True,    # 21.1 简报里的来源必须在这次搜到的结果里出现过，对不上的删掉
    "agent_filler_gen": True,      # 21.1 要查一会儿时先用她自己的口气现写一句短回（不是固定那几句）
    "favors": True,                # 21.1 每晚记人情账（谁帮过她、她答应过谁），人卡里提一句
    "anchor_volume": 20,           # 21.1 她今天在这个群说了这么多句以后，每次在提示词最后提一遍人格核心（0 = 不管）
    "rules_off": [],               # 21.1 小屋“规则”里关掉的质检规则（按名字）
    "hard_rules": True,            # 21.0 四条硬底线集中检查：别碰的话题、编线下、说幕后、对方难受时损他
    "person_spec": True,           # 每周给常聊的人写“相处说明书”（他说话的潜台词、能损什么、别碰什么）
    "gag_days": 3,                 # 20.0 她自己的梗这几天里反复用过：先歇一歇（0 = 只看最近几小时）
    "label_nb": True,              # 16.0 用你在小屋判过的“该不该说”训练小判断器（两类各 30 条以上才训练）
    "label_nb_min_acc": 0.75,      # 小判断器留出准确率到这个数才用
    "weekly_probe": True,          # 16.0 每周一定向测试：每条签名、每个“不懂 / 没兴趣”编一段假聊天让她回、打分
    "pester_calls": 4,             # 13.2 同一个人 15 分钟里叫她这么多次：当成在逗她，回得短、可以不回（0 = 不管）
    "stale_batch_minutes": 30,     # 13.1 这批消息里最新的一条都是这么多分钟以前的了，没被叫到就不插话（比如补进来的旧消息）
    "mask_pii": True,              # 群友的话写进数据库前把手机号、身份证号、银行卡号、验证码、邮箱换成占位
    "keep_days": 0,                # 原始群消息留多少天（0 = 一直留；整理出的事实和画像不受影响）
    # 20.0 / 20.1 / 20.2
    "typing_abort": True,          # 她主动说的那句还在打字、群里又说了好几句：这句不发了
    "drift_anchor": True,          # 问她自己 / 有人难过 / 聊人生这种最容易漂走的时候，在最后再提一遍她是谁
    "number_check": True,          # 查资料后她说的数字要在简报里出现过，对不上就重写
    "ledger": True,                # 查到的硬事实记进账本（带有效期、会被新消息取代）
    "feed": True,                  # “刷到的”：后台按大家的兴趣攒候选、按事件去重、规则打分过线才分享
    "feed_hours": 6,               # 多久刷一次候选
    "feed_topics": 5,              # 每次刷几个话题
    "feed_min": 0.25,              # 候选的分（兴趣 × 新鲜 × 新颖）到这个数才分享
    "rel_tone": True,              # 关系网加情绪色彩（互损 / 互相支持 / 有摩擦），一周两次
    "weekly_qa": True,             # 每周问她 15 个关于自己的问题，看会不会改口
    "weekly_pairs": True,          # 每周测一次：同一句错话带着情绪说，她会不会改口附和
    # ---- 22.0 发前检查（chat/checks.py）：off 关 / shadow 只记录不拦 / on 命中就重写（重写还不过、没被叫到就不说） ----
    "check_ask_repeat": "on",        # 同一件事 24 小时内问过 / 叮嘱过，或者对方说过“别问了”
    "check_addressee": "on",         # 回的是 A、嘴里叫的是 B；或者那句是 A 在问 B
    "image_intent": True,            # 22.4 有人发图：先看懂图 → 再猜他为什么发（带把握）→ 再决定怎么回
    "ask_repeat_hours": 72,          # 同一件事多久内别再问（23.1：24 → 72）
    "ask_closed_days": 7,            # 对方说了“别问了 / 别鞭尸”：这件事多少天别再提
    # ---- 23.1 别刷屏、别说两遍 ----
    "burst_max": 5,                  # 没被叫到时：她最近 5 分钟已经发了这么多条就先不说（接她的话也一样）
    "burst_soft": 3,                 # 到这么多条以后，“接她的话 / 等的人开口了”不再免冷却和门槛
    "hush_minutes": 10,              # 群里有人嫌她（“你为什么要回答两次”“？”连着两次）或她自己说了“闭嘴”：这么多分钟只回叫到她的
    "check_answered": "on",          # 23.1 这条群友消息她已经回过了，没被叫到又去回一次
    "check_catchphrase": "on",       # 23.1 她自己的一个说法今天已经用了两次
    # ---- 23.2 10-23 日报的问题 ----
    "check_video_pending": "on",     # 23.2 群友刚发的 B 站视频她还在看，却空说“这就去看” / 评内容：没被叫到就不说，看完再说
    "check_seen_missing": "on",      # 30.2 群友说“原文在这”但她没收到东西，却说“我看了”
    # ---- 23.3 被纠正先认错、统一发前自检 ----
    "day_lookup": True,              # 24.0 聊天里提到某一天（昨天、周五那次、10-23），把那天群里的事给她
    "human_examples": True,          # 24.0 给她看这个群以前碰到类似的话，群友是怎么接的（学口吻，不照抄）
    "dry_run": False,                # 24.0 演练：什么都不真发，只记进发送账本（测新版本用）
    "send_per_day": 300,             # 24.0 每个群每天最多发多少条（所有路一起算，防止哪里出循环刷屏）
    "agent_prefilter": True,         # 24.0 “想一想”之前先用规则筛（没有问句、时间、数字、比赛、链接这类信号就不调）
    # ---- 25.0 承诺对账、像自己以前、照抄、接错人、聊天记录分块滑窗（chat/promise.py） ----
    "promise_ledger": True,          # 她说“我去看看 / 我翻翻 / 晚点告诉你”时记一笔，到点没给结果就补（再查一次，查不到就认）
    "promise_minutes": 8,            # 答应了多少分钟还没给就补
    "promise_expire_minutes": 90,    # 过了这么久就不补了（话题早过去了）
    "check_promise": "on",           # 说“核好了 / 查到了”却没有查资料记录；答应过还没给又来一句“我再翻翻”
    "check_dodge": "on",             # 25.1 查到数据、给了判断，她却说“不敢点 / 没人评过 / 是我表达含糊”
    "check_life_done": "on",         # 31.0 把安排里的打算 / 还没到点的事说成做过了（“想去吃某家店”→“我去吃了”）
    "check_copy": "on",              # 照抄给她参考的原话（真人接法、想起来的事）：连续 10 字以上，或 6 字以上占原句八成
    "context_chunk": 15,             # 聊天记录每满这么多条才往后挪一次（实际带 30~44 条）：相邻几轮的开头不变，缓存能命中；0 = 每条都挪
    "speaker_call": True,            # 聊天记录每一行写“称呼（群名片）”：别把 A 的话安到 B 头上
    "check_stale_ask": "on",         # 23.2 大家在聊 A，她突然追问之前的事 B（“老大那文档发了没”）
    "check_misattrib": "on",         # 25.0 点名“X 你……”，那句关键的话却是别人刚说的（接错人）
    "check_critic": "on",            # 23.3 统一发前自检：便宜模型拿着 checks.FAILURE_MODES 看一遍（正则都没命中时才问）
    "critic_per_hour": 30,           # 23.3 自检每小时最多问几次
    "topic_watch": True,             # 30.0 群里这几天一直在聊、还会有新进展的事：每天 10 点后各查一次最新进展，记进账本
    "topic_watch_max": 2,            # 30.0 每天最多跟进几件
    "serious_keep_full": True,      # 30.1 被叫到 / 查过资料时不按条数挑短的（短的常常是压缩成评语的版本）
    "asked_bypass": True,           # 30.1 没 @ 但明显在问她：不受刷屏刹车 / 冷却拦
    "agent_parallel": 2,            # 30.1 想一想一次要查几件就一起查几件（最多这么多）
    "day_start_hour": 5,            # 30.2 她的“今天”从几点算（夜里聊到一两点还是同一天）
    "spot_autotune": True,           # 30.0 按一周的抽查自动调：梗错得多就提高读懂的把握要求；“原本那句更好”多就让自检先只记录
    "cold_sticker": True,            # 30.0 她刚才主动说的几句都没人接、这次又是主动说：有合适的表情包就只发表情包
    "sticker_on_sticker": 0.5,       # 30.0 别人刚发表情包时，她也回表情包的概率（以前是 0：真人常常表情包对表情包）
    "prefer_short": True,            # 30.0 主动说时几个说法差不多好、第一个比群友长一半以上：挑短的
    "drop_bad_bubble": True,         # 29.0 毛病只在其中一条里：去掉那条、剩下的照发，不重写
    "check_parallel": True,          # 29.0 备选说法的检查和第一个同时跑（第一个有毛病时少等一轮）
    "closed_topics": True,           # 29.0 群友说过“别提 X 了 / 压力很大”的事，这几天谁都别提（问候、关心、玩笑都算）
    "weather_bg": True,              # 29.0 天气放后台查，拼提示词时不等
    "meme_weekly": True,             # 29.0 每周一份热梗：搜“本周热梗”和群里在聊的圈子，存进黑话表（先懂后用：群友用了才带给她）
    "spot_check": True,              # 29.0 小屋“学到的”里每天抽几条让你点对错（想说的 vs 发出的、查到的梗）
    "pick_clean": True,              # 27.0 有毛病先在她自己写的别的说法里挑没毛病的；没被叫到就不重写（挑不出就不说）
    "rethink_on_stop": True,         # 27.0 想的时候有人说“别提了 / 压力很大 / 别问了”：重新想一次
    "memory_merge": True,            # 28.0 记忆合成几块（关于这个群 / 关于你自己 / 和这几句有关的），每块有字数上限
    "mem_self_chars": 1800,          # 28.0 “关于你自己”最多多少字
    "mem_group_chars": 1800,         # 28.0 “关于这个群”最多多少字
    "mem_recall_chars": 2200,        # 28.0 “和这几句有关的”最多多少字
    "critic_v2": True,               # 26.0 自检先分事实 / 看法 / 玩笑 / 关心：玩笑、看法不拿去对资料；只指毛病、不写替换句
    "rewrite_minimal": True,         # 26.0 重写只改有毛病的几个字，保留原来的接法、语气、玩笑；改不了就不说
    "drop_not_rewrite": True,        # 26.0 已经回过 / 突然翻旧事：没被叫到就不说（以前会重写成另一句旧话）
    "read_turn": True,               # 26.0 接话前先读懂最新几句：实际意思、语气、里面的梗（和“想一想”一起跑）
    "read_per_hour": 60,             # 26.0 读懂每小时最多几次
    "meme_lookup": True,             # 26.0 拿不准的梗去中文搜“X 是什么梗”，查到的记进黑话表
    "meme_per_day": 30,              # 26.0 每天最多查几个梗
    "meme_sure": 0.7,                # 26.0 读懂时把握低于这个的梗：告诉她别硬接、可以问
    "agent_buy_profile": True,       # 26.0 选购题把问的人画像（预算、用途、在意什么）给研究员
    "video_about_her": True,         # 23.2 群友发的视频讲的是她 / 这个群：看完主动说一两句具体感想（像被叫到一样）
    # ---- 22.0 / 22.1 记忆（memory/autobio.py） ----
    "self_ledger": True,             # 自述账本：她说过的自己的事一条条存下来（只增不删），聊到相关的就放进提示词
    "stances": True,                 # 立场账：她表过的态
    "timeline": True,                # 群时间线：哪天群里发生了什么，最近一周放进提示词
    "drift_rx": True,                # 走样处方：每周把最差的几句写成 3 条具体改法，下一周照着改
    "lore_v2": True,                 # 群梗只由群友的话触发；30 天没有群友用过的梗先不提
    "lore_stale_days": 30,
    "far_name": True,                # 微信不能引用：回的是好几条以前的话时，开头点个名
    "far_name_distance": 3,          # 隔了几条群友消息算“以前的话”
    "metrics_daily": True,           # 每晚算昨天的确定性指标和“个性保留”（eval/daily.py，不花钱）
}

# 渠道不一样、默认值不一样的几个（QQ 能点表情、能戳一戳，补消息少补一点；微信做不到点表情和戳）
CHANNEL_DEFAULTS = {"qq": {"react_chance": 0.08, "poke_back_chance": 0.4, "catch_up_count": 80}, "wechat": {}}


def defaults_for(channel: str | None) -> dict:
    return {**DEFAULTS, **CHANNEL_DEFAULTS.get(str(channel or "wechat"), {})}


DEAD_KNOBS = ("interject_chance", "owner_chance", "interest_boost", "question_boost", "busy_factor",
              "ai_voice_chance", "ai_voice_character",
              "check_source_gate", "check_self_consistency", "check_template", "check_time_sense", "check_tease_risk", "check_responsive", "check_room_read", "check_other_claims", "check_disputed", "check_image_guess", "check_media_claim", "check_pushback", "check_self_repeat", "check_self_like",
              "self_score", "self_score_min", "appraise", "appraise_enforce", "ablate_rate", "self_like_threshold")   # 4.3 语音去掉了

# 小屋里能调的“性格开关”：键 → (标签, 类型, 最小, 最大, 步长, 说明)
KNOBS = {
    "talkativeness": ("话痨度", "float", 0, 1, 0.05, "最近 1 小时里留给她的发言份额；她说多了会自动收紧"),
    "max_per_10min": ("10 分钟最多说几轮", "int", 1, 30, 1, "被叫到时放宽到两倍"),
    "max_bubbles": ("一次最多几条", "int", 1, 5, 1, "一轮回复最多拆成几条消息"),
    "cooldown_seconds": ("两次插话最短间隔（秒）", "int", 0, 1800, 10, "没被叫到时"),
    "pace": ("回复节奏倍数", "float", 0, 3, 0.1, "看到消息、打字的等待时间都乘它；0 = 秒回"),
    "instant": ("看到就回", "bool", None, None, None, "不装作没看到、不模拟打字；关掉才按“回复节奏倍数”等"),
    "typing_chars_per_sec": ("打字速度（字/秒）", "float", 1, 30, 1, "越大越快"),
    "repeat_chance": ("跟着复读的概率", "float", 0, 1, 0.05, "群里有人在复读时"),
    "welcome_chance": ("欢迎新人的概率", "float", 0, 1, 0.05, ""),
    "idle_hours": ("群冷多久会冒泡（小时）", "float", 0.5, 24, 0.5, ""),
    "idle_chance": ("冒泡概率（每小时）", "float", 0, 1, 0.05, ""),
    "idle_gap_hours": ("两次冒泡最短间隔（小时）", "float", 1, 48, 1, ""),
    "active_hours": ("冒泡时段", "str", None, None, None, "格式 10:00-23:30"),
    "profile_every": ("多少句更新一次画像", "int", 5, 200, 5, "某人又说了这么多句就重新整理他的画像"),
    "impression_every": ("多少条整理一次群里的梗", "int", 10, 500, 10, "顺便学说话方式和黑话"),
    "learn": ("跟着群里学说话", "bool", None, None, None, "学到的在“学到的”页里，可以点不用"),
    "plan_json": ("先想再说", "bool", None, None, None, "先写一句想法、选回哪条、说多长，再写正文"),
    "see_images": ("看群里的图片", "bool", None, None, None, "会多花看图模型的调用次数"),
    "night_catch_up": ("半夜被叫早上补回", "bool", None, None, None, "免打扰时段被叫到先不回，醒了补一句"),
    "quality_check": ("发之前检查", "bool", None, None, None, "AI 腔、越界说法、和刚说过的太像的不发"),
    "candidates": ("多写几种说法挑一条", "int", 1, 3, 1, "1 = 只写一种；2~3 会多花一点输出"),
    "typo_rate": ("打错字的概率", "float", 0, 0.2, 0.01, "偶尔打错一个字，有时补一句“*正确的字”"),
    "stickers": ("存表情包、偶尔发", "bool", None, None, None, "群里常用的表情包她会存下来"),
    "memory_recall": ("想起以前的事", "bool", None, None, None, "聊到了会想起群里以前说过的话"),
    "profile_deep": ("每天仔细回想画像", "bool", None, None, None, "每天一次，这一周说话最多的几个人，拿一周的话把画像从头梳理一遍"),
    "profile_detail": ("详细画像最多几个人", "int", 1, 8, 1, "提示词里写详细画像的人数，其他人一句话"),
    "lore": ("群梗词条", "bool", None, None, None, "聊到关键词才告诉她这个梗的意思，提过以后一阵子不再提"),
    "read_turn": ("先读懂再接", "bool", None, None, None, "26.0 接话前先读一遍最新几句：实际意思、语气、里面的梗"),
    "meme_lookup": ("不懂的梗去查", "bool", None, None, None, "26.0 拿不准的梗搜“X 是什么梗”，查到的记进黑话表"),
    "critic_v2": ("自检分清玩笑和事实", "bool", None, None, None, "26.0 玩笑、看法不拿去对资料；只指毛病不写替换句"),
    "rewrite_minimal": ("重写只改有毛病的字", "bool", None, None, None, "26.0 保留原来的接法、语气、玩笑；改不了就不说"),
    "pick_clean": ("有毛病先换个说法", "bool", None, None, None, "27.0 在她写的几个说法里挑没毛病的；没被叫到不重写，挑不出就不说"),
    "meme_sure": ("读懂时梗的把握要求", "float", 0.5, 0.95, 0.05, "低于这个的梗提醒她别硬接；按抽查会自动调"),
    "check_critic": ("统一自检", "str", None, None, None, "on 拦 / shadow 只记录 / off 关；按抽查会自动调"),
    "topic_watch": ("每天跟进群里在聊的事", "bool", None, None, None, "30.0 每天 10 点后查一次最新进展"),
    "mood": ("持续的心情", "bool", None, None, None, "被夸、被怼、没人理会影响她一阵子（半衰期 2 小时）"),
    "human_latency": ("按群友的节奏回", "bool", None, None, None, "从这个群群友自己的回复间隔里抽“看到消息”要多久"),
    "explore_rate": ("随机试一次的比例", "float", 0, 0.1, 0.01, "本该不说的时候偶尔说一次，让以后的自我调整不偏"),
    "read_links": ("点开群友发的链接", "bool", None, None, None, "读标题、简介和正文开头，她能接着聊"),
    "web_search": ("不知道的先搜一搜", "bool", None, None, None, "被问到作品、游戏、新闻、概念这类事时先上网查"),
    "search_per_hour": ("每小时最多搜几次", "int", 0, 60, 1, "所有群加起来"),
    "agent": ("先想一想再回（查资料、设提醒）", "bool", None, None, None, "被问到要查的事会多搜几次、打开网页看，再用她的话说；有人让她到点提醒也会当场记下"),
    "agent_per_day": ("每天最多查几次资料", "int", 0, 200, 5, "每个群；超过了退回只搜一次"),
    "agent_deep_thinking": ("深查时多想想", "bool", None, None, None, "打开思考模式：更准，但要慢一倍（20~40 秒）"),
    "agent_filler": ("要查一会儿先说“等我看看”", "bool", None, None, None, "被问到、要深查时；每个群 10 分钟最多一次"),
    "remember_web": ("记住查到的东西", "bool", None, None, None, "以后聊到关键词会想起来，常用到的会变成她的兴趣"),
    "weekly_reflect": ("每周回想一次", "bool", None, None, None, "每周一写几句“这一周的我”，人物跟着经历慢慢变"),
    "monthly_self": ("每月整理一次自己", "bool", None, None, None, "把每周的回想沉淀成“我是怎样的人”"),
    "plans": ("记住约好的事", "bool", None, None, None, "到点提醒大家，考试面试这类事过后问结果"),
    "plans_per_day": ("每天最多提醒几次", "int", 0, 10, 1, "每个群"),
    "fix_knowledge": ("被纠正了就改", "bool", None, None, None, "有人说“不对 / 其实是……”时对一下她记的知识"),
    "share_per_day": ("每天最多分享几次", "int", 0, 5, 1, "刷到和大家兴趣有关的新消息，随手转进群；0 = 关"),
    "share_chance": ("一天里分享的概率", "float", 0, 1, 0.05, ""),
    "share_fresh_days": ("只分享几天内的新消息", "int", 1, 14, 1, "事情发生在这么多天以前的算旧闻，不分享；看不出是哪天的也不分享"),
    "photo_share_chance": ("分享时发照片的概率", "float", 0, 1, 0.05, "要配了画图接口"),
    "watch_videos": ("点开群友发的 B 站视频", "bool", None, None, None, "读字幕（没有就听一遍）、看几张画面、弹幕、热评，能聊具体哪一段"),
    "video_about_her": ("讲她的视频看完主动说", "bool", None, None, None, "23.2 群友发的视频讲的是她 / 这个群：看完说一两句具体感想"),
    "video_frames": ("看视频时截几张画面", "int", 0, 12, 1, "交给看图模型看画面；0 = 只听不看"),
    "weather": ("记住群友在哪、那边天气", "bool", None, None, None, "出差、旅游、回老家；提示词里带上那边的天气"),
    "weather_care": ("外地要变天提醒一句", "bool", None, None, None, "大雨、暴雪、太热太冷；每个群每天最多一次"),
    "weekly_eval": ("每周评测", "bool", None, None, None, "每周一挑 10 句她说的话找毛病打分；人设改过就和上一版盲比"),
    "call_names": ("叫人名字 / 称呼", "bool", None, None, None, "关掉：这个群不叫任何人的名字，直接说、引用回复（适合大家平时不喊名字的群）"),
    "group_note": ("关于这个群的交代", "str", None, None, None, "写给她看的一两句话，比如“大家互称 X 总”“别提考研”"),
    "guess_to_her": ("没 @ 也接追问", "bool", None, None, None, "她刚说完，同一个人接着说“你……”或接她的话：当成在跟她说"),
    "celebrate": ("生日和节日说一声", "bool", None, None, None, "生日在“她的日子”里填；节日按日历（节气只算冬至）"),
    "gate_judge": ("拿不准时问一下", "bool", None, None, None, "差一点就想插话时，让便宜模型判断“说 / 等 / 不”（只回一个字）"),
    "unprompted_per_day": ("每天主动开口最多几次", "int", 0, 200, 5, "插话、冒泡、分享加起来，不算被叫到；0 = 不限"),
    "signatures": ("按情境想起自己一贯的做法", "bool", None, None, None, "每轮挑 1~3 条人格签名（如果……就……）放在最后提醒她；人设要配了人格文件"),
    "persona_examples": ("按情境带上对话示例", "bool", None, None, None, "每轮按关键词挑 1~2 段人设里的对话示例，让她学口吻"),
    "hold_thoughts": ("想说的话先憋着、等时机", "bool", None, None, None, "时机不对的话先留着，聊到相关的或群冷了再说"),
    "rel_experience": ("记住和每个人怎么相处", "bool", None, None, None, "每晚看她对每个人说的话有没有人接，写几条经验，聊到他时想起来"),
    "absent_hours": ("隔多久回来算“刚回来”（小时）", "float", 0.5, 24, 0.5, "告诉她他不在时群里聊了什么，别默认他看过"),
    "state_judge": ("用模型判断群友今天的状态", "bool", None, None, None, "关键词命中后让便宜模型看他今天的话，分清玩笑和真的"),
    "bids": ("接住别人的搭话", "bool", None, None, None, "她点过的人随后说的第一句、接了她说过的词：当成在跟她说"),
    "bid_hours": ("她说完多久内算在接她的话（小时）", "float", 0.5, 12, 0.5, ""),
    "burst_max": ("5 分钟最多主动说几条", "int", 0, 30, 1, "23.1 没被叫到时；接她的话也算；0 = 不限"),
    "burst_soft": ("说到几条后接话也要过门槛", "int", 0, 30, 1, "23.1 一来一回说多了，“接她的话”不再免冷却"),
    "hush_minutes": ("被嫌话多后安静几分钟", "int", 0, 120, 5, "23.1 有人说“你为什么要回答两次”、连着两个“？”、或她自己说了“闭嘴”"),
    "open_question_minutes": ("问了大家多久内的回答算在回她（分钟）", "int", 1, 60, 1, "她问“中午吃啥”这种没点名的，有人回就接住"),
    "no_repeat_today": ("今天说过的不再说", "bool", None, None, None, "同样的意思、梗、安排里的事，一天只主动说一次"),
    "no_monologue": ("没人理不追发", "bool", None, None, None, "她上一句还没人接时，不主动冒泡、分享"),
    "lonely_hours": ("深夜没人不主动开口", "str", None, None, None, "比如 01:00-07:00；这段时间群里 20 分钟没人说话就不主动说；空着 = 不管"),
    "pitch_queue": ("觉得太早就先等等", "bool", None, None, None, "她觉得“再等等”的话先放着，群里停下来再判一次，过时了就丢"),
    "pitch_min_seconds": ("至少等几秒", "int", 30, 300, 10, ""),
    "pitch_max_seconds": ("最多等几秒", "int", 60, 600, 10, "过了就不说了"),
    "pitch_quiet_seconds": ("多久没人说话算停下来", "int", 10, 180, 5, ""),
    "pitch_max_new": ("等的时候又来几句就丢", "int", 2, 30, 1, "话题已经走了"),
    "borderline_judge": ("擦边过的再问一下", "bool", None, None, None, "门控刚过阈值一点点、没人叫她时，再让便宜模型判一次"),
    "borderline_band": ("离阈值多近算擦边", "float", 0, 0.3, 0.01, ""),
    "person_card": ("人卡", "bool", None, None, None, "在聊的人合成一张 ≤120 字的卡，代替分散的四段"),
    "day_lookup": ("提到哪天就回查那天", "bool", None, None, None, "有人说“昨天”“周五那次”“10-23”时，把那天的群日报和记下的事给她，免得记混日子"),
    "human_examples": ("参考群友的接法", "bool", None, None, None, "以前群里有人说过类似的话，下一个群友是怎么接的，给她学口吻（不照抄）"),
    "dry_run": ("演练（不真发）", "bool", None, None, None, "打开后她照常想、照常“说”，但什么都不发进群，只记进发送账本；测新版本用"),
    "send_per_day": ("每天最多发几条", "int", 0, 2000, 10, "所有路一起算（接话、冒泡、分享、提醒、表情包）；0 = 不限"),
    "share_min_score": ("分享评分卡门槛", "int", 0, 20, 1, "新鲜 / 相关 / 有意思 / 可信 / 能聊 各 0~4 分，加起来到这个数才分享"),
    "agent_prefilter": ("查资料前先用规则筛", "bool", None, None, None, "没有问句、时间、数字、比赛、链接这类信号就不调“想一想”，回得更快"),
    "listener_mode": ("听者", "str", None, None, None, "auto：先只记录，和你的标注一致率够了自动接管；on：直接管；shadow：只记录；off：不用"),
    "listener_agree": ("听者接管要的一致率", "float", 0.5, 0.95, 0.05, ""),
    "listener_margin": ("听者比她自己准多少就接管", "float", 0, 0.5, 0.01, "和你一致率比她现在的做法高这么多（且 ≥ 60%）也接管"),
    "listener_min_each": ("接管前每类要几条标注", "int", 5, 100, 1, "“该接”“时机不对”各要这么多"),
    "listener_panel": ("两个模型一起判", "bool", None, None, None, "配了 routes.listener2 才有用；意见不一就先等等"),
    "move_cooldown": ("接法别老一样", "bool", None, None, None, "半小时里同一种接法用过 2 次就提醒她换一种"),
    "move_cooldown_minutes": ("接法冷却（分钟）", "int", 5, 120, 5, ""),
    "length_match": ("长度靠拢群友", "bool", None, None, None, "告诉她群里一条一般几个字；主动说太长就换短的"),
    "length_max_ratio": ("主动说最多是群友的几倍长", "float", 1.5, 6, 0.5, ""),
    "talk_control": ("群里一句话调话量", "bool", None, None, None, "有人冲她说“少说点 / 多说点”就照做"),
    "talk_control_hours": ("一句话管多久（小时）", "float", 0.5, 24, 0.5, "群主说的管到明早"),
    "share_target": ("她最多占平均份额的多少", "float", 0, 2, 0.1, "3 个活跃的人时平均份额是 25%；0 = 不管"),
    "mention_read_cap": ("被叫到最多拖几秒才看", "int", 0, 120, 5, "在忙的时候除外"),
    "offline_note_minutes": ("隔多久才回要说一声", "int", 5, 240, 5, "带一句“刚掉线了 / 才看到”"),
    "agent_mention_quick_cap": ("被叫到时快查最多几秒", "int", 5, 30, 1, ""),
    "agent_mention_deep_cap": ("被叫到时深查最多几秒", "int", 10, 60, 1, ""),
    "agent_mention_judge_cap": ("被叫到时下判断 / 选购最多几秒", "int", 15, 90, 1, "要拿原始数据自己判断，比普通深查多给一点"),
    "agent_source_hints": ("给研究员常去的数据站", "bool", None, None, None, "判断题、选购题按领域提示去哪个网站拿数据"),
    "agent_stop_on_covered": ("证实了就停", "bool", None, None, None, "研究员写的“要证实”的都有资料了就交卷"),
    "agent_verify_cites": ("核对引用", "bool", None, None, None, "简报里的来源必须是这次真搜到的"),
    "promise_ledger": ("答应了要给结果", "bool", None, None, None, "说了“我去看看 / 晚点告诉你”到点还没给，就再查一次补上；查不到就认一句"),
    "promise_minutes": ("答应了多久没给就补", "int", 3, 60, 1, "分钟"),
    "context_chunk": ("聊天记录分块挪", "int", 0, 30, 5, "每满这么多条才往后挪一次，缓存命中更多；0 = 每条都挪"),
    "speaker_call": ("聊天记录写称呼", "bool", None, None, None, "每一行写“称呼（群名片）”，免得把 A 的话安到 B 头上"),
    "agent_filler_gen": ("先回一句用她自己的话", "bool", None, None, None, "要查一会儿时先短回一句，用她的口气现写"),
    "favors": ("记人情账", "bool", None, None, None, "每晚记谁帮过她、夸过她、求过她、她答应过谁；人卡里提一句，合适时自然地还"),
    "anchor_volume": ("说多了加人格锚点", "int", 0, 100, 5, "她今天在这个群说了这么多句以后，每次在最后提一遍她是谁（聊久了人设会退回默认）"),
    "hard_rules": ("四条硬底线", "bool", None, None, None, "别碰的话题、编线下、说幕后、对方难受时损他：碰了就重写，没被叫到就不说"),
    "person_spec": ("给常聊的人写相处说明书", "bool", None, None, None, "每周一次：他说话的潜台词、能损什么、别碰什么"),
    "label_nb": ("用你的标注训练小判断器", "bool", None, None, None, "“该不该说”两类各 30 条以上才训练；有把握时不用再问大模型"),
    "weekly_probe": ("每周定向测试人格", "bool", None, None, None, "每条签名、每个“不懂 / 没兴趣”编一段假聊天让她回、打分"),
    "pester_calls": ("被同一个人连着叫几次就降温", "int", 0, 20, 1, "15 分钟里叫她这么多次：回得更短，可以不回；正经问事照常回；0 = 不管"),
    "rethink_max": ("新消息来了重新想几次", "int", 0, 3, 1, "想好了还没发、群里又说了几句：重新想一遍再发"),
    "hear_voice": ("听群友的语音", "bool", None, None, None, "转成文字、带上情绪和笑声（免费的 SenseVoice）"),
    "voice_reply": ("让她说话时发语音", "bool", None, None, None, "要配本地语音合成 net.tts；QQ 才能发"),
    "plans_cheer": ("事前打个气", "bool", None, None, None, "考试、面试、比赛开始前几个小时说一句加油"),
    "birthday_card": ("生日发贺卡", "bool", None, None, None, "祝完生日再发一张画的贺卡（要配画图接口）"),
    "typing_abort": ("打字时话题走了就不发", "bool", None, None, None, "她主动说的那句还在打字、群里又说了 3 条以上：这句不发了"),
    "drift_anchor": ("容易出戏时提醒她是谁", "bool", None, None, None, "问她自己、有人难过、聊人生大问题时，在最后再提一遍人设"),
    "number_check": ("转述的数字要对得上", "bool", None, None, None, "查资料后说的数字简报里没有：重写，还不行就删掉那句"),
    "ledger": ("记下查到的事实", "bool", None, None, None, "带有效期，被新消息取代就不再用（学到的 → 她查到的事实）"),
    "feed": ("按大家的兴趣刷新闻", "bool", None, None, None, "后台攒候选、同一件事只分享一次；关掉就回到以前的“想到再搜”"),
    "feed_min": ("分享的门槛", "float", 0, 1, 0.05, "候选的分（兴趣 × 新鲜 × 新颖）到这个数才分享"),
    "rel_tone": ("看谁和谁是互损 / 有摩擦", "bool", None, None, None, "用来判断能不能当着谁开某人的玩笑"),
}


# 只能全局调的（每天一次的整体活、启动时就定下来的东西）；其余的开关每个群都能单独调（2.2）
GLOBAL_ONLY = ("profile_every", "profile_deep", "lore", "mood", "monthly_self")


def group_key(gid: int) -> str:
    """小屋里给某一个群单独调的开关存在哪（只存和全局不一样的那几个）。"""
    return f"web_overrides_g{int(gid)}"


def clean_knob(key: str, val):
    """把网页传来的值按类型和范围整理好；不认识 / 不合法返回 None。"""
    if key not in KNOBS:
        return None
    _, kind, lo, hi, _, _ = KNOBS[key]
    try:
        if kind == "bool":
            return bool(val) if not isinstance(val, str) else val.lower() in ("1", "true", "yes", "on")
        if kind == "str":
            s = str(val or "").strip()[:300 if key == "group_note" else 40]
            return s or None
        v = float(val)
        v = max(float(lo), min(float(hi), v))
        return int(round(v)) if kind == "int" else round(v, 3)
    except (TypeError, ValueError):
        return None


# 27.1 小屋“性格开关”默认只显示这些常用的（其余点“显示全部”才出来；调过的总会显示）。以前 141 个全摊开，找不到要调的
_MAIN = ("talkativeness", "max_per_10min", "cooldown_seconds", "max_bubbles", "unprompted_per_day", "unprompted_hours",
         "active_hours", "idle_chance", "welcome_chance", "share_chance", "group_note", "owner_note", "call_names", "pace",
         "stickers", "see_images", "voice_reply", "hear_voice", "night_catch_up", "memory_recall", "profile_every",
         "agent", "agent_per_day", "search_per_hour", "read_turn", "meme_lookup", "critic_v2", "pick_clean",
         "dry_run", "send_per_day")
MAIN_KNOBS = tuple(k for k in _MAIN if k in KNOBS)
