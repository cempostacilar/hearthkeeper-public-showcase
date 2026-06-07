#!/bin/bash
# Hearthkeeper - Start server with Cloudflare Tunnel
#
# Usage:
#   bash start.sh              # production mode
#   bash start.sh --dry-run    # dry run (no real cart building)
#   bash start.sh --dev        # dev mode (use Vite frontend)
#
# This script:
#   1. Builds the frontend (unless --dev)
#   2. Starts FastAPI on port 8000
#   3. Starts the named Cloudflare Tunnel → https://hearthkeeper.cc
#
# Domain: hearthkeeper.cc (permanent, HTTPS via Cloudflare)

PORT=8000
DOMAIN="hearthkeeper.cc"
TUNNEL_NAME="hearthkeeper"
CLOUDFLARED="/c/Program Files (x86)/cloudflared/cloudflared.exe"
EXTRA_ARGS="$@"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Build frontend unless in dev mode
if [[ "$*" != *"--dev"* ]]; then
    echo "  Building frontend..."
    cd "$SCRIPT_DIR/frontend" && npx vite build --logLevel error 2>&1
    echo "  Frontend built."
    cd "$SCRIPT_DIR"
fi

# Start FastAPI in background
echo "  Starting Hearthkeeper backend..."
python3 "$SCRIPT_DIR/app.py" $EXTRA_ARGS &
APP_PID=$!
sleep 2

# Start named Cloudflare Tunnel (permanent domain, not random URL)
echo ""
echo "  Starting Cloudflare Tunnel..."
echo "  ============================================="
echo "  Domain:  https://$DOMAIN"
echo "  Local:   http://localhost:$PORT"
echo "  ============================================="
echo ""
"$CLOUDFLARED" tunnel run "$TUNNEL_NAME" 2>&1 &
TUNNEL_PID=$!

# Wait for either to exit
trap "kill $APP_PID $TUNNEL_PID 2>/dev/null; exit" INT TERM
wait $APP_PID
kill $TUNNEL_PID 2>/dev/null
