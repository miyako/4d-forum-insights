"""Offline publication tests; run with python3 -m unittest discover -s tests -p test_publish_data.py."""

import importlib.util
import hashlib
import io
import json
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import unittest
from unittest.mock import patch
import uuid

SPEC = importlib.util.spec_from_file_location("publish_data", Path(__file__).parents[1] / "scripts/publish_data.py")
publisher = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(publisher)


class FakeGitHub(publisher.GitHub):
    def __init__(self):
        self.repo = "owner/repo"
        self.owner = None
        self.serial = 0
        self.calls = []
        self.assets = {}

    def api(self, endpoint, method="GET", **fields):
        self.calls.append((endpoint, method))
        if endpoint.startswith("releases/tags/"):
            return {"assets": [{"name": name, "id": hashlib.sha256(content).hexdigest()}
                               for name, content in self.assets.items()]}
        if endpoint == "commits/HEAD":
            return {"commit": {"tree": {"sha": "tree"}}}
        if endpoint == "git/commits":
            self.serial += 1
            return {"sha": f"owner-{self.serial}"}
        if endpoint == "git/refs":
            if self.owner:
                raise subprocess.CalledProcessError(1, ["gh"], stderr="HTTP 422: Reference already exists")
            self.owner = fields["sha"]
            return {}
        if endpoint == f"git/ref/{publisher.LOCK_REF}":
            return {"object": {"sha": self.owner}}
        if endpoint == f"git/refs/{publisher.LOCK_REF}" and method == "DELETE":
            self.owner = None
            return
        raise AssertionError((endpoint, method, fields))

    def release(self, *args):
        if args[0] == "download":
            name = args[args.index("--pattern") + 1]
            directory = Path(args[args.index("--dir") + 1])
            (directory / name).write_bytes(self.assets[name])
        elif args[0] == "upload":
            path = Path(args[2])
            self.assets[path.name] = path.read_bytes()
        else:
            raise AssertionError(args)


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.root = Path("build") / f"test-publish-{uuid.uuid4().hex}"
        self.root.mkdir(parents=True)

    def tearDown(self):
        shutil.rmtree(self.root)

    def test_competing_publishers_cannot_enter_and_failure_releases(self):
        gh = FakeGitHub()
        with self.assertRaisesRegex(ValueError, "upload failed"):
            with gh.lease(wait=0):
                owner = gh.owner
                with self.assertRaisesRegex(RuntimeError, "locked"):
                    with gh.lease(wait=0):
                        self.fail("Concurrent writer entered")
                self.assertEqual(gh.owner, owner)
                raise ValueError("upload failed")
        self.assertIsNone(gh.owner)
        with gh.lease(wait=0):
            self.assertIsNotNone(gh.owner)

    def test_abandoned_lock_is_not_expired(self):
        gh = FakeGitHub()
        gh.owner = "crashed-owner"
        with self.assertRaisesRegex(RuntimeError, "crashed-owner"):
            with gh.lease(wait=0):
                self.fail("Abandoned lease must require explicit recovery")
        self.assertEqual(gh.owner, "crashed-owner")

    def test_create_response_loss_does_not_deadlock_its_owner(self):
        class LostResponseGitHub(FakeGitHub):
            def api(self, endpoint, method="GET", **fields):
                result = super().api(endpoint, method, **fields)
                if endpoint == "git/refs":
                    raise subprocess.CalledProcessError(1, ["gh"], stderr="connection closed")
                return result

        gh = LostResponseGitHub()
        with gh.lease(wait=0):
            self.assertIsNotNone(gh.owner)
        self.assertIsNone(gh.owner)

    def test_ownership_change_is_never_deleted(self):
        gh = FakeGitHub()
        with self.assertRaisesRegex(RuntimeError, "changed"):
            with gh.lease():
                gh.owner = "other-owner"
        self.assertEqual(gh.owner, "other-owner")

    def source(self):
        source = self.root / "source"
        source.mkdir()
        (source / "manifest.json").write_text(json.dumps({
            "files": {"topics": "topics.json", "text": "text.json"},
        }))
        for page, columns in (
            ("tips", {"headline": ["New 日本語"], "body": ["Text"]}),
            ("voices", {"question": ["Question"], "positions": [["One", "Two"]], "outcome": ["Result"]}),
        ):
            (source / page).mkdir()
            (source / page / "entries.json").write_text(json.dumps({"ids": [1], **columns}))
        (source / "topics.json").write_text(json.dumps({
            "fields": ["id", "title", "title_en", "line"],
            "rows": [[1, "Original 日本語", "English title", "English summary"],
                     [2, "Untranslated original", None, ""],
                     [3, "Original français", None, "Summary only"]],
        }))
        (source / "text.json").write_text(json.dumps({
            "count": 3, "para": ["English paragraph", "", "Paragraph only"],
        }))
        return source

    def test_english_publish_excludes_locales(self):
        source = self.source()
        (source / "locales").mkdir()
        (source / "locales" / "stale.json").write_text("bad localization")
        archive = self.root / "site-data.tar.gz"
        publisher.pack_site(source, archive)
        with tarfile.open(archive) as tar:
            self.assertIn("data/manifest.json", tar.getnames())
            self.assertFalse(any("locales" in name for name in tar.getnames()))

    def test_newer_timestamp_cannot_bypass_changed_base(self):
        gh = FakeGitHub()
        source = self.source()
        manifest = source / "manifest.json"
        manifest.write_text(json.dumps({"generated_at": "2026-10-27T09:00:00+00:00"}))
        archive = self.root / "site-data.tar.gz"
        publisher.pack_site(source, archive)
        published = archive.read_bytes()
        gh.assets["site-data.tar.gz"] = published
        directory = self.root / "guard"
        directory.mkdir()
        provenance = self.root / "provenance.json"
        with gh.lease():
            publisher.capture_english_base(gh, "data", source, provenance, directory)
        # An offline re-export can claim a later time; that must not establish lineage.
        manifest.write_text(json.dumps({"generated_at": "2036-10-27T09:00:01Z"}))
        publisher.seal_english_candidate(source, "data", provenance)
        gh.assets["site-data.tar.gz"] = published + b"newer release archive"
        with gh.lease():
            with self.assertRaisesRegex(ValueError, "base asset changed"):
                publisher.guard_english_publication(gh, "data", source, directory, provenance)
        self.assertEqual(gh.assets["site-data.tar.gz"], published + b"newer release archive")

    def test_missing_or_unsealed_provenance_fails_explicitly(self):
        gh = FakeGitHub()
        source = self.source()
        provenance = self.root / "missing.json"
        with self.assertRaisesRegex(ValueError, "Missing English provenance"):
            publisher.guard_english_publication(gh, "data", source, self.root, provenance)
        publisher.capture_english_base(gh, "data", source, provenance, self.root)
        with self.assertRaisesRegex(ValueError, "Unsealed English provenance"):
            publisher.guard_english_publication(gh, "data", source, self.root, provenance)

    def test_equal_timestamp_embeddings_only_candidate_with_same_base_is_allowed(self):
        gh = FakeGitHub()
        source = self.source()
        manifest = source / "manifest.json"
        manifest.write_text(json.dumps({"generated_at": "2026-10-27T09:00:00Z", "embeddings": {"model": "old"}}))
        archive = self.root / "site-data.tar.gz"
        publisher.pack_site(source, archive)
        gh.assets["site-data.tar.gz"] = archive.read_bytes()
        provenance = self.root / "provenance.json"
        publisher.capture_english_base(gh, "data", source, provenance, self.root)
        publisher.seal_english_candidate(source, "data", provenance)
        manifest.write_text(json.dumps({"generated_at": "2026-10-27T09:00:00Z", "embeddings": {"model": "new"}}))
        (source / "embeddings.bin").write_bytes(b"new vectors")
        with gh.lease():
            publisher.guard_english_publication(gh, "data", source, self.root, provenance)

    def test_adopting_existing_candidate_requires_exact_current_english_content(self):
        gh = FakeGitHub()
        source = self.source()
        archive = self.root / "site-data.tar.gz"
        publisher.pack_site(source, archive)
        gh.assets["site-data.tar.gz"] = archive.read_bytes()
        provenance = self.root / "provenance.json"
        (source / "embeddings.bin").write_bytes(b"reviewed new vectors")
        with gh.lease():
            publisher.capture_english_base(gh, "data", source, provenance, self.root, adopt=True)
            publisher.guard_english_publication(gh, "data", source, self.root, provenance)
        (source / "tips/entries.json").write_text('{"ids": []}')
        with self.assertRaisesRegex(ValueError, "differs from current published English"):
            publisher.capture_english_base(gh, "data", source, provenance, self.root, adopt=True)
        with self.assertRaisesRegex(ValueError, "content changed"):
            publisher.guard_english_publication(gh, "data", source, self.root, provenance)

    def test_every_production_english_upload_dispatches_localization(self):
        source = self.source()
        (source / "manifest.json").write_text(json.dumps({"generated_at": "2026-10-27T09:00:00Z"}))
        gh = FakeGitHub()
        provenance = self.root / "provenance.json"
        publisher.capture_english_base(gh, "data", source, provenance, self.root)
        publisher.seal_english_candidate(source, "data", provenance)
        with patch.object(publisher, "GitHub", return_value=gh), \
                patch.object(publisher, "ensure_release", return_value=set()), \
                patch.object(gh, "api", wraps=gh.api) as api, \
                patch.object(publisher.sys, "argv",
                             ["publish_data.py", "publish-site", "--source", str(source),
                              "--provenance", str(provenance)]):
            def dispatch_capable(endpoint, method="GET", **fields):
                if endpoint == "dispatches":
                    self.assertEqual(method, "POST")
                    self.assertEqual(fields, {"event_type": "english-data-published"})
                    self.assertIn("site-data.tar.gz", gh.assets)
                    return
                return FakeGitHub.api(gh, endpoint, method, **fields)

            api.side_effect = dispatch_capable
            publisher.main()
            self.assertTrue(any(call.args[0] == "dispatches" for call in api.call_args_list))

    def test_failed_localization_dispatch_does_not_fail_english_publication(self):
        gh = FakeGitHub()
        gh.assets["site-data.tar.gz"] = b"published English"
        with patch.object(gh, "api", side_effect=subprocess.CalledProcessError(1, ["gh"])):
            self.assertFalse(publisher.notify_localization(gh))
        self.assertEqual(gh.assets["site-data.tar.gz"], b"published English")

    def test_delayed_localization_preserves_new_english_and_current_translation(self):
        gh = FakeGitHub()
        source = self.source()
        archive = self.root / "site-data.tar.gz"
        publisher.pack_site(source, archive)
        english_bytes = archive.read_bytes()
        gh.assets["site-data.tar.gz"] = english_bytes
        current_hash = publisher.english_hashes(archive)["tips"]["1"]
        locale = source / "locales" / "fr"
        locale.mkdir(parents=True)
        overlay_path = locale / "tips.json"
        overlay = {"version": 1, "locale": "fr", "page": "tips", "entries": {
            "1": {"source_hash": current_hash, "translation_version": "1",
                  "fields": {"headline": "Actuel", "body": "Texte"}},
        }}
        overlay_path.write_text(json.dumps(overlay))
        directory = self.root / "first"
        directory.mkdir()
        with gh.lease():
            publisher.publish_localization(gh, "data", source / "locales", directory, set(gh.assets))
        overlay["entries"]["1"]["source_hash"] = "0" * 64
        overlay["entries"]["1"]["fields"]["headline"] = "Retardé"
        overlay_path.write_text(json.dumps(overlay))
        directory = self.root / "second"
        directory.mkdir()
        with gh.lease():
            publisher.publish_localization(gh, "data", source / "locales", directory, set(gh.assets))
        overlays = publisher.read_overlays(directory / "localization.tar.gz")
        entry = overlays["data/locales/fr/tips.json"]["entries"]["1"]
        self.assertEqual(entry["fields"]["headline"], "Actuel")
        self.assertEqual(gh.assets["site-data.tar.gz"], english_bytes)
        overlay["entries"]["1"]["source_hash"] = current_hash
        overlay["entries"]["1"]["translation_version"] = "0"
        overlay_path.write_text(json.dumps(overlay))
        directory = self.root / "old-version"
        directory.mkdir()
        with gh.lease():
            publisher.publish_localization(gh, "data", source / "locales", directory, set(gh.assets))
        overlays = publisher.read_overlays(directory / "localization.tar.gz")
        self.assertEqual(overlays["data/locales/fr/tips.json"]["entries"]["1"]["fields"]["headline"], "Actuel")

    def test_python_hash_matches_localization_javascript(self):
        source = self.source()
        archive = self.root / "site-data.tar.gz"
        publisher.pack_site(source, archive)
        result = subprocess.run(
            ["node", "--input-type=module", "-e",
             "import {sourceHash} from './scripts/localization.mjs';"
             "console.log(sourceHash({headline:'New 日本語',body:'Text'}));"],
            check=True, capture_output=True, text=True,
        )
        self.assertEqual(publisher.english_hashes(archive)["tips"]["1"], result.stdout.strip())

    def test_requests_hashes_match_javascript_and_keep_all_topic_ids(self):
        source = self.source()
        archive = self.root / "site-data.tar.gz"
        publisher.pack_site(source, archive)
        expected = [
            {"title_en": "English title", "line": "English summary", "para": "English paragraph"},
            {"title_en": "", "line": "", "para": ""},
            {"title_en": "", "line": "Summary only", "para": "Paragraph only"},
        ]
        result = subprocess.run(
            ["node", "--input-type=module", "-e",
             "import {sourceHash} from './scripts/localization.mjs';"
             f"console.log(JSON.stringify({json.dumps(expected)}.map(sourceHash)));"],
            check=True, capture_output=True, text=True,
        )
        hashes = publisher.english_hashes(archive)["requests"]
        self.assertEqual(list(hashes), ["1", "2", "3"])
        self.assertEqual(list(hashes.values()), json.loads(result.stdout))
        loaded = subprocess.run(
            ["node", "--input-type=module", "-e",
             "import {readSourceRecords} from './scripts/localization.mjs';"
             f"console.log(JSON.stringify(readSourceRecords({json.dumps(str(source))},'requests')));"],
            check=True, capture_output=True, text=True,
        )
        self.assertEqual({str(record["id"]): record["source_hash"] for record in json.loads(loaded.stdout)},
                         hashes)
        topics = json.loads((source / "topics.json").read_text())
        text = json.loads((source / "text.json").read_text())
        topics["rows"][0][1] = "Changed original title"
        self.assertEqual(publisher.request_hashes(topics, text), hashes)
        text["para"][0] = "Changed English paragraph"
        self.assertNotEqual(publisher.request_hashes(topics, text)["1"], hashes["1"])
        self.assertEqual(publisher.request_hashes(topics, text)["3"], hashes["3"])
        with self.assertRaisesRegex(ValueError, "paragraph count"):
            publisher.request_hashes(topics, {"para": []})
        topics["rows"][1][3] = None
        text["para"][1] = None
        self.assertEqual(publisher.request_hashes(topics, text)["2"], hashes["2"])

    def test_requests_overlay_shape_rejects_raw_fields_and_nonstring_translations(self):
        overlay = {"version": 1, "locale": "ja", "page": "requests", "entries": {
            "1": {"source_hash": "0" * 64, "translation_version": "1",
                  "fields": {"title_en": "", "line": "要約", "para": "段落"}},
        }}
        publisher.validate_overlay(overlay)
        overlay["entries"]["1"]["fields"] = {"title_en": "", "line": "", "para": ""}
        publisher.validate_overlay(overlay)
        for fields in ({}, {"title": "Original"}, {"line": "要約"},
                       {"title_en": "", "line": None, "para": "段落"}):
            with self.subTest(fields=fields):
                overlay["entries"]["1"]["fields"] = fields
                with self.assertRaises(ValueError):
                    publisher.validate_overlay(overlay)

    def test_requests_resolve_manifest_paths_with_cache_keys(self):
        source = self.source()
        nested = source / "generated"
        nested.mkdir()
        (source / "topics.json").rename(nested / "topics-current.json")
        (source / "text.json").rename(nested / "paragraphs-current.json")
        manifest = {"files": {"topics": "generated/topics-current.json?v=123",
                              "text": "generated/paragraphs-current.json?v=456#text"}}
        (source / "manifest.json").write_text(json.dumps(manifest))
        archive = self.root / "site-data.tar.gz"
        publisher.pack_site(source, archive)
        hashes = publisher.english_hashes(archive)["requests"]
        loaded = subprocess.run(
            ["node", "--input-type=module", "-e",
             "import {readSourceRecords} from './scripts/localization.mjs';"
             f"console.log(JSON.stringify(readSourceRecords({json.dumps(str(source))},'requests')));"],
            check=True, capture_output=True, text=True,
        )
        self.assertEqual(hashes, {str(record["id"]): record["source_hash"]
                                  for record in json.loads(loaded.stdout)})
        publisher.prepare_localization(source, self.root / "missing", self.root / "prepared")
        for unsafe in ("../topics.json", "/topics.json"):
            manifest["files"]["topics"] = unsafe
            with self.subTest(path=unsafe), self.assertRaisesRegex(ValueError, "escapes"):
                publisher.request_source_paths(manifest)

    def test_requests_preparation_and_publication_keep_current_tips_and_voices(self):
        source = self.source()
        archive = self.root / "site-data.tar.gz"
        publisher.pack_site(source, archive)
        gh = FakeGitHub()
        gh.assets["site-data.tar.gz"] = archive.read_bytes()
        hashes = publisher.english_hashes(archive)
        seeds = self.root / "seeds"
        (seeds / "fr").mkdir(parents=True)
        for page, fields in (
            ("tips", {"headline": "Conseil", "body": "Texte"}),
            ("voices", {"question": "Question", "positions": ["Un", "Deux"], "outcome": "Résultat"}),
            ("requests", {"title_en": "Titre", "line": "Résumé", "para": "Paragraphe"}),
        ):
            overlay = {"version": 1, "locale": "fr", "page": page, "entries": {
                "1": {"source_hash": hashes[page]["1"], "translation_version": "1", "fields": fields},
            }}
            (seeds / "fr" / f"{page}.json").write_text(json.dumps(overlay))
        store = self.root / "prepared"
        publisher.prepare_localization(source, seeds, store)
        first = self.root / "first"
        first.mkdir()
        with gh.lease():
            publisher.publish_localization(gh, "data", store, first, set(gh.assets))
        released = publisher.read_overlays(first / "localization.tar.gz")
        for page in ("requests", "tips", "voices"):
            self.assertIn("1", released[f"data/locales/fr/{page}.json"]["entries"])
        requests_path = store / "fr/requests.json"
        delayed = json.loads(requests_path.read_text())
        delayed["entries"]["1"]["source_hash"] = "0" * 64
        delayed["entries"]["1"]["fields"]["line"] = "Obsolète"
        requests_path.write_text(json.dumps(delayed))
        second = self.root / "second"
        second.mkdir()
        with gh.lease():
            publisher.publish_localization(gh, "data", store, second, set(gh.assets))
        self.assertEqual(released, publisher.read_overlays(second / "localization.tar.gz"))
        self.assertEqual(archive.read_bytes(), gh.assets["site-data.tar.gz"])
        prepared = self.root / "released"
        publisher.prepare_localization(source, self.root / "missing", prepared, second / "localization.tar.gz")
        self.assertEqual(json.loads((prepared / "fr/requests.json").read_text()),
                         released["data/locales/fr/requests.json"])

    def test_preparation_merges_current_seeds_and_release_without_stale_replacement(self):
        source = self.source()
        english = self.root / "site-data.tar.gz"
        publisher.pack_site(source, english)
        digest = publisher.english_hashes(english)["tips"]["1"]
        seeds = self.root / "seeds"
        (seeds / "fr").mkdir(parents=True)
        seed = {"version": 1, "locale": "fr", "page": "tips", "entries": {
            "1": {"source_hash": digest, "translation_version": "1",
                  "fields": {"headline": "Seed", "body": "Texte"}},
        }}
        (seeds / "fr/tips.json").write_text(json.dumps(seed))
        released = json.loads(json.dumps(seed))
        released["entries"]["1"]["source_hash"] = "0" * 64
        released["entries"]["1"]["fields"]["headline"] = "Stale"
        japanese = {**seed, "locale": "ja"}
        asset = self.root / "localization.tar.gz"
        with tarfile.open(asset, "w:gz") as tar:
            for name, document in (("data/locales/fr/tips.json", released),
                                   ("data/locales/ja/tips.json", japanese)):
                encoded = json.dumps(document).encode()
                member = tarfile.TarInfo(name)
                member.size = len(encoded)
                tar.addfile(member, io.BytesIO(encoded))
        store = self.root / "prepared"
        publisher.prepare_localization(source, seeds, store, asset)
        french = json.loads((store / "fr/tips.json").read_text())
        self.assertEqual(french["entries"]["1"]["fields"]["headline"], "Seed")
        self.assertIn("1", json.loads((store / "ja/tips.json").read_text())["entries"])
        self.assertEqual(json.loads((store / "ja/voices.json").read_text())["entries"], {})
        seedless = self.root / "seedless"
        publisher.prepare_localization(source, self.root / "missing-seeds", seedless, asset)
        self.assertIn("1", json.loads((seedless / "ja/tips.json").read_text())["entries"])
        self.assertEqual(json.loads((seedless / "fr/tips.json").read_text())["entries"], {})

    def test_invalid_optional_asset_does_not_block_english_preparation(self):
        source = self.source()
        asset = self.root / "localization.tar.gz"
        asset.write_bytes(b"not an archive")
        store = self.root / "prepared"
        publisher.prepare_localization(source, self.root / "missing-seeds", store, asset)
        self.assertEqual(json.loads((store / "fr/tips.json").read_text())["entries"], {})

    def test_old_translation_version_is_valid_but_never_prepared_as_current(self):
        source = self.source()
        archive = self.root / "site-data.tar.gz"
        publisher.pack_site(source, archive)
        digest = publisher.english_hashes(archive)["tips"]["1"]
        seeds = self.root / "seeds"
        (seeds / "fr").mkdir(parents=True)
        old = {"version": 1, "locale": "fr", "page": "tips", "entries": {
            "1": {"source_hash": digest, "translation_version": "old-v0",
                  "fields": {"headline": "Ancien", "body": "Texte"}},
        }}
        publisher.validate_overlay(old)
        (seeds / "fr/tips.json").write_text(json.dumps(old))
        store = self.root / "prepared"
        publisher.prepare_localization(source, seeds, store)
        self.assertEqual(json.loads((store / "fr/tips.json").read_text())["entries"], {})


