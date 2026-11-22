"""Verify an isolated Questions checkpoint before any automated continuation."""

import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3

from . import questions, questions_corpus as corpus


def snapshot(path):
    path = Path(path).resolve()
    if not path.is_file():
        raise ValueError(f"Snapshot does not exist: {path}")
    if Path(str(path) + "-wal").exists():
        raise ValueError(f"Snapshot has a WAL; create a standalone SQLite backup: {path}")
    con = sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)
    con.row_factory = sqlite3.Row
    if con.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        con.close()
        raise ValueError(f"Snapshot integrity check failed: {path}")
    return con


def verify(source, ratings, model, revision):
    source, ratings = Path(source).resolve(), Path(ratings).resolve()
    if source == ratings or (source.exists() and ratings.exists() and source.samefile(ratings)):
        raise ValueError("Source and ratings snapshots must be separate")
    if not model or not revision:
        raise ValueError("Expected model and runtime revision are required")
    source_sha, ratings_sha = corpus.file_hash(source), corpus.file_hash(ratings)
    with closing(snapshot(source)) as src, closing(snapshot(ratings)) as result:
        if corpus.get_state(result, "scope") != {"scope": "all"}:
            raise ValueError("Checkpoint is not an all-language corpus")
        runtime = corpus.get_state(result, "runtime")
        if not isinstance(runtime, dict) or (
            runtime.get("model"), runtime.get("revision"), runtime.get("prompt"), runtime.get("evidence")
        ) != (model, revision, corpus.PROMPT_VERSION, corpus.EVIDENCE_VERSION):
            raise ValueError("Checkpoint runtime or evidence version does not match the requested runtime")
        state = corpus.get_state(result, "source")
        rows = src.execute("SELECT * FROM topics ORDER BY id").fetchall()
        if not isinstance(state, dict) or state.get("source_content_sha256") != corpus.content_hash(rows):
            raise ValueError("Source content differs from the prepared checkpoint")
        decisions = result.execute("SELECT * FROM questions_decisions ORDER BY topic_id").fetchall()
        if not rows or [row["id"] for row in rows] != [row["topic_id"] for row in decisions]:
            raise ValueError("Source and checkpoint membership differ")
        if result.execute("SELECT COUNT(*) FROM questions_attempts WHERE outcome='running'").fetchone()[0]:
            raise ValueError("Checkpoint contains unfinished attempts")
        current = pending = blocked = stale = 0
        for row, decision in zip(rows, decisions):
            if decision["input_hash"] != corpus.fingerprint(row):
                raise ValueError(f"Source fingerprint differs for topic {row['id']}")
            if decision["answers"]:
                if (decision["decision_model"], decision["model_revision"], decision["decision_version"],
                    decision["evidence_version"]) != (model, revision, corpus.PROMPT_VERSION, corpus.EVIDENCE_VERSION):
                    raise ValueError(f"Existing decision is not current for topic {row['id']}")
                try:
                    questions.validate_answers(json.loads(decision["answers"]))
                except (KeyError, TypeError, ValueError) as error:
                    raise ValueError(f"Invalid decision for topic {row['id']}") from error
                if decision["readiness_error"] or decision["decision_error"]:
                    blocked += 1
                elif decision["decision_input_hash"] != decision["input_hash"]:
                    stale += 1
                else:
                    current += 1
            elif decision["readiness_error"] or decision["decision_error"]:
                blocked += 1
            else:
                pending += 1
        summary = {
            "topics": len(rows), "current": current, "pending": pending, "stale": stale, "blocked": blocked,
            "source_content_sha256": state["source_content_sha256"],
            "source_snapshot_sha256": source_sha, "ratings_snapshot_sha256": ratings_sha,
            "model": model, "revision": revision, "prompt": corpus.PROMPT_VERSION,
            "evidence": corpus.EVIDENCE_VERSION,
        }
    if source_sha != corpus.file_hash(source) or ratings_sha != corpus.file_hash(ratings):
        raise ValueError("A snapshot changed during verification")
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--ratings", type=Path, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--revision", required=True)
    args = parser.parse_args()
    print(json.dumps(verify(args.source, args.ratings, args.model, args.revision), indent=2))


if __name__ == "__main__":
    main()
