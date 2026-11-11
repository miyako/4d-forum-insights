# Questions: English-only pilot

The original pilot explores `/questions/` without publication or changes to the weekly delta. UI development
and a separately approved, labelled small public preview now proceed while full-language processing
continues; see the final sections. Original pilot/private outputs remain ignored files under `build/`.

## Scope and meaning

- One entry per thread, describing the main question and the substantive gap that remains.
- Broad questions and questions inside bug reports remain eligible. Missing necessary information is recorded
  separately as **Needs clarification**.
- **Unanswered** means **zero replies**, an objective metadata flag, not a coverage degree. Replies can exist
  without a useful answer. Missing reply-count metadata is shown as unknown rather than assumed zero.
- **Answer coverage** is an ordered decision-model assessment: No useful answer, Mostly unanswered,
  Partially answered, Mostly answered, Fully answered. Probability distributions retain uncertainty;
  there is no Unclear degree or separate Workaround axis. A version-scoped “no” or an adequate workaround
  can fully answer the question.
- **Poster acceptance** is evidence of resolution. An explicit positive reply such as
  “that worked” or “this answers my question” is evidence the decision model can use for Fully answered.
  The experimental writer flag is retained only as a diagnostic: it never changes coverage or status.
  Thanks alone or “I will try” is insufficient. The reply must belong to the original poster, be present
  in the supplied evidence, and be cited with a paraphrase of the acceptance.
- **Solved** is a forum metadata state. A marker set by the poster or a moderator has exactly the same
  significance. Marked-solved threads remain solved, regardless of the model's coverage rating. The marker
  alone does not mean the poster wrote an acceptance reply or force the model metric to full.
  **Closed** is independent and does not establish resolution.
- **Importance (impact)**: Critical, Significant, Minor, Unknown; evidenced consequences, not complaint
  intensity, popularity or complexity.
- **Complexity**: Complex, Moderate, Straightforward, Unknown; not author skill or estimated answering effort.
- **Domains**: Coding, User interface, Administration, Installation & setup, Migration, Web & network,
  Database management, Integration, Other. Independent yes/no decisions support multiple labels. The preview
  uses probability >= 0.6 as an exploratory threshold, not a calibrated confidence guarantee.
- Version scope is preserved only when evidenced. No inference that a newer version fixes an old limitation.
  Historically confirmed resolutions remain resolved.

## Sampling and evidence

The default is **30 enriched English-source threads**. Selection is deterministic (seed 7) and balances
no-reply threads, accepted-answer threads, unaccepted Tips-open threads, unaccepted Tips-solved threads,
and other threads. Empty strata redistribute their slots. Tips classifications are hints for sampling,
not eligibility gates or truth labels; a DB without Tips ratings still works.

The cohort is persisted before model requests, so rerunning finishes the same sample rather than sampling
30 more threads. Changing sample size or seed requires a separate pilot DB.

The decision model sees anonymised, numbered opening/reply excerpts prioritizing the first reply,
the latest reply and the original asker's last three follow-ups, with other replies spread across the thread.
Both models use the same core post-selection order. The larger writer input retains the decision model's
core posts, while adding context. Long posts preserve their beginning and ending with an explicit middle-
omission marker, rather than dropping the resolution at the end. Short threads receive larger per-post
allowances. Original-poster posts are explicitly labeled alongside anonymised participant letters.
The writing model gets a larger evidence budget and checks question presence and positive poster acceptance
while writing the English question, answer summary, remaining gap, version scope, clarification and acceptance
evidence. It does not rate coverage, importance or complexity.
Both inputs explicitly identify omitted/unavailable evidence. Code is replaced by placeholders in this first
pilot; code-dependent assessments therefore need particular scrutiny. Source post numbers are required in
write-ups and validated against the actual supplied excerpts.

