"""Create a private, review-required Questions candidate from offline SQLite snapshots."""

import argparse
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3

from . import questions, questions_corpus as corpus
from .scrape import split_raw
from .voices import _designature, forbidden_names

WARNING_TEXT = {
    "code replaced by placeholders; code-dependent understanding is limited":
        "Code-dependent understanding is limited because code was replaced by placeholders during classification.",
    "some listed posts unavailable": "Some listed posts were unavailable in the source snapshot.",
    "fresh English opening unavailable; using original-language opening":
        "Fresh English opening was unavailable during classification.",
    "source fetch error recorded; available evidence may be incomplete":
        "A source fetch error was recorded; available evidence may be incomplete.",
    "reply count unknown": "The reply count is unknown.",
}

PATTERNS = {
    "email": r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b",
    "mention": r"(?<!\w)@(?!context\b|import\b)[\w.-]+",
    "link": r"(?:https?://|www\.)[^\s<>]+|\[[^\]]*\]\([^)]*\)",
    "code": r"```[\s\S]*?```|~~~[\s\S]*?~~~",
    "html": r"<(?:script|iframe|object)\b[^>]*>[\s\S]*?</(?:script|iframe|object)>",
    "path": r"(?:Macintosh HD/(?:Users|Volumes)/|(?<![\w:])(?:[A-Z]:[\\/]|/(?:Users|home|Volumes|private|var|tmp)/))[^\s,;)\"']*",
    "ip": r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])",
    "credential": r"\b(?:password|passwd|secret|api[_ -]?key|access[_ -]?token)\s*[:=]\s*\S+",
    "token": r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]+|sk-[A-Za-z0-9]{20,})\b",
    "participant_label": r"\b(?:participant|user|member|poster|person)s?\s+[A-Z]\b",
}


def _readonly_snapshot(path):
    path = Path(path).resolve(strict=True)
    if not path.is_file():
        raise ValueError("snapshot must be a regular file")
    wal = Path(str(path) + "-wal")
    if wal.exists() and wal.stat().st_size:
        raise ValueError("snapshot has a nonempty WAL; create a standalone SQLite backup first")
    con = sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)
    con.row_factory = sqlite3.Row
    con.execute("BEGIN")
    if con.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        con.close()
        raise ValueError("snapshot integrity check failed")
    return con


def _privacy_findings(text, posts, topic_id, field):
    kinds = []
    names = forbidden_names(posts)
    words = {m.group().lower() for m in re.finditer(r"\b[\w'’-]+\b", text)}
    if names & words:
        kinds.append("participant_name")
    if any(len(p["user"]) >= 3 and re.search(
            r"(?<!\w)" + re.escape(p["user"]) + r"(?!\w)", text, re.I) for p in posts):
        kinds.append("username")
    if _designature(text) != text.rstrip():
        kinds.append("signoff")
    kinds.extend(kind for kind, pattern in PATTERNS.items() if re.search(pattern, text, re.I))
    return [{"id": topic_id, "field": field, "kind": kind} for kind in kinds]


def _baseline(path, expected_hash):
    data = Path(path).read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    if digest != expected_hash:
        raise ValueError("baseline SHA-256 mismatch")
    baseline = json.loads(data)
    if (baseline.get("version") != 1 or baseline.get("public_preview") is not True
            or baseline.get("private_preview") is not False or baseline.get("publish_allowed") is not True
            or baseline.get("schema_version") not in
            ("questions-ui-public-preview-v1", "questions-ui-public-processed-preview-v1")
            or not isinstance(baseline.get("entries"), list)):
        raise ValueError("baseline must be a previously approved public Questions preview")
    rows = {}
    for row in baseline["entries"]:
        topic_id = row.get("id")
        if (type(topic_id) is not int or not 0 < topic_id <= 9007199254740991 or topic_id in rows
                or not all(isinstance(row.get(k), str) and row[k].strip() for k in ("title", "body"))):
            raise ValueError("invalid or duplicate baseline entry")
        rows[topic_id] = row
    return rows, digest


def _bool_or_none(value):
    if value is None:
        return None
    if value not in (0, 1):
        raise ValueError("invalid source boolean metadata")
    return bool(value)


