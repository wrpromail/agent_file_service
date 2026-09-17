#!/usr/bin/env bash
# 本地 lite 启动：SQLite + 本地盘，单进程 API（含后台解析 / GC）
# 用法：./scripts/dev.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8020}"
RELOAD="${RELOAD:-1}"

if [[ ! -f .env ]]; then
  echo "ℹ️  无 .env，从 .env.example 复制（lite 默认）"
  cp .env.example .env
fi

# 强制本机开发走 lite（即使 shell 里残留了 full 的 DATABASE_URL / S3 等）
export DEPLOY_MODE="${DEPLOY_MODE:-lite}"
export DATABASE_URL="${DATABASE_URL:-sqlite:///./data/db/file_service.db}"
export STORAGE_BACKEND="${STORAGE_BACKEND:-local}"
export LOCAL_STORAGE_PATH="${LOCAL_STORAGE_PATH:-./data}"

export CONFIG_PATH="${CONFIG_PATH:-${ROOT}/config.yaml}"

echo "======================================================="
echo " file-service 本地 lite"
echo "  DEPLOY_MODE=${DEPLOY_MODE}"
echo "  DATABASE_URL=${DATABASE_URL}"
echo "  STORAGE_BACKEND=${STORAGE_BACKEND}"
echo "  LOCAL_STORAGE_PATH=${LOCAL_STORAGE_PATH}"
echo "  CONFIG_PATH=${CONFIG_PATH}"
echo "  listen=http://${HOST}:${PORT}"
echo "======================================================="

if [[ -d .venv ]]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

python -c "from app.bootstrap import ensure_runtime_dirs, ensure_schema; ensure_runtime_dirs(); ensure_schema()"

# 避免与已占用的 8020  silently 冲突
if command -v lsof >/dev/null 2>&1; then
  if lsof -nP -iTCP:"${PORT}" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "⚠️  端口 ${PORT} 已被占用："
    lsof -nP -iTCP:"${PORT}" -sTCP:LISTEN || true
    echo "   可先：pkill -f 'uvicorn app.main:app'  或  PORT=8021 ./scripts/dev.sh"
    exit 1
  fi
fi

UVICORN_ARGS=(uvicorn app.main:app --host "${HOST}" --port "${PORT}")
if [[ "${RELOAD}" == "1" ]]; then
  UVICORN_ARGS+=(--reload)
fi

exec "${UVICORN_ARGS[@]}"
