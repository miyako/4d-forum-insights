#!/usr/bin/env python3
"""Prepare an explicitly approved new source without modifying a Questions checkpoint."""

import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pipeline/src"))
from fminer import questions_corpus as corpus, questions_preflight as preflight
from questions_checkpoint_store import verify_transition


def copy_private(source, destination):
    with Path(source).open("rb") as src, destination.open("xb") as dst:
        os.fchmod(dst.fileno(), 0o600)
        shutil.copyfileobj(src, dst)


def transition(source, ratings, new_source, approved_sha256, output, model, revision):
    source, ratings, new_source = map(lambda path: Path(path).resolve(), (source, ratings, new_source))
    old_state = preflight.verify(source, ratings, model, revision)
    if corpus.file_hash(new_source) != approved_sha256:
        raise ValueError("New source does not match the explicitly approved SHA-256")
    with closing(preflight.snapshot(new_source)):
        pass
    output = Path(output).resolve()
    output.mkdir(mode=0o700)
    copy_private(new_source, output / "source.db")
    copy_private(ratings, output / "ratings.db")
    if (corpus.file_hash(output / "source.db") != approved_sha256
            or corpus.file_hash(new_source) != approved_sha256
            or corpus.file_hash(output / "ratings.db") != old_state["ratings_snapshot_sha256"]
            or corpus.file_hash(ratings) != old_state["ratings_snapshot_sha256"]):
        raise ValueError("A source or checkpoint snapshot changed while copying")
    con = corpus.connect(output / "source.db", output / "ratings.db")
    try:
        corpus.prepare(con, output / "source.db")
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        con.execute("PRAGMA journal_mode=DELETE")
    finally:
        con.close()
    old = output / "previous"
    old.mkdir(mode=0o700)
    copy_private(source, old / "source.db")
    copy_private(ratings, old / "ratings.db")
    if (corpus.file_hash(old / "source.db") != old_state["source_snapshot_sha256"]
            or corpus.file_hash(old / "ratings.db") != old_state["ratings_snapshot_sha256"]):
        raise ValueError("Previous snapshots changed during transition")
    _, state = verify_transition(old, output, model, revision)
    return {"previous": old_state, "prepared": state}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--ratings", type=Path, required=True)
    parser.add_argument("--new-source", type=Path, required=True)
    parser.add_argument("--approved-source-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--revision", required=True)
    args = parser.parse_args()
    os.umask(0o077)
    print(json.dumps(transition(args.source, args.ratings, args.new_source, args.approved_source_sha256,
                                args.output, args.model, args.revision), indent=2))


if __name__ == "__main__":
    main()
