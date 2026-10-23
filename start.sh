#!/usr/bin/env bash
# Linux / macOS 启动：bash start.sh。程序退出会自动重启；换人设时也是靠这个自动重开。
cd "$(dirname "$0")"
[ -f .venv/bin/activate ] && . .venv/bin/activate
export PYTHONUTF8=1 PYTHONIOENCODING=utf-8 AIGROUP_LAUNCHER=1
while true; do
  python run.py
  code=$?
  if [ "$code" = "3" ]; then
    echo "不再自动重启（看上面的提示）。"
    break
  fi
  echo "程序退出了（$code），10 秒后自动重启（Ctrl+C 停止）……"
  sleep 10
done
