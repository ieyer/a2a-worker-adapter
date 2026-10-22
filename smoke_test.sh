#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

# The smoke test only ever talks to 127.0.0.1. Ignore ambient proxy
# configuration: some environments export no_proxy entries (e.g. bare
# IPv6 literals) that HTTP client libraries fail to parse.
export no_proxy="127.0.0.1,localhost"
export NO_PROXY="127.0.0.1,localhost"
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY all_proxy

PYTHON="${PYTHON:-.venv/bin/python}"
PORT="${A2A_DEMO_PORT:-8765}"
BASE_URL="http://127.0.0.1:${PORT}"
TOKEN="${A2A_SMOKE_TOKEN:-smoke-secret}"
SERVER_LOG="$(mktemp)"
CLIENT_OUTPUT="$(mktemp)"
TMP_DATA="$(mktemp -d)"

A2A_WORKER_ID="worker-a" \
A2A_AGENT_NAME="worker-a A2A Runtime Adapter" \
A2A_BEARER_TOKEN="$TOKEN" \
A2A_IDEMPOTENCY_DB="$TMP_DATA/idempotency.sqlite3" \
WORKER_WAKE_MODE=command \
WORKER_WAKE_COMMAND="$PYTHON tests/fake_runtime.py" \
"$PYTHON" server.py --host 127.0.0.1 --port "$PORT" --agent-url "$BASE_URL/" >"$SERVER_LOG" 2>&1 &
SERVER_PID=$!
cleanup() {
  kill "$SERVER_PID" 2>/dev/null || true
  wait "$SERVER_PID" 2>/dev/null || true
  rm -f "$SERVER_LOG" "$CLIENT_OUTPUT"
  rm -rf "$TMP_DATA"
}
trap cleanup EXIT

ready=0
for _ in $(seq 1 50); do
  if curl --silent --show-error --fail "$BASE_URL/healthz" -o /dev/null 2>/dev/null; then
    ready=1
    break
  fi
  sleep 0.2
done
if [[ "$ready" != 1 ]]; then
  echo "Server did not become ready at $BASE_URL" >&2
  cat "$SERVER_LOG" >&2
  exit 1
fi

curl --silent --show-error --fail "$BASE_URL/.well-known/agent-card.json" | "$PYTHON" -c '
import json, sys
card=json.load(sys.stdin)
assert card["name"] == "worker-a A2A Runtime Adapter", card
iface=card["supportedInterfaces"][0]
assert iface["protocolBinding"] == "JSONRPC", iface
assert iface["protocolVersion"] == "1.0", iface
assert card["skills"][0]["id"] == "dispatch_assigned_task", card
assert "bearerAuth" in card["securitySchemes"], card
assert card["securityRequirements"], card
print("Agent Card + bearer declaration OK")
'

status="$(curl --silent -o /dev/null -w '%{http_code}' -H 'A2A-Version: 1.0' -H 'Content-Type: application/json' -d '{"jsonrpc":"2.0","id":"auth-check","method":"SendMessage","params":{}}' "$BASE_URL/")"
if [[ "$status" != 401 ]]; then
  echo "Expected unauthenticated JSON-RPC to return 401, got $status" >&2
  exit 1
fi

A2A_BEARER_TOKEN="$TOKEN" A2A_WORKER_ID="worker-a" "$PYTHON" client.py \
  --url "$BASE_URL/" \
  --task-id "T-A2A-SMOKE" \
  --worker-id "worker-a" \
  --message "smoke ping" >"$CLIENT_OUTPUT"

"$PYTHON" - "$CLIENT_OUTPUT" <<'PY'
import json
import sys
from pathlib import Path

events=[json.loads(line) for line in Path(sys.argv[1]).read_text().splitlines() if line.strip()]
assert any(e.get("kind") == "agent_card" for e in events), events
assert any("FAKE_RUNTIME:smoke ping" in json.dumps(e) for e in events), events
assert any("TASK_STATE_COMPLETED" in json.dumps(e) for e in events), events
print("Authenticated A2A adapter round-trip OK")
PY
