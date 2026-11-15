#!/bin/bash
# 用 llama.cpp 的 llama-server 起改文模型（NVIDIA / AMD / CPU 都行）。
# 用法：GGUF=/path/to/rewrite3-27b-IQ4_XS.gguf PORT=8080 ./start_rewrite_llama.sh
# 想开 MTP 投机解码（llama.cpp nightly）再加：--spec-type draft-mtp --spec-draft-n-max 3
set -e
GGUF="${GGUF:-$HOME/models/rewrite3-27b-IQ4_XS.gguf}"
PORT="${PORT:-8080}"
LLAMA_SERVER="${LLAMA_SERVER:-llama-server}"

"$LLAMA_SERVER" \
  -m "$GGUF" \
  --host 0.0.0.0 \
  --port "$PORT" \
  -c 16384 \
  -ngl 99 \
  --jinja \
  --alias rewrite3-27B
