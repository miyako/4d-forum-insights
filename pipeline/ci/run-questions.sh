#!/usr/bin/env bash
# Private English Questions pilot. Own servers only; no publishing or shared DB pushes.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p logs

SERVER=${LLAMA_SERVER:-llama-server}
DECISION_MODEL=${DECISION_MODEL:-ggml-org/Kev-4B-GGUF:Q8_0}
GEN_MODEL=${GEN_MODEL:-ggml-org/gemma-4-E4B-it-GGUF:Q8_0}
WORKERS=${WORKERS:-1}
THREADS=${THREADS:-$(sysctl -n hw.ncpu 2>/dev/null || nproc)}
DECISION_PORT=${DECISION_PORT:-18080}
GEN_PORT=${GEN_PORT:-18081}
DECISION_ARGS=${DECISION_ARGS:--c 16384 -fa on}
GEN_ARGS=${GEN_ARGS:---spec-type draft-mtp --no-mmproj -c 32768 -fa on --jinja --cache-ram 0}

command -v "$SERVER" >/dev/null || { echo "Missing llama-server: set LLAMA_SERVER to its path" >&2; exit 1; }
pid=""
cleanup() {
    if [ -n "$pid" ]; then
        kill "$pid" 2>/dev/null || true
        wait "$pid" 2>/dev/null || true
        pid=""
    fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

start_server() {
    local model=$1 port=$2 args=$3 logfile=$4
    if curl -s -m 2 "http://127.0.0.1:$port/health" >/dev/null; then
        echo "Port $port is occupied; select a different pilot port" >&2
        exit 1
    fi
    # shellcheck disable=SC2086
    "$SERVER" -hf "$model" $args --host 127.0.0.1 --port "$port" -np "$WORKERS" -t "$THREADS" \
        > "$logfile" 2>&1 &
    pid=$!
    for _ in $(seq 1 720); do
        if ! kill -0 "$pid" 2>/dev/null; then
            tail -50 "$logfile" >&2
            exit 1
        fi
        if curl -s -m 3 "http://127.0.0.1:$port/health" | grep -q ok; then
            return
        fi
        sleep 5
    done
    echo "Pilot server on $port did not become ready" >&2
    exit 1
}

if [ "${SKIP_CLASSIFY:-}" != 1 ]; then
    echo "== Questions decision pilot"
    start_server "$DECISION_MODEL" "$DECISION_PORT" "$DECISION_ARGS" logs/questions-decision-server.log
    uv run -q fminer questions-classify --base-url "http://127.0.0.1:$DECISION_PORT" \
        --sample "${SAMPLE:-30}" --seed "${SEED:-7}" --workers "$WORKERS" \
        ${CLASSIFY_BUDGET:+--budget-minutes "$CLASSIFY_BUDGET"}
    cleanup
fi
if [ "${SKIP_WRITE:-}" != 1 ]; then
    echo "== Questions English write-ups"
    start_server "$GEN_MODEL" "$GEN_PORT" "$GEN_ARGS" logs/questions-gen-server.log
    uv run -q fminer questions-write --base-url "http://127.0.0.1:$GEN_PORT/v1" --workers "$WORKERS" \
        ${WRITE_BUDGET:+--budget-minutes "$WRITE_BUDGET"}
    cleanup
fi
uv run -q fminer questions-stats
