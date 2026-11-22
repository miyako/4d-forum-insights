# On-device Questions PI proposals and held-out measurements

This is a **local command-line tool**, not a hosted Copilot review or automatic
privacy clearance. It operates on file paths without exposing private text in
messages. No private input or real model inference was used to implement/test it.
It supports an exact existing candidate and all its source languages; it does
not regenerate display text from topic summaries or change Questions ratings.

## Prerequisites and server ownership

Use `config/questions-weekly.json`'s pinned Gemma Q8_0 artifact:
`ggml-org/gemma-4-E4B-it-GGUF`, revision
`8903cb408c7df39cde2ed15fac397e6d7be92951`,
`gemma-4-E4B-it-Q8_0.gguf`, 8,031,237,344 bytes,
SHA-256 `49008e7cc7edadf154c8748904a89ee86eb6905d220aac568f6b7b92ca6aa0c6`.
The source owner handles the single download to an independently durable cache.
The tool never downloads weights or substitutes Kev/another model.

Use the verified local llama-server build 11669 / `620d2225`, sufficient available
memory for the 8 GB weights plus a 16,384-token context, one slot and a separately
available port. Check memory/owned server slots before running; do not terminate
another owner's Kev/server process to make room. The launcher fails on an occupied
port, starts only its own PID and stops only that PID. It sets `--offline`,
`--no-webui`, `--log-disable`, context 16384 and one slot. Its private startup log
stays local; request/prompt/response text is never printed/uploaded.

Set `LLAMA_SERVER` and `GEMMA_MODEL_FILE` to verified absolute paths.
Private runs require `PRIVACY_API_KEY_FILE`, a current-user-owned regular **0600** file containing
one local API key. Its value is read only by the local client/server, never printed
or passed on the command line. No OpenAI/Copilot credential is used. The local tool
also verifies model file hash, binary/runtime, owned server PID's model/alias/context/
port arguments and local `/models` alias before sending any identity text. The
existing Kev server cannot pass these checks merely by occupying a loopback port.
`PRIVACY_ALLOW_UNAUTHENTICATED=1` is an explicit local diagnostic exception only;
the standalone client likewise requires `--allow-unauthenticated` if no key file
is supplied. The source owner's private review policy uses authenticated mode.

## Bounded existing-candidate proposals

Prepare independently verified standalone source/candidate snapshots and hashes.
The candidate must remain private (`private_preview:true`, `publish_allowed:false`)
with exact `{id,title,body}` display fields. The source-content hash is the existing
`questions_corpus.content_hash` over all source topics ordered by ID, not a raw DB
file hash. Optional required IDs are a private JSON integer array; default is all
candidate entries, regardless of original source `lang`.

```sh
bash pipeline/ci/run-questions-privacy-local.sh propose \
  --source /private/source.db --source-content-sha256 VERIFIED_SOURCE_CONTENT_SHA \
  --candidate /private/candidate.json --candidate-sha256 VERIFIED_CANDIDATE_BYTE_SHA \
  --ids /private/required-ids.json \
  --backup-dir /durable/independent/questions-backups \
  --out /private/local-pi-results --limit 30 --minutes 20
```

The backup directory must be outside disposable worktrees. Source SQLite online
backups include WAL contents and are integrity/checksum checked; candidate bytes
are copied independently; previous result DBs are backed up before retries.
An advisory single-writer lock protects each result set.

`records.db` creates one aggregate record plus **two `field_results` slots per
required ID** before inference. Each title/body slot has its own UTF-8 text SHA-256,
input hash bound to source/candidate/scope/model/prompt/rules/runtime identity,
`pending`, `held` or `review_required` outcome, attempts, completed attempts,
known model-call count, and private output/audit. A title failure cannot abort its
body. Budget exhaustion leaves an unattempted body **pending**; it gets priority
over held title retries next invocation. Successful fields are reused independently.
Any held field holds its aggregate record; otherwise pending fields keep it
pending. Only two successful proposals produce aggregate `review_required`, still
not privacy clearance. Partial aggregate display fields remain NULL.

`--limit 30` bounds distinct topics touched, not fields: both fields are eligible
within the time budget. `--max-field-attempts` defaults to two attempts per field
across invocations (the legacy `--max-record-attempts` flag is an alias). Attempts
are reserved durably before inference; interruptions consume the cap. At most four
field attempts occur per topic with the default policy; aggregate `attempts` is
the maximum field attempt count, not total HTTP calls. Exhausted fields remain held.
This coverage schema changes run identity: **use a new output directory for old
topic-only results**, preserving old evidence. No old topic hold or successful
topic row is silently promoted to field coverage.