The preview places decision coverage, importance and complexity distributions in blue beside the writing
results in purple. Only the decision model controls question detection and coverage. Writer question and
acceptance fields remain experimental diagnostics; conflicts do not alter decisions or block processing.
These differences are review flags, not an error-rate estimate.
Coverage × importance and coverage × complexity tables summarize question threads; all three degrees have
filters, alongside poster acceptance and zero replies. Solved and Closed/Open are displayed separately.
Only current-prompt, current-input outputs appear. The first run's static report and page are preserved as
`build/questions-pilot/prior-report.json` and `prior-index.html`; old model judgments are not relabeled as
new ones.
Every entry is labeled **unreviewed**. This sample is deliberately biased for review, not a prevalence estimate.

## Running locally

Requires `uv` and a compatible `llama-server` with `/v1/systemone`. Models run one at a time on dedicated
ports 18080/18081, with one worker by default to preserve context. Existing listeners are never stopped.

```sh
LLAMA_SERVER=/path/to/llama-server make questions-pilot
# Optional: QUESTIONS_SAMPLE=15 FMINER_DB_SRC=/path/to/forum.db QUESTIONS_DB=build/questions-small.db

make questions-report
python3 -m http.server 8002 --bind 127.0.0.1 -d build/questions-pilot
# http://127.0.0.1:8002/
```

The source is opened read-only and copied using SQLite online backup into `build/questions.db`.
Model results live in a separate `questions_pilot` table, never in the existing classifications/entries.
Current exports cannot accidentally expose them. `report.json` and `index.html` contain generated text,
ratings and topic metadata, never original post text or participant names.

`DECISION_MODEL`, `GEN_MODEL`, `DECISION_ARGS`, `GEN_ARGS`, `DECISION_PORT`, `GEN_PORT`, `WORKERS`,
`CLASSIFY_BUDGET`, `WRITE_BUDGET`, `SKIP_CLASSIFY=1` and `SKIP_WRITE=1` configure the runner.
With a budget, rerun the same command to finish pending work. Failed requests are stored and logged; the command
fails explicitly and can be retried. Changed source content or prompts invalidates stale ratings/write-ups.

```sh
uv run -q --project pipeline python -m unittest discover -s tests -p 'test_questions*.py'
```

## Review before expanding

Check whether the extracted question and remaining gap reflect the thread; whether “no” answers, adequate
workarounds, explicit poster acceptance and missing context are distinguished; and whether cited posts
support the assessment. Look closely at question-detection differences, acceptance diagnostics, impact
claims, complexity and version scope.
Do not publish the pilot as a current unresolved-issues list before this review.

## First pilot (2026-11-07)

These are historical results under the original rules, not the current five-degree coverage scale.

All 30 threads were classified and written successfully. This source snapshot had no Tips ratings, so the
cohort contained 10 no-reply threads, 10 accepted-answer controls and 10 other threads.

| Writing model's provisional assessment | Threads |
|---|---:|
| Unanswered | 2 |
| Answered, not confirmed | 11 |
| Answered and confirmed | 10 |
| Not a question | 7 |

**16 of 30 threads have decision/writer disagreements.** These are not validated resolution counts.
The local preview includes side-by-side results and a different-coverage-labels filter. That comparison
counts only threads both models identify as questions, rather than combining confirmation differences and
coverage differences into one purported error count.

The initial writing prompt manufactured questions from announcements and confused lack of confirmation with
a substantive missing answer. Asking for eligibility before generating the question, requiring empty
question/answer/gap fields for non-questions, and validating confirmation and answer evidence corrected those
obvious output failures. Two entries still claimed full coverage with no answer; explicit validation and retry
changed them to Unanswered.

Coverage remains the main quality concern: some plausible suggestions may be treated as complete answers
even when a specific sub-question is still open. Version references and explicit asker confirmations also
need human review. Keep this as a private pilot; do not publish or expand the run based on these counts alone.

## Rerun with the revised rules (2026-11-07)

The same 30 topic IDs were rerun with five coverage degrees, binary positive poster acceptance, and
importance/complexity distributions. Both models again identify 23 questions and 7 non-questions.
All 30 classifications and write-ups completed; no pending entries remain.

| Coverage | Raw decision-model labels | Coverage used after poster-acceptance checks |
|---|---:|---:|
| No useful answer | 5 | 3 |
| Mostly unanswered | 6 | 5 |
| Partially answered | 0 | 0 |
| Mostly answered | 5 | 3 |
| Fully answered | 7 | 12 |

