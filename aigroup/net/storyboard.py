"""31.0 看视频用“一张宫格图”（思路来自 dsh-video-storyboard，MIT；这里是自己的 Python 实现）。

以前：均匀撒 8 个点 + 弹幕热点各截一张，8 张图一起交给看图模型。毛病是
① 快剪的地方和十几秒不动的空镜分到的张数一样；② 截在转场上会拿到糊掉的叠影；③ 8 张图 8 份图片费用；
④ 回来的描述只认时间点，群友说“第 5 个镜头那里”对不上。

现在：
1. 扫一遍最低清晰度的视频流（每秒 2 帧、缩到 96×54，只留数字，不存图）：
   - 场景切换分数 = 相邻两帧 HSV 三通道差的平均（只看亮度会漏掉“同亮度换颜色”的切点）
   - 清晰度 = 灰度图拉普拉斯方差（转场叠影、运动模糊的帧这个值低）
2. 找切点：阈值按“大约 1.5 秒一个切点”反推，下限 12（固定倍数的阈值在快剪动画上会一个切点都找不到）
3. 分格：每秒的权重 = 1 + 这附近的剪辑密度 + 这附近的台词密度（都按全片平均归一、封顶 3），在积分上等分；
   格子边界吸附到最近的真实切点（挪动不超过理想格长的 30%）。画面几乎不动的片子格数减半（16 张一样的图没意义）
4. 每格取 3 个候选时刻（25% / 50% / 75%），挑清晰度最高的那个，避开切点后 0.5 秒（转场）
5. 按挑好的时刻各截一张 480 宽的图，拼成一张宫格：每格左上角烧上编号，右下角烧上时间；
   每格那段时间说的话放在文字里一起给看图模型（不烧进图里，省图片面积）
6. 追问某个时间（“3:12 那里画面上是啥”）时，对那几秒再截 4 张、拼一张 2×2 的大格子单独看一次（zoom_times）

全部是规则和 ffmpeg / numpy / Pillow，不多调模型；看图从“N 张图”变成“1 张图”。
缺 ffmpeg / numpy / Pillow、视频太长（> scan_max 秒）、扫描失败时，调用方退回原来的均匀撒点。
"""
from __future__ import annotations

import asyncio
import io
import logging
import shutil

log = logging.getLogger(__name__.split(".")[0] + ".bili")

SCAN_FPS = 2
SCAN_W, SCAN_H = 96, 54
CUT_EVERY = 1.5          # 目标：大约 1.5 秒一个切点
CUT_FLOOR = 12.0         # 切点分数下限（0~255 刻度）
SNAP = 0.30              # 格子边界吸附到切点时，最多挪动理想格长的 30%
CELL_W, CELL_H = 384, 216


def available() -> bool:
    try:
        import numpy  # noqa: F401
        from PIL import Image  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return bool(shutil.which("ffmpeg"))


# ---------------- 1. 扫描 ----------------
def _hsv(rgb):
    """rgb uint8 (h, w, 3) → hsv float (h, w, 3)，三个通道都在 0~255。"""
    import numpy as np
    x = rgb.astype(np.float32) / 255.0
    r, g, b = x[..., 0], x[..., 1], x[..., 2]
    mx, mn = x.max(-1), x.min(-1)
    d = mx - mn
    h = np.zeros_like(mx)
    nz = d > 1e-6
    rm = nz & (mx == r)
    gm = nz & (mx == g) & ~rm
    bm = nz & ~rm & ~gm
    h[rm] = ((g - b)[rm] / d[rm]) % 6
    h[gm] = (b - r)[gm] / d[gm] + 2
    h[bm] = (r - g)[bm] / d[bm] + 4
    h = h / 6.0
    s = np.where(mx > 1e-6, d / np.maximum(mx, 1e-6), 0)
    return np.stack([h * 255, s * 255, mx * 255], -1)


def frame_stats(prev_hsv, rgb) -> tuple[float, float, object]:
    """一帧的（和上一帧的切换分数, 清晰度, 这一帧的 hsv）。色相按圆周算差（0 和 255 是挨着的）。"""
    import numpy as np
    hsv = _hsv(rgb)
    if prev_hsv is None:
        diff = 0.0
    else:
        dh = np.abs(hsv[..., 0] - prev_hsv[..., 0])
        dh = np.minimum(dh, 255 - dh)
        diff = float((dh + np.abs(hsv[..., 1] - prev_hsv[..., 1]) + np.abs(hsv[..., 2] - prev_hsv[..., 2])).mean() / 3)
    g = rgb.astype(np.float32) @ np.array([0.299, 0.587, 0.114], np.float32)
    lap = (-4 * g[1:-1, 1:-1] + g[:-2, 1:-1] + g[2:, 1:-1] + g[1:-1, :-2] + g[1:-1, 2:])
    return diff, float(lap.var()), hsv


