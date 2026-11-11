"""Offline isolation, multilingual evidence, freshness and durable retry tests."""

import json
import io
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pipeline/src"))
from fminer import db, questions, questions_corpus as corpus
from test_questions import answers, raw


def response(build):
    build(6000)
    return {"answers": answers()}


class CorpusTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.source = Path(self.temp.name) / "source.db"
        self.results = Path(self.temp.name) / "ratings.db"
        with db.connect(self.source) as con:
            con.executescript(questions.SCHEMA)
            for n, lang in enumerate(("en", "fr", "de", "ja", "es", "it"), 1):
                con.execute(
                    """INSERT INTO topics(id,title,title_en,lang,first_post_en,thread_raw,posts_count,
                       enriched_at,raw_changed_at,has_accepted_answer,closed)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (n, "Original title", "English title", lang, "Can this setting be changed?",
                     raw(["Question?", "No, not in v19."]), 2, "2026-01-02", "2026-01-01", 0, 1))
            con.execute("INSERT INTO questions_pilot(topic_id,stratum) VALUES (1,'other')")
        self.before = corpus.file_hash(self.source)
        self.con = corpus.connect(self.source, self.results)

    def tearDown(self):
        self.con.close()
        self.temp.cleanup()

    def prepare(self, **kwargs):
        return corpus.prepare(self.con, self.source, **kwargs)

    def classify(self, **kwargs):
        return corpus.classify(self.con, "http://unused", "", "weights-sha256/runtime-v1",
                               log=lambda _: None, **kwargs)

    def test_source_is_read_only_results_are_isolated_and_all_languages_are_members(self):
        report = self.prepare()
        self.assertEqual(report["topics"], 6)
        self.assertEqual(report["ready"], dict.fromkeys(("en", "fr", "de", "ja", "es", "it"), 1))
        with self.assertRaises(sqlite3.OperationalError):
            self.con.execute("UPDATE source.topics SET title='bad'")
        with sqlite3.connect(self.source) as source:
            self.assertEqual(source.execute("SELECT COUNT(*) FROM questions_pilot").fetchone()[0], 1)
            self.assertEqual(source.execute("SELECT COUNT(*) FROM classifications").fetchone()[0], 0)
        self.assertEqual(self.before, corpus.file_hash(self.source))
        with sqlite3.connect(self.results) as results:
            names = {r[0] for r in results.execute("SELECT name FROM sqlite_master")}
        self.assertNotIn("topics", names)
        self.assertNotIn("questions_pilot", names)
        self.assertNotIn("classifications", names)

    def test_invalid_source_or_scope_is_explicit(self):
        with self.assertRaises(ValueError):
            corpus.connect(self.source, self.source)
        with self.assertRaises(ValueError):
            corpus.connect(Path(self.temp.name) / "missing.db", self.results)
        with self.assertRaises(ValueError):
            self.prepare(scope="english")
        with self.assertRaisesRegex(ValueError, "prepare"):
            self.classify()

    def test_working_db_and_hardlink_results_are_rejected_without_modification(self):
        import os
        linked = Path(self.temp.name) / "linked.db"
        os.link(self.source, linked)
        with self.assertRaises(ValueError):
            corpus.connect(self.source, linked)
        other_source = Path(self.temp.name) / "other-source.db"
        with sqlite3.connect(self.source) as source, sqlite3.connect(other_source) as backup:
            source.backup(backup)
        with self.assertRaisesRegex(ValueError, "isolated"):
            corpus.connect(other_source, self.source)
        self.assertEqual(self.before, corpus.file_hash(self.source))

    def test_corpus_clarifications_preserve_pilot_rubric_and_exact_values(self):
        self.assertEqual(set(corpus.QUESTIONS), set(questions.QUESTIONS))
        for key, spec in questions.QUESTIONS.items():
            self.assertEqual(corpus.QUESTIONS[key]["type"], spec["type"])
            if "criteria" in spec:
                self.assertEqual(corpus.QUESTIONS[key]["criteria"], spec["criteria"])
        self.assertNotIn("social/feature teaser", questions.QUESTIONS["has_question"]["instructions"])
        self.assertNotIn("Repeated application/client termination", questions.QUESTIONS["impact"]["instructions"])

    def test_published_gap_and_bad_manifest_are_not_silently_accepted(self):
        manifest = {"stats": {"topics": 6}, "generated_at": "2026-11-03"}
        report = self.prepare(published_manifest=manifest, published_ids=[1, 2, 3, 4, 5, 99])
        comparison = report["published_comparison"]
        self.assertEqual(comparison["missing_source_ids"], [99])
        self.assertEqual(comparison["source_only_ids"], [6])
        with self.assertRaisesRegex(ValueError, "count"):
            self.prepare(published_manifest=manifest, published_ids=[1])
        with self.assertRaisesRegex(ValueError, "both"):
            self.prepare(published_manifest=manifest)

    def test_retries_resume_and_model_prompt_evidence_and_input_changes_invalidate(self):
        self.prepare()
        with patch.object(corpus, "CorpusDecider") as constructor:
            decider = constructor.return_value
            decider.model_id.return_value = "Kev"
            decider.ask_fitting.side_effect = response
            self.classify()
            self.classify()
            self.assertEqual(decider.ask_fitting.call_count, 6)
            self.con.execute("UPDATE questions_decisions SET model_revision='old' WHERE topic_id=1")
            self.classify()
            self.con.execute("UPDATE questions_decisions SET decision_version='old' WHERE topic_id=2")
            self.classify()
            self.con.execute("UPDATE questions_decisions SET evidence_version='old' WHERE topic_id=3")
            self.classify()
            self.assertEqual(decider.ask_fitting.call_count, 9)
            with sqlite3.connect(self.source) as source:
                source.execute("UPDATE topics SET has_accepted_answer=1 WHERE id=4")
            with self.assertRaisesRegex(RuntimeError, "snapshot changed"):
                self.classify()
            self.prepare()
            self.classify()
            self.assertEqual(decider.ask_fitting.call_count, 10)
            self.assertEqual(corpus.progress(self.con)["counts"], {"current": 6})

    def test_failures_are_private_persisted_and_retriable(self):
        self.prepare()
        with patch.object(corpus, "CorpusDecider") as constructor:
            decider = constructor.return_value
            decider.model_id.return_value = "Kev"
            decider.ask_fitting.side_effect = RuntimeError("Private original text AlicePrivate")
            with self.assertRaisesRegex(RuntimeError, "persisted"):
                self.classify(limit=1)
            self.assertEqual(corpus.progress(self.con)["counts"]["failed"], 1)
            stored = json.dumps([dict(r) for r in self.con.execute("SELECT * FROM questions_attempts")])
            self.assertNotIn("AlicePrivate", stored)
            self.assertNotIn("Private original text", stored)
            decider.ask_fitting.side_effect = response
            self.classify(limit=1)
            self.assertEqual([r[0] for r in self.con.execute(
                "SELECT outcome FROM questions_attempts ORDER BY id")], ["failed", "completed"])
            self.assertEqual(corpus.progress(self.con)["counts"]["current"], 1)

    def test_missing_raw_blocks_but_missing_english_uses_originals(self):
        with sqlite3.connect(self.source) as source:
            source.execute("UPDATE topics SET first_post_en=NULL,enriched_at=NULL WHERE id=2")
            source.execute("UPDATE topics SET thread_raw=NULL WHERE id=3")
            source.execute("UPDATE topics SET posts_count=NULL WHERE id=4")
        report = self.prepare()
        self.assertEqual(report["missing_evidence_ids"], [3])
        self.assertEqual(report["ready"]["fr"], 1)
        row = self.con.execute("SELECT * FROM source.topics WHERE id=2").fetchone()
        text, _ = corpus.evidence(row, 2100)
        self.assertIn("original language", text)
        self.assertIn("Question?", text)
        self.assertNotIn("English title", text)
        row = self.con.execute("SELECT * FROM source.topics WHERE id=4").fetchone()
        self.assertIn("replies: unknown", corpus.evidence(row, 2100)[0])
        self.assertIsNone(self.con.execute("SELECT replies FROM questions_decisions WHERE topic_id=4").fetchone()[0])

    def test_stale_enrichment_never_replaces_original_evidence(self):
        with sqlite3.connect(self.source) as source:
            source.execute(
                "UPDATE topics SET raw_changed_at='2026-01-03',summary_paragraph='An old summary' WHERE id=1")
        row = self.con.execute("SELECT * FROM source.topics WHERE id=1").fetchone()
        text, _ = corpus.evidence(row, 6000)
        self.assertIn("Question?", text)
        self.assertNotIn("Can this setting be changed?", text)
        self.assertNotIn("An old summary", text)

    def test_existing_summary_is_secondary_context_not_acceptance_evidence(self):
        with sqlite3.connect(self.source) as source:
            source.execute("UPDATE topics SET summary_paragraph=? WHERE id=1",
                           ("A useful English summary. " * 100 + "The main solution was accepted.",))
        row = self.con.execute("SELECT * FROM source.topics WHERE id=1").fetchone()
        text, cited = corpus.evidence(row, 3600)
        self.assertIn("NOT acceptance evidence", text)
        self.assertIn("Numbered original posts control", text)
        self.assertIn("The main solution was accepted.", text)
        self.assertEqual(cited, {1, 2})
        self.assertLessEqual(len(text), 3600)
        self.assertNotIn("A useful English summary", corpus.evidence(row, 2100)[0])

    def test_core_posts_head_tail_and_anonymity_at_all_budgets(self):
        texts = ["Question?"] + ["Technical detail. " * 200] * 28 + [
            "More context. " * 200 + "Now everything is working as expected."]
        source_raw = raw(texts)
        for n in (8, 16, 24, 30):
            source_raw = source_raw.replace(
                f"BobPrivate | 2026-01-01 12:00:00 UTC | #{n}",
                f"AlicePrivate | 2026-01-01 12:00:00 UTC | #{n}")
        with sqlite3.connect(self.source) as source:
            source.execute("UPDATE topics SET thread_raw=?,posts_count=30 WHERE id=1", (source_raw,))
        row = self.con.execute("SELECT * FROM source.topics WHERE id=1").fetchone()
        previous = set()
        for budget in (2100, 3600, 6000):
            text, cited = corpus.evidence(row, budget)
            self.assertLessEqual(len(text), budget)
            self.assertTrue({1, 2, 16, 24, 30} <= cited)
            self.assertTrue(previous <= cited)
            self.assertIn("Now everything is working as expected.", text)
            self.assertNotIn("AlicePrivate", text)
            self.assertNotIn("BobPrivate", text)
            self.assertIn("code-dependent", text)
            previous = cited

    def test_six_post_thread_keeps_all_three_poster_followups_at_every_budget(self):
        texts = ["Question?", "Early answer.", "Detail."] + [
            "Technical context. " * 100 + "This still does not work."] * 3
        source_raw = raw(texts)
        for n in (3, 4, 5):
            source_raw = source_raw.replace(
                f"BobPrivate | 2026-01-01 12:00:00 UTC | #{n}",
                f"AlicePrivate | 2026-01-01 12:00:00 UTC | #{n}")
        with sqlite3.connect(self.source) as source:
            source.execute("UPDATE topics SET thread_raw=?,posts_count=6 WHERE id=1", (source_raw,))
        row = self.con.execute("SELECT * FROM source.topics WHERE id=1").fetchone()
        for budget in (2100, 3600, 6000):
            text, cited = corpus.evidence(row, budget)
            self.assertEqual(cited, {1, 2, 3, 4, 5, 6})
            self.assertLessEqual(len(text), budget)
            self.assertIn("This still does not work.", text)

    def test_bounded_selection_does_not_change_membership_or_run_writer(self):
        self.prepare()
        with patch.object(corpus, "CorpusDecider") as constructor, patch.object(questions, "LLM") as writer:
            decider = constructor.return_value
            decider.model_id.return_value = "Kev"
            decider.ask_fitting.side_effect = response
            self.classify(topic_ids=[2, 6], limit=1)
            self.assertEqual(decider.ask_fitting.call_count, 1)
            writer.assert_not_called()
            self.assertEqual(self.con.execute("SELECT COUNT(*) FROM questions_decisions").fetchone()[0], 6)
            with self.assertRaisesRegex(ValueError, "not in the source"):
                self.classify(topic_ids=[999])
            with self.assertRaises(ValueError):
                self.classify(budget_minutes=0)

    def test_budget_stops_starting_work_and_restarts_same_membership(self):
        self.prepare()
        with patch.object(corpus, "CorpusDecider") as constructor, patch.object(
                corpus.time, "monotonic", side_effect=[0, 0, 0, 1, 1, 61, 61]):
            decider = constructor.return_value
            decider.model_id.return_value = "Kev"
            decider.ask_fitting.side_effect = response
            run = self.classify(budget_minutes=1)
            self.assertEqual(run["completed"], 1)
            self.assertEqual(decider.ask_fitting.call_count, 1)
        self.assertEqual(corpus.progress(self.con)["counts"], {"current": 1, "pending": 5})

    def test_interrupted_attempt_is_preserved_and_retried(self):
        self.prepare()
        row = self.con.execute("SELECT * FROM questions_decisions WHERE topic_id=1").fetchone()
        self.con.execute(
            """INSERT INTO questions_attempts(topic_id,input_hash,decision_model,model_revision,decision_version,
               evidence_version,started_at,outcome) VALUES (?,?,?,?,?,?,?,'running')""",
            (1, row["input_hash"], "Kev", "weights-sha256/runtime-v1", corpus.PROMPT_VERSION,
             corpus.EVIDENCE_VERSION, "2026-01-01"))
        self.con.commit()
        with patch.object(corpus, "CorpusDecider") as constructor:
            decider = constructor.return_value
            decider.model_id.return_value = "Kev"
            decider.ask_fitting.side_effect = response
            self.classify(topic_ids=[1])
        self.assertEqual([r[0] for r in self.con.execute(
            "SELECT outcome FROM questions_attempts ORDER BY id")], ["interrupted", "completed"])

    def test_cli_never_opens_default_db_and_uses_exclusive_writer_lock(self):
        import fcntl
        from fminer import main
        self.prepare()
        argv = ["fminer", "questions-corpus", "stats", "--source", str(self.source), "--results", str(self.results)]
        with patch.object(sys, "argv", argv), patch.object(db, "connect", side_effect=AssertionError(
                "shared DB must not be opened")), redirect_stdout(io.StringIO()):
            main()
        lock_path = Path(str(self.results) + ".lock")
        with lock_path.open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with redirect_stdout(io.StringIO()), patch.object(sys, "stderr", io.StringIO()), self.assertRaises(
                    SystemExit):
                corpus.cli(["prepare", "--source", str(self.source), "--results", str(self.results),
                            "--scope", "all"])


if __name__ == "__main__":
    unittest.main()
