# French and Japanese overlays

English is canonical. Requests, Tips and Voices expose English, French and Japanese display languages; this does not
change the source-thread language filter, topic IDs, ratings, order or links. English stays available without
any overlay. Missing or outdated translations are explicitly marked as English rather than silently attached
to changed content. Original forum titles and English-only source summaries are labelled separately.

Only the already published generated fields are translated: Requests `title_en`/`line`/`para`
(English title, summary line and paragraph), Tips `headline`/`body`, Voices
`question`/`positions`/`outcome`. No raw forum posts, encrypted DB or `DB_KEY` are required. Translation never
re-rates a topic or re-summarizes a discussion. In particular, anonymous positions remain unattributed and keep
their array order and cardinality.

## Portable checkpoints

`localization/{fr,ja}/{requests,tips,voices}.json` contains the initial agent backfill, independently of the raw forum
DB. This generated corpus, `site/data/`, and hosted checkpoints under `build/localization/` are ignored by
git; thousands of generated records are not committed with the PR. Preserve initial overlay/vector
artifacts on a separate **nonproduction draft release** for review. Only a maintainer promotes approved
artifacts afterward; initial review artifacts must not replace the live `data` release or deploy Pages.
Later hosted checkpoints are stored independently from the English release. Each overlay has:

```json
{
  "version": 1,
  "locale": "fr",
  "page": "tips",
  "entries": {
    "9370": {
      "source_hash": "64 lowercase SHA256 hex characters",
      "translation_version": "1",
      "fields": {"headline": "…", "body": "…"}
    }
  }
}
```

The source hash is SHA256 of UTF-8 canonical JSON of **only** the English translated fields: recursively
sort object keys, retain array order, use compact JSON with literal Unicode. Topic and page identify the
record; locale and translation/prompt version identify the translation. A changed field invalidates the
whole record, not another topic or locale. A failed batch never replaces a successful checkpoint. Unchanged
entries survive English-only exports. Orphaned/stale records can remain in the portable store, but are never
counted as current or attached by the UI.
Requests reads every row of `topics.json` using its `fields` indexes and the corresponding
`text.json.para` value at the same row index. Both file paths come from
`manifest.files.topics`/`manifest.files.text`, ignoring URL cache keys. Its exact source object is
`{"title_en": "...", "line": "...", "para": "..."}`. Null generated fields become
empty strings (`title_en` is omitted by the exporter when identical to the original title); it never falls back
to the original forum title. Empty generated fields stay empty in translations. All 17,017 topics
in the current corpus remain represented, including topics without a generated English title.
Original titles, source-language metadata, scores, vectors and other generated artifacts are not
translation inputs and remain unchanged.
Well-formed older translation-version strings remain valid portable records; they are stale, not a reason
to reject the entire checkpoint. Preparation and publication accept only the exact current version (`1`)
as current, so an older-version entry cannot replace a current translation.

The CLI validates IDs, exact field sets, nonempty strings (except preserved empty Requests fields),
position counts and source hashes. Code, URLs,
placeholders, common product names and technical identifiers are masked and checked before verbatim
restoration. Technical names not recognized by the mask must still be preserved by the translation prompt.
This is deterministic structural validation, **not** a claim that automated checks prove semantic accuracy.
English question-word and whitespace word-count checks are deliberately not applied to French/Japanese.

Requests also masks bare uppercase commands/constants, dotted API paths and calls, `#` directives and
underscore identifiers. Its batch/text-cache `protection_version` is `2`; stale masked batches are
rejected. Tips/Voices masking and portable overlay `translation_version: "1"` remain unchanged.

## Local frontier-model backfill

Authenticate the installed Copilot CLI normally. Do not copy credentials into this repository.
Select an available **hosted frontier model** explicitly; the runner never substitutes a local small model.
For example, after checking that the account supports the selected model:

```sh
node scripts/localize.mjs --data site/data --store localization \
  --page voices --locale all --model "$LOCALIZATION_MODEL" \
  --batch-size 15 --entries 554 --minutes 45 --workers 4
node scripts/localize.mjs --data site/data --store localization \
  --page tips --locale all --model "$LOCALIZATION_MODEL" \
  --batch-size 30 --entries 20574 --minutes 180 --workers 4
node scripts/localize.mjs --data site/data --store localization \
  --page requests --locale all --model "$LOCALIZATION_MODEL" \
  --batch-size 15 --entries 34034 --minutes 180 --workers 4
```

