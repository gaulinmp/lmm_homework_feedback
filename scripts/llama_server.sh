#!/usr/bin/env bash
# Start llama-server with an OpenAI-compatible API on 127.0.0.1:8080.
# LangChain (via app.llm.providers.openai_compat) talks to this endpoint.
#
# § Bound to loopback by design — never expose the model server externally,
# even for "internal testing" (§14, §17 of the design doc).
#
# Override LLAMA_DIR, LLAMA_MODEL, or the sampling flags via env if needed.
# Invoked by deploy/tutor-llama.service.

set -euo pipefail

LLAMA_DIR="${LLAMA_DIR:-$HOME/Dropbox/Documents/Programming/AI/llama.cpp/build/bin}"
LLAMA_BIN="${LLAMA_BIN:-$LLAMA_DIR/llama-server}"
LLAMA_MODEL="${LLAMA_MODEL:-unsloth/gemma-4-26B-A4B-it-GGUF:UD-Q4_K_XL}"
LLAMA_HOST="${LLAMA_HOST:-127.0.0.1}"
LLAMA_PORT="${LLAMA_PORT:-8080}"
LLAMA_CTX="${LLAMA_CTX:-8192}"

cd "$LLAMA_DIR"

exec "$LLAMA_BIN" \
    -hf "$LLAMA_MODEL" \
    --host "$LLAMA_HOST" \
    --port "$LLAMA_PORT" \
    --ctx-size "$LLAMA_CTX" \
    --temp 1.0 \
    --top-p 0.95 \
    --top-k 64

# Alternative models tried during development:
#   unsloth/Qwen3.6-27B-GGUF:UD-Q4_K_XL
