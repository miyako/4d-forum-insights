#!/usr/bin/env python3
"""Private incremental Questions checkpoint; never installs or publishes site data."""

import argparse
from collections import Counter
from contextlib import closing
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline/src"))
from fminer import db, enrich, questions, questions_corpus as corpus, questions_privacy as privacy
from fminer.questions_export import WARNING_TEXT, _bool_or_none
from fminer.scrape import split_raw
import publish_data as publisher

DISPLAY_SCHEMA = """
CREATE TABLE IF NOT EXISTS displays (
 topic_id INTEGER PRIMARY KEY, freshness_hash TEXT NOT NULL, source_hash TEXT NOT NULL,
 fields TEXT, display_hash TEXT, audit TEXT, audit_hash TEXT, error TEXT
);
"""


def config(path):
    data = json.loads(Path(path).read_text())
    if data["version"] != 1 or data["context"] != 16384 or data["parallel"] != 1:
        raise ValueError("unsupported weekly model configuration")
    return data


def identity(cfg):
    return privacy.digest([cfg, privacy.PROMPT_VERSION, privacy.RULE_VERSION,
                           corpus.PROMPT_VERSION, corpus.EVIDENCE_VERSION])


def timestamp(value):
    if not isinstance(value, str):
        raise ValueError("missing review timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("review timestamp requires timezone")


def readiness(root, cfg):
    root = Path(root)
    gate = json.loads((root / "readiness.json").read_text())
    evidence_path = root / "evaluation.json"
    evidence = json.loads(evidence_path.read_text())
    if (gate.get("schema") != "questions-weekly-readiness-v1"
            or gate.get("initial_en_fr_ja_published") is not True
            or gate.get("weekly_private_processing_authorized") is not True
            or gate.get("identity") != identity(cfg)
            or gate.get("evaluation_sha256") != corpus.file_hash(evidence_path)
            or not isinstance(gate.get("reviewer"), str) or not gate["reviewer"].strip()):
        raise ValueError("weekly readiness is not authorized for exact model/prompt/rule identity")
    timestamp(gate.get("reviewed_at"))
    if (evidence.get("schema") != "questions-redaction-heldout-v1"
            or evidence.get("identity") != identity(cfg)
            or evidence.get("source_linked") is not True
            or evidence.get("independent_heldout") is not True
            or evidence.get("review_approved") is not True
            or not isinstance(evidence.get("dataset_sha256"), str)
            or not re.fullmatch(r"[a-f0-9]{64}", evidence["dataset_sha256"])):
        raise ValueError("source-linked held-out evaluation is missing")
    # Thresholds are an explicit reviewer policy, not invented model accuracy claims.
    for locale in ("en", "fr", "ja"):
        measured = evidence.get("languages", {}).get(locale, {})
        total, missed = measured.get("identifiers"), measured.get("missed")
        minimum = measured.get("minimum_recall")
        if (type(total) is not int or total <= 0 or type(missed) is not int or not 0 <= missed <= total
                or isinstance(minimum, bool) or not isinstance(minimum, (int, float))
                or not 0 < minimum <= 1 or (total - missed) / total < minimum):
            raise ValueError("held-out language evaluation does not meet reviewed policy")
    with closing(sqlite3.connect((root / "ratings.db").resolve().as_uri() + "?mode=ro", uri=True)) as con:
        runtime = corpus.get_state(con, "runtime")
        if (not runtime or runtime.get("model") != cfg["decision_model"]
                or runtime.get("revision") != cfg["decision_revision"]
                or runtime.get("prompt") != corpus.PROMPT_VERSION or runtime.get("evidence") != corpus.EVIDENCE_VERSION
                or corpus.get_state(con, "scope") != {"scope": "all"} or not corpus.get_state(con, "source")):
            raise ValueError("durable source-backed seed/runtime provenance differs; do not reconstruct or re-rate corpus")
    delta = json.loads((root / "delta-source.json").read_text())
    if (delta.get("schema") != "questions-raw-delta-v1" or not delta.get("baseline")
            or delta.get("initial_baseline_sha256") != gate.get("raw_delta_seed_sha256")):
        raise ValueError("reviewed raw-source delta baseline is missing")
    return gate


def display_connect(path):
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=FULL")
    con.executescript(DISPLAY_SCHEMA)
    return con


def fresh_hash(topic, rating, cfg):
    return privacy.digest([corpus.fingerprint(topic), topic["op_username"], rating["decision_input_hash"],
                           rating["decision_model"], rating["model_revision"],
                           rating["decision_version"], rating["evidence_version"], identity(cfg)])


def proposals(source, root, cfg, endpoint, limit=100, minutes=30, detector=privacy.detect):
    if limit < 1 or minutes <= 0:
        raise ValueError("positive execution bounds required")
    root = Path(root)
    privacy.local_endpoint(endpoint)
    errors = done = held = 0
    deadline = time.monotonic() + minutes * 60
    with closing(corpus.connect(source, root / "ratings.db")) as ratings, \
            closing(display_connect(root / "display.db")) as displays:
        topics = ratings.execute("SELECT * FROM source.topics ORDER BY id").fetchall()
        if corpus.content_hash(topics) != corpus.get_state(ratings, "source")["source_content_sha256"]:
            raise ValueError("source changed since classification preparation")
        for topic in topics:
            rating = ratings.execute("SELECT * FROM questions_decisions WHERE topic_id=?", (topic["id"],)).fetchone()
            old = displays.execute("SELECT * FROM displays WHERE topic_id=?", (topic["id"],)).fetchone()
            if (not rating or corpus.fingerprint(topic) != rating["input_hash"]
                    or not corpus.current(rating, cfg["decision_model"], cfg["decision_revision"])):
                held += 1
                continue
            freshness = fresh_hash(topic, rating, cfg)
            if old and old["freshness_hash"] == freshness and old["fields"] and not old["error"]:
                continue
            if done + errors >= limit or time.monotonic() >= deadline:
                held += 1
                continue
            fields = {"title": topic["title_en"], "body": topic["summary_paragraph"]}
            _, english_fresh = corpus.prepared(topic)
            try:
                if not english_fresh or any(not isinstance(v, str) or not v.strip() for v in fields.values()):
                    raise ValueError("fresh English display unavailable")
                posts = split_raw(topic["thread_raw"])
                audit, cleared = {}, {}
                for field, text in fields.items():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise ValueError("redaction budget exhausted")
                    cleared[field], audit[field] = detector(
                        text, posts, endpoint, cfg["redaction_model"], timeout=min(120, remaining), attempts=2,
                        op_username=topic["op_username"])
                audit.update(source_hash=corpus.fingerprint(topic), freshness_hash=freshness,
                             identity=identity(cfg), review_required=True, publication_allowed=False)
                displays.execute(
                    """INSERT OR REPLACE INTO displays VALUES (?,?,?,?,?,?,?,NULL)""",
                    (topic["id"], freshness, corpus.fingerprint(topic), json.dumps(cleared, ensure_ascii=False),
                     privacy.digest(cleared), json.dumps(audit, ensure_ascii=False), privacy.digest(audit)))
                done += 1
            except ValueError:
                # Invalidate the previous cleared output on failure; retain audit in encrypted DB backup.
                displays.execute("INSERT OR REPLACE INTO displays VALUES (?,?,?,NULL,NULL,NULL,NULL,?)",
                                 (topic["id"], freshness, corpus.fingerprint(topic),
                                  "proposal failed or fresh English missing; held for private review"))
                errors += 1
            displays.commit()
    print(json.dumps({"proposed": done, "held": held, "errors": errors, "publication_allowed": False}))
    if errors:
        raise RuntimeError("Questions proposals held after validation failures; inspect encrypted checkpoint")


def review_matches(row, review):
    return (isinstance(review, dict) and review.get("schema") == "questions-weekly-display-review-v1"
            and all(review.get(k) == row[k] for k in ("source_hash", "freshness_hash", "display_hash", "audit_hash"))
            and review.get("source_linked") is True and review.get("technical_meaning_preserved") is True
            and review.get("privacy_cleared") is True and review.get("openai_authorized") is True
            and isinstance(review.get("reviewer"), str) and bool(review["reviewer"].strip()))


def export_cleared(source, root, cfg, out):
    readiness(root, cfg)
    root, out = Path(root), Path(out)
    reviews = json.loads((root / "reviews.json").read_text())
    if reviews.get("schema") != "questions-weekly-reviews-v1" or not isinstance(reviews.get("entries"), dict):
        raise ValueError("invalid private display reviews")
    entries, docs, held = [], [], 0
    with closing(corpus.connect(source, root / "ratings.db")) as ratings, \
            closing(display_connect(root / "display.db")) as displays:
        for topic in ratings.execute("SELECT * FROM source.topics ORDER BY id"):
            row = displays.execute("SELECT * FROM displays WHERE topic_id=?", (topic["id"],)).fetchone()
            rating = ratings.execute("SELECT * FROM questions_decisions WHERE topic_id=?", (topic["id"],)).fetchone()
            review = reviews["entries"].get(str(topic["id"]))
            if (not row or row["error"] or not row["fields"] or not rating
                    or not corpus.current(rating, cfg["decision_model"], cfg["decision_revision"])
                    or corpus.fingerprint(topic) != rating["input_hash"]
                    or row["freshness_hash"] != fresh_hash(topic, rating, cfg) or not review_matches(row, review)):
                held += 1
                continue
            timestamp(review.get("reviewed_at"))
            fields, audit = json.loads(row["fields"]), json.loads(row["audit"])
            if (set(fields) != {"title", "body"} or any(not isinstance(v, str) for v in fields.values())
                    or privacy.digest(fields) != row["display_hash"] or privacy.digest(audit) != row["audit_hash"]
                    or any(privacy.findings(text, split_raw(topic["thread_raw"]), topic["op_username"])
                           for text in fields.values())):
                raise ValueError("reviewed display failed exact-hash or source-linked guards")
            entries.append({"id": topic["id"], **fields})
            docs.append({"id": topic["id"], "title": fields["title"], "content": fields["body"]})
    out.mkdir(parents=True, exist_ok=True)
    (out / "questions").mkdir(exist_ok=True)
    (out / "questions/entries.json").write_text(json.dumps({"version": 1, "entries": entries}, ensure_ascii=False))
    manifest_path = out / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {"questions": []}
    manifest.update(private_preview=True, publish_allowed=False, questions_view={
        "enabled": False, "semantic_enabled": False, "files": {"entries": "questions/entries.json"}})
    manifest_path.write_text(json.dumps(manifest))
    (out / "embed-input.jsonl").write_text("".join(json.dumps(doc, ensure_ascii=False) + "\n" for doc in docs))
    print(json.dumps({"cleared": len(entries), "held": held, "publication_allowed": False}))


def prepare(source, root):
    """Membership stays complete; unchanged evidence keeps ratings and original selection time."""
    with closing(corpus.connect(source, Path(root) / "ratings.db")) as con:
        previous = corpus.get_state(con, "source")
        report = corpus.prepare(con, source)
        if previous and previous["source_content_sha256"] == report["source_content_sha256"]:
            report["selected_at"] = previous["selected_at"]
            corpus.set_state(con, "source", report)
            con.commit()
    print("Questions source prepared; exact evidence/model/prompt freshness controls reuse")


def raw_fingerprint(row):
    return privacy.digest({key: row[key] for key in ("title", "first_post_raw", "thread_raw")})


def capture_delta(source, root):
    path = Path(root) / "delta-source.json"
    if path.exists():
        return
    with closing(db.connect(source)) as con:
        baseline = {str(row["id"]): raw_fingerprint(row) for row in con.execute("SELECT * FROM topics ORDER BY id")}
    if not baseline:
        raise ValueError("raw delta baseline source is empty")
    path.write_text(json.dumps({"schema": "questions-raw-delta-v1", "baseline": baseline,
                                "initial_baseline_sha256": privacy.digest(baseline), "pending": []}, sort_keys=True))


def enrich_delta(source, root, cfg, endpoint, limit=100, minutes=20):
    path = Path(root) / "delta-source.json"
    state = json.loads(path.read_text())
    if state.get("schema") != "questions-raw-delta-v1":
        raise ValueError("unknown raw delta baseline schema")
    with closing(db.connect(source)) as con:
        rows = con.execute("SELECT * FROM topics ORDER BY id").fetchall()
        baseline = {str(row["id"]): raw_fingerprint(row) for row in rows}
        if set(state["baseline"]) - baseline.keys():
            raise ValueError("raw delta source lost reviewed membership")
        pending = set(state["pending"]) | {int(key) for key, value in baseline.items()
                                           if state["baseline"].get(key) != value}
        state.update(baseline=baseline, pending=sorted(pending))
        path.write_text(json.dumps(state, sort_keys=True)) # Persist pending IDs before any model work.
        before = {row["id"]: row["enriched_at"] for row in rows}
        try:
            if pending:
                enrich.run(con, endpoint, cfg["redaction_model"], workers=1, limit=limit, redo=True,
                           budget_minutes=minutes, topic_ids=sorted(pending))
        finally:
            for row in con.execute("SELECT * FROM topics ORDER BY id"):
                if (row["id"] in pending and row["enriched_at"] != before[row["id"]]
                        and row["enriched_at"] and not row["enrich_error"] and row["first_post_en"]):
                    pending.remove(row["id"])
            state["pending"] = sorted(pending)
            path.write_text(json.dumps(state, sort_keys=True))
        print(json.dumps({"raw_delta_enrichment_pending": len(pending)}))
        if any(row["enrich_error"] for row in con.execute("SELECT * FROM topics") if row["id"] in pending):
            raise RuntimeError("local delta enrichment failed; pending records retained")


def status(source, root, cfg):
    root = Path(root)
    reviews = json.loads((root / "reviews.json").read_text())["entries"]
    overlays = {locale: json.loads((root / f"locales/{locale}/questions.json").read_text())["entries"]
                if (root / f"locales/{locale}/questions.json").is_file() else {} for locale in ("fr", "ja")}
    counts, languages = Counter(), {}
    with closing(corpus.connect(source, root / "ratings.db")) as ratings, \
            closing(display_connect(root / "display.db")) as displays:
        for topic in ratings.execute("SELECT * FROM source.topics ORDER BY id"):
            rating = ratings.execute("SELECT * FROM questions_decisions WHERE topic_id=?", (topic["id"],)).fetchone()
            row = displays.execute("SELECT * FROM displays WHERE topic_id=?", (topic["id"],)).fetchone()
            if not rating:
                state = "decision_missing"
            elif rating["readiness_error"]:
                state = "evidence_unready"
            elif rating["decision_error"]:
                state = "decision_error"
            elif (corpus.fingerprint(topic) != rating["input_hash"]
                  or not corpus.current(rating, cfg["decision_model"], cfg["decision_revision"])):
                state = "decision_stale"
            elif not row:
                state = "display_missing"
            elif row["freshness_hash"] != fresh_hash(topic, rating, cfg):
                state = "display_stale"
            elif row["error"] or not row["fields"]:
                state = "display_error"
            elif not review_matches(row, reviews.get(str(topic["id"]))):
                state = "display_unreviewed"
            else:
                fields, audit = json.loads(row["fields"]), json.loads(row["audit"])
                review = reviews[str(topic["id"])]
                try:
                    timestamp(review.get("reviewed_at"))
                    if (privacy.digest(fields) != row["display_hash"] or privacy.digest(audit) != row["audit_hash"]
                            or any(privacy.findings(text, split_raw(topic["thread_raw"]), topic["op_username"])
                                   for text in fields.values())):
                        raise ValueError("reviewed output failed guards")
                    state = "display_cleared"
                except ValueError:
                    state = "display_review_invalid"
                if state == "display_cleared":
                    for locale in ("fr", "ja"):
                        entry = overlays[locale].get(str(topic["id"]))
                        current = bool(entry and entry.get("source_hash") == privacy.digest(fields)
                                       and entry.get("translation_version") == "1")
                        counts[f"{locale}_current" if current else f"{locale}_missing_or_stale"] += 1
            counts[state] += 1
            languages.setdefault(topic["lang"] or "unknown", Counter())[state] += 1
    total = sum(sum(counter.values()) for counter in languages.values())
    report = {"version": 1, "topics": total, "counts": dict(counts),
              "languages": {lang: dict(counter) for lang, counter in languages.items()},
              "unresolved": total - counts["display_cleared"], "publication_allowed": False}
    (root / "status.json").write_text(json.dumps(report, sort_keys=True) + "\n")
    print(json.dumps(report, sort_keys=True))
    return report


def candidate(source, root, cfg):
    """Construct a private publication-review bundle; never infer publication permission."""
    root = Path(root)
    export_cleared(source, root, cfg, root / "embedding")
    display = json.loads((root / "embedding/questions/entries.json").read_text())
    fields = {row["id"]: row for row in display["entries"]}
    entries, bindings = [], []
    with closing(corpus.connect(source, root / "ratings.db")) as ratings, \
            closing(display_connect(root / "display.db")) as displays:
        source_state = corpus.get_state(ratings, "source")
        topics = ratings.execute("SELECT * FROM source.topics ORDER BY id").fetchall()
        if corpus.content_hash(topics) != source_state["source_content_sha256"]:
            raise ValueError("publication source changed since preparation")
        if set(fields) != {topic["id"] for topic in topics}:
            raise RuntimeError("publication held: source/decision/display/privacy review is incomplete")
        for topic in topics:
            rating = ratings.execute("SELECT * FROM questions_decisions WHERE topic_id=?", (topic["id"],)).fetchone()
            row = displays.execute("SELECT * FROM displays WHERE topic_id=?", (topic["id"],)).fetchone()
            warnings = json.loads(rating["evidence_warnings"])
            entry = {**fields[topic["id"]], "active": topic["last_posted_at"] or topic["bumped_at"] or topic["created_at"],
                     "lang": rating["lang"], "accepted": _bool_or_none(rating["solved"]),
                     "closed": _bool_or_none(rating["closed"]), "replies": rating["replies"],
                     "answers": questions.display_answers(json.loads(rating["answers"])),
                     "evidence_warnings": [WARNING_TEXT[w] for w in warnings]}
            entries.append(entry)
            bindings.append({key: row[key] for key in
                             ("topic_id", "source_hash", "freshness_hash", "display_hash", "audit_hash")})
    directory = root / "publication"
    directory.mkdir(exist_ok=True)
    public = {
        "schema_version": "questions-ui-public-complete-v1", "version": 1,
        "private_preview": False, "public_preview": True, "publish_allowed": True,
        "representative": False, "snapshot_scope": "complete", "corpus_complete": True,
        "processed_count": len(entries), "corpus_total": len(entries),
        "eligible_question_count": sum(e["answers"]["has_question"]["noul"] >= 0.5 for e in entries),
        "generated_at": source_state["selected_at"], "source_selected_at": source_state["selected_at"],
        "entries": entries,
    }
    private = {**public, "schema_version": "questions-ui-private-preview-v1", "snapshot_scope": "processed",
               "private_preview": True, "public_preview": False, "publish_allowed": False}
    audit = {"schema_version": "questions-export-private-audit-v1", "identity": identity(cfg),
             "source_content_sha256": source_state["source_content_sha256"],
             "source_selected_at": source_state["selected_at"], "display_bindings": bindings,
             "processed_count": len(entries), "corpus_total": len(entries), "corpus_complete": True,
             "eligible_question_count": public["eligible_question_count"],
             "privacy_review_required": True, "publication_authorized": False}
    private_bytes = (json.dumps(private, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n").encode()
    audit["candidate_sha256"] = hashlib.sha256(private_bytes).hexdigest()
    for name, data in (("candidate.json", private), ("audit.json", audit), ("public-preview.json", public)):
        (directory / name).write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n")
        (directory / name).chmod(0o600)
    documents = {f"data/locales/{locale}/questions.json": publisher.validate_overlay(
        json.loads((root / f"locales/{locale}/questions.json").read_text())) for locale in ("fr", "ja")}
    publisher.public_questions_overlays(documents, public, require_complete=True)
    for locale in ("fr", "ja"):
        path = root / f"portable-locales/{locale}/questions.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(documents[f"data/locales/{locale}/questions.json"], ensure_ascii=False) + "\n")
        path.chmod(0o600)
    print("Complete weekly candidate prepared privately; exact-byte translated-field/publication review still required")


def backup(source, target):
    source, target = Path(source).resolve(), Path(target).resolve()
    if source == target or target.exists():
        raise ValueError("backup target must be a new independent path")
    target.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as original, \
            closing(sqlite3.connect(target)) as copy:
        original.backup(copy)
        if copy.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise ValueError("backup integrity failed")
    os.chmod(target, 0o600)
    target.with_suffix(target.suffix + ".sha256").write_text(corpus.file_hash(target) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("readiness", "prepare", "propose", "export", "candidate", "backup",
                                          "status", "capture-delta", "enrich-delta"))
    parser.add_argument("--config", default="config/questions-weekly.json")
    parser.add_argument("--root", default="build/questions-checkpoint")
    parser.add_argument("--source", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--base-url", default="http://127.0.0.1:18081/v1")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--minutes", type=float, default=30)
    args = parser.parse_args()
    cfg = config(args.config)
    if args.command == "backup":
        backup(args.source, args.out)
    elif args.command == "readiness":
        readiness(args.root, cfg)
        print("Weekly private-processing readiness verified; publication remains disabled")
    elif args.command == "propose":
        proposals(args.source, args.root, cfg, args.base_url, args.limit, args.minutes)
    elif args.command == "prepare":
        prepare(args.source, args.root)
    elif args.command == "candidate":
        candidate(args.source, args.root, cfg)
    elif args.command == "status":
        status(args.source, args.root, cfg)
    elif args.command == "capture-delta":
        capture_delta(args.source, args.root)
    elif args.command == "enrich-delta":
        enrich_delta(args.source, args.root, cfg, args.base_url, args.limit, args.minutes)
    else:
        export_cleared(args.source, args.root, cfg, args.out)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError, OSError, sqlite3.Error, KeyError):
        sys.exit("Weekly Questions stopped; private readiness/provenance/validation requires review")
