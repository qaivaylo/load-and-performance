#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="${ROOT_DIR}/docker-compose.yml"

if ! command -v docker >/dev/null 2>&1; then
  echo "[start_monitoring_stack] ERROR: docker command not available on runner" >&2
  exit 1
fi

if docker compose version >/dev/null 2>&1; then
  COMPOSE_CMD=(docker compose -f "${COMPOSE_FILE}")
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE_CMD=(docker-compose -f "${COMPOSE_FILE}")
else
  echo "[start_monitoring_stack] ERROR: docker compose plugin or docker-compose binary not found" >&2
  exit 1
fi

"${COMPOSE_CMD[@]}" up -d prometheus grafana
