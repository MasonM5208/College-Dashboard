#!/bin/sh
# Container entrypoint: migrate first, then serve.
#
# `set -e` is what makes this safe. If the migration step exits non-zero the shell
# stops here and the container dies with the migration error as the last thing in
# its log, instead of starting a web server on top of a schema that is wrong.
set -e

echo "[entrypoint] Applying any pending database migrations ..."
python -m app.migrate

echo "[entrypoint] Starting the web server on port 8000 ..."

# --host 0.0.0.0 is the CONTAINER's network namespace, not the host's, and is not
# the public binding that SPEC §4 forbids. The host only ever opens the tailnet
# address, because that is what the `ports:` line in docker-compose.yml publishes.
# Confirm it on the VPS with `ss -ltnp` — see docs/SETUP.md.
exec uvicorn app.main:app \
  --host 0.0.0.0 \
  --port 8000 \
  --workers 1 \
  --log-level info \
  --no-server-header
