#!/bin/bash
# 起 jevnovel 0.8B 打分服务（需要 torch + transformers 的 python 环境）。
# 用法：JEV_MODEL_DIR=/path/to/jev-0.8B-1007-bf16 PYTHON=python ./start_jev.sh
set -e
PYTHON="${PYTHON:-python3}"
JEV_MODEL_DIR="${JEV_MODEL_DIR:?请设置 JEV_MODEL_DIR（jev 0.8B 学生包目录，含 infer.py）}"
PORT="${PORT:-8885}"
DEVICE="${DEVICE:-cuda:0}"

"$PYTHON" "$(dirname "$0")/serve_jev.py" \
  --model-dir "$JEV_MODEL_DIR" \
  --host 0.0.0.0 \
  --port "$PORT" \
  --device "$DEVICE"
