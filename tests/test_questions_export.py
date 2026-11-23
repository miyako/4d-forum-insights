"""Synthetic, offline Questions export and privacy-review fixtures."""

import hashlib
import json
import os
from contextlib import closing, contextmanager
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from test_questions import answers, raw, ROOT
from fminer import db, questions_corpus as corpus, questions_export as export


@contextmanager
def connection(path, *, source_schema=False):
    with closing(db.connect(path) if source_schema else sqlite3.connect(path, uri=True)) as con:
        with con:
            yield con


class QuestionsExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "source.db"
        self.ratings = self.root / "ratings.db"
        self.base = self.root / "baseline.json"
        with connection(self.source, source_schema=True) as con:
            for n, lang in ((1, "en"), (2, "fr"), (3, "ja")):
                con.execute(
                    """INSERT INTO topics(id,title,title_en,first_post_en,thread_raw,posts_count,lang,
                       has_accepted_answer,closed,enriched_at,raw_changed_at,summary_paragraph,created_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (n, "Original", "A setting", "How does it work?",
                     raw(["How?", "A reply"]), None if n == 1 else 2, lang,
                     None if n == 1 else 0, 1, "2026-01-02", "2026-01-01",
                     "A participant asks about a setting.", "2026-01-01T00:00:00Z"))
        with connection(self.ratings) as con:
            con.row_factory = sqlite3.Row
            con.executescript(corpus.SCHEMA)
            con.execute("ATTACH DATABASE ? AS source", (self.source.as_uri() + "?mode=ro",))
            corpus.prepare(con, self.source)
            corpus.set_state(con, "runtime", {"model": "synthetic", "revision": "test"})
            con.execute(
                """UPDATE questions_decisions SET answers=?,decision_input_hash=input_hash,
                   decision_model='synthetic',model_revision='test',decision_version=?,evidence_version=?
                   WHERE topic_id IN (1,2)""",
                (json.dumps(answers()), corpus.PROMPT_VERSION, corpus.EVIDENCE_VERSION))
            con.commit()
        self.baseline = {
            "schema_version": "questions-ui-public-processed-preview-v1", "version": 1,
            "public_preview": True, "private_preview": False, "publish_allowed": True,
            "entries": [{"id": 1, "title": "Already redacted title", "body": "Already redacted body"}],
        }
        self.write_baseline()

    def tearDown(self):
        self.temp.cleanup()

    def write_baseline(self):
        self.base.write_text(json.dumps(self.baseline))
        self.base_hash = hashlib.sha256(self.base.read_bytes()).hexdigest()

    def build(self):
        return export.build_candidate(self.source, self.ratings, self.base, self.base_hash)

    def test_partial_preserves_approved_text_metadata_ratings_and_flags(self):
        with connection(self.source) as con:
            con.execute("UPDATE topics SET title_en='AlicePrivate email@example.com' WHERE id=2")
        # A fresh prepared snapshot is required after any source edit.
        with connection(self.ratings) as con:
            con.row_factory = sqlite3.Row
            con.execute("ATTACH DATABASE ? AS source", (self.source.as_uri() + "?mode=ro",))
            corpus.prepare(con, self.source)
            con.execute("UPDATE questions_decisions SET decision_input_hash=input_hash WHERE topic_id=2")
            con.commit()
        before = [hashlib.sha256(p.read_bytes()).hexdigest() for p in (self.source, self.ratings, self.base)]
        candidate, audit = self.build()
        self.assertEqual(candidate["processed_count"], 2)
        self.assertEqual(candidate["corpus_total"], 3)
        self.assertEqual(candidate["eligible_question_count"], 2)
        self.assertFalse(candidate["corpus_complete"])
        self.assertTrue(candidate["private_preview"])
        self.assertFalse(candidate["publish_allowed"])
        one, two = candidate["entries"]
        self.assertEqual((one["title"], one["body"]), ("Already redacted title", "Already redacted body"))
        self.assertIsNone(one["accepted"])
        self.assertIsNone(one["replies"])
        self.assertTrue(one["closed"])
        self.assertEqual(one["answers"], answers())
        self.assertEqual(audit["not_current_ids"], [3])
        self.assertEqual(audit["baseline_retained_ids"], [1])
        self.assertEqual(audit["display_bindings"][0]["title_sha256"],
                         hashlib.sha256(one["title"].encode()).hexdigest())
        self.assertEqual(audit["display_bindings"][0]["display_source"], "approved_baseline")
        self.assertIn({"id": 2, "field": "title", "kind": "email"}, audit["privacy_findings"])
        self.assertIn({"id": 2, "field": "title", "kind": "username"}, audit["privacy_findings"])
        self.assertNotIn("AlicePrivate", json.dumps(audit))
        self.assertTrue(audit["privacy_review_required"])
        self.assertEqual(before, [hashlib.sha256(p.read_bytes()).hexdigest() for p in
                                  (self.source, self.ratings, self.base)])

    def test_missing_display_stale_decisions_and_complete_scope(self):
        with connection(self.source) as con:
            con.execute("UPDATE topics SET summary_paragraph=NULL WHERE id=2")
        with connection(self.ratings) as con:
            con.row_factory = sqlite3.Row
            con.execute("ATTACH DATABASE ? AS source", (self.source.as_uri() + "?mode=ro",))
            corpus.prepare(con, self.source)
            con.execute("UPDATE questions_decisions SET decision_input_hash=input_hash WHERE topic_id=2")
            con.commit()
        candidate, audit = self.build()
        self.assertEqual(candidate["processed_count"], 1)
        self.assertEqual(audit["missing_fresh_display_ids"], [2])
        self.assertFalse(candidate["corpus_complete"])
        with connection(self.ratings) as con:
            con.execute("UPDATE questions_decisions SET decision_version='old' WHERE topic_id=1")
        candidate, audit = self.build()
        self.assertEqual(audit["baseline_not_exported_ids"], [1])
        self.assertEqual(candidate["processed_count"], 0)

    def test_complete_is_private_and_never_automatically_cleared(self):
        with connection(self.ratings) as con:
            con.execute("""UPDATE questions_decisions SET answers=?,decision_input_hash=input_hash,
                        decision_model='synthetic',model_revision='test',decision_version=?,evidence_version=?
                        WHERE topic_id=3""",
                        (json.dumps(answers()), corpus.PROMPT_VERSION, corpus.EVIDENCE_VERSION))
        candidate, audit = self.build()
        self.assertTrue(candidate["corpus_complete"])
        self.assertEqual(candidate["processed_count"], 3)
        self.assertFalse(candidate["publish_allowed"])
        self.assertFalse(audit["publication_authorized"])
        self.assertTrue(audit["privacy_review_required"])

    def test_review_pipeline_stages_private_candidate_without_network_or_publication(self):
        with connection(self.ratings) as con:
            corpus.set_state(con, "runtime", {"model": "synthetic", "revision": "test",
                                            "prompt": corpus.PROMPT_VERSION, "evidence": corpus.EVIDENCE_VERSION})
        build = ROOT / "build"
        build.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="questions-review-test-", dir=build) as temp:
            root = Path(temp)
            for name, original in (("source.db", self.source), ("ratings.db", self.ratings)):
                with closing(sqlite3.connect(original)) as src, closing(sqlite3.connect(root / name)) as dst:
                    src.backup(dst)
                    dst.execute("PRAGMA journal_mode=DELETE")
            data, store = root / "data", root / "store"
            data.mkdir()
            store.mkdir()
            (data / "manifest.json").write_text(json.dumps({"questions": []}))
            output = root / "review"
            cmd = [sys.executable, "scripts/questions_prepare_review.py",
                   "--source", str(root / "source.db"), "--ratings", str(root / "ratings.db"),
                   "--baseline", str(self.base), "--baseline-sha256", self.base_hash,
                   "--data", str(data), "--store", str(store), "--model", "synthetic",
                   "--revision", "test", "--output", str(output)]
            wrong = subprocess.run(cmd[:-3] + ["wrong", "--output", str(output)],
                                   cwd=ROOT, capture_output=True, text=True)
            self.assertNotEqual(wrong.returncode, 0)
            self.assertFalse(output.exists())
            result = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(json.loads(result.stdout)["review_required"])
            audit = json.loads((output / "candidate/audit.json").read_text())
            localization = json.loads((output / "localization-report.json").read_text())
            manifest = json.loads((output / "preview/data/manifest.json").read_text())
            self.assertEqual(output.stat().st_mode & 0o777, 0o700)
            self.assertTrue(audit["privacy_review_required"])
            self.assertFalse(audit["publication_authorized"])
            self.assertTrue(manifest["private_preview"])
            self.assertFalse(manifest["publish_allowed"])
            self.assertFalse(manifest["questions_view"]["enabled"])
            self.assertFalse(manifest["questions_view"]["semantic_enabled"])
            self.assertEqual((localization["requested"], localization["translated"]), (0, 0))
            self.assertEqual(localization["coverage"]["fr/questions"]["missing"], 2)
            again = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
            self.assertNotEqual(again.returncode, 0)
            self.assertIn("already exists", again.stderr)
            broken = root / "broken-data"
            broken.mkdir()
            failed_output = root / "failed"
            failed_cmd = list(cmd)
            failed_cmd[failed_cmd.index("--data") + 1] = str(broken)
            failed_cmd[-1] = str(failed_output)
            failed = subprocess.run(failed_cmd, cwd=ROOT, capture_output=True, text=True)
            self.assertNotEqual(failed.returncode, 0)
            self.assertIn("inspect private log", failed.stderr)
            self.assertEqual((failed_output / "staging-error.log").stat().st_mode & 0o777, 0o600)
            encrypted = root / "checkpoint.enc"
            env = {**os.environ, "DB_KEY": "synthetic-review-key"}
            subprocess.run(["node", "scripts/questions_checkpoint.mjs", "pack",
                            "--source", str(root / "source.db"), "--ratings", str(root / "ratings.db"),
                            "--output", str(encrypted)], cwd=ROOT, env=env, check=True, capture_output=True)
            checkpoint_hash = corpus.file_hash(encrypted)
            checkpoint_output = root / "checkpoint-review"
            checkpoint_cmd = [sys.executable, "scripts/questions_prepare_review.py",
                              "--checkpoint", str(encrypted), "--checkpoint-sha256", checkpoint_hash,
                              "--baseline", str(self.base), "--baseline-sha256", self.base_hash,
                              "--data", str(data), "--store", str(store), "--model", "synthetic",
                              "--revision", "test", "--output", str(checkpoint_output)]
            reviewed = subprocess.run(checkpoint_cmd, cwd=ROOT, env=env, capture_output=True, text=True)
            self.assertEqual(reviewed.returncode, 0, reviewed.stderr)
            self.assertEqual(json.loads(reviewed.stdout)["checkpoint_sha256"], checkpoint_hash)
            checkpoint_audit = json.loads((checkpoint_output / "candidate/audit.json").read_text())
            self.assertFalse(checkpoint_audit["publication_authorized"])
            wrong_checkpoint = list(checkpoint_cmd)
            wrong_checkpoint[wrong_checkpoint.index("--checkpoint-sha256") + 1] = "0" * 64
            wrong_checkpoint[-1] = str(root / "wrong-checkpoint")
            self.assertNotEqual(subprocess.run(wrong_checkpoint, cwd=ROOT, env=env, capture_output=True).returncode, 0)
            self.assertFalse((root / "wrong-checkpoint").exists())

    def test_decision_revision_and_invalid_answers_are_not_exported_as_current(self):
        with connection(self.ratings) as con:
            con.execute("UPDATE questions_decisions SET model_revision='different-engine' WHERE topic_id=2")
        candidate, audit = self.build()
        self.assertEqual([entry["id"] for entry in candidate["entries"]], [1])
        self.assertEqual(audit["not_current_ids"], [2, 3])
        with connection(self.ratings) as con:
            con.execute("UPDATE questions_decisions SET model_revision='test',answers='{}' WHERE topic_id=2")
        with self.assertRaises((KeyError, ValueError)):
            self.build()

    def test_tampered_fingerprint_or_unknown_warning_fails_closed(self):
        with connection(self.ratings) as con:
            con.execute("UPDATE questions_decisions SET input_hash='tampered' WHERE topic_id=2")
        with self.assertRaisesRegex(ValueError, "fingerprint mismatch"):
            self.build()
        with connection(self.ratings) as con:
            con.execute("UPDATE questions_decisions SET input_hash=decision_input_hash, evidence_warnings=? "
                        "WHERE topic_id=2", (json.dumps(["unrecognised warning"]),))
        with self.assertRaisesRegex(ValueError, "unknown evidence warning"):
            self.build()

    def test_rejects_mismatched_hash_source_membership_running_and_wal(self):
        with self.assertRaisesRegex(ValueError, "baseline SHA"):
            export.build_candidate(self.source, self.ratings, self.base, "0" * 64)
        with connection(self.source) as con:
            con.execute("UPDATE topics SET title='changed' WHERE id=3")
        with self.assertRaisesRegex(ValueError, "source snapshot differs"):
            self.build()
        with connection(self.source) as con:
            con.execute("UPDATE topics SET title='Original' WHERE id=3")
        with connection(self.ratings) as con:
            con.execute("""INSERT INTO questions_attempts(topic_id,input_hash,decision_model,model_revision,
                           decision_version,evidence_version,started_at,outcome)
                           VALUES (1,'a','b','c','d','e','now','running')""")
        with self.assertRaisesRegex(ValueError, "running attempts"):
            self.build()
        with connection(self.ratings) as con:
            con.execute("DELETE FROM questions_attempts")
            con.execute("DELETE FROM questions_decisions WHERE topic_id=3")
        with self.assertRaisesRegex(ValueError, "membership differ"):
            self.build()
        Path(str(self.source) + "-wal").write_bytes(b"not a standalone backup")
        with self.assertRaisesRegex(ValueError, "nonempty WAL"):
            self.build()

    def test_cli_writes_new_private_output_only_with_hash_bound_audit(self):
        out = self.root / "candidate"
        command = [str(ROOT / "scripts/questions_export.py"), "--source", str(self.source), "--ratings",
                   str(self.ratings), "--baseline", str(self.base), "--baseline-sha256", self.base_hash,
                   "--output", str(out)]
        import sys
        subprocess.run([sys.executable, *command], check=True, capture_output=True)
        data = (out / "candidate.json").read_bytes()
        audit = json.loads((out / "audit.json").read_text())
        self.assertEqual(audit["candidate_sha256"], hashlib.sha256(data).hexdigest())
        self.assertEqual((out / "candidate.json").stat().st_mode & 0o777, 0o600)
        self.assertEqual(out.stat().st_mode & 0o777, 0o700)
        self.assertNotEqual(subprocess.run([sys.executable, *command], capture_output=True).returncode, 0)
        self.assertEqual(data, (out / "candidate.json").read_bytes())


if __name__ == "__main__":
    unittest.main()
