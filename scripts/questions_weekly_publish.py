#!/usr/bin/env python3
"""Publish only an independently reviewed complete weekly bundle; all gates default OFF."""

from contextlib import closing
import inspect
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys

import publish_data as publisher
import questions_weekly as weekly
import questions_weekly_review as independent


def publication_review(root, cfg, expected_review_sha256):
    root = Path(root)
    gate = weekly.readiness(root, cfg)
    review_path = root / "publication-review.json"
    if expected_review_sha256 and weekly.corpus.file_hash(review_path) != expected_review_sha256:
        raise ValueError("independently verified weekly publication review SHA-256 is required")
    review = json.loads(review_path.read_text())
    if expected_review_sha256 is None:
        if (gate.get("automated_review_authorized") is not True
                or gate.get("weekly_publication_authorized") is not True
                or review.get("automated_review_version") != weekly.independent_review.VERSION
                or review.get("english_reviews_sha256") != weekly.corpus.file_hash(root / "reviews.json")
                or review.get("translation_reviews_sha256") != weekly.corpus.file_hash(root / "translation-reviews.json")):
            raise ValueError("automatic publication requires independently checked current review proofs")
    elif not re.fullmatch(r"[a-f0-9]{64}", expected_review_sha256):
        raise ValueError("invalid publication review SHA-256")
    if (review.get("schema") != "questions-weekly-publication-review-v1"
            or review.get("identity") != weekly.identity(cfg)
            or any(review.get(key) is not True for key in
                   ("source_linked", "translated_fields_reviewed", "complete_current_locales",
                    "publication_authorized", "technical_meaning_preserved"))
            or not isinstance(review.get("reviewer"), str) or not review["reviewer"].strip()):
        raise ValueError("weekly publication is not independently authorized")
    weekly.timestamp(review.get("reviewed_at"))
    for name in ("publication/candidate.json", "publication/audit.json", "publication/public-preview.json",
                 "portable-locales/fr/questions.json", "portable-locales/ja/questions.json"):
        if review.get("sha256", {}).get(name) != weekly.corpus.file_hash(root / name):
            raise ValueError("weekly publication exact-byte review binding differs")
    if review.get("ui_review_sha256") != weekly.corpus.file_hash(root / "ui-publication-review.json"):
        raise ValueError("shared UI publication review binding differs")
    if not re.fullmatch(r"[a-f0-9]{64}", review.get("expected_live_preview_sha256", "")):
        raise ValueError("weekly publication requires a reviewed existing live preview base")
    preview = json.loads((root / "publication/public-preview.json").read_text())
    private = json.loads((root / "publication/candidate.json").read_text())
    audit = json.loads((root / "publication/audit.json").read_text())
    if ([weekly.complete_public_entry(entry) for entry in private["entries"]] != preview["entries"]
            or audit["identity"] != weekly.identity(cfg)
            or preview.get("corpus_complete") is not True or preview.get("snapshot_scope") != "processed"
            or not preview["entries"] or preview["processed_count"] != len(preview["entries"])
            or preview["corpus_total"] != len(preview["entries"])):
        raise ValueError("publication corpus/provenance mismatch")
    return review


def publish(root, cfg, review_sha256=None):
    root = Path(root)
    if os.environ.get("PUBLICATION_ENABLED") != "true":
        raise ValueError("weekly publication readiness is OFF; encrypted progress retained, nothing published")
    # Refuse old sample-only publisher/Pages dependencies rather than relaxing the full-corpus gate.
    if (not {"allow_complete", "review"} <= inspect.signature(publisher.publish_questions_preview).parameters.keys()
            or json.loads(Path("config/questions-preview.json").read_text()).get("complete_enabled") is not True):
        raise ValueError("reviewed complete Questions UI/Pages dependency is not installed/enabled")
    gh = publisher.GitHub()
    with gh.lease():
        # Validate against the current durable source under the same lease as public asset CAS.
        with publisher.workspace() as temp:
            source = temp / "forum.db"
            subprocess.run(["scripts/db-store.sh", "pull", str(source)], check=True,
                           stdout=subprocess.DEVNULL)
            if review_sha256 is None:
                live = temp / "reviewed-live-base"
                live.mkdir()
                gh.release("download", "data", "--pattern", "questions-preview.json", "--dir", str(live))
                live_path = live / "questions-preview.json"
                if publisher.read_public_questions(live_path).get("corpus_complete") is not True:
                    raise ValueError("automatic weekly publication requires the initial complete live release")
                independent.authorize_publication(source, root, cfg, weekly.corpus.file_hash(live_path))
            review = publication_review(root, cfg, review_sha256)
            with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as con:
                con.row_factory = sqlite3.Row
                topics = con.execute("SELECT * FROM topics ORDER BY id").fetchall()
                audit = json.loads((root / "publication/audit.json").read_text())
                if weekly.corpus.content_hash(topics) != audit["source_content_sha256"]:
                    raise ValueError("durable forum source changed; review a refreshed candidate")
                preview = json.loads((root / "publication/public-preview.json").read_text())
                rows = {row["id"]: row for row in preview["entries"]}
                if len(rows) != len(topics) or set(rows) != {row["id"] for row in topics}:
                    raise ValueError("publication membership differs from current durable source")
                overlays = {locale: json.loads((root / f"portable-locales/{locale}/questions.json").read_text())
                            for locale in ("fr", "ja")}
                for topic in topics:
                    posts = weekly.split_raw(topic["thread_raw"] or "")
                    texts = [rows[topic["id"]]["title"], rows[topic["id"]]["body"]]
                    for locale in ("fr", "ja"):
                        texts.extend(overlays[locale]["entries"][str(topic["id"])]["fields"].values())
                    if any(weekly.privacy.findings(text, posts, topic["op_username"]) for text in texts):
                        raise ValueError("source-linked EN/FR/JA identifier guards block publication")
            subprocess.run(["node", "scripts/localize.mjs", "--page", "questions", "--check-only",
                            "--require-complete", "--data", str(root / "embedding"), "--store", str(root / "portable-locales"),
                            "--report", "build/questions-publication-locales.json"], check=True,
                           stdout=subprocess.DEVNULL)
            if review_sha256 is None:
                subprocess.run(["python3", "scripts/questions_weekly_store.py", "push", str(root)], check=True,
                               stdout=subprocess.DEVNULL, env={**os.environ, "DATA_PUBLICATION_CHILD": "1"})
            # Public normalization, complete-locale enforcement and live-preview CAS are owned by the publisher.
            publisher.publish_questions_preview(
                gh, "data", root / "publication/public-preview.json", root / "portable-locales", temp,
                publisher.ensure_release(gh, "data"), review["expected_live_preview_sha256"], allow_complete=True,
                review={"candidate": root / "publication/candidate.json", "audit": root / "publication/audit.json",
                        "clearance": root / "ui-publication-review.json",
                        "clearance_sha256": review["ui_review_sha256"]})
    print("Reviewed complete weekly Questions/FR/JA published; Pages may now deploy")


if __name__ == "__main__":
    try:
        publish(Path("build/questions-checkpoint"), weekly.config("config/questions-weekly.json"),
                os.environ.get("REVIEW_SHA256"))
    except (ValueError, RuntimeError, OSError, KeyError, sqlite3.Error, subprocess.CalledProcessError):
        sys.exit("Weekly Questions publication blocked; inspect readiness/current-source/locale/review gates")
