#!/usr/bin/env bash
# Starts llama-server with an OpenAI-compatible API on http://localhost:8080
# LangChain connects to this endpoint.

LLAMA_DIR="$HOME/Dropbox/Documents/Programming/AI/llama.cpp/build/bin/"
LLAMA_BIN="$LLAMA_DIR/llama-server"

cd "$LLAMA_DIR"

exec "$LLAMA_BIN" \
    -hf unsloth/gemma-4-26B-A4B-it-GGUF:UD-Q4_K_XL \
    --ctx-size 8192 \
    --temp 1.0 \
    --top-p 0.95 \
    --top-k 64 \
    --port 8080

# unsloth/Qwen3.6-27B-GGUF:UD-Q4_K_XL
#
