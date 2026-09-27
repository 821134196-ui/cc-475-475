#!/usr/bin/env bash
# 一条本地启动命令（仅需 Python 3 标准库）
#   ./run.sh            启动系统（保留已有数据），浏览器打开 http://localhost:8000
#   ./run.sh --reset    清空并重新生成示例数据（无标签 / 停机 / 申诉样例）
#   PORT=8123 ./run.sh  自定义端口
set -e
cd "$(dirname "$0")"
PORT="${PORT:-8000}"
exec python3 fridge/server.py --port "$PORT" "$@"
