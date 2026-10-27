#!/usr/bin/env python3
"""Cooperative, durable release-publication lease for CI and local publishers."""

import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import time
import uuid


LOCK_REF = "heads/data-publication-lock"
TRANSLATION_VERSION = "1"
LOCALE_PATHS = [f"data/locales/{locale}/{view}.json"
                for locale in ("fr", "ja") for view in ("tips", "voices")]


def run(*args, check=True):
    return subprocess.run(args, check=check, capture_output=True, text=True)


class GitHub:
    def __init__(self, repo=None):
        self.repo = repo or os.environ.get("GH_REPO") or os.environ.get("GITHUB_REPOSITORY")
        if not self.repo:
            self.repo = json.loads(run("gh", "repo", "view", "--json", "nameWithOwner").stdout)["nameWithOwner"]

    def api(self, endpoint, method="GET", **fields):
        args = ["gh", "api", f"repos/{self.repo}/{endpoint}", "--method", method]
        for key, value in fields.items():
            args += ["-f", f"{key}={value}"]
        result = run(*args)
        return json.loads(result.stdout) if result.stdout.strip() else None

    def release(self, *args):
        return run("gh", "release", *args, "--repo", self.repo)

    def lock_owner(self):
        return self.api(f"git/ref/{LOCK_REF}")["object"]["sha"]

    @contextlib.contextmanager
    def lease(self, wait=1800):
        head = self.api("commits/HEAD")
        identity = {
            "nonce": uuid.uuid4().hex,
            "run": os.environ.get("GITHUB_RUN_ID"),
            "host": os.uname().nodename,
            "pid": os.getpid(),
        }
        sha = self.api("git/commits", "POST", tree=head["commit"]["tree"]["sha"],
                       message="Data publication lease\n\n" + json.dumps(identity))["sha"]
        deadline = time.monotonic() + wait
        while True:
            try:
                self.api("git/refs", "POST", ref=f"refs/{LOCK_REF}", sha=sha)
                break
            except subprocess.CalledProcessError:
                # Only a positively identified competing lease is retryable.
                owner = self.lock_owner()
                if owner == sha:
                    break  # The create succeeded remotely but its response was lost.
                if time.monotonic() >= deadline:
                    raise RuntimeError(f"Publication locked by {owner}; inspect before explicit recovery")
                print(f"Waiting for publication lease {owner}", file=sys.stderr, flush=True)
                time.sleep(5)
        try:
            yield
        finally:
            # Never remove a lease whose ownership has changed.
            if self.lock_owner() != sha:
                raise RuntimeError("Publication lease changed; refusing to remove another owner's lock")
            self.api(f"git/refs/{LOCK_REF}", "DELETE")


@contextlib.contextmanager
def workspace():
    path = Path("build") / f"publish-{uuid.uuid4().hex}"
    path.mkdir(parents=True)
    try:
        yield path
    finally:
        shutil.rmtree(path)


def ensure_release(gh, release):
    try:
        tags = gh.api("releases/tags/" + release)
    except subprocess.CalledProcessError as error:
        if "HTTP 404" not in (error.stderr or ""):
            raise
        gh.release("create", release, "--title", "Published data", "--latest=false",
                   "--notes", "English site data, optional localization overlays, and encrypted pipeline DB.")
        return set()
    return {asset["name"] for asset in tags["assets"]}


def read_overlays(archive):
    documents = {}
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar:
            name = member.name.removeprefix("./")
            if name in LOCALE_PATHS:
                if not member.isfile() or name in documents:
                    raise ValueError(f"Invalid overlay member: {name}")
                documents[name] = validate_overlay(json.load(tar.extractfile(member)))
                locale, filename = Path(name).parts[-2:]
                if documents[name]["locale"] != locale or documents[name]["page"] != Path(filename).stem:
                    raise ValueError(f"Locale/page identity mismatch: {name}")
    return documents


