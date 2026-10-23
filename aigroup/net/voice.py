"""13.0 语音：听懂群友的语音（免费），她自己偶尔发一条语音（在服务器上本地合成，不花钱）。

听：OpenAI 兼容的 /audio/transcriptions，默认硅基流动免费的 FunAudioLLM/SenseVoiceSmall（和看 B 站视频共用 net.asr）。
    SenseVoice 会把情绪和声音事件写成 emoji（😊 开心、😔 难过、😡 生气、😀 笑声、😭 哭……），这里翻成文字：
    “[语音 8 秒，带笑，听起来挺开心：卧槽今天赢了]”，她能接情绪，不只接字。只存文字，音频不留。
    QQ：先用 NapCat 的 get_record 转成 mp3 拿 base64，拿不到再用消息里的下载地址。微信的语音还读不到。
说：sherpa-onnx 跑 Kokoro（CPU 就行，2 核 4G 的服务器也能跑）。net.tts.model_dir 指到解压好的模型目录
    （比如 kokoro-int8-multi-lang-v1_1），没装 sherpa-onnx 或没模型就不发语音。只用一个固定的合成音色，
    不克隆任何真人的声音。只在有人让她“发语音 / 说句话听听”、生日祝福时用，每天有上限。
"""
from __future__ import annotations

import asyncio
import base64
import io
import logging
import re
import wave
from pathlib import Path

log = logging.getLogger("aigroup.voice")

EMOTIONS = {"😊": "听起来挺开心", "😔": "听起来有点低落", "😡": "听起来有点生气", "😰": "听起来有点慌",
            "🤢": "听起来很嫌弃", "😮": "听起来很惊讶"}
EVENTS = {"😀": "带笑", "😭": "在哭", "👏": "有掌声", "🎼": "有背景音乐", "🤧": "咳嗽 / 打喷嚏"}
ASK_VOICE = re.compile(r"(发|来|说)(个|条|句|段)?语音|说句话(听听|来听)|听听你的声音|你(的)?声音")


def parse_sensevoice(text: str) -> tuple[str, list[str]]:
    """SenseVoice 的输出 → (干净的文字, [情绪 / 事件说明])。"""
    tags: list[str] = []
    for e, w in {**EVENTS, **EMOTIONS}.items():
        if e in str(text or "") and w not in tags:
            tags.append(w)
    clean = re.sub("[\U0001F000-\U0001FFFF☀-➿]", "", str(text or ""))
    clean = re.sub(r"<\|[^|]*\|>", "", clean)
    return re.sub(r"\s+", " ", clean).strip(), tags


def voice_text(text: str, tags: list[str], seconds: float | None = None) -> str:
    head = "语音" + (f" {int(seconds)} 秒" if seconds else "")
    if tags:
        head += "，" + "，".join(tags)
    return f"[{head}：{text}]" if text else f"[{head}，没听清]"


def wav_seconds(data: bytes) -> float | None:
    try:
        with wave.open(io.BytesIO(data)) as w:
            return w.getnframes() / float(w.getframerate())
    except Exception:  # noqa: BLE001
        return None


class Hearing:
    def __init__(self, cfg):
        from aigroup.net.bili import ASR
        self.asr = ASR(cfg)

    @property
    def ready(self) -> bool:
        return self.asr.enabled

    async def fetch_qq(self, ob, file: str | None, url: str | None) -> bytes | None:
        """QQ 的语音：NapCat get_record（转 mp3）→ base64；不行再按下载地址拿（可能是 silk，转写接口不一定认）。"""
        if file:
            try:
                d = await ob.call("get_record", file=file, out_format="mp3")
                b64 = (d or {}).get("base64")
                if b64:
                    return base64.b64decode(b64)
                path = (d or {}).get("file")
                if path and Path(path).is_file():                  # NapCat 和程序在同一台机器、同一个文件系统
                    return Path(path).read_bytes()
            except Exception as e:  # noqa: BLE001
                log.debug("get_record 失败：%s", e)
        if url:
            from aigroup.onebot import fetch_bytes
            return await fetch_bytes(url, limit=4 * 1024 * 1024) or None
        return None

    async def hear(self, data: bytes) -> tuple[str, list[str]]:
        raw = await self.asr.one_raw(data, "voice.mp3", "audio/mpeg")
        return parse_sensevoice(raw)


class Voice:
    """本地语音合成（sherpa-onnx + Kokoro）。"""

    def __init__(self, cfg):
        c = dict(((cfg.get("net") or {}).get("tts")) or {})
        self.dir = Path(str(c.get("model_dir") or "")).expanduser() if c.get("model_dir") else None
        if self.dir is not None and not self.dir.is_absolute():
            self.dir = Path(cfg.root) / self.dir
        self.sid = int(c.get("sid", 46) or 0)            # Kokoro multi-lang v1.1 里的中文女声（zf_ 开头那几个，46 左右）
        self.speed = float(c.get("speed", 1.0) or 1.0)
        self.per_day = int(c.get("per_day", 5) or 0)
        self.enabled = bool(c.get("enabled", True)) and self.dir is not None and self.dir.is_dir()
        self._tts = None

    @property
    def ready(self) -> bool:
        if not self.enabled:
            return False
        try:
            import sherpa_onnx  # noqa: F401  # type: ignore
        except Exception:  # noqa: BLE001
            return False
        return True

    def _load(self):
        if self._tts is not None:
            return self._tts
        import sherpa_onnx  # type: ignore
        d = self.dir
        model = next((str(p) for p in (d / "model.int8.onnx", d / "model.onnx") if p.is_file()), "")
        lex = ",".join(str(d / x) for x in ("lexicon-us-en.txt", "lexicon-zh.txt") if (d / x).is_file())
        fst = ",".join(str(p) for p in sorted(d.glob("*.fst")))
        cfg = sherpa_onnx.OfflineTtsConfig(
            model=sherpa_onnx.OfflineTtsModelConfig(
                kokoro=sherpa_onnx.OfflineTtsKokoroModelConfig(
                    model=model, voices=str(d / "voices.bin"), tokens=str(d / "tokens.txt"),
                    data_dir=str(d / "espeak-ng-data"), dict_dir=str(d / "dict") if (d / "dict").is_dir() else "",
                    lexicon=lex),
                num_threads=2, provider="cpu"),
            rule_fsts=fst, max_num_sentences=1)
        self._tts = sherpa_onnx.OfflineTts(cfg)
        return self._tts

    def _synth(self, text: str) -> bytes:
        tts = self._load()
        audio = tts.generate(text, sid=self.sid, speed=self.speed)
        import numpy as np  # type: ignore
        pcm = (np.clip(np.asarray(audio.samples), -1, 1) * 32767).astype("<i2").tobytes()
        out = io.BytesIO()
        with wave.open(out, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(int(audio.sample_rate))
            w.writeframes(pcm)
        return out.getvalue()

    async def synth(self, text: str) -> bytes | None:
        text = re.sub(r"\[[^\]]*\]|[\U0001F000-\U0001FFFF]", "", str(text or "")).strip()[:80]
        if not text or not self.ready:
            return None
        try:
            return await asyncio.to_thread(self._synth, text)
        except Exception as e:  # noqa: BLE001
            log.info("语音没合成出来：%s", e)
            return None
