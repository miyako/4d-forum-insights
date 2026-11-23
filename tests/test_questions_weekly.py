"""Offline delta reuse, review gates and consistent private checkpoints."""

from contextlib import closing, redirect_stdout
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

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "pipeline/src"))
import questions_weekly as weekly
import questions_weekly_publish as publishing
import questions_weekly_store as store
import questions_weekly_review as independent
from fminer import db, enrich, questions_corpus as corpus
from test_questions import answers, raw
import test_publish_data as publication_fixtures


class WeeklyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "checkpoint"
        self.root.mkdir()
        self.source = Path(self.temp.name) / "source.db"
        self.cfg = weekly.config(ROOT / "config/questions-weekly.json")
        self.cfg["openai_redaction_model"] = "synthetic-2026-01-01"
        with db.connect(self.source) as con:
            for n in (1, 2):
                con.execute(
                    """INSERT INTO topics(id,title,title_en,lang,first_post_en,thread_raw,posts_count,
                       enriched_at,raw_changed_at,has_accepted_answer,closed,summary_paragraph)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (n, "How does ORDA work?", "How does ORDA work?", "en", "Question?",
                     raw(["Question?", "Use ORDA in 4D v20."]), 2, "2026-01-02", "2026-01-01",
                     0, 0, "Use ORDA in 4D v20."))
        with closing(corpus.connect(self.source, self.root / "ratings.db")) as con:
            corpus.prepare(con, self.source)
            corpus.set_state(con, "runtime", {"model": self.cfg["decision_model"],
                                              "revision": self.cfg["decision_revision"],
                                              "prompt": corpus.PROMPT_VERSION, "evidence": corpus.EVIDENCE_VERSION})
            con.execute("""UPDATE questions_decisions SET answers=?,decision_input_hash=input_hash,
                        decision_model=?,model_revision=?,decision_version=?,evidence_version=?""",
                        (json.dumps(answers()), self.cfg["decision_model"], self.cfg["decision_revision"],
                         corpus.PROMPT_VERSION, corpus.EVIDENCE_VERSION))
            con.commit()
        with closing(weekly.display_connect(self.root / "display.db")):
            pass
        self.write_readiness()

    def tearDown(self):
        self.temp.cleanup()

    def write_readiness(self):
        evaluation = {"schema": "questions-redaction-heldout-v1", "identity": weekly.identity(self.cfg),
                      "provider": "openai", "model_snapshot": self.cfg["openai_redaction_model"],
                      "source_linked": True, "independent_heldout": True, "review_approved": True,
                      "dataset_sha256": "a" * 64,
                      "languages": {lang: {"identifiers": 10, "missed": 0, "minimum_recall": 1}
                                    for lang in ("en", "fr", "ja")}}
        (self.root / "evaluation.json").write_text(json.dumps(evaluation))
        readiness = {"schema": "questions-weekly-readiness-v1", "identity": weekly.identity(self.cfg),
                     "initial_en_fr_ja_published": True, "weekly_private_processing_authorized": True,
                     "openai_redaction_authorized": True,
                     "automated_review_authorized": True, "weekly_publication_authorized": True,
                     "evaluation_sha256": corpus.file_hash(self.root / "evaluation.json"),
                     "reviewer": "synthetic-test-only", "reviewed_at": "2026-01-03T00:00:00Z"}
        weekly.capture_delta(self.source, self.root)
        delta = json.loads((self.root / "delta-source.json").read_text())
        delta["privacy_pending"] = [1, 2]
        (self.root / "delta-source.json").write_text(json.dumps(delta))
        readiness["raw_delta_seed_sha256"] = json.loads((self.root / "delta-source.json").read_text())["initial_baseline_sha256"]
        (self.root / "readiness.json").write_text(json.dumps(readiness))
        (self.root / "reviews.json").write_text(json.dumps({"schema": "questions-weekly-reviews-v1", "entries": {}}))

    @staticmethod
    def detector(text, posts, model, **kwargs):
        return text, {"spans": [], "original_hash": weekly.privacy.digest(text)}

    def propose(self, detector=None):
        with redirect_stdout(io.StringIO()):
            weekly.proposals(self.source, self.root, self.cfg, detector=detector or self.detector)

    def test_uncleared_weekly_fields_stay_pending_without_hosted_calls_or_retries(self):
        (self.root / "hosted-input-clearance.json").write_text(json.dumps({
            "schema": "questions-hosted-input-clearance-v1",
            "hashes": [weekly.privacy.digest("An unrelated cleared field.")],
        }))
        with patch.object(weekly.openai_privacy, "complete", side_effect=AssertionError("network attempted")), \
                redirect_stdout(io.StringIO()):
            weekly.proposals(self.source, self.root, self.cfg)
        with closing(weekly.display_connect(self.root / "display.db")) as con:
            rows = con.execute("SELECT outcome, attempts, error FROM display_fields").fetchall()
            self.assertEqual([tuple(row) for row in rows], [("pending", 0, "hosted_input_not_cleared")] * 4)
        self.assertEqual(json.loads((self.root / "delta-source.json").read_text())["privacy_pending"], [1, 2])

    def approve(self):
        entries = {}
        with closing(weekly.display_connect(self.root / "display.db")) as con:
            for row in con.execute("SELECT * FROM displays"):
                entries[str(row["topic_id"])] = {
                    "schema": "questions-weekly-display-review-v1",
                    **{key: row[key] for key in ("source_hash", "freshness_hash", "display_hash", "audit_hash")},
                    "source_linked": True, "technical_meaning_preserved": True, "privacy_cleared": True,
                    "openai_authorized": True, "reviewer": "synthetic-test-only",
                    "reviewed_at": "2026-01-03T00:00:00Z"}
        (self.root / "reviews.json").write_text(json.dumps({"schema": "questions-weekly-reviews-v1", "entries": entries}))

    @staticmethod
    def independent_check(original, output, posts, model, **kwargs):
        decision = {"privacy_clear": True, "meaning_preserved": True, "uncertain": False}
        return decision, {"version": independent.reviewer.VERSION, "model": model,
                          "original_hash": weekly.privacy.digest(original),
                          "display_hash": weekly.privacy.digest(output), "decision": decision,
                          "response_hash": weekly.privacy.digest(decision), "model_calls": 1,
                          "usage": {"prompt_tokens": 10, "completion_tokens": 5}}

    def independent_review(self, locale="en", checker=None):
        with redirect_stdout(io.StringIO()):
            independent.review_fields(self.source, self.root, self.cfg, locale,
                                      review=checker or self.independent_check)

    def seed_translation_overlays(self):
        source = {row["id"]: row for row in self.exported()}
        for locale in ("fr", "ja"):
            directory = self.root / "locales" / locale
            directory.mkdir(parents=True, exist_ok=True)
            document = {"version": 1, "locale": locale, "page": "questions", "entries": {
                str(id): {"source_hash": weekly.privacy.digest({"title": row["title"], "body": row["body"]}),
                          "translation_version": "1", "fields": {"title": row["title"], "body": row["body"]}}
                for id, row in source.items()}}
            (directory / "questions.json").write_text(json.dumps(document))

    def test_independent_checks_create_exact_approvals_and_unchanged_rerun_has_no_requests(self):
        self.propose()
        self.assertEqual(self.exported(), [])
        self.independent_review()
        self.assertEqual(len(self.exported()), 2)
        self.independent_review(checker=lambda *args, **kwargs: self.fail("unchanged review requested"))
        self.seed_translation_overlays()
        for locale in ("fr", "ja"):
            self.independent_review(locale)
        file = self.root / "locales/fr/questions.json"
        overlay = json.loads(file.read_text())
        overlay["entries"]["2"]["fields"]["title"] = "Nouvelle question ORDA?"
        file.write_text(json.dumps(overlay))
        calls = []
        def check(*args, **kwargs):
            calls.append((args[1], kwargs["locale"]))
            return self.independent_check(*args, **kwargs)
        self.independent_review("fr", check)
        self.independent_review("ja", lambda *args, **kwargs: self.fail("unchanged Japanese requested"))
        self.assertEqual(calls, [("Nouvelle question ORDA?", "fr")])

    def test_rejection_or_interrupted_check_never_becomes_clearance(self):
        self.propose()
        def rejecting(*args, **kwargs):
            decision, audit = self.independent_check(*args, **kwargs)
            decision["meaning_preserved"] = False
            audit["response_hash"] = weekly.privacy.digest(decision)
            return decision, audit
        with self.assertRaises(RuntimeError):
            self.independent_review(checker=rejecting)
        self.assertEqual(self.exported(), [])
        with self.assertRaises(RuntimeError):
            self.independent_review(checker=lambda *args, **kwargs: self.fail("rejected field retried"))
        with closing(weekly.display_connect(self.root / "display.db")) as con:
            con.execute("DELETE FROM review_fields")
            con.commit()
        calls = []
        def failing(original, *args, **kwargs):
            calls.append(original)
            if original.startswith("How"):
                raise weekly.privacy.DetectionFailure(2, "timeout")
            return self.independent_check(original, *args, **kwargs)
        with self.assertRaises(RuntimeError):
            self.independent_review(checker=failing)
        self.assertIn("Use ORDA in 4D v20.", calls)
        with closing(weekly.display_connect(self.root / "display.db")) as con:
            title = con.execute("SELECT attempts,outcome FROM review_fields WHERE topic_id=1 AND field='title'").fetchone()
            self.assertEqual(tuple(title), (2, "held"))
        self.assertEqual(self.exported(), [])

    def test_automatic_publication_review_requires_all_three_languages_and_exact_proofs(self):
        self.propose()
        self.independent_review()
        self.seed_translation_overlays()
        self.independent_review("fr")
        with redirect_stdout(io.StringIO()):
            weekly.candidate(self.source, self.root, self.cfg)
        with self.assertRaisesRegex(ValueError, "translation reviews"):
            independent.authorize_publication(self.source, self.root, self.cfg, "c" * 64)
        self.independent_review("ja")
        with redirect_stdout(io.StringIO()):
            independent.authorize_publication(self.source, self.root, self.cfg, "c" * 64)
        publishing.publication_review(self.root, self.cfg, None)
        with (self.root / "portable-locales/fr/questions.json").open("a") as stream:
            stream.write("\n")
        with self.assertRaisesRegex(ValueError, "exact-byte"):
            publishing.publication_review(self.root, self.cfg, None)
        reviews_path = self.root / "reviews.json"
        reviews = json.loads(reviews_path.read_text())
        reviews["entries"]["1"]["proof_sha256"]["title"] = "0" * 64
        reviews_path.write_text(json.dumps(reviews))
        with self.assertRaisesRegex(ValueError, "proofs differ"):
            independent.authorize_publication(self.source, self.root, self.cfg, "c" * 64)

    def test_offline_automatic_publication_checks_real_preflight_and_saves_reviewed_checkpoint(self):
        self.propose()
        self.independent_review()
        self.seed_translation_overlays()
        for locale in ("fr", "ja"):
            self.independent_review(locale)
        with redirect_stdout(io.StringIO()):
            weekly.candidate(self.source, self.root, self.cfg)
        gh = publication_fixtures.FakeGitHub()
        gh.assets["questions-preview.json"] = (self.root / "publication/public-preview.json").read_bytes()
        fixture = publication_fixtures.PublicationTests()
        fixture.root = Path(self.temp.name) / "published-english"
        fixture.root.mkdir()
        archive = fixture.root / "site-data.tar.gz"
        weekly.publisher.pack_site(fixture.source(), archive)
        original_english = archive.read_bytes()
        gh.assets["site-data.tar.gz"] = original_english
        calls = []
        original_run = subprocess.run
        original_read = Path.read_text
        def run(command, *args, **kwargs):
            if command[:2] == ["scripts/db-store.sh", "pull"]:
                weekly.backup(self.source, Path(command[2]))
                return subprocess.CompletedProcess(command, 0)
            if command[:3] == ["python3", "scripts/questions_weekly_store.py", "push"]:
                directory = Path(self.temp.name) / "saved-checkpoint"
                directory.mkdir()
                packed = store.pack(self.root, directory)
                with tarfile.open(packed) as tar:
                    self.assertIn("translation-reviews.json", tar.getnames())
                    self.assertIn("publication-review.json", tar.getnames())
                self.assertFalse(any(kind == "release" and args[0] == "upload" for kind, args in gh.calls))
                calls.append("checkpoint")
                return subprocess.CompletedProcess(command, 0)
            return original_run(command, *args, **kwargs)
        def read(path, *args, **kwargs):
            if path.resolve() == ROOT / "config/questions-preview.json":
                return '{"enabled":true,"complete_enabled":true}'
            return original_read(path, *args, **kwargs)
        with patch.dict("os.environ", {"PUBLICATION_ENABLED": "true"}), \
                patch.object(publishing.publisher, "GitHub", return_value=gh), \
                patch.object(publishing.subprocess, "run", side_effect=run), \
                patch.object(Path, "read_text", read), redirect_stdout(io.StringIO()):
            publishing.publish(self.root, self.cfg)
        self.assertEqual(calls, ["checkpoint"])
        self.assertEqual(gh.assets["site-data.tar.gz"], original_english)
        self.assertTrue(json.loads(gh.assets["questions-preview.json"])["corpus_complete"])
        self.assertIn("localization.tar.gz", gh.assets)
        self.assertIsNone(gh.owner)

    def exported(self):
        with redirect_stdout(io.StringIO()):
            weekly.export_cleared(self.source, self.root, self.cfg, self.root / "embedding")
        return json.loads((self.root / "embedding/questions/entries.json").read_text())["entries"]

    def test_unreviewed_proposals_never_enter_translation_then_exact_clearance_reuses(self):
        self.propose()
        self.assertEqual(self.exported(), [])
        self.approve()
        self.assertEqual(len(self.exported()), 2)
        with patch.object(weekly.openai_privacy, "detect", side_effect=AssertionError("no new inference")):
            self.propose()
        manifest = json.loads((self.root / "embedding/manifest.json").read_text())
        self.assertEqual(manifest["questions"], [])
        self.assertFalse(manifest["publish_allowed"])

    def test_changed_evidence_invalidates_only_changed_rating_display_and_review(self):
        self.propose()
        self.approve()
        with db.connect(self.source) as con:
            con.execute("UPDATE topics SET thread_raw=? WHERE id=2", (raw(["Edited question?", "Reply."]),))
        delta = json.loads((self.root / "delta-source.json").read_text())
        delta["privacy_pending"] = [2]
        (self.root / "delta-source.json").write_text(json.dumps(delta))
        with redirect_stdout(io.StringIO()):
            weekly.prepare(self.source, self.root)
        with closing(corpus.connect(self.source, self.root / "ratings.db")) as con:
            rows = con.execute("SELECT * FROM questions_decisions ORDER BY topic_id").fetchall()
            self.assertTrue(corpus.current(rows[0], self.cfg["decision_model"], self.cfg["decision_revision"]))
            self.assertFalse(corpus.current(rows[1], self.cfg["decision_model"], self.cfg["decision_revision"]))
        self.assertEqual([row["id"] for row in self.exported()], [1])
        with closing(corpus.connect(self.source, self.root / "ratings.db")) as con:
            con.execute("UPDATE questions_decisions SET decision_input_hash=input_hash WHERE topic_id=2")
            con.commit()
        calls = []

        def detect(text, *args, **kwargs):
            calls.append(text)
            return self.detector(text, *args, **kwargs)
        self.propose(detect)
        self.assertEqual(len(calls), 2) # Two fields of changed topic only.
        self.assertEqual([row["id"] for row in self.exported()], [1]) # Identical wording is not fresh approval.

    def test_validation_failure_invalidates_output_and_prevents_complete_candidate(self):
        self.propose()
        self.approve()
        with closing(weekly.display_connect(self.root / "display.db")) as con:
            con.execute("UPDATE displays SET error='retry' WHERE topic_id=2")
            con.commit()
        delta = json.loads((self.root / "delta-source.json").read_text())
        delta["privacy_pending"] = [2]
        (self.root / "delta-source.json").write_text(json.dumps(delta))
        def failing(*args, **kwargs):
            raise ValueError("private response details")
        with self.assertRaises(RuntimeError):
            self.propose(failing)
        self.assertEqual([row["id"] for row in self.exported()], [1])
        with redirect_stdout(io.StringIO()), self.assertRaisesRegex(RuntimeError, "publication held"):
            weekly.candidate(self.source, self.root, self.cfg)

    def test_original_poster_change_invalidates_only_its_privacy_clearance(self):
        self.propose()
        self.approve()
        with db.connect(self.source) as con:
            con.execute("UPDATE topics SET op_username='NewSourceOP' WHERE id=2")
        self.assertEqual([row["id"] for row in self.exported()], [1])

    def test_title_failure_does_not_skip_body_and_delta_queue_survives(self):
        calls = []
        def detector(text, *args, **kwargs):
            calls.append(text)
            if text == "How does ORDA work?":
                raise weekly.privacy.DetectionFailure(2, "uncertain")
            return text, {"spans": [], "model_calls": 1,
                          "usage": {"prompt_tokens": 10, "completion_tokens": 5}, "usage_unreported_calls": 0}
        with self.assertRaises(RuntimeError):
            self.propose(detector)
        with closing(weekly.display_connect(self.root / "display.db")) as con:
            results = con.execute("SELECT field,outcome,attempts,model_calls FROM display_fields WHERE topic_id=1 ORDER BY field").fetchall()
            self.assertEqual([tuple(row) for row in results],
                             [("body", "review_required", 1, 1), ("title", "held", 2, 4)])
            self.assertIsNone(con.execute("SELECT fields FROM displays WHERE topic_id=1").fetchone()[0])
        self.assertIn("Use ORDA in 4D v20.", calls)
        self.assertEqual(json.loads((self.root / "delta-source.json").read_text())["privacy_pending"], [1, 2])
        self.assertEqual(self.exported(), [])
        with self.assertRaises(RuntimeError):
            self.propose(lambda *args, **kwargs: self.fail("exhausted or successful fields retried"))

    def test_proposal_queue_only_processes_new_or_changed_ids(self):
        delta = json.loads((self.root / "delta-source.json").read_text())
        delta["privacy_pending"] = [2]
        (self.root / "delta-source.json").write_text(json.dumps(delta))
        self.propose()
        with closing(weekly.display_connect(self.root / "display.db")) as con:
            self.assertEqual([row[0] for row in con.execute("SELECT topic_id FROM displays")], [2])
        self.assertEqual(json.loads((self.root / "delta-source.json").read_text())["privacy_pending"], [])

    def test_openai_processing_requires_exact_authorized_model_evaluation(self):
        gate = json.loads((self.root / "readiness.json").read_text())
        gate["openai_redaction_authorized"] = False
        (self.root / "readiness.json").write_text(json.dumps(gate))
        with self.assertRaisesRegex(ValueError, "not authorized"):
            self.propose(lambda *args, **kwargs: self.fail("unapproved request"))
        with self.assertRaises(ValueError):
            weekly.readiness(self.root, {**self.cfg, "openai_redaction_model": "different-2026-01-01"})

    def test_model_and_measured_recall_changes_block_readiness(self):
        weekly.readiness(self.root, self.cfg)
        with self.assertRaises(ValueError):
            weekly.readiness(self.root, {**self.cfg, "redaction_sha256": "b" * 64})
        evidence = json.loads((self.root / "evaluation.json").read_text())
        evidence["languages"]["ja"]["missed"] = 1
        (self.root / "evaluation.json").write_text(json.dumps(evidence))
        gate = json.loads((self.root / "readiness.json").read_text())
        gate["evaluation_sha256"] = corpus.file_hash(self.root / "evaluation.json")
        (self.root / "readiness.json").write_text(json.dumps(gate))
        with self.assertRaisesRegex(ValueError, "reviewed policy"):
            weekly.readiness(self.root, self.cfg)

    def test_unresolved_accounting_covers_every_topic_without_private_text(self):
        self.propose()
        with redirect_stdout(io.StringIO()):
            report = weekly.status(self.source, self.root, self.cfg)
        self.assertEqual(report["topics"], 2)
        self.assertEqual(report["unresolved"], 2)
        self.assertEqual(report["counts"]["display_unreviewed"], 2)
        self.approve()
        with redirect_stdout(io.StringIO()):
            report = weekly.status(self.source, self.root, self.cfg)
        self.assertEqual(report["unresolved"], 0)
        self.assertEqual(report["counts"]["fr_missing_or_stale"], 2)
        self.assertNotIn("ORDA", json.dumps(report))

    def test_raw_delta_enrichment_ignores_old_backlog_and_metadata_only_changes(self):
        with closing(db.connect(self.source)) as con:
            con.execute("UPDATE topics SET first_post_raw='Original opening'")
            con.execute("UPDATE topics SET enriched_at=NULL WHERE id=1")
            con.commit()
        (self.root / "delta-source.json").unlink()
        weekly.capture_delta(self.source, self.root)
        with patch.object(weekly.enrich, "run") as run, redirect_stdout(io.StringIO()):
            weekly.enrich_delta(self.source, self.root, self.cfg, "http://127.0.0.1:18081/v1")
            run.assert_not_called()
        with closing(db.connect(self.source)) as con:
            con.execute("UPDATE topics SET title='Edited title',thread_raw=? WHERE id=2",
                        (raw(["Edited question?", "New reply."]),))
            con.commit()
        def complete(con, endpoint, model, **kwargs):
            self.assertEqual(kwargs["topic_ids"], [2])
            self.assertTrue(kwargs["redo"])
            con.execute("UPDATE topics SET enriched_at='2026-02-03',first_post_en='Edited question?' WHERE id=2")
            con.commit()
        with patch.object(weekly.enrich, "run", side_effect=complete), redirect_stdout(io.StringIO()):
            weekly.enrich_delta(self.source, self.root, self.cfg, "http://127.0.0.1:18081/v1")
        self.assertEqual(json.loads((self.root / "delta-source.json").read_text())["pending"], [])
        with closing(db.connect(self.source)) as con:
            self.assertIsNone(con.execute("SELECT enriched_at FROM topics WHERE id=1").fetchone()[0])

    def test_optional_enrichment_filter_is_applied_before_limit(self):
        with closing(db.connect(self.source)) as con:
            con.execute("UPDATE topics SET first_post_raw='Original opening'")
            con.commit()
            with patch.object(enrich, "LLM") as llm, patch.object(enrich, "_run_pool") as pool:
                llm.return_value.model_id.return_value = "local-gemma"
                enrich.run(con, "http://127.0.0.1:18081/v1", "local-gemma", redo=True,
                           topic_ids=[1], limit=1, log=lambda _: None)
                self.assertEqual([row["id"] for row in pool.call_args.args[1]], [1])
            with self.assertRaises(ValueError):
                enrich.run(con, "", "", topic_ids=[False])

    def test_complete_domain_labels_are_probability_based_including_nonquestions(self):
        labels = answers()
        labels["has_question"]["noul"] = 0.1
        for domain in weekly.questions.DOMAINS:
            labels["domain_" + domain]["noul"] = 0.0
        labels["domain_database"]["noul"] = 0.9
        labels["domain_coding"]["noul"] = 0.6
        labels["domain_interface"]["noul"] = 0.599
        entry = {"id": 1, "answers": labels, "domains": ["interface"]}
        self.assertEqual(weekly.complete_public_entry(entry)["domains"], ["coding", "database"])
        self.assertEqual(entry["domains"], ["interface"])
        labels["domain_coding"]["noul"] = 0
        labels["domain_database"]["noul"] = 0
        self.assertEqual(weekly.complete_public_entry(entry)["domains"], [])

    def test_publication_exact_locale_hash_and_default_off_gate(self):
        self.propose()
        self.approve()
        source = {row["id"]: row for row in self.exported()}
        for locale in ("fr", "ja"):
            directory = self.root / "locales" / locale
            directory.mkdir(parents=True)
            document = {"version": 1, "locale": locale, "page": "questions", "entries": {
                str(id): {"source_hash": weekly.privacy.digest({"title": row["title"], "body": row["body"]}),
                          "translation_version": "1", "fields": {"title": row["title"], "body": row["body"]}}
                for id, row in source.items()}}
            (directory / "questions.json").write_text(json.dumps(document))
        with redirect_stdout(io.StringIO()):
            weekly.candidate(self.source, self.root, self.cfg)
        private = json.loads((self.root / "publication/candidate.json").read_text())
        public = json.loads((self.root / "publication/public-preview.json").read_text())
        self.assertEqual(public["snapshot_scope"], "processed")
        self.assertTrue(public["corpus_complete"])
        self.assertNotIn("domains", private["entries"][0])
        self.assertEqual(public["entries"][0]["domains"], list(weekly.questions.DOMAINS))
        # Shared preflight format is separately reviewed; this fixture is never production clearance.
        (self.root / "ui-publication-review.json").write_text("{}")
        review = {"schema": "questions-weekly-publication-review-v1", "identity": weekly.identity(self.cfg),
                  "source_linked": True, "translated_fields_reviewed": True, "complete_current_locales": True,
                  "publication_authorized": True, "technical_meaning_preserved": True,
                  "reviewer": "synthetic-test-only", "reviewed_at": "2026-01-03T00:00:00Z",
                  "ui_review_sha256": corpus.file_hash(self.root / "ui-publication-review.json"),
                  "expected_live_preview_sha256": "c" * 64, "sha256": {
                      name: corpus.file_hash(self.root / name) for name in
                      ("publication/candidate.json", "publication/audit.json", "publication/public-preview.json",
                       "portable-locales/fr/questions.json", "portable-locales/ja/questions.json")}}
        path = self.root / "publication-review.json"
        path.write_text(json.dumps(review))
        review_hash = corpus.file_hash(path)
        publishing.publication_review(self.root, self.cfg, review_hash)
        preview_path = self.root / "publication/public-preview.json"
        original_preview = preview_path.read_bytes()
        public["entries"][0]["domains"] = []
        preview_path.write_text(json.dumps(public))
        review["sha256"]["publication/public-preview.json"] = corpus.file_hash(preview_path)
        path.write_text(json.dumps(review))
        with self.assertRaisesRegex(ValueError, "corpus/provenance mismatch"):
            publishing.publication_review(self.root, self.cfg, corpus.file_hash(path))
        preview_path.write_bytes(original_preview)
        review["sha256"]["publication/public-preview.json"] = corpus.file_hash(preview_path)
        path.write_text(json.dumps(review))
        review_hash = corpus.file_hash(path)
        with patch.dict("os.environ", {"PUBLICATION_ENABLED": "false"}), \
                patch.object(publishing.publisher, "GitHub", side_effect=AssertionError("no network")), \
                self.assertRaisesRegex(ValueError, "OFF"):
            publishing.publish(self.root, self.cfg, review_hash)
        with (self.root / "portable-locales/ja/questions.json").open("a") as stream:
            stream.write("\n")
        with self.assertRaisesRegex(ValueError, "exact-byte"):
            publishing.publication_review(self.root, self.cfg, review_hash)

    def test_sqlite_backup_includes_committed_wal_and_archive_integrity(self):
        with closing(sqlite3.connect(self.root / "display.db")) as con:
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("INSERT INTO displays(topic_id,freshness_hash,source_hash,error) VALUES (9,'f','s','held')")
            con.commit()
            target = Path(self.temp.name) / "independent" / "display.db"
            weekly.backup(self.root / "display.db", target)
            with closing(sqlite3.connect(target)) as copy:
                self.assertEqual(copy.execute("SELECT topic_id FROM displays").fetchone()[0], 9)
            self.assertEqual(target.with_suffix(".db.sha256").read_text().strip(), corpus.file_hash(target))
            directory = Path(self.temp.name) / "pack"
            directory.mkdir()
            archive = store.pack(self.root, directory)
            restored = Path(self.temp.name) / "restored"
            store.unpack(archive, restored)
            with closing(sqlite3.connect(restored / "display.db")) as copy:
                self.assertEqual(copy.execute("SELECT topic_id FROM displays").fetchone()[0], 9)
        unsafe = Path(self.temp.name) / "unsafe.tar"
        with tarfile.open(unsafe, "w") as tar:
            item = tarfile.TarInfo("../escape.json")
            tar.addfile(item, io.BytesIO(b""))
        with self.assertRaises(ValueError):
            store.unpack(unsafe, Path(self.temp.name) / "unsafe-restore")

    @unittest.skipUnless(shutil.which("zstd") and shutil.which("openssl"), "requires zstd and openssl")
    def test_local_encrypted_baseline_backup_needs_no_review_and_cannot_overwrite(self):
        for name in ("display.db", "readiness.json", "evaluation.json", "reviews.json"):
            (self.root / name).unlink()
        output = Path(self.temp.name) / store.ASSET
        with patch.dict(os.environ, {"DB_KEY": "test-only-passphrase"}):
            store.backup(self.root, output)
            before = output.read_bytes()
            with self.assertRaises(FileExistsError):
                store.backup(self.root, output)
            self.assertEqual(output.read_bytes(), before)
            store.restore(output, Path(self.temp.name) / "restored-baseline", Path(self.temp.name))
        self.assertFalse((Path(self.temp.name) / "restored-baseline/readiness.json").exists())
        with self.assertRaisesRegex(ValueError, "required files"):
            store.pack(self.root, Path(self.temp.name))


if __name__ == "__main__":
    unittest.main()
