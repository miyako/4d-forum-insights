#!/usr/bin/env python3
"""Apply an explicitly selected public-only Questions update under the release lease."""

from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import tempfile

import publish_data as publisher
from questions_weekly_store import encryption

BASELINE = "questions-public-baseline.tar.zst.enc"
BASELINE_DIGEST = "5ad3ff3fc4d154a3c6f9463fefbb9874ca9a634546730e32041f6abbb44b5aed"
PREVIEW = "questions/preview.json"
LOCALES = {locale: f"locales/{locale}/questions.json" for locale in ("fr", "ja")}
FILES = (PREVIEW, *LOCALES.values())


def digest(data):
    return hashlib.sha256(data).hexdigest()


def read_baseline(gh, work):
    gh.release("download", "data", "--pattern", BASELINE, "--dir", str(work))
    compressed = work / "baseline.tar.zst"
    archive = work / "baseline.tar"
    subprocess.run((*encryption(), "-d", "-in", str(work / BASELINE), "-out", str(compressed)), check=True)
    subprocess.run(("zstd", "-d", "-q", str(compressed), "-o", str(archive)), check=True)
    with tarfile.open(archive) as tar:
        members = tar.getmembers()
        if sorted(m.name for m in members) != sorted((*FILES, "SHA256SUMS")) or any(not m.isfile() for m in members):
            raise ValueError("Unexpected public baseline archive members")
        checksums = tar.extractfile("SHA256SUMS").read()
        data = {name: tar.extractfile(name).read() for name in FILES}
    expected = "".join(f"{digest(data[name])}  {name}\n" for name in FILES).encode()
    if digest(checksums) != BASELINE_DIGEST or checksums != expected:
        raise ValueError("Public Questions baseline checksum mismatch")
    path = work / "baseline-preview.json"
    path.write_bytes(data[PREVIEW])
    snapshot = publisher.read_public_questions(path)
    if snapshot.get("corpus_complete") is not True:
        raise ValueError("Public Questions baseline is not complete")
    overlays = {"data/" + path: publisher.validate_overlay(json.loads(data[path])) for path in LOCALES.values()}
    publisher.public_questions_overlays(overlays, snapshot, require_complete=True)
    return {row["id"] for row in snapshot["entries"]}


def staged_update(current, overlays, update, base_hash, baseline_ids, work, same_export=False):
    if (not isinstance(update, dict) or set(update) != {"schema", "base_sha256", "source_selected_at", "entries", "locales"}
            or update["schema"] != "questions-cleared-update-v1" or update["base_sha256"] != base_hash
            or not isinstance(update["entries"], list) or not update["entries"]):
        raise ValueError("Cleared update does not bind the current Questions release")
    rows = {row["id"]: row for row in current["entries"]}
    if not baseline_ids <= rows.keys() or current.get("corpus_complete") is not True:
        raise ValueError("Current release does not include the full public baseline")
    source_at = update["source_selected_at"]
    if not isinstance(source_at, str) or not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})", source_at):
        raise ValueError("Cleared update source date must be a timezone-aware ISO timestamp")
    new_date = datetime.fromisoformat(source_at.replace("Z", "+00:00"))
    old_date = datetime.fromisoformat(current["source_selected_at"].replace("Z", "+00:00"))
    if new_date < old_date or (new_date == old_date and not same_export):
        raise ValueError("Cleared update must advance the source date")
    delta = update["entries"]
    ids = publisher.questions_hashes({"version": 1, "entries": delta})
    rows.update((row["id"], row) for row in delta)
    updated = {**current, "entries": list(rows.values()), "source_selected_at": source_at,
               "generated_at": source_at, "processed_count": len(rows), "corpus_total": len(rows),
               "eligible_question_count": sum(row["answers"]["has_question"]["noul"] >= .5 for row in rows.values())}
    candidate = work / "staged.json"
    candidate.write_text(json.dumps(updated, ensure_ascii=False))
    normalized = publisher.read_public_questions(candidate)
    if normalized != updated:
        raise ValueError("Cleared update contains noncanonical or nonpublic fields")
    if not isinstance(update["locales"], dict) or set(update["locales"]) != set(LOCALES):
        raise ValueError("Cleared update must carry both FR and JA overlays")
    docs = {}
    for locale, relative in LOCALES.items():
        overlay = publisher.validate_overlay(update["locales"][locale])
        if (overlay["locale"] != locale or overlay["page"] != "questions"
                or set(overlay["entries"]) != set(ids)):
            raise ValueError(f"Cleared update {locale} coverage differs from the updated IDs")
        for key, entry in overlay["entries"].items():
            if entry != publisher.portable_questions_entry(entry) or entry["source_hash"] != ids[key]:
                raise ValueError(f"Nonportable or stale {locale} translation in cleared update")
        name = "data/" + relative
        docs[name] = {**overlay, "entries": {**overlays[name]["entries"], **overlay["entries"]}}
    publisher.public_questions_overlays(docs, normalized, require_complete=True)
    return normalized, docs


