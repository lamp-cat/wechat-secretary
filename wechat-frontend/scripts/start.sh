#!/bin/bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PATH="/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
cd "$ROOT"
HOST="$( "$ROOT/.venv/bin/python" - <<'PY'
import json, pathlib
p = pathlib.Path.home() / ".config/wechat-frontend/config.json"
cfg = json.loads(p.read_text()) if p.is_file() else {}
print(cfg.get("bind_host", "127.0.0.1"))
PY
)"
PORT="$( "$ROOT/.venv/bin/python" - <<'PY'
import json, pathlib
p = pathlib.Path.home() / ".config/wechat-frontend/config.json"
cfg = json.loads(p.read_text()) if p.is_file() else {}
print(cfg.get("bind_port", 8088))
PY
)"
exec "$ROOT/.venv/bin/uvicorn" app.main:app --host "$HOST" --port "$PORT" --log-level info
