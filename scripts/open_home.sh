#!/usr/bin/env bash
# 她跑在 Linux 服务器上时，在自己电脑（macOS / Linux）上打开小屋：SSH 隧道把服务器只开在本机的端口接过来。
#   8768 小屋  5800 微信网页 VNC（扫码登录）
# 用法：bash scripts/open_home.sh ubuntu@1.2.3.4 [端口]（服务器第一次填完会记在 data/server.txt）
cd "$(dirname "$0")/.." || exit 1
mkdir -p data
[ -n "$1" ] && echo "$1" > data/server.txt
SRV=$(cat data/server.txt 2>/dev/null)
[ -z "$SRV" ] && { echo "用法：bash scripts/open_home.sh ubuntu@服务器IP"; exit 1; }
KEY=(); [ -f data/server_key ] && KEY=(-i data/server_key)
ssh "${KEY[@]}" -o StrictHostKeyChecking=accept-new -o IdentitiesOnly=yes -o ServerAliveInterval=30 -f -N \
  -L 8768:127.0.0.1:8768 -L 5800:127.0.0.1:5800 "$SRV" || exit 1
URL="http://127.0.0.1:${2:-8768}"
(command -v open >/dev/null && open "$URL") || (command -v xdg-open >/dev/null && xdg-open "$URL") || echo "浏览器打开 $URL"
