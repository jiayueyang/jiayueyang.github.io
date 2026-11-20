#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p data
# A restart can run the lifecycle hook again while the previous process lives.
if curl --silent --fail --max-time 2 http://127.0.0.1:8000/api/config >/dev/null; then exit 0; fi
nohup .venv/bin/python -m uvicorn papertranslate.api:app --host 0.0.0.0 --port 8000 --no-proxy-headers --no-access-log >data/server.log 2>&1 </dev/null &
