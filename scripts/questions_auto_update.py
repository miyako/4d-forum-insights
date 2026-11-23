#!/usr/bin/env python3
"""Build a bounded Questions delta from the forum ingest and shared search export."""

from contextlib import closing
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import tarfile
import tempfile

import publish_data as publisher
import questions_prepare_public_update as prepare
import questions_public_weekly as weekly
from fminer import questions, questions_corpus
from fminer.questions_export import PATTERNS
from fminer.questions_openai_privacy import check_text
from fminer.questions_privacy import DetectionFailure, ProposalError

LIMIT = 3


def timestamp(value):
    if not value:
        return datetime.min.replace(tzinfo=timezone.utc)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def needs_decision(previous, source, baseline_date):
    return (previous is None
            or timestamp(source["raw_changed_at"]) > timestamp(previous.get("source_checked_at", baseline_date))
            or bool(source["has_accepted_answer"]) != previous["accepted"]
            or bool(source["closed"]) != previous["closed"]
            or questions.reply_count(source) != previous["replies"]
            or timestamp(source["last_posted_at"]) > timestamp(previous.get("active")))


def display_for(topic, paragraph, previous, overlays):
    if not paragraph or (topic["lang"] != "en" and not topic["title_en"]):
        return None, None
    display = {"title": topic["title_en"] or topic["title"], "body": paragraph}
    if any(not text.strip() or len(text) > limit
           for text, limit in zip(display.values(), (200, 1200))):
        return None, None
    for field, text in display.items():
        check_text(text)
        for kind in ("code", "html", "link", "email", "mention", "path", "ip"):
            text = re.sub(PATTERNS[kind], "[redacted]", text, flags=re.IGNORECASE)
        text = re.sub(r"`[^`]*`", "[code]", text)
        display[field] = text
    key = str(topic["id"])
    digest = publisher.questions_hashes({"version": 1, "entries": [{"id": topic["id"], **display}]})[key]
    if previous is not None and all(previous[field] == text for field, text in display.items()):
        cached = {locale: overlays["data/" + path]["entries"].get(key) for locale, path in weekly.LOCALES.items()}
        if all(entry and entry["source_hash"] == digest
               and entry["translation_version"] == publisher.TRANSLATION_VERSION for entry in cached.values()):
            return display, cached
    return display, None


