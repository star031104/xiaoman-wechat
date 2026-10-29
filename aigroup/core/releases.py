"""31.2 每次启动对一下发版记录：deploy/releases.log 最后一行的提交不是现在跑的这个，就补一行“上线”。

10-29 上了 30.4（6b8935d），releases.log 最后一行还是 10-28 的 30.3；31.0 / 31.1 用 release_gate --record 记的又是改之前的提交
（闸门在提交前跑）。日报按 releases.log 分版本，记错了就分错段。现在以“启动时真在跑的提交”为准：
只在已经有 releases.log 的机器上补（自己部署的不会多出这个文件）。
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path

log = logging.getLogger("aigroup")
ROOT = Path(__file__).resolve().parents[2]


def changelog_version(root: Path = ROOT) -> str:
    try:
        for line in (root / "CHANGELOG.md").read_text(encoding="utf-8").splitlines()[:40]:
            m = re.match(r"^##\s*([0-9][0-9.]*)", line)
            if m:
                return m.group(1)
    except OSError:
        pass
    return "?"


def last_commit(text: str) -> str:
    lines = [ln for ln in text.splitlines() if ln.strip() and not ln.startswith("#")]
    if not lines:
        return ""
    parts = lines[-1].split()
    return parts[2] if len(parts) >= 3 else ""


def note_deploy(root: Path = ROOT, now: datetime | None = None) -> str:
    """补记一行，返回补的那行（没补返回空）。出错不影响启动。"""
    if os.environ.get("PYTEST_CURRENT_TEST") and root == ROOT:
        return ""                                  # 跑测试时启动的不算上线（不然测试会往真的 releases.log 里写）
    log_path = root / "deploy" / "releases.log"
    if not log_path.is_file() or not (root / ".git").exists():
        return ""
    try:
        head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=root, capture_output=True, text=True,
                              timeout=10).stdout.strip()
        if not head or last_commit(log_path.read_text(encoding="utf-8")) == head:
            return ""
        line = f"{(now or datetime.now()):%Y-%m-%dT%H:%M} {changelog_version(root)} {head} 上线\n"
        with log_path.open("a", encoding="utf-8") as f:
            f.write(line)
        log.info("发版记录补了一行：%s", line.strip())
        return line
    except Exception as e:  # noqa: BLE001
        log.info("发版记录没补上：%s", e)
        return ""
