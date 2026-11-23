"""Synthetic end-to-end update tests with a fake release and local decision server."""

import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import unittest
from contextlib import closing
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import questions_update as update
import test_questions_checkpoint_store as checkpoint_fixtures
import test_questions_batch as batch_fixtures
from test_questions import answers


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.fixture = checkpoint_fixtures.CheckpointStoreTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.gh = self.fixture.gh
        self.root = self.fixture.directory
        self.executable, self.weights = self.root / "server", self.root / "weights"
        self.executable.write_bytes(b"synthetic server")
        self.weights.write_bytes(b"synthetic weights")
        self.runtime = self.root / "runtime.json"
        self.runtime.write_text(json.dumps({
            "schema_version": "questions-runtime-v1", "model": "Kev", "revision": "pinned-revision",
            "server_sha256": update.corpus.file_hash(self.executable),
            "weights_sha256": update.corpus.file_hash(self.weights),
            "prompt": update.corpus.PROMPT_VERSION, "evidence": update.corpus.EVIDENCE_VERSION,
        }))
        self.pin = update.corpus.file_hash(self.runtime)

    def run_update(self, name, max_stale=0, max_new=0):
        return update.update(self.gh, "data", self.root / name, self.runtime, self.pin,
                             self.executable, self.weights, 1, 1, max_stale, max_new)

    def replace_forum(self, sql):
        changed = self.root / "changed.db"
        shutil.copyfile(self.fixture.fixture.source, changed)
        with closing(sqlite3.connect(changed)) as con, con:
            con.execute(sql)
        compressed = self.root / "changed.zst"
        encrypted = self.root / "changed.enc"
        subprocess.run(["zstd", "-q", str(changed), "-o", str(compressed)], check=True)
        subprocess.run(["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-iter", "200000", "-salt",
                        "-pass", "env:DB_KEY", "-in", str(compressed), "-out", str(encrypted)], check=True)
        self.gh.put(update.store.FORUM, encrypted.read_bytes())

    def test_completed_batch_saves_encrypted_progress_without_plaintext_output(self):
        with patch.object(update.batch, "server", batch_fixtures.fake_server), \
                patch.object(update.corpus, "CorpusDecider") as factory:
            factory.return_value.model_id.return_value = "Kev"
            factory.return_value.ask_fitting.side_effect = lambda build: (build(6000), {"answers": answers()})[1]
            result = self.run_update("success")
        self.assertEqual((result["before"]["current"], result["after"]["current"]), (1, 2))
        self.assertEqual({path.name for path in (self.root / "success").iterdir()},
                         {"candidate.enc", "result.json"})
        update.store.unpack(self.root / "success" / "candidate.enc", self.root / "inspect")
        self.assertEqual(update.preflight.verify(self.root / "inspect/source.db", self.root / "inspect/ratings.db",
                                                 "Kev", "pinned-revision")["current"], 2)
        checkpoint_id = self.gh.files[update.store.CHECKPOINT][0]
        with patch.object(update.batch, "server") as start:
            self.assertTrue(self.run_update("unchanged")["unchanged"])
            start.assert_not_called()
        self.assertEqual(self.gh.files[update.store.CHECKPOINT][0], checkpoint_id)

    def test_source_change_requires_explicit_stale_policy(self):
        self.replace_forum("UPDATE topics SET first_post_en='New question?' WHERE id=1")
        with self.assertRaisesRegex(ValueError, "exceeds the configured"):
            self.run_update("reject")
        self.assertEqual(self.gh.files[update.store.CHECKPOINT][1], self.fixture.old.read_bytes())
        with patch.object(update.batch, "server", batch_fixtures.fake_server), \
                patch.object(update.corpus, "CorpusDecider") as factory:
            factory.return_value.model_id.return_value = "Kev"
            factory.return_value.ask_fitting.side_effect = lambda build: (build(6000), {"answers": answers()})[1]
            result = self.run_update("allow", max_stale=1)
        self.assertEqual(result["after"]["current"], 1)
        self.assertEqual(result["transition"]["invalidated"], 1)

    def test_current_decision_becoming_blocked_counts_as_invalidated(self):
        self.replace_forum("UPDATE topics SET thread_raw=NULL WHERE id=1")
        with self.assertRaisesRegex(ValueError, "exceeds the configured"):
            self.run_update("blocked")
        self.assertEqual(self.gh.files[update.store.CHECKPOINT][1], self.fixture.old.read_bytes())

    def test_new_topics_require_explicit_membership_policy(self):
        self.replace_forum(
            """INSERT INTO topics(id,title,title_en,lang,first_post_en,thread_raw,posts_count,
               enriched_at,raw_changed_at,has_accepted_answer,closed)
               SELECT 3,title,title_en,lang,first_post_en,thread_raw,posts_count,
               enriched_at,raw_changed_at,has_accepted_answer,closed FROM topics WHERE id=1"""
        )
        with self.assertRaisesRegex(ValueError, "exceeds the configured"):
            self.run_update("new-rejected")
        with patch.object(update.batch, "server", batch_fixtures.fake_server), \
                patch.object(update.corpus, "CorpusDecider") as factory:
            factory.return_value.model_id.return_value = "Kev"
            factory.return_value.ask_fitting.side_effect = lambda build: (build(6000), {"answers": answers()})[1]
            result = self.run_update("new-allowed", max_new=1)
        self.assertEqual(result["transition"]["new_topics"], 1)

    def test_stale_release_preserves_encrypted_candidate_for_recovery(self):
        with patch.object(update.batch, "server", batch_fixtures.fake_server), \
                patch.object(update.corpus, "CorpusDecider") as factory:
            factory.return_value.model_id.return_value = "Kev"
            def decide(build):
                build(6000)
                self.gh.put(update.store.FORUM, self.fixture.forum.read_bytes())
                return {"answers": answers()}
            factory.return_value.ask_fitting.side_effect = decide
            with self.assertRaisesRegex(ValueError, "changed since capture"):
                self.run_update("stale")
        self.assertTrue((self.root / "stale/candidate.enc").is_file())
        self.assertEqual({path.name for path in (self.root / "stale").iterdir()}, {"candidate.enc"})
        self.assertEqual(self.gh.files[update.store.CHECKPOINT][1], self.fixture.old.read_bytes())

    def test_failed_decisions_are_checkpointed_and_the_run_fails(self):
        with patch.object(update.batch, "server", batch_fixtures.fake_server), \
                patch.object(update.corpus, "CorpusDecider") as factory:
            factory.return_value.model_id.return_value = "Kev"
            factory.return_value.ask_fitting.side_effect = RuntimeError("private content")
            with self.assertRaisesRegex(RuntimeError, "checkpointed"):
                self.run_update("failed")
        self.assertTrue(json.loads((self.root / "failed/result.json").read_text())["failed_attempts"])
        self.assertNotEqual(self.gh.files[update.store.CHECKPOINT][1], self.fixture.old.read_bytes())

    def test_output_must_be_private_and_survive_runner_temp_cleanup(self):
        public = self.root / "public"
        public.mkdir(mode=0o755)
        public.chmod(0o755)
        with self.assertRaisesRegex(ValueError, "owner-only"):
            self.run_update("public/run")
        with patch.dict("os.environ", {"RUNNER_TEMP": str(self.root)}):
            with self.assertRaisesRegex(ValueError, "automatically cleaned"):
                self.run_update("inside-runner-temp")


if __name__ == "__main__":
    unittest.main()