Each successful batch is atomically persisted immediately; reruns translate only missing/stale records.
`--entries` counts locale-records (a topic translated into two languages counts twice). `--minutes` caps
wall time, `--timeout` caps an individual CLI call, `--retries` is capped at three, and `--workers` at eight.
`--reasoning-effort low` can reduce backfill overhead if the configured frontier model supports it;
otherwise leave it unset (or set `LOCALIZATION_REASONING_EFFORT` to a supported effort).
The CLI exposes no tools to the translator and disables built-in MCP access. The published text is treated
as untrusted data, never as instructions. A store lock prevents simultaneous local writers. After an
interruption, verify no runner still owns it before removing the exact `<store>.lock` directory.

`build/localization-report.json` records actual current/stale/missing coverage and failed IDs; `complete` is
true only if every requested page/locale is current. Reaching a budget is a partial checkpoint, not completion.
The report also records a page snapshot hash over the sorted topic IDs and English source hashes, so coverage
can be tied to the exact English snapshot rather than just a matching record count.
Use `--check-only --require-complete` for an initial-backfill acceptance check that fails on any missing/stale record.
Failures produce a nonzero exit status **after** saving successful peers. A later run resumes safely.

For the larger initial Requests corpus, use the exact-string deduplicating runner:

```sh
node scripts/backfill-requests.mjs --data site/data --store build/localization-requests \
  --locale all --model gpt-6.1-sol --reasoning-effort low \
  --batch-size 30 --entries 100000 --minutes 300 --workers 8 --timeout 300
node scripts/backfill-requests.mjs --data site/data --store build/localization-requests \
  --locale all --check-only --require-complete
```

Check model availability with the locally authenticated CLI first. This runner translates each identical
nonempty generated string once per locale, across title, line and paragraph fields and topic IDs. The
17,017-topic snapshot contains 41,042 unique strings per locale; `--entries` here budgets **locale-strings**,
not the locale-records counted by `localize.mjs`. It interleaves locales, caps workers at eight, validates
protected tokens and batch identities, and atomically checkpoints `text-cache/{fr,ja}.json` after every
successful batch. Topic overlays and `report.json` are refreshed periodically and on graceful shutdown.
Rerun the same command/store to resume; do not remove its lock while a runner is active. The report's
`complete` flag describes persisted topic overlays, not a sample or the number of submitted strings.

The deduplicated store contains Requests only. Combine it with the existing independent production
overlays **locally**, without overwriting Tips/Voices or publishing anything:

```sh
python3 scripts/publish_data.py prepare-localization \
  --data site/data --seeds build/localization-requests --store build/review-localization \
  --archive build/published/localization.tar.gz
node scripts/localize.mjs --data site/data --store build/review-localization \
  --check-only --require-complete --report build/review-localization-coverage.json
```

Keep the combined portable store, Requests text cache, coverage report and pinned English source archive
on the nonproduction draft review release. Maintainer approval is still required before promotion.
Hosted small deltas continue using `localize.mjs`; they do not redo this initial string backfill.

An older exact-string cache requires an explicit one-time `--upgrade-protection` writable run.
This backs up the old cache under `cache-backups/protection-v1`, validates its original protected
tokens, then revalidates every string against the Requests-specific mask. Passing entries are retained;
only rejected strings are removed and retranslated. Topic overlays with an uncertified field are
removed until all their fields are current. Do not combine an upgrade with `--check-only`, or promote
the old archive while the upgrade is incomplete. Afterward rerun `--check-only --require-complete`
and regenerate portable review artifacts and checksums.

To translate through the current agent instead of the programmatic CLI, use the same validated exchange:

```sh
node scripts/localize.mjs --page voices --locale ja --batch-size 15 \
  --export-batch build/translation-request.json
# Have the frontier agent translate request.records fields, retaining IDs/placeholders.
# Save {"batch_id":"request.batch_id","records":[{"id":123,"fields":{...}}]} as the response.
node scripts/localize.mjs --page voices --locale ja \
  --import-batch build/translation-request.json --response build/translation-response.json
node scripts/localize.mjs --check-only --data site/data --store localization
```

Export/import requires one explicit page and locale. All batch records must validate before any is imported.
The single response batch ID binds the locale, source hashes, field content and prompt version. This avoids
asking the model to copy a random 64-character hash for every record; portable overlays still retain every
source hash. A missing or mismatched response batch identity is rejected before any checkpoint is changed.
Do not claim initial backfill completion from a sample or an export: inspect the persisted coverage report.

