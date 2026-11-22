# Weekly incremental Questions pipeline

`.github/workflows/questions-weekly.yml` adds the Questions path on **Monday
03:17 UTC**, matching the existing delta schedule. It does not replace
`delta.yml`, Requests, Tips or Voices. Both CI jobs use concurrency group `delta`;
the Questions processor holds the existing `data-publication-lock` lease across
source/checkpoint pull, delta processing and encrypted saves. Local release
writers must use the same lease. An interrupted lease is never stolen by age:
use the existing inspected-owner recovery procedure in [delta setup](delta-setup.md).

**Activation and publication are OFF by default.** No workflow was dispatched,
data published or Pages deployment performed while implementing this pipeline.
The immediate initial full-corpus EN/FR/JA publication is owned separately and
must precede weekly activation. Missing gates fail explicitly, not as successful
no-op updates.

## Dataflow and freshness

1. Restore `forum.db.zst.enc` and `questions-weekly.tar.zst.enc` from the existing
   `data` release using `DB_KEY`. Verify private archive membership, checksums and
   SQLite integrity. Make online SQLite backups, including committed WAL data,
   with checksums in `$RUNNER_TEMP/questions-before-RUN-ATTEMPT`, outside the
   checkout, before inference.
2. Fetch new/edited threads using the existing delta fetch and 300-topic rolling
   sweep (45 recent days). Compare title/original opening/full raw thread hashes
   and original-poster identity against the reviewed raw-source seed; locally enrich only changed/new raw
   records and persisted unfinished delta IDs (at most 100). Pre-existing
   enrichment backlog is not swept or allowed to re-rate frozen decisions.
   There is no full-corpus summary backfill. Source membership remains all languages.
   Votes/views/listing timestamps alone do not trigger new Questions inference.
3. Reuse `questions_corpus.current` decisions with exact source fingerprint,
   decision input hash, model/revision, prompt and evidence version. Re-prepare
   membership, then classify at most 100 pending records within 45 minutes.
   The frozen final decisions are a required durable seed, not reconstructed
   from public arrays or re-rated for a test.
4. OpenAI receives one generated English title/body field and only source identity
   hints whose normalized text matches that field. This may transmit identifiers
   that redaction has not yet caught. It never receives source posts, the forum
   database, classifier decisions, private audits or keys as input content (the
   API key is used only for authentication). Known credential/token
   patterns are held locally before a request; pattern checks cannot guarantee
   detection of every secret. Hosted processing requires an explicit reviewed
   authorization and account/data-handling policy. OpenAI proposes exact PI substrings/kinds.
   Python resolves unique exact substrings to Unicode code-point offsets, validates kinds,
   bounds and non-overlap, and applies `[redacted]` deterministically without
   rewriting other characters. Existing source-linked identifier rules,
   sign-off/name checks and additional NFKC/casefold username/explicit OP checks run
   after redaction. Invalid, uncertain, incomplete, oversized or residual-PI
   responses hold the field and invalidate its previous current output. Only
   queued changed/new IDs are processed; the persisted queue survives retries.
   Each title/body has a separate hash-bound durable slot, pending-first scheduling
   and up to two durable attempts, each containing up to two HTTP calls. A title
   hold never suppresses a body attempt. Only two valid fields make an unreviewed
   proposal; no model response alone is clearance. Excess context is held, never silently
   truncated. Ambiguous repeated substrings are held, not guessed; model numeric
   offsets are not blindly corrected. OP metadata changes invalidate PI freshness
   even when Questions decision evidence is unchanged.
5. Reuse successful display proposals only with exact source/decision/model/
   prompt/rule freshness. An independent source-linked exact-hash display review
   is required before translation. Changed source or model/prompt/rules makes
   the previous approval stale even if display wording happens to be identical.
   Failed/stale rows cannot enter the translation dataset.
