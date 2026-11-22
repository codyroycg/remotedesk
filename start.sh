#!/usr/bin/env sh
# Start the RemoteDesk server (macOS / Linux). Usage: ./start.sh
set -e
cd "$(dirname "$0")/server"
if ! command -v node >/dev/null 2>&1; then
  echo "Node.js is required. Install it from https://nodejs.org (version 18 or newer), then run this again."
  exit 1
fi
[ -d node_modules ] || npm install --omit=dev --no-fund --no-audit
exec node server.js
