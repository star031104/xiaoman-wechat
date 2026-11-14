"""34.0 截图先过 OCR：看图模型看模糊的字会自己“补”，引用图上原文只认 OCR 抽到的。

用 rapidocr 3.9+（PP-OCRv6 small，ONNX Runtime，CPU，模型随 pip 包带着）；没装就什么都不做。
- 36.4 从 rapidocr_onnxruntime（PP-OCRv4 mobile）换过来：群里 40 张真实截图里，英文词间空格、形近字（硕士 / 硕土、
  虎扑 / 虎补、大白兔 / 大白免）、中英混排、引号括号都认得更对；2 核服务器上一张中位约 1.9 秒（旧的 1.0 秒）。
  PP-OCRv6 medium 在 2 核上一张 10 秒以上，不用。
- 36.5 检测框门槛 0.5 → 0.4：聊天截图里“名字：话”前面的名字（字小、颜色浅）以前会漏，降了能认出来；
  代价是多出一些单个的符号 / 字母（“#”“Q”“0”），只有一个字、又不是汉字的行直接丢掉。
  试过 unclip_ratio 2.0（框放大）：形近字反而认错（硕士 → 硕土），不用。
"""
from __future__ import annotations

import io
import logging
import re
import threading

log = logging.getLogger("aigroup.ocr")
_engine = None
_lock = threading.Lock()
_broken = False
PARAMS = {"Det.box_thresh": 0.4}
CJK = re.compile(r"[一-鿿]")


def _get():
    global _engine, _broken
    if _engine is None and not _broken:
        with _lock:
            if _engine is None and not _broken:
                try:
                    from rapidocr import RapidOCR
                    _engine = RapidOCR(params=dict(PARAMS))
                    logging.getLogger("RapidOCR").setLevel(logging.ERROR)   # 它自己打彩色 INFO / “没认出字”的 WARNING，太吵
                except Exception as e:  # noqa: BLE001
                    _broken = True
                    log.info("OCR 用不了（没装 rapidocr？）：%s", e)
    return _engine


def noise(txt: str) -> bool:
    """只有一个字、又不是汉字（“#”“Q”“0”“+”）：多半是图标、头像角标，丢掉。"""
    t = re.sub(r"\s+", "", txt or "")
    return len(t) <= 1 and not CJK.search(t)


def ocr_text(data: bytes, min_conf: float = 0.6, limit: int = 400) -> str:
    """图上的字，按从上到下的顺序，一行一段；没字 / 出错返回空字符串。"""
    eng = _get()
    if eng is None or not data:
        return ""
    try:
        from PIL import Image
        import numpy as np
        im = Image.open(io.BytesIO(data)).convert("RGB")
        if max(im.size) > 1600:
            im.thumbnail((1600, 1600))
        r = eng(np.array(im))
    except Exception as e:  # noqa: BLE001
        log.info("OCR 出错：%s", e)
        return ""
    if getattr(r, "txts", None) is None or getattr(r, "boxes", None) is None:
        return ""
    lines = []
    for box, txt, conf in zip(r.boxes.tolist(), r.txts, r.scores):
        try:
            txt = str(txt).strip()
            if float(conf) >= min_conf and txt and not noise(txt):
                lines.append((min(p[1] for p in box), min(p[0] for p in box), txt))
        except Exception:  # noqa: BLE001
            continue
    lines.sort(key=lambda x: (round(x[0] / 12), x[1]))
    out = " / ".join(x[2] for x in lines)
    return out[:limit]
