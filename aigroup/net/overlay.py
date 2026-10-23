"""13.0 图上的中文字：画图模型写中文常出错（缺笔画、乱码），所以字由程序叠上去。

要 Pillow 和一个中文字体：配置 net.font_file，或者自动找（Windows 的微软雅黑 / 黑体；Linux 的 Noto CJK / 文泉驿，
Ubuntu 上 `sudo apt install fonts-noto-cjk`）。没有 Pillow 或找不到字体时返回 None（调用方照常发原图，字另发一句）。
"""
from __future__ import annotations

import io
import logging
from pathlib import Path

log = logging.getLogger("aigroup.photo")

FONTS = [
    "C:/Windows/Fonts/msyhbd.ttc", "C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/simhei.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc", "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Bold.ttc", "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc", "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/System/Library/Fonts/PingFang.ttc",
]


def find_font(prefer: str = "") -> str | None:
    for f in ([prefer] if prefer else []) + FONTS:
        if f and Path(f).is_file():
            return f
    for d in ("/usr/share/fonts", str(Path.home() / ".fonts")):
        for f in sorted(Path(d).rglob("*CJK*")) if Path(d).is_dir() else []:
            if f.suffix.lower() in (".ttc", ".otf", ".ttf"):
                return str(f)
    return None


def _wrap(draw, text: str, font, width: int) -> list[str]:
    lines = []
    for para in str(text).split("\n"):
        cur = ""
        for ch in para:
            if draw.textlength(cur + ch, font=font) > width and cur:
                lines.append(cur)
                cur = ch
            else:
                cur += ch
        lines.append(cur)
    return [ln for ln in lines if ln.strip()][:4]


def overlay_text(data: bytes, text: str, font_file: str = "", where: str = "bottom") -> bytes | None:
    """在图的下方（或上方）叠一条半透明底的白字，字号随图大小。失败返回 None。"""
    text = str(text or "").strip()[:60]
    if not text:
        return data
    try:
        from PIL import Image, ImageDraw, ImageFont  # type: ignore
    except Exception:  # noqa: BLE001
        log.info("没装 Pillow：图上的字没叠上去")
        return None
    path = find_font(font_file)
    if not path:
        log.info("找不到中文字体：图上的字没叠上去（Linux 上装 fonts-noto-cjk，或者写 net.font_file）")
        return None
    try:
        im = Image.open(io.BytesIO(data)).convert("RGBA")
        w, h = im.size
        size = max(18, int(min(w, h) * 0.075))
        font = ImageFont.truetype(path, size)
        layer = Image.new("RGBA", im.size, (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        lines = _wrap(d, text, font, int(w * 0.86))
        line_h = int(size * 1.35)
        box_h = line_h * len(lines) + size
        y0 = h - box_h - int(h * 0.04) if where == "bottom" else int(h * 0.04)
        d.rounded_rectangle([int(w * 0.04), y0, int(w * 0.96), y0 + box_h], radius=size // 2, fill=(0, 0, 0, 120))
        y = y0 + size // 2
        for ln in lines:
            tw = d.textlength(ln, font=font)
            d.text(((w - tw) / 2, y), ln, font=font, fill=(255, 255, 255, 255),
                   stroke_width=max(1, size // 18), stroke_fill=(0, 0, 0, 160))
            y += line_h
        out = io.BytesIO()
        Image.alpha_composite(im, layer).convert("RGB").save(out, "JPEG", quality=90)
        return out.getvalue()
    except Exception as e:  # noqa: BLE001
        log.info("图上叠字失败：%s", e)
        return None
