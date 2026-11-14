"""34.0 截图先过 OCR：看图模型看模糊的字会自己“补”，引用图上原文只认 OCR 抽到的。

36.4 换成 rapidocr 3.9+（默认 PP-OCRv6 small，ONNX，CPU，模型随 pip 包带着）：
    群里 40 张真实截图对比旧的 rapidocr_onnxruntime（PP-OCRv4 mobile）：英文单词之间的空格、形近字（硕士 / 硕土、
    虎扑 / 虎补、大白兔 / 大白免）、中英混排都认得更对；2 核服务器上一张中位 1.9 秒（旧的 1.0 秒）。
    PP-OCRv6 medium 在 2 核上一张 10 秒以上，不用。
    新包没装就退回旧包；两个都没装就什么都不做。
"""
from __future__ import annotations

import io
import logging
import threading

log = logging.getLogger("aigroup.ocr")
_engine = None
_kind = ""
_lock = threading.Lock()
_broken = False


def _new_engine():
    from rapidocr import RapidOCR
    eng = RapidOCR()
    logging.getLogger("RapidOCR").setLevel(logging.ERROR)      # 它自己打彩色 INFO / “没认出字”的 WARNING，太吵（建好之后再调，建的时候它会重设）
    return eng


def _old_engine():
    from rapidocr_onnxruntime import RapidOCR
    return RapidOCR()


def _get():
    global _engine, _kind, _broken
    if _engine is None and not _broken:
        with _lock:
            if _engine is None and not _broken:
                errs = []
                for kind, make in (("rapidocr", _new_engine), ("rapidocr_onnxruntime", _old_engine)):
                    try:
                        _engine, _kind = make(), kind
                        log.info("OCR 用的是 %s", kind)
                        break
                    except Exception as e:  # noqa: BLE001
                        errs.append(f"{kind}: {e}")
                if _engine is None:
                    _broken = True
                    log.info("OCR 用不了（rapidocr 没装？）：%s", "；".join(errs))
    return _engine


def _run(eng, arr) -> list:
    """统一成 [(box, txt, conf), ...]；box 是四个点 [(x, y), ...]。"""
    if _kind == "rapidocr":
        r = eng(arr)
        if getattr(r, "txts", None) is None or getattr(r, "boxes", None) is None:
            return []
        return list(zip([b.tolist() if hasattr(b, "tolist") else b for b in r.boxes], r.txts, r.scores))
    res, _ = eng(arr)
    return list(res or [])


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
        res = _run(eng, np.array(im))
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
