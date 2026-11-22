#!/usr/bin/env python3
"""Prepare an isolated Questions review candidate without model calls or publication."""

import argparse
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pipeline/src"))
from fminer import questions_corpus, questions_export, questions_preflight
import questions_checkpoint_store


def run_stage(command, phase, output):
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
    if result.returncode:
        log = output / f"{phase}-error.log"
        fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(result.stderr or result.stdout or f"Exit status {result.returncode}\n")
        raise RuntimeError(f"{phase} failed; inspect private log {log}")
    return result


def prepare(source, ratings, baseline, baseline_sha256, data, store, model, revision, output):
    source, ratings, baseline, data, store, output = (
        Path(path).resolve() for path in (source, ratings, baseline, data, store, output)
    )
    build = (ROOT / "build").resolve()
    if output == build or not output.is_relative_to(build):
        raise ValueError("Review output must be a new subdirectory of this worktree's build/")
    for path in (source, ratings, baseline, data, store):
        if path == output or path.is_relative_to(output) or output.is_relative_to(path):
            raise ValueError("Review output must not overlap any input")
    if output.exists():
        raise ValueError("Review output already exists")
    preflight = questions_preflight.verify(source, ratings, model, revision)
    output.mkdir(mode=0o700)
    candidate_dir = output / "candidate"
    with redirect_stdout(io.StringIO()):
        questions_export.cli([
            "--source", str(source), "--ratings", str(ratings), "--baseline", str(baseline),
            "--baseline-sha256", baseline_sha256, "--output", str(candidate_dir),
        ])
    audit = json.loads((candidate_dir / "audit.json").read_text())
    if (audit["source_content_sha256"] != preflight["source_content_sha256"]
        or audit["ratings_snapshot_sha256"] != preflight["ratings_snapshot_sha256"]
        or questions_corpus.file_hash(source) != preflight["source_snapshot_sha256"]
        or questions_corpus.file_hash(ratings) != preflight["ratings_snapshot_sha256"]):
        raise ValueError("Snapshots changed after preflight; candidate requires a fresh review")
    result = run_stage([
        "node", "scripts/questions_local_preview.mjs",
        "--source", str(candidate_dir / "candidate.json"), "--audit", str(candidate_dir / "audit.json"),
        "--data", str(data), "--store", str(store), "--output", str(output / "preview"),
    ], "staging", output)
    report_path = output / "localization-report.json"
    run_stage([
        "node", "scripts/localize.mjs", "--page", "questions", "--locale", "all", "--check-only",
        "--data", str(output / "preview/data"), "--store", str(output / "preview/data/locales"),
        "--report", str(report_path),
    ], "localization", output)
    localization = json.loads(report_path.read_text())
    if localization["requested"] != 0 or localization["translated"] != 0:
        raise ValueError("Unexpected translation activity during review preparation")
    return {"preflight": preflight, "candidate_sha256": audit["candidate_sha256"],
            "privacy_findings": len(audit["privacy_findings"]), "review_required": True,
            "localization": localization["coverage"], "output": str(output), "staging": result.stdout.strip()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source")
    parser.add_argument("--ratings")
    parser.add_argument("--checkpoint")
    parser.add_argument("--checkpoint-sha256")
    for name in ("baseline", "baseline-sha256", "data", "store",
                 "model", "revision", "output"):
        parser.add_argument("--" + name, required=True)
    args = vars(parser.parse_args())
    checkpoint = args.pop("checkpoint")
    checkpoint_sha256 = args.pop("checkpoint_sha256")
    if checkpoint:
        if args["source"] or args["ratings"] or not checkpoint_sha256:
            parser.error("--checkpoint requires --checkpoint-sha256 and excludes --source/--ratings")
    elif not args["source"] or not args["ratings"] or checkpoint_sha256:
        parser.error("supply --source and --ratings, or --checkpoint and --checkpoint-sha256")
    os.umask(0o077)
    if checkpoint:
        if questions_corpus.file_hash(checkpoint) != checkpoint_sha256:
            raise ValueError("Checkpoint does not match the independently verified SHA-256")
        build = ROOT / "build"
        build.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".questions-review-", dir=build) as temp:
            restored = Path(temp) / "restored"
            questions_checkpoint_store.unpack(checkpoint, restored)
            if questions_corpus.file_hash(checkpoint) != checkpoint_sha256:
                raise ValueError("Checkpoint changed during review preparation")
            args["source"], args["ratings"] = restored / "source.db", restored / "ratings.db"
            result = prepare(**args)
            result["checkpoint_sha256"] = checkpoint_sha256
    else:
        result = prepare(**args)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
