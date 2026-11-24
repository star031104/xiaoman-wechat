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


def ocr_lines(data: bytes, min_conf: float = 0.6) -> tuple[list[dict], object]:
    """38.2 图上的每一行字带位置：[{x0, x1, y0, y1（都是占整张图的比例）, text, conf}]，按从上到下排好；
    第二个返回值是缩到 ≤1600 的 RGB 数组（给 chat_layout 取气泡颜色用），出错 / 没字返回 ([], None)。"""
    eng = _get()
    if eng is None or not data:
        return [], None
    try:
        from PIL import Image
        import numpy as np
        im = Image.open(io.BytesIO(data)).convert("RGB")
        if max(im.size) > 1600:
            im.thumbnail((1600, 1600))
        arr = np.array(im)
        r = eng(arr)
    except Exception as e:  # noqa: BLE001
        log.info("OCR 出错：%s", e)
        return [], None
    if getattr(r, "txts", None) is None or getattr(r, "boxes", None) is None:
        return [], arr
    h, w = arr.shape[0], arr.shape[1]
    lines = []
    for box, txt, conf in zip(r.boxes.tolist(), r.txts, r.scores):
        try:
            txt = str(txt).strip()
            if float(conf) >= min_conf and txt and not noise(txt):
                xs, ys = [p[0] for p in box], [p[1] for p in box]
                lines.append({"x0": min(xs) / w, "x1": max(xs) / w, "y0": min(ys) / h, "y1": max(ys) / h,
                              "text": txt, "conf": round(float(conf), 2)})
        except Exception:  # noqa: BLE001
            continue
    lines.sort(key=lambda d: (round(d["y0"] * h / 12), d["x0"]))
    return lines, arr


def lines_text(lines: list[dict], limit: int = 400) -> str:
    return " / ".join(d["text"] for d in lines)[:limit]


def ocr_text(data: bytes, min_conf: float = 0.6, limit: int = 400) -> str:
    """图上的字，按从上到下的顺序，一行一段；没字 / 出错返回空字符串。"""
    lines, _ = ocr_lines(data, min_conf)
    return lines_text(lines, limit)


# ---------------- 38.2 聊天截图：分清谁说的 ----------------
UI_RX = re.compile(r"^(可按住转文字|按住说话|发送|查看应用|全部回复|倒序|热门|撤回了一条消息.*)$")
TIME_RX = re.compile(r"^(\d{1,2}月\d{1,2}日)?\s*(上午|下午|晚上|凌晨|昨天|星期.)?\s*\d{1,2}[:：]\d{2}$")
QUOTE_RX = re.compile(r"^.{1,10}[：:]")


def _self_color(c) -> bool:
    """自己发的气泡：微信绿（亮 160,236,112 / 暗 62,181,117）、QQ 蓝。"""
    r, g, b = (int(x) for x in c[:3])
    return (g > r + 35 and g > b + 15) or (b > r + 50 and b > g + 10 and b > 150)


def _dist(a, b) -> int:
    return sum(abs(int(x) - int(y)) for x, y in zip(a[:3], b[:3]))


