"""34.0 截图先过 OCR：看图模型看模糊的字会自己“补”，引用图上原文只认 OCR 抽到的。

用 RapidOCR（PP-OCR 的 ONNX 版，CPU 上一张 1~2 秒，模型随 pip 包带着）；没装就什么都不做。
"""
from __future__ import annotations

import io
import logging
import threading

log = logging.getLogger("aigroup.ocr")
_engine = None
_lock = threading.Lock()
_broken = False


def _get():
    global _engine, _broken
    if _engine is None and not _broken:
        with _lock:
            if _engine is None and not _broken:
                try:
                    from rapidocr_onnxruntime import RapidOCR
                    _engine = RapidOCR()
                except Exception as e:  # noqa: BLE001
                    _broken = True
                    log.info("OCR 用不了（没装 rapidocr_onnxruntime？）：%s", e)
    return _engine


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
        res, _ = eng(np.array(im))
    except Exception as e:  # noqa: BLE001
        log.info("OCR 出错：%s", e)
        return ""
    lines = []
    for box, txt, conf in res or []:
        try:
            if float(conf) >= min_conf and str(txt).strip():
                lines.append((min(p[1] for p in box), min(p[0] for p in box), str(txt).strip()))
        except Exception:  # noqa: BLE001
            continue
    lines.sort(key=lambda x: (round(x[0] / 12), x[1]))
    out = " / ".join(x[2] for x in lines)
    return out[:limit]
