#!/usr/bin/env python3
"""Verify, encrypt and independently round-trip a private Questions checkpoint."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pipeline/src"))
from fminer import questions_corpus, questions_preflight


def run_node(arguments, phase):
    try:
        return subprocess.run(["node", str(ROOT / "scripts/questions_checkpoint.mjs"), *arguments],
                              check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as error:
        raise RuntimeError(f"Checkpoint {phase} failed: {error.stderr.strip()}") from error


def save(source, ratings, model, revision, output):
    source, ratings, output = (Path(value).resolve() for value in (source, ratings, output))
    if output.exists():
        raise ValueError("Checkpoint output already exists")
    if not os.environ.get("DB_KEY", "").strip():
        raise ValueError("DB_KEY is required")
    check = questions_preflight.verify(source, ratings, model, revision)
    with tempfile.TemporaryDirectory(prefix=".questions-checkpoint-", dir=output.parent) as temp:
        private = Path(temp)
        encrypted = private / "checkpoint.enc"
        pack = run_node([
            "pack", "--source", str(source),
            "--ratings", str(ratings), "--output", str(encrypted),
        ], "encryption")
        packed = json.loads(pack.stdout)
        if (questions_corpus.file_hash(source) != check["source_snapshot_sha256"]
                or questions_corpus.file_hash(ratings) != check["ratings_snapshot_sha256"]):
            raise ValueError("Snapshots changed during checkpoint creation")
        restored = private / "restored"
        run_node(["unpack", "--input", str(encrypted), "--output", str(restored)], "verification")
        if (questions_corpus.file_hash(restored / "source.db") != check["source_snapshot_sha256"]
                or questions_corpus.file_hash(restored / "ratings.db") != check["ratings_snapshot_sha256"]
                or questions_corpus.file_hash(encrypted) != packed["sha256"]):
            raise ValueError("Encrypted checkpoint round-trip did not match the verified snapshots")
        os.link(encrypted, output)
    return {"sha256": packed["sha256"], "bytes": packed["bytes"], "topics": check["topics"],
            "current": check["current"], "pending": check["pending"], "stale": check["stale"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("source", "ratings", "model", "revision", "output"):
        parser.add_argument("--" + name, required=True)
    os.umask(0o077)
    print(json.dumps(save(**vars(parser.parse_args()))))


if __name__ == "__main__":
    main()
