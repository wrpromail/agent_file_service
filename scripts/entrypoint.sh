#!/bin/sh
set -e

# SERVICE_ROLE=api|worker (set by image target; overrideable)
ROLE="${SERVICE_ROLE:-api}"

# Shared volume prep (SQLite parent + local object store root)
python -c "from app.bootstrap import ensure_runtime_dirs; ensure_runtime_dirs()"

# Only API creates missing tables — avoids dual-writer races on SQLite at startup.
# Worker depends_on api healthy in compose so schema is ready first.
# Schema is still evolving; create_all does not alter existing columns.
if [ "$ROLE" = "api" ]; then
  python -c "from app.bootstrap import ensure_schema; ensure_schema()"

  # Optional: inject browser console Bearer/tenant at start (do not bake secrets into image layers).
  # Set DEV_CONSOLE_API_TOKEN / DEV_CONSOLE_TENANT_ID when DEV_CONSOLE_ENABLED=true.
  if [ -n "${DEV_CONSOLE_API_TOKEN:-}" ] || [ -n "${DEV_CONSOLE_TENANT_ID:-}" ]; then
    python - <<'PY'
import json
import os
import re
from pathlib import Path

path = Path("/app/web/shared/config.js")
if not path.is_file():
    raise SystemExit(0)
text = path.read_text(encoding="utf-8")
token = os.environ.get("DEV_CONSOLE_API_TOKEN")
tenant = os.environ.get("DEV_CONSOLE_TENANT_ID")
if token:
    text, n = re.subn(
        r'apiToken:\s*"(?:\\.|[^"\\])*"',
        f"apiToken: {json.dumps(token)}",
        text,
        count=1,
    )
    if n != 1:
        raise SystemExit("entrypoint: failed to inject apiToken into config.js")
if tenant:
    text, n = re.subn(
        r'tenantId:\s*"(?:\\.|[^"\\])*"',
        f"tenantId: {json.dumps(tenant)}",
        text,
        count=1,
    )
    if n != 1:
        raise SystemExit("entrypoint: failed to inject tenantId into config.js")
try:
    path.write_text(text, encoding="utf-8")
except OSError as exc:
    print(f"WARNING: could not write {path} ({exc}); mount may be read-only")
else:
    print("dev console config.js injected from DEV_CONSOLE_* env")
PY
  fi
fi

exec "$@"
