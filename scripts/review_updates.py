#!/usr/bin/env python3
"""Draft-release candidates and human-merge-bound publication. No model execution."""

import argparse
import base64
import copy
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tarfile

import publish_data as publication


DESCRIPTOR = ".github/data-updates/current.json"
DB_ASSET = "forum.db.zst.enc"
SITE_ASSET = "site-data.tar.gz"
FILES = {DB_ASSET, SITE_ASSET, "base.json"}
BOT = "github-actions[bot]"


def api(gh, endpoint, method="GET", payload=None):
    target = f"repos/{gh.repo}" + (f"/{endpoint}" if endpoint else "")
    command = ["gh", "api", target, "--method", method]
    if payload is not None:
        command += ["--input", "-"]
    result = subprocess.run(command, input=json.dumps(payload) if payload is not None else None,
                            text=True, capture_output=True, check=True)
    return json.loads(result.stdout) if result.stdout.strip() else None


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n")


def run_identity(gh):
    run_id = int(os.environ["GITHUB_RUN_ID"])
    attempt = int(os.environ["GITHUB_RUN_ATTEMPT"])
    source = os.environ["GITHUB_SHA"]
    branch = os.environ["DEFAULT_BRANCH"]
    if os.environ["GITHUB_REF"] != f"refs/heads/{branch}" or not re.fullmatch(r"[0-9a-f]{40}", source):
        raise ValueError("Delta candidates must be built from the trusted default branch")
    return {"repository": gh.repo, "run_id": run_id, "run_attempt": attempt,
            "source_sha": source, "default_branch": branch,
            "tag": f"delta-candidate-{run_id}-{attempt}",
            "branch": f"data-update-{run_id}-{attempt}"}


def release_by_tag(gh, tag):
    return api(gh, f"releases/tags/{tag}")


def download(gh, release, name, directory):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    if path.exists():
        raise ValueError(f"Refusing to overwrite downloaded file: {path}")
    gh.release("download", release["tag_name"], "--pattern", name, "--dir", str(directory))
    return path


def current_asset(gh, name, directory):
    release = release_by_tag(gh, "data")
    asset = next((item for item in release["assets"] if item["name"] == name), None)
    if asset is None:
        return None, None
    path = download(gh, release, name, directory)
    after = release_by_tag(gh, "data")
    if not any(item["id"] == asset["id"] and item["name"] == name for item in after["assets"]):
        raise ValueError("Live asset changed while downloading")
    return {"id": asset["id"], "sha256": digest(path)}, path


def draft_release(gh, identity, create=False):
    if "release_id" in identity:
        release = api(gh, f"releases/{identity['release_id']}")
        if release["id"] != identity["release_id"]:
            raise ValueError("Candidate draft release identity changed")
    else:
        # REST's tag endpoint returns published releases only. Drafts must be
        # discovered via the authenticated list, then addressed by immutable ID.
        matches, page = [], 1
        while True:
            batch = api(gh, f"releases?per_page=100&page={page}")
            matches.extend(item for item in batch if item["tag_name"] == identity["tag"])
            if len(batch) < 100:
                break
            page += 1
        if len(matches) > 1:
            raise ValueError("Candidate tag is ambiguous; refusing draft lookup")
        release = matches[0] if matches else None
    if release is None:
        if not create:
            raise ValueError("Private candidate release is missing")
        release = api(gh, "releases", "POST", {
            "tag_name": identity["tag"], "target_commitish": identity["source_sha"],
            "name": identity["tag"], "draft": True, "prerelease": True,
            "body": "Private delta review candidate. Never publish this draft release. "
                    "Only a verified human-merged descriptor can promote its assets.",
        })
    if (release["draft"] is not True or release["tag_name"] != identity["tag"]
            or release["target_commitish"] != identity["source_sha"]
            or release["author"]["login"] != BOT):
        raise ValueError("Candidate store must remain a bot-owned draft at the original source commit")
    return release


def upload_once(gh, identity, path):
    release = draft_release(gh, identity)
    existing = next((item for item in release["assets"] if item["name"] == path.name), None)
    if existing:
        with publication.workspace() as directory:
            previous = download(gh, release, path.name, directory)
            if digest(previous) != digest(path):
                raise ValueError(f"Immutable candidate asset already exists with different bytes: {path.name}")
    else:
        gh.release("upload", identity["tag"], str(path))
    release = draft_release(gh, identity)
    asset = next(item for item in release["assets"] if item["name"] == path.name)
    if (asset["state"] != "uploaded" or asset["size"] != path.stat().st_size
            or asset["uploader"]["login"] != BOT):
        raise ValueError(f"Candidate upload is incomplete or has an unexpected owner: {path.name}")
    return {"id": asset["id"], "sha256": digest(path), "size": path.stat().st_size}