async def scan(url: str, seconds: int, timeout: float = 120.0) -> dict:
    """解一遍视频流（每秒 2 帧、96×54），返回 {"t": [...], "diff": [...], "sharp": [...]}；失败返回 {}。"""
    import numpy as np
    ff = shutil.which("ffmpeg")
    if not ff:
        return {}
    hdr = "Referer: https://www.bilibili.com/\r\nUser-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36\r\n"
    head = ["-headers", hdr] if str(url).startswith("http") else []
    cmd = [ff, "-nostdin", "-loglevel", "error", *head, "-i", url, "-t", str(int(seconds)), "-an",
           "-vf", f"fps={SCAN_FPS},scale={SCAN_W}:{SCAN_H}", "-pix_fmt", "rgb24", "-f", "rawvideo", "pipe:1"]
    size = SCAN_W * SCAN_H * 3
    ts, diffs, sharps = [], [], []
    p = None
    try:
        p = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)

        async def read_all():
            prev, i = None, 0
            while True:
                try:
                    buf = await p.stdout.readexactly(size)
                except asyncio.IncompleteReadError:
                    break
                rgb = np.frombuffer(buf, np.uint8).reshape(SCAN_H, SCAN_W, 3)
                d, s, prev = frame_stats(prev, rgb)
                ts.append(i / SCAN_FPS)
                diffs.append(d)
                sharps.append(s)
                i += 1
            await p.wait()
        await asyncio.wait_for(read_all(), timeout)
    except Exception as e:  # noqa: BLE001
        log.info("扫视频没扫完：%s", e)
        if p is not None and p.returncode is None:
            try:
                p.kill()
            except Exception:  # noqa: BLE001
                pass
        if len(ts) < 10:
            return {}
    if len(ts) < 4:
        return {}
    return {"t": ts, "diff": diffs, "sharp": sharps}


# ---------------- 2. 切点 ----------------
def find_cuts(t: list[float], diff: list[float], every: float = CUT_EVERY, floor: float = CUT_FLOOR) -> list[float]:
    """局部最大值里，按分数从高到低取“大约每 every 秒一个”那么多，分数不到 floor 的不算；两个切点至少隔 0.5 秒。"""
    if len(diff) < 3:
        return []
    dur = t[-1] - t[0]
    peaks = [i for i in range(1, len(diff)) if diff[i] >= floor and diff[i] >= diff[i - 1]
             and (i + 1 >= len(diff) or diff[i] >= diff[i + 1])]
    want = max(1, int(dur / every))
    peaks.sort(key=lambda i: -diff[i])
    out: list[float] = []
    for i in peaks:
        if len(out) >= want:
            break
        if all(abs(t[i] - x) >= 0.5 for x in out):
            out.append(t[i])
    return sorted(out)


# ---------------- 3. 分格 ----------------
def _ratio(counts: list[float], width: int) -> list[float]:
    """每秒的“附近密度 / 全片平均”，窗口 ±width 秒，封顶 3。"""
    n = len(counts)
    if n == 0:
        return []
    avg = sum(counts) / n
    if avg <= 0:
        return [0.0] * n
    pre = [0.0]
    for c in counts:
        pre.append(pre[-1] + c)
    out = []
    for s in range(n):
        a, b = max(0, s - width), min(n, s + width + 1)
        out.append(min(3.0, (pre[b] - pre[a]) / (b - a) / avg))
    return out


