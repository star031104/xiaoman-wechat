"""小工具：命令行问答、表情、日期。"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta

WEEK = "一二三四五六日"
RANGE_RE = re.compile(r"(\d{1,2})[:：](\d{2})\s*[-~—–到至]+\s*(\d{1,2})[:：](\d{2})")


def ask(prompt: str, default: str = "") -> str:
    try:
        v = input(f"{prompt}{f' [{default}]' if default else ''}：").strip()
    except EOFError:
        v = ""
    return v or default


def yes(prompt: str, default: bool = True) -> bool:
    v = ask(prompt + ("（Y/n）" if default else "（y/N）")).lower()
    return default if not v else v.startswith("y") or v in ("是", "要", "好")


def say(ok: bool | None, text: str):
    print(("✅ " if ok else "❌ " if ok is False else "   ") + text)


EMOTION_EMOJI = {
    "开心": ["😊", "😄", "😁", "🥰", "☺", "😆"],
    "大笑": ["😂", "🤣", "😆"],
    "喜欢": ["🥰", "😍", "❤", "💕", "😘", "💗"],
    "亲亲": ["😘", "💋", "😚"],
    "害羞": ["😳", "☺", "🙈", "🥺"],
    "难过": ["😢", "😭", "😞", "😔", "🥺"],
    "委屈": ["🥺", "😢", "😣", "😞"],
    "生气": ["😠", "😡", "😤", "💢"],
    "哼": ["😤", "😒", "🙄"],
    "惊讶": ["😮", "😲", "😳", "🤯", "😱"],
    "无语": ["😑", "😐", "🙄", "😶"],
    "疑惑": ["🤔", "😕", "❓"],
    "困": ["😴", "🥱", "💤"],
    "晚安": ["😴", "🌙", "💤"],
    "抱抱": ["🤗", "🫂", "🥰"],
    "加油": ["💪", "✊", "👍"],
    "好的": ["👌", "👍", "🙆"],
    "饿": ["🤤", "😋", "🍔"],
    "想你": ["🥺", "💕", "🥰"],
    "得意": ["😎", "😏", "😌"],
    "哭": ["😭", "😢"],
    "期待": ["🤩", "😍", "✨", "😊"],
    "摸摸头": ["🤗", "🥰", "😊"],
    "安慰": ["🤗", "🫂", "🥺"],
    "鼓励": ["💪", "✊", "👍"],
    "坏笑": ["😏", "😈", "😼"],
    "傲娇": ["😤", "😒", "😏"],
    "震惊": ["😱", "😲", "🤯"],
    "得逞": ["😏", "😎", "😈"],
}


def emotion_to_emojis(emotion: str) -> list[str]:
    emotion = emotion.strip()
    if emotion in EMOTION_EMOJI:
        return EMOTION_EMOJI[emotion]
    for k, v in EMOTION_EMOJI.items():
        if k in emotion or emotion in k:
            return v
    # 直接写了 emoji
    if any(ord(ch) > 0x2600 for ch in emotion):
        return [emotion]
    return []


def period_of_day(h: int) -> str:
    if h < 5:
        return "深夜"
    if h < 8:
        return "清晨"
    if h < 11:
        return "上午"
    if h < 13:
        return "中午"
    if h < 18:
        return "下午"
    if h < 23:
        return "晚上"
    return "深夜"


def calendar_hint(now: datetime) -> str:
    """把本周、下周每天的日期列出来，免得模型把“下周二”算错。"""
    monday = now - timedelta(days=now.weekday())
    wk = "一二三四五六日"
    def week(start):
        return " ".join(f"{(start + timedelta(days=i)):%m-%d}({wk[i]})" for i in range(7))
    return f"本周：{week(monday)}\n下周：{week(monday + timedelta(days=7))}"


def norm_md(v) -> str | None:
    """'3-5' / '03-05' / '3月5日' / '2001-03-05' / date → 'MM-DD'；不合法返回 None。"""
    if v is None:
        return None
    if isinstance(v, (date, datetime)):
        return f"{v.month:02d}-{v.day:02d}"
    nums = re.findall(r"\d+", str(v))
    if len(nums) >= 3 and len(nums[0]) == 4:
        nums = nums[1:]
    if len(nums) < 2:
        return None
    m, d = int(nums[0]), int(nums[1])
    try:
        date(2000, m, d)                           # 2000 是闰年：2 月 29 也算合法
    except ValueError:
        return None
    return f"{m:02d}-{d:02d}"
