#!/bin/bash
# 用 vLLM 起改文模型（NVIDIA 卡）。需要 vLLM >= 0.28（支持 Qwen3.5 的 MTP 投机解码）。
# 用法：MODEL_DIR=/path/to/rewrite3-27b-fp8 PORT=8080 ./start_rewrite_vllm.sh
set -e
MODEL_DIR="${MODEL_DIR:-$HOME/models/rewrite3-27b-fp8}"
PORT="${PORT:-8080}"
SERVED_NAME="${SERVED_NAME:-rewrite3-27B}"
GPU_UTIL="${GPU_UTIL:-0.85}"

vllm serve "$MODEL_DIR" \
  --served-model-name "$SERVED_NAME" \
  --speculative-config '{"method":"qwen3_5_mtp","num_speculative_tokens":3}' \
  --kv-cache-dtype fp8 \
  --max-model-len 16384 \
  --max-num-seqs 32 \
  --gpu-memory-utilization "$GPU_UTIL" \
  --host 0.0.0.0 \
  --port "$PORT"
