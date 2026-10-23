"""4.2 她的“照片”：偶尔在群里晒一张日常（吃的、桌面、窗外、宠物），不拍自己的脸。

用 config.llm.yaml 里 routes.image 的画图接口（OpenAI 兼容的 /images/generations；硅基流动的 Z-Image-Turbo / Kolors 都行）；
没配就不发。每天最多 photo_per_day 张（默认 1）。画风固定写在人设的 look 里（没写就用日系插画风）。
13.0：画面里有她自己时用 routes.image_ref（带 data/avatar/ 里的参考图，长相一致）；图上的中文字用 Pillow 叠上去（overlay.py）。
"""
from __future__ import annotations

import base64
import logging
import time
from datetime import datetime

import httpx

log = logging.getLogger("aigroup.photo")

STYLE = "日系插画风格的手机随手拍，生活感，自然光，画面干净，不出现文字，不出现人脸"


class ImageGen:
    """route=image：日常的图（Z-Image-Turbo 一类，只给文字）；route=image_ref：带参考图画她自己
    （Qwen-Image-Edit-2509 一类，参考图走 image / image2 / image3，最多 3 张；这类模型不收 image_size）。"""

    def __init__(self, cfg, route: str = "image"):
        llm = cfg.get("llm") or {}
        profiles = llm.get("profiles") or {}
        keys = dict(cfg.get("keys") or {})
        self.chain = []
        for n in ((llm.get("routes") or {}).get(route) or []):
            if n in profiles:
                p = dict(profiles[n], name=n)
                p["api_key"] = p.get("api_key") or keys.get(p.get("key") or "", "")
                self.chain.append(p)
        self.proxy = cfg.get("proxy") or None
        self.last_price: float | None = None          # 上一张图这个接口自己写的单价（美元，profile 里的 image_price）

    @property
    def ready(self) -> bool:
        return bool(self.chain)

    @staticmethod
    def _uri(b: bytes) -> str:
        mime = "image/png" if b[:4] == b"\x89PNG" else "image/jpeg"
        return f"data:{mime};base64,{base64.b64encode(b).decode()}"

    async def generate(self, prompt: str, size: str = "768x1024", refs: list[bytes] | None = None) -> bytes | None:
        for p in self.chain:
            try:
                body = {"model": p["model"], "prompt": prompt, **dict(p.get("extra_body") or {})}
                if refs:
                    for i, b in enumerate(refs[:3]):
                        body["image" if i == 0 else f"image{i + 1}"] = self._uri(b)
                if not (refs and "edit" in str(p["model"]).lower()):
                    body.update(size=size, image_size=size)
                async with httpx.AsyncClient(timeout=float(p.get("timeout", 120)),
                                             proxy=self.proxy if p.get("use_proxy") else None) as cli:
                    r = await cli.post(f"{str(p['base_url']).rstrip('/')}/images/generations",
                                       headers={"Authorization": f"Bearer {p.get('api_key') or 'none'}"}, json=body)
                    r.raise_for_status()
                    d = r.json()
                    item = (d.get("data") or d.get("images") or [{}])[0]      # OpenAI 是 data，硅基流动是 images
                    if item.get("b64_json"):
                        data = base64.b64decode(item["b64_json"])
                    else:
                        img = await cli.get(item["url"])
                        img.raise_for_status()
                        data = img.content
                    self.last_price = float(p["image_price"]) if p.get("image_price") is not None else None
                    return data
            except Exception as e:  # noqa: BLE001
                log.warning("用 %s 画图失败：%s", p["name"], str(e)[:200])
        return None


class PhotoMaker:
    def __init__(self, cfg, db, persona=None, per_day: int = 1):
        self.gen = ImageGen(cfg)
        self.ref_gen = ImageGen(cfg, "image_ref")
        self.db, self.tz, self.persona, self.per_day = db, cfg.tz, persona, int(per_day)
        from pathlib import Path
        self.avatar_dir = Path(cfg.root) / "data" / "avatar"
        self.font = str((cfg.get("net") or {}).get("font_file") or "") if hasattr(cfg, "get") else ""

    @property
    def ready(self) -> bool:
        return self.gen.ready and self.per_day > 0

    def _key(self) -> str:
        return f"qq_photos_{datetime.now(self.tz):%Y-%m-%d}"

    def remaining_today(self) -> int:
        return max(0, self.per_day - int(self.db.get(self._key(), 0) or 0))

    def prompt(self, scene: str) -> str:
        look = str(getattr(self.persona, "photo_style", "") or "").strip()
        return f"{look or STYLE}。画面：{scene.strip()}"

    # ---------- 13.0 她自己的样子：data/avatar/ 里的参考图（1~3 张，同一个虚拟形象） ----------
    def avatar_refs(self) -> list[bytes]:
        if not self.avatar_dir.is_dir():
            return []
        files = sorted(f for f in self.avatar_dir.iterdir() if f.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp"))
        return [f.read_bytes() for f in files[:3] if f.stat().st_size < 6 * 1024 * 1024]

    @property
    def can_draw_self(self) -> bool:
        return self.ref_gen.ready and bool(self.avatar_refs())

    def _budget_ok(self) -> bool:
        llm = getattr(self, "llm", None)
        if llm is not None and hasattr(llm, "budget"):
            from aigroup.llm.budget import BudgetExceeded
            try:
                llm.budget.check(float(llm.stats().get("usd", 0) or 0), llm._today(), False)
            except BudgetExceeded:
                return False
        return True

    def _charge(self, gen: ImageGen):
        llm = getattr(self, "llm", None)
        if llm is not None and hasattr(llm, "add_cost"):
            llm.add_cost(gen.last_price if gen.last_price is not None else llm.budget.image_usd)   # 画图按张记账
        self.db.set(self._key(), int(self.db.get(self._key(), 0) or 0) + 1)

    async def make(self, scene: str, *, with_self: bool = False, text: str = "", count: bool = True) -> bytes | None:
        """画一张。with_self：画面里有她自己（要有参考图和 image_ref 接口，不然退回不画她的版本）；
        text：要写在图上的中文（程序叠加，不让模型写）；count=False 不占每天的照片名额（生日卡这类）。"""
        if not self.gen.ready and not (with_self and self.can_draw_self):
            return None
        if (count and self.remaining_today() <= 0) or not self._budget_ok():
            return None
        t0 = time.time()
        if with_self and self.can_draw_self:
            gen = self.ref_gen
            prompt = (f"保持参考图里这个角色的长相、发型、服装风格完全一致（她叫{getattr(self.persona, 'name', '她')}），"
                      f"{self.prompt(scene)}。画面里不要出现任何文字")
            data = await gen.generate(prompt, refs=self.avatar_refs())
        else:
            gen = self.gen
            data = await gen.generate(self.prompt(scene) + "。画面里不要出现任何文字")
        if data:
            if count:
                self._charge(gen)
            else:
                self._charge_only(gen)
            if text:
                from aigroup.net.overlay import overlay_text
                data = overlay_text(data, text, self.font) or data
            log.info("画好一张图（%.1fs）：%s", time.time() - t0, scene)
        return data

    def _charge_only(self, gen: ImageGen):
        llm = getattr(self, "llm", None)
        if llm is not None and hasattr(llm, "add_cost"):
            llm.add_cost(gen.last_price if gen.last_price is not None else llm.budget.image_usd)
