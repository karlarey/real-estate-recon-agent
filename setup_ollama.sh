#!/usr/bin/env bash
# Pull the model into the Ollama container, then confirm the app can see it.
#
#   ./setup_ollama.sh
#
# Run this once after `docker compose up`. Safe to re-run.
set -euo pipefail

MODEL="${OLLAMA_MODEL:-llama3.1:8b}"

echo "Pulling ${MODEL} into the ollama container (this can take a few minutes)..."
docker compose exec ollama ollama pull "${MODEL}"

echo
echo "Models available:"
docker compose exec ollama ollama list

echo
echo "Asking the app whether it can see the model:"
curl -s http://localhost:8000/api/chat/status
echo
