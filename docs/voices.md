# Forum Voices

`/voices/` lists forum threads where the community weighed in. For each thread it shows the question at stake,
the views that came up and how the discussion ended. Like Forum Tips, it is built from public threads and links
to each one. It never shows usernames or post text.

## Selection

1. **Candidates (metadata only).** These are threads with at least 6 posts, at least 5 participants, and at
   least 2 participants who posted 3 times or more, which signals a back-and-forth rather than a round of
   replies. That gives 1,840 of the 17,005 topics.
2. **`fminer voices-classify`.** Kev-4B (`/v1/systemone`) answers five questions, stored under
   `prompt_version = voices-v2-<hash>`:
   - `exchange` (choice): help / views / news / social. Voices keeps **views** and **news**.
   - `debate` (score, 5 levels): how far apart the opinions are, from "no disagreement" to "a strong,
     sustained debate". The question says that troubleshooting together is not disagreement.
   - `settled` (choice): consensus / decided (4D gave an official answer) / unresolved / none.
   - `subject` (choice): technical / product (direction, features) / business (licensing, pricing, ownership)
     / community.
   - `heated` (yes/no): the discussion becomes personal or emotional. It is used only to rank and is never
     shown.
3. **Rank** = debate score × P(views or news).

What Kev sees:
- the English title and opening post, and the thread summary;
- the counts: posts, participants, days, participants who returned;
- a spread of replies from the start to the end of the thread, in their original language and shortened,
  with **participants replaced by letters** (A = the author) and @mentions replaced the same way.

If a request is too long for the server slot, `ask_fitting` retries with a shorter post.

`scripts/build_data.py` keeps `voices-…` runs out of the main page's runs and counts, just as it does for
`tips-…` runs.

## Pilot (300 random candidates, 2026-10-25)

| | v1 (4 questions) | v2 (+ `exchange`, clearer `debate`) |
|---|---|---|
| Real exchanges of views in the top 20 / top 50 (hand-checked) | ~13 / ~22 | ~19 / ~38 |
| Time for 300 threads (Kev, 4 slots, M1 Pro) | 18 min | 32 min |

- **v1 mixed up two kinds of thread.** It confused long troubleshooting threads ("Server is not found",
  "Log file when run as service") with debates.
- **v2's top 30 are almost all exchanges of views:** the Volaris acquisition, 4D on Linux, the curse of
  Franglish commands, indentation, ChatGPT, `#DECLARE`, the future of arrays, best way to program, Qodly
  competitors, documentation problems. The remaining noise is mostly "New Tech Tips" announcement threads.
- **Distribution:** `exchange` is help 248, views 48, news 3, social 1. `subject` is mostly technical, and
  `settled` is mostly unresolved or consensus.
- **Projected size:** about 17% of candidates are views or news, so about 315 threads.

## Full run (2026-10-25/26, M1 Pro 16 GB)

- **voices-classify:** 1,840 candidates in 3 h 12 min (9.5/min, Kev, 4 slots), no errors. `exchange`: help
  1,509, views 292, news 33, social 6. `settled`: consensus 808, unresolved 704, none 256, decided 72.
- **Selected:** 277 threads (views or news, debate ≥ 0.75). About 36 of the top 40 are real debates: "Please
  let 0 be falsy", the 4D price increase, "I hate markdown", the Volaris acquisition threads, ORDA vs classic
  speed, a built-in MCP server, "Where are we going 4D?", a `return` keyword, the new documentation style,
  4D on Linux, AI-assisted coding. The rest are bug threads with an argument in them.
- **voices-write:** 277 write-ups in 36 min (gemma, 4 slots), no errors. All passed the format and no-names
  checks on the first or a retried attempt, and a manual scan of the capitalised words found only product and
  technology names. The writer never answered `discussion: false`, so the selection rests on Kev.
- **Published:** 277 discussions, 212 KB (`voices/entries.json`).

## Write-ups (`fminer voices-write`)

Selected threads (`exchange` is views or news, and `debate` ≥ 0.75, i.e. at least "minor differences") get a
write-up from gemma-4-E4B (chat API, JSON schema), stored in `voices_entries`:

- `question`: the issue, as one neutral question;
- `positions`: the 2–4 main views, one sentence each, unattributed ("Some participants argue…");
- `outcome`: how it ended (a shared view, an answer from 4D, or still divided);
- `discussion`: the writer's own check; `false` means there was no real exchange of views (hidden on the page
  unless `all=1`).

Gemma sees the same anonymised thread as Kev (letters instead of usernames, @mentions replaced) with longer
replies, and sign-offs ("Regards, Chuck", a last line with just a name) removed.

**No-names check.** Every write-up is checked against the thread's usernames (and their parts:
`Bruno.Smith` → bruno, smith) and the names found in sign-offs. A write-up that contains one, or refers to
participants by letter, is retried (up to 4 attempts, rising temperature); if every attempt fails, the entry is
stored with `problems: ["names"]` and **never exported**. The check cannot know every first name mentioned in
passing, so each card links to a removal request.

## Published data

`scripts/build_data.py` writes `site/data/voices/entries.json` (columnar) and `manifest.voices` (title, run,
question labels from `config/voices.json`, presets, removal link). Only selected threads with a publishable
write-up are exported, with: the rank, the debate level, kind, outcome and subject (with probabilities), the
number of participants, of returning participants and of days, and the write-up. `heated` is not exported.

## The page

`/voices/` reuses the Tips layout: keyword, hybrid and semantic search (the same topic embeddings), subject
chips, outcome filter, year, and sorting by liveliness (rank), date, participants or views. Each card shows the
outcome badge, the question, the views, the outcome, the subject, the debate level and the counts, and links to
the thread. "About this summary" shows the model ratings and a removal-request link.

## Run it

```bash
make voices KB_DB=build/work.db                 # both steps, one llama-server at a time (pipeline/ci/run-voices.sh)
make voices-stats KB_DB=build/work.db
make data DB=build/work.db && make serve        # then open /voices/
```

By hand:

```bash
make snapshot                                   # or use the working DB: FMINER_DB=build/work.db
cd pipeline
# one llama-server at a time; Kev on its own port
llama-server -hf ggml-org/Kev-4B-GGUF:Q8_0 -c 16384 -np 4 -fa on --port 18080 &
FMINER_DB=../build/work.db uv run fminer voices-classify --base-url http://127.0.0.1:18080 [--sample 300]
FMINER_DB=../build/work.db uv run fminer voices-stats --top 50
# then stop Kev and start gemma (see run-voices.sh for its arguments) on :18081
FMINER_DB=../build/work.db uv run fminer voices-write --base-url http://127.0.0.1:18081/v1
```

All 1,840 candidates take about 3.3 hours. `--budget-minutes` makes the run time-boxed, and the run resumes
where it stopped.

## Updates

After a delta (new replies re-enrich a thread), `make voices` re-rates changed candidates (`--stale`), rates new
candidates, and rewrites the write-ups whose rating is newer.
