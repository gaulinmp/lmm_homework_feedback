#!/usr/bin/env bash
# Starts llama-server with an OpenAI-compatible API on http://localhost:8080
# LangChain connects to this endpoint.

LLAMA_BIN="$HOME/Dropbox/Documents/Programming/AI/llama.cpp/build/bin/llama-server"

exec "$LLAMA_BIN" \
    -hf unsloth/Qwen3.6-27B-GGUF:UD-Q4_K_XL \
    --ctx-size 16384 \
    --temp 1.0 \
    --top-p 0.95 \
    --min-p 0.01 \
    --top-k 40 \
    --host 127.0.0.1 \
    --port 8080