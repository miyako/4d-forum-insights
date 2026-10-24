#!/usr/bin/env bash
# Knowledge base ("tips"): classify area + Q&A type with the decision model, then write the entries with the
# generative model. Runs ONE llama-server at a time (16 GB machines cannot hold both models) and is resumable:
# rerun it to continue, or after a delta to pick up new and changed topics (--stale).
#
# Env: FMINER_DB (DB to update), LLAMA_SERVER, SAMPLE (classify a random sample only), MIN_P (default 0),
#      CLASSIFY_BUDGET / WRITE_BUDGET (minutes, empty = no limit), WORKERS (default 4), THREADS,
#      GEN_MODEL, DECISION_MODEL, GEN_ARGS, DECISION_ARGS, GEN_PORT / DECISION_PORT (default 18081 / 18080),
#      SKIP_CLASSIFY=1, SKIP_WRITE=1
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p logs

SERVER=${LLAMA_SERVER:-llama-server}
GEN_MODEL=${GEN_MODEL:-ggml-org/gemma-4-E4B-it-GGUF:Q8_0}
DECISION_MODEL=${DECISION_MODEL:-ggml-org/Kev-4B-GGUF:Q8_0}
WORKERS=${WORKERS:-4}
THREADS=${THREADS:-$(nproc 2>/dev/null || sysctl -n hw.ncpu)}
GEN_ARGS=${GEN_ARGS:---spec-type draft-mtp --no-mmproj -c 32768 -fa on --jinja}
DECISION_ARGS=${DECISION_ARGS:--c 16384 -fa on}
GEN_PORT=${GEN_PORT:-18081}
DECISION_PORT=${DECISION_PORT:-18080}

fminer() { uv run -q fminer "$@"; }

wait_ready() {
    for _ in $(seq 1 720); do
        curl -s -m 3 "http://127.0.0.1:$1/health" | grep -q ok && return 0
        kill -0 "$2" 2>/dev/null || { echo "llama-server exited"; tail -50 "$3"; return 1; }
        sleep 5
    done
    echo "llama-server on :$1 not ready"; return 1
}

pid=""
cleanup() { [ -z "$pid" ] || { kill "$pid" 2>/dev/null || true; wait "$pid" 2>/dev/null || true; }; }
trap cleanup EXIT

if [ "${SKIP_CLASSIFY:-}" != 1 ]; then
    echo "== kb-classify with $DECISION_MODEL on :$DECISION_PORT"
    # shellcheck disable=SC2086
    $SERVER -hf "$DECISION_MODEL" $DECISION_ARGS --host 127.0.0.1 --port "$DECISION_PORT" -np "$WORKERS" -t "$THREADS" \
        > logs/kb-decision-server.log 2>&1 &
    pid=$!
    wait_ready "$DECISION_PORT" $pid logs/kb-decision-server.log
    fminer kb-classify --base-url "http://127.0.0.1:$DECISION_PORT" --workers "$WORKERS" --stale \
        ${SAMPLE:+--sample "$SAMPLE"} ${CLASSIFY_BUDGET:+--budget-minutes "$CLASSIFY_BUDGET"}
    cleanup; pid=""
fi

if [ "${SKIP_WRITE:-}" != 1 ]; then
    echo "== kb-write with $GEN_MODEL on :$GEN_PORT"
    # shellcheck disable=SC2086
    $SERVER -hf "$GEN_MODEL" $GEN_ARGS --host 127.0.0.1 --port "$GEN_PORT" -np "$WORKERS" -t "$THREADS" \
        > logs/kb-gen-server.log 2>&1 &
    pid=$!
    wait_ready "$GEN_PORT" $pid logs/kb-gen-server.log
    fminer kb-write --base-url "http://127.0.0.1:$GEN_PORT/v1" --workers "$WORKERS" --min-p "${MIN_P:-0}" \
        ${WRITE_BUDGET:+--budget-minutes "$WRITE_BUDGET"}
    cleanup; pid=""
fi

fminer kb-stats
