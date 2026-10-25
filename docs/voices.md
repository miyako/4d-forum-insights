# Forum Voices (in progress)

`/voices/` will list forum threads where the community weighed in. For each thread it will show the question
at stake, the views that came up and how the discussion ended. Like Forum Tips, it is built from public threads
and links to each one. It never shows usernames or post text.

Status: the decision-model step is implemented and piloted. Next come the write-ups (gemma) and the page.

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

## Run it

```bash
make snapshot                                   # or use the working DB: FMINER_DB=build/work.db
cd pipeline
# one llama-server at a time; Kev on its own port
llama-server -hf ggml-org/Kev-4B-GGUF:Q8_0 -c 16384 -np 4 -fa on --port 18080 &
FMINER_DB=../build/work.db uv run fminer voices-classify --base-url http://127.0.0.1:18080 [--sample 300]
FMINER_DB=../build/work.db uv run fminer voices-stats --top 50
```

All 1,840 candidates take about 3.3 hours. `--budget-minutes` makes the run time-boxed, and the run resumes
where it stopped.

## Next

1. **`voices-write` (gemma, about 315 threads, about 20 min).** It writes the question at stake, 2–4 positions
   in neutral, unattributed language ("Some argued…, others…") and the outcome, in about 80–120 words. Each
   write-up is checked automatically: it is rejected if it contains any participant's username, or a name
   from a signature, and the model then retries.
2. **`/voices/` page ("Forum Voices").** It reuses the tips UI, with filters for subject and settled, sorting
   by range of views, participants or recency, and a cross-link when a thread is also a feature request. The
   footer has a link to request removal (GitHub issues).
3. **Delta.** `voices-classify --stale` re-rates threads when they are bumped.