def plan_cells(duration: float, cuts: list[float], n: int, talk: list[tuple[float, int]] | None = None,
               motion_p90: float | None = None) -> list[tuple[float, float]]:
    """把 [0, duration) 分成 n 格：剪辑密、话密的地方格子短。talk：[(秒, 字数)]。返回 [(开始, 结束)]。"""
    dur = max(1.0, float(duration))
    if motion_p90 is not None and motion_p90 < 3.0:            # 画面几乎不动（口播、PPT、静态图）：格数减半
        n = max(4, n // 2)
    n = max(1, min(n, int(dur)))
    secs = int(dur) + 1
    cut_cnt = [0.0] * secs
    for c in cuts:
        if 0 <= c < secs:
            cut_cnt[int(c)] += 1
    talk_cnt = [0.0] * secs
    for s, k in talk or []:
        if 0 <= s < secs:
            talk_cnt[int(s)] += k
    cr, tr = _ratio(cut_cnt, 5), _ratio(talk_cnt, 10)
    w = [1.0 + cr[i] + tr[i] for i in range(secs)]
    total = sum(w)
    bounds, acc, k = [0.0], 0.0, 1
    for s in range(secs):
        acc += w[s]
        while k < n and acc >= total * k / n:
            bounds.append(float(s + 1))
            k += 1
    bounds = bounds[:n] + [dur]
    ideal = dur / n
    for j in range(1, len(bounds) - 1):                         # 吸附到最近的真实切点
        near = [c for c in cuts if abs(c - bounds[j]) <= ideal * SNAP]
        if near:
            bounds[j] = min(near, key=lambda c: abs(c - bounds[j]))
    cells = []
    for j in range(len(bounds) - 1):
        a, b = bounds[j], max(bounds[j] + 0.5, bounds[j + 1])
        if cells and a < cells[-1][1]:
            a = cells[-1][1]
        if b - a >= 0.5:
            cells.append((round(a, 2), round(min(b, dur), 2)))
    return cells


def pick_moment(cell: tuple[float, float], t: list[float], sharp: list[float], cuts: list[float]) -> float:
    """格子里 25% / 50% / 75% 三个候选，挑最清楚的；切点后 0.5 秒内的（转场）不选。"""
    a, b = cell
    cands = [a + (b - a) * f for f in (0.25, 0.5, 0.75)]
    best, best_s = cands[1], -1.0
    for c in cands:
        if any(0 <= c - x < 0.5 for x in cuts):
            continue
        if not t:
            return c
        i = min(range(len(t)), key=lambda k: abs(t[k] - c))
        if sharp[i] > best_s:
            best, best_s = c, sharp[i]
    return round(best, 2)


def talk_from_lines(lines: str) -> list[tuple[float, int]]:
    """“[mm:ss] 一大段话”→ [(秒, 字数)]（一行的字数平均摊到到下一行之前）。"""
    from aigroup.net.bili import line_time
    rows = [(line_time(ln), ln.split("]", 1)[-1].strip()) for ln in (lines or "").splitlines()]
    rows = [(s, x) for s, x in rows if s is not None]
    out = []
    for j, (s, x) in enumerate(rows):
        e = rows[j + 1][0] if j + 1 < len(rows) else s + 20
        span = max(1, int(e - s))
        per = max(1, len(x) // span)
        out += [(float(s + k), per) for k in range(span)]
    return out


def lines_in(lines: str, a: float, b: float, limit: int = 60) -> str:
    """这一格 [a, b) 里说的话（按行的时间算；一行盖住这一格也算）。"""
    from aigroup.net.bili import line_time
    rows = [(line_time(ln), ln.split("]", 1)[-1].strip()) for ln in (lines or "").splitlines()]
    rows = [(s, x) for s, x in rows if s is not None and x]
    got = []
    for j, (s, x) in enumerate(rows):
        e = rows[j + 1][0] if j + 1 < len(rows) else s + 20
        if s < b and e > a:
            got.append(x)
    txt = "".join(got)
    return (txt[:limit] + "…") if len(txt) > limit else txt


# ---------------- 4. 拼图 ----------------
def _font(size: int):
    from PIL import ImageFont
    for path in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
                 "C:/Windows/Fonts/arialbd.ttf", "/System/Library/Fonts/Supplemental/Arial Bold.ttf"):
        try:
            return ImageFont.truetype(path, size)
        except Exception:  # noqa: BLE001
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:                                         # 老 Pillow 没有 size 参数
        return ImageFont.load_default()


def compose(shots: list[tuple[int, bytes, str]], cols: int | None = None, cell_w: int = CELL_W, cell_h: int = CELL_H) -> bytes:
    """[(编号, jpeg, 右下角的字)] → 一张宫格 JPEG。编号烧在左上角（格高 ~22%，小模型数格子会数错、读数字很准）。"""
    from PIL import Image, ImageDraw
    n = len(shots)
    if n == 0:
        return b""
    cols = cols or (2 if n <= 4 else 3 if n <= 9 else 4)
    rows = (n + cols - 1) // cols
    gap = 4
    W, H = cols * cell_w + (cols + 1) * gap, rows * cell_h + (rows + 1) * gap
    sheet = Image.new("RGB", (W, H), (20, 20, 20))
    big, small = _font(max(18, int(cell_h * 0.22))), _font(max(12, int(cell_h * 0.10)))
    d = ImageDraw.Draw(sheet)
    for k, (num, jpg, label) in enumerate(shots):
        x0, y0 = gap + (k % cols) * (cell_w + gap), gap + (k // cols) * (cell_h + gap)
        try:
            im = Image.open(io.BytesIO(jpg)).convert("RGB")
            im.thumbnail((cell_w, cell_h))
            sheet.paste(im, (x0 + (cell_w - im.width) // 2, y0 + (cell_h - im.height) // 2))
        except Exception:  # noqa: BLE001
            d.rectangle([x0, y0, x0 + cell_w, y0 + cell_h], fill=(60, 60, 60))
        d.text((x0 + 6, y0 + 2), str(num), font=big, fill=(255, 235, 59), stroke_width=3, stroke_fill=(0, 0, 0))
        if label:
            tw = d.textlength(label, font=small)
            d.text((x0 + cell_w - tw - 6, y0 + cell_h - small.size - 6), label, font=small, fill=(255, 255, 255),
                   stroke_width=2, stroke_fill=(0, 0, 0))
    out = io.BytesIO()
    sheet.save(out, "JPEG", quality=80)
    return out.getvalue()


def zoom_times(t: float, duration: float, k: int = 4, span: float = 6.0) -> list[float]:
    """追问某一秒：在它前后 span 秒里均匀取 k 个时刻（不出片头片尾）。"""
    a = max(0.0, t - span / 2)
    b = min(max(a + 1, float(duration or t + span)), a + span)
    return [round(a + (b - a) * (i + 0.5) / k, 2) for i in range(k)]
