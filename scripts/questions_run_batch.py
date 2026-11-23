#!/usr/bin/env python3
"""Run a bounded private Questions batch with an independently pinned local runtime."""

import argparse
from contextlib import closing, contextmanager
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pipeline/src"))
from fminer import questions_corpus as corpus, questions_preflight as preflight
from questions_checkpoint_store import verify_transition
from questions_transition import copy_private


def runtime_manifest(path, approved_sha256, executable, weights):
    data = Path(path).read_bytes()
    if len(data) > 65536 or hashlib.sha256(data).hexdigest() != approved_sha256:
        raise ValueError("Runtime manifest does not match the independently approved SHA-256")
    manifest = json.loads(data)
    required = {"schema_version", "model", "revision", "server_sha256", "weights_sha256", "prompt", "evidence"}
    if (not isinstance(manifest, dict) or set(manifest) != required
            or manifest["schema_version"] != "questions-runtime-v1"
            or not isinstance(manifest["model"], str) or not manifest["model"].strip()
            or not isinstance(manifest["revision"], str) or not manifest["revision"].strip()
            or manifest["prompt"] != corpus.PROMPT_VERSION or manifest["evidence"] != corpus.EVIDENCE_VERSION):
        raise ValueError("Invalid or incompatible runtime manifest")
    for path, key in ((executable, "server_sha256"), (weights, "weights_sha256")):
        expected = manifest[key]
        if (not isinstance(expected, str) or len(expected) != 64
                or any(c not in "0123456789abcdef" for c in expected)
                or corpus.file_hash(path) != expected):
            raise ValueError(f"Runtime {key} mismatch")
    return manifest


def actionable(ratings):
    with closing(preflight.snapshot(ratings)) as con:
        return con.execute(
            """SELECT COUNT(*) FROM questions_decisions WHERE readiness_error IS NULL AND
               (answers IS NULL OR answers='' OR decision_error IS NOT NULL OR decision_input_hash IS NOT input_hash)"""
        ).fetchone()[0]


@contextmanager
def deadline(seconds):
    if signal.getitimer(signal.ITIMER_REAL)[0]:
        raise RuntimeError("A process timer is already active")
    previous = signal.getsignal(signal.SIGALRM)

    def expired(signum, frame):
        raise TimeoutError("Questions batch time budget expired")

    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


@contextmanager
def server(executable, weights, model, log_path, timeout=300):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    with log_path.open("xb") as log:
        os.fchmod(log.fileno(), 0o600)
        process = subprocess.Popen(
            [str(executable), "-m", str(weights), "--alias", model, "-c", "16384", "-fa", "on",
             "--host", "127.0.0.1", "--port", str(port), "-np", "1"],
            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError(f"Decision server exited; inspect private log {log_path}")
                try:
                    with urllib.request.urlopen(url + "/health", timeout=2) as response:
                        if json.load(response).get("status") == "ok":
                            break
                except (OSError, ValueError, urllib.error.URLError):
                    pass
                time.sleep(1)
            else:
                raise RuntimeError(f"Decision server did not become ready; inspect private log {log_path}")
            yield url
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


def run(source, ratings, runtime, runtime_sha256, executable, weights, output, limit, minutes):
    source, ratings, runtime, executable, weights, output = (
        Path(path).resolve() for path in (source, ratings, runtime, executable, weights, output)
    )
    if not 1 <= limit <= 1000 or not 0 < minutes <= 240:
        raise ValueError("Batch limit must be 1–1000 and budget at most 240 minutes")
    manifest = runtime_manifest(runtime, runtime_sha256, executable, weights)
    original = preflight.verify(source, ratings, manifest["model"], manifest["revision"])
    output.mkdir(mode=0o700)
    previous = output / "previous"
    previous.mkdir(mode=0o700)
    for src, name, expected in ((source, "source.db", original["source_snapshot_sha256"]),
                                (ratings, "ratings.db", original["ratings_snapshot_sha256"])):
        copy_private(src, previous / name)
        copy_private(src, output / name)
        if any(corpus.file_hash(path) != expected for path in (src, previous / name, output / name)):
            raise ValueError("Input snapshot changed while copying")
    failure = None
    try:
        if actionable(output / "ratings.db"):
            with server(executable, weights, manifest["model"], output / "server.log") as url:
                run_classifier(output, url, manifest, limit, minutes)
    except Exception as error:
        failure = error
    runtime_manifest(runtime, runtime_sha256, executable, weights)
    _, result = verify_transition(previous, output, manifest["model"], manifest["revision"])
    if failure:
        raise RuntimeError(f"Batch failed; private checkpoint remains in {output}") from failure
    return {"before": original, "after": result, "output": str(output)}


def run_classifier(output, url, manifest, limit, minutes):
    con = corpus.connect(output / "source.db", output / "ratings.db")
    try:
        with deadline(minutes * 60):
            corpus.classify(con, url, "", manifest["revision"], limit=limit, budget_minutes=minutes,
                            log=lambda _: None, expected_model=manifest["model"])
    except BaseException:
        con.rollback()
        con.execute(
            """UPDATE questions_attempts SET outcome='interrupted',finished_at=?,
               error='batch interrupted; retry is safe' WHERE outcome='running'""", (corpus.now(),))
        con.commit()
        raise
    finally:
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        con.execute("PRAGMA journal_mode=DELETE")
        con.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("source", "ratings", "runtime", "runtime-sha256", "executable", "weights", "output"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--limit", type=int, required=True)
    parser.add_argument("--minutes", type=float, required=True)
    args = vars(parser.parse_args())
    os.umask(0o077)
    print(json.dumps(run(**args), indent=2))


if __name__ == "__main__":
    main()