def check_archive(path):
    seen = set()
    manifest = None
    total = 0
    with tarfile.open(path, "r:gz") as archive:
        for member in archive:
            name = PurePosixPath(member.name)
            if (name.is_absolute() or ".." in name.parts or not name.parts
                    or name.parts[0] != "data" or member.name in seen
                    or name.as_posix() != member.name.rstrip("/") or member.size < 0
                    or not (member.isfile() or member.isdir())):
                raise ValueError(f"Unsafe or duplicate candidate archive member: {member.name}")
            seen.add(member.name)
            total += member.size
            if total > 2 * 1024**3:
                raise ValueError("Candidate archive exceeds the 2 GiB uncompressed limit")
            if any(part.lower().startswith("questions") for part in name.parts[1:]):
                raise ValueError("Questions payloads are excluded from routine updates")
            if name.parts[1:2] == ("locales",):
                raise ValueError("Routine English candidates cannot contain localization")
            if member.name == "data/manifest.json" and member.isfile():
                manifest = json.load(archive.extractfile(member))
    if not isinstance(manifest, dict):
        raise ValueError("Candidate has no manifest")
    if manifest.get("private_preview") is True or manifest.get("publish_allowed") is False:
        raise ValueError("Private preview data cannot be promoted")
    view = manifest.get("questions_view", {})
    if view.get("enabled") is True or publication.questions_source_path(manifest):
        raise ValueError("Routine candidates must keep Questions disabled")
    return publication.archive_corpus_hash(path)


def prepare(gh, db, work=Path("build"), source=Path("site/data")):
    identity = run_identity(gh)
    with gh.lease(), publication.workspace() as directory:
        english, _ = current_asset(gh, SITE_ASSET, directory / "english")
        checkpoint, encrypted = current_asset(gh, DB_ASSET, directory / "db")
        if checkpoint is None:
            raise ValueError("Seed the encrypted working DB before building a candidate")
        subprocess.run(["bash", "scripts/db-store.sh", "restore", str(db), str(encrypted)], check=True)
        publication.write_provenance(work / "site-data-provenance.json", {
            "version": 1, "repository": gh.repo, "release": "data",
            "source": str(source.resolve()), "base": english, "content_hash": None,
        })
        write_json(work / "update-base.json", {
            **identity, "bases": {SITE_ASSET: english, DB_ASSET: checkpoint},
        })


def checkpoint(gh, db):
    identity = run_identity(gh)
    output = Path("build/delta-checkpoint") / DB_ASSET
    subprocess.run(["bash", "scripts/db-store.sh", "stage", str(db), str(output)], check=True)
    draft_release(gh, identity, create=True)
    upload_once(gh, identity, output)


def candidate(gh, work=Path("build"), source=Path("site/data")):
    identity = run_identity(gh)
    base = json.loads((work / "update-base.json").read_text())
    if any(base.get(key) != value for key, value in identity.items()):
        raise ValueError("Candidate source/run identity differs from captured base")
    provenance = publication.load_provenance(work / "site-data-provenance.json", source, "data", gh.repo)
    if (provenance["base"] != base["bases"][SITE_ASSET]
            or provenance["content_hash"] != publication.directory_corpus_hash(source)):
        raise ValueError("Candidate differs from the sealed capture-before-processing export")
    with publication.workspace() as directory:
        archive = directory / SITE_ASSET
        publication.pack_site(source, archive)
        content_hash = check_archive(archive)
        if content_hash != provenance["content_hash"]:
            raise ValueError("Candidate changed during packaging")
        base_file = directory / "base.json"
        write_json(base_file, base)
        encrypted = work / "delta-checkpoint" / DB_ASSET
        if not encrypted.read_bytes().startswith(b"Salted__"):
            raise ValueError("Working DB checkpoint must be OpenSSL-encrypted")
        release = draft_release(gh, identity)
        files = {path.name: upload_once(gh, identity, path) for path in (archive, base_file, encrypted)}
        descriptor = {"version": 1, **identity, "release_id": release["id"],
                      "english_content_hash": content_hash, "files": files}
        content = json.dumps(descriptor, sort_keys=True, indent=2) + "\n"
        commit = api(gh, f"git/commits/{identity['source_sha']}")
        blob = api(gh, "git/blobs", "POST", {"content": content, "encoding": "utf-8"})
        tree = api(gh, "git/trees", "POST", {
            "base_tree": commit["tree"]["sha"],
            "tree": [{"path": DESCRIPTOR, "mode": "100644", "type": "blob", "sha": blob["sha"]}],
        })
        head = api(gh, "git/commits", "POST", {
            "message": f"Review delta candidate {identity['run_id']}/{identity['run_attempt']}",
            "tree": tree["sha"], "parents": [identity["source_sha"]],
        })
        api(gh, "git/refs", "POST", {"ref": f"refs/heads/{identity['branch']}", "sha": head["sha"]})
        pr = api(gh, "pulls", "POST", {
            "title": f"Review site update ({identity['run_id']}/{identity['run_attempt']})",
            "head": identity["branch"], "base": identity["default_branch"], "maintainer_can_modify": False,
            "body": f"Review the private [candidate release]({release['html_url']}) and "
                    f"[delta run](https://github.com/{gh.repo}/actions/runs/{identity['run_id']}).\n\n"
                    "Only this descriptor is committed; all generated data stays off Git. "
                    "Human merge authorizes promotion of exactly these file hashes, subject to "
                    "both English and DB base checks. Stale candidates fail closed. "
                    "Requests (including bugs/features), Tips and Voices only; Questions excluded. "
                    "Localization is review-only, not automatically published.",
        })
        print(f"Candidate PR: {pr['html_url']}")


