#!/usr/bin/env python3
"""Encrypted private checkpoint storage, called only inside the shared publication lease."""

from contextlib import closing
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import sqlite3
import subprocess
import sys
import tarfile
import tempfile

ASSET = "questions-weekly.tar.zst.enc"
FILES = {"ratings.db", "display.db", "readiness.json", "evaluation.json", "reviews.json", "translation-reviews.json",
         "publication-review.json", "ui-publication-review.json", "status.json", "delta-source.json"}
PREFIXES = ("locales/", "portable-locales/", "embedding/", "publication/")
BASELINE_FILES = {"ratings.db", "delta-source.json"}
RUNNABLE_FILES = BASELINE_FILES | {"display.db", "readiness.json", "evaluation.json", "reviews.json"}


def allowed(name):
    path = PurePosixPath(name)
    return (not path.is_absolute() and ".." not in path.parts and name == str(path)
            and (name in FILES or name.startswith(PREFIXES))
            and not name.endswith(("-wal", "-shm", ".lock", ".tmp")))


def run(*args, **kwargs):
    return subprocess.run(args, check=True, capture_output=True, **kwargs)


def checksum(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def pack(root, directory, *, required=RUNNABLE_FILES):
    root, directory = Path(root), Path(directory)
    staged = directory / "staged"
    staged.mkdir()
    hashes = {}
    for source in sorted(root.rglob("*")):
        name = source.relative_to(root).as_posix()
        if source.is_symlink():
            raise ValueError("checkpoint contains symlink")
        if not source.is_file():
            continue
        if name in {"ratings.db-wal", "ratings.db-shm", "display.db-wal", "display.db-shm", "ratings.db.lock"}:
            continue # SQLite backup below incorporates WAL; transient lock files are not state.
        if not allowed(name):
            raise ValueError("checkpoint contains unexpected file")
        target = staged / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if name.endswith(".db"):
            with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as original, \
                    closing(sqlite3.connect(target)) as copy:
                original.backup(copy)
                if copy.execute("PRAGMA journal_mode=DELETE").fetchone() != ("delete",):
                    raise ValueError("checkpoint SQLite copy must be standalone")
                if copy.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                    raise ValueError("checkpoint SQLite integrity failed")
        else:
            target.write_bytes(source.read_bytes())
        hashes[name] = checksum(target)
    if not required <= hashes.keys():
        raise ValueError("checkpoint is missing required files")
    (staged / "checksums.json").write_text(json.dumps(hashes, sort_keys=True))
    archive = directory / "checkpoint.tar"
    with tarfile.open(archive, "w") as tar:
        for name in sorted({*hashes, "checksums.json"}):
            tar.add(staged / name, arcname=name, recursive=False)
    return archive


def unpack(archive, root):
    root = Path(root)
    if root.exists():
        raise ValueError("refusing to replace existing checkpoint directory")
    root.mkdir(parents=True, mode=0o700)
    seen, total = set(), 0
    with tarfile.open(archive) as tar:
        for member in tar:
            if (not member.isfile() or member.name in seen
                    or (member.name != "checksums.json" and not allowed(member.name))):
                raise ValueError("unsafe private checkpoint archive")
            seen.add(member.name)
            total += member.size
            if total > 2_000_000_000:
                raise ValueError("checkpoint archive exceeds size bound")
            target = root / member.name
            target.parent.mkdir(parents=True, exist_ok=True)
            with tar.extractfile(member) as source, target.open("xb") as output:
                while block := source.read(1024 * 1024):
                    output.write(block)
            target.chmod(0o600)
    hashes = json.loads((root / "checksums.json").read_text())
    if set(hashes) != seen - {"checksums.json"}:
        raise ValueError("checkpoint checksum membership mismatch")
    for name, expected in hashes.items():
        if not allowed(name) or checksum(root / name) != expected:
            raise ValueError("checkpoint checksum mismatch")
        if name.endswith(".db"):
            with closing(sqlite3.connect((root / name).resolve().as_uri() + "?mode=ro", uri=True)) as con:
                if con.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                    raise ValueError("restored checkpoint SQLite integrity failed")
    (root / "checksums.json").unlink()


def encryption():
    if not os.environ.get("DB_KEY"):
        raise ValueError("checkpoint encryption requires DB_KEY")
    return ("openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-iter", "200000", "-pass", "env:DB_KEY")


def encrypt(archive, output):
    compressed = str(archive) + ".zst"
    run("zstd", "-q", "-T2", str(archive), "-o", compressed)
    run(*encryption(), "-salt", "-in", compressed, "-out", str(output))


def restore(encrypted, root, directory):
    compressed = directory / "restored.tar.zst"
    archive = directory / "restored.tar"
    run(*encryption(), "-d", "-in", str(encrypted), "-out", str(compressed))
    run("zstd", "-d", "-q", str(compressed), "-o", str(archive))
    unpack(archive, root)


def backup(root, output):
    output = Path(output)
    with tempfile.TemporaryDirectory(prefix=".questions-backup-", dir=output.parent) as temp:
        directory = Path(temp)
        encrypted = directory / ASSET
        encrypt(pack(root, directory, required=BASELINE_FILES), encrypted)
        restore(encrypted, directory / "verified", directory)
        encrypted.chmod(0o600)
        os.link(encrypted, output)  # Publish locally without ever replacing an existing backup.


def main():
    os.umask(0o077)
    if len(sys.argv) == 4 and sys.argv[1] == "backup":
        backup(sys.argv[2], sys.argv[3])
        print("Local Questions backup encrypted and restore verified; no workflow activation")
        return
    if len(sys.argv) != 3 or sys.argv[1] not in ("pull", "push"):
        raise ValueError("usage: questions_weekly_store.py pull|push CHECKPOINT_DIRECTORY | backup CHECKPOINT_DIRECTORY ARCHIVE")
    if os.environ.get("DATA_PUBLICATION_CHILD") != "1" or not os.environ.get("DB_KEY"):
        raise ValueError("checkpoint storage requires DB_KEY and shared publication lease")
    command, root = sys.argv[1], Path(sys.argv[2])
    with tempfile.TemporaryDirectory(prefix="questions-store-", dir=os.environ.get("RUNNER_TEMP")) as temp:
        directory = Path(temp)
        encrypted = directory / ASSET
        if command == "pull":
            run("gh", "release", "download", "data", "--pattern", ASSET, "--dir", str(directory))
            restore(encrypted, root, directory)
        else:
            encrypt(pack(root, directory), encrypted)
            previous = directory / "previous"
            previous.mkdir()
            # A missing seed is a blocker, not permission to replace/reconstruct private results.
            run("gh", "release", "download", "data", "--pattern", ASSET, "--dir", str(previous))
            old = previous / "questions-weekly.prev.tar.zst.enc"
            (previous / ASSET).rename(old)
            run("gh", "release", "upload", "data", str(old), "--clobber")
            run("gh", "release", "upload", "data", str(encrypted), "--clobber")
    print(f"Private Questions checkpoint {command} verified (encrypted asset only)")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, sqlite3.Error, subprocess.CalledProcessError, tarfile.TarError):
        sys.exit("Encrypted Questions checkpoint operation failed; inspect lease/seed/integrity without exposing contents")
