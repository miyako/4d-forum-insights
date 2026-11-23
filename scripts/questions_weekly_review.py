#!/usr/bin/env python3
"""Independent AI checks with resumable private proofs; no publication or source writes."""

import argparse
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import time

import questions_weekly as weekly

privacy = weekly.privacy
reviewer = weekly.independent_review
REVIEWER = "Independent OpenAI request; AI-assisted, hosted"
TRANSLATION_SCHEMA = "questions-weekly-translation-review-v1"


def write_json(path, data):
    path = Path(path)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True) + "\n")
    temporary.chmod(0o600)
    temporary.replace(path)


def load_reviews(root, locale):
    name = "reviews.json" if locale == "en" else "translation-reviews.json"
    path = Path(root) / name
    schema = "questions-weekly-reviews-v1" if locale == "en" else "questions-weekly-translation-reviews-v1"
    if path.exists():
        data = json.loads(path.read_text())
        if data.get("schema") != schema or not isinstance(data.get("entries"), dict):
            raise ValueError("invalid private review records")
    else:
        data = {"schema": schema, "entries": {}}
    return path, data


def translation_matches(row, approval, translated, locale, cfg):
    return (
        isinstance(approval, dict) and approval.get("schema") == TRANSLATION_SCHEMA
        and approval.get("locale") == locale and approval.get("identity") == weekly.identity(cfg)
        and all(approval.get(key) == row[key] for key in
                ("source_hash", "freshness_hash", "display_hash", "audit_hash"))
        and approval.get("translation_hash") == privacy.digest(translated)
        and all(approval.get(key) is True for key in
                ("privacy_cleared", "technical_meaning_preserved", "source_linked"))
        and isinstance(approval.get("reviewer"), str) and bool(approval["reviewer"].strip())
    )


def binding(topic, row, field, locale, original, output, cfg):
    return privacy.digest([weekly.identity(cfg), topic["id"], field, locale,
                           row["source_hash"], row["freshness_hash"], row["display_hash"],
                           row["audit_hash"], original, output])


def accepted(cache, expected, cfg, original, output):
    if not cache or cache["binding"] != expected or cache["outcome"] != "accepted" or not cache["audit"]:
        return False
    audit = json.loads(cache["audit"])
    decision = audit.get("decision")
    return (
        audit.get("version") == reviewer.VERSION and audit.get("model") == cfg["openai_redaction_model"]
        and audit.get("binding") == expected
        and decision == {"privacy_clear": True, "meaning_preserved": True, "uncertain": False}
        and all(type(value) is bool for value in decision.values())
        and audit.get("original_hash") == privacy.digest(original)
        and audit.get("display_hash") == privacy.digest(output)
        and type(audit.get("model_calls")) is int and 1 <= audit["model_calls"] <= 2
        and audit.get("response_hash") == privacy.digest(decision)
    )


def proofs_match(topic, row, original, output, locale, approval, displays, cfg):
    version = approval.get("automated_review_version")
    if version is None:
        return True  # Initial seed review provenance is imported explicitly, not generated here.
    if version != reviewer.VERSION:
        return False
    for field in ("title", "body"):
        expected = binding(topic, row, field, locale, original[field], output[field], cfg)
        cache = displays.execute("SELECT * FROM review_fields WHERE topic_id=? AND field=? AND locale=?",
                                 (topic["id"], field, locale)).fetchone()
        if (not accepted(cache, expected, cfg, original[field], output[field])
                or approval.get("proof_sha256", {}).get(field) != privacy.digest(json.loads(cache["audit"]))):
            return False
    return True


def current_row(topic, rating, row, cfg):
    if (not rating or not row or row["error"] or not row["fields"] or not row["audit"]
            or not weekly.corpus.current(rating, cfg["decision_model"], cfg["decision_revision"])
            or rating["input_hash"] != weekly.corpus.fingerprint(topic)
            or row["source_hash"] != weekly.corpus.fingerprint(topic)
            or row["freshness_hash"] != weekly.fresh_hash(topic, rating, cfg)):
        return False
    fields, audit = json.loads(row["fields"]), json.loads(row["audit"])
    if (set(fields) != {"title", "body"} or any(not isinstance(value, str) for value in fields.values())
            or row["display_hash"] != privacy.digest(fields) or row["audit_hash"] != privacy.digest(audit)):
        raise ValueError("display proof failed exact-hash validation")
    return True


