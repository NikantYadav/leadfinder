#!/usr/bin/env bash
# Enfolo Lead Finder installer: sets up the environment and starts the web UI.
set -e
cd "$(dirname "$0")"

PY=""
for c in python3.12 python3.11 python3.10 python3; do
  if command -v "$c" >/dev/null && "$c" -c 'import sys; sys.exit(sys.version_info < (3,10))'; then PY="$c"; break; fi
done
[ -z "$PY" ] && { echo "ERROR: Python 3.10+ is required."; exit 1; }
echo "Using $($PY --version)"

[ -d env ] || "$PY" -m venv env
./env/bin/pip install -q --upgrade pip
./env/bin/pip install -q -r requirements.txt
./env/bin/python -m playwright install chromium || echo "WARN: Playwright browser install failed (only needed for './lf capture')."

[ -f .env ] || { cp .env.example .env; echo "Created .env from .env.example (all keys optional)."; }
chmod +x lf
mkdir -p data output

if command -v docker >/dev/null && docker info >/dev/null 2>&1; then
  docker start openserp >/dev/null 2>&1 || \
    docker run -d --init --name openserp -p 127.0.0.1:7000:7000 karust/openserp serve -a 0.0.0.0 -p 7000 >/dev/null \
    && echo "OpenSERP running on 127.0.0.1:7000"
else
  echo "WARN: Docker not available - free OpenSERP search disabled. Add TINYFISH_API_KEY or SERPER_API_KEY to .env instead."
fi

command -v claude >/dev/null || echo "WARN: 'claude' CLI not on PATH - the web UI needs Claude Code installed and logged in."

./env/bin/python tests/test_core.py >/dev/null && echo "Tests OK" || echo "WARN: core tests failed."

echo "Starting web UI at http://127.0.0.1:8787"
exec ./lf ui "$@"
