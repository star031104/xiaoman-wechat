#!/bin/sh
# 在服务器上起 Linux 版微信（Docker）。网页 VNC 只开在 127.0.0.1:5800：
# 在自己电脑上 ssh -N -L 5800:127.0.0.1:5800 用户@服务器，浏览器开 http://127.0.0.1:5800 扫码登录她的小号。
# 登录信息、聊天库都在 $D 下面，重启容器不用重新扫码（微信隔一段时间可能要求重新确认）。
set -e
D=${WECHAT_DIR:-$HOME/xiaoman/wechat}
cd "$(dirname "$0")"
mkdir -p "$D/config" "$D/xwechat" "$D/data/_outbox"
sudo docker build -t xiaoman-wechat-box .
sudo docker rm -f wechat >/dev/null 2>&1 || true
sudo docker run -d --name wechat --restart unless-stopped \
  -p 127.0.0.1:5800:5800 \
  -v "$D/config":/config -v "$D/xwechat":/root/.xwechat -v "$D/data":/root/xwechat_files \
  -e HOME=/root -e USER_ID=0 -e GROUP_ID=0 -e TZ=Asia/Shanghai -e LANG=zh_CN.UTF-8 -e KEEP_APP_RUNNING=1 \
  -e DISPLAY_WIDTH=1280 -e DISPLAY_HEIGHT=800 \
  --log-opt max-size=10m --log-opt max-file=2 --shm-size 256m --memory 1500m \
  xiaoman-wechat-box
echo "起来了：ssh 隧道接 5800 端口后浏览器打开 http://127.0.0.1:5800 扫码"
