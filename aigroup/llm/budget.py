"""每天的花费上限（11.3 / 群聊 4.3）：三个她加起来每天最多花多少钱（人民币，按大模型返回的 token 估算，画图按张算）。

config.yaml：
    llm:
      budget:
        daily_cny: 5            # 0 = 不限
        name: yuki              # 这个程序在共享账本里的名字（yuki / qq / wx）
        shared_file: /home/ubuntu/ailife/budget.json   # 服务器上的小管家每 5 分钟写一次三个程序今天各花了多少
        usd_cny: 7.2
到了上限：后台的活（整理记忆、日记、搜一搜、分享、画图……）先停，聊天还能再多用一成（免得说到一半突然不理人），
再超就也停，第二天自动恢复。小屋首页能看到今天花了多少。
"""
from __future__ import annotations

import json
import logging
import time

log = logging.getLogger(__name__.split(".")[0] + ".budget")


class BudgetExceeded(RuntimeError):
    pass


class Budget:
    def __init__(self, cfg):
        c = dict(((cfg.get("llm") or {}).get("budget")) or {})
        self.cap = float(c.get("daily_cny", 0) or 0)
        self.rate = float(c.get("usd_cny", 7.2) or 7.2)
        self.name = str(c.get("name") or "")
        self.shared = str(c.get("shared_file") or "")
        self.overrun = float(c.get("chat_overrun", 1.1) or 1.1)
        self.image_usd = float(c.get("image_usd", 0.01) or 0)
        self._cache: tuple[float, dict] = (0.0, {})
        self._warned = ""

    def others(self, day: str) -> float:
        """共享账本里另外两个程序今天花的（人民币）。"""
        if not self.shared:
            return 0.0
        t, d = self._cache
        if time.time() - t > 60:
            try:
                with open(self.shared, encoding="utf-8") as f:
                    d = json.load(f)
            except (OSError, ValueError):
                d = {}
            self._cache = (time.time(), d)
        if d.get("day") != day:
            return 0.0
        return sum(float(v or 0) for k, v in (d.get("bots") or {}).items() if k != self.name)

    def total(self, own_usd: float, day: str) -> float:
        return own_usd * self.rate + self.others(day)

    def check(self, own_usd: float, day: str, foreground: bool):
        if self.cap <= 0:
            return
        tot = self.total(own_usd, day)
        limit = self.cap * (self.overrun if foreground else 1.0)
        if tot >= limit:
            if self._warned != day + str(foreground):
                self._warned = day + str(foreground)
                log.warning("今天的花费到上限了（约 ¥%.2f / ¥%g）：%s先停", tot, self.cap, "聊天也" if foreground else "后台的活")
            raise BudgetExceeded(f"今天的花费到上限了（约 ¥{tot:.2f} / ¥{self.cap:g}）")

    def status(self, own_usd: float, day: str) -> dict:
        tot = self.total(own_usd, day)
        return {"cap": self.cap, "own_cny": round(own_usd * self.rate, 3), "total_cny": round(tot, 3),
                "over": bool(self.cap > 0 and tot >= self.cap)}