def validate_overlay(document):
    if not isinstance(document, dict) or document.get("version") != 1 or not isinstance(document.get("entries"), dict):
        raise ValueError("Locale overlays require version: 1 and entries keyed by ID")
    if document.get("locale") not in ("fr", "ja") or document.get("page") not in ("tips", "voices"):
        raise ValueError("Invalid locale/page identity")
    expected_fields = {"headline", "body"} if document["page"] == "tips" else {"question", "positions", "outcome"}
    for key, entry in document["entries"].items():
        if (not re.fullmatch(r"[1-9][0-9]*", key) or int(key) > 9007199254740991
                or not isinstance(entry, dict)
                or not re.fullmatch(r"[a-f0-9]{64}", str(entry.get("source_hash", "")))
                or not isinstance(entry.get("translation_version"), str)
                or not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", entry["translation_version"])):
            raise ValueError("Each locale entry requires valid source_hash and translation_version")
        fields = entry.get("fields")
        if not isinstance(fields, dict) or set(fields) != expected_fields:
            raise ValueError("Unexpected translation fields")
        for field, value in fields.items():
            values = value if field == "positions" else [value]
            if not isinstance(values, list) or not values or any(not isinstance(v, str) or not v.strip() for v in values):
                raise ValueError(f"Invalid translated field: {field}")
    return document


def column_hashes(page, columns):
    fields = ("headline", "body") if page == "tips" else ("question", "positions", "outcome")
    hashes = {}
    for index, topic_id in enumerate(columns["ids"]):
        source = {field: columns[field][index] for field in fields}
        canonical = json.dumps(source, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        hashes[str(topic_id)] = hashlib.sha256(canonical.encode()).hexdigest()
    return hashes


def english_hashes(archive):
    with tarfile.open(archive, "r:gz") as tar:
        return {page: column_hashes(page, json.load(tar.extractfile(f"data/{page}/entries.json")))
                for page in ("tips", "voices")}


def prepare_localization(data, seeds, store, archive=None):
    """Merge validated, current seeds/release entries without extracting archive paths."""
    hashes = {}
    for page in ("tips", "voices"):
        path = data / page / "entries.json"
        hashes[page] = column_hashes(page, json.loads(path.read_text())) if path.is_file() else {}
    published = {}
    if archive and archive.is_file():
        try:
            published = read_overlays(archive)
        except (ValueError, KeyError, tarfile.TarError) as error:
            print(f"::warning::Ignoring invalid optional localization asset: {error}", file=sys.stderr)
    for name in LOCALE_PATHS:
        relative = name.removeprefix("data/locales/")
        locale, filename = Path(relative).parts
        page = Path(filename).stem
        merged = {"version": 1, "locale": locale, "page": page, "entries": {}}
        seed = seeds / relative
        candidates = [validate_overlay(json.loads(seed.read_text()))] if seed.is_file() else []
        if name in published:
            candidates.append(published[name])
        for document in candidates:
            if document["locale"] != locale or document["page"] != page:
                raise ValueError(f"Locale/page identity mismatch: {name}")
            merged["entries"].update({key: entry for key, entry in document["entries"].items()
                                      if hashes[page].get(key) == entry["source_hash"]
                                      and entry["translation_version"] == TRANSLATION_VERSION})
        output = store / relative
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(merged, ensure_ascii=False) + "\n")


def pack_site(source, output):
    if not (source / "manifest.json").is_file():
        raise ValueError("site/data is empty: run make data first")
    with tarfile.open(output, "w:gz") as tar:
        for path in sorted(source.rglob("*")):
            relative = path.relative_to(source)
            if relative.parts[0] == "locales":
                continue
            if path.is_symlink():
                raise ValueError(f"Symlinks are not publishable: {path}")
            if path.is_file():
                tar.add(path, arcname=f"data/{relative}", recursive=False)


def is_corpus_file(name):
    parts = Path(name).parts
    return parts[0] != "locales" and not (len(parts) == 1 and name.startswith("embeddings"))


def corpus_hash(files):
    """Bind provenance to English content, allowing embeddings-only rebuilds."""
    digest = hashlib.sha256()
    seen = set()
    for name, content in sorted(files):
        if not is_corpus_file(name):
            continue
        if name in seen:
            raise ValueError(f"Duplicate English archive member: {name}")
        seen.add(name)
        if name == "manifest.json":
            manifest = json.loads(content)
            for key in ("embeddings", "embedding_config"):
                manifest.pop(key, None)
            content = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        digest.update(name.encode() + b"\0" + hashlib.sha256(content).digest())
    if "manifest.json" not in seen:
        raise ValueError("English candidate requires manifest.json")
    return digest.hexdigest()


def directory_corpus_hash(source):
    files = []
    for path in source.rglob("*"):
        relative = path.relative_to(source).as_posix()
        if not is_corpus_file(relative):
            continue
        if path.is_symlink():
            raise ValueError(f"Symlinks are not publishable: {path}")
        if path.is_file():
            files.append((relative, path.read_bytes()))
    return corpus_hash(files)


def archive_corpus_hash(archive):
    with tarfile.open(archive, "r:gz") as tar:
        return corpus_hash([(member.name.removeprefix("./").removeprefix("data/"),
                             tar.extractfile(member).read())
                            for member in tar if member.isfile()
                            and member.name.removeprefix("./").startswith("data/")
                            and is_corpus_file(member.name.removeprefix("./").removeprefix("data/"))])


def current_english(gh, release, directory):
    try:
        assets = gh.api("releases/tags/" + release)["assets"]
    except subprocess.CalledProcessError as error:
        if "HTTP 404" in (error.stderr or ""):
            return None, None
        raise
    asset = next((asset for asset in assets if asset["name"] == "site-data.tar.gz"), None)
    if asset is None:
        return None, None
    gh.release("download", release, "--pattern", "site-data.tar.gz", "--dir", str(directory))
    archive = directory / "site-data.tar.gz"
    return {"id": asset["id"], "sha256": hashlib.sha256(archive.read_bytes()).hexdigest()}, archive


def write_provenance(path, record):
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_name(path.name + "." + uuid.uuid4().hex)
    pending.write_text(json.dumps(record, sort_keys=True) + "\n")
    pending.replace(path)


def load_provenance(path, source, release, repo=None, sealed=True):
    if not path.is_file():
        raise ValueError("Missing English provenance: capture-english-base BEFORE export/build, then seal-english-candidate")
    record = json.loads(path.read_text())
    if (record.get("version") != 1 or record.get("source") != str(source.resolve())
            or record.get("release") != release or (repo and record.get("repository") != repo)
            or "base" not in record):
        raise ValueError("English provenance does not match candidate source/repository/release")
    if sealed and not record.get("content_hash"):
        raise ValueError("Unsealed English provenance: export must succeed before seal-english-candidate")
    return record


def capture_english_base(gh, release, source, provenance, directory, adopt=False):
    base, archive = current_english(gh, release, directory)
    content_hash = None
    if adopt:
        content_hash = directory_corpus_hash(source)
        if archive is None or content_hash != archive_corpus_hash(archive):
            raise ValueError("Cannot adopt existing candidate: English content differs from current published English")
    write_provenance(provenance, {
        "version": 1, "repository": gh.repo, "release": release,
        "source": str(source.resolve()), "base": base, "content_hash": content_hash,
    })


def seal_english_candidate(source, release, provenance):
    record = load_provenance(provenance, source, release, sealed=False)
    if record.get("content_hash"):
        raise ValueError("English provenance already sealed; capture a new base BEFORE a new export")
    record["content_hash"] = directory_corpus_hash(source)
    write_provenance(provenance, record)


def guard_english_publication(gh, release, source, directory, provenance):
    record = load_provenance(provenance, source, release, gh.repo)
    if record["content_hash"] != directory_corpus_hash(source):
        raise ValueError("English candidate content changed after sealing; recapture before rebuilding, not at publication")
    current, _ = current_english(gh, release, directory)
    if record["base"] != current:
        raise ValueError("English base asset changed since capture; refuse stale candidate regardless of generated_at")
    return record["content_hash"]


def notify_localization(gh):
    try:
        # repository_dispatch can start another workflow even when sent using GITHUB_TOKEN.
        gh.api("dispatches", "POST", event_type="english-data-published")
        return True
    except subprocess.CalledProcessError:
        print("::warning::English data was published, but localization dispatch failed; "
              "run localization.yml manually. English publication is not rolled back.",
              file=sys.stderr)
        return False


def publish_localization(gh, release, store, directory, assets):
    if "site-data.tar.gz" not in assets:
        raise ValueError("Publish English site data before localization")
    gh.release("download", release, "--pattern", "site-data.tar.gz", "--dir", str(directory))
    hashes = english_hashes(directory / "site-data.tar.gz")
    documents = {}
    if "localization.tar.gz" in assets:
        gh.release("download", release, "--pattern", "localization.tar.gz", "--dir", str(directory))
        documents = read_overlays(directory / "localization.tar.gz")
    updated = False
    for name in LOCALE_PATHS:
        path = store / name.removeprefix("data/locales/")
        if path.is_file():
            incoming = validate_overlay(json.loads(path.read_text()))
            locale, filename = Path(name).parts[-2:]
            page = Path(filename).stem
            if incoming.get("locale") != locale or incoming.get("page") != page:
                raise ValueError(f"Locale/page identity mismatch: {path}")
            current = documents.setdefault(name, {"version": 1, "locale": locale,
                                                   "page": page, "entries": {}})
            # Compare with English downloaded inside this lease, not the runner's old snapshot.
            current["entries"].update({
                key: value for key, value in incoming["entries"].items()
                if hashes[page].get(key) == value["source_hash"]
                and value["translation_version"] == TRANSLATION_VERSION
            })
            updated = True
    if not updated:
        raise ValueError("No localization overlays found")
    output = directory / "localization.tar.gz"
    with tarfile.open(output, "w:gz") as tar:
        for name, document in sorted(documents.items()):
            path = directory / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(document, ensure_ascii=False) + "\n")
            tar.add(path, arcname=name)
    gh.release("upload", release, str(output), "--clobber")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo")
    parser.add_argument("--wait", type=int, default=1800, help="seconds to wait; never expires a held lease")
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("publish-site", "publish-localization"):
        child = sub.add_parser(command)
        child.add_argument("--source", type=Path, default=Path("site/data"))
        child.add_argument("--release", default="data")
        child.add_argument("--deploy", action="store_true")
        if command == "publish-site":
            child.add_argument("--provenance", type=Path, default=Path("build/site-data-provenance.json"))
        if command == "publish-localization":
            child.add_argument("--store", type=Path,
                               help="overlay root containing fr/ and ja/ (default: SOURCE/locales)")
    for command in ("capture-english-base", "seal-english-candidate", "adopt-published-english"):
        child = sub.add_parser(command)
        child.add_argument("--source", type=Path, default=Path("site/data"))
        child.add_argument("--release", default="data")
        child.add_argument("--provenance", type=Path, default=Path("build/site-data-provenance.json"))
    child = sub.add_parser("with-lock")
    child.add_argument("args", nargs=argparse.REMAINDER)
    child = sub.add_parser("prepare-localization")
    child.add_argument("--data", type=Path, default=Path("site/data"))
    child.add_argument("--seeds", type=Path, default=Path("localization"))
    child.add_argument("--store", type=Path, default=Path("build/localization"))
    child.add_argument("--archive", type=Path)
    child = sub.add_parser("recover-lock")
    child.add_argument("--expected-sha", required=True)
    child.add_argument("--confirm-owner-stopped", action="store_true", required=True)
    args = parser.parse_args()
    if args.command == "prepare-localization":
        prepare_localization(args.data, args.seeds, args.store, args.archive)
        return
    if args.command == "seal-english-candidate":
        seal_english_candidate(args.source, args.release, args.provenance)
        return
    gh = GitHub(args.repo)
    if args.command == "recover-lock":
        if gh.lock_owner() != args.expected_sha:
            raise RuntimeError("Lock owner differs from expected SHA; refusing recovery")
        gh.api(f"git/refs/{LOCK_REF}", "DELETE")
        return
    with gh.lease(args.wait):
        if args.command == "with-lock":
            command = args.args[1:] if args.args[:1] == ["--"] else args.args
            if not command:
                parser.error("with-lock requires a command")
            # This flag is only for the child DB helper, never an ambient lock bypass.
            subprocess.run(command, check=True, env={**os.environ, "DATA_PUBLICATION_CHILD": "1"})
            return
        with workspace() as directory:
            if args.command in ("capture-english-base", "adopt-published-english"):
                capture_english_base(gh, args.release, args.source, args.provenance, directory,
                                     adopt=args.command == "adopt-published-english")
                return
            if args.command == "publish-site":
                expected_hash = guard_english_publication(gh, args.release, args.source, directory, args.provenance)
                ensure_release(gh, args.release)
                output = directory / "site-data.tar.gz"
                pack_site(args.source, output)
                if archive_corpus_hash(output) != expected_hash:
                    raise ValueError("English candidate changed during packaging; refusing publication")
                gh.release("upload", args.release, str(output), "--clobber")
                if args.release == "data":
                    notify_localization(gh)
            else:
                assets = ensure_release(gh, args.release)
                publish_localization(gh, args.release, args.store or args.source / "locales",
                                     directory, assets)
        if args.deploy:
            run("gh", "workflow", "run", "pages.yml", "--repo", gh.repo)


if __name__ == "__main__":
    try:
        main()
    except (subprocess.CalledProcessError, RuntimeError, ValueError) as error:
        print(f"Publication failed: {error}", file=sys.stderr)
        if isinstance(error, subprocess.CalledProcessError) and error.stderr:
            print(error.stderr, file=sys.stderr)
        sys.exit(1)