Among the 23 question threads, 10 are marked solved and 3 have zero replies. These metadata states are
independent of the coverage table. The writer flags 11 positive poster replies; 5 change the coverage used
to Fully answered while preserving the original decision distribution.

Importance: 3 Significant, 4 Minor, 16 Not enough evidence, no Critical. Complexity: 9 Complex, 5 Moderate,
4 Straightforward, 5 Not enough evidence. The preview exposes the full probability distributions, not just
the chosen labels, and the coverage × importance/complexity tables.

Two writing attempts incorrectly cited other participants as the original poster. Their retries constrained
acceptance citations to actual original-poster reply numbers present in the excerpts; neither can now use
a solved marker alone as evidence of a positive poster reply.

**Acceptance still needs review.** For example, topic 18531 was flagged on a reply that only thanks people
and asks another question. Topic 20345 accepts an explanation of a later sub-question, which may not resolve
the original question. These are model-quality limitations, not verified acceptances. The preview explicitly
warns about gratitude and secondary-point acceptance. Do not interpret the adjusted coverage counts as
human-validated results or publish them yet.

## Prompt refinement after reviewing the rerun

The next prompt versions anchor both models to the opening post's intended outcome and material constraints.
Coverage includes calibration examples distinguishing a central unanswered need, substantial answered and
unanswered parts, and only a minor remaining requested detail.

Positive acceptance must refer to the **main** solution, not courtesy, tentative understanding, discovering
a function, or accepting a secondary explanation. The writer must name the accepted solution and explain
how the poster's reply establishes that the original need was met. Later reports that the same problem
persists prevent an earlier positive reply being treated as current acceptance. A clearly accepted complete
answer followed by an unrelated new question is not automatically rejected.

Importance still measures evidenced impact, but explicit operational consequences do not require exact
financial amounts or user counts. Missing impact is not Minor, and a generic crash/server/database topic
does not imply Critical. Complexity is independent of severity, skill, command count and answer effort.

The evidence refinement fixes the observed WEB SEND TEXT case: the original poster's resolution statement
was previously absent at the decision model's shorter budget. Head-and-tail excerpts now preserve closing
resolution statements even at the context-shrink budgets.

These refinements change both prompt versions and invalidate old cached ratings for the next run.
The refinement was initially made without another model run. The subsequent controlled validation is
described below.

## Historical combined validation: writer gate not passed

The refined decision prompt and question-aware writer schema were run on the same 30 topic IDs.
All classifications and write-ups completed with valid current versions, input hashes, probability
distributions and owned acceptance citations. A conditional JSON schema prevents a model from declaring
an announcement a non-question while filling its question/answer/gap fields.

The validation expectations were recorded before inspecting the completed writer judgments:

- All five synthetic coverage calibration cases passed, including Partially answered.
- All eight live-thread coverage anchors passed. The previously truncated WEB SEND TEXT resolution now
  receives Fully answered directly from the decision model.
- Both impact anchors passed: evidenced server disruption is Significant; an ordinary capability question
  with no described consequences retains Unknown.
- Question detection matches the expectations for all 14 anchor threads, including both announcements.
- **10 of 14 anchor threads pass the complete gate. Four fail poster-acceptance checks.**

The four failures are topic 18531 (courtesy thanks), 20345 (secondary explanation), 24059 (tentative z-order
inference), and 37038 (acknowledging an overlooked function). The writer still marks these as acceptance
of the main solution despite the refined instructions. Valid citation ownership does not establish the
meaning of the cited reply.

**At this checkpoint, bulk processing had not been handed off.** More prompt wording alone had not
demonstrated reliable writer acceptance detection. An independent verifier would be necessary before
allowing that writer flag to promote coverage automatically. The subsequent decision-led approval below
instead removes this promotion entirely.

The pass/fail manifest is preserved as the session artifact `questions-prompt-validation.json`, alongside
`questions-coverage-calibration.json` and the generated pilot report. These results validate specific
English-source examples, not corpus-wide accuracy or all source languages. A future all-language session
must additionally spot-check multilingual evidence before expanding.

