#!/usr/bin/env python3
"""On-device, source-linked span proposals/evaluation. Never grants publication clearance."""

import argparse
from collections import Counter
from contextlib import closing
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shlex
import sqlite3
import subprocess
import sys
import time
import uuid
from urllib.parse import urlparse

import httpx

import questions_weekly as weekly
from fminer import questions_corpus as corpus, questions_privacy as privacy
from fminer.questions_export import _readonly_snapshot
from fminer.scrape import split_raw

SCHEMA = """
CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS records(
 topic_id INTEGER PRIMARY KEY,input_hash TEXT NOT NULL,outcome TEXT NOT NULL,
 fields TEXT,audit TEXT,error TEXT,attempts INTEGER NOT NULL DEFAULT 0
);
"""


def read_verified(path, expected):
    if not expected or corpus.file_hash(path) != expected:
        raise ValueError("input byte hash mismatch")
    return json.loads(Path(path).read_text())


def verify_server(cfg, model_file, binary, pid, endpoint, api_key_file=None):
    privacy.local_endpoint(endpoint)
    model_file, binary = Path(model_file).resolve(strict=True), Path(binary).resolve(strict=True)
    if corpus.file_hash(model_file) != cfg["redaction_sha256"]:
        raise ValueError("Gemma weight SHA256 mismatch; no model substitution allowed")
    version = subprocess.run([str(binary), "--version"], capture_output=True, text=True, check=True)
    if "11669" not in version.stdout + version.stderr or "620d2225" not in version.stdout + version.stderr:
        raise ValueError("verified llama.cpp build11669 runtime required")
    process = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True, check=True)
    argv = shlex.split(process.stdout.strip())
    if not argv or Path(argv[0]).resolve() != binary:
        raise ValueError("server PID is not the verified local llama-server")
    expected = {"-m": str(model_file), "--alias": cfg["redaction_model"], "--host": "127.0.0.1",
                "--port": str(urlparse(endpoint).port), "-c": str(cfg["context"]), "-np": "1"}
    for flag, value in expected.items():
        if argv.count(flag) != 1 or argv[argv.index(flag) + 1] != value:
            raise ValueError("owned server model/alias/context/slot/port differs")
    if not {"--offline", "--no-webui", "--log-disable"} <= set(argv):
        raise ValueError("owned server must disable network fetching, web UI and logging")
    if api_key_file and ("--api-key-file" not in argv
                         or argv[argv.index("--api-key-file") + 1] != str(api_key_file)):
        raise ValueError("owned server API-key-file differs")
    with httpx.Client(timeout=5, trust_env=False, follow_redirects=False,
                      headers=privacy.private_headers(api_key_file)) as client:
        response = client.get(endpoint.rstrip("/") + "/models")
        response.raise_for_status()
        if response.json()["data"][0]["id"] != cfg["redaction_model"]:
            raise ValueError("local endpoint is not the owned Gemma alias")
    return {"binary_sha256": corpus.file_hash(binary), "model_sha256": cfg["redaction_sha256"],
            "model_revision": cfg["redaction_hf_revision"], "runtime": "build11669/620d2225",
            "context": cfg["context"], "slots": 1}


def backup_inputs(source, candidate, directory):
    directory = Path(directory).resolve()
    if directory.is_relative_to(Path(__file__).resolve().parents[1]) or "copilot-worktrees" in directory.parts:
        raise ValueError("independent durable backup directory must be outside disposable worktrees")
    directory.mkdir(parents=True, exist_ok=True)
    source_target = directory / ("source-" + corpus.file_hash(source)[:16] + ".db")
    if not source_target.exists():
        weekly.backup(source, source_target)
    with closing(_readonly_snapshot(source_target)) as con:
        if con.execute("PRAGMA integrity_check").fetchall()[0][0] != "ok":
            raise ValueError("independent backup failed integrity")
    candidate_target = directory / ("candidate-" + corpus.file_hash(candidate)[:16] + ".json")
    if not candidate_target.exists():
        candidate_target.write_bytes(Path(candidate).read_bytes())
        candidate_target.chmod(0o600)
    if corpus.file_hash(candidate_target) != corpus.file_hash(candidate):
        raise ValueError("independent candidate backup mismatch")
    return source_target


