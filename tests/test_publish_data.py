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
        self.calls.append(("release", args))
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

    def public_questions(self):
        result = subprocess.run(["node", "--input-type=module", "-e", """
import {COVERAGE,IMPACT,COMPLEXITY,DOMAINS} from './site/js/questions-core.js';
const rating=labels=>({choice:Object.keys(labels)[0],probabilities:Object.fromEntries(Object.keys(labels).map((key,i)=>[key,i===0?1:0]))});
console.log(JSON.stringify({version:1,schema_version:'questions-ui-public-processed-preview-v1',
public_preview:true,private_preview:false,publish_allowed:true,representative:false,generated_at:'2026-11-11T00:00:00Z',
snapshot_scope:'processed',processed_count:2,corpus_total:10,corpus_complete:false,eligible_question_count:1,
entries:[1,2].map(id=>({id,title:'English title '+id,body:'Existing summary',active:null,lang:'fr',accepted:null,closed:false,replies:null,
answers:{has_question:{noul:id===1?.8:.4},coverage:rating(COVERAGE),impact:rating(IMPACT),technicality:rating(COMPLEXITY),
clarification:{noul:.1},...Object.fromEntries(Object.keys(DOMAINS).map(key=>['domain_'+key,{noul:.5}]))}}))}));
"""], check=True, capture_output=True, text=True)
        path = self.root / "questions-candidate.json"
        path.write_text(result.stdout)
        return path, json.loads(result.stdout)

    def test_questions_publication_requires_complete_locales_and_preserves_english_and_other_pages(self):
        source = self.source()
        english = self.root / "site-data.tar.gz"
        publisher.pack_site(source, english)
        candidate, questions = self.public_questions()
        hashes = publisher.questions_hashes(questions)
        store = self.root / "questions-store"
        for locale in ("fr", "ja"):
            (store / locale).mkdir(parents=True)
            overlay = {"version": 1, "locale": locale, "page": "questions", "entries": {
                key: {"source_hash": digest, "translation_version": "1",
                      "field_sources": {"private": "/private/cache/path"},
                      "fields": {"title": "Titre traduit", "body": "Résumé traduit"}}
                for key, digest in hashes.items()
            }}
            (store / locale / "questions.json").write_text(json.dumps(overlay))
        gh = FakeGitHub()
        gh.assets["site-data.tar.gz"] = english.read_bytes()
        first = self.root / "first"
        first.mkdir()
        japanese = store / "ja/questions.json"
        original = japanese.read_text()
        incomplete = json.loads(original)
        del incomplete["entries"]["2"]
        japanese.write_text(json.dumps(incomplete))
        with gh.lease(), self.assertRaisesRegex(ValueError, "Incomplete ja"):
            publisher.publish_questions_preview(gh, "data", candidate, store, first, set(gh.assets))
        self.assertEqual(set(gh.assets), {"site-data.tar.gz"}, "no uploads when completeness fails")
        japanese.write_text(original)
        tip = {"version": 1, "locale": "fr", "page": "tips", "entries": {
            "1": {"source_hash": publisher.english_hashes(english)["tips"]["1"],
                  "translation_version": "1", "fields": {"headline": "Conseil", "body": "Texte"}},
        }}
        existing = self.root / "existing.tar.gz"
        with tarfile.open(existing, "w:gz") as tar:
            data = json.dumps(tip).encode()
            member = tarfile.TarInfo("data/locales/fr/tips.json")
            member.size = len(data)
            tar.addfile(member, io.BytesIO(data))
        gh.assets["localization.tar.gz"] = existing.read_bytes()
        second = self.root / "second"
        second.mkdir()
        with gh.lease():
            publisher.publish_questions_preview(gh, "data", candidate, store, second, set(gh.assets))
        self.assertEqual(gh.assets["site-data.tar.gz"], english.read_bytes())
        documents = publisher.read_overlays(second / "localization.tar.gz")
        self.assertEqual(documents["data/locales/fr/tips.json"], tip)
        for locale in ("fr", "ja"):
            entries = documents[f"data/locales/{locale}/questions.json"]["entries"]
            self.assertEqual(set(entries), set(hashes))
            self.assertNotIn("field_sources", entries["1"])
        uploads = [args[2] for kind, args in gh.calls if kind == "release" and args[0] == "upload"]
        self.assertEqual([Path(path).name for path in uploads], ["localization.tar.gz", "questions-preview.json"])
        published = json.loads(gh.assets["questions-preview.json"])
        self.assertEqual(published["entries"], questions["entries"])
        # A later Requests/Tips publisher must authenticate Questions against the separate current asset.
        third = self.root / "third"
        third.mkdir()
        with gh.lease():
            publisher.publish_localization(gh, "data", store, third, set(gh.assets))
        self.assertEqual(publisher.read_overlays(third / "localization.tar.gz"), documents)
        fourth = self.root / "fourth"
        fourth.mkdir()
        unchanged_assets = dict(gh.assets)
        with gh.lease(), self.assertRaisesRegex(ValueError, "preview changed"):
            publisher.publish_questions_preview(gh, "data", candidate, store, fourth, set(gh.assets),
                                                expected_preview_hash="0" * 64)
        self.assertEqual(gh.assets, unchanged_assets, "a stale base cannot upload either asset")

    def test_expanded_pages_gate_rejects_missing_stale_or_empty_body_translations(self):
        source = self.source()
        candidate, questions = self.public_questions()
        (source / "questions").mkdir()
        (source / "questions/preview.json").write_text(candidate.read_text())
        manifest = json.loads((source / "manifest.json").read_text())
        manifest["questions_view"] = {"public_preview": True, "snapshot_scope": "processed",
                                     "files": {"entries": "questions/preview.json"}}
        (source / "manifest.json").write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, "Incomplete fr"):
            publisher.check_questions_localization(source)
        hashes = publisher.questions_hashes(questions)
        for locale in ("fr", "ja"):
            directory = source / "locales" / locale
            directory.mkdir(parents=True)
            document = {"version": 1, "locale": locale, "page": "questions", "entries": {
                key: {"source_hash": digest, "translation_version": "1",
                      "fields": {"title": "Titre", "body": "Résumé"}} for key, digest in hashes.items()
            }}
            (directory / "questions.json").write_text(json.dumps(document))
        publisher.check_questions_localization(source)
        path = source / "locales/fr/questions.json"
        document = json.loads(path.read_text())
        document["entries"]["1"]["source_hash"] = "0" * 64
        path.write_text(json.dumps(document))
        with self.assertRaisesRegex(ValueError, "Incomplete fr"):
            publisher.check_questions_localization(source)
        document["entries"]["1"]["source_hash"] = hashes["1"]
        document["entries"]["1"]["fields"]["body"] = ""
        path.write_text(json.dumps(document))
        with self.assertRaisesRegex(ValueError, "Incomplete fr"):
            publisher.check_questions_localization(source)

    def test_questions_overlays_reuse_canonical_title_body_in_prepare_and_final_archive(self):
        source = self.source()
        manifest_path = source / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["questions_view"] = {"files": {"entries": "questions/sample.json?t=1"}}
        manifest["questions"] = [{"id": "fr"}]
        manifest_path.write_text(json.dumps(manifest))
        (source / "questions").mkdir()
        rows = {"version": 1, "entries": [
            {"id": 42, "title": "Original English", "body": "Existing summary", "lang": "fr"},
            {"id": 43, "title": "Without summary", "body": None, "lang": "ja"},
        ]}
        (source / "questions/sample.json").write_text(json.dumps(rows))
        hashes = publisher.questions_hashes(rows)
        canonical = json.dumps({"title": "Original English", "body": "Existing summary"},
                               sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        self.assertEqual(hashes["42"], hashlib.sha256(canonical.encode()).hexdigest())
        seeds = self.root / "seeds"
        (seeds / "fr").mkdir(parents=True)
        document = {"version": 1, "locale": "fr", "page": "questions", "entries": {
            "42": {"source_hash": hashes["42"], "translation_version": "1",
                   "fields": {"title": "Titre traduit", "body": "Résumé traduit"}},
            "43": {"source_hash": hashes["43"], "translation_version": "1",
                   "fields": {"title": "Sans résumé", "body": ""}},
        }}
        (seeds / "fr/questions.json").write_text(json.dumps(document))
        prepared = self.root / "prepared"
        publisher.prepare_localization(source, seeds, prepared)
        self.assertEqual(json.loads((prepared / "fr/questions.json").read_text()), document)
        private_document = json.loads(json.dumps(document))
        private_document["entries"]["42"]["field_sources"] = {"private_provenance": "/private/cache/path"}
        (seeds / "fr/questions.json").write_text(json.dumps(private_document))
        publisher.prepare_localization(source, seeds, prepared)
        self.assertEqual(json.loads((prepared / "fr/questions.json").read_text()), document,
                         "even local browser overlays exclude private field provenance")
        self.assertEqual(json.loads((seeds / "fr/questions.json").read_text()), private_document,
                         "durable provenance is retained in the source store")
        archive = self.root / "questions-source.tar.gz"
        publisher.pack_site(source, archive)
        self.assertEqual(publisher.english_hashes(archive)["questions"], hashes)
        self.assertEqual(json.loads(manifest_path.read_text())["questions"], [{"id": "fr"}])
        rows["entries"][0]["body"] = "Changed summary"
        (source / "questions/sample.json").write_text(json.dumps(rows))
        publisher.prepare_localization(source, seeds, prepared)
        self.assertEqual(set(json.loads((prepared / "fr/questions.json").read_text())["entries"]), {"43"})
        with self.assertRaisesRegex(ValueError, "escapes"):
            publisher.questions_source_path({"questions_view": {"files": {"entries": "../private.json"}}})

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

    def test_javascript_embeddings_rewrite_of_python_manifest_keeps_seal(self):
        gh = FakeGitHub()
        source = self.source()
        manifest = source / "manifest.json"
        manifest.write_text(json.dumps({"questions": [{"levels": [{"value": float(k)} for k in "01234"]}],
                                        "cuts": [2.0, 1.682], "embeddings": None}))
        provenance = self.root / "provenance.json"
        publisher.capture_english_base(gh, "data", source, provenance, self.root)
        publisher.seal_english_candidate(source, "data", provenance)
        subprocess.run(["node", "-e", "const fs=require('fs'),p=process.argv[1],m=JSON.parse(fs.readFileSync(p,'utf8'));"
                        "m.embeddings={count:1};fs.writeFileSync(p,JSON.stringify(m))", str(manifest)], check=True)
        self.assertNotIn(".0", manifest.read_text())
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
        self.assertLess(pages.index("Install approved Questions public preview"), pages.index("prepare-localization"))
        self.assertIn("check-questions-localization --data site/data", pages)

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