6. Existing `backfill-requests.mjs --page questions` sends **only cleared display strings**
   to OpenAI using `OPENAI_API_KEY`, `OPENAI_MODEL` and optional
   `LOCALIZATION_REASONING_EFFORT`. Neither raw posts nor private identity hints,
   ratings, span audits or review records enter that request. Existing FR/JA
   exact-string caches are reused by canonical string hash and validated protection/
   translation version; record overlays are materialized against exact title/body
   hashes. Unchanged body/title strings survive another field changing. Only missing/
   changed translations are requested (200 strings,
   15 minutes, 10/ batch, 120-second request timeout, two attempts).
7. Existing local ONNX/CPU `build_embeddings.mjs` uses cleared display text and
   its existing pinned embedding model, quantization, and model+text cache key.
   The vectors remain private checkpoint data for future Questions semantic
   integration; this workflow does not change the currently disabled public
   Questions semantic contract or Requests vectors.
8. Always save consistent encrypted partial results and the updated encrypted
   working source. The previous encrypted checkpoint is retained. Only after a
   separate full-snapshot publication review can the existing Questions
   publisher update its preview/locales assets and the existing Pages workflow
   run. It never uploads the private embedding manifest or checkpoint as site
   data, and never overwrites `site-data.tar.gz`.

`manifest.questions` remains the Requests classification array.
`manifest.questions_view.files.entries` remains the Questions data contract.
Complete public entries derive `domains` in `questions.DOMAINS` order from
independent domain probabilities at or above 0.6, including nonquestion
controls. Private/partial candidate entries keep their existing shape; supplied
domain arrays are not trusted. Weekly publication preflight checks the derived
public entries against the private candidate before exact-byte review. Complete
snapshots retain `snapshot_scope: "processed"` with `corpus_complete: true`,
matching the shared UI validator.
Complete public normalization/Pages support is a dependency of the UI owner;
the publisher refuses older sample-only dependencies.

## Model and execution pins

`config/questions-weekly.json` records full weight and binary SHA-256s:

| Role | Model / quantization | Runtime |
|---|---|---|
| Classification | Kev-4B Q8_0, HF `f4624b6bb7072f1bab726a622c1cad3f96030b9e` | matches final source-backed checkpoint revision |
| Enrichment | Gemma-4-E4B-it Q8_0, HF `8903cb408c7df39cde2ed15fac397e6d7be92951` | local text-only, no mmproj/MTP download; never used for weekly PI redaction |
| PI proposals | `OPENAI_REDACTION_MODEL` explicit supported OpenAI model ID | structured outputs, `OPENAI_API_KEY`; no silent model fallback |
| Local server | llama.cpp `b11669`, verified Ubuntu x64 archive SHA-256 | `ubuntu-latest`, CPU, 4 threads, context 16384, one slot |

