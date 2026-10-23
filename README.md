<div align="center">

# 小满 · xiaoman-wechat

**住在微信群里、像一个真人群友的 AI**

会看气氛接话，也知道什么时候闭嘴；记得每个群友；有自己的性格和日子；还有一间可以看她、调她的“小屋”

[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Platform](https://img.shields.io/badge/Platform-Windows%20%7C%20Linux-555)](#放在服务器上24-小时在线)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

[像人的六件事](#像人的六件事) · [她的小屋](#她的小屋) · [安装](#windows-上装和启动) · [服务器](#放在服务器上24-小时在线) · [机制总览](docs/机制总览.md) · [人格刻画](docs/人格刻画.md) · [注意](#用之前请看)

</div>

---

大多数群聊机器人是“被 @ 了就回一句”的助手。小满想做的是另一件事：**一个像人的群友**。
不只是说话像人，还包括什么时候开口、什么时候闭嘴、怎么对不同的人、被逗了怎么办、记不记得你上周说过的事、会不会慢慢变得更懂这个群。

她用一个单独的微信小号待在你的群里：**读电脑上微信自己的本地数据库收消息，像人一样操作微信窗口发消息**。不 hook、不注入、不改微信的任何文件。

## 像人的六件事

| | 做了什么 | 详细 |
| --- | --- | --- |
| **有自己的人格** | 不用一串形容词，而是用心理学的人格模型写：特质剖面（大五 30 facet）、“如果……就……”的情境签名、价值排序、幽默分寸；每一轮按当下的情境提醒她“你一贯是这样的” | [人格刻画](docs/人格刻画.md) |
| **懂每一个人** | 给每个群友建画像：性格、说话风格、他在什么情况下怎么反应、他在群里图什么、别碰的话题、记得的事；回话前先判断对方这句想要什么 | [人格刻画 · 第 5 节](docs/人格刻画.md#5-她怎么理解每一个人) |
| **懂分寸、会看时机** | 不是每条都接：先估计这时候插话自不自然，拿不准再问一个便宜模型；她想插话时，另一个不带人设的“听者”判断是正好、太早还是不该说；太早就先放着、群里停下来再想；没人接就安静；深夜不主动开口；每天主动开口有预算 | [说话时机](docs/说话时机.md) |
| **真的看图、看 B 站** | 群里的图片和表情包解开来给看图模型看；群友发的 B 站视频读字幕（没有就听一遍）、截几帧画面、看弹幕最热闹的几段和热评 | [机制总览](docs/机制总览.md) |
| **会自己查、不乱说** | 被问到最近的事、数据、新闻，先判断查多深，再让一个不带人设的“研究员”多角度搜、打开网页看数字、核对来源，交一份带来源和把握的简报，她再用自己的话说；查不到就说查不到，说法不一样就说不一样 | [机制总览 ③](docs/机制总览.md) |
| **会慢慢长大** | 记住群里的梗和说法、约好的事到点提醒；每周回想“这一周的我”、每月整理“我是怎样的人”（要有证据）；性格要改得你点头；每周自动评测她有没有走样 | [人格刻画 · 第 6、7 节](docs/人格刻画.md#6-成长与审核) |

### 她的人格是怎么写的

形容词不是人格。“嘴贫、傲娇、心软”这种写法，模型会执行过头（被逗时回个不停、毒舌变刻薄），长对话里还会被稀释。所以人格拆成六层：核心身份、特质剖面（大五 / IPIP-NEO）、情境签名（Mischel & Shoda 的 CAPS）、价值与动机（Schwartz、依恋理论、Martin 幽默风格）、声音、叙事身份（McAdams）。写在人设旁边的一个 YAML 里（示例 [`persona/traits.example.yaml`](persona/traits.example.yaml)）：

```yaml
facets:
  A2: {level: 4, note: 说话直，不懂就说不懂}          # 坦率高
  A4: {level: 2, note: 被怼先呛回一句，但真错了就认}   # 顺从低
  A6: {level: 4, note: 见人真难过立刻软下来}          # 温柔高 → 嘴上不让人、心里软
humor:
  mix: {亲和: 50, 调侃: 30, 自嘲: 20, 攻击: 0}
signatures:
  - when: 同一个人连着逗你、让你复述骂人的话
    flags: [pestered]
    do: 回一次就够，后面越回越短，或者不回
    not: 跟着骂别人；同一个问题追问第二遍
```

程序认出当下的情境（被叫到、被连着逗、没人接、被夸、被损、有人难过、深夜……），挑 1~3 条签名和 1~2 段对话示例放在**提示词最后、离生成最近的地方**，对抗“人设在长对话里漂移”。

### 她怎么理解每一个人

- **进群前先认识大家**：把以前的聊天记录整理成画像草稿，你看过、删掉不想让她知道的再灌给她；真正说上话以前，她不会把听来的私事说出来。
- **每晚更新**：只写有证据的；别人损他的话不当成他的性格；画像有历史版本，能退回。
- **人卡**：在聊的每个人合成一张短卡（多熟、今天状态、刚回来没看到、别碰、能不能调侃、跟他怎么说），放进当轮提示词。
- **分寸**：熟悉度分三档，刚认识的人先客气；每个人的“别碰”在发之前会被拦下；入库前脱敏，可以让她“忘掉某人”。

## 她的小屋

一个本机网页（http://127.0.0.1:8768）：看她此刻在干嘛、心情怎样、她记得什么、她心里每个群友的样子，大部分都能直接改。

- **首屏是她此刻和她的立绘**，颜色跟着真实时间走：早上暖白、傍晚玫瑰、深夜深紫；深浅色跟系统，也能手动切；手机上导航在底部。
- **首页**：今天的群（四个大数字、消息图、她的安排）。**工作台**：群聊记录、群友画像、学到的说法和黑话，以及性格开关、大模型、日志。
- **你来教她**：给“这一刻她该不该说”打标（键盘 Y / N / S）、抽查群友画像、审核她想改的性格、看关系网和相处经验；性格开关拖一下马上生效，每个群可以单独调。
- **立绘和头像**：放在 `home/wechat/data/avatar/`（文件名带 `portrait` 的是立绘、带 `avatar` 的是头像）。配了能带参考图的画图模型时，她发自拍会照着画，长相前后一致。
- 每一页、每个开关的说明：[docs/小屋.md](docs/小屋.md)

## Windows 上装和启动

**要准备的**：一台 Windows 电脑（要一直开着、别锁屏）、**微信 4.x 电脑版**、一个给她用的**微信小号**（拉进你的群）、一个大模型接口（任何 OpenAI 兼容的地址 + Key，比如 DeepSeek）。

**1. 装**（只要做一次）

在 PowerShell 里粘贴这一行（代码下到 `文档\xiaoman-wechat`，GitHub 慢时自动换加速地址）：

```powershell
irm https://raw.githubusercontent.com/star031104/xiaoman-wechat/main/get.ps1 | iex
```

或者在 GitHub 上 **Code → Download ZIP**，解压后双击 **`install.bat`**。它会装 Python 环境和依赖、ffmpeg，然后打开配置向导：找到微信和她的账号、拿数据库密钥、选群、选你自己、加一个大模型接口，再从模板复制一份人设给你。

**她是谁由你来定**：仓库里不带现成的角色。`persona/template.md` 是一个写好的示例（每一项都带注释），向导会把它复制到 `home\wechat\persona\`，你把名字、身份、性格、说话方式、对话示例改成自己的角色，写法见 [docs/PERSONA.md](docs/PERSONA.md)。

**2. 微信这边**：电脑上登录她的小号，在微信里**双击你的群，让它单独开一个窗口**（可以最小化，别关）——她靠这个窗口发消息。

**3. 启动**

| 怎么做 | 效果 |
| --- | --- |
| 双击 **`start.vbs`** | 后台启动，**一个黑窗口都没有** |
| 双击 **`start.bat`** | 同上（第一次会装托盘图标要用的包） |
| `scripts\start_console.bat` | 在窗口里跑、能看实时输出（排查问题用） |

任务栏右下角会出现一个微信绿的 **W**（可能藏在 `^` 里）：双击打开小屋；右键：状态、看日志、体检、换大模型、重启、开机自动启动、退出。程序意外退出 10 秒后自动重开。

**4. 进群前先“认识大家”**（可选，推荐）

双击 `scripts\onboard.bat`：导出群里以前的聊天记录（只读，一般从你自己的大号读）→ 大模型整理成一份草稿 `home\wechat\data\seed_draft.yaml`（群在聊什么、群梗、每个常说话的人的画像）→ 你看一遍、删掉不想让她知道的 → 回车灌给她。

**5. 平时**

- 换 / 加大模型：小屋的“大模型”页，或 `scripts\llm.bat`
- 哪里没配好：`scripts\check.bat`；微信接入专门的体检：`scripts\wxdiag.bat`
- 改人设：直接改 `home\wechat\persona\` 里你的文件（不会进 git），写法见 [docs/PERSONA.md](docs/PERSONA.md)

## 放在服务器上，24 小时在线

Linux 服务器上用 Docker 跑一个 Linux 版微信（网页 VNC 扫码登录），程序读容器里的微信库、用 `docker exec` 发消息。2 核 4G 就够。

```bash
git clone https://github.com/star031104/xiaoman-wechat.git /opt/xiaoman-wechat
cd /opt/xiaoman-wechat
bash install.sh                        # 建 .venv、装依赖、跑配置向导
bash deploy/wechat/wechat_up.sh        # 起 Docker 里的微信，SSH 隧道接 5800 端口扫码
sudo cp deploy/systemd/xiaoman-wechat.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now xiaoman-wechat
journalctl -u xiaoman-wechat -f        # 看日志
```

小屋只开在服务器本机：在自己电脑上双击 `scripts\open_home.bat`（macOS / Linux：`bash scripts/open_home.sh ubuntu@服务器`）用 SSH 隧道打开。详细步骤见 [docs/WECHAT_LINUX.md](docs/WECHAT_LINUX.md)。

## 常用命令

都是 `python run.py <命令>`（Windows 上先 `scripts\env.bat` 进环境）。不写命令 = 启动她；`python run.py help` 看全部。

| 命令 | 做什么 |
| --- | --- |
| （不写） | 启动 |
| `setup` | 配置向导（再跑一次只改你想改的） |
| `check` | 体检：微信、群、人设、大模型 |
| `llm` | 加 / 换 / 测大模型接口 |
| `people` | 她心里每个群友的样子 |
| `onboard` | 进群前的准备：导出聊天记录 → 整理成草稿 → 你看过 → 灌给她（也能分开跑：`wxexport` → `seedgen` → `seed`） |
| `replay` / `calibrate` | 拿真实聊天回放、按扣分制评她；看质检会不会误伤 |
| `wxdiag` · `wxkeys` · `wxtail` · `wxsend` | 微信接入：体检、重新拿密钥、看收到的消息、发一句试试 |
| `forget-person` / `forget-group` | 彻底忘掉一个人 / 一个群 |

她的家默认在 `home/wechat/`（配置、密钥、数据库、日志、你自己的人设，都不进 git）。想放到别处：`python run.py --home D:\bots\xiaoman`，或者设环境变量 `AIGROUP_HOME`。

```
xiaoman-wechat/
├── run.py                       入口：python run.py [命令]
├── aigroup/                     她的全部代码
│   ├── chat/                    群聊主流程：收消息 → 该不该说 → 想 → 说 → 看效果
│   ├── memory/                  群友画像、事实、群梗、学到的说法、表情包
│   ├── agent/ · net/            研究员、读网页、搜索、B 站、天气
│   ├── wechat/                  接微信：读加密的本地库、拿密钥、操作窗口、Linux 版
│   ├── eval/                    回放、评审、每周人格检查
│   └── web/                     小屋（page.html + tokens.css）
├── persona/                     人设模板（一个写好的示例）、人格文件示例
├── config.wechat.example.yaml   全部配置项和说明
├── home/wechat/                 ← 她的家，不进 git
├── deploy/                      服务器：systemd、Docker 里的微信
├── scripts/                     托盘、体检、换大模型、开机自启、隐私扫描……
├── tests/                       python -m pytest（不需要真微信，也不需要 Windows）
└── docs/                        机制、说话时机、人格刻画、小屋、微信接入、开发说明
```

## 用之前请看

- **这是一个个人兴趣项目，不是微信官方的东西。** 用程序操作微信可能违反《微信软件许可及服务协议》，有被限制功能甚至封号的风险。**请只用小号**，别用你自己的主号，后果自负。
- **要让群友知道她是 AI。** 人设模板里的示例角色是一个“大学生”，`ai_open` 默认是 `false`：按角色说话、不主动说自己是 AI。拉她进群前请先告诉大家（群公告里说一声就行），或者在你的人设里写 `ai_open: true`：她被问到就大方承认，也不再编上课、出门这类线下经历。不要用它冒充真人、骗人、刷屏、拉人、发广告。
- **群友的隐私。** 聊天记录、群友画像、数据库密钥都只在你自己的电脑 / 服务器上（`home/` 在 `.gitignore` 里）；发给大模型的只有拼提示词需要的那部分，会经过你选的大模型服务商。入库前会脱敏手机号、证件号、卡号、验证码、邮箱。请先征得群友同意再拉她进群。
- 程序里不内置任何大模型接口和 Key，费用由你自己的接口承担；DeepSeek 一天大概几毛到几块钱，小屋里能看到每天的花费，也能设上限。
- 读微信数据库的思路参考了 [wechatauto-replica](https://github.com/Xpercent/wechatauto-replica)（Apache-2.0），见 [NOTICE](NOTICE)；小屋的设计风格参考了 [Wakuwaku](https://github.com/zhoupengjie/Wakuwaku)（MIT）。

## 更多

- **她现在靠哪些机制运转（一页看完）**：[docs/机制总览.md](docs/机制总览.md) · 说话时机细节：[docs/说话时机.md](docs/说话时机.md)
- **人格刻画**：[docs/人格刻画.md](docs/人格刻画.md) · 写人设：[docs/PERSONA.md](docs/PERSONA.md)
- **小屋每一页、每个开关**：[docs/小屋.md](docs/小屋.md)
- **微信是怎么接的、常见问题**：[docs/GROUP_WECHAT.md](docs/GROUP_WECHAT.md) · 服务器：[docs/WECHAT_LINUX.md](docs/WECHAT_LINUX.md)
- **改代码**：[docs/群聊开发说明.md](docs/群聊开发说明.md) · 跑测试：`python -m pytest`
- 这个仓库是从 [ai-life](https://github.com/star031104/ai-life)（QQ 私聊的有希 + QQ 群 + 微信群）里拆出来的微信群部分，更新记录见 [CHANGELOG.md](CHANGELOG.md)

MIT License