def proposals(source, candidate_path, candidate_sha, source_content_sha, results, ids, cfg,
              minutes=20, limit=30, detector=privacy.detect, endpoint="http://127.0.0.1:18081/v1", runtime=None,
              api_key_file=None):
    candidate = read_verified(candidate_path, candidate_sha)
    if candidate.get("private_preview") is not True or candidate.get("publish_allowed") is not False:
        raise ValueError("existing candidate must remain private and review-required")
    required = {}
    for row in candidate["entries"]:
        topic_id = row["id"]
        if type(topic_id) is not int or topic_id <= 0 or topic_id in required \
                or any(not isinstance(row.get(field), str) for field in ("title", "body")):
            raise ValueError("invalid or duplicate candidate display record")
        required[topic_id] = row
    selected = sorted(required if ids is None else ids)
    if not selected or any(type(topic_id) is not int for topic_id in selected) \
            or len(selected) != len(set(selected)) or set(selected) - required.keys():
        raise ValueError("required ID scope is empty/duplicate/outside candidate")
    if limit < 1 or minutes <= 0:
        raise ValueError("positive local execution bounds required")
    results = Path(results)
    results.mkdir(parents=True, exist_ok=True, mode=0o700)
    if any((results / name).resolve() in (Path(candidate_path).resolve(), Path(source).resolve())
           for name in ("records.db", "progress.json")):
        raise ValueError("output overlaps source/candidate")
    identity = {"candidate_sha256": candidate_sha, "source_content_sha256": source_content_sha,
                "selection_sha256": privacy.digest(selected), "model_prompt_rules": weekly.identity(cfg),
                "runtime": runtime}
    deadline = time.monotonic() + minutes * 60
    with closing(_readonly_snapshot(source)) as src, closing(sqlite3.connect(results / "records.db")) as con:
        topics = {row["id"]: row for row in src.execute("SELECT * FROM topics ORDER BY id")}
        if corpus.content_hash(topics.values()) != source_content_sha or set(selected) - topics.keys():
            raise ValueError("source-linked membership/content hash mismatch")
        if {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")} - {"state", "records"}:
            raise ValueError("results DB contains unrelated/private source tables")
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=FULL")
        con.executescript(SCHEMA)
        previous = con.execute("SELECT value FROM state WHERE key='identity'").fetchone()
        if previous and json.loads(previous[0]) != identity:
            raise ValueError("private review input/model/scope changed; use new results directory")
        con.execute("INSERT OR REPLACE INTO state VALUES ('identity',?)", (json.dumps(identity),))
        for topic_id in selected:
            fields = {field: required[topic_id][field] for field in ("title", "body")}
            input_hash = privacy.digest([corpus.fingerprint(topics[topic_id]), fields, identity])
            con.execute("INSERT OR IGNORE INTO records(topic_id,input_hash,outcome) VALUES (?,?,'pending')",
                        (topic_id, input_hash))
        con.commit() # One durable result slot per required ID exists before any inference.
        done = 0
        for topic_id, input_hash, outcome in con.execute(
                "SELECT topic_id,input_hash,outcome FROM records ORDER BY topic_id").fetchall():
            if outcome == "review_required":
                continue
            if done >= limit or time.monotonic() >= deadline:
                break
            done += 1
            fields, audit = {}, {}
            try:
                posts = split_raw(topics[topic_id]["thread_raw"] or "")
                if not posts or posts[0]["n"] != 1:
                    raise ValueError("source identity evidence is missing")
                for field in ("title", "body"):
                    text = required[topic_id][field]
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise ValueError("bounded local budget exhausted")
                    fields[field], audit[field] = detector(
                        text, posts, endpoint, cfg["redaction_model"], timeout=min(120, remaining), attempts=2,
                        api_key_file=api_key_file)
                    if privacy.findings(fields[field], posts):
                        raise ValueError("source-linked identifiers remain")
                audit.update(input_hash=input_hash, original_fields_sha256=privacy.digest(
                    {field: required[topic_id][field] for field in ("title", "body")}),
                    corrected_fields_sha256=privacy.digest(fields), publication_allowed=False)
                con.execute("""UPDATE records SET outcome='review_required',fields=?,audit=?,error=NULL,
                               attempts=attempts+1 WHERE topic_id=?""",
                            (json.dumps(fields, ensure_ascii=False), json.dumps(audit, ensure_ascii=False), topic_id))
            except (ValueError, httpx.HTTPError):
                con.execute("""UPDATE records SET outcome='held',fields=NULL,audit=NULL,
                               error='local response/source validation failed',attempts=attempts+1 WHERE topic_id=?""",
                            (topic_id,))
            con.commit()
        counts = dict(con.execute("SELECT outcome,COUNT(*) FROM records GROUP BY outcome"))
        report = {"version": 1, "identity": identity, "required": len(selected), "counts": counts,
                  "proposal_unresolved": counts.get("pending", 0) + counts.get("held", 0),
                  "unresolved": len(selected),
                  "privacy_cleared": 0, "publication_allowed": False}
        (results / "progress.json").write_text(json.dumps(report, sort_keys=True) + "\n")
        (results / "records.db").chmod(0o600)
        (results / "progress.json").chmod(0o600)
    print(json.dumps({key: report[key] for key in ("required", "counts", "unresolved", "proposal_unresolved", "privacy_cleared",
                                                 "publication_allowed")}))
    return report


def evaluate(fixture_path, fixture_sha, results, cfg, endpoint, runtime=None, detector=privacy.detect,
             api_key_file=None, minutes=20, limit=30):
    if minutes <= 0 or limit < 1:
        raise ValueError("positive held-out execution bounds required")
    fixture = read_verified(fixture_path, fixture_sha)
    if (fixture.get("schema") != "questions-local-pi-gold-v1" or fixture.get("independent_heldout") is not True
            or not isinstance(fixture.get("cases"), list) or not fixture["cases"]):
        raise ValueError("independently labeled held-out fixture required")
    languages, outcomes, kinds = {}, [], {}
    seen = set()
    deadline, calls, elapsed, attempted = time.monotonic() + minutes * 60, 0, 0, 0
    for case in fixture["cases"]:
        if case["id"] in seen or case["lang"] not in ("en", "fr", "ja"):
            raise ValueError("duplicate held-out ID or unsupported language")
        seen.add(case["id"])
        text, gold = case["text"], case["expected_spans"]
        privacy.apply_spans(text, {"uncertain": False, "spans": gold}) # Validate gold exact offsets too.
        expected, removed, held, collateral = len(gold), 0, False, 0
        began = time.monotonic()
        detected = []
        not_run = attempted >= limit or began >= deadline
        try:
            if not_run:
                raise ValueError("bounded evaluation budget exhausted")
            attempted += 1
            _, audit = detector(text, case["posts"], endpoint, cfg["redaction_model"],
                                timeout=min(120, deadline - began), attempts=2, api_key_file=api_key_file)
            calls += audit.get("model_calls", 1)
            spans = audit["spans"]
            removed_indices = {i for span in spans for i in range(span["start"], span["end"])}
            detected = [all(i in removed_indices for i in range(target["start"], target["end"])) for target in gold]
            removed = sum(detected)
            covered = {i for target in gold for i in range(target["start"], target["end"])}
            collateral = len(removed_indices - covered)
        except (ValueError, httpx.HTTPError) as error:
            calls += getattr(error, "model_calls", 0)
            held = True # Conservative: failed/uncertain proposals count as missed labels, not recalled PI.
        elapsed += time.monotonic() - began
        for target, found in zip(gold, detected or [False] * len(gold)):
            counts = kinds.setdefault(case["lang"] + "/" + target["kind"], Counter())
            counts.update(identifiers=1, detected=int(found), missed=int(not found))
        counts = languages.setdefault(case["lang"], Counter())
        counts.update(cases=1, identifiers=expected, detected=removed, missed=expected - removed,
                      held_cases=int(held), not_run=int(not_run), non_pi_characters_redacted=collateral)
        outcomes.append({"id": case["id"], "lang": case["lang"], "expected": expected,
                         "detected": removed, "held": held, "not_run": not_run,
                         "non_pi_characters_redacted": collateral, "seconds": time.monotonic() - began})
    report = {"schema": "questions-local-pi-measurement-v1", "dataset_sha256": fixture_sha,
              "identity": weekly.identity(cfg), "runtime": runtime, "independent_heldout": True,
              "languages": {lang: dict(counts) for lang, counts in languages.items()},
              "span_kinds": {kind: dict(counts) for kind, counts in kinds.items()},
              "model_calls": calls, "seconds": elapsed, "fixture_scope": fixture.get("scope", "unspecified"),
              "outcomes": outcomes, "review_approved": False, "publication_allowed": False}
    results = Path(results)
    results.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = results / "evaluation.json"
    if path.exists():
        raise ValueError("evaluation output exists; preserve it and use a new directory")
    path.write_text(json.dumps(report, sort_keys=True) + "\n")
    path.chmod(0o600)
    print(json.dumps({"languages": report["languages"], "review_approved": False, "publication_allowed": False}))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("propose", "evaluate"))
    parser.add_argument("--config", default="config/questions-weekly.json")
    parser.add_argument("--model-file", type=Path, required=True)
    parser.add_argument("--llama-server", type=Path, required=True)
    parser.add_argument("--server-pid", type=int, required=True)
    parser.add_argument("--api-key-file", type=Path)
    parser.add_argument("--base-url", default="http://127.0.0.1:18081/v1")
    parser.add_argument("--source", type=Path)
    parser.add_argument("--source-content-sha256")
    parser.add_argument("--candidate", type=Path)
    parser.add_argument("--candidate-sha256")
    parser.add_argument("--ids", type=Path, help="JSON array of required IDs; default all candidate entries")
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument("--minutes", type=float, default=20)
    parser.add_argument("--fixture", type=Path)
    parser.add_argument("--fixture-sha256")
    args = parser.parse_args()
    cfg = weekly.config(args.config)
    runtime = verify_server(cfg, args.model_file, args.llama_server, args.server_pid, args.base_url, args.api_key_file)
    if args.command == "propose":
        if not all((args.source, args.candidate, args.backup_dir, args.source_content_sha256, args.candidate_sha256)):
            parser.error("propose requires exact source/candidate hashes and independent durable backup directory")
        read_verified(args.candidate, args.candidate_sha256)
        args.out.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (args.out / "records.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            source = backup_inputs(args.source, args.candidate, args.backup_dir)
            if (args.out / "records.db").exists():
                weekly.backup(args.out / "records.db", args.backup_dir / ("records-" + uuid.uuid4().hex + ".db"))
            ids = json.loads(args.ids.read_text()) if args.ids else None
            proposals(source, args.candidate, args.candidate_sha256, args.source_content_sha256,
                      args.out, ids, cfg, args.minutes, args.limit, endpoint=args.base_url, runtime=runtime,
                      api_key_file=args.api_key_file)
    else:
        evaluate(args.fixture, args.fixture_sha256, args.out, cfg, args.base_url, runtime,
                 api_key_file=args.api_key_file, minutes=args.minutes, limit=args.limit)


if __name__ == "__main__":
    os.umask(0o077)
    try:
        main()
    except (ValueError, RuntimeError, OSError, KeyError, TypeError, IndexError, sqlite3.Error,
            httpx.HTTPError, subprocess.CalledProcessError):
        sys.exit("Local privacy run stopped; private input/runtime/hash/coverage validation failed")
