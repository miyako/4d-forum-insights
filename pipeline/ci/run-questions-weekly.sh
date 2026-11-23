#!/usr/bin/env bash
# Private bounded delta; public publication is a separate reviewed gate.
set -euo pipefail
cd "$(dirname "$0")/../.."
umask 077
: "${DATA_PUBLICATION_CHILD:?run inside publish_data.py with-lock}"
: "${RUNNER_TEMP:?CI requires independent temporary backup storage}"
ROOT=build/questions-checkpoint
SOURCE=build/questions-source.db
CFG=config/questions-weekly.json
PID=""
status=0
restored=0
source_ready=0
stop_server() {
    if [ -n "$PID" ]; then
        kill "$PID" 2>/dev/null || true
        wait "$PID" 2>/dev/null || true
        PID=""
    fi
}
save() {
    status=$?
    trap - EXIT
    stop_server
    # Preserve partial results on model/translation failure; persistence failure must fail the run.
    if [ "$restored" = 1 ]; then
        uv run --project pipeline python scripts/questions_weekly.py status --source "$SOURCE" --root "$ROOT" || status=1
        python3 scripts/questions_weekly_store.py push "$ROOT" || status=1
    fi
    if [ "$source_ready" = 1 ]; then
        scripts/db-store.sh push "$SOURCE" || status=1
    fi
    exit "$status"
}
trap save EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

python3 scripts/questions_weekly_store.py pull "$ROOT"
uv run --project pipeline python scripts/questions_weekly.py readiness --root "$ROOT"
restored=1
scripts/db-store.sh pull "$SOURCE"
source_ready=1

# SQLite online backup includes committed WAL content; checksums/integrity are verified before inference.
BACKUP="$RUNNER_TEMP/questions-before-$GITHUB_RUN_ID-$GITHUB_RUN_ATTEMPT"
uv run --project pipeline python scripts/questions_weekly.py backup --source "$SOURCE" --out "$BACKUP/source.db"
uv run --project pipeline python scripts/questions_weekly.py backup --source "$ROOT/ratings.db" --out "$BACKUP/ratings.db"
uv run --project pipeline python scripts/questions_weekly.py backup --source "$ROOT/display.db" --out "$BACKUP/display.db"

export FMINER_DB="$PWD/$SOURCE"
mkdir -p build/questions-private-logs
uv run --project pipeline python scripts/questions_weekly.py capture-delta --source "$SOURCE" --root "$ROOT"
(cd pipeline && timeout 15m uv run -q fminer delta --sweep-window 300 --sweep-recent-days 45) \
    > build/questions-private-logs/delta.log 2>&1 || {
        code=$?; echo "::error::Forum delta failed; private diagnostics remain local"; exit "$code";
    }

mkdir -p build/questions-models
tag=$(jq -r .llama_tag "$CFG")
archive="llama-$tag-bin-ubuntu-x64.tar.gz"
timeout 5m gh release download "$tag" -R ggml-org/llama.cpp --pattern "$archive" --dir build/questions-models
printf '%s  %s\n' "$(jq -r .llama_archive_sha256 "$CFG")" "build/questions-models/$archive" | sha256sum -c -
mkdir build/questions-models/llama
tar -xzf "build/questions-models/$archive" -C build/questions-models/llama --strip-components=1
SERVER="$PWD/build/questions-models/llama/llama-server"
export LD_LIBRARY_PATH="$PWD/build/questions-models/llama:${LD_LIBRARY_PATH:-}"

start_server() {
    local role=$1 port=$2
    local repo rev file sha alias
    repo=$(jq -r ".${role}_repo" "$CFG")
    rev=$(jq -r ".${role}_hf_revision" "$CFG")
    file=$(jq -r ".${role}_file" "$CFG")
    sha=$(jq -r ".${role}_sha256" "$CFG")
    alias=$(jq -r ".${role}_model" "$CFG")
    if curl -fsS --max-time 2 "http://127.0.0.1:$port/health" >/dev/null 2>&1; then
        echo "::error::Questions model port already occupied"; exit 1
    fi
    timeout 15m curl -fsSL --retry 2 --connect-timeout 30 --max-time 600 \
        "https://huggingface.co/$repo/resolve/$rev/$file" -o "build/questions-models/$role.gguf"
    printf '%s  %s\n' "$sha" "build/questions-models/$role.gguf" | sha256sum -c -
    "$SERVER" -m "build/questions-models/$role.gguf" --alias "$alias" \
        --host 127.0.0.1 --port "$port" -c 16384 -np 1 -t 4 -fa on --jinja \
        --cache-ram 0 --offline --no-webui --log-disable > "build/questions-models/$role.log" 2>&1 &
    PID=$!
    for _ in $(seq 1 120); do
        kill -0 "$PID" 2>/dev/null || { echo "::error::Local llama-server exited (private log retained locally)"; exit 1; }
        if curl -fsS --max-time 3 "http://127.0.0.1:$port/health" | grep -q ok; then return; fi
        sleep 5
    done
    echo "::error::Local llama-server readiness timeout"; exit 1
}

# Only changed/new raw source text plus resumable delta IDs, never pre-existing corpus backlog.
start_server redaction 18081
timeout 25m uv run --project pipeline python scripts/questions_weekly.py enrich-delta \
    --source "$SOURCE" --root "$ROOT" --base-url http://127.0.0.1:18081/v1 --limit 100 --minutes 20 \
    > build/questions-private-logs/enrich.log 2>&1 || {
        code=$?; echo "::error::Local delta enrichment failed; encrypted pending IDs retained"; exit "$code";
    }
stop_server
rm -f build/questions-models/redaction.gguf

uv run --project pipeline python scripts/questions_weekly.py prepare --source "$SOURCE" --root "$ROOT"
start_server decision 18080
timeout 55m bash -c 'cd pipeline && uv run -q python ../scripts/questions_corpus.py classify \
    --source "../$1" --results "../$2/ratings.db" --model "$3" --model-revision "$4" \
    --base-url http://127.0.0.1:18080 --limit 100 --budget-minutes 45' _ \
    "$SOURCE" "$ROOT" "$(jq -r .decision_model "$CFG")" "$(jq -r .decision_revision "$CFG")"
stop_server
rm -f build/questions-models/decision.gguf

(cd pipeline && timeout 35m uv run -q python ../scripts/questions_weekly.py propose \
    --config "../$CFG" --root "../$ROOT" --source "../$SOURCE" --limit 100 --minutes 30)

timeout 35m uv run --project pipeline python scripts/questions_weekly_review.py english \
    --root "$ROOT" --source "$SOURCE" --limit 100 --minutes 30
uv run --project pipeline python scripts/questions_weekly.py export --root "$ROOT" --source "$SOURCE" --out "$ROOT/embedding"
node scripts/questions_weekly_translate.mjs --data "$ROOT/embedding" --store "$ROOT/locales" \
    --entries 200 --minutes 15 --batch-size 10 --workers 1 --timeout 120 --retries 2
# Existing local ONNX/CPU embedding implementation and model+text cache identity, not a hosted API.
if [ -s "$ROOT/embedding/embed-input.jsonl" ]; then
    EMBED_THREADS=4 timeout 15m node scripts/build_embeddings.mjs \
        --input "$ROOT/embedding/embed-input.jsonl" --out "$ROOT/embedding"
fi
timeout 65m uv run --project pipeline python scripts/questions_weekly_review.py translations \
        --root "$ROOT" --source "$SOURCE" --limit 100 --minutes 30
uv run --project pipeline python scripts/questions_weekly.py candidate --root "$ROOT" --source "$SOURCE"
