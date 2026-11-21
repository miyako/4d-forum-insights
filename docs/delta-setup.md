# Delta updates: one-time setup of `DB_KEY` and the encrypted DB

The weekly [delta workflow](../.github/workflows/delta.yml) needs the pipeline's last approved working database. This page explains where that database lives, what `DB_KEY` is, and how to set both up once.

## What is what

These assets live on the [`data` release](https://github.com/miyako/4d-forum-insights/releases/tag/data):

| Asset | What it is | Public? |
|---|---|---|
| `site-data.tar.gz` | The website's data: titles, English titles, machine-generated summaries, metadata, model scores and search vectors. These are the same files the site serves under `/data/`. | Yes, by design |
| `localization.tar.gz` | Optional French/Japanese Requests, Tips and Voices overlays, published separately from English. Missing or stale translations fall back to English. | Yes, by design |
| `forum.db.zst.enc` | The pipeline's working database (`forum.db`, about 160 MB), which contains **full forum thread text**. It is zstd-compressed and then AES-256 encrypted (about 47 MB). The previous copy is kept as `forum.db.prev.zst.enc`. | No: unreadable without `DB_KEY` |

- The repository is public, so anyone can download release assets. That is why the database is encrypted.
- `forum.db.zst.enc` exists only after the one-time seed below. Until then, the delta workflow logs a warning and does nothing.

**`DB_KEY`** is the passphrase that encrypts and decrypts `forum.db.zst.enc`. It is the same value in two places:

| Where | Needed | Why |
|---|---|---|
| GitHub repository secret `DB_KEY` | Always | The workflow uses it to decrypt the database, apply the delta and re-encrypt it. |
| macOS Keychain on your Mac | Only when you run `make db-seed`, `make db-pull` or `make db-push` | Your Mac does the first encryption, and later any local runs. |

- **Do not** put it in `~/.zshrc` or any file in the repository, and do not paste it into chats or issues.
- GitHub secrets are capped at 48 KB, so the database itself cannot be a secret. Storing only the passphrase as a secret, next to an encrypted file, is GitHub's recommended pattern for large secrets.

## Setup

Requirements: `openssl` (3.x), `zstd`, `sqlite3` and `gh`, logged in as a repository admin. Use either option for steps 1 and 2.

### Option A: Terminal

1. Create a random passphrase and store it in the Keychain:

   ```sh
   security add-generic-password -a "$USER" -s 4d-forum-insights-DB_KEY -w "$(openssl rand -base64 32)"
   security find-generic-password -s 4d-forum-insights-DB_KEY -w     # check: prints it
   ```

2. Copy it from the Keychain into the GitHub secret:

   ```sh
   security find-generic-password -s 4d-forum-insights-DB_KEY -w | tr -d '\n' \
     | gh secret set DB_KEY -R miyako/4d-forum-insights
   gh secret list -R miyako/4d-forum-insights                         # check: lists DB_KEY
   ```

### Option B: Apps

1. **Keychain Access**: choose **File → New Password Item…**, then fill in:
   - Keychain Item Name: `4d-forum-insights-DB_KEY`
   - Account Name: your macOS user name
   - Password: the output of `openssl rand -base64 32`
2. **GitHub**: open [Settings → Secrets and variables → Actions](https://github.com/miyako/4d-forum-insights/settings/secrets/actions), click **New repository secret**, then fill in:
   - Name: `DB_KEY`
   - Secret: the same passphrase

### Step 3, for both options: seed the encrypted DB (once)

Run this in a checkout of this repository after the upstream `~/4d-feature-miner` pipeline has finished:

```sh
DB_KEY="$(security find-generic-password -s 4d-forum-insights-DB_KEY -w)" make db-seed
gh release view data -R miyako/4d-forum-insights      # check: forum.db.zst.enc is listed
```

`make db-seed` does three things:

- It takes a read-only snapshot of `~/4d-feature-miner/data/forum.db` (`make snapshot`).
- It compresses and encrypts the snapshot.
- It uploads the result as `forum.db.zst.enc`.

`DB_KEY` is set only for that one command.

## After setup

- The delta workflow runs every Monday at 03:17 UTC and creates a private draft candidate plus a review PR, not a live update. Manual dispatch and its existing budgets/skip controls remain available from the default branch.
- **Local runs** (the fallback in the README) read the key the same way:

  ```sh
  export DB_KEY="$(security find-generic-password -s 4d-forum-insights-DB_KEY -w)"   # this shell only
  make db-pull && make delta-local && make db-push
  ```

## Lost or rotating the key

- **If the Keychain entry is lost**, the workflow keeps working, because GitHub still has the secret. However, GitHub never shows secret values, so you cannot read the database from your Mac any more.
- **To recover, or to rotate the key**:
  1. Create a new passphrase and update both the Keychain entry and the secret (steps 1 and 2).
  2. Re-seed from a local database: `make db-seed` (from upstream), or `make db-push` after `make delta-local`.

  Copies encrypted with the old key cannot be decrypted with the new one. Each push keeps the previous copy as `forum.db.prev.zst.enc`, so after the second push both assets use the new key.
- To update an existing Keychain entry, add `-U`: `security add-generic-password -U -a "$USER" -s 4d-forum-insights-DB_KEY -w "<new>"`.

## Review-before-live updates

The scheduled/manual delta has no Pages or workflow-dispatch permissions. It can create
draft releases and pull requests; enable **Allow GitHub Actions to create and approve
pull requests** in repository Actions settings (the workflow itself never approves or
merges a PR). No PAT or new signing/encryption key is required.

`scripts/review_updates.py prepare` acquires the existing publication lease, downloads
both live English and encrypted DB assets, records each immutable asset ID and exact
SHA256, and restores the captured DB locally before releasing the lease. Export retains
the original English provenance; it never recaptures around an old result.
`checkpoint` compresses/encrypts the working DB with the existing AES-256-CBC/PBKDF2
settings and stores it on `delta-candidate-RUN_ID-ATTEMPT`, an **unpublished draft release**.
Successful exports add `site-data.tar.gz` and `base.json` without clobbering candidate files.
Failed model progress stays encrypted on the draft but has no promotable PR.
Cancellation/runner loss may prevent checkpoint creation; no promise of persistence is
made for a terminated runner.

The bot opens `data-update-RUN_ID-ATTEMPT` with only
`.github/data-updates/current.json`. The descriptor binds repository, source commit,
run/attempt, draft-release ID, immutable asset IDs/sizes and every full-file SHA256.
The archive includes embeddings but excludes localization and Questions payloads.
The archive validator rejects private-preview flags, enabled Questions manifests,
Questions file paths, escaping/noncanonical paths, symlinks and duplicate members.
No generated corpus or plaintext DB enters Git, PR comments or Actions artifacts.
Draft release assets require write access in this public repository; keep these
releases unpublished. They are durable, not an expiring Actions cache.

After **human merge**, `promote-data.yml` checks out trusted default-branch code.
It re-reads the merged PR and descriptor from GitHub, requires a same-repository,
bot-created, descriptor-only PR merged by a human, and verifies its head/merged
descriptor, original successful default-branch delta run/attempt and bot-owned draft.
It downloads and validates all reviewed bytes before entering the publication lease.
Inside the lease it checks **both** original live bases, backs up the original encrypted
DB on the draft, promotes the approved checkpoint (keeping `forum.db.prev.zst.enc`)
and English archive, then dispatches Pages from the default branch.
The merge alone does not guarantee a successful upload/deployment: inspect the promotion
and Pages results. Merging while the delta run is still unfinished fails closed;
retry after the originating run succeeds.

An ordinary main-branch Pages build still reads the approved `data` release. Descriptor
PRs do not trigger a pre-merge Pages deployment. Local/manual publication remains a
separate explicitly authorized operation and invalidates pending candidates' bases.
Questions processing/export is **not automated**. The separate approved-preview
configuration and existing preview asset are not changed by delta or promotion.
Localization is also **not automatically published**: its workflow only retains
review checkpoints; use the existing source-hash-validating local publisher after
separate review/authorization.

### Partial promotion and retry

GitHub release clobber is not atomic across files. Before each live replacement,
promotion appends an immutable `promotion-NNNNNN.json` journal on its private draft,
binding the descriptor hash, PR, current asset IDs/hashes and pending operation.
It marks an asset complete only after re-downloading the approved bytes.
It never requests Pages until both assets are complete. An existing public deployment
is unchanged by a failed upload, but another explicitly requested Pages build during
partial recovery could observe incomplete release data; finish recovery before
running other publishers or Pages builds.

For a retry, manually dispatch **Promote reviewed site update** on the default branch
with the original merged PR number. No models or rebuilding are involved.
The retry re-verifies PR/run/candidate identity and all file hashes. It accepts unchanged
recorded live assets, or (only for the journal's pending asset) absence caused by clobber
or the exact approved bytes whose upload succeeded before acknowledgement/journaling.
A different intervening publication fails closed; never rewrite the original bases.
Completed retries do not re-upload or re-dispatch. A lost Pages dispatch acknowledgement
can cause an extra deployment request; both serve approved release data.

A hard-killed publisher can leave the shared lease held. Follow the owner/SHA-based
[lease recovery](#recovering-a-crashed-publisher), then retry the same PR.
Do not delete the draft or its journal/backup during a pending promotion. Cleanup of
old reviewed/rejected drafts is a separate explicit maintainer task; it is not automatic.
If a different publisher changed a partially promoted release, inspect and explicitly
restore/reconcile the approved state before constructing a fresh candidate.
An incomplete/zero-byte journal upload also fails closed with its exact asset ID.
After verifying that the original publisher has stopped and recovering any held lease,
inspect the failed asset and preceding valid journal against the live identities.
Remove only that confirmed incomplete upload before retrying; never delete a valid
journal record or guess recovery from asset age. Promotion performs no automatic
journal deletion or rollback.

Offline tests:
`python3 -m unittest discover -s tests -p 'test_*updates.py'` and
`python3 -m unittest discover -s tests -p test_publish_data.py`.

## Race-safe publication (CI and local)

Use `make publish` for local English publication, not `gh release upload --clobber`.
It calls `python3 scripts/publish_data.py publish-site --deploy`. Delta instead uses
the merge-bound review protocol above, which shares the same durable lease.
Both publication paths publish only
English data: `data/locales` is deliberately excluded even if the local export
contains old translations. English publication never reads or depends on localization.
English publication requires **base-asset CAS provenance**, not a timestamp
comparison. `make snapshot data embeddings` (or `make all`) captures the live
English asset's immutable ID and exact archive SHA256 before reading/exporting.
`make data` captures before export when run alone. Capture briefly holds the
shared lease; the build does not hold it. Successful export seals the provenance
against the candidate's English content. Under the publication lease, the
publisher re-downloads the current asset and requires the same ID and SHA256.
A concurrent delta/local English publication therefore invalidates the candidate,
regardless of clock skew or a newer-looking `generated_at`.

The ignored `build/site-data-provenance.json` binds repository, release, source
directory, base asset and candidate English content. Missing, unfinished, changed
content, or changed-base provenance fails closed. `make publish` never captures
a base on demand. Embedding files and the manifest's `embeddings` /
`embedding_config` metadata are derived outputs and excluded from the English
content binding, so an embedding-only rebuild may retain equal `generated_at`.
All other English content is bound; changing a timestamp cannot repair provenance.

Delta captures before pulling/processing its DB, then passes
`CAPTURE_ENGLISH_BASE=0` to export without replacing that original base.
For explicit/custom builds the equivalent sequence is:

```sh
python3 scripts/publish_data.py capture-english-base
# Read/build/export English here, before sealing. Do not reuse an old offline
# candidate under a freshly captured base.
python3 scripts/publish_data.py seal-english-candidate
make embeddings
# Publication is a separate, reviewed operation:
make publish
```

`ENGLISH_PROVENANCE=path` overrides the provenance path for Make; CLI commands
accept `--provenance path`, `--source site/data`, and `--release data`.
Use separate source/provenance paths for independent builds. If the base changed,
reconcile/rebuild from current English before creating a new candidate; do not
merely recapture at publication around an old offline DB or old artifact.

### Promoting an existing embedding-only candidate

Review artifacts do not upload themselves. After review, an existing candidate
without provenance may be adopted only if its **entire underlying English
content matches the current published English**, ignoring only embeddings and
localization. This verified adoption is explicit and refuses obsolete corpus
content; it does not trust or alter timestamps:

```sh
# After unpacking the approved candidate into site/data:
python3 scripts/publish_data.py adopt-published-english
# Inspect/preserve build/site-data-provenance.json with the candidate.
# Only when promotion is approved:
make publish
```

Adoption captures immutable base identity under the lease and seals the matching
candidate. Publication still verifies that base has not changed since adoption.
Do not use unverified `capture-english-base` plus `seal-english-candidate` to
launder an existing artifact. For a new corpus export, capture before reading its
source and build/seal normally instead.

After separately reviewing and authorizing retained localization checkpoints, a maintainer can call:

```sh
python3 scripts/publish_data.py publish-localization --store build/localization --deploy
```

It reads the latest English and localization release assets **inside** the publication
lease, merges overlays by locale/page/topic ID, and uploads only `localization.tar.gz`.
Incoming entries must match the current English source hash. A translation job that
started before an English update therefore cannot overwrite that update or replace
a current translation with an obsolete one. Pages extracts the optional overlay
asset independently after English; the browser additionally checks each source hash.

All cooperating live release writers (including encrypted DB pushes) acquire the same
repository-wide `refs/heads/data-publication-lock` ref by atomic REST create-ref.
Its commit identifies the owner (Actions run ID or local host/PID and unique nonce).
Release reads, merges and writes stay inside that lease. The ref survives crashes;
the default 30-minute acquisition wait **does not expire or steal a held lock**.
The lock is independent of Actions workflow concurrency, so manual/local and CI
publishers cannot bypass each other. Do not raw-upload release assets or delete
this ref while a publisher is running.

### Recovering a crashed publisher

Inspect the ref and its commit before intervening:

```sh
gh api repos/OWNER/REPO/git/ref/heads/data-publication-lock
gh api repos/OWNER/REPO/git/commits/LOCK_SHA --jq .message
```

Verify the named Actions run has finished/cancelled, or the local owner process
has stopped. Stop other recovery attempts; never recover based on age alone.
Then remove **only that observed owner**:

```sh
python3 scripts/publish_data.py --repo OWNER/REPO recover-lock \
  --expected-sha LOCK_SHA --confirm-owner-stopped
```

Recovery refuses a different SHA. A failed
upload can temporarily leave its own asset absent (GitHub's clobber is not atomic);
the independent English/localization assets and source-hash fallback prevent
cross-asset corruption. If the English asset identity is unchanged, retry the
publisher. If clobber removed it, CAS intentionally rejects the former base:
a maintainer must explicitly restore a reviewed candidate using capture-before-
restoration and sealing against the now-empty base. Do not silently refresh
provenance as part of a retry.

Optional local seeds live under ignored `localization/{fr,ja}/{requests,tips,voices}.json`. Hosted jobs
prepare `build/localization` from the independent downloaded asset (plus seeds if supplied),
and use `node scripts/localize.mjs --data site/data --store build/localization …`
to checkpoint new translations. The archive uses
`data/locales/{fr,ja}/{requests,tips,voices}.json`; extracting with `--strip-components=2`
places overlays directly into `build/localization`. Do not replace whole seed
files with older release files: merge validated entries matching current English
source hashes. Pages combines optional seeds with the latest release overlays after
extracting English and works normally with no seed directory. Generated corpora
are not committed. Initial overlay/vector artifacts belong on a separate
nonproduction draft release for review and later maintainer promotion, never the
live `data` release. Neither seed generation nor a local initial build publishes
anything until an explicit publication command is run.

The shared, offline preparation interface used by hosted jobs and Pages is:

```sh
python3 scripts/publish_data.py prepare-localization \
  --data site/data --seeds localization --store build/localization \
  --archive build/published/localization.tar.gz
```

The archive is optional. Invalid optional release overlays are ignored with a
warning; current seeds remain usable. All output entries match current English
source hashes. Pages uses `--store site/data/locales`.
Requests uses all topic rows from `topics.json` and aligned paragraphs from `text.json`.
Its canonical per-topic source fields are exactly `title_en`, `line` and `para`; null
generated fields normalize to empty strings, never the original forum title.
These generated fields are the only Requests translation inputs. No topic filtering,
re-rating, embedding rebuild or corpus changes are part of localization. The current
17,017-topic snapshot therefore remains intact. See [localization](localization.md)
for the source-hash and empty-field contract.

The [localization workflow](../.github/workflows/localization.yml) runs on the
`english-data-published` repository dispatch sent by local `make publish`, or by manual
dispatch. Review-protocol promotion deliberately does not send a localization dispatch.
Dispatch is best-effort: a dispatch failure warns without failing or rolling back
English publication. Retry localization manually if needed. Model/credential
failures occur only in the separate localization workflow. The local publisher's `contents: write`
allows sending the dispatch with `github.token`; repository dispatch is an
exception to GitHub's normal token-trigger recursion suppression.
The localization job has only `contents: read`: it cannot upload release overlays or
dispatch Pages. Successful translations are retained for separate maintainer review
and explicitly authorized local publication, using the existing publication lease.
Set repository
variable `LOCALIZATION_MODEL` to an explicitly available hosted model and secret
`COPILOT_GITHUB_TOKEN` to a token authorized for Copilot CLI; there is no implicit
model or authentication fallback. Optional variable `LOCALIZATION_REASONING_EFFORT`
must be supported by that model. It installs verified CLI version `1.0.104`, with
tools and built-in MCPs disabled by the runner.

Manual dispatch accepts `page` (`all`, `requests`, `tips`, `voices`), `locale` (`all`, `fr`,
`ja`), `entries` (1–1000), `minutes` (1–40), and `batch_size` (1–50). Defaults are
all/all/100/20/20. Successful batches are checkpointed (not published), even when
another batch fails. A separate **Translation status** job still fails for model,
authentication, or infrastructure errors; inspect the coverage summary and
14-day `localization-progress` artifact. Translation-budget exhaustion alone can
leave missing entries without being an error. Until reviewed checkpoints are explicitly
published, another hosted run starts from the currently approved overlays rather than
automatically resuming unpublished artifacts.
The workflow ignores dispatch payloads and always fetches current release assets
using trusted default-branch code, never a triggering PR head or its artifacts.

Offline publication tests:
`python3 -m unittest discover -s tests -p test_publish_data.py`.
