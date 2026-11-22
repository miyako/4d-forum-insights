"""Synthetic local-only scope, resumability, validation measurement and server-identity tests."""

from contextlib import closing, redirect_stdout
import io
import json
from pathlib import Path
import shlex
import sqlite3
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import questions_local_privacy as local
from questions_privacy_fixture import fixture
import test_questions_weekly as helpers


class LocalTests(unittest.TestCase):
    def setUp(self):
        self.helper = helpers.WeeklyTests()
        self.helper.setUp()
        self.cfg, self.root = self.helper.cfg, self.helper.root
        self.source = self.root / "source-snapshot.db"
        local.weekly.backup(self.helper.source, self.source)
        self.candidate = self.root / "candidate.json"
        self.candidate.write_text(json.dumps({"private_preview": True, "publish_allowed": False, "entries": [
            {"id": 1, "title": "AlicePrivate uses ORDA.", "body": "4D v20 uses ORDA."},
            {"id": 2, "title": "Question in another source language?", "body": "ORDA works."},
        ]}))
        self.candidate_sha = local.corpus.file_hash(self.candidate)
        with closing(local._readonly_snapshot(self.source)) as con:
            self.source_sha = local.corpus.content_hash(con.execute("SELECT * FROM topics ORDER BY id"))

    def tearDown(self):
        self.helper.tearDown()

    def propose(self, detector, limit=1):
        with redirect_stdout(io.StringIO()):
            return local.proposals(self.source, self.candidate, self.candidate_sha, self.source_sha,
                                   self.root / "local-results", None, self.cfg, limit=limit, detector=detector)

    def test_all_required_slots_persist_before_model_and_resume_without_clearance(self):
        def detector(text, *args, **kwargs):
            return text.replace("AlicePrivate", "[redacted]"), {"spans": []}
        report = self.propose(detector)
        self.assertEqual(report["required"], 2)
        self.assertEqual(report["counts"], {"pending": 1, "review_required": 1})
        self.assertEqual(report["privacy_cleared"], 0)
        self.assertEqual(report["unresolved"], 2)
        report = self.propose(detector)
        self.assertEqual(report["counts"], {"review_required": 2})
        self.assertEqual(report["proposal_unresolved"], 0)
        self.assertFalse(report["publication_allowed"])
        with closing(sqlite3.connect(self.root / "local-results/records.db")) as con:
            self.assertEqual(con.execute("SELECT COUNT(DISTINCT topic_id) FROM records").fetchone()[0], 2)

    def test_failure_is_held_and_source_hash_or_scope_changes_cannot_overwrite_results(self):
        def fail(*args, **kwargs):
            raise ValueError("bad model")
        report = self.propose(fail)
        self.assertEqual(report["counts"], {"held": 1, "pending": 1})
        with self.assertRaises(ValueError):
            local.proposals(self.source, self.candidate, self.candidate_sha, self.source_sha,
                            self.root / "local-results", [2], self.cfg, detector=fail)
        with self.assertRaises(ValueError):
            local.proposals(self.source, self.candidate, self.candidate_sha, "a" * 64,
                            self.root / "wrong-source", None, self.cfg, detector=fail)

    def test_source_original_poster_is_passed_without_inventing_posts_and_cannot_leak(self):
        with closing(local.sqlite3.connect(self.source)) as con:
            con.execute("UPDATE topics SET op_username='OnlyHiddenOP' WHERE id=1")
            con.commit()
        with closing(local._readonly_snapshot(self.source)) as con:
            self.source_sha = local.corpus.content_hash(con.execute("SELECT * FROM topics ORDER BY id"))
        doc = json.loads(self.candidate.read_text())
        doc["entries"][0]["title"] = "OnlyHiddenOP uses ORDA."
        self.candidate.write_text(json.dumps(doc))
        self.candidate_sha = local.corpus.file_hash(self.candidate)
        def detector(text, posts, *args, **kwargs):
            self.assertEqual(kwargs["op_username"], "OnlyHiddenOP")
            self.assertNotIn("OnlyHiddenOP", {post["user"] for post in posts})
            return text, {"spans": []}
        report = self.propose(detector)
        self.assertEqual(report["counts"], {"held": 1, "pending": 1})

    def test_persistent_holds_cannot_starve_pending_ids_and_retry_cap_preserves_holds(self):
        def fail(*args, **kwargs):
            raise local.privacy.DetectionFailure(2, "exact_text_ambiguous")
        self.propose(fail)
        report = self.propose(fail)
        self.assertEqual(report["counts"], {"held": 2})
        with closing(sqlite3.connect(self.root / "local-results/records.db")) as con:
            self.assertEqual(con.execute("SELECT topic_id,attempts FROM records ORDER BY topic_id").fetchall(),
                             [(1, 1), (2, 1)])
        self.propose(fail, limit=2)
        report = self.propose(lambda *args, **kwargs: self.fail("retry cap ignored"), limit=2)
        self.assertEqual(report["counts"], {"held": 2})
        self.assertEqual(report["retry_exhausted"], 2)
        self.assertEqual(report["held_reasons"], {"exact_text_ambiguous": 2})
        self.assertFalse(report["publication_allowed"])

    def test_held_audit_has_field_hash_reason_but_no_exception_or_model_text(self):
        def fail(*args, **kwargs):
            raise ValueError("private model/source text must never be recorded")
        self.propose(fail)
        with closing(sqlite3.connect(self.root / "local-results/records.db")) as con:
            fields, audit, error = con.execute("SELECT fields,audit,error FROM records WHERE topic_id=1").fetchone()
        self.assertIsNone(fields)
        self.assertNotIn("private model/source text", audit + error)
        self.assertEqual(error, "input_or_transport_validation")
        self.assertEqual(json.loads(audit)["failed_field"], "title")

    def test_interrupted_attempt_is_durable_and_does_not_starve_pending(self):
        with self.assertRaises(KeyboardInterrupt):
            self.propose(lambda *args, **kwargs: (_ for _ in ()).throw(KeyboardInterrupt()))
        with closing(sqlite3.connect(self.root / "local-results/records.db")) as con:
            self.assertEqual(con.execute("SELECT outcome,error,attempts FROM records WHERE topic_id=1").fetchone(),
                             ("held", "attempt_interrupted", 1))
        self.propose(lambda text, *args, **kwargs: (text, {"spans": []}))
        with closing(sqlite3.connect(self.root / "local-results/records.db")) as con:
            self.assertEqual(con.execute("SELECT outcome FROM records WHERE topic_id=2").fetchone()[0],
                             "review_required")

    def test_missing_source_evidence_is_held_without_model_call(self):
        with closing(sqlite3.connect(self.source)) as con:
            con.execute("UPDATE topics SET thread_raw='' WHERE id=1")
            con.commit()
        with closing(local._readonly_snapshot(self.source)) as con:
            self.source_sha = local.corpus.content_hash(con.execute("SELECT * FROM topics ORDER BY id"))
        report = self.propose(lambda *args, **kwargs: self.fail("missing evidence reached model"))
        self.assertEqual(report["held_reasons"], {"source_evidence_missing": 1})

    def test_synthetic_gold_measurement_counts_misses_overredaction_and_failures(self):
        data = fixture()
        self.assertEqual(len(data["cases"]), 27)
        golden = self.root / "gold.json"
        golden.write_text(json.dumps(data))
        by_text = {row["text"]: row for row in data["cases"]}
        def detector(text, *args, **kwargs):
            case = by_text[text]
            if case["id"] == "ja-phone":
                raise local.privacy.DetectionFailure(2)
            spans = case["expected_spans"]
            if case["id"] == "en-negative":
                spans = [{"start": 0, "end": 4, "text": text[:4], "kind": "person"}]
            return "", {"spans": spans, "model_calls": 1}
        with redirect_stdout(io.StringIO()):
            report = local.evaluate(golden, local.corpus.file_hash(golden), self.root / "measured",
                                    self.cfg, "http://127.0.0.1:18081/v1", detector=detector)
        self.assertEqual(report["languages"]["ja"]["missed"], 1)
        self.assertEqual(report["languages"]["en"]["non_pi_characters_redacted"], 4)
        self.assertEqual(report["languages"]["ja"]["held_cases"], 1)
        self.assertEqual(report["span_kinds"]["ja/phone"]["missed"], 1)
        self.assertEqual(report["model_calls"], 28)
        self.assertFalse(report["review_approved"])
        self.assertFalse(report["independent_heldout"])
        self.assertFalse(report["independence_verified"])

    def test_verified_weights_are_not_enough_to_reuse_a_kev_server(self):
        model, binary = self.root / "gemma.gguf", self.root / "llama-server"
        model.write_text("synthetic weight")
        binary.write_text("synthetic binary")
        argv = [str(binary), "-m", str(model), "--alias", "ggml-org/Kev-4B-GGUF:Q8_0",
                "--host", "127.0.0.1", "--port", "18081", "-c", "16384", "-np", "1",
                "--offline", "--no-webui", "--log-disable"]
        with patch.object(local.corpus, "file_hash", return_value=self.cfg["redaction_sha256"]), \
                patch.object(local.subprocess, "run", side_effect=[
                    SimpleNamespace(stdout="version 11669 (620d2225)", stderr=""),
                    SimpleNamespace(stdout=shlex.join(argv))]), self.assertRaises(ValueError):
            local.verify_server(self.cfg, model, binary, 1234, "http://127.0.0.1:18081/v1")

    def test_local_api_key_file_permissions_are_checked_without_exposing_value(self):
        key = self.root / "synthetic-api-key"
        key.write_text("synthetic-only")
        key.chmod(0o600)
        self.assertEqual(local.privacy.private_headers(key), {"Authorization": "Bearer synthetic-only"})
        key.chmod(0o644)
        with self.assertRaises(ValueError):
            local.privacy.private_headers(key)


if __name__ == "__main__":
    unittest.main()
