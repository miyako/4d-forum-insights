"""Offline candidate/promotion and encrypted checkpoint tests; no GitHub or model calls."""

import base64
import contextlib
import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location("review_updates", ROOT / "scripts/review_updates.py")
review = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(review)


class FakeGitHub:
    repo = "owner/repo"

    def __init__(self):
        self.serial = 0
        self.locked = False
        self.uploads = []
        self.writes = []
        self.fail_upload = None
        self.fail_after_upload = False
        self.releases = {
            "data": {"id": 1, "tag_name": "data", "draft": False, "assets": []},
            "delta-candidate-7-1": {"id": 2, "tag_name": "delta-candidate-7-1", "draft": True,
                                    "target_commitish": "a" * 40, "author": {"login": review.BOT},
                                    "html_url": "https://github.com/owner/repo/releases/tag/delta-candidate-7-1",
                                    "assets": []},
        }
        self.bytes = {}
        self.pr = {"merged": True, "merged_by": {"type": "User"}, "user": {"login": review.BOT},
                   "base": {"ref": "main", "repo": {"full_name": self.repo}},
                   "head": {"ref": "data-update-7-1", "sha": "head", "repo": {"full_name": self.repo}},
                   "merge_commit_sha": "merge", "changed_files": 1}
        self.changed = [{"filename": review.DESCRIPTOR, "status": "added"}]
        self.run = {"status": "completed", "conclusion": "success", "workflow_id": 3,
                    "head_sha": "a" * 40, "run_attempt": 1, "head_branch": "main",
                    "event": "schedule", "repository": {"full_name": self.repo},
                    "head_repository": {"full_name": self.repo}}
        self.descriptor = None

    def add_asset(self, tag, name, content):
        self.serial += 1
        assets = self.releases[tag]["assets"]
        assets[:] = [asset for asset in assets if asset["name"] != name]
        asset = {"id": self.serial, "name": name, "size": len(content), "state": "uploaded",
                 "uploader": {"login": review.BOT}}
        assets.append(asset)
        self.bytes[tag, name] = content
        return {"id": asset["id"], "sha256": hashlib.sha256(content).hexdigest(), "size": len(content)}

    def api(self, endpoint, method="GET", payload=None):
        if method == "POST":
            self.writes.append((endpoint, copy.deepcopy(payload)))
            if endpoint == "git/blobs":
                return {"sha": "blob"}
            if endpoint == "git/trees":
                return {"sha": "tree"}
            if endpoint == "git/commits":
                return {"sha": "candidate-commit"}
            if endpoint == "git/refs":
                return {}
            if endpoint == "pulls":
                return {"html_url": "https://github.com/owner/repo/pull/12"}
            raise AssertionError((endpoint, payload))
        if endpoint == "":
            return {"default_branch": "main"}
        if endpoint.startswith("releases/tags/"):
            release = self.releases[endpoint.removeprefix("releases/tags/")]
            if release["draft"]:
                raise AssertionError("REST tag lookup cannot retrieve a draft release")
            return copy.deepcopy(release)
        if endpoint.startswith("releases?"):
            return copy.deepcopy(list(self.releases.values()))
        if endpoint == "releases/2":
            return copy.deepcopy(self.releases["delta-candidate-7-1"])
        if endpoint == "pulls/12":
            return copy.deepcopy(self.pr)
        if endpoint == "pulls/12/files?per_page=100":
            return self.changed
        if endpoint.startswith("contents/"):
            return {"encoding": "base64", "content": base64.b64encode(json.dumps(self.descriptor).encode()).decode()}
        if endpoint == "actions/runs/7/attempts/1":
            return copy.deepcopy(self.run)
        if endpoint == "actions/workflows/delta.yml":
            return {"id": 3}
        if endpoint == "git/commits/" + "a" * 40:
            return {"tree": {"sha": "base-tree"}}
        raise AssertionError((endpoint, method, payload))

    @contextlib.contextmanager
    def lease(self):
        if self.locked:
            raise AssertionError("Nested lease")
        self.locked = True
        try:
            yield
        finally:
            self.locked = False

    def release(self, *args):
        command, tag = args[:2]
        if command == "download":
            name = args[args.index("--pattern") + 1]
            directory = Path(args[args.index("--dir") + 1])
            (directory / name).write_bytes(self.bytes[tag, name])
        elif command == "upload":
            if tag == "data":
                assert self.locked, "Live uploads must hold the publication lease"
            path = Path(args[2])
            self.uploads.append((tag, path.name))
            if tag == "data" and path.name == self.fail_upload:
                self.releases[tag]["assets"][:] = [
                    item for item in self.releases[tag]["assets"] if item["name"] != path.name]
                if self.fail_after_upload:
                    self.add_asset(tag, path.name, path.read_bytes())
                raise subprocess.CalledProcessError(1, ["gh"], stderr="simulated lost upload")
            self.add_asset(tag, path.name, path.read_bytes())
        else:
            raise AssertionError(args)