def chat_layout(lines: list[dict], arr=None) -> dict:
    """聊天截图：每行字是左边（别人）、右边（截图的人自己）、中间（时间 / 系统提示），左边气泡上面的小字当名字。
    只认气泡颜色（字两边的底色一样、又和背景不一样 = 在气泡里；微信绿 / QQ 蓝 = 自己发的），光看位置会把表格、帖子
    也当成聊天（试过：薪资表被拆成“左边说校名、右边说数字”），所以没有气泡就不算聊天截图。
    返回 {"chat": 像不像聊天截图, "turns": [{"side": "left/right", "who": 名字或"", "text": …}]}。"""
    if len(lines) < 3 or arr is None:
        return {"chat": False, "turns": []}
    try:
        h, w = arr.shape[0], arr.shape[1]
        corners = [arr[2, w // 2], arr[h - 3, w // 2], arr[h // 2, 1], arr[h // 2, w - 2]]
        bg = sorted(corners, key=lambda c: sum(_dist(c, d) for d in corners))[0]
    except Exception:  # noqa: BLE001
        return {"chat": False, "turns": []}
    L = min(d["x0"] for d in lines)
    R = max(d["x1"] for d in lines)
    mh = _med_h(lines)
    info = []
    for d in lines:
        ym = min(h - 1, int((d["y0"] + d["y1"]) / 2 * h))
        cl = arr[ym, max(0, int(d["x0"] * w - 0.012 * w))]
        cr = arr[ym, min(w - 1, int(d["x1"] * w + 0.012 * w))]
        me = _self_color(cl) or _self_color(cr)
        inb = _dist(cl, bg) > 12 and _dist(cr, bg) > 12 and _dist(cl, cr) < 30
        info.append((d, cl, me, inb))
    others = [tuple(int(x) // 8 for x in cl[:3]) for d, cl, me, inb in info if inb and not me]
    mode = max(set(others), key=others.count) if others else None
    tagged = []
    for i, (d, cl, me, inb) in enumerate(info):
        t = d["text"]
        mid = (d["x0"] + d["x1"]) / 2
        if UI_RX.match(t):
            side = ""
        elif TIME_RX.match(t) or (not inb and not me and abs(mid - 0.5) < 0.06 and d["x0"] - L > 0.15 and R - d["x1"] > 0.15):
            side = "mid"
        elif me and (R - d["x1"] < 0.12 or (tagged and tagged[-1][0] == "right"
                                            and d["y0"] - tagged[-1][1]["y1"] < 0.9 * (d["y1"] - d["y0"]))):
            side = "right"
        elif inb:
            off = mode is not None and _dist(tuple(int(x) // 8 for x in cl[:3]), mode) > 2
            side = "quote" if (off and QUOTE_RX.match(t)) else ("left" if d["x0"] - L < 0.3 else "")
        elif len(t) <= 12 and d["x0"] - L < 0.15 and (d["y1"] - d["y0"]) <= 1.05 * mh \
                and i + 1 < len(info) and info[i + 1][3] and not info[i + 1][2]:
            side = "name"
        else:
            side = ""
        tagged.append((side, d))
    n_l = sum(1 for s, _ in tagged if s == "left")
    n_r = sum(1 for s, _ in tagged if s == "right")
    n_n = sum(1 for s, _ in tagged if s == "name")
    chat = (n_l >= 1 and n_r >= 1 and n_l + n_r >= 3) or (n_l >= 3 and n_n >= 1)
    if not chat:
        return {"chat": False, "turns": []}
    turns: list[dict] = []
    who = ""
    prev = None
    for side, d in tagged:
        if side == "name":
            who = d["text"].rstrip("：: ")
            prev = None
            continue
        if side not in ("left", "right"):
            prev = None
            continue
        if turns and prev is not None and turns[-1]["side"] == side and d["y0"] - prev < 0.9 * (d["y1"] - d["y0"]):
            turns[-1]["text"] += d["text"]
        else:
            turns.append({"side": side, "who": who if side == "left" else "", "text": d["text"]})
        prev = d["y1"]
    return {"chat": True, "turns": turns}


def _med_h(lines: list[dict]) -> float:
    hs = sorted(d["y1"] - d["y0"] for d in lines)
    return hs[len(hs) // 2] if hs else 0.0


def render_turns(turns: list[dict], limit: int = 360) -> str:
    """“左边（小王）：……／右边（截图的人自己）：……”"""
    out = []
    for t in turns:
        who = "截图的人自己" if t["side"] == "right" else (t.get("who") or "对方")
        out.append(f"{'右边' if t['side'] == 'right' else '左边'}（{who}）：{t['text'][:60]}")
    return "／".join(out)[:limit]


def crops_for(data: bytes, lines: list[dict], words: list[str], k: int = 2) -> list[bytes]:
    """38.2 被问到细节：问的词落在哪几行字上，就把那一块裁出来放大（宽到 ≥1000 像素）；对不上就裁字最密的一块。"""
    if not data or not lines:
        return []
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(data)).convert("RGB")
    except Exception:  # noqa: BLE001
        return []
    W, H = im.size
    hits = [d for d in lines if any(w and w in d["text"] for w in words)]
    if not hits:
        small = sorted(lines, key=lambda d: d["y1"] - d["y0"])
        hits = small[: max(1, len(small) // 3)]
    groups: list[list[dict]] = []
    for d in sorted(hits, key=lambda d: d["y0"]):
        if groups and d["y0"] - groups[-1][-1]["y1"] < 0.08:
            groups[-1].append(d)
        else:
            groups.append([d])
    out = []
    for g in groups[:k]:
        x0 = max(0.0, min(d["x0"] for d in g) - 0.05)
        x1 = min(1.0, max(d["x1"] for d in g) + 0.05)
        y0 = max(0.0, min(d["y0"] for d in g) - 0.06)
        y1 = min(1.0, max(d["y1"] for d in g) + 0.06)
        c = im.crop((int(x0 * W), int(y0 * H), int(x1 * W), int(y1 * H)))
        if c.size[0] < 1000:
            f = min(4.0, 1000 / max(1, c.size[0]))
            c = c.resize((int(c.size[0] * f), int(c.size[1] * f)))
        b = io.BytesIO()
        c.save(b, "JPEG", quality=90)
        out.append(b.getvalue())
    return out