def descriptor_at(gh, sha):
    document = api(gh, f"contents/{DESCRIPTOR}?ref={sha}")
    if document.get("encoding") != "base64":
        raise ValueError("Expected an inline descriptor, not an external download")
    return json.loads(base64.b64decode(document["content"], validate=False))


def validate_descriptor(descriptor, repo):
    if (descriptor.get("version") != 1 or descriptor.get("repository") != repo
            or set(descriptor.get("files", {})) != FILES):
        raise ValueError("Invalid candidate descriptor schema/repository/files")
    for key in ("run_id", "run_attempt", "release_id"):
        if type(descriptor.get(key)) is not int or descriptor[key] < 1:
            raise ValueError(f"Invalid candidate {key}")
    run_id, attempt = descriptor["run_id"], descriptor["run_attempt"]
    if (descriptor.get("tag") != f"delta-candidate-{run_id}-{attempt}"
            or descriptor.get("branch") != f"data-update-{run_id}-{attempt}"):
        raise ValueError("Candidate branch/tag does not bind its run and attempt")
    for field in ("source_sha", "english_content_hash"):
        length = 40 if field == "source_sha" else 64
        if not re.fullmatch(rf"[0-9a-f]{{{length}}}", descriptor.get(field, "")):
            raise ValueError(f"Invalid candidate {field}")
    for file in descriptor["files"].values():
        if (set(file) != {"id", "sha256", "size"} or type(file["id"]) is not int or file["id"] < 1
                or type(file["size"]) is not int or not 0 < file["size"] <= 2 * 1024**3
                or not re.fullmatch(r"[0-9a-f]{64}", file["sha256"])):
            raise ValueError("Invalid candidate asset identity/hash/size")


def approved_descriptor(gh, number):
    pr = api(gh, f"pulls/{number}")
    default = api(gh, "")["default_branch"]
    if (not pr.get("merged") or pr["merged_by"]["type"] != "User" or pr["user"]["login"] != BOT
            or pr["base"]["ref"] != default or pr["base"]["repo"]["full_name"] != gh.repo
            or pr["head"]["repo"] is None or pr["head"]["repo"]["full_name"] != gh.repo
            or pr["changed_files"] != 1):
        raise ValueError("Promotion requires a bot-created, same-repo, descriptor-only PR merged by a human")
    changed = api(gh, f"pulls/{number}/files?per_page=100")
    if len(changed) != 1 or changed[0]["filename"] != DESCRIPTOR or changed[0]["status"] not in ("added", "modified"):
        raise ValueError("Only the review descriptor may change in a candidate PR")
    descriptor = descriptor_at(gh, pr["merge_commit_sha"])
    validate_descriptor(descriptor, gh.repo)
    if (descriptor != descriptor_at(gh, pr["head"]["sha"])
            or descriptor["branch"] != pr["head"]["ref"] or descriptor["default_branch"] != default):
        raise ValueError("Merged candidate identity differs from the reviewed PR head")
    run = api(gh, f"actions/runs/{descriptor['run_id']}/attempts/{descriptor['run_attempt']}")
    workflow = api(gh, "actions/workflows/delta.yml")
    if (run["status"] != "completed" or run["conclusion"] != "success"
            or run["workflow_id"] != workflow["id"] or run["head_sha"] != descriptor["source_sha"]
            or run["run_attempt"] != descriptor["run_attempt"] or run["head_branch"] != default
            or run["event"] not in ("schedule", "workflow_dispatch")
            or run["repository"]["full_name"] != gh.repo or run["head_repository"]["full_name"] != gh.repo):
        raise ValueError("Candidate does not originate from a successful trusted delta run/attempt")
    release = draft_release(gh, descriptor)
    if release["id"] != descriptor["release_id"]:
        raise ValueError("Candidate draft release identity changed")
    return descriptor


