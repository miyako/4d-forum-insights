"""Synthetic, offline Questions export and privacy-review fixtures."""

import hashlib
import json
from contextlib import closing, contextmanager
from pathlib import Path
import sqlite3
import subprocess
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