def build_update(source, snapshot, overlays, work, start_kev, limit=LIMIT):
    with tarfile.open(work / "site-data.tar.gz") as archive:
        manifest = json.load(archive.extractfile("data/manifest.json"))
        topics = json.load(archive.extractfile("data/topics.json"))
        paragraphs = json.load(archive.extractfile("data/text.json"))["para"]
        indexed = set(json.load(archive.extractfile("data/embeddings-ids.json")))
    export_date = manifest["generated_at"]
    if timestamp(export_date) < timestamp(snapshot["source_selected_at"]):
        raise ValueError("Shared forum export is older than the current Questions release")
    baseline_date = json.loads((work / "baseline-preview.json").read_bytes())["source_selected_at"]
    previous = {row["id"]: row for row in snapshot["entries"]}
    published = {}
    for fields, text in zip(topics["rows"], paragraphs, strict=True):
        item = dict(zip(topics["fields"], fields, strict=True))
        published[item["id"]] = (item, text)
    entries = []
    started = False
    attempted = 0
    locales = {locale: {"version": 1, "locale": locale, "page": "questions", "entries": {}}
               for locale in weekly.LOCALES}
    pending = {"index or source": 0, "missing or invalid English display": 0, "credential pattern": 0,
               "Kev timeout": 0, "OpenAI failure": 0, "sensitive display": 0, "batch limit": 0}
    with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as con:
        con.row_factory = sqlite3.Row
        rows = list(con.execute("""SELECT id,raw_changed_at,last_posted_at,posts_count,
                                          has_accepted_answer,closed,fetch_error
                                   FROM topics ORDER BY id DESC"""))
        candidates = []
        for row in rows:
            topic_id = row["id"]
            old = previous.get(topic_id)
            if not needs_decision(old, row, baseline_date):
                continue
            if (topic_id not in indexed or topic_id not in published or row["fetch_error"]
                    or not row["posts_count"] or timestamp(row["raw_changed_at"]) > timestamp(export_date)):
                pending["index or source"] += 1
                continue
            topic, paragraph = published[topic_id]
            if (topic["posts"] != row["posts_count"]
                    or bool(row["closed"]) != bool(topic["flags"] & manifest["flags"]["closed"])
                    or bool(row["has_accepted_answer"]) != bool(topic["flags"] & manifest["flags"]["accepted"])):
                pending["index or source"] += 1
                continue
            try:
                display, cached = display_for(topic, paragraph, old, overlays)
            except ProposalError as error:
                if error.reason != "credential_in_display":
                    raise
                pending["credential pattern"] += 1
                continue
            if display is None:
                pending["missing or invalid English display"] += 1
                continue
            candidates.append((topic_id, topic, display, cached))
        if candidates:
            offset = (timestamp(export_date).date().toordinal() // 7 * limit) % len(candidates)
            candidates = candidates[offset:] + candidates[:offset]
        for topic_id, topic, display, cached in candidates:
            if attempted == limit:
                pending["batch limit"] += 1
                continue
            if not started:
                start_kev()
                started = True
            attempted += 1
            try:
                evidence, answers = prepare.kev_answers(source, topic_id)
            except prepare.IncompleteEvidence:
                pending["index or source"] += 1
                continue
            except prepare.DecisionTimeout:
                pending["Kev timeout"] += 1
                print(f"::warning::Kev timed out for topic {topic_id}; it remains pending")
                continue
            try:
                if cached is None:
                    display = prepare.enrich_display(display, evidence, os.environ["OPENAI_MODEL"])
                entry = prepare.public_row(topic, evidence, answers, display)
                entry["source_checked_at"] = export_date
                if cached is None:
                    translated = prepare.translate(entry, os.environ["OPENAI_MODEL"])
                    cached = {locale: doc["entries"][str(topic_id)] for locale, doc in translated.items()}
            except ProposalError as error:
                if error.reason != "sensitive_display":
                    raise
                pending["sensitive display"] += 1
                print(f"::warning::Topic {topic_id} contains sensitive display data; it remains pending")
                continue
            except DetectionFailure as error:
                pending["OpenAI failure"] += 1
                print(f"::warning::OpenAI processing failed for topic {topic_id} ({error.reason}); it remains pending")
                continue
            for locale in locales:
                locales[locale]["entries"][str(topic_id)] = cached[locale]
            entries.append(entry)
    print(f"Questions: {len(entries)} prepared; pending by reason: {json.dumps(pending, sort_keys=True)}")
    if not entries:
        if pending["Kev timeout"] or pending["OpenAI failure"]:
            raise RuntimeError("Kev or OpenAI failed; no Questions updates were prepared")
        return None
    return {"schema": "questions-cleared-update-v1",
            "base_sha256": weekly.digest((work / "questions-preview.json").read_bytes()),
            "source_selected_at": export_date, "entries": entries, "locales": locales}


def main():
    if os.environ.get("DATA_PUBLICATION_CHILD") != "1":
        raise ValueError("Automatic Questions updates require the shared release lease")
    gh = publisher.GitHub(os.environ["GITHUB_REPOSITORY"])
    with tempfile.TemporaryDirectory(prefix="questions-auto-") as directory:
        work = Path(directory)
        baseline = weekly.read_baseline(gh, work)
        for asset in ("questions-preview.json", "localization.tar.gz", "site-data.tar.gz"):
            gh.release("download", "data", "--pattern", asset, "--dir", str(work))
        current = work / "questions-preview.json"
        snapshot = publisher.read_public_questions(current)
        overlays = publisher.read_overlays(work / "localization.tar.gz")
        source = work / "forum.db"
        subprocess.run(("bash", "scripts/db-store.sh", "pull", str(source)), check=True)
        process = None

        def start():
            nonlocal process
            process = subprocess.Popen(("bash", "-c", """
set -euo pipefail
CFG=config/questions-weekly.json
PID=""
source pipeline/ci/questions-llama.sh
trap stop_server EXIT
install_questions_llama >&2
start_server decision 18080 >&2
printf 'ready\\n'
read -r shutdown || true
"""), stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
            if process.stdout.readline().strip() != "ready":
                raise RuntimeError("Kev launcher did not become ready")

        try:
            update = build_update(source, snapshot, overlays, work, start)
        finally:
            if process is not None:
                process.stdin.close()
                process.stdout.close()
                process.wait()
        if update is not None:
            weekly.apply_update(gh, work, current, snapshot, overlays, update, baseline, same_export=True)


if __name__ == "__main__":
    main()
