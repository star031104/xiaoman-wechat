"""36.4 / 36.5 OCR：rapidocr 3.9+（PP-OCRv6 small），检测框门槛 0.4，单个非汉字的行丢掉。"""
import io

import pytest


def _img(text: str) -> bytes:
    from PIL import Image, ImageDraw, ImageFont
    im = Image.new("RGB", (900, 160), "white")
    try:
        font = ImageFont.load_default(size=56)
    except TypeError:                       # Pillow < 10.1
        pytest.skip("Pillow 太旧，画不了大字")
    ImageDraw.Draw(im).text((30, 40), text, fill="black", font=font)
    b = io.BytesIO()
    im.save(b, "PNG")
    return b.getvalue()


def test_ocr_reads_text():
    from aigroup.net import ocr
    if ocr._get() is None:
        pytest.skip("没装 rapidocr")
    out = ocr.ocr_text(_img("Hello World 2026"))
    assert "2026" in out and "hello" in out.lower()


def test_ocr_blank_image_empty():
    from aigroup.net import ocr
    from PIL import Image
    b = io.BytesIO()
    Image.new("RGB", (400, 300), "white").save(b, "PNG")
    assert ocr.ocr_text(b.getvalue()) == ""


def test_noise_lines():
    from aigroup.net.ocr import noise
    assert noise("#") and noise("Q") and noise("0") and noise(" + ")
    assert not noise("回") and not noise("12") and not noise("小王")


def test_no_old_package_fallback():
    import inspect

    from aigroup.net import ocr
    assert "rapidocr_onnxruntime" not in inspect.getsource(ocr._get)