Local models are downloaded from immutable HF revisions and checked before execution.
The repository selects `gpt-6-luna` for both redaction and translation. OpenAI's
[model documentation](https://developers.openai.com/api/docs/models/gpt-6-luna)
lists `gpt-6-luna` as its available snapshot, with no dated alternative. The
redactor accepts this exact documented ID (or an explicitly dated snapshot),
uses low reasoning effort and requires the response model to match exactly.
This is not a claim of an immutable weight revision; the provider can change
behavior under an undated ID. Existing evaluation and independent review gates
remain required. Redaction requests use `https://api.openai.com/v1/chat/completions`.

Servers bind loopback ports 18080/18081 only; startup is bounded at ten minutes,
model downloads at 15 minutes with two retries. Model steps have outer
25/55/35-minute timeouts; redaction and translation requests are separately bounded.
An OpenAI redaction run touches at most 100 queued topics and 200 fields, with
at most four HTTP calls per field (800 total), 2,400 completion tokens per call,
12,000 display characters and 4,000 hint characters per field; 120 seconds per
field attempt and 30 minutes per run. These are execution bounds, **not a dollar
cap**. Configure account-level spending controls before activation. The private
current field cache reports known API usage and calls; retries, errors without
usage and interrupted calls may have unknown cost, and changing a field discards
its older cache totals. Provider billing is authoritative; do not treat these
partial counters as total spend.
The job's 300-minute limit leaves room for setup, lease acquisition and encrypted
saves. A hard runner termination cannot guarantee an exit trap; restore the last
verified encrypted checkpoint and inspect any surviving lease. No Copilot token
or new self-hosted runner is needed. Logs/unencrypted DBs are never uploaded as
Actions artifacts or public release assets.

## Readiness, review and durable seeding

An authorized owner must first consolidate the reviewed source-backed ratings,
cleared displays and FR/JA caches into an encrypted checkpoint. This code does
**not** read another worktree's private DB or automatically seed/reconstruct it.
Its stable layout is:

```text
ratings.db                   # existing corpus_state/decisions/attempts schema
display.db                   # displays table from scripts/questions_weekly.py
                             # plus independent display_fields proposal slots
readiness.json
evaluation.json
reviews.json
delta-source.json            # reviewed initial raw hashes + pending enrichment/redaction IDs
locales/fr/questions.json    # existing overlay contract
locales/ja/questions.json
locales/text-cache/          # existing canonical string caches/provenance
portable-locales/            # stripped complete overlays, staged before review
embedding/                  # derived private vectors/cache, optional initially
publication/                # private proposed candidate/audit/public preview
publication-review.json     # separate review, optional until publishing
ui-publication-review.json  # shared publisher/preflight review format
```

All non-DB files are private too. Store only through
`questions_weekly_store.py` **inside the shared lease**. It uses existing
AES-256-CBC/PBKDF2 encryption (200,000 iterations) plus an encrypted internal
SHA-256 manifest and integrity checks; this preserves the repository's existing
encryption pattern, not a claim of authenticated encryption. Restore rejects
traversal, symlinks, unexpected files and inconsistent membership. Push refuses
a missing seed, preserves `questions-weekly.prev.tar.zst.enc`, and never uploads
an unencrypted checkpoint.

For local inference, first create a new independent **durable** SQLite backup
outside the disposable checkout with
`uv run --project pipeline python scripts/questions_weekly.py backup`.
`RUNNER_TEMP` backups are CI recovery aids, not long-term local retention; the
encrypted release checkpoint is CI's durable copy. Initial seeding/import of the
source owner's separate frozen ratings and reviewed display audit still needs
explicit owner reconciliation. No production seed has been created here.

The review formats are weekly-specific; frozen snapshot approvals do not grant
blanket authorization for future deltas:

- `readiness.json`: schema `questions-weekly-readiness-v1`, `identity` from
  `questions_weekly.identity(config)`, `initial_en_fr_ja_published:true`,
  `weekly_private_processing_authorized:true`, `openai_redaction_authorized:true`,
  `evaluation_sha256`, nonempty
  `reviewer`, timezone-aware `reviewed_at`, `raw_delta_seed_sha256` matching
  `delta-source.json.initial_baseline_sha256`. The seeded corpus runtime must also
  match the exact final Kev revision/prompt/evidence.

During authorized seed reconciliation only, use
`uv run --project pipeline python scripts/questions_weekly.py capture-delta --source REVIEWED_SOURCE --root PRIVATE_CHECKPOINT`
to initialize `delta-source.json` from the exact reviewed initial source. It
does not infer anything or overwrite an existing baseline. Bind its
`initial_baseline_sha256` in readiness before activation. This prevents the first
weekly run from treating the old full-corpus backlog as new delta work.
- `evaluation.json`: schema `questions-redaction-heldout-v1`, same `identity`,
  `provider:"openai"`, `model_snapshot` equal to `OPENAI_REDACTION_MODEL`,
  `source_linked:true`, `independent_heldout:true`, `review_approved:true`,
  `dataset_sha256`, and `languages.en/fr/ja` each with positive integer
  `identifiers`, integer `missed`, and explicitly reviewed `minimum_recall`.
  Actual measured recall must meet the approved thresholds. The assertion
  record must be backed by independently reviewed evidence; the code does not
  manufacture an evaluation or authenticate the reviewer. Regex success and
  synthetic tests do not establish PI recall. Earlier Gemma evaluations do not
  authorize OpenAI redaction. Changing the model, prompts,
  rules or classifier identity invalidates this readiness.
- `reviews.json`: schema `questions-weekly-reviews-v1`, `entries` keyed by ID.
  Each review has schema `questions-weekly-display-review-v1`, exact
  `source_hash`, `freshness_hash`, `display_hash`, `audit_hash` from `display.db`,
  `source_linked:true`, `technical_meaning_preserved:true`,
  `privacy_cleared:true`, `openai_authorized:true`, `reviewer`, `reviewed_at`.
  Reviews may be AI-assisted but must identify their provenance honestly;
  scan-only or self-generated automatic clearance is not independent review.
- `publication-review.json`: schema `questions-weekly-publication-review-v1`,
  same `identity`, `source_linked:true`, `translated_fields_reviewed:true`,
  `complete_current_locales:true`, `publication_authorized:true`,
  `technical_meaning_preserved:true`, `reviewer`, `reviewed_at`,
  `expected_live_preview_sha256`, and `sha256` keyed by the exact five relative
  paths `publication/candidate.json`, `publication/audit.json`,
  `publication/public-preview.json`, `portable-locales/fr/questions.json`,
  `portable-locales/ja/questions.json`, plus `ui_review_sha256` binding the shared
  UI preflight's `ui-publication-review.json`. Set the independently verified byte hash of this
  review as repository variable `QUESTIONS_WEEKLY_PUBLICATION_REVIEW_SHA256`.
  A new/edited candidate requires a fresh review, not a timestamp-only update.

The shared `questions-private-publication-review-v1` UI clearance separately
binds `candidate_sha256`, `audit_sha256`, `preview_sha256`, `fr_sha256`,
`ja_sha256`, true `source_linked_privacy_review`, `translation_review`,
`publication_authorized`, reviewer/time and `expected_current_preview_sha256`.
The imported complete publisher must support `allow_complete` and `review`;
it runs its existing independent preflight under the publication lease and
publishes the already-normalized reviewed preview/portable locale bytes. The
weekly gate adds current durable-source/model/evaluation freshness; it does
not bypass that shared preflight.

Set `QUESTIONS_WEEKLY_ENABLED=true` **only after** initial full-corpus EN/FR/JA
publication, measured source-linked redaction approval and encrypted seeding.
Separately set `QUESTIONS_WEEKLY_PUBLICATION_ENABLED=true` only after the weekly
publication review and UI complete-preview gate are ready. The existing UI
config's `complete_enabled` must be enabled by its owner. Missing/held source,
ratings or display records prevent complete candidate construction. Missing/
stale locales, changed review bytes, source-linked PI findings, changed durable
forum source or changed live preview block publication.

**Current blockers:** full-corpus source-linked English/privacy and FR/JA
clearance, separately measured OpenAI held-out PI evaluation, reviewed hosted
data-handling authorization and exact model snapshot, reconciled encrypted seed, complete
UI/Pages support and corresponding readiness/review records are not supplied by
this assignment. The weekly path remains unactivated. Any existing source-linked
review findings require resolution; this implementation claims no validated
model accuracy.

The provider/identity change invalidates prior weekly readiness and display
freshness. Existing initial approvals cannot be automatically reissued under the
new identity; owners must reconcile already-cleared full-corpus display/locale
assets and review bindings without sending the old corpus for automatic redaction.
The initial raw-delta seed must be recaptured with OP identity included, under
review and before activation. Independent review is still an external prerequisite:
the workflow consumes exact-hash approvals; it does not author them.

`questions_weekly.py status` writes private `status.json` with mutually exclusive
per-topic stage counts, per-source-language counts, unresolved display/review
total and separate FR/JA current vs missing/stale counts among cleared records.
The exit handler persists these counts with partial progress; it does not label
unfinished records as cleared or expose their text in the Actions summary.

Offline behavioral checks:
`uv run --project pipeline python -m unittest discover -s tests -p 'test_questions*.py'`;
`node --test tests/questions-weekly-translation.test.mjs tests/questions-localization.test.mjs`.
