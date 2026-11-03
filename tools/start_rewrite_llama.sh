#!/bin/bash
# 用 llama.cpp 的 llama-server 起改文模型（NVIDIA / AMD / CPU 都行）。
# 用法：GGUF=/path/to/rewrite3-shorter-4B-Q8_0.gguf PORT=8080 ./start_rewrite_llama.sh
# 想开 MTP 投机解码再加：--spec-type draft-mtp --spec-draft-n-max 2
set -e
GGUF="${GGUF:-$HOME/models/rewrite3-shorter-4B-Q8_0.gguf}"
PORT="${PORT:-8080}"
LLAMA_SERVER="${LLAMA_SERVER:-llama-server}"

"$LLAMA_SERVER" \
  -m "$GGUF" \
  --host 0.0.0.0 \
  --port "$PORT" \
  -c 32768 \
  -ngl 99 \
  --jinja \
  --alias rewrite3-shorter-4B
