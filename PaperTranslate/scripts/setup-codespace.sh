#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
sudo apt-get update -qq
sudo apt-get install -y --no-install-recommends ffmpeg fonts-noto-cjk
python -m venv .venv
.venv/bin/pip install -r requirements.txt