def verified_files(gh, descriptor, directory):
    release = draft_release(gh, descriptor)
    paths = {}
    for name, expected in descriptor["files"].items():
        asset = next((item for item in release["assets"] if item["name"] == name), None)
        if (asset is None or asset["id"] != expected["id"] or asset["size"] != expected["size"]
                or asset["state"] != "uploaded" or asset["uploader"]["login"] != BOT):
            raise ValueError(f"Candidate asset identity changed: {name}")
        path = download(gh, release, name, directory)
        if path.stat().st_size != expected["size"] or digest(path) != expected["sha256"]:
            raise ValueError(f"Candidate bytes do not match the merged descriptor: {name}")
        paths[name] = path
    after = draft_release(gh, descriptor)
    for name, expected in descriptor["files"].items():
        if not any(item["name"] == name and item["id"] == expected["id"] for item in after["assets"]):
            raise ValueError("Candidate asset changed during verification")
    base = json.loads(paths["base.json"].read_text())
    keys = ("repository", "run_id", "run_attempt", "source_sha", "default_branch", "tag", "branch")
    if any(base.get(key) != descriptor[key] for key in keys) or set(base.get("bases", {})) != {SITE_ASSET, DB_ASSET}:
        raise ValueError("Captured bases do not belong to the reviewed run")
    for name, value in base["bases"].items():
        if value is None and name == SITE_ASSET:
            continue
        if (not isinstance(value, dict) or set(value) != {"id", "sha256"}
                or type(value["id"]) is not int or value["id"] < 1
                or not re.fullmatch(r"[0-9a-f]{64}", value["sha256"])):
            raise ValueError("Invalid original live asset identity")
    if not paths[DB_ASSET].read_bytes().startswith(b"Salted__"):
        raise ValueError("Candidate checkpoint is not encrypted")
    if check_archive(paths[SITE_ASSET]) != descriptor["english_content_hash"]:
        raise ValueError("Candidate English content hash differs from reviewed descriptor")
    return paths, base["bases"]