Semantic search remains in the future page's scope: keyword, semantic and hybrid modes, initially reusing
the existing multilingual embedding model and topic vectors. Canonical English generated entries and stable
topic IDs should remain compatible with that index. No generative model is needed at query time. UI work
and publication remain deferred; the subsequent approval below supersedes the full-corpus processing gate.

## Approved production scope: decision-led processing

After reviewing the failed combined gate, the user approved the **decision-only gate** for bulk processing.
The five coverage calibration cases, eight live coverage anchors and both impact anchors passed; writer
acceptance failures are explicitly excluded from production authority, not relabeled as successful checks.
The historical combined validation manifest remains unchanged.

Production uses Kev to decide question presence, coverage, importance, complexity and domains. Its coverage
prompt already considers explicit poster acceptance of the main solution. Solved, Closed and reply count
remain metadata. No separate unvalidated writer flag can promote coverage to Fully answered.

The next session should process the full source-language corpus using the latest available working DB,
reusing its existing English enrichment and the Requests/Tips text and localization assets. Do not retranslate
or summarize every thread, run the experimental writer as a prerequisite, add a new acceptance-verifier
blocker, redesign the UI, or publish results. Full-language processing must record input coverage, missing
evidence, errors and resumable progress, and spot-check multilingual examples without claiming that the
English pilot establishes corpus-wide accuracy.

### Model specifications for the introduction

| Role | Model | Precision / representation |
|---|---|---|
| Multilingual semantic search | `onnx-community/embeddinggemma-2-ONNX` | ONNX q4 inference; fp16 WASM fallback; native 768-dimensional sentence embeddings truncated to 256 MRL dimensions, normalized and stored as int8 |
| Decision ratings | `ggml-org/Kev-4B-GGUF:Q8_0` | GGUF Q8_0 through llama.cpp |
| Source-to-English enrichment and readable summaries | `ggml-org/gemma-4-E4B-it-GGUF:Q8_0` | GGUF Q8_0 through llama.cpp; reuse existing enrichment |
| Published-field display translations | GPT-6.1 Sol (`gpt-6.1-sol`) | Hosted frontier model, selected by the repository's `LOCALIZATION_MODEL` variable; provider-managed precision |

Embedding inference uses Transformers.js 4.3.1 and revision
`daa72c51243991dfcaf9f9137d2c573d8f7790c0`. The published manifest checked on 2026-11-07 contains
17,042 compatible 256-dimensional int8 vectors with zero stale rows. GGUF model repositories and quantization
variants are configured; no immutable GGUF weight revision is currently pinned. No generative model runs
at semantic-search query time. Questions will reuse the existing multilingual search infrastructure when
its UI is revisited.

## Private web UI preview (2026-11-09)

UI development is now approved independently of the running classifier. A consistent, read-only snapshot
supplied 31 fresh decisions across six source languages without pausing the model. This frozen sample is
not representative and is not approved for publication. Do not commit its JSON or use it with `make publish`,
DB upload, or Pages deployment.

`site/questions/` reuses the site's responsive reading layout, theme preferences and EN/FR/JA interface
navigation. It has keyword search, shareable filter state, coverage/impact/complexity/domain facets,
independent Solved/Closed/reply filters, clarification filtering, probability details, evidence warnings
and links back to Requests, Tips and Voices. Those three pages gain a **hidden-by-default** Questions link.
Cross-page navigation keeps the query, compatible search mode and UI language.
All four headers use a compact popup page menu with the current page identified, native tab navigation,
arrow-key shortcuts, Escape and outside-click dismissal.

Coverage circles fill clockwise from the top at 0%, 25%, 50%, 75% and 100% for the five ordered coverage
degrees. These are ordinal symbols, not measured answered percentages or model probabilities. Importance
and complexity use red, amber, blue and neutral grey labels for their high, middle, low and Unknown choices,
respectively; text labels remain visible, so colour is never the only indication.

**Needs clarification** means the model thinks essential answering details are missing, for example
reproduction steps, environment, version, or a clearer intended outcome. The filter uses its independent
probability at >= 0.5. It does not mean zero replies, imply an unsolved state, or exclude broad questions.
An expandable explanation beside the controls makes this distinction available in EN/FR/JA.

