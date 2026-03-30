#!/usr/bin/env bash
set -euo pipefail
ollama pull qwen3:8b
ollama pull qwen2.5-coder:7b
ollama pull embeddinggemma
