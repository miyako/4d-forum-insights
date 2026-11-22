#!/usr/bin/env python3
"""Run one opt-in Questions checkpoint update without publishing site data."""

import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pipeline/src"))
from fminer import questions_corpus as corpus, questions_preflight as preflight
import publish_data
import questions_checkpoint_store as store
import questions_run_batch as batch
import questions_save_checkpoint as save
import questions_transition as transition


def attempts(path):
    with closing(preflight.snapshot(path)) as con:
        return con.execute("SELECT COUNT(*) FROM questions_attempts").fetchone()[0]


def invalidated(previous, prepared, model, revision):
    with closing(preflight.snapshot(previous)) as old, closing(preflight.snapshot(prepared)) as new:
        old_rows = {row["topic_id"]: row for row in old.execute("SELECT * FROM questions_decisions")}
        new_rows = {row["topic_id"]: row for row in new.execute("SELECT * FROM questions_decisions")}
        return sum(corpus.current(row, model, revision) and not corpus.current(new_rows[topic_id], model, revision)
                   for topic_id, row in old_rows.items())


def update(gh, release, output, runtime, runtime_sha256, executable, weights, limit, minutes, max_stale, max_new):
    if not os.environ.get("DB_KEY", "").strip():
        raise ValueError("DB_KEY is required")
    if not 1 <= limit <= 1000 or not 0 < minutes <= 240:
        raise ValueError("Batch limit must be 1–1000 and budget at most 240 minutes")
    if not 0 <= max_stale <= 10000 or not 0 <= max_new <= 10000:
        raise ValueError("Source transition limits must be between 0 and 10000")
    manifest = batch.runtime_manifest(runtime, runtime_sha256, executable, weights)
    output = Path(output).resolve()
    parent = output.parent
    info = parent.stat()
    if not parent.is_dir() or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("Output parent must be an owner-only private directory owned by the runner")
    if output.is_relative_to(ROOT):
        raise ValueError("Output must be outside the repository workspace")
    runner_temp = os.environ.get("RUNNER_TEMP")
    if runner_temp and output.is_relative_to(Path(runner_temp).resolve()):
        raise ValueError("Output must be outside the runner's automatically cleaned temporary directory")
    output.mkdir(mode=0o700)
    with tempfile.TemporaryDirectory(prefix=".private-", dir=output) as temp:
        work = Path(temp)
        store.capture(gh, release, work / "capture")
        previous = work / "previous"
        store.unpack(work / "capture" / "checkpoint" / store.CHECKPOINT, previous)
        before = preflight.verify(previous / "source.db", previous / "ratings.db",
                                  manifest["model"], manifest["revision"])
        forum = work / "forum.db"
        store.decrypt_forum(work / "capture" / "forum" / store.FORUM, forum)
        source, ratings = previous / "source.db", previous / "ratings.db"
        changed_source = store.source_digest(forum) != before["source_content_sha256"]
        transition_counts = {"invalidated": 0, "new_topics": 0}
        if changed_source:
            prepared = work / "prepared"
            transition_result = transition.transition(source, ratings, forum, corpus.file_hash(forum), prepared,
                                                      manifest["model"], manifest["revision"])
            state = transition_result["prepared"]
            transition_counts = {
                "invalidated": invalidated(ratings, prepared / "ratings.db", manifest["model"], manifest["revision"]),
                "new_topics": state["topics"] - before["topics"],
            }
            if transition_counts["invalidated"] > max_stale or transition_counts["new_topics"] > max_new:
                raise ValueError("Source transition exceeds the configured stale or new-topic limit")
            source, ratings = prepared / "source.db", prepared / "ratings.db"
        if not changed_source and not batch.actionable(ratings):
            result = {"before": before, "after": before, "transition": transition_counts, "unchanged": True}
            (output / "result.json").write_text(json.dumps(result, indent=2) + "\n")
            return result
        result_dir = work / "batch"
        failure = None
        try:
            batch.run(source, ratings, runtime, runtime_sha256, executable, weights, result_dir, limit, minutes)
        except Exception as error:
            failure = error
        if failure and not (result_dir / "source.db").is_file():
            raise failure
        batch.runtime_manifest(runtime, runtime_sha256, executable, weights)
        _, after = store.verify_transition(previous, result_dir, manifest["model"], manifest["revision"])
        if failure and attempts(result_dir / "ratings.db") == attempts(previous / "ratings.db"):
            raise failure
        candidate = output / "candidate.enc"
        saved = save.save(result_dir / "source.db", result_dir / "ratings.db",
                          manifest["model"], manifest["revision"], candidate)
        published = store.commit(gh, release, work / "capture" / "base.json", candidate,
                                 manifest["model"], manifest["revision"])
        result = {"before": before, "after": after, "transition": transition_counts, "checkpoint": published["checkpoint"],
                  "candidate_sha256": saved["sha256"], "failed_attempts": bool(failure)}
        (output / "result.json").write_text(json.dumps(result, indent=2) + "\n")
        if failure:
            raise RuntimeError("Decision failures were checkpointed; retry the batch") from failure
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo")
    parser.add_argument("--release", default="data")
    for name in ("output", "runtime", "runtime-sha256", "executable", "weights"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--limit", type=int, required=True)
    parser.add_argument("--minutes", type=float, required=True)
    parser.add_argument("--max-stale", type=int, default=0)
    parser.add_argument("--max-new", type=int, default=0)
    args = vars(parser.parse_args())
    repo, release = args.pop("repo"), args.pop("release")
    os.umask(0o077)
    result = update(publish_data.GitHub(repo), release, **args)
    fields = ("topics", "current", "pending", "stale", "blocked")
    summary = {key: {field: result[key][field] for field in fields} for key in ("before", "after")}
    summary["unchanged"] = result.get("unchanged", False)
    summary["transition"] = result["transition"]
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