Text is reused, privacy-scrubbed English enrichment, explicitly labeled **thread summary**, not an extracted
answer or remaining gap. New generated-field localization is not performed. Coverage comes only from
`answers.coverage`; Solved is authoritative forum metadata but never promotes this model metric.
Unknown reply counts and unknown Solved/Closed states remain unknown. Questions use the pilot's inclusive
question-presence threshold of 0.5 and independent domain threshold of 0.6.

Stage a private preview using a supplied snapshot and a copy of the existing public site data:

```sh
mkdir -p build/questions-preview-base
gh release download data --repo miyako/4d-forum-insights \
  --pattern site-data.tar.gz --dir build/questions-preview-base
tar -xzf build/questions-preview-base/site-data.tar.gz -C build/questions-preview-base
node scripts/questions_preview.mjs --snapshot /path/to/questions-ui-preview.json \
  --data build/questions-preview-base/data
python3 -m http.server 8003 --bind 127.0.0.1 -d build/questions-ui
# http://127.0.0.1:8003/questions/?ui=en
```

The adapter accepts only `questions-ui-private-preview-v1` snapshots marked `private_preview:true` and
`publish_allowed:false`. It validates canonical decision distributions and allowlists display metadata.
It copies the public site/data into `build/questions-ui`, never overwrites `site/data`, and marks the staged
manifest private. Requests' existing `manifest.questions` classification array is preserved.

The **provisional frontend namespace** is `manifest.questions_view`, with `enabled`, `preview`, `semantic_enabled`, and
`files.entries`. Its entries file uses `{version:1, entries:[...]}` with stable topic IDs, `title`, optional
`body`, ISO `active` timestamp or null, `lang`, nullable `accepted`/`closed`/`replies`, canonical `answers`,
optional `evidence_warnings`, `decision_model` and `decision_version`. `accepted` maps the snapshot's Solved
metadata; it is not a generated poster-acceptance flag. Only the decision model's question gate controls
eligibility; experimental writer fields are not read.

Preview navigation requires all private markers and `questions_view.preview:true`; production navigation
requires explicit `questions_view.enabled:true` and no private markers. Pages rejects private manifests
outright and removes the Questions route unless production or a separately approved public preview is enabled. Full-data export, final contract agreement
and publication remain the processing session's separate rollout work; finishing classification alone
does not automatically deploy anything.

Semantic/hybrid infrastructure is prepared but **dormant**: the private adapter explicitly sets
`questions_view.semantic_enabled:false`. Separate authorization is required before setting it to true.
Without that opt-in, even compatible public embeddings are not passed to the semantic loader, and semantic
controls remain disabled. No embedding rebuild or new-field localization is performed.

Once authorized, compatible public embeddings can be reused lazily by stable topic ID; Questions
membership is applied **after** global ranking to avoid dropping a small sample before matching its IDs.
Absent/incompatible embeddings disable semantic controls with a visible explanation. Loading or inference
failure shows explicit keyword fallback status. This is infrastructure reuse, not new Questions-specific
embeddings or a claim that semantic quality has been evaluated on the sample.

```sh
node --test tests/questions-ui.test.mjs tests/locale-ui.test.mjs tests/requests-ui.test.mjs
```

## Authorized small public preview (2026-11-09)

The user separately approved publication of the UI with a **publication-safe small sample**, not the private
31-row development snapshot or the full corpus. The processing owner constructed and privacy-reviewed a
separate 15-question artifact: three threads each from EN/FR/DE/JA/ES. Text remains existing English thread
summaries; assessments remain unreviewed, probabilistic and explicitly non-representative. Raw source,
participant identities, private approval notes, runtime/DB provenance, and original private snapshots
must not be uploaded.

The independently validated `questions-preview.json` asset lives on the existing `data` release.
Upload it only through the shared publication lease:

```sh
python3 scripts/publish_data.py with-lock -- \
  gh release upload data build/questions-public/questions-preview.json \
  --repo miyako/4d-forum-insights --clobber
```

