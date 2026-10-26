#!/usr/bin/env bash
# End-to-end delta used by .github/workflows/delta.yml (also runs locally on a Mac).
#   fetch delta (no LLM)
#   → gemma: enrich + enrich --resummarise-empty (backfill missing/short summaries, upstream 8381102)
#   → Kev:   classify (main site) → kb-classify (Tips) → voices-classify (Voices)
#   → gemma: kb-write (Tips entries) → voices-write (Voices write-ups)
# One llama-server at a time. Every model step is time-boxed and resumable: whatever does not fit is picked up by
# the next run. enrich and the backfill share ENRICH_BUDGET, kb-/voices-classify share SIDE_BUDGET, and
# kb-/voices-write share WRITE_BUDGET. DEADLINE caps the whole script, so later steps shrink or are skipped.
#
# Env: FMINER_DB (working DB), LLAMA_SERVER (llama-server binary), SKIP_MODELS=1, SKIP_SIDE=1 (no Tips/Voices),
#      FULL_LIST=1, SWEEP_WINDOW, SWEEP_RECENT_DAYS, ENRICH_BUDGET / CLASSIFY_BUDGET / SIDE_BUDGET / WRITE_BUDGET /
#      DEADLINE (minutes), WORKERS, THREADS, MIN_P (kb-write, default 0), GEN_MODEL, DECISION_MODEL, GEN_ARGS,
#      DECISION_ARGS, FREE_MODEL_CACHE=1 (delete a model after use),
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
DEADLINE=${DEADLINE:-}
MARGIN=10   # minutes kept back from DEADLINE for in-flight topics and stopping the server

fminer() { uv run -q fminer "$@"; }

wait_ready() {  # model download + load can take a while on a fresh runner
    for _ in $(seq 1 720); do
        curl -s -m 3 "http://127.0.0.1:$1/health" | grep -q ok && return 0
        kill -0 "$2" 2>/dev/null || { echo "llama-server exited"; tail -50 "$3"; return 1; }
        sleep 5
    done
    echo "llama-server on :$1 not ready"; return 1
}

pid=""
start_server() {  # <model> <args> <port> <log>
    # shellcheck disable=SC2086
    $SERVER -hf "$1" $2 --host 127.0.0.1 --port "$3" -np "$WORKERS" -t "$THREADS" > "$4" 2>&1 &
    pid=$!
    wait_ready "$3" $pid "$4"
}
stop_server() { [ -z "$pid" ] || { kill "$pid" 2>/dev/null || true; wait "$pid" 2>/dev/null || true; }; pid=""; }
trap stop_server EXIT

free_model() {  # runners have ~14 GB of free disk; the two Q8 models together need ~12 GB
    [ "${FREE_MODEL_CACHE:-}" = 1 ] || return 0
    local repo=${1%%:*}
    rm -rf ~/.cache/huggingface/hub/models--"${repo//\//--}" ~/.cache/llama.cpp/"${repo//\//_}"* || true
}

left() {  # minutes left of <budget> after DEADLINE (whole script, minus MARGIN) and an optional step start
    local b=$1 used=${2:-}
    [ -z "$used" ] || b=$(( b - (SECONDS - used) / 60 ))
    if [ -n "$DEADLINE" ]; then
        local d=$(( DEADLINE - MARGIN - SECONDS / 60 ))
        [ "$d" -ge "$b" ] || b=$d
    fi
    echo "$b"
}

pending() {  # prints: enrich resummarise classify kb_classify voices_classify kb_write voices_write
    uv run -q python - "$DECISION_MODEL" "${MIN_P:-0}" <<'EOF'
import sys
from fminer import db, kb, voices
from fminer.classify import PROMPT_VERSION
from fminer.enrich import MIN_LINE, MIN_PARAGRAPH
model, min_p = sys.argv[1], float(sys.argv[2])
con = db.connect()
con.executescript(kb.SCHEMA)
con.executescript(voices.SCHEMA)
q = lambda sql, *a: con.execute(sql, a).fetchone()[0]
enrich = q("""SELECT COUNT(*) FROM topics WHERE first_post_raw IS NOT NULL AND
              (enriched_at IS NULL OR enriched_at < COALESCE(raw_changed_at, ''))""")
classify = q("""SELECT COUNT(*) FROM topics t WHERE t.enriched_at IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM classifications c WHERE c.topic_id=t.id AND c.model=? AND c.prompt_version=?
                AND c.created_at >= t.enriched_at)""", model, PROMPT_VERSION)
resummarise = q("""SELECT COUNT(*) FROM topics WHERE enriched_at IS NOT NULL AND
                   (length(trim(COALESCE(summary_paragraph, ''))) < ? OR length(trim(COALESCE(summary_line, ''))) < ?)""",
                MIN_PARAGRAPH, MIN_LINE)
print(enrich, resummarise, classify, len(kb.classify_todo(con, model, stale=True)),
      len(voices.classify_todo(con, model, stale=True)), len(kb.write_todo(con, min_p)), len(voices.write_todo(con)))
EOF
}
refresh() {
    read -r to_enrich to_resummarise to_classify to_kb_classify to_voices_classify to_kb_write to_voices_write < <(pending)
    if [ "${SKIP_SIDE:-}" = 1 ]; then to_kb_classify=0 to_voices_classify=0 to_kb_write=0 to_voices_write=0; fi
    echo "pending: enrich=$to_enrich resummarise=$to_resummarise classify=$to_classify" \
         "kb-classify=$to_kb_classify voices-classify=$to_voices_classify kb-write=$to_kb_write voices-write=$to_voices_write"
}

