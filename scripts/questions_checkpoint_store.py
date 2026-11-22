#!/usr/bin/env python3
"""Lease and compare-and-swap encrypted Questions checkpoints on the data release."""

import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pipeline/src"))
from fminer import questions_corpus as corpus, questions_preflight
import publish_data

CHECKPOINT = "questions-checkpoint.enc"
FORUM = "forum.db.zst.enc"
BACKUP_PREFIX = "questions-checkpoint-backup-"


def private_directory(path):
    directory = Path(path).resolve()
    info = directory.stat()
    if not directory.is_dir() or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("Private work directory must be owner-only and owned by the current user")
    return directory


def assets(gh, release):
    rows = gh.api("releases/tags/" + release)["assets"]
    result = {}
    for row in rows:
        if row["name"] in result:
            raise ValueError(f"Duplicate release asset: {row['name']}")
        result[row["name"]] = row["id"]
    return result


def download(gh, release, name, directory, asset_ids):
    if name not in asset_ids:
        return None
    directory.mkdir(mode=0o700)
    gh.release("download", release, "--pattern", name, "--dir", str(directory))
    path = directory / name
    return {"id": asset_ids[name], "sha256": corpus.file_hash(path), "size": path.stat().st_size}


def capture(gh, release, output, initial=False):
    output = Path(output).resolve()
    output.mkdir(mode=0o700)
    with gh.lease():
        ids = assets(gh, release)
        forum = download(gh, release, FORUM, output / "forum", ids)
        checkpoint = download(gh, release, CHECKPOINT, output / "checkpoint", ids)
        if not forum:
            raise ValueError("Encrypted forum database is missing")
        if (checkpoint is None) != initial:
            raise ValueError("Initial seeding must be explicit and only when no checkpoint exists")
        if initial and any(name.startswith(BACKUP_PREFIX) for name in ids):
            raise ValueError("Checkpoint backups exist; recover the previous checkpoint instead of reseeding")
        after = assets(gh, release)
        if after.get(FORUM) != forum["id"] or after.get(CHECKPOINT) != (checkpoint or {}).get("id"):
            raise ValueError("Release assets changed during capture")
        base = {"version": 1, "repo": gh.repo, "release": release,
                "forum": forum, "checkpoint": checkpoint}
        (output / "base.json").write_text(json.dumps(base, sort_keys=True) + "\n")
    return base


