#!/usr/bin/env bash
# On-device only. Download/choose weights separately; never touches an existing Kev/Gemma server.
set -euo pipefail
cd "$(dirname "$0")/../.."
umask 077
: "${LLAMA_SERVER:?set exact verified llama-server path}"
: "${GEMMA_MODEL_FILE:?set durable verified Q8_0 GGUF path}"
PORT=${PRIVACY_PORT:-18081}
THREADS=${PRIVACY_THREADS:-4}
CFG=config/questions-weekly.json
uv run --project pipeline python - "$GEMMA_MODEL_FILE" "$CFG" "$PORT" <<'PY'
import hashlib, json, pathlib, socket, sys
model, config, port = pathlib.Path(sys.argv[1]), json.load(open(sys.argv[2])), int(sys.argv[3])
sha = hashlib.sha256()
with model.open("rb") as stream:
    for data in iter(lambda: stream.read(1024 * 1024), b""):
        sha.update(data)
if sha.hexdigest() != config["redaction_sha256"]:
    sys.exit("Wrong Gemma weights: no substitutions allowed")
with socket.socket() as probe:
    probe.bind(("127.0.0.1", port))
print("Gemma Q8_0 hash and dedicated loopback port verified")
PY
MODEL=$(uv run --project pipeline python -c 'import json; print(json.load(open("config/questions-weekly.json"))["redaction_model"])')
KEY_ARGS=()
if [ -n "${PRIVACY_API_KEY_FILE:-}" ]; then
    KEY_ARGS=(--api-key-file "$PRIVACY_API_KEY_FILE")
fi
SERVER_DIR=$(mktemp -d "${TMPDIR:-/tmp}/questions-owned-server.XXXXXX")
PID=""
cleanup() {
    if [ -n "$PID" ]; then
        kill "$PID" 2>/dev/null || true
        wait "$PID" 2>/dev/null || true
    fi
    # Private log remains at this exact path for local troubleshooting; never upload it.
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
"$LLAMA_SERVER" -m "$GEMMA_MODEL_FILE" --alias "$MODEL" --host 127.0.0.1 --port "$PORT" \
    -c 16384 -np 1 -t "$THREADS" -fa on --jinja --cache-ram 0 --offline --no-webui \
    --log-disable "${KEY_ARGS[@]}" > "$SERVER_DIR/server.log" 2>&1 &
PID=$!
ready=0
for _ in $(seq 1 120); do
    kill -0 "$PID" 2>/dev/null || { echo "Owned Gemma server exited; no private log printed" >&2; exit 1; }
    if uv run --project pipeline python - "$PORT" "${PRIVACY_API_KEY_FILE:-}" <<'PY'
import httpx, sys
from fminer.questions_privacy import private_headers
try:
    with httpx.Client(timeout=3, trust_env=False, headers=private_headers(sys.argv[2] or None)) as client:
        response = client.get("http://127.0.0.1:" + sys.argv[1] + "/health")
        sys.exit(0 if response.status_code == 200 else 1)
except (httpx.HTTPError, ValueError, OSError):
    sys.exit(1)
PY
    then ready=1; break; fi
    sleep 5
done
[ "$ready" = 1 ] || { echo "Owned Gemma readiness timed out" >&2; exit 1; }
uv run --project pipeline python scripts/questions_local_privacy.py "$@" \
    --model-file "$GEMMA_MODEL_FILE" --llama-server "$LLAMA_SERVER" --server-pid "$PID" \
    --base-url "http://127.0.0.1:$PORT/v1" "${KEY_ARGS[@]}"
