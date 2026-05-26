#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"
export PATH="$HOME/.local/bin:$PATH"

HOST="${MODELS3D_HOST:-0.0.0.0}"
PORT="${MODELS3D_PORT:-8000}"
IP=$(hostname -I | awk '{print $1}')

echo "→ Avvio models3d-web su http://${IP}:${PORT} (LAN) — http://localhost:${PORT}"
exec env MODELS3D_HOST="$HOST" MODELS3D_PORT="$PORT" uv run models3d-web