echo "::group::delta fetch"
fminer delta ${FULL_LIST:+--full-list} --sweep-window "${SWEEP_WINDOW:-300}" --sweep-recent-days "${SWEEP_RECENT_DAYS:-45}"
echo "::endgroup::"

if [ "${SKIP_MODELS:-}" = 1 ]; then
    echo "SKIP_MODELS=1: not running the models"
    fminer stats
    exit 0
fi

status=""
refresh

# 1. gemma: enrich + summary backfill (shared ENRICH_BUDGET)
budget=$(left "${ENRICH_BUDGET:-180}")
if [ "$to_enrich" -gt 0 ] || [ "$to_resummarise" -gt 0 ]; then
    if [ "$budget" -lt 5 ]; then
        echo "enrich skipped: $budget min left"
    else
        echo "::group::enrich ($to_enrich topics) + summary backfill ($to_resummarise topics), budget $budget min"
        start=$SECONDS
        start_server "$GEN_MODEL" "$GEN_ARGS" "$GEN_PORT" logs/gen-server.log
        gen="--base-url http://127.0.0.1:$GEN_PORT/v1 --workers $WORKERS"
        if [ "$to_enrich" -gt 0 ]; then
            # shellcheck disable=SC2086
            fminer enrich $gen --stale --budget-minutes "$budget" || status=$?
        fi
        b=$(left "$budget" $start)
        if [ -z "$status" ] && [ "$to_resummarise" -gt 0 ]; then
            if [ "$b" -ge 5 ]; then
                # shellcheck disable=SC2086
                fminer enrich $gen --resummarise-empty --budget-minutes "$b" || status=$?
            else
                echo "summary backfill skipped: only $b min of the enrich budget left"
            fi
        fi
        stop_server
        free_model "$GEN_MODEL"
        [ -z "$status" ] || exit "$status"
        echo "::endgroup::"
        refresh
    fi
fi

# 2. Kev: classify (CLASSIFY_BUDGET), then kb-classify + voices-classify (shared SIDE_BUDGET)
if [ "$to_classify" -gt 0 ] || [ "$to_kb_classify" -gt 0 ] || [ "$to_voices_classify" -gt 0 ]; then
    if [ "$(left "${CLASSIFY_BUDGET:-90}")" -lt 5 ]; then
        echo "classify skipped: $(left "${CLASSIFY_BUDGET:-90}") min left"
    else
        echo "::group::decision model: classify $to_classify, kb-classify $to_kb_classify, voices-classify $to_voices_classify"
        start_server "$DECISION_MODEL" "$DECISION_ARGS" "$DECISION_PORT" logs/decision-server.log
        dec="--base-url http://127.0.0.1:$DECISION_PORT --workers $WORKERS"
        if [ "$to_classify" -gt 0 ]; then
            # shellcheck disable=SC2086
            fminer classify $dec --stale --budget-minutes "$(left "${CLASSIFY_BUDGET:-90}")" || status=$?
        fi
        start=$SECONDS
        if [ -z "$status" ] && [ "$to_kb_classify" -gt 0 ]; then
            b=$(left "${SIDE_BUDGET:-45}" $start)
            if [ "$b" -ge 5 ]; then
                # shellcheck disable=SC2086
                fminer kb-classify $dec --stale --budget-minutes "$b" || status=$?
            else echo "kb-classify skipped: $b min left"; fi
        fi
        if [ -z "$status" ] && [ "$to_voices_classify" -gt 0 ]; then
            b=$(left "${SIDE_BUDGET:-45}" $start)
            if [ "$b" -ge 5 ]; then
                # shellcheck disable=SC2086
                fminer voices-classify $dec --stale --budget-minutes "$b" || status=$?
            else echo "voices-classify skipped: $b min left"; fi
        fi
        stop_server
        free_model "$DECISION_MODEL"
        [ -z "$status" ] || exit "$status"
        echo "::endgroup::"
        refresh
    fi
fi

# 3. gemma: kb-write + voices-write (shared WRITE_BUDGET)
if [ "$to_kb_write" -gt 0 ] || [ "$to_voices_write" -gt 0 ]; then
    budget=$(left "${WRITE_BUDGET:-60}")
    if [ "$budget" -lt 5 ]; then
        echo "kb-/voices-write skipped: $budget min left"
    else
        echo "::group::generative model: kb-write $to_kb_write, voices-write $to_voices_write, budget $budget min"
        start=$SECONDS
        start_server "$GEN_MODEL" "$GEN_ARGS" "$GEN_PORT" logs/gen-server.log
        gen="--base-url http://127.0.0.1:$GEN_PORT/v1 --workers $WORKERS"
        if [ "$to_kb_write" -gt 0 ]; then
            # shellcheck disable=SC2086
            fminer kb-write $gen --min-p "${MIN_P:-0}" --budget-minutes "$budget" || status=$?
        fi
        b=$(left "$budget" $start)
        if [ -z "$status" ] && [ "$to_voices_write" -gt 0 ]; then
            if [ "$b" -ge 5 ]; then
                # shellcheck disable=SC2086
                fminer voices-write $gen --budget-minutes "$b" || status=$?
            else echo "voices-write skipped: $b min left"; fi
        fi
        stop_server
        free_model "$GEN_MODEL"
        [ -z "$status" ] || exit "$status"
        echo "::endgroup::"
        refresh
    fi
fi

fminer stats
[ "${SKIP_SIDE:-}" = 1 ] || { fminer kb-stats; fminer voices-stats; }