def review_fields(source, root, cfg, locale="en", limit=100, minutes=30, review=reviewer.review):
    if locale not in ("en", "fr", "ja") or type(limit) is not int or not 1 <= limit <= 100 or not 0 < minutes <= 30:
        raise ValueError("invalid independent-review execution bounds")
    root = Path(root)
    gate = weekly.readiness(root, cfg)
    if gate.get("automated_review_authorized") is not True:
        raise ValueError("independent hosted review is not authorized in readiness")
    path, approvals = load_reviews(root, locale)
    english = load_reviews(root, "en")[1]["entries"]
    overlay = None
    if locale != "en":
        overlay = weekly.publisher.validate_overlay(json.loads((root / f"locales/{locale}/questions.json").read_text()))
    deadline = time.monotonic() + minutes * 60
    attempted, held, examined = 0, 0, 0
    with closing(weekly.corpus.connect(source, root / "ratings.db")) as ratings, \
            closing(weekly.display_connect(root / "display.db")) as displays:
        topics = ratings.execute("SELECT * FROM source.topics ORDER BY id").fetchall()
        if weekly.corpus.content_hash(topics) != weekly.corpus.get_state(ratings, "source")["source_content_sha256"]:
            raise ValueError("review source changed since preparation")
        work = []
        for topic in topics:
            row = displays.execute("SELECT * FROM displays WHERE topic_id=?", (topic["id"],)).fetchone()
            rating = ratings.execute("SELECT * FROM questions_decisions WHERE topic_id=?", (topic["id"],)).fetchone()
            key = str(topic["id"]) if locale == "en" else f"{locale}:{topic['id']}"
            approval = approvals["entries"].get(key)
            if not current_row(topic, rating, row, cfg):
                if key in approvals["entries"]:
                    del approvals["entries"][key]
                continue
            fields, audit = json.loads(row["fields"]), json.loads(row["audit"])
            if locale == "en":
                original = {"title": topic["title_en"], "body": topic["summary_paragraph"]}
                output = fields
                if (weekly.review_matches(row, approval)
                        and proofs_match(topic, row, original, output, locale, approval, displays, cfg)):
                    weekly.timestamp(approval.get("reviewed_at"))
                    continue
                for field in ("title", "body"):
                    if not isinstance(original[field], str):
                        raise ValueError("independent review requires original English display text")
                    field_audit = audit[field]
                    reconstructed, _ = privacy.apply_spans(
                        original[field], {"uncertain": False, "spans": field_audit["spans"]})
                    if reconstructed != fields[field] or field_audit["original_hash"] != privacy.digest(original[field]):
                        raise ValueError("proposal does not reconstruct the original field")
            else:
                if not weekly.review_matches(row, english.get(str(topic["id"]))):
                    continue
                translated = overlay["entries"].get(str(topic["id"]))
                if (not translated or translated.get("source_hash") != privacy.digest(fields)
                        or translated.get("translation_version") != "1"):
                    if key in approvals["entries"]:
                        del approvals["entries"][key]
                    continue
                output = translated["fields"]
                if set(output) != {"title", "body"} or any(not isinstance(text, str) for text in output.values()):
                    raise ValueError("translation review requires both display fields")
                original = fields
                if (translation_matches(row, approval, output, locale, cfg)
                        and proofs_match(topic, row, original, output, locale, approval, displays, cfg)):
                    weekly.timestamp(approval.get("reviewed_at"))
                    continue
            checks = {}
            for field in ("title", "body"):
                expected = binding(topic, row, field, locale, original[field], output[field], cfg)
                cache = displays.execute(
                    "SELECT * FROM review_fields WHERE topic_id=? AND field=? AND locale=?",
                    (topic["id"], field, locale)).fetchone()
                if not cache or cache["binding"] != expected:
                    displays.execute(
                        "INSERT OR REPLACE INTO review_fields(topic_id,field,locale,binding,outcome) VALUES (?,?,?,?,'pending')",
                        (topic["id"], field, locale, expected))
                    cache = displays.execute(
                        "SELECT * FROM review_fields WHERE topic_id=? AND field=? AND locale=?",
                        (topic["id"], field, locale)).fetchone()
                checks[field] = (expected, cache)
            work.append((topic, row, original, output, key, checks))
        displays.commit()
        write_json(path, approvals)
        for state in ("pending", "held"):
            for topic, row, original, output, key, checks in work:
                if examined >= limit or time.monotonic() >= deadline:
                    break
                active = [(field, expected, cache) for field, (expected, cache) in checks.items()
                          if cache["outcome"] == state and cache["attempts"] < 2]
                if not active:
                    continue
                examined += 1
                for field, expected, _ in active:
                    if time.monotonic() >= deadline:
                        break
                    displays.execute(
                        "UPDATE review_fields SET outcome='held',attempts=attempts+1,audit=NULL,error='attempt_interrupted' "
                        "WHERE topic_id=? AND field=? AND locale=?", (topic["id"], field, locale))
                    displays.commit()
                    try:
                        decision, proof = review(
                            original[field], output[field], weekly.split_raw(topic["thread_raw"] or ""),
                            cfg["openai_redaction_model"], locale=locale,
                            timeout=min(120, deadline - time.monotonic()), attempts=2,
                            op_username=topic["op_username"])
                        proof["binding"] = expected
                        valid = (decision == {"privacy_clear": True, "meaning_preserved": True, "uncertain": False}
                                 and all(type(value) is bool for value in decision.values())
                                 and proof.get("decision") == decision and proof.get("version") == reviewer.VERSION
                                 and proof.get("model") == cfg["openai_redaction_model"]
                                 and proof.get("original_hash") == privacy.digest(original[field])
                                 and proof.get("display_hash") == privacy.digest(output[field])
                                 and proof.get("response_hash") == privacy.digest(decision)
                                 and type(proof.get("model_calls")) is int and 1 <= proof["model_calls"] <= 2)
                        outcome = "accepted" if valid else "rejected"
                        error = None if valid else "independent_review_rejected"
                    except ValueError as failure:
                        proof = {"binding": expected, "model_calls": getattr(failure, "model_calls", 0),
                                 "usage": getattr(failure, "usage", {}),
                                 "usage_unreported_calls": getattr(failure, "usage_unreported_calls", 0)}
                        outcome = "held"
                        error = getattr(failure, "reason", "independent_review_input_or_transport")
                    displays.execute(
                        "UPDATE review_fields SET outcome=?,audit=?,error=? WHERE topic_id=? AND field=? AND locale=?",
                        (outcome, json.dumps(proof), error, topic["id"], field, locale))
                    displays.commit()
                    attempted += 1
                for field in ("title", "body"):
                    expected = checks[field][0]
                    cache = displays.execute(
                        "SELECT * FROM review_fields WHERE topic_id=? AND field=? AND locale=?",
                        (topic["id"], field, locale)).fetchone()
                    checks[field] = (expected, cache)
        for topic, row, original, output, key, checks in work:
            if all(accepted(cache, expected, cfg, original[field], output[field])
                   for field, (expected, cache) in checks.items()):
                approval = {
                    "schema": "questions-weekly-display-review-v1" if locale == "en" else TRANSLATION_SCHEMA,
                    **{name: row[name] for name in ("source_hash", "freshness_hash", "display_hash", "audit_hash")},
                    "identity": weekly.identity(cfg), "source_linked": True, "privacy_cleared": True,
                    "technical_meaning_preserved": True, "openai_authorized": True,
                    "reviewer": REVIEWER, "reviewed_at": datetime.now(timezone.utc).isoformat(),
                    "automated_review_version": reviewer.VERSION,
                    "proof_sha256": {field: privacy.digest(json.loads(cache["audit"]))
                                     for field, (_, cache) in checks.items()},
                }
                if locale != "en":
                    approval.update(locale=locale, translation_hash=privacy.digest(output))
                approvals["entries"][key] = approval
            else:
                approvals["entries"].pop(key, None)
                held += 1
        write_json(path, approvals)
    print(json.dumps({"locale": locale, "review_calls_attempted": attempted, "held_topics": held,
                      "publication_performed": False}))
    if held:
        raise RuntimeError("independent review incomplete or rejected; private progress retained")


