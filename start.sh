#!/bin/sh
set -e

export PLAYWRIGHT_BROWSERS_PATH="${PLAYWRIGHT_BROWSERS_PATH:-/app/.playwright}"
export ML_OUT_DIR="${ML_OUT_DIR:-/app/data}"

mkdir -p "$ML_OUT_DIR"

exec uvicorn app.main:app \
    --host 0.0.0.0 \
    --port "${PORT:-8000}" \
    --workers 1 \
    --timeout-keep-alive 30 \
    --log-level info \
    --no-access-log