def build_candidate(source, ratings, baseline_path, baseline_sha256):
    """Return private candidate and review audit. Inputs must be standalone immutable backups."""
    baseline, baseline_hash = _baseline(baseline_path, baseline_sha256)
    if Path(source).resolve() == Path(ratings).resolve() or os.path.samefile(source, ratings):
        raise ValueError("source and ratings snapshots must be separate")
    with closing(_readonly_snapshot(source)) as src, closing(_readonly_snapshot(ratings)) as decisions:
        source_state = corpus.get_state(decisions, "source")
        runtime = corpus.get_state(decisions, "runtime")
        if not source_state or not runtime or corpus.get_state(decisions, "scope") != {"scope": "all"}:
            raise ValueError("all-language preparation and runtime provenance are required")
        if decisions.execute("SELECT COUNT(*) FROM questions_attempts WHERE outcome='running'").fetchone()[0]:
            raise ValueError("ratings snapshot has running attempts")
        topics = src.execute("SELECT * FROM topics ORDER BY id").fetchall()
        rows = decisions.execute("SELECT * FROM questions_decisions ORDER BY topic_id").fetchall()
        digest = corpus.content_hash(topics)
        if digest != source_state["source_content_sha256"] or len(topics) != source_state["topics"]:
            raise ValueError("source snapshot differs from prepared source")
        if [t["id"] for t in topics] != [r["topic_id"] for r in rows] or not topics:
            raise ValueError("source and ratings membership differ")
        if set(baseline) - {t["id"] for t in topics}:
            raise ValueError("baseline contains IDs outside selected source")

        entries, findings, missing, not_current, retained, bindings = [], [], [], [], [], []
        statuses = Counter()
        for topic, row in zip(topics, rows):
            topic_id = topic["id"]
            if type(topic_id) is not int or not 0 < topic_id <= 9007199254740991:
                raise ValueError("invalid topic ID")
            if corpus.fingerprint(topic) != row["input_hash"]:
                raise ValueError(f"source fingerprint mismatch for topic {topic_id}")
            if (row["lang"] != (topic["lang"] or "unknown") or row["solved"] != topic["has_accepted_answer"]
                    or row["closed"] != topic["closed"] or row["replies"] != questions.reply_count(topic)):
                raise ValueError(f"source metadata mismatch for topic {topic_id}")
            if not corpus.current(row, runtime["model"], runtime["revision"]):
                not_current.append(topic_id)
                continue
            answers = questions.display_answers(json.loads(row["answers"]))
            if topic_id in baseline:
                title, body = baseline[topic_id]["title"], baseline[topic_id]["body"]
                retained.append(topic_id)
            else:
                _, fresh = corpus.prepared(topic)
                title, body = topic["title_en"], topic["summary_paragraph"]
                if not fresh or not all(isinstance(v, str) and v.strip() for v in (title, body)):
                    missing.append(topic_id)
                    continue
            warnings = json.loads(row["evidence_warnings"])
            if not isinstance(warnings, list) or any(w not in WARNING_TEXT for w in warnings):
                raise ValueError(f"unknown evidence warning for topic {topic_id}")
            posts = split_raw(topic["thread_raw"] or "")
            for field, text in (("title", title), ("body", body)):
                findings.extend(_privacy_findings(text, posts, topic_id, field))
            entry = {
                "id": topic_id, "title": title, "body": body,
                "active": topic["last_posted_at"] or topic["bumped_at"] or topic["created_at"],
                "lang": row["lang"], "accepted": _bool_or_none(row["solved"]),
                "closed": _bool_or_none(row["closed"]), "replies": row["replies"],
                "answers": answers, "evidence_warnings": [WARNING_TEXT[w] for w in warnings],
                "decision_model": row["decision_model"], "decision_version": row["decision_version"],
            }
            if entry["replies"] is not None and (type(entry["replies"]) is not int or entry["replies"] < 0):
                raise ValueError(f"invalid reply count for topic {topic_id}")
            if entry["active"] is not None:
                datetime.fromisoformat(entry["active"].replace("Z", "+00:00"))
            entries.append(entry)
            bindings.append({
                "id": topic_id, "input_hash": row["input_hash"],
                "title_sha256": hashlib.sha256(title.encode()).hexdigest(),
                "body_sha256": hashlib.sha256(body.encode()).hexdigest(),
                "display_source": "approved_baseline" if topic_id in baseline else "fresh_english_enrichment",
            })
            statuses[row["lang"]] += 1

    candidate = {
        "schema_version": "questions-ui-private-preview-v1", "version": 1,
        "private_preview": True, "publish_allowed": False,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "snapshot_scope": "processed", "processed_count": len(entries),
        "eligible_question_count": sum(e["answers"]["has_question"]["noul"] >= 0.5 for e in entries),
        "corpus_total": len(topics), "corpus_complete": len(entries) == len(topics), "entries": entries,
    }
    audit = {
        "schema_version": "questions-export-private-audit-v1", "privacy_review_required": True,
        "publication_authorized": False, "source_content_sha256": digest,
        "ratings_snapshot_sha256": corpus.file_hash(ratings), "baseline_sha256": baseline_hash,
        "model": runtime["model"], "model_revision": runtime["revision"],
        "decision_version": corpus.PROMPT_VERSION, "evidence_version": corpus.EVIDENCE_VERSION,
        "processed_count": len(entries), "eligible_question_count": candidate["eligible_question_count"],
        "corpus_total": len(topics), "corpus_complete": candidate["corpus_complete"],
        "languages": dict(statuses), "not_current_ids": not_current,
        "missing_fresh_display_ids": missing, "baseline_retained_ids": retained,
        "baseline_not_exported_ids": sorted(set(baseline) - {e["id"] for e in entries}),
        "display_bindings": bindings, "privacy_findings": findings,
    }
    return candidate, audit


def _write_private(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)


def cli(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="standalone private source SQLite backup")
    parser.add_argument("--ratings", type=Path, required=True, help="standalone private ratings SQLite backup")
    parser.add_argument("--baseline", type=Path, required=True, help="approved canonical public preview")
    parser.add_argument("--baseline-sha256", required=True, help="independently verified approved baseline hash")
    parser.add_argument("--output", type=Path, required=True, help="new private output directory")
    args = parser.parse_args(argv)
    candidate, audit = build_candidate(args.source, args.ratings, args.baseline, args.baseline_sha256)
    payload = (json.dumps(candidate, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n").encode()
    audit["candidate_sha256"] = hashlib.sha256(payload).hexdigest()
    audit_data = (json.dumps(audit, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode()
    args.output.mkdir(mode=0o700, parents=False, exist_ok=False)
    _write_private(args.output / "candidate.json", payload)
    _write_private(args.output / "audit.json", audit_data)
    print(json.dumps({"candidate_sha256": audit["candidate_sha256"], "processed_count": len(candidate["entries"]),
                      "privacy_findings": len(audit["privacy_findings"]), "review_required": True}))