def already_applied(current, overlays, update):
    if (not isinstance(update, dict) or update.get("schema") != "questions-cleared-update-v1"
            or update.get("source_selected_at") != current["source_selected_at"]):
        return False
    rows = {row["id"]: row for row in current["entries"]}
    delta = update["entries"]
    if not delta or any(rows.get(row["id"]) != row for row in delta):
        return False
    ids = {str(row["id"]) for row in delta}
    return all(
        patch["locale"] == locale and patch["page"] == "questions" and set(patch["entries"]) == ids
        and all(overlays["data/" + path]["entries"].get(key) == value for key, value in patch["entries"].items())
        for locale, path in LOCALES.items()
        for patch in (update["locales"][locale],)
    )


def preflight_pages(gh, work, candidate):
    if not (work / "site-data.tar.gz").exists():
        gh.release("download", "data", "--pattern", "site-data.tar.gz", "--dir", str(work))
    assets = work / "pages"
    assets.mkdir()
    with tarfile.open(work / "site-data.tar.gz") as tar:
        manifest = tar.extractfile("data/manifest.json").read()
        (assets / "manifest.json").write_bytes(manifest)
        embeddings = json.loads(manifest).get("embeddings") or {}
        for key in ("ids", "bin"):
            name = embeddings.get(key, "").partition("?")[0]
            if name not in {"embeddings-ids.json", "embeddings.bin"}:
                raise ValueError("Expected the public shared embedding assets")
            with tar.extractfile("data/" + name) as source, (assets / name).open("wb") as target:
                shutil.copyfileobj(source, target)
    publisher.run("node", "scripts/questions_public_preview.mjs", "--source", str(candidate),
                  "--data", str(assets), "--allow-complete")


def request_pages():
    with open(os.environ["GITHUB_OUTPUT"], "a") as output:
        output.write("published=true\n")


def apply_update(gh, work, current, snapshot, overlays, update, baseline, same_export=False):
    preview, overlays = staged_update(snapshot, overlays, update, digest(current.read_bytes()), baseline, work,
                                     same_export=same_export)
    preflight_pages(gh, work, work / "staged.json")
    if os.environ.get("PUBLICATION_ENABLED") != "true":
        print("Questions update staged and validated; publication is off")
        return
    out = work / "publish"
    out.mkdir()
    store = work / "locales"
    checked = {}
    for locale, relative in LOCALES.items():
        path = work / relative
        path.parent.mkdir(parents=True)
        checked[locale] = json.dumps(overlays["data/" + relative], ensure_ascii=False).encode() + b"\n"
        path.write_bytes(checked[locale])
    publisher.publish_localization(gh, "data", store, out, {"site-data.tar.gz", "localization.tar.gz"},
                                   questions=preview, require_questions=True, reviewed_questions=checked)
    shutil.copyfile(work / "staged.json", current)
    gh.release("upload", "data", str(current), "--clobber")
    request_pages()


def main():
    if os.environ.get("DATA_PUBLICATION_CHILD") != "1":
        raise ValueError("Public Questions updates require the shared release lease")
    sha = os.environ.get("CLEARED_UPDATE_SHA256", "")
    if sha and not re.fullmatch(r"[0-9a-f]{64}", sha):
        raise ValueError("Invalid cleared update hash")
    gh = publisher.GitHub(os.environ["GITHUB_REPOSITORY"])
    with tempfile.TemporaryDirectory(prefix="questions-public-weekly-") as temp:
        work = Path(temp)
        baseline = read_baseline(gh, work)
        print(f"Restored and verified {len(baseline)} published Questions records")
        if not sha:
            print("No cleared update selected; nothing published")
            return
        name = f"questions-cleared-update-{sha}.json"
        for asset in (name, "questions-preview.json", "localization.tar.gz"):
            gh.release("download", "data", "--pattern", asset, "--dir", str(work))
        packet = (work / name).read_bytes()
        if digest(packet) != sha:
            raise ValueError("Cleared update asset does not match the approved digest")
        current = work / "questions-preview.json"
        snapshot = publisher.read_public_questions(current)
        overlays = publisher.read_overlays(work / "localization.tar.gz")
        update = json.loads(packet)
        if already_applied(snapshot, overlays, update):
            publisher.public_questions_overlays(overlays, snapshot, require_complete=True)
            if os.environ.get("RETRY_PAGES") == "true" and os.environ.get("PUBLICATION_ENABLED") == "true":
                preflight_pages(gh, work, current)
                request_pages()
            print("Cleared update is already in the release; no assets rewritten")
            return
        apply_update(gh, work, current, snapshot, overlays, update, baseline)


if __name__ == "__main__":
    main()
