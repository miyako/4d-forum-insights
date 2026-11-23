import io
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tarfile
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pipeline/src"))
sys.path.insert(0, str(ROOT / "scripts"))
import questions_auto_update as auto


class AutomaticTimeoutTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.work = Path(self.temp.name)
        self.source = self.work / "source.db"
        self.date = "2026-01-03T00:00:00Z"
        with sqlite3.connect(self.source) as db:
            db.execute("""CREATE TABLE topics (
                id INTEGER, raw_changed_at TEXT, last_posted_at TEXT,
                posts_count INTEGER, has_accepted_answer INTEGER, closed INTEGER, fetch_error TEXT,
                thread_raw TEXT
            )""")
            db.executemany("INSERT INTO topics VALUES (?, ?, NULL, 2, 0, 0, NULL, 'thread')",
                           [(n, self.date) for n in (1, 2, 3)])
        self.snapshot = {"source_selected_at": self.date, "entries": []}
        (self.work / "baseline-preview.json").write_text(json.dumps(self.snapshot))
        (self.work / "questions-preview.json").write_text(json.dumps(self.snapshot))
        docs = {
            "data/manifest.json": {"generated_at": self.date, "flags": {"closed": 1, "accepted": 4}},
            "data/topics.json": {
                "fields": ["id", "title", "title_en", "lang", "posts", "flags"],
                "rows": [[n, f"Question {n}", None, "en", 2, 0] for n in (1, 2, 3)],
            },
            "data/text.json": {"para": ["Public summary"] * 3},
            "data/embeddings-ids.json": [1, 2, 3],
        }
        with tarfile.open(self.work / "site-data.tar.gz", "w:gz") as archive:
            for name, doc in docs.items():
                data = json.dumps(doc).encode()
                member = tarfile.TarInfo(name)
                member.size = len(data)
                archive.addfile(member, io.BytesIO(data))

    def tearDown(self):
        self.temp.cleanup()

    def test_timeout_keeps_completed_work_and_failed_topic_pending(self):
        output = io.StringIO()
        translations = {locale: {"entries": {"3": {"fields": {}}}} for locale in auto.weekly.LOCALES}
        with (patch.object(auto.prepare, "kev_answers",
                           side_effect=[({}, {}), auto.prepare.DecisionTimeout("timeout"),
                                        auto.prepare.IncompleteEvidence("missing")]),
              patch.object(auto.prepare, "public_row", side_effect=lambda topic, *_: {"id": topic["id"]}),
              patch.object(auto.prepare, "enrich_display", side_effect=lambda display, *_: display),
              patch.object(auto.prepare, "translate", return_value=translations),
              patch.dict(os.environ, {"OPENAI_MODEL": "test-model"}),
              redirect_stdout(output)):
            update = auto.build_update(self.source, self.snapshot, {}, self.work, lambda: None)
        self.assertEqual([entry["id"] for entry in update["entries"]], [3])
        self.assertIn("Kev timed out for topic 2", output.getvalue())
        self.assertIn('"Kev timeout": 1', output.getvalue())

    def test_all_timed_out_does_not_report_success(self):
        with (patch.object(auto.prepare, "kev_answers", side_effect=auto.prepare.DecisionTimeout("timeout")),
              redirect_stdout(io.StringIO())):
            with self.assertRaisesRegex(RuntimeError, "no Questions updates were prepared"):
                auto.build_update(self.source, self.snapshot, {}, self.work, lambda: None)

    def test_openai_failure_retains_previously_prepared_update(self):
        output = io.StringIO()
        translations = {locale: {"entries": {"3": {"fields": {}}}} for locale in auto.weekly.LOCALES}
        with (patch.object(auto.prepare, "kev_answers", return_value=({}, {})),
              patch.object(auto.prepare, "public_row", side_effect=lambda topic, *_: {"id": topic["id"]}),
              patch.object(auto.prepare, "enrich_display",
                           side_effect=[{"title": "Title", "body": "Body"},
                                        auto.DetectionFailure(1, "timeout"),
                                        auto.DetectionFailure(1, "timeout")]),
              patch.object(auto.prepare, "translate", return_value=translations),
              patch.dict(os.environ, {"OPENAI_MODEL": "test-model"}),
              redirect_stdout(output)):
            update = auto.build_update(self.source, self.snapshot, {}, self.work, lambda: None)
        self.assertEqual([entry["id"] for entry in update["entries"]], [3])
        self.assertIn('"OpenAI failure": 2', output.getvalue())

    def test_all_held_topics_do_not_report_success(self):
        with (patch.object(auto.prepare, "kev_answers", return_value=({}, {})),
              patch.object(auto.prepare, "enrich_display",
                           side_effect=auto.ProposalError("source_code_in_display")),
              patch.dict(os.environ, {"OPENAI_MODEL": "test-model"}),
              redirect_stdout(io.StringIO())):
            with self.assertRaisesRegex(RuntimeError, "no Questions updates were prepared"):
                auto.build_update(self.source, self.snapshot, {}, self.work, lambda: None)

    def test_preview_binding_rejects_changed_display_before_start(self):
        started = []
        with self.assertRaisesRegex(ValueError, "display changed"):
            auto.build_update(self.source, self.snapshot, {}, self.work, lambda: started.append(True),
                              preview_binding=(2, "0" * 64))
        self.assertEqual(started, [])

    def test_preview_binding_only_processes_selected_topic(self):
        display = {"title": "Question 2", "body": "Public summary"}
        digest = hashlib.sha256(json.dumps(display, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        translations = {locale: {"entries": {"2": {"fields": {}}}} for locale in auto.weekly.LOCALES}
        with (patch.object(auto.prepare, "kev_answers", return_value=({}, {})) as kev,
              patch.object(auto.prepare, "public_row", side_effect=lambda topic, *_: {"id": topic["id"]}),
              patch.object(auto.prepare, "enrich_display", side_effect=lambda value, *_: value),
              patch.object(auto.prepare, "translate", return_value=translations),
              patch.dict(os.environ, {"OPENAI_MODEL": "test-model"}),
              redirect_stdout(io.StringIO())):
            update = auto.build_update(self.source, self.snapshot, {}, self.work, lambda: None,
                                       preview_binding=(2, digest))
        self.assertEqual([entry["id"] for entry in update["entries"]], [2])
        kev.assert_called_once_with(self.source, 2)

    def test_read_timeout_is_distinct_from_other_server_errors(self):
        corpus = auto.prepare.questions_corpus
        with (patch.object(corpus, "readiness", return_value=(None, None)),
              patch.object(corpus, "split_raw", return_value=[{"n": 1}, {"n": 2}]),
              patch.object(corpus, "CorpusDecider") as factory):
            client = factory.return_value.http.__enter__.return_value
            model = json.loads((ROOT / "config/questions-weekly.json").read_text())["decision_model"]
            client.get.return_value.json.return_value = {"data": [{"id": model}]}
            factory.return_value.ask_fitting.side_effect = corpus.httpx.ReadTimeout("timeout")
            with self.assertRaises(auto.prepare.DecisionTimeout):
                auto.prepare.kev_answers(self.source, 1)

    def test_batch_moves_past_timed_out_topics_on_later_week(self):
        attempted = []
        with sqlite3.connect(self.source) as db:
            db.executemany("INSERT INTO topics VALUES (?, ?, NULL, 2, 0, 0, NULL, 'thread')",
                           [(n, self.date) for n in (4, 5, 6)])
        with tarfile.open(self.work / "site-data.tar.gz", "w:gz") as archive:
            docs = {
                "data/manifest.json": {"generated_at": self.date, "flags": {"closed": 1, "accepted": 4}},
                "data/topics.json": {
                    "fields": ["id", "title", "title_en", "lang", "posts", "flags"],
                    "rows": [[n, f"Question {n}", None, "en", 2, 0] for n in range(1, 7)],
                },
                "data/text.json": {"para": ["Public summary"] * 6},
                "data/embeddings-ids.json": list(range(1, 7)),
            }
            for name, doc in docs.items():
                data = json.dumps(doc).encode()
                member = tarfile.TarInfo(name)
                member.size = len(data)
                archive.addfile(member, io.BytesIO(data))

        def timeout(source, topic_id):
            attempted.append(topic_id)
            raise auto.prepare.DecisionTimeout("timeout")

        with patch.object(auto.prepare, "kev_answers", side_effect=timeout), redirect_stdout(io.StringIO()):
            for _ in range(2):
                with self.assertRaises(RuntimeError):
                    auto.build_update(self.source, self.snapshot, {}, self.work, lambda: None)
                self.date = "2026-01-10T00:00:00Z"
                docs["data/manifest.json"]["generated_at"] = self.date
                with tarfile.open(self.work / "site-data.tar.gz", "w:gz") as archive:
                    for name, doc in docs.items():
                        data = json.dumps(doc).encode()
                        member = tarfile.TarInfo(name)
                        member.size = len(data)
                        archive.addfile(member, io.BytesIO(data))
        self.assertEqual(len(set(attempted)), 6)


if __name__ == "__main__":
    unittest.main()