def descriptor_hash(descriptor):
    return hashlib.sha256(json.dumps(descriptor, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def read_journal(gh, descriptor, directory):
    release = draft_release(gh, descriptor)
    journals = sorted((asset for asset in release["assets"]
                       if re.fullmatch(r"promotion-[0-9]{6}\.json", asset["name"])), key=lambda asset: asset["name"])
    if not journals:
        return None
    asset = journals[-1]
    if asset["state"] != "uploaded" or asset["size"] == 0:
        raise ValueError(f"Incomplete promotion journal asset {asset['id']}; "
                         "inspect and recover this exact failed upload before retrying")
    if asset["uploader"]["login"] != BOT:
        raise ValueError("Promotion journal is not owned by the trusted publisher")
    path = download(gh, release, asset["name"], directory)
    state = json.loads(path.read_text())
    if (state.get("descriptor_hash") != descriptor_hash(descriptor)
            or state.get("sequence") != int(asset["name"][10:16])
            or type(state.get("pr")) is not int or state["pr"] < 1
            or set(state.get("assets", {})) != {DB_ASSET, SITE_ASSET}
            or state.get("pending") not in (None, DB_ASSET, SITE_ASSET)
            or type(state.get("published")) is not bool
            or type(state.get("deployment_requested")) is not bool):
        raise ValueError("Promotion journal does not bind the reviewed candidate")
    for name, value in state["assets"].items():
        if value is None and name == SITE_ASSET and not state["published"]:
            continue
        if (not isinstance(value, dict) or set(value) != {"id", "sha256"}
                or type(value["id"]) is not int or value["id"] < 1
                or not re.fullmatch(r"[0-9a-f]{64}", value["sha256"])):
            raise ValueError("Invalid journal asset identity")
    if (state["published"] and (state["pending"] is not None or any(
            state["assets"][name]["sha256"] != descriptor["files"][name]["sha256"] for name in FILES - {"base.json"}))
            or state["deployment_requested"] and not state["published"]):
        raise ValueError("Inconsistent promotion journal phase")
    return state


def save_journal(gh, descriptor, state, directory):
    state["sequence"] += 1
    if state["sequence"] > 999999:
        raise ValueError("Promotion journal sequence exhausted")
    path = directory / f"promotion-{state['sequence']:06d}.json"
    write_json(path, state)
    upload_once(gh, descriptor, path)


def guard_retry(state, current, descriptor):
    for name in (DB_ASSET, SITE_ASSET):
        if current[name] == state["assets"][name]:
            continue
        if state["pending"] == name:
            # Clobber can delete the old asset before upload succeeds. A crash may also
            # occur after upload but before recording its new immutable ID.
            if current[name] is None or current[name]["sha256"] == descriptor["files"][name]["sha256"]:
                continue
        raise ValueError(f"Live {name} changed outside this promotion; refusing stale/partial retry")


def promote(gh, number):
    descriptor = approved_descriptor(gh, number)
    with publication.workspace() as directory:
        paths, bases = verified_files(gh, descriptor, directory / "candidate")
        with gh.lease():
            state = read_journal(gh, descriptor, directory / "journal")
            current, live_paths = {}, {}
            for name in (DB_ASSET, SITE_ASSET):
                current[name], live_paths[name] = current_asset(gh, name, directory / f"live-{name}")
            if state is None:
                if current != bases:
                    raise ValueError("English or encrypted DB base changed; rebuild a new candidate")
                backup = directory / "previous-forum.db.zst.enc"
                backup.write_bytes(live_paths[DB_ASSET].read_bytes())
                upload_once(gh, descriptor, backup)
                state = {"sequence": 0, "descriptor_hash": descriptor_hash(descriptor),
                         "pr": number, "assets": copy.deepcopy(bases), "pending": None,
                         "published": False, "deployment_requested": False}
                save_journal(gh, descriptor, state, directory)
            if state["pr"] != number:
                raise ValueError("Promotion journal belongs to a different PR")
            guard_retry(state, current, descriptor)
            if not state["published"]:
                for name in (DB_ASSET, SITE_ASSET):
                    if state["assets"][name] and state["assets"][name]["sha256"] == descriptor["files"][name]["sha256"]:
                        continue
                    state["pending"] = name
                    save_journal(gh, descriptor, state, directory)
                    if name == DB_ASSET:
                        release = draft_release(gh, descriptor)
                        backup = download(gh, release, "previous-forum.db.zst.enc", directory / "backup")
                        if digest(backup) != bases[DB_ASSET]["sha256"]:
                            raise ValueError("Original DB backup is invalid")
                        previous = directory / "forum.db.prev.zst.enc"
                        previous.write_bytes(backup.read_bytes())
                        gh.release("upload", "data", str(previous), "--clobber")
                    gh.release("upload", "data", str(paths[name]), "--clobber")
                    identity, _ = current_asset(gh, name, directory / f"after-{name}")
                    if identity is None or identity["sha256"] != descriptor["files"][name]["sha256"]:
                        raise ValueError("Uploaded asset did not retain the approved bytes")
                    state["assets"][name] = identity
                    state["pending"] = None
                    save_journal(gh, descriptor, state, directory)
                state["published"] = True
                save_journal(gh, descriptor, state, directory)
            if not state["deployment_requested"]:
                publication.run("gh", "workflow", "run", "pages.yml", "--repo", gh.repo,
                                "--ref", descriptor["default_branch"])
                state["deployment_requested"] = True
                save_journal(gh, descriptor, state, directory)
    print(f"Approved PR #{number}: publication verified and Pages deployment requested; localization not published.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("prepare", "checkpoint"):
        child = sub.add_parser(command)
        child.add_argument("--db", type=Path, default=Path("build/work.db"))
    sub.add_parser("candidate")
    child = sub.add_parser("promote")
    child.add_argument("--pr", type=int, required=True)
    args = parser.parse_args()
    gh = publication.GitHub()
    if args.command == "promote":
        if args.pr < 1:
            parser.error("--pr must be positive")
        promote(gh, args.pr)
    elif args.command == "candidate":
        candidate(gh)
    elif args.command == "prepare":
        prepare(gh, args.db)
    else:
        checkpoint(gh, args.db)


if __name__ == "__main__":
    try:
        main()
    except (subprocess.CalledProcessError, RuntimeError, ValueError, KeyError, TypeError, OSError) as error:
        print(f"Review update failed: {error}", file=sys.stderr)
        if isinstance(error, subprocess.CalledProcessError) and error.stderr:
            print(error.stderr, file=sys.stderr)
        sys.exit(1)
