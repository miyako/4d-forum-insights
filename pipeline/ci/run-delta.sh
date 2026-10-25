#!/usr/bin/env bash
# End-to-end delta used by .github/workflows/delta.yml (also runs locally on a Mac).
#   fetch delta (no LLM) → enrich (generative model) → enrich --resummarise-empty (same model:
#   backfill missing/short summaries, upstream 8381102) → classify (decision model)
# All model steps are time-boxed and resumable: whatever does not fit is picked up by the next run.
# enrich and the backfill share ENRICH_BUDGET.
#
# Env: FMINER_DB (working DB), LLAMA_SERVER (llama-server binary), SKIP_MODELS=1, FULL_LIST=1,
#      SWEEP_WINDOW, SWEEP_RECENT_DAYS, ENRICH_BUDGET / CLASSIFY_BUDGET (minutes), WORKERS, THREADS,
#      GEN_MODEL, DECISION_MODEL, GEN_ARGS, DECISION_ARGS, FREE_MODEL_CACHE=1 (delete a model after use),
#      GEN_PORT / DECISION_PORT (default 18081 / 18080, away from a local upstream pipeline on 8081 / 8080)
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p logs

SERVER=${LLAMA_SERVER:-llama-server}
GEN_MODEL=${GEN_MODEL:-ggml-org/gemma-4-E4B-it-GGUF:Q8_0}
DECISION_MODEL=${DECISION_MODEL:-ggml-org/Kev-4B-GGUF:Q8_0}
WORKERS=${WORKERS:-2}
THREADS=${THREADS:-$(nproc 2>/dev/null || sysctl -n hw.ncpu)}
GEN_ARGS=${GEN_ARGS:---spec-type draft-mtp --no-mmproj -c 16384 -fa on --jinja --cache-ram 0}
DECISION_ARGS=${DECISION_ARGS:--c 16384 -fa on}
GEN_PORT=${GEN_PORT:-18081}
DECISION_PORT=${DECISION_PORT:-18080}

fminer() { uv run -q fminer "$@"; }

wait_ready() {  # model download + load can take a while on a fresh runner
    for _ in $(seq 1 720); do
        curl -s -m 3 "http://127.0.0.1:$1/health" | grep -q ok && return 0
        kill -0 "$2" 2>/dev/null || { echo "llama-server exited"; tail -50 "$3"; return 1; }
        sleep 5
    done
    echo "llama-server on :$1 not ready"; return 1
}

stop_server() { kill "$1" 2>/dev/null || true; wait "$1" 2>/dev/null || true; }

free_model() {  # runners have ~14 GB of free disk; the two Q8 models together need ~12 GB
    [ "${FREE_MODEL_CACHE:-}" = 1 ] || return 0
    local repo=${1%%:*}
    rm -rf ~/.cache/huggingface/hub/models--${repo//\//--} ~/.cache/llama.cpp/${repo//\//_}* || true
}

pending() {  # prints: <topics to enrich> <summaries to backfill> <topics to classify with DECISION_MODEL>
    uv run -q python - "$DECISION_MODEL" <<'EOF'
import sys
from fminer import db
from fminer.classify import PROMPT_VERSION
from fminer.enrich import MIN_LINE, MIN_PARAGRAPH
con = db.connect()
q = lambda sql, *a: con.execute(sql, a).fetchone()[0]
enrich = q("""SELECT COUNT(*) FROM topics WHERE first_post_raw IS NOT NULL AND
              (enriched_at IS NULL OR enriched_at < COALESCE(raw_changed_at, ''))""")
classify = q("""SELECT COUNT(*) FROM topics t WHERE t.enriched_at IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM classifications c WHERE c.topic_id=t.id AND c.model=? AND c.prompt_version=?
                AND c.created_at >= t.enriched_at)""", sys.argv[1], PROMPT_VERSION)
resummarise = q("""SELECT COUNT(*) FROM topics WHERE enriched_at IS NOT NULL AND
                   (length(trim(COALESCE(summary_paragraph, ''))) < ? OR length(trim(COALESCE(summary_line, ''))) < ?)""",
                MIN_PARAGRAPH, MIN_LINE)
print(enrich, resummarise, classify)
EOF
}

echo "::group::delta fetch"
fminer delta ${FULL_LIST:+--full-list} --sweep-window "${SWEEP_WINDOW:-300}" --sweep-recent-days "${SWEEP_RECENT_DAYS:-45}"
echo "::endgroup::"

if [ "${SKIP_MODELS:-}" = 1 ]; then
    echo "SKIP_MODELS=1: not running the models"
else
    read -r to_enrich to_resummarise to_classify < <(pending)
    echo "pending: enrich=$to_enrich resummarise=$to_resummarise classify=$to_classify"
    budget=${ENRICH_BUDGET:-180}
    if [ "$to_enrich" -gt 0 ] || [ "$to_resummarise" -gt 0 ]; then
        echo "::group::enrich ($to_enrich topics) + summary backfill ($to_resummarise topics), budget $budget min"
        start=$SECONDS
        # shellcheck disable=SC2086
        $SERVER -hf "$GEN_MODEL" $GEN_ARGS --host 127.0.0.1 --port "$GEN_PORT" -np "$WORKERS" -t "$THREADS" > logs/gen-server.log 2>&1 &
        pid=$!
        wait_ready "$GEN_PORT" $pid logs/gen-server.log
        gen="--base-url http://127.0.0.1:$GEN_PORT/v1 --workers $WORKERS"
        if [ "$to_enrich" -gt 0 ]; then
            # shellcheck disable=SC2086
            fminer enrich $gen --stale --budget-minutes "$budget" || status=$?
        fi
        left=$(( budget - (SECONDS - start) / 60 ))
        if [ -z "${status:-}" ]; then
            if [ "$left" -ge 5 ]; then
                # shellcheck disable=SC2086
                fminer enrich $gen --resummarise-empty --budget-minutes "$left" || status=$?
            else
                echo "summary backfill skipped: only $left min of the enrich budget left"
            fi
        fi
        stop_server $pid
        free_model "$GEN_MODEL"
        [ -z "${status:-}" ] || exit "$status"
        echo "::endgroup::"
        read -r to_enrich to_resummarise to_classify < <(pending)
    fi
    if [ "$to_classify" -gt 0 ]; then
        echo "::group::classify ($to_classify topics, budget ${CLASSIFY_BUDGET:-90} min)"
        # shellcheck disable=SC2086
        $SERVER -hf "$DECISION_MODEL" $DECISION_ARGS --host 127.0.0.1 --port "$DECISION_PORT" -np "$WORKERS" -t "$THREADS" > logs/decision-server.log 2>&1 &
        pid=$!
        wait_ready "$DECISION_PORT" $pid logs/decision-server.log
        fminer classify --base-url "http://127.0.0.1:$DECISION_PORT" --stale --workers "$WORKERS" --budget-minutes "${CLASSIFY_BUDGET:-90}" || status=$?
        stop_server $pid
        free_model "$DECISION_MODEL"
        [ -z "${status:-}" ] || exit "$status"
        echo "::endgroup::"
    fi
fi
fminer stats