class ReviewUpdatesTests(unittest.TestCase):
    def setUp(self):
        Path("build").mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="test-review-", dir="build")
        self.root = Path(self.temp.name)
        self.gh = FakeGitHub()
        self.api_patch = patch.object(review, "api", side_effect=lambda gh, *args: gh.api(*args))
        self.api_patch.start()
        self.dispatch_patch = patch.object(review.publication, "run")
        self.dispatch = self.dispatch_patch.start()
        source = self.root / "source"
        source.mkdir()
        (source / "manifest.json").write_text(json.dumps({
            "files": {"topics": "topics.json"}, "questions_view": {"enabled": False}}))
        (source / "topics.json").write_text('{"rows":[[1,"Synthetic old title"]]}')
        archive = self.root / "old.tar.gz"
        review.publication.pack_site(source, archive)
        english = self.gh.add_asset("data", review.SITE_ASSET, archive.read_bytes())
        db = self.gh.add_asset("data", review.DB_ASSET, b"Salted__synthetic-old-encrypted-db")
        bases = {review.SITE_ASSET: {key: english[key] for key in ("id", "sha256")},
                 review.DB_ASSET: {key: db[key] for key in ("id", "sha256")}}
        self.identity = {"repository": self.gh.repo, "run_id": 7, "run_attempt": 1,
                         "source_sha": "a" * 40, "default_branch": "main",
                         "tag": "delta-candidate-7-1", "branch": "data-update-7-1"}
        base = {**self.identity, "bases": bases}
        (source / "topics.json").write_text('{"rows":[[1,"Synthetic new title"]]}')
        (source / "embeddings.bin").write_bytes(b"synthetic-vectors")
        candidate = self.root / review.SITE_ASSET
        review.publication.pack_site(source, candidate)
        files = {
            review.SITE_ASSET: self.gh.add_asset(self.identity["tag"], review.SITE_ASSET, candidate.read_bytes()),
            review.DB_ASSET: self.gh.add_asset(self.identity["tag"], review.DB_ASSET, b"Salted__synthetic-new-encrypted-db"),
            "base.json": self.gh.add_asset(self.identity["tag"], "base.json", json.dumps(base).encode()),
        }
        self.gh.descriptor = {"version": 1, **self.identity, "release_id": 2, "files": files,
                              "english_content_hash": review.check_archive(candidate)}
        self.source = source

    def tearDown(self):
        self.api_patch.stop()
        self.dispatch_patch.stop()
        self.temp.cleanup()

    def assert_no_live_writes(self):
        self.assertFalse(any(tag == "data" for tag, _ in self.gh.uploads))
        self.dispatch.assert_not_called()

    def test_success_promotes_exact_reviewed_bytes_and_previous_db_then_idempotent_retry(self):
        old_db = self.gh.bytes["data", review.DB_ASSET]
        review.promote(self.gh, 12)
        for name in (review.DB_ASSET, review.SITE_ASSET):
            self.assertEqual(self.gh.bytes["data", name], self.gh.bytes[self.identity["tag"], name])
        self.assertEqual(self.gh.bytes["data", "forum.db.prev.zst.enc"], old_db)
        self.dispatch.assert_called_once_with("gh", "workflow", "run", "pages.yml", "--repo", self.gh.repo, "--ref", "main")
        uploads = list(self.gh.uploads)
        review.promote(self.gh, 12)
        self.assertEqual(self.gh.uploads, uploads)
        self.assertEqual(self.dispatch.call_count, 1)

    def test_capture_restores_exact_db_under_lease_and_candidate_opens_only_descriptor_pr(self):
        env = {"GITHUB_RUN_ID": "7", "GITHUB_RUN_ATTEMPT": "1", "GITHUB_SHA": "a" * 40,
               "DEFAULT_BRANCH": "main", "GITHUB_REF": "refs/heads/main"}
        with patch.dict(os.environ, env):
            def restore(command, **options):
                self.assertTrue(self.gh.locked, "DB restore must use the asset captured inside the lease")
                self.assertEqual(command[:3], ["bash", "scripts/db-store.sh", "restore"])
                self.assertEqual(Path(command[4]).read_bytes(), self.gh.bytes["data", review.DB_ASSET])
            with patch.object(review.subprocess, "run", side_effect=restore):
                review.prepare(self.gh, self.root / "work.db", work=self.root, source=self.source)
            self.assertFalse(self.gh.locked)
            provenance = self.root / "site-data-provenance.json"
            review.publication.seal_english_candidate(self.source, "data", provenance)
            checkpoint = self.root / "delta-checkpoint" / review.DB_ASSET
            checkpoint.parent.mkdir()
            checkpoint.write_bytes(self.gh.bytes[self.identity["tag"], review.DB_ASSET])
            self.gh.releases[self.identity["tag"]]["assets"][:] = [
                asset for asset in self.gh.releases[self.identity["tag"]]["assets"] if asset["name"] == review.DB_ASSET]
            review.candidate(self.gh, work=self.root, source=self.source)
        tree = next(payload for endpoint, payload in self.gh.writes if endpoint == "git/trees")
        self.assertEqual([entry["path"] for entry in tree["tree"]], [review.DESCRIPTOR])
        blob = next(payload for endpoint, payload in self.gh.writes if endpoint == "git/blobs")
        self.gh.descriptor = json.loads(blob["content"])
        review.validate_descriptor(self.gh.descriptor, self.gh.repo)
        self.assert_no_live_writes()
        review.promote(self.gh, 12)
        self.dispatch.assert_called_once()

    def test_both_base_asset_ids_and_hashes_are_required_before_any_upload(self):
        for name in (review.DB_ASSET, review.SITE_ASSET):
            with self.subTest(name=name):
                original = copy.deepcopy(self.gh.releases["data"]["assets"])
                self.gh.add_asset("data", name, self.gh.bytes["data", name])
                with self.assertRaisesRegex(ValueError, "base changed"):
                    review.promote(self.gh, 12)
                self.assertEqual(self.gh.uploads, [])
                self.assert_no_live_writes()
                self.gh.releases["data"]["assets"] = original

    def test_initial_site_publication_requires_approved_db_and_absent_english_base(self):
        self.gh.releases["data"]["assets"][:] = [
            asset for asset in self.gh.releases["data"]["assets"] if asset["name"] != review.SITE_ASSET]
        base = json.loads(self.gh.bytes[self.identity["tag"], "base.json"])
        base["bases"][review.SITE_ASSET] = None
        self.gh.descriptor["files"]["base.json"] = self.gh.add_asset(
            self.identity["tag"], "base.json", json.dumps(base).encode())
        review.promote(self.gh, 12)
        self.assertEqual(self.gh.bytes["data", review.SITE_ASSET], self.gh.bytes[self.identity["tag"], review.SITE_ASSET])
        self.dispatch.assert_called_once()

    def test_failed_and_running_origin_runs_cannot_promote(self):
        for status, conclusion in (("in_progress", None), ("completed", "failure")):
            with self.subTest(status=status):
                self.gh.run.update(status=status, conclusion=conclusion)
                with self.assertRaisesRegex(ValueError, "successful trusted"):
                    review.promote(self.gh, 12)
                self.assert_no_live_writes()

    def test_unmerged_bot_merged_fork_or_code_changing_pr_is_rejected(self):
        original = copy.deepcopy(self.gh.pr)
        changes = [
            {"merged": False},
            {"merged_by": {"type": "Bot"}},
            {"head": {**original["head"], "repo": {"full_name": "fork/repo"}}},
            {"changed_files": 2},
        ]
        for changed in changes:
            with self.subTest(changed=changed):
                self.gh.pr = {**copy.deepcopy(original), **changed}
                with self.assertRaisesRegex(ValueError, "merged by a human"):
                    review.promote(self.gh, 12)
                self.assert_no_live_writes()
        self.gh.pr = original
        self.gh.changed[0]["filename"] = "scripts/review_updates.py"
        with self.assertRaisesRegex(ValueError, "Only the review descriptor"):
            review.promote(self.gh, 12)
        self.assert_no_live_writes()

    def test_wrong_workflow_source_branch_attempt_or_run_repo_is_rejected(self):
        original = copy.deepcopy(self.gh.run)
        for changed in ({"workflow_id": 9}, {"head_sha": "b" * 40}, {"head_branch": "feature"},
                        {"run_attempt": 2}, {"head_repository": {"full_name": "fork/repo"}}):
            with self.subTest(changed=changed):
                self.gh.run = {**copy.deepcopy(original), **changed}
                with self.assertRaisesRegex(ValueError, "successful trusted"):
                    review.promote(self.gh, 12)
                self.assert_no_live_writes()

    def test_published_or_replaced_candidate_release_is_rejected(self):
        release = self.gh.releases[self.identity["tag"]]
        release["draft"] = False
        with self.assertRaisesRegex(ValueError, "must remain"):
            review.promote(self.gh, 12)
        release["draft"] = True
        release["id"] = 99
        with self.assertRaisesRegex(ValueError, "release identity changed"):
            review.promote(self.gh, 12)
        self.assert_no_live_writes()

    def test_every_asset_hash_and_immutable_asset_id_is_verified(self):
        for name in review.FILES:
            with self.subTest(name=name):
                assets = copy.deepcopy(self.gh.releases[self.identity["tag"]]["assets"])
                content = self.gh.bytes[self.identity["tag"], name]
                self.gh.add_asset(self.identity["tag"], name, content)
                with self.assertRaisesRegex(ValueError, "asset identity changed"):
                    review.promote(self.gh, 12)
                self.gh.releases[self.identity["tag"]]["assets"] = assets
                self.gh.bytes[self.identity["tag"], name] = content.replace(b"a", b"b", 1) if b"a" in content else b"x" * len(content)
                with self.assertRaisesRegex(ValueError, "bytes do not match"):
                    review.promote(self.gh, 12)
                self.gh.bytes[self.identity["tag"], name] = content
                self.assert_no_live_writes()

    def test_changed_embeddings_cannot_hide_behind_equal_english_corpus_hash(self):
        previous_hash = self.gh.descriptor["english_content_hash"]
        (self.source / "embeddings.bin").write_bytes(b"different-vectors")
        archive = self.root / "changed-embeddings.tar.gz"
        review.publication.pack_site(self.source, archive)
        self.assertEqual(review.check_archive(archive), previous_hash)
        self.gh.bytes[self.identity["tag"], review.SITE_ASSET] = archive.read_bytes()
        with self.assertRaisesRegex(ValueError, "bytes do not match"):
            review.promote(self.gh, 12)
        self.assert_no_live_writes()

    def retry_upload_failure(self, name, after=False):
        self.gh.fail_upload = name
        self.gh.fail_after_upload = after
        with self.assertRaises(subprocess.CalledProcessError):
            review.promote(self.gh, 12)
        self.dispatch.assert_not_called()
        self.gh.fail_upload = None
        review.promote(self.gh, 12)
        for asset in (review.DB_ASSET, review.SITE_ASSET):
            self.assertEqual(self.gh.bytes["data", asset], self.gh.bytes[self.identity["tag"], asset])
        self.dispatch.assert_called_once()

    def test_deleted_asset_is_retryable_only_for_pending_asset(self):
        self.retry_upload_failure(review.DB_ASSET)

    def test_lost_upload_ack_is_retryable_only_for_pending_asset(self):
        self.retry_upload_failure(review.SITE_ASSET, after=True)

    def test_unrelated_publication_after_partial_failure_blocks_retry(self):
        self.gh.fail_upload = review.SITE_ASSET
        with self.assertRaises(subprocess.CalledProcessError):
            review.promote(self.gh, 12)
        self.gh.fail_upload = None
        self.gh.add_asset("data", review.DB_ASSET, b"Salted__another-approved-db")
        uploads = list(self.gh.uploads)
        with self.assertRaisesRegex(ValueError, "outside this promotion"):
            review.promote(self.gh, 12)
        self.assertEqual(self.gh.uploads, uploads)
        self.dispatch.assert_not_called()

    def test_deployment_failure_is_retryable_without_reuploading_approved_assets(self):
        self.dispatch.side_effect = subprocess.CalledProcessError(1, ["gh"])
        with self.assertRaises(subprocess.CalledProcessError):
            review.promote(self.gh, 12)
        live_uploads = [item for item in self.gh.uploads if item[0] == "data"]
        self.dispatch.side_effect = None
        review.promote(self.gh, 12)
        self.assertEqual([item for item in self.gh.uploads if item[0] == "data"], live_uploads)
        self.assertEqual(self.dispatch.call_count, 2)

    def test_incomplete_journal_upload_fails_closed_until_explicit_recovery(self):
        self.gh.fail_upload = review.DB_ASSET
        with self.assertRaises(subprocess.CalledProcessError):
            review.promote(self.gh, 12)
        self.gh.fail_upload = None
        self.gh.add_asset(self.identity["tag"], "promotion-000003.json", b"")
        uploads = list(self.gh.uploads)
        with self.assertRaisesRegex(ValueError, "Incomplete promotion journal asset"):
            review.promote(self.gh, 12)
        self.assertEqual(self.gh.uploads, uploads)
        self.dispatch.assert_not_called()

    def test_archive_rejects_questions_localization_and_unsafe_paths(self):
        for path in ("data/questions/preview.json", "data/locales/ja/tips.json", "data/../escape", "/data/absolute"):
            with self.subTest(path=path):
                archive = self.root / "unsafe.tar.gz"
                with tarfile.open(archive, "w:gz") as tar:
                    content = b"{}"
                    info = tarfile.TarInfo(path)
                    info.size = len(content)
                    tar.addfile(info, io.BytesIO(content))
                with self.assertRaises(ValueError):
                    review.check_archive(archive)

    def test_archive_rejects_enabled_questions_and_private_preview_flags(self):
        for changed in ({"questions_view": {"enabled": True}}, {"private_preview": True}, {"publish_allowed": False}):
            with self.subTest(changed=changed):
                (self.source / "manifest.json").write_text(json.dumps(changed))
                archive = self.root / "gated.tar.gz"
                review.publication.pack_site(self.source, archive)
                with self.assertRaises(ValueError):
                    review.check_archive(archive)

    def test_origin_identity_refuses_nondefault_branch(self):
        env = {"GITHUB_RUN_ID": "7", "GITHUB_RUN_ATTEMPT": "1", "GITHUB_SHA": "a" * 40,
               "DEFAULT_BRANCH": "main", "GITHUB_REF": "refs/heads/feature"}
        with patch.dict(os.environ, env):
            with self.assertRaisesRegex(ValueError, "trusted default"):
                review.run_identity(self.gh)