`progress.json` includes topic and title/body counts, required/completed/model-
attempted field counts, per-field held reasons, exhausted retries and unaccounted
interrupted attempts. A completed source-evidence hold is **not** a model attempt;
transport attempts are **not** valid proposals. Unknown interrupted calls are not
invented. Private `field-coverage.json` has exactly one entry per ID/field with
input/text hashes, state/attempt counts and audit/output hashes (no display text);
its exact file SHA-256 is bound in progress. The live SQLite field slots remain
authoritative if interruption prevents refreshing reports. Held field audits
contain fixed categories/hashes/call counts, never response/source/exception text.
No output overwrites the original candidate or any source/ratings DB.

Gemma sees each display field plus source-linked identity hints, including explicit
`topics.op_username` even when absent from parsed authors, through authenticated
loopback HTTP. No post content is invented. Gemma proposes `{text,kind}`; Python
resolves unique exact substrings to Unicode offsets and validates matched strings,
non-overlap and schema, then deterministically applies spans. Existing source-linked
rules plus conservative NFKC username/OP checks hold residual identifiers. Matching
is never fuzzy, case-folded or width-normalized; absent, repeated or overlapping
proposed substrings hold instead of guessing. Context overflow,
uncertainty, malformed/unfinished responses and missing identity evidence are held,
not truncated or silently accepted. Two attempts per field; bounded request timeout.

`progress.json` reports required-ID membership and mutually exclusive outcomes.
`proposal_unresolved` counts pending/held proposals; `unresolved` includes **every**
record still lacking independent privacy clearance. `privacy_cleared` remains zero:
even empty spans and regex passes are proposals, not independent review. A separate
source-linked local review must verify privacy and retained technical meaning before
corrected output is promoted into exact-hash translation/publication clearance.
The corrected local synthetic challenge still had two missed gold spans in
unheld outputs: complete field coverage and valid proposals cannot establish
privacy recall or authorize publication.

## Synthetic challenge evaluation and independence

Create the deterministic 27-case EN/FR/JA planted-PI challenge fixture outside the worktree:

```sh
uv run --project pipeline python scripts/questions_privacy_fixture.py --out /private/gold.json
# Preserve the printed SHA and this synthetic challenge result.
bash pipeline/ci/run-questions-privacy-local.sh evaluate \
  --fixture /private/gold.json --fixture-sha256 VERIFIED_FIXTURE_SHA \
  --out /private/local-pi-evaluation --limit 30 --minutes 20
```

The fixture has explicit offsets for planted personal names, accents/Japanese,
emails, phones, user paths, accounts, addresses and full-width usernames; negative
cases contain 4D versions/APIs/commands and public product documentation URLs.
The gold format is `questions-local-pi-gold-v1`, with `independent_heldout:false`
and cases `{id,lang,text,posts,expected_spans}`. Each span has exact
`{start,end,text,kind}`. Real held-out inputs must be independently labeled
on device. The generated fixture was authored in the same implementation stream
as the detector and used for tool/protocol diagnosis, not independently held out.
Separation from tuning does **not** establish independent authoring/labeling.
The measurement explicitly records input assertions but keeps
`independent_heldout:false` / `independence_verified:false`: a supplied boolean is
not proof. Prior detector-selected AI findings/corrections are biased challenge
cases, not independent corpus-recall labels.

The evaluation writes private per-case/per-language/per-span-kind identifier counts,
detected/missed counts, failed/held/not-run cases, non-PI characters redacted,
elapsed seconds, model-call counts and exact dataset/runtime/model/prompt/rule
identity and held reason categories. Failed/uncertain outputs count conservatively
as missed gold labels; these include protocol failures, not just valid detection misses.
Non-PI characters removed and held technical negatives require technical-meaning
review; zero collateral characters alone does not establish semantic fidelity.
Existing conservative rules hold any remaining URL, including a public product
URL: report that limitation rather than relabeling harmless URLs as personal PI.

**No readiness threshold or approval is manufactured.** `review_approved` and
`publication_allowed` remain false. Synthetic passes establish only the planted
fixtures, never real-corpus recall. A separately approved residual-risk/threshold
policy and independent local source-linked review remain blockers; uncertain
records cannot be published automatically. No real pinned-Gemma evaluation was
performed in this implementation session.

Offline implementation checks:
`uv run --project pipeline python -m unittest discover -s tests -p 'test_questions*.py'`.