`config/questions-preview.json` explicitly opts the Pages build into this asset. The installer
`scripts/questions_public_preview.mjs` accepts only the separate `questions-ui-public-preview-v1` schema,
`public_preview:true`, `private_preview:false`, `publish_allowed:true`, `representative:false`,
valid decision distributions, and 1–60 rows. It allowlists public fields and refuses private manifests or
replacing an enabled production Questions dataset with a sample.

Pages installs the sample as `data/questions/preview.json` and sets `questions_view` to
`{enabled:false, preview:true, public_preview:true, semantic_enabled:false, files:{entries:...}}`.
The public banner distinguishes this frozen sample from the full dataset in EN/FR/JA. Questions becomes
available in all four popup page menus. A missing optional asset leaves Questions disabled with a build
warning; malformed or private data fails deployment. Disable the config flag to stop installing the preview.

The existing `site-data.tar.gz`, English corpus, localization overlays and encrypted DB are not replaced.
The build-time manifest retains Requests' `questions` array. Full processing is uninterrupted; no new
embedding build, new generated-field localization, semantic query, or automatic full-corpus publication
is authorized by this preview.

## Local translated rebuild (2026-11-11)

The user requested a safe bulk-processing pause and a **local-only** preview with actual French and
Japanese record translations. The classifier stopped at 8,082 current decisions before translation began.
This request does not authorize inference resume, release uploads or changing the published 15-row preview.

