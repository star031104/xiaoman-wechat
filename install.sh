#!/usr/bin/env bash
# Linux / macOS 安装：bash install.sh（建 .venv、装依赖，最后跑配置向导）
# 服务器上用 Docker 跑微信，见 docs/WECHAT_LINUX.md
set -e
cd "$(dirname "$0")"
PY=${PYTHON:-python3}
echo "[1/3] 创建虚拟环境 .venv（Python 3.10+）"
[ -d .venv ] || $PY -m venv .venv
. .venv/bin/activate
python -m pip install -U pip
echo "[2/3] 安装依赖"
pip install -r requirements.txt || pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
pip install lunar_python || echo "（农历库没装上，不影响使用）"
pip install trafilatura || echo "（trafilatura 没装上，不影响使用：读网页用自带的办法）"
pip install curl_cffi || echo "（curl_cffi 没装上，不影响使用：被网站拦下时少一种办法）"
if [ "${AILIFE_BROWSER:-1}" != "0" ]; then        # 19.3 无头浏览器：读脚本加载的网页（约 100MB；AILIFE_BROWSER=0 跳过）
  pip install playwright && { python -m playwright install chromium-headless-shell \
    || PLAYWRIGHT_DOWNLOAD_HOST=https://npmmirror.com/mirrors/playwright python -m playwright install chromium-headless-shell; } \
    || echo "（无头浏览器没装上，不影响使用：脚本加载的网页会改用别的办法读）"
fi
echo "[3/3] 检查 ffmpeg（看动图 / 视频、听 B 站视频要用）"
command -v ffmpeg >/dev/null 2>&1 || echo "  没找到 ffmpeg：Ubuntu/Debian 运行 sudo apt install ffmpeg；macOS 运行 brew install ffmpeg"
if [ ! -f "home/wechat/config.yaml" ]; then
  echo; echo "第一次用：配置"
  python run.py setup "$@"
fi
echo; echo "装好了。bash start.sh 启动（服务器上用 systemd，见 docs/WECHAT_LINUX.md）。"