class WorkflowPermissionsTests(unittest.TestCase):
    def test_requests_workflow_selection_and_static_pages_packaging(self):
        workflows = Path(__file__).parents[1] / ".github/workflows"
        localization = (workflows / "localization.yml").read_text()
        self.assertIn("options: [all, tips, voices, requests]", localization)
        self.assertIn('("all", "tips", "voices", "requests")', localization)
        pages = (workflows / "pages.yml").read_text()
        self.assertIn("test -f site/requests/index.html", pages)
        self.assertRegex(pages, r"(?m)^          path: site(?:\s+#.*)?$")

    def test_localization_publisher_can_upload_assets_and_dispatch_pages(self):
        workflow = (Path(__file__).parents[1] / ".github/workflows/localization.yml").read_text()
        job = workflow.split("\n  localize:\n", 1)[1].split("\n  translation-status:", 1)[0]
        match = re.search(r"^    permissions:\n((?:^      [^\n]+\n)+)", job, re.MULTILINE)
        self.assertIsNotNone(match, "The publication job needs explicit token permissions")
        permissions = dict(line.strip().split(": ", 1) for line in match.group(1).splitlines())
        self.assertEqual(permissions.get("contents"), "write")
        self.assertEqual(permissions.get("actions"), "write")


if __name__ == "__main__":
    unittest.main()