def unpack(bundle, directory):
    try:
        subprocess.run(["node", str(ROOT / "scripts/questions_checkpoint.mjs"), "unpack",
                        "--input", str(bundle), "--output", str(directory)],
                       check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as error:
        raise ValueError("Checkpoint authentication or unpack failed") from error


def decrypt_forum(encrypted, output):
    if not os.environ.get("DB_KEY", "").strip():
        raise ValueError("DB_KEY is required")
    decrypted = output.with_suffix(".zst")
    try:
        subprocess.run(["openssl", "enc", "-d", "-aes-256-cbc", "-pbkdf2", "-iter", "200000",
                        "-pass", "env:DB_KEY", "-in", str(encrypted), "-out", str(decrypted)],
                       check=True, capture_output=True)
        subprocess.run(["zstd", "-d", "-q", "-o", str(output), str(decrypted)],
                       check=True, capture_output=True)
    except subprocess.CalledProcessError as error:
        raise ValueError("Cannot decrypt the captured forum database") from error


def source_digest(path):
    with closing(questions_preflight.snapshot(path)) as con:
        return corpus.content_hash(con.execute("SELECT * FROM topics ORDER BY id"))


def verify_transition(old, new, model, revision):
    old_state = questions_preflight.verify(old / "source.db", old / "ratings.db", model, revision)
    new_state = questions_preflight.verify(new / "source.db", new / "ratings.db", model, revision)
    with closing(questions_preflight.snapshot(old / "ratings.db")) as before, \
            closing(questions_preflight.snapshot(new / "ratings.db")) as after:
        previous = {r["topic_id"]: r for r in before.execute("SELECT * FROM questions_decisions")}
        updated = {r["topic_id"]: r for r in after.execute("SELECT * FROM questions_decisions")}
        if previous.keys() - updated.keys():
            raise ValueError("Checkpoint removed existing topic IDs")
        protected = ("answers", "decision_input_hash", "decision_model", "model_revision", "decision_version",
                     "evidence_version", "classified_at", "decision_error", "readiness_error",
                     "evidence_budget", "evidence_posts", "elapsed_seconds")
        for topic_id, row in previous.items():
            current = updated[topic_id]
            if (corpus.current(row, model, revision) and row["input_hash"] == current["input_hash"]
                    and any(row[key] != current[key] for key in protected)):
                raise ValueError(f"Checkpoint replaced a current unchanged decision for topic {topic_id}")
        old_attempts = [tuple(row) for row in before.execute("SELECT * FROM questions_attempts ORDER BY id")]
        new_attempts = [tuple(row) for row in after.execute("SELECT * FROM questions_attempts ORDER BY id")]
        if new_attempts[:len(old_attempts)] != old_attempts:
            raise ValueError("Checkpoint changed or removed existing attempt history")
    return old_state, new_state


def commit(gh, release, base_path, candidate, model, revision, seed_sha256=None):
    base = json.loads(Path(base_path).read_text())
    if base.get("version") != 1 or base.get("repo") != gh.repo or base.get("release") != release:
        raise ValueError("Checkpoint capture does not match this repository and release")
    if not isinstance(base.get("forum"), dict) or "checkpoint" not in base:
        raise ValueError("Invalid checkpoint capture")
    directory_parent = private_directory(Path(base_path).resolve().parent)
    with tempfile.TemporaryDirectory(prefix=".questions-checkpoint-", dir=directory_parent) as work:
        directory = Path(work)
        staged = directory / CHECKPOINT
        if Path(candidate).stat().st_size > 513 * 1024 * 1024:
            raise ValueError("Candidate checkpoint exceeds the size limit")
        shutil.copyfile(candidate, staged)
        with gh.lease():
            ids = assets(gh, release)
            forum = download(gh, release, FORUM, directory / "forum", ids)
            current = download(gh, release, CHECKPOINT, directory / "current", ids)
            if forum != base["forum"] or current != base["checkpoint"]:
                raise ValueError("Forum or Questions checkpoint changed since capture")
            if not current:
                if any(name.startswith(BACKUP_PREFIX) for name in ids):
                    raise ValueError("Checkpoint backups exist; recover the previous checkpoint instead of reseeding")
                if not seed_sha256 or seed_sha256 != corpus.file_hash(staged):
                    raise ValueError("Initial seed requires an independently approved checkpoint SHA-256")
            elif seed_sha256 is not None:
                raise ValueError("An initial seed hash is not valid for an existing checkpoint")
            new = directory / "new"
            unpack(staged, new)
            new_state = questions_preflight.verify(new / "source.db", new / "ratings.db", model, revision)
            if current:
                old = directory / "old"
                unpack(directory / "current" / CHECKPOINT, old)
                verify_transition(old, new, model, revision)
            forum_db = directory / "forum.db"
            decrypt_forum(directory / "forum" / FORUM, forum_db)
            if source_digest(forum_db) != new_state["source_content_sha256"]:
                raise ValueError("Candidate source does not match the captured forum database")
            if current:
                backup_name = f"{BACKUP_PREFIX}{current['sha256']}.enc"
                if backup_name not in ids:
                    backup = directory / backup_name
                    shutil.copyfile(directory / "current" / CHECKPOINT, backup)
                    gh.release("upload", release, str(backup))
                backup_ids = assets(gh, release)
                backup = download(gh, release, backup_name, directory / "backup", backup_ids)
                if not backup or backup["sha256"] != current["sha256"] or backup["size"] != current["size"]:
                    raise ValueError("Checkpoint backup does not match")
            refreshed = assets(gh, release)
            if refreshed.get(FORUM) != forum["id"] or refreshed.get(CHECKPOINT) != (current or {}).get("id"):
                raise ValueError("Release assets changed before checkpoint upload")
            if current and refreshed.get(backup_name) != backup["id"]:
                raise ValueError("Checkpoint backup changed before checkpoint upload")
            gh.release("upload", release, str(staged), "--clobber")
            ids = assets(gh, release)
            published = download(gh, release, CHECKPOINT, directory / "published", ids)
            if published["sha256"] != corpus.file_hash(staged):
                raise ValueError("Uploaded checkpoint does not match the verified candidate")
            return {"checkpoint": published, "topics": new_state["topics"], "current": new_state["current"]}


def recover(gh, release, backup_sha256, model, revision, private_dir):
    if len(backup_sha256) != 64 or any(c not in "0123456789abcdef" for c in backup_sha256):
        raise ValueError("Recovery requires an exact lowercase backup SHA-256")
    backup_name = f"{BACKUP_PREFIX}{backup_sha256}.enc"
    with tempfile.TemporaryDirectory(prefix=".questions-recovery-", dir=private_directory(private_dir)) as work, gh.lease():
        directory = Path(work)
        ids = assets(gh, release)
        if CHECKPOINT in ids:
            raise ValueError("Checkpoint exists; recovery will not replace it")
        backup = download(gh, release, backup_name, directory / "backup", ids)
        if not backup or backup["sha256"] != backup_sha256:
            raise ValueError("Requested backup is missing or does not match its SHA-256")
        unpack(directory / "backup" / backup_name, directory / "restored")
        state = questions_preflight.verify(directory / "restored" / "source.db",
                                           directory / "restored" / "ratings.db", model, revision)
        refreshed = assets(gh, release)
        if CHECKPOINT in refreshed or refreshed.get(backup_name) != backup["id"]:
            raise ValueError("Release assets changed before recovery")
        restored = directory / CHECKPOINT
        shutil.copyfile(directory / "backup" / backup_name, restored)
        gh.release("upload", release, str(restored))
        uploaded = download(gh, release, CHECKPOINT, directory / "uploaded", assets(gh, release))
        if not uploaded or uploaded["sha256"] != backup_sha256:
            raise ValueError("Restored checkpoint does not match the backup")
        return {"checkpoint": uploaded, "topics": state["topics"], "current": state["current"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo")
    parser.add_argument("--release", default="data")
    commands = parser.add_subparsers(dest="command", required=True)
    read = commands.add_parser("capture")
    read.add_argument("--output", type=Path, required=True)
    read.add_argument("--initial", action="store_true")
    write = commands.add_parser("commit")
    write.add_argument("--base", type=Path, required=True)
    write.add_argument("--candidate", type=Path, required=True)
    write.add_argument("--model", required=True)
    write.add_argument("--revision", required=True)
    write.add_argument("--seed-sha256", help="Independently approved encrypted checkpoint hash for initial seeding")
    recovery = commands.add_parser("recover")
    recovery.add_argument("--backup-sha256", required=True)
    recovery.add_argument("--model", required=True)
    recovery.add_argument("--revision", required=True)
    recovery.add_argument("--private-dir", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    gh = publish_data.GitHub(args.repo)
    if args.command == "capture":
        result = capture(gh, args.release, args.output, args.initial)
    elif args.command == "commit":
        result = commit(gh, args.release, args.base, args.candidate, args.model, args.revision, args.seed_sha256)
    else:
        result = recover(gh, args.release, args.backup_sha256, args.model, args.revision, args.private_dir)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