All 15 approved sample records have FR and JA `title`/`body` translations (30 locale-topic records).
The rebuild reuses 52 exact-string translations from the existing Requests cache; GPT-6.1 Sol translated
only eight missing titles in two calls. An unchanged second writable localization run reported zero
requested strings and zero model calls. Translations are independently checkpointed under
`localization/{fr,ja}/questions.json` and `localization/text-cache/{fr,ja}.json`, not inside the frozen sample
or classifier DB. See [localization.md](localization.md#reusable-questions-translations-local-review-only)
for the stable final-exporter contract, provenance and narrow field-invalidation behavior.

The UI loads `data/locales/{fr,ja}/questions.json` by stable topic ID and canonical `{title,body}` hash.
Only display prose changes: source language, numeric model probabilities, nullable forum metadata,
filters and IDs remain untouched. Keyword search indexes both translated and canonical text. The English
view remains canonical; missing/stale translations are explicitly labelled English fallbacks, not counted
as translated records. Technical-only titles may naturally be identical in all locales.

After generating/reusing translations, stage the local site without replacing the existing private preview:

```sh
node scripts/questions_local_preview.mjs \
  --source build/questions-public/questions-preview.json \
  --data build/questions-preview-base/data --store localization \
  --archive build/questions-localization-seeds/localization.tar.gz
python3 -m http.server 8003 --bind 127.0.0.1 -d build/questions-translated-ui
# http://127.0.0.1:8003/questions/?ui=fr
# http://127.0.0.1:8003/questions/?ui=ja
```

This copies the approved small sample and matching overlays into an isolated ignored build directory,
marks the staged manifest private/nonpublishable, and leaves semantic search disabled. Rebuilding is
read-only with respect to the durable localization store; raw source and original ratings are not accessed.
The Python static server runs no inference. Stop it when review is finished if local resource use is a concern.

## Expanded private processed snapshot (2026-11-11)

The subsequent explicit user selection expands only the **local** preview to all **8,082** freshness-checked
decisions available at the safe pause. It does not expand the public 15-row allowlist. The source owner
exported the snapshot in a consistent read-only transaction and checked every decision against current
source/runtime fingerprints. All 8,082 records have valid synthesized display text and metadata; **7,681**
meet `answers.has_question.noul >= 0.5`, and **401** non-question controls stay in the data but are not rendered
as questions. Solved threads are retained (1,272 across the processed snapshot). This is still an incomplete,
nonrepresentative portion of the 17,042-topic corpus; classification remains paused.

The private adapter accepts either the original `items` snapshot or the normalized version-1 `entries`
snapshot, never both. It requires `schema_version: "questions-ui-private-preview-v1"`,
`private_preview: true` and `publish_allowed: false`; row fields are allowlisted again during staging.
Only privacy-scrubbed existing English enrichment is passed to the browser or translation route. No raw
opening/replies, participant identities, private freshness audit, DB files or runtime paths are copied.
The source-language field remains distinct from the target display locale.

```sh
node scripts/questions_local_preview.mjs \
  --source build/questions-expanded/questions-private-8082.json \
  --data build/questions-preview-base/data --store localization \
  --archive build/questions-localization-seeds/localization.tar.gz \
  --output build/questions-expanded-ui
```

`snapshot_scope: "processed"` selects an accurate paused/incomplete banner rather than the old small-sample
description. The UI shows translation coverage for **all records** and **eligible questions** separately.
Pending or stale overlays retain explicitly labelled English fallback; neither an English fallback nor
technical tokens identical in both languages constitute new translation work. Exact source-hash validation
continues to reject stale entries. The original 15 records retain their exact canonical title/body hashes.

Before any new translation calls, `--seed-only` recovered **10,878 FR** and **10,875 JA** distinct strings,
including all 30 previously cached strings per locale. The remaining **5,260 FR / 5,263 JA** strings represent
about 1.11 million source characters, or 264 nominal 40-string batches before retries. This is a preflight
inventory, not a completion claim. Bounded runs use the configured GPT-6.1 Sol route with two workers;
each successful batch atomically checkpoints the same durable store. Snapshot, cache-audit and per-run
coverage reports stay under ignored `build/questions-expanded/`. Never replace a fuller store with the
old 15-row backup; seed exact matches into the current store instead.

## Conditional expanded publication and resume authorization

The later November 11 user instruction authorizes publishing the expanded **8,082-of-17,042 processed
preview only after both FR/JA overlays are complete**, followed by bulk resume only after the live
deployment is verified. It does not declare the unfinished corpus complete. The processing owner separately
regenerates and audits a publication-safe export; changing the private snapshot's flags is not approval.
The source audit checks all rows with source-linked participant/privacy helpers and deterministic sensitive
patterns, with targeted manual review of flagged candidates, not a manual reading of every sentence.
Unrecognized identifying free text remains a review limitation; private audit details must never ship.

Expanded approval uses the distinct `questions-ui-public-processed-preview-v1` schema, requiring
`snapshot_scope: "processed"`, an exact `processed_count`, exact `eligible_question_count`,
`corpus_total > processed_count`, and `corpus_complete: false`, alongside the existing public approval flags.
The old small-sample schema still rejects more than 60 rows. The public banner displays the processed/total
counts, eligible questions, incomplete/nonrepresentative scope and unreviewed model-assessment limitation.
The first expanded audit redacts ten English fields (three titles and seven summaries), invalidating only
those fields; unchanged text and all prior 15 records retain their caches.

After complete translations, a zero-call unchanged rerun, translated-text privacy review and normal merged
code integration, publish through the existing durable release lease:

```sh
python3 scripts/publish_data.py publish-questions-preview \
  --source build/questions-expanded/questions-public-8082.json --store localization \
  --expected-preview-sha256 <reviewed-current-questions-preview-sha256>
gh workflow run pages.yml --ref main
```

The publisher refreshes English data, the existing Questions asset and existing localization inside the
lease. It merges current overlays without replacing Requests/Tips/Voices, rejects missing/stale/empty
translations, strips private field provenance from Questions overlays, uploads the complete localization
archive first, then switches the Questions asset. It never uploads the raw DB, text cache, private audit or
backup. A later standard localization publication also authenticates Questions hashes against the separate
current approved asset, not the English archive alone.
The expected preview hash is checked again inside the lease; a concurrently changed asset requires a new
review rather than an automatic overwrite. A larger already published processed subset cannot be downgraded.

Pages installs the approved Questions source **before** preparing locale overlays. Expanded public preview
deployment fails closed if either locale is incomplete; the older small public sample and unrelated pages
retain optional English fallback. The route remains a preview, with production rollout and semantic search
disabled. Verify workflow success **and** live source/overlay IDs, canonical hashes, exact counts and zero
stale fallback before reporting publication complete or notifying the processing owner that the conditional
resume gate is satisfied. Workflow dispatch alone never releases the classifier.
