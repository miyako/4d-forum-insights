"""Offline verification of private Questions checkpoint compatibility."""

import json
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pipeline/src"))
from fminer import db, questions, questions_corpus as corpus, questions_preflight as preflight
from test_questions import answers, raw


class PreflightTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.source = Path(self.temp.name) / "source.db"
        self.ratings = Path(self.temp.name) / "ratings.db"
        with closing(db.connect(self.source)) as source, source:
            for topic_id in (1, 2):
                source.execute(
                    """INSERT INTO topics(id,title,title_en,lang,first_post_en,thread_raw,posts_count,
                       enriched_at,raw_changed_at,has_accepted_answer,closed)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (topic_id, "Title", "Title", "en", "Question?", raw(["Question?", "Reply."]),
                     2, "2026-01-02", "2026-01-01", 0, 0))
        con = corpus.connect(self.source, self.ratings)
        try:
            corpus.prepare(con, self.source)
            with patch.object(corpus, "CorpusDecider") as factory:
                factory.return_value.model_id.return_value = "Kev"
                factory.return_value.ask_fitting.side_effect = lambda build: (build(6000), {"answers": answers()})[1]
                corpus.classify(con, "http://unused", "", "pinned-revision", limit=1, log=lambda _: None)
        finally:
            con.close()
        for name in ("source", "ratings"):
            original = getattr(self, name)
            frozen = original.with_name(original.stem + "-snapshot.db")
            with closing(sqlite3.connect(original)) as src, closing(sqlite3.connect(frozen)) as dst:
                src.backup(dst)
                dst.execute("PRAGMA journal_mode=DELETE")
            setattr(self, name, frozen)

    def tearDown(self):
        self.temp.cleanup()

    def verify(self, **kwargs):
        return preflight.verify(kwargs.get("source", self.source), kwargs.get("ratings", self.ratings),
                                kwargs.get("model", "Kev"), kwargs.get("revision", "pinned-revision"))

    def test_compatible_seed_is_read_only_and_preserves_partial_progress(self):
        before = (corpus.file_hash(self.source), corpus.file_hash(self.ratings))
        result = self.verify()
        self.assertEqual((result["topics"], result["current"], result["pending"], result["blocked"]), (2, 1, 1, 0))
        self.assertEqual(result["prompt"], "questions-corpus-p1-68290b6c")
        self.assertEqual(before, (corpus.file_hash(self.source), corpus.file_hash(self.ratings)))

    def test_runtime_or_source_change_cannot_silently_invalidate_seed(self):
        with self.assertRaisesRegex(ValueError, "runtime"):
            self.verify(revision="different-runtime")
        with closing(sqlite3.connect(self.source)) as source, source:
            source.execute("UPDATE topics SET title='Changed' WHERE id=1")
        with self.assertRaisesRegex(ValueError, "Source content differs"):
            self.verify()

    def test_stale_answers_and_unfinished_attempts_fail_closed(self):
        with closing(sqlite3.connect(self.ratings)) as ratings, ratings:
            ratings.execute("UPDATE questions_decisions SET decision_version='old' WHERE topic_id=1")
        with self.assertRaisesRegex(ValueError, "not current"):
            self.verify()
        with closing(sqlite3.connect(self.ratings)) as ratings, ratings:
            ratings.execute("UPDATE questions_decisions SET decision_version=? WHERE topic_id=1",
                            (corpus.PROMPT_VERSION,))
            ratings.execute("UPDATE questions_attempts SET outcome='running' WHERE topic_id=1")
        with self.assertRaisesRegex(ValueError, "unfinished attempts"):
            self.verify()

    def test_changed_evidence_retains_stale_decision_without_counting_it_current(self):
        with closing(sqlite3.connect(self.ratings)) as ratings, ratings:
            ratings.execute("UPDATE questions_decisions SET decision_input_hash='old-evidence' WHERE topic_id=1")
        result = self.verify()
        self.assertEqual((result["current"], result["stale"], result["pending"]), (0, 1, 1))

    def test_changed_membership_fingerprint_and_invalid_answers_fail_closed(self):
        with closing(sqlite3.connect(self.ratings)) as ratings, ratings:
            ratings.execute("DELETE FROM questions_decisions WHERE topic_id=2")
        with self.assertRaisesRegex(ValueError, "membership"):
            self.verify()
        self.tearDown()
        self.setUp()
        with closing(sqlite3.connect(self.ratings)) as ratings, ratings:
            ratings.execute("UPDATE questions_decisions SET input_hash='wrong' WHERE topic_id=1")
        with self.assertRaisesRegex(ValueError, "fingerprint"):
            self.verify()
        self.tearDown()
        self.setUp()
        with closing(sqlite3.connect(self.ratings)) as ratings, ratings:
            ratings.execute("UPDATE questions_decisions SET answers=? WHERE topic_id=1", (json.dumps({}),))
        with self.assertRaisesRegex(ValueError, "Invalid decision"):
            self.verify()

    def test_same_database_and_wal_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "separate"):
            self.verify(ratings=self.source)
        Path(str(self.ratings) + "-wal").touch()
        with self.assertRaisesRegex(ValueError, "WAL"):
            self.verify()

    def test_checkpoint_creation_verifies_and_encrypts_without_changing_snapshots(self):
        output = Path(self.temp.name) / "checkpoint.enc"
        cmd = [sys.executable, str(ROOT / "scripts/questions_save_checkpoint.py"),
               "--source", str(self.source), "--ratings", str(self.ratings),
               "--model", "Kev", "--revision", "pinned-revision", "--output", str(output)]
        env = {**os.environ, "DB_KEY": "test-key"}
        before = (corpus.file_hash(self.source), corpus.file_hash(self.ratings))
        result = subprocess.run(cmd, env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["current"], 1)
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)
        self.assertEqual(before, (corpus.file_hash(self.source), corpus.file_hash(self.ratings)))
        self.assertNotEqual(subprocess.run(cmd, env=env, capture_output=True).returncode, 0)
        bad_output = Path(self.temp.name) / "bad.enc"
        bad = cmd[:-3] + ["different-revision", "--output", str(bad_output)]
        self.assertNotEqual(subprocess.run(bad, env=env, capture_output=True).returncode, 0)
        self.assertFalse(bad_output.exists())


if __name__ == "__main__":
    unittest.main()
