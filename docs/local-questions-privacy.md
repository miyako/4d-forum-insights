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

Set `LLAMA_SERVER` and `GEMMA_MODEL_FILE` to verified absolute paths. Optional
`PRIVACY_API_KEY_FILE` must be a current-user-owned regular **0600** file containing
one local API key. Its value is read only by the local client/server, never printed
or passed on the command line. No OpenAI/Copilot credential is used. The local tool
also verifies model file hash, binary/runtime, owned server PID's model/alias/context/
port arguments and local `/models` alias before sending any identity text. The
existing Kev server cannot pass these checks merely by occupying a loopback port.

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

`records.db` creates one slot per required ID before inference. It binds exact
candidate/source/selection/model/prompt/rules/runtime identity, then records exact
source input and field hashes, structured span audit, corrected display hashes,
bounded attempts and `pending`, `held` or `review_required` outcome. It refuses
changed inputs/scope/model in the same results directory. Retries skip successful
current proposals; unfinished/error records resume in the same required scope.
No output overwrites the original candidate or any source/ratings DB.

Gemma sees only each display field plus source-linked identity hints through
authenticated loopback HTTP. Python validates exact Unicode offsets/matched strings,
non-overlap and schema, then deterministically applies spans. Existing source-linked
rules plus NFKC username checks hold residual identifiers. Context overflow,
uncertainty, malformed/unfinished responses and missing identity evidence are held,
not truncated or silently accepted. Two attempts per field; bounded request timeout.

`progress.json` reports required-ID membership and mutually exclusive outcomes.
`proposal_unresolved` counts pending/held proposals; `unresolved` includes **every**
record still lacking independent privacy clearance. `privacy_cleared` remains zero:
even empty spans and regex passes are proposals, not independent review. A separate
source-linked local review must verify privacy and retained technical meaning before
corrected output is promoted into exact-hash translation/publication clearance.

## Held-out synthetic evaluation

Create the deterministic 27-case EN/FR/JA planted-PI fixture outside the worktree:

```sh
uv run --project pipeline python scripts/questions_privacy_fixture.py --out /private/gold.json
# Keep the printed SHA and fixtures separate from prompt tuning.
bash pipeline/ci/run-questions-privacy-local.sh evaluate \
  --fixture /private/gold.json --fixture-sha256 VERIFIED_FIXTURE_SHA \
  --out /private/local-pi-evaluation --limit 30 --minutes 20
```

The fixture has explicit offsets for planted personal names, accents/Japanese,
emails, phones, user paths, accounts, addresses and full-width usernames; negative
cases contain 4D versions/APIs/commands and public product documentation URLs.
The gold format is `questions-local-pi-gold-v1`, with `independent_heldout:true`
and cases `{id,lang,text,posts,expected_spans}`. Each span has exact
`{start,end,text,kind}`. Real held-out inputs must be independently labeled
on device; prior detector-selected AI findings/corrections are biased challenge
cases, not independent corpus-recall labels.

The evaluation writes private per-case/per-language/per-span-kind identifier counts,
detected/missed counts, failed/held/not-run cases, non-PI characters redacted,
elapsed seconds, model-call counts and exact dataset/runtime/model/prompt/rule
identity. Failed/uncertain outputs count conservatively as missed gold labels.
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
