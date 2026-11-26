#!/usr/bin/env bash
set -euo pipefail

export YTDIGEST_HOME="${YTDIGEST_HOME:-/data}"
mkdir -p "${YTDIGEST_HOME}/state" "${YTDIGEST_HOME}/logs" "${YTDIGEST_HOME}/work"

# 有参数时按参数执行（例如 docker compose run --rm digest python run.py --dry-run）
if [ "$#" -gt 0 ]; then
    exec "$@"
fi

exec python /app/scheduler.py