class CheckpointTests(unittest.TestCase):
    @unittest.skipUnless(all(shutil.which(tool) for tool in ("sqlite3", "zstd", "openssl")), "DB tools not installed")
    def test_stage_and_restore_encrypt_roundtrip_without_any_github_calls(self):
        Path("build").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="test-checkpoint-", dir="build") as temporary:
            root = Path(temporary).resolve()
            db = root / "source.db"
            with sqlite3.connect(db) as con:
                con.execute("CREATE TABLE synthetic(value TEXT)")
                con.execute("INSERT INTO synthetic VALUES ('test-only synthetic text')")
            shim = root / "gh"
            marker = root / "unexpected-gh-call"
            shim.write_text(f"#!/bin/sh\ntouch '{marker}'\nexit 1\n")
            shim.chmod(0o755)
            env = {**os.environ, "DB_KEY": "synthetic-test-only-key", "PATH": f"{root}:{os.environ['PATH']}"}
            encrypted = root / "checkpoint.enc"
            restored = root / "restored.db"
            for command in (["stage", str(db), str(encrypted)], ["restore", str(restored), str(encrypted)]):
                subprocess.run(["bash", str(ROOT / "scripts/db-store.sh"), *command],
                               check=True, capture_output=True, text=True, env=env)
            self.assertFalse(marker.exists(), "Local staging/restoration must never contact GitHub")
            self.assertTrue(encrypted.read_bytes().startswith(b"Salted__"))
            self.assertNotIn(b"test-only synthetic text", encrypted.read_bytes())
            with sqlite3.connect(restored) as con:
                self.assertEqual(con.execute("SELECT value FROM synthetic").fetchone()[0], "test-only synthetic text")


if __name__ == "__main__":
    unittest.main()
