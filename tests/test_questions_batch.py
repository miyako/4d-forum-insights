"""Synthetic private batch tests; no model server or external service is contacted."""

from contextlib import contextmanager, closing
import json
from pathlib import Path
import sqlite3
import sys
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import questions_run_batch as batch
import test_questions_preflight as fixtures
from test_questions import answers


@contextmanager
def fake_server(*args, **kwargs):
    yield "http://synthetic"


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.PreflightTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.root = Path(self.fixture.temp.name)
        self.executable, self.weights = self.root / "server", self.root / "weights"
        self.executable.write_bytes(b"synthetic executable")
        self.weights.write_bytes(b"synthetic weights")
        self.manifest = self.root / "runtime.json"
        self.manifest.write_text(json.dumps({
            "schema_version": "questions-runtime-v1", "model": "Kev", "revision": "pinned-revision",
            "server_sha256": batch.corpus.file_hash(self.executable),
            "weights_sha256": batch.corpus.file_hash(self.weights),
            "prompt": batch.corpus.PROMPT_VERSION, "evidence": batch.corpus.EVIDENCE_VERSION,
        }))
        self.pin = batch.corpus.file_hash(self.manifest)

    def run_batch(self, output, source=None, ratings=None):
        return batch.run(source or self.fixture.source, ratings or self.fixture.ratings, self.manifest, self.pin,
                         self.executable, self.weights, output, 1, 1)

    def test_batch_preserves_history_and_resumes_without_rerating(self):
        with patch.object(batch, "server", fake_server), patch.object(batch.corpus, "CorpusDecider") as factory:
            decider = factory.return_value
            decider.model_id.return_value = "Kev"
            decider.ask_fitting.side_effect = lambda build: (build(6000), {"answers": answers()})[1]
            output = self.root / "batch"
            report = self.run_batch(output)
            self.assertEqual((report["before"]["current"], report["after"]["current"]), (1, 2))
            self.run_batch(self.root / "rerun", output / "source.db", output / "ratings.db")
            self.assertEqual(decider.ask_fitting.call_count, 1)
            with closing(sqlite3.connect(output / "ratings.db")) as con:
                self.assertEqual(con.execute("SELECT COUNT(*) FROM questions_attempts").fetchone()[0], 2)

    def test_bad_runtime_pin_or_model_identity_never_calls_inference(self):
        self.weights.write_bytes(b"wrong weights")
        with patch.object(batch, "server") as start:
            with self.assertRaisesRegex(ValueError, "weights_sha256 mismatch"):
                self.run_batch(self.root / "bad")
            start.assert_not_called()
        self.weights.write_bytes(b"synthetic weights")
        with patch.object(batch, "server", fake_server), patch.object(batch.corpus, "CorpusDecider") as factory:
            factory.return_value.model_id.return_value = "different-model"
            output = self.root / "wrong-model"
            with self.assertRaisesRegex(RuntimeError, "private checkpoint remains"):
                self.run_batch(output)
            factory.return_value.ask_fitting.assert_not_called()
            self.assertEqual(batch.preflight.verify(output / "source.db", output / "ratings.db",
                                                    "Kev", "pinned-revision")["current"], 1)

    def test_failed_decision_is_saved_for_retry_without_private_error_text(self):
        with patch.object(batch, "server", fake_server), patch.object(batch.corpus, "CorpusDecider") as factory:
            factory.return_value.model_id.return_value = "Kev"
            factory.return_value.ask_fitting.side_effect = RuntimeError("private source text")
            output = self.root / "failed"
            with self.assertRaisesRegex(RuntimeError, "private checkpoint remains"):
                self.run_batch(output)
            state = batch.preflight.verify(output / "source.db", output / "ratings.db", "Kev", "pinned-revision")
            self.assertEqual(state["blocked"], 1)
            with closing(sqlite3.connect(output / "ratings.db")) as con:
                error = con.execute("SELECT error FROM questions_attempts ORDER BY id DESC").fetchone()[0]
            self.assertNotIn("private source text", error)

    def test_batch_runs_against_a_local_synthetic_server(self):
        script = """#!/usr/bin/env python3
import http.server, json, sys
port = int(sys.argv[sys.argv.index('--port') + 1])
model = sys.argv[sys.argv.index('--alias') + 1]
answers = json.loads(%r)
class Handler(http.server.BaseHTTPRequestHandler):
    def reply(self, body):
        encoded = json.dumps(body).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)
    def do_GET(self):
        self.reply({'status': 'ok'} if self.path == '/health' else {'data': [{'id': model}]})
    def do_POST(self):
        self.rfile.read(int(self.headers['Content-Length']))
        self.reply({'answers': answers})
    def log_message(self, *args):
        pass
http.server.HTTPServer(('127.0.0.1', port), Handler).serve_forever()
""" % json.dumps(answers())
        self.executable.write_text(script)
        self.executable.chmod(0o700)
        manifest = json.loads(self.manifest.read_text())
        manifest["server_sha256"] = batch.corpus.file_hash(self.executable)
        self.manifest.write_text(json.dumps(manifest))
        self.pin = batch.corpus.file_hash(self.manifest)
        result = self.run_batch(self.root / "http-batch")
        self.assertEqual(result["after"]["current"], 2)

    def test_deadline_interrupts_a_slow_decision_and_preserves_attempt(self):
        with patch.object(batch, "server", fake_server), patch.object(batch.corpus, "CorpusDecider") as factory:
            factory.return_value.model_id.return_value = "Kev"
            factory.return_value.ask_fitting.side_effect = lambda _: time.sleep(2)
            output = self.root / "deadline"
            started = time.monotonic()
            with self.assertRaisesRegex(RuntimeError, "private checkpoint remains"):
                batch.run(self.fixture.source, self.fixture.ratings, self.manifest, self.pin,
                          self.executable, self.weights, output, 1, 0.001)
            self.assertLess(time.monotonic() - started, 1)
            with closing(sqlite3.connect(output / "ratings.db")) as con:
                self.assertEqual(con.execute("SELECT outcome FROM questions_attempts ORDER BY id DESC").fetchone()[0],
                                 "failed")


if __name__ == "__main__":
    unittest.main()
