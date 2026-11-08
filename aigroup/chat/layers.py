"""35.0 把 GroupChat 的 25 个 Mixin 按“她怎么过一天”分成 7 层（只是分组，方法一个没改）。

以前 GroupChat 一口气继承 23 个 Mixin，新人看不出哪块管什么、改一处会不会碰到别处。现在：

  看到   Senses     收消息、补历史、看图看视频、点开链接、刷到的东西
  时机   Timing     攒一批 → 算该不该说 → 冷场冒泡、等等再说、说多了收着（gate、listener、idle、v35 的攒批）
  说话   Voice      拼提示词 → 问大模型 → 发前检查 → 发出去、记账（prompt、speak、checks、outbox、promise、meme）
  身份   Identity   人设 v2、关系四档、按钮梗、情绪、想念（v33）+ 她自己的经历（autobio）+ 人物慢慢长（grow）
  关系   Social     群友画像、熟悉度、关系网、像人指标（people、social、insight、humane）
  记忆   Recall     整理印象 / 记的事、带出处的摘要、说话风格（digest、episodes、style）
  工具   Tools      联网查资料、研究员、看效果、每周评测（net、agent、feedback、eval）

规则：Mixin 之间不许有同名方法（tests/group/test_v350.py 会查），所以分组的先后不影响行为。
"""
from __future__ import annotations

from aigroup.agent.mixin import AgentMixin
from aigroup.chat.checks import ChecksMixin
from aigroup.chat.feed import FeedMixin
from aigroup.chat.feedback import FeedbackMixin
from aigroup.chat.grow import GrowMixin
from aigroup.chat.humane import HumaneMixin
from aigroup.chat.idle import IdleMixin
from aigroup.chat.ingest import IngestMixin
from aigroup.chat.insight import InsightMixin
from aigroup.chat.listener import ListenerMixin
from aigroup.chat.meme import MemeMixin
from aigroup.chat.outbox import OutboxMixin
from aigroup.chat.prompt import PromptMixin
from aigroup.chat.promise import PromiseMixin
from aigroup.chat.social import SocialMixin
from aigroup.chat.speak import SpeakMixin
from aigroup.chat.v33 import V33Mixin
from aigroup.chat.v35 import V35Mixin
from aigroup.chat.v352 import V352Mixin
from aigroup.chat.v36 import V36Mixin
from aigroup.eval.weekly import EvalMixin
from aigroup.memory.autobio import AutobioMixin
from aigroup.memory.digest import DigestMixin
from aigroup.memory.episodes import EpisodesMixin
from aigroup.memory.people import PeopleMixin
from aigroup.memory.style import StyleMixin
from aigroup.net.knowledge import NetMixin


class Senses(FeedMixin, IngestMixin):
    """看到：收消息（ingest）、刷到的候选（feed）。"""


class Timing(IdleMixin, ListenerMixin, V35Mixin, V352Mixin, V36Mixin):
    """时机：冷场冒泡 / 等等再说（idle）、旁听判断（listener）、攒一批再回和说多了收着（v35）、
    安静期 / 拦过的意思 / 在跟别人说 / 额度按时段摊（v352）。"""


class Voice(SpeakMixin, PromptMixin, ChecksMixin, OutboxMixin, PromiseMixin, MemeMixin):
    """说话：说（speak）、提示词（prompt）、发前检查（checks）、发件箱和叫停（outbox）、答应的事（promise）、梗（meme）。"""


class Identity(V33Mixin, AutobioMixin, GrowMixin):
    """身份：v2 人设和关系四档等（v33）、她自己的经历（autobio）、人物慢慢长（grow）。"""


class Social(PeopleMixin, SocialMixin, InsightMixin, HumaneMixin):
    """关系：群友画像（people）、熟悉度（social）、关系网（insight）、像人指标（humane）。"""


class Recall(DigestMixin, EpisodesMixin, StyleMixin):
    """记忆：整理印象和记的事（digest）、带出处的摘要（episodes）、说话风格（style）。"""


class Tools(NetMixin, AgentMixin, FeedbackMixin, EvalMixin):
    """工具：联网（net）、研究员（agent）、看效果（feedback）、每周评测（eval）。"""


LAYERS = (Senses, Timing, Voice, Identity, Social, Recall, Tools)
