"""xiaoman-wechat 启动入口：微信群里的小满。

    python run.py [命令]            不带命令 = 启动她；python run.py help 看全部命令
    python run.py wechat [命令]     和上面一样（兼容老写法）

“她的家”放她自己的东西：config.yaml、.env（密钥）、data/（数据库、日志、备份）、你自己写的人设，默认是 home/wechat。
想放到别处：加 --home 目录，或者设环境变量 AIGROUP_HOME。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

CODE = Path(__file__).resolve().parent
TARGETS = {"wechat": "微信群"}
ALIASES = {"wx": "wechat", "weixin": "wechat"}


def home_of(target: str = "wechat", override: str | None = None) -> Path:
    if override:
        return Path(override).expanduser().resolve()
    env = os.environ.get("AIGROUP_HOME")
    if env:
        return Path(env).expanduser().resolve()
    return CODE / "home" / "wechat"


def main(argv: list[str]) -> int:
    argv = list(argv)
    override = None
    if "--home" in argv:
        i = argv.index("--home")
        if i + 1 >= len(argv):
            print("--home 后面要写目录")
            return 2
        override = argv[i + 1]
        del argv[i:i + 2]
    if argv and ALIASES.get(argv[0], argv[0]) in TARGETS:
        argv = argv[1:]
    home = home_of("wechat", override)
    home.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(CODE))
    os.environ["AIGROUP_HOME"] = str(home)
    os.environ["AIGROUP_CHANNEL"] = "wechat"
    from aigroup.cli import main as cli
    return int(cli(argv) or 0)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] in ("-h", "--help"):
        print(__doc__)
        sys.exit(0)
    sys.exit(main(sys.argv[1:]))
