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
CREATE TABLE IF NOT EXISTS field_results(
 topic_id INTEGER NOT NULL,field TEXT NOT NULL CHECK(field IN ('title','body')),
 input_hash TEXT NOT NULL,text_sha256 TEXT NOT NULL,
 outcome TEXT NOT NULL CHECK(outcome IN ('pending','held','review_required')),
 output TEXT,audit TEXT,error TEXT,attempts INTEGER NOT NULL DEFAULT 0,
 completed_attempts INTEGER NOT NULL DEFAULT 0,model_calls INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(topic_id,field)
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


def aggregate_record(con, topic_id):
    rows = con.execute("""SELECT * FROM field_results WHERE topic_id=?
                          ORDER BY CASE field WHEN 'title' THEN 0 ELSE 1 END""", (topic_id,)).fetchall()
    if len(rows) != 2:
        raise ValueError("exact title/body result slots required")
    if any(row["outcome"] not in ("pending", "held", "review_required") for row in rows):
        raise ValueError("invalid field outcome")
    outcomes = {row["field"]: row["outcome"] for row in rows}
    outcome = "held" if "held" in outcomes.values() else (
        "pending" if "pending" in outcomes.values() else "review_required")
    audit = {"field_outcomes": outcomes, "field_input_hashes": {row["field"]: row["input_hash"] for row in rows},
             "field_text_sha256": {row["field"]: row["text_sha256"] for row in rows},
             "publication_allowed": False}
    error, fields = None, None
    if outcome == "review_required":
        fields = {row["field"]: row["output"] for row in rows}
        audit.update({row["field"]: json.loads(row["audit"]) for row in rows})
        audit["corrected_fields_sha256"] = privacy.digest(fields)
    elif outcome == "held":
        failed = next(row for row in rows if row["outcome"] == "held")
        error = failed["error"]
        audit.update(failed_field=failed["field"], reason=error)
    con.execute("""UPDATE records SET outcome=?,fields=?,audit=?,error=?,attempts=? WHERE topic_id=?""",
                (outcome, json.dumps(fields, ensure_ascii=False) if fields is not None else None,
                 json.dumps(audit, ensure_ascii=False), error, max(row["attempts"] for row in rows), topic_id))


def proposals(source, candidate_path, candidate_sha, source_content_sha, results, ids, cfg,
              minutes=20, limit=30, detector=privacy.detect, endpoint="http://127.0.0.1:18081/v1", runtime=None,
              api_key_file=None, max_field_attempts=2):
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
    if limit < 1 or minutes <= 0 or type(max_field_attempts) is not int or max_field_attempts < 1:
        raise ValueError("positive local execution bounds required")
    results = Path(results)
    results.mkdir(parents=True, exist_ok=True, mode=0o700)
    if any((results / name).resolve() in (Path(candidate_path).resolve(), Path(source).resolve())
           for name in ("records.db", "progress.json", "field-coverage.json")):
        raise ValueError("output overlaps source/candidate")
    identity = {"candidate_sha256": candidate_sha, "source_content_sha256": source_content_sha,
                "selection_sha256": privacy.digest(selected), "model_prompt_rules": weekly.identity(cfg),
                "runtime": runtime, "coverage_schema": "questions-local-fields-v1"}
    deadline = time.monotonic() + minutes * 60
    with closing(_readonly_snapshot(source)) as src, closing(sqlite3.connect(results / "records.db")) as con:
        (results / "records.db").chmod(0o600)
        con.row_factory = sqlite3.Row
        topics = {row["id"]: row for row in src.execute("SELECT * FROM topics ORDER BY id")}
        if corpus.content_hash(topics.values()) != source_content_sha or set(selected) - topics.keys():
            raise ValueError("source-linked membership/content hash mismatch")
        tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if tables - {"state", "records", "field_results"}:
            raise ValueError("results DB contains unrelated/private source tables")
        previous = con.execute("SELECT value FROM state WHERE key='identity'").fetchone() if "state" in tables else None
        if previous and json.loads(previous[0]) != identity:
            raise ValueError("private review input/model/scope/coverage changed; use new results directory")
        if previous and ("field_results" not in tables
                         or con.execute("SELECT COUNT(*) FROM field_results").fetchone()[0] != len(selected) * 2
                         or con.execute("SELECT COUNT(*) FROM records").fetchone()[0] != len(selected)):
            raise ValueError("durable field coverage slots are missing; restore verified backup")
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=FULL")
        con.executescript(SCHEMA)
        con.execute("INSERT OR REPLACE INTO state VALUES ('identity',?)", (json.dumps(identity),))
        for topic_id in selected:
            fields = {field: required[topic_id][field] for field in ("title", "body")}
            input_hash = privacy.digest([corpus.fingerprint(topics[topic_id]), fields, identity])
            con.execute("INSERT OR IGNORE INTO records(topic_id,input_hash,outcome) VALUES (?,?,'pending')",
                        (topic_id, input_hash))
            if con.execute("SELECT input_hash FROM records WHERE topic_id=?", (topic_id,)).fetchone()[0] != input_hash:
                raise ValueError("record input hash differs")
            for field, text in fields.items():
                text_sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
                field_hash = privacy.digest([input_hash, field, text_sha])
                con.execute("""INSERT OR IGNORE INTO field_results(topic_id,field,input_hash,text_sha256,outcome)
                               VALUES (?,?,?,?,'pending')""", (topic_id, field, field_hash, text_sha))
                saved = con.execute("SELECT input_hash,text_sha256 FROM field_results WHERE topic_id=? AND field=?",
                                    (topic_id, field)).fetchone()
                if tuple(saved) != (field_hash, text_sha):
                    raise ValueError("field input hash differs")
            aggregate_record(con, topic_id)
        if (con.execute("SELECT COUNT(*) FROM records").fetchone()[0] != len(selected)
                or con.execute("SELECT COUNT(*) FROM field_results").fetchone()[0] != len(selected) * 2):
            raise ValueError("field coverage membership differs")
        con.commit() # Two durable field slots per required ID exist before any inference.
        attempted_topics = set()
        for row in con.execute(
                """SELECT topic_id,field,input_hash,text_sha256,outcome FROM field_results
                   WHERE outcome='pending' OR (outcome='held' AND attempts<?)
                   ORDER BY CASE outcome WHEN 'pending' THEN 0 ELSE 1 END, attempts, topic_id,
                            CASE field WHEN 'title' THEN 0 ELSE 1 END""",
                (max_field_attempts,)).fetchall():
            if time.monotonic() >= deadline:
                break
            topic_id, field = row["topic_id"], row["field"]
            if topic_id not in attempted_topics and len(attempted_topics) >= limit:
                continue
            attempted_topics.add(topic_id)
            held_audit = {"input_hash": row["input_hash"], "text_sha256": row["text_sha256"],
                          "reason": "attempt_interrupted",
                          "publication_allowed": False}
            con.execute("""UPDATE field_results SET outcome='held',output=NULL,audit=?,
                           error='attempt_interrupted',attempts=attempts+1 WHERE topic_id=? AND field=?""",
                        (json.dumps(held_audit), topic_id, field))
            aggregate_record(con, topic_id)
            con.commit()
            model_calls = 0
            try:
                posts = split_raw(topics[topic_id]["thread_raw"] or "")
                if not posts or posts[0]["n"] != 1:
                    raise privacy.ProposalError("source_evidence_missing")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise privacy.ProposalError("context_or_time_budget")
                output, audit = detector(
                    required[topic_id][field], posts, endpoint, cfg["redaction_model"],
                    timeout=min(120, remaining), attempts=2, api_key_file=api_key_file,
                    op_username=topics[topic_id]["op_username"])
                model_calls = audit.get("model_calls", 0)
                if privacy.findings(output, posts, topics[topic_id]["op_username"]):
                    raise privacy.ProposalError("residual_identifiers")
                audit.update(input_hash=row["input_hash"], text_sha256=row["text_sha256"],
                             publication_allowed=False)
                con.execute("""UPDATE field_results SET outcome='review_required',output=?,audit=?,error=NULL,
                               completed_attempts=completed_attempts+1,model_calls=model_calls+?
                               WHERE topic_id=? AND field=?""",
                            (output, json.dumps(audit, ensure_ascii=False), audit.get("model_calls", 0), topic_id, field))
            except (ValueError, httpx.HTTPError) as error:
                reason = getattr(error, "reason", "input_or_transport_validation")
                held_audit.update(reason=reason, model_calls=getattr(error, "model_calls", model_calls))
                con.execute("""UPDATE field_results SET outcome='held',output=NULL,audit=?,error=?,
                               completed_attempts=completed_attempts+1,model_calls=model_calls+?
                               WHERE topic_id=? AND field=?""",
                            (json.dumps(held_audit), reason, held_audit["model_calls"], topic_id, field))
            aggregate_record(con, topic_id)
            con.commit()
        counts = dict(con.execute("SELECT outcome,COUNT(*) FROM records GROUP BY outcome"))
        held_reasons = dict(con.execute("SELECT error,COUNT(*) FROM records WHERE outcome='held' GROUP BY error"))
        exhausted = con.execute("SELECT COUNT(DISTINCT topic_id) FROM field_results WHERE outcome='held' AND attempts>=?",
                                (max_field_attempts,)).fetchone()[0]
        field_counts = {field: dict(con.execute("SELECT outcome,COUNT(*) FROM field_results WHERE field=? GROUP BY outcome",
                                               (field,))) for field in ("title", "body")}
        coverage = []
        for row in con.execute("SELECT * FROM field_results ORDER BY topic_id,field"):
            coverage.append({key: row[key] for key in ("topic_id", "field", "input_hash", "text_sha256", "outcome",
                                                      "attempts", "completed_attempts", "model_calls", "error")})
            coverage[-1].update(audit_sha256=privacy.digest(json.loads(row["audit"])) if row["audit"] else None,
                                output_sha256=hashlib.sha256(row["output"].encode()).hexdigest()
                                if row["output"] is not None else None)
        coverage_doc = {"schema": "questions-local-fields-v1", "identity": identity, "fields": coverage,
                        "privacy_cleared": 0, "publication_allowed": False}
        coverage_path = results / "field-coverage.json"
        coverage_path.write_text(json.dumps(coverage_doc, sort_keys=True) + "\n")
        coverage_path.chmod(0o600)
        report = {"version": 1, "identity": identity, "required": len(selected), "counts": counts,
                  "held_reasons": held_reasons, "max_field_attempts": max_field_attempts,
                  "retry_exhausted": exhausted,
                  "field_counts": field_counts, "fields_required": len(coverage),
                  "field_held_reasons": {field: dict(con.execute(
                      "SELECT error,COUNT(*) FROM field_results WHERE field=? AND outcome='held' GROUP BY error",
                      (field,))) for field in ("title", "body")},
                  "fields_completed": sum(row["completed_attempts"] > 0 and row["error"] != "attempt_interrupted"
                                          for row in coverage),
                  "fields_model_attempted": sum(row["model_calls"] > 0 for row in coverage),
                  "unaccounted_field_attempts": sum(row["attempts"] - row["completed_attempts"] for row in coverage),
                  "field_retry_exhausted": sum(row["outcome"] == "held" and row["attempts"] >= max_field_attempts
                                               for row in coverage),
                  "field_coverage_sha256": corpus.file_hash(coverage_path),
                  "proposal_unresolved": counts.get("pending", 0) + counts.get("held", 0),
                  "unresolved": len(selected),
                  "privacy_cleared": 0, "publication_allowed": False}
        (results / "progress.json").write_text(json.dumps(report, sort_keys=True) + "\n")
        (results / "records.db").chmod(0o600)
        (results / "progress.json").chmod(0o600)
    print(json.dumps({key: report[key] for key in ("required", "counts", "field_counts", "fields_required",
                                                 "fields_completed", "fields_model_attempted", "unresolved",
                                                 "proposal_unresolved", "privacy_cleared", "publication_allowed")}))
    return report


def evaluate(fixture_path, fixture_sha, results, cfg, endpoint, runtime=None, detector=privacy.detect,
             api_key_file=None, minutes=20, limit=30):
    if minutes <= 0 or limit < 1:
        raise ValueError("positive held-out execution bounds required")
    fixture = read_verified(fixture_path, fixture_sha)
    if (fixture.get("schema") != "questions-local-pi-gold-v1"
            or not isinstance(fixture.get("cases"), list) or not fixture["cases"]):
        raise ValueError("explicitly labeled evaluation fixture required")
    languages, outcomes, kinds = {}, [], {}
    seen = set()
    deadline, calls, elapsed, attempted = time.monotonic() + minutes * 60, 0, 0, 0
    for case in fixture["cases"]:
        if case["id"] in seen or case["lang"] not in ("en", "fr", "ja"):
            raise ValueError("duplicate held-out ID or unsupported language")
        seen.add(case["id"])
        text, gold = case["text"], case["expected_spans"]
        privacy.apply_spans(text, {"uncertain": False, "spans": gold}) # Validate gold exact offsets too.
        expected, removed, held, collateral, reason = len(gold), 0, False, 0, None
        began = time.monotonic()
        detected = []
        not_run = attempted >= limit or began >= deadline
        try:
            if not_run:
                raise ValueError("bounded evaluation budget exhausted")
            attempted += 1
            _, audit = detector(text, case["posts"], endpoint, cfg["redaction_model"],
                                timeout=min(120, deadline - began), attempts=2, api_key_file=api_key_file,
                                op_username=case.get("op_username"))
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
            reason = "not_run" if not_run else getattr(error, "reason", "input_or_transport_validation")
        elapsed += time.monotonic() - began
        for target, found in zip(gold, detected or [False] * len(gold)):
            counts = kinds.setdefault(case["lang"] + "/" + target["kind"], Counter())
            counts.update(identifiers=1, detected=int(found), missed=int(not found))
        counts = languages.setdefault(case["lang"], Counter())
        counts.update(cases=1, identifiers=expected, detected=removed, missed=expected - removed,
                      held_cases=int(held), not_run=int(not_run), non_pi_characters_redacted=collateral)
        if reason:
            counts["held_reason_" + reason] += 1
        outcomes.append({"id": case["id"], "lang": case["lang"], "expected": expected,
                         "detected": removed, "held": held, "not_run": not_run,
                         "held_reason": reason,
                         "non_pi_characters_redacted": collateral, "seconds": time.monotonic() - began})
    report = {"schema": "questions-local-pi-measurement-v1", "dataset_sha256": fixture_sha,
              "identity": weekly.identity(cfg), "runtime": runtime, "independent_heldout": False,
              "input_independence_asserted": fixture.get("independent_heldout") is True,
              "independence_verified": False, "label_provenance": fixture.get("label_provenance", "unspecified"),
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
    parser.add_argument("--allow-unauthenticated", action="store_true",
                        help="explicit local-only diagnostic exception; private runs require API key by default")
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
    parser.add_argument("--max-field-attempts", "--max-record-attempts", dest="max_field_attempts", type=int, default=2,
                        help="per-field attempt cap (legacy record flag alias); exhausted fields remain held")
    parser.add_argument("--fixture", type=Path)
    parser.add_argument("--fixture-sha256")
    args = parser.parse_args()
    if args.api_key_file is None and not args.allow_unauthenticated:
        parser.error("private local review requires a mode-0600 API-key file")
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
                      api_key_file=args.api_key_file, max_field_attempts=args.max_field_attempts)
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
