"""13.0 提交前的隐私扫描：要推到 GitHub 的文件里不能有密钥、自己的群号 / 微信号、朋友的名字、服务器 IP。

用法：
    python scripts/privacy_scan.py            扫 git ls-files 列出的所有文件（推送前跑一遍）
    python scripts/privacy_scan.py --staged   只扫这次要提交的（.githooks/pre-commit 用这个）
装钩子（一次就好）：git config core.hooksPath .githooks

除了通用的规则（GitHub token、sk- 开头的 Key、wxid_、xxx@chatroom、IP 地址、私钥），还会读仓库根目录的
.privacy-words（不进仓库，.gitignore 里有）：一行一个你不想出现在仓库里的词——自己的群号、QQ 号、朋友的真名和外号、
服务器 IP、域名。命中任何一条就列出来，钩子会拒绝这次提交。确实是误报的行，在行尾写上 `privacy: ok`。
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RULES = [
    ("GitHub token", re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}")),
    ("API Key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}")),
    ("私钥", re.compile(r"-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("微信号", re.compile(r"\bwxid_[a-z0-9]{8,}")),
    ("微信群 id", re.compile(r"\b\d{8,}@chatroom")),
    ("IP 地址", re.compile(r"(?<![\d.])(?!127\.|0\.|10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.)(\d{1,3}\.){3}\d{1,3}(?![\d.])")),
]
# 示例和测试里常用的假值
ALLOW = re.compile(r"^(?:1\.2\.3\.4|0\.0\.0\.0|8\.8\.8\.8|1\.1\.1\.1|198\.1[89]\.\S*|100\.100\.100\.100|169\.254\.\S*|"
                   r"1?2345678@chatroom|123456@chatroom|\d*(\d)\1{5,}\d*@chatroom)$")
SKIP_EXT = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".webp", ".zip", ".whl", ".pyc", ".db", ".ttc", ".ttf"}
OK = "privacy: ok"


def files(staged: bool) -> list[Path]:
    args = ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"] if staged else ["git", "ls-files"]
    out = subprocess.run(args, cwd=ROOT, capture_output=True, text=True, check=True).stdout
    return [ROOT / f for f in out.splitlines() if f.strip()]


def words() -> list[str]:
    p = ROOT / ".privacy-words"
    if not p.exists():
        return []
    return [w.strip() for w in p.read_text(encoding="utf-8").splitlines() if w.strip() and not w.startswith("#")]


def scan(paths: list[Path], extra: list[str]) -> list[str]:
    hits = []
    for f in paths:
        if f.suffix.lower() in SKIP_EXT or not f.is_file() or f.name == ".privacy-words":
            continue
        try:
            text = f.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if OK in line:
                continue
            for name, rx in RULES:
                m = rx.search(line)
                if not m or ALLOW.search(m.group(0)) or (name == "IP 地址" and "<path" in line):
                    continue
                hits.append(f"{f.relative_to(ROOT)}:{i}  {name}：{line.strip()[:100]}")
            for w in extra:
                if w in line:
                    hits.append(f"{f.relative_to(ROOT)}:{i}  私人词「{w[:2]}…」")
    return hits


def main(argv: list[str]) -> int:
    hits = scan(files("--staged" in argv), words())
    if hits:
        print("隐私扫描没通过（这些不能进仓库）：")
        print("\n".join(hits[:80]))
        print("\n改掉它们再提交；确实是误报的行，行尾加注释 `privacy: ok`。")
        return 1
    print("隐私扫描：没发现问题")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
