# fminer — vendored copy

This directory is a **copy** of the `fminer` package from the local `4d-feature-miner` repository
(upstream commit `845ee37`, plus the empty-summary fix from `8381102` ported by hand), vendored so GitHub Actions can run the incremental (delta) pipeline.
The original is never modified from here. Changes made in this copy (all marked `vendored copy` in the code):

| File | Change |
|------|--------|
| `db.py` | `FMINER_DB` env var overrides the DB path; new `topics.raw_changed_at` column (migrated automatically) |
| `scrape.py` | `store_raw()` only moves `raw_changed_at` when the thread text really changed |
| `enrich.py`, `classify.py` | `--stale` (redo topics whose input changed) and `--budget-minutes` (time-boxed CI runs) |
| `enrich.py` (port of upstream `8381102`) | summary schema `minLength`/`maxLength` (line 30–400, paragraph 80–1500 chars); `summarise()` retries up to 4 times with temperature +0.25 per attempt on JSON errors or short fields and keeps the longest result; `--resummarise-empty` backfills missing/short summaries from the stored English text; rewriting a summary deletes the topic's non-`:original` classifications so `classify` redoes them. Unlike upstream, the backfill also honours `--budget-minutes` |
| `enrich.py` (`LLM.chat`) | JSON-schema requests ban gemma-4's thinking channel (`logit_bias` on `<\|channel>`, no `chat_template_kwargs`); upstream gets empty replies on some topics (11750, 13580). See `../docs/tips.md` |
| `delta.py` (new) | `fminer delta`: robots.txt check, `/latest.json` walk, raw fetch, edit sweep |
| `kb.py` (new) | `fminer kb-classify` / `kb-write` / `kb-stats`: the `/tips/` knowledge base (see `../docs/tips.md`) |
| `ci/run-kb.sh` (new) | runs kb-classify then kb-write, one llama-server at a time (18080 / 18081) |
| `ci/run-delta.sh` (new) | the end-to-end delta used by `.github/workflows/delta.yml` (its llama-servers listen on 18081 / 18080, so a local run never collides with the upstream pipeline on 8081 / 8080) |

**Keep `classify.QUESTIONS` identical to upstream**: the prompt version stored with every classification is a
hash of it, so any difference starts a new run instead of extending the existing one.

---

# 4d-feature-miner

Find feature requests on [discuss.4d.com](https://discuss.4d.com) that were not posted in the
[Feature Request](https://discuss.4d.com/c/feature-request/27) category.

Pipeline: **scrape** (Discourse JSON + `/raw`) → **enrich** (language detection, English translation of
title + first post, one-line and one-paragraph summaries with a local LLM) → **classify** (decision model via
llama.cpp `/v1/systemone`) → **calibrate / query**. Everything lives in `data/forum.db` (SQLite + FTS5).

## Setup

```sh
uv sync
git clone --depth 1 https://github.com/ggml-org/llama.cpp
cmake -S llama.cpp -B llama.cpp/build -DCMAKE_BUILD_TYPE=Release
cmake --build llama.cpp/build -j 8 --target llama-server

# generative model for translation/summaries (MTP speculative decoding ≈2.5x faster)
llama.cpp/build/bin/llama-server -hf ggml-org/gemma-4-E4B-it-GGUF:Q8_0 --spec-type draft-mtp \
    --no-mmproj --port 8081 -c 32768 -np 4 -fa on --jinja
# decision model
llama.cpp/build/bin/llama-server -hf ggml-org/Kev-4B-GGUF:Q8_0 --port 8080 -c 16384 -np 4 -fa on
```

## Usage

```sh
uv run fminer list --full        # topic metadata from every category (resumable); later runs are incremental
uv run fminer fetch              # raw thread text for new/updated topics (~0.6 topic/s, be polite)
uv run fminer enrich             # translate + summarise (→ :8081)
uv run fminer classify           # decision model (→ :8080); --original to skip translation (multilingual models)
uv run fminer calibrate          # AUC + bucket table vs. the Feature Request category
uv run fminer query --min-bucket likely --search 'write AND pro'
uv run fminer query --json > likely.json
uv run fminer export data/topics.jsonl
uv run fminer stats
./pipeline.sh                    # unattended: enrich while fetching, then swap to Kev-4B, classify, calibrate, export
```

## Notes

- The forum blocks generic crawler user agents; its robots.txt allow-lists `4d`, which the scraper uses
  (with a contact address). Disallowed paths (e.g. `/search`) are not used; requests are throttled.
- The unit of classification is the **topic** (title + first post + LLM summary of the thread). The forum
  category is deliberately *not* given to the decision model.
- Each topic gets three answers: `fr` (score, 5 levels → bucket), `request` (noul: author asks 4D for a
  change) and `gap` (noul: thread reveals a missing capability). Runs are keyed by model + a hash of the
  questions, so different models/prompts can be compared with `calibrate`.
- The FR category gives reliable positives but the rest are noisy negatives, so the AUC is a lower bound.
  Bucket cut-offs are the nearest level of the expected score (≥3.5 most likely, ≥2.5 likely, …).
- 16 GB Macs can't hold gemma-4-E4B Q8 and Kev-4B Q8 at once without heavy swapping (generation drops to
  ~1 tok/s), so `pipeline.sh` runs the two servers one after the other.
- Start gemma with `--cache-ram 0`. llama-server's host prompt cache defaults to 8 GiB. Every enrich or
  kb-write prompt is a different thread, so the cache never gets a hit, but it fills within minutes and
  pushes a 16 GB machine into swap. `ci/run-delta.sh` and `ci/run-kb.sh` pass it by default.
- Early model comparison (FR category vs. rest, small sample): Kev-4B AUC ≈ 0.98–0.99; Julia-1 on
  untranslated text ≈ 0.55 (unusable for this task).