## Hosted incremental setup

The localization workflow is separate from the English delta: a localization/authentication/model failure
must not block English generation or publication. It runs after an English update and supports manual
locale/page/batch/time/entry budgets. It does **not** download a generative model on the hosted runner.

For this personal repository:

1. Create a fine-grained PAT with **Copilot Requests** permission and an appropriate Copilot subscription.
2. Save it as repository secret `COPILOT_GITHUB_TOKEN`.
3. Set repository variable `LOCALIZATION_MODEL` to a hosted frontier model ID actually available to that
   account. The desktop agent's model is not automatically available in Actions.

The normal `github.token` is for release writes, **not** assumed to authorize Copilot requests.
Organization policy-based `GITHUB_TOKEN` support is a different setup and is not assumed here.
Missing credentials/model configuration are reported explicitly. Never log token values or store them in
artifacts. See [GitHub's Actions authentication guide](https://docs.github.com/en/copilot/how-tos/copilot-cli/automate-copilot-cli/automate-with-actions).

Initial backfill artifacts are prepared locally for review; creating the PR does not merge it, upload a
production data release or deploy Pages. Review coverage before promoting a seed or resuming a backlog.

## Publication and local preview interfaces

`make data` keeps the English exporter unchanged, then merges optional local seeds into `site/data/locales`.
`make serve` performs the same merge before starting the local server; `make locales` refreshes just
overlays without opening the pipeline DB. If `build/published/localization.tar.gz` was downloaded locally,
its validated, current records are merged after seeds. Local preparation never uploads anything:

```sh
python3 scripts/publish_data.py prepare-localization \
  --data site/data --seeds localization --store build/localization \
  --archive build/published/localization.tar.gz
node scripts/localize.mjs --data site/data --store build/localization \
  --page all --locale all --model "$LOCALIZATION_MODEL" \
  --batch-size 20 --entries 100 --minutes 20 --workers 1 --timeout 180 --retries 2 \
  --report build/localization-report.json
```

Both the seed directory and archive are optional; missing or invalid optional release overlays fall
back to current local seeds, or English if no seed exists. Default Pages does not require seeds:
it downloads the independent `localization.tar.gz` asset from the production `data` release.
Preparation merges by locale/page/topic ID and accepts only hashes matching current English.
Pages uses the same helper with `--store site/data/locales`, first removing any stale locales embedded
inside older English archives. The Pages artifact packages the entire `site/` directory, including
the static `site/requests/index.html` entry point; no server rewrite or separate Requests data export
is required.

Approved publication is explicit:

```sh
python3 scripts/publish_data.py publish-localization --store build/localization --deploy
# English only; never writes localization.tar.gz:
make publish
```

Localization uploads only the independent `localization.tar.gz` release asset, with archive paths
`data/locales/{fr,ja}/{requests,tips,voices}.json`. It re-reads both current English and current localization
inside the shared durable Git-ref publication lease, merges successful entries by their source hashes,
and cannot overwrite English. English, localization, local/manual publishers and encrypted DB pushes
all respect that lease. There is no unsafe lock expiry; see [lease recovery](delta-setup.md#recovering-a-crashed-publisher).
English publication additionally checks immutable base-asset CAS provenance, captured before export
and sealed against the candidate's English corpus. A changed asset ID/archive SHA rejects publication
even if an old offline candidate claims a newer timestamp. Embedding-only updates may keep equal
`generated_at`; verified `adopt-published-english` supports reviewed existing candidates only when their
underlying English matches the current asset. Missing provenance fails closed; `make publish` never
captures a fresh base on demand. See [English publication and promotion](delta-setup.md#race-safe-publication-ci-and-local)
for exact capture/seal/adoption commands.

After every production English upload, the shared publisher sends an `english-data-published`
repository dispatch, covering both delta and local/manual `make publish`. Dispatch failure only warns:
English remains published, and localization can be started manually. Model/authentication failures
also cannot roll back or block English publication. The hosted workflow ignores dispatch payloads,
checks out trusted default-branch code, and downloads current release assets.
Manual inputs are `page` (all/requests/tips/voices), `locale` (all/fr/ja), `entries` (1–1000),
`minutes` (1–40), and `batch_size` (1–50); defaults are all/all/100/20/20.
Partial successes publish even if translation fails, while a separate **Translation status** job
reports model/authentication/infrastructure failures. Coverage and partial checkpoints are retained
in the job summary and the 14-day `localization-progress` artifact.
