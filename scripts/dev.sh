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

PYTHON_BIN="${PYTHON:-}"
if [[ -z "${PYTHON_BIN}" ]]; then
  if command -v python >/dev/null 2>&1; then
    PYTHON_BIN=python
  else
    PYTHON_BIN=python3
  fi
fi
if ! command -v "${PYTHON_BIN}" >/dev/null 2>&1; then
  echo "error: python not found. Activate a Python >= 3.14 env first, e.g. conda activate dev2609"
  exit 1
fi
if ! "${PYTHON_BIN}" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 14) else 1)'; then
  PY_VER="$("${PYTHON_BIN}" -c 'import sys; print("%d.%d" % (sys.version_info.major, sys.version_info.minor))')"
  echo "error: Python >= 3.14 required (got ${PY_VER} from ${PYTHON_BIN}). conda activate dev2609"
  exit 1
fi

echo "======================================================="
echo " file-service 本地 lite"
echo "  DEPLOY_MODE=${DEPLOY_MODE}"
echo "  DATABASE_URL=${DATABASE_URL}"
echo "  STORAGE_BACKEND=${STORAGE_BACKEND}"
echo "  LOCAL_STORAGE_PATH=${LOCAL_STORAGE_PATH}"
echo "  CONFIG_PATH=${CONFIG_PATH}"
echo "  python=${PYTHON_BIN} ($("${PYTHON_BIN}" -c 'import sys; print(sys.version.split()[0])'))"
echo "  listen=http://${HOST}:${PORT}"
echo "======================================================="

"${PYTHON_BIN}" -c "from app.bootstrap import ensure_runtime_dirs, ensure_schema; ensure_runtime_dirs(); ensure_schema()"

# 避免与已占用的 8020  silently 冲突
if command -v lsof >/dev/null 2>&1; then
  if lsof -nP -iTCP:"${PORT}" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "⚠️  端口 ${PORT} 已被占用："
    lsof -nP -iTCP:"${PORT}" -sTCP:LISTEN || true
    echo "   可先：pkill -f 'uvicorn app.main:app'  或  PORT=8021 ./scripts/dev.sh"
    exit 1
  fi
fi

UVICORN_ARGS=("${PYTHON_BIN}" -m uvicorn app.main:app --host "${HOST}" --port "${PORT}")
if [[ "${RELOAD}" == "1" ]]; then
  UVICORN_ARGS+=(--reload)
fi

exec "${UVICORN_ARGS[@]}"