def authorize_publication(source, root, cfg, expected_live_sha256):
    root = Path(root)
    gate = weekly.readiness(root, cfg)
    if (gate.get("automated_review_authorized") is not True
            or gate.get("weekly_publication_authorized") is not True):
        raise ValueError("automatic publication review is not authorized in readiness")
    if not isinstance(expected_live_sha256, str) or not re.fullmatch(r"[a-f0-9]{64}", expected_live_sha256):
        raise ValueError("current live Questions asset hash is required")
    english = load_reviews(root, "en")[1]["entries"]
    translations = load_reviews(root, "fr")[1]["entries"]
    overlays = {locale: weekly.publisher.validate_overlay(
        json.loads((root / f"portable-locales/{locale}/questions.json").read_text())) for locale in ("fr", "ja")}
    audit = json.loads((root / "publication/audit.json").read_text())
    with closing(weekly.corpus.connect(source, root / "ratings.db")) as ratings, \
            closing(weekly.display_connect(root / "display.db")) as displays:
        topics = ratings.execute("SELECT * FROM source.topics ORDER BY id").fetchall()
        if weekly.corpus.content_hash(topics) != audit["source_content_sha256"]:
            raise ValueError("publication review source changed")
        for topic in topics:
            row = displays.execute("SELECT * FROM displays WHERE topic_id=?", (topic["id"],)).fetchone()
            rating = ratings.execute("SELECT * FROM questions_decisions WHERE topic_id=?", (topic["id"],)).fetchone()
            if not current_row(topic, rating, row, cfg) or not weekly.review_matches(row, english.get(str(topic["id"]))):
                raise ValueError("publication requires current independent English reviews")
            weekly.timestamp(english[str(topic["id"])].get("reviewed_at"))
            fields = json.loads(row["fields"])
            original = {"title": topic["title_en"], "body": topic["summary_paragraph"]}
            if not proofs_match(topic, row, original, fields, "en", english[str(topic["id"])], displays, cfg):
                raise ValueError("publication English review proofs differ")
            for locale in ("fr", "ja"):
                translated = overlays[locale]["entries"].get(str(topic["id"]))
                if (not translated or translated.get("source_hash") != row["display_hash"]
                        or not translation_matches(row, translations.get(f"{locale}:{topic['id']}"),
                                                   translated["fields"], locale, cfg)):
                    raise ValueError("publication requires current independent translation reviews")
                if not proofs_match(topic, row, fields, translated["fields"], locale,
                                    translations[f"{locale}:{topic['id']}"], displays, cfg):
                    raise ValueError("publication translation review proofs differ")
                weekly.timestamp(translations[f"{locale}:{topic['id']}"].get("reviewed_at"))
    weekly.candidate(source, root, cfg)
    names = ("publication/candidate.json", "publication/audit.json", "publication/public-preview.json",
             "portable-locales/fr/questions.json", "portable-locales/ja/questions.json")
    hashes = {name: weekly.corpus.file_hash(root / name) for name in names}
    now = datetime.now(timezone.utc).isoformat()
    ui = {"schema_version": "questions-private-publication-review-v1",
          "candidate_sha256": hashes[names[0]], "audit_sha256": hashes[names[1]],
          "preview_sha256": hashes[names[2]], "fr_sha256": hashes[names[3]], "ja_sha256": hashes[names[4]],
          "source_linked_privacy_review": True, "translation_review": True, "publication_authorized": True,
          "reviewer": REVIEWER, "reviewed_at": now, "expected_current_preview_sha256": expected_live_sha256}
    write_json(root / "ui-publication-review.json", ui)
    publication = {"schema": "questions-weekly-publication-review-v1", "identity": weekly.identity(cfg),
                   "source_linked": True, "translated_fields_reviewed": True, "complete_current_locales": True,
                   "publication_authorized": True, "technical_meaning_preserved": True,
                   "reviewer": REVIEWER, "reviewed_at": now, "sha256": hashes,
                   "automated_review_version": reviewer.VERSION,
                   "english_reviews_sha256": weekly.corpus.file_hash(root / "reviews.json"),
                   "translation_reviews_sha256": weekly.corpus.file_hash(root / "translation-reviews.json"),
                   "ui_review_sha256": weekly.corpus.file_hash(root / "ui-publication-review.json"),
                   "expected_live_preview_sha256": expected_live_sha256}
    write_json(root / "publication-review.json", publication)
    print("Independent field and translation checks bound to publication bytes; nothing published")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("english", "translations", "publication"))
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--root", type=Path, default=Path("build/questions-checkpoint"))
    parser.add_argument("--config", default="config/questions-weekly.json")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--minutes", type=float, default=30)
    parser.add_argument("--expected-live-sha256")
    args = parser.parse_args()
    cfg = weekly.config(args.config)
    if args.command == "publication":
        authorize_publication(args.source, args.root, cfg, args.expected_live_sha256)
    else:
        for locale in (("en",) if args.command == "english" else ("fr", "ja")):
            review_fields(args.source, args.root, cfg, locale, args.limit, args.minutes)


if __name__ == "__main__":
    main()
