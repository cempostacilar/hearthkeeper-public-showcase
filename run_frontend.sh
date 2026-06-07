#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/frontend"

if [[ ! -f package.json ]]; then
  echo "[ERROR] package.json not found in $(pwd)"
  exit 1
fi

echo "[INFO] Working directory: $(pwd)"

if [[ ! -d node_modules ]]; then
  echo "[INFO] Installing npm dependencies..."
  npm install
fi

echo "[INFO] Starting Vite dev server..."
npm run dev
