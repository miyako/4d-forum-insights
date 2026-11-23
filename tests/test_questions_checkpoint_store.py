"""Synthetic tests for the private encrypted checkpoint release protocol."""

from contextlib import contextmanager, closing
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import questions_checkpoint_store as store
import questions_transition
import test_questions_preflight as fixtures


class FakeGitHub:
    repo = "example/private-data"

    def __init__(self):
        self.files = {}
        self.next_id = 1
        self.fail_checkpoint_upload = False
        self.corrupt_backup = False

    @contextmanager
    def lease(self):
        yield

    def api(self, endpoint):
        return {"assets": [{"name": name, "id": item[0]} for name, item in self.files.items()]}

    def put(self, name, data):
        self.files[name] = (self.next_id, data)
        self.next_id += 1

    def release(self, *args):
        if args[0] == "download":
            name, directory = args[args.index("--pattern") + 1], Path(args[args.index("--dir") + 1])
            (directory / name).write_bytes(self.files[name][1])
        elif args[0] == "upload":
            path = Path(args[2])
            if path.name == store.CHECKPOINT and self.fail_checkpoint_upload:
                self.files.pop(path.name, None)
                raise RuntimeError("interrupted upload")
            data = path.read_bytes()
            if path.name.startswith("questions-checkpoint-backup-") and self.corrupt_backup:
                data += b"corrupted"
            self.put(path.name, data)
        else:
            raise AssertionError(args)


class CheckpointStoreTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.PreflightTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.directory = Path(self.fixture.temp.name)
        self.env = patch.dict(os.environ, {"DB_KEY": "synthetic-key"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.gh = FakeGitHub()
        self.forum = self.directory / "forum.enc"
        compressed = self.directory / "forum.zst"
        subprocess.run(["zstd", "-q", str(self.fixture.source), "-o", str(compressed)], check=True)
        subprocess.run(["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-iter", "200000", "-salt",
                        "-pass", "env:DB_KEY", "-in", str(compressed), "-out", str(self.forum)], check=True)
        self.gh.put(store.FORUM, self.forum.read_bytes())
        self.old = self.pack("old.enc")
        self.gh.put(store.CHECKPOINT, self.old.read_bytes())

    def pack(self, name):
        result = self.directory / name
        subprocess.run(["node", str(ROOT / "scripts/questions_checkpoint.mjs"), "pack",
                        "--source", str(self.fixture.source), "--ratings", str(self.fixture.ratings),
                        "--output", str(result)], check=True, capture_output=True)
        return result

    def capture(self):
        directory = self.directory / "capture"
        store.capture(self.gh, "data", directory)
        return directory / "base.json"

    def commit(self, base, candidate):
        return store.commit(self.gh, "data", base, candidate, "Kev", "pinned-revision")

    def test_round_trip_preserves_verified_backup_and_rejects_reuse(self):
        base = self.capture()
        candidate = self.pack("new.enc")
        result = self.commit(base, candidate)
        self.assertEqual(self.gh.files[store.CHECKPOINT][1], candidate.read_bytes())
        backup = f"questions-checkpoint-backup-{store.corpus.file_hash(self.old)}.enc"
        self.assertEqual(self.gh.files[backup][1], self.old.read_bytes())
        self.assertEqual(result["current"], 1)
        with self.assertRaisesRegex(ValueError, "changed since capture"):
            self.commit(base, candidate)

    def test_changed_forum_and_wrong_key_reject_before_upload(self):
        base = self.capture()
        candidate = self.pack("new.enc")
        self.gh.put(store.FORUM, self.forum.read_bytes())
        with self.assertRaisesRegex(ValueError, "changed since capture"):
            self.commit(base, candidate)
        self.assertEqual(self.gh.files[store.CHECKPOINT][1], self.old.read_bytes())

        base.unlink()
        shutil.rmtree(base.parent)
        base = self.capture()
        with patch.dict(os.environ, {"DB_KEY": "wrong"}):
            with self.assertRaisesRegex(ValueError, "authentication"):
                self.commit(base, candidate)
        self.assertEqual(self.gh.files[store.CHECKPOINT][1], self.old.read_bytes())

    def test_candidate_must_match_captured_forum_content(self):
        source = self.directory / "updated.db"
        shutil.copyfile(self.fixture.source, source)
        with closing(sqlite3.connect(source)) as con, con:
            con.execute("UPDATE topics SET first_post_en='Changed question?' WHERE id=1")
        transition = self.directory / "transition"
        questions_transition.transition(self.fixture.source, self.fixture.ratings, source,
                                        store.corpus.file_hash(source), transition, "Kev", "pinned-revision")
        candidate = self.directory / "different-source.enc"
        subprocess.run(["node", str(ROOT / "scripts/questions_checkpoint.mjs"), "pack",
                        "--source", str(transition / "source.db"), "--ratings", str(transition / "ratings.db"),
                        "--output", str(candidate)], check=True, capture_output=True)
        with self.assertRaisesRegex(ValueError, "does not match the captured forum"):
            self.commit(self.capture(), candidate)
        self.assertEqual(self.gh.files[store.CHECKPOINT][1], self.old.read_bytes())

    def test_lost_attempts_and_replaced_current_decisions_are_rejected(self):
        base = self.capture()
        with closing(sqlite3.connect(self.fixture.ratings)) as con, con:
            con.execute("DELETE FROM questions_attempts")
        candidate = self.pack("lost.enc")
        with self.assertRaisesRegex(ValueError, "attempt history"):
            self.commit(base, candidate)

        with closing(sqlite3.connect(self.fixture.ratings)) as con, con:
            con.execute("ATTACH DATABASE ? AS old", (str(self.directory / "ratings.db"),))
            con.execute("INSERT INTO questions_attempts SELECT * FROM old.questions_attempts")
            con.execute("UPDATE questions_decisions SET decision_error='changed' WHERE topic_id=1")
        candidate = self.pack("changed.enc")
        with self.assertRaisesRegex(ValueError, "replaced a current"):
            self.commit(base, candidate)
        self.assertEqual(self.gh.files[store.CHECKPOINT][1], self.old.read_bytes())

    def test_corrupt_backup_prevents_replacement_and_interrupted_upload_preserves_backup(self):
        base = self.capture()
        candidate = self.pack("new.enc")
        self.gh.corrupt_backup = True
        with self.assertRaisesRegex(ValueError, "backup does not match"):
            self.commit(base, candidate)
        self.assertEqual(self.gh.files[store.CHECKPOINT][1], self.old.read_bytes())
        backup = f"questions-checkpoint-backup-{store.corpus.file_hash(self.old)}.enc"
        del self.gh.files[backup]
        self.gh.corrupt_backup = False
        self.gh.fail_checkpoint_upload = True
        with self.assertRaisesRegex(RuntimeError, "interrupted"):
            self.commit(base, candidate)
        self.assertEqual(self.gh.files[backup][1], self.old.read_bytes())
        self.assertNotIn(store.CHECKPOINT, self.gh.files)
        with self.assertRaisesRegex(ValueError, "changed since capture"):
            self.commit(base, candidate)
        with self.assertRaisesRegex(ValueError, "recover the previous checkpoint"):
            store.capture(self.gh, "data", self.directory / "reseed", initial=True)
        self.gh.fail_checkpoint_upload = False
        store.recover(self.gh, "data", store.corpus.file_hash(self.old), "Kev", "pinned-revision", self.directory)
        self.assertEqual(self.gh.files[store.CHECKPOINT][1], self.old.read_bytes())
        with self.assertRaisesRegex(ValueError, "will not replace"):
            store.recover(self.gh, "data", store.corpus.file_hash(self.old), "Kev", "pinned-revision", self.directory)

    def test_initial_seed_requires_explicit_absence(self):
        with self.assertRaisesRegex(ValueError, "Initial seeding"):
            store.capture(self.gh, "data", self.directory / "bad", initial=True)
        del self.gh.files[store.CHECKPOINT]
        directory = self.directory / "initial"
        store.capture(self.gh, "data", directory, initial=True)
        with self.assertRaisesRegex(ValueError, "approved checkpoint SHA-256"):
            self.commit(directory / "base.json", self.old)
        store.commit(self.gh, "data", directory / "base.json", self.old, "Kev", "pinned-revision",
                     store.corpus.file_hash(self.old))
        self.assertEqual(self.gh.files[store.CHECKPOINT][1], self.old.read_bytes())

    def test_explicit_transition_keeps_old_checkpoint_and_marks_changed_evidence_stale(self):
        source = self.directory / "updated.db"
        shutil.copyfile(self.fixture.source, source)
        with closing(sqlite3.connect(source)) as con, con:
            con.execute("UPDATE topics SET first_post_en='A different question?' WHERE id=1")
        before = store.corpus.file_hash(self.fixture.ratings)
        with self.assertRaisesRegex(ValueError, "approved SHA-256"):
            questions_transition.transition(self.fixture.source, self.fixture.ratings, source, "not-approved",
                                            self.directory / "bad-transition", "Kev", "pinned-revision")
        output = self.directory / "transition"
        result = questions_transition.transition(self.fixture.source, self.fixture.ratings, source,
                                                  store.corpus.file_hash(source), output,
                                                  "Kev", "pinned-revision")
        self.assertEqual((result["prepared"]["current"], result["prepared"]["stale"]), (0, 1))
        self.assertEqual(store.corpus.file_hash(output / "previous" / "ratings.db"), before)
        self.assertEqual(store.corpus.file_hash(self.fixture.ratings), before)

    def test_transition_rejects_lost_topic_membership(self):
        source = self.directory / "updated.db"
        shutil.copyfile(self.fixture.source, source)
        with closing(sqlite3.connect(source)) as con, con:
            con.execute("DELETE FROM topics WHERE id=2")
        with self.assertRaisesRegex(ValueError, "lost persisted topic IDs"):
            questions_transition.transition(self.fixture.source, self.fixture.ratings, source,
                                            store.corpus.file_hash(source), self.directory / "transition",
                                            "Kev", "pinned-revision")


if __name__ == "__main__":
    unittest.main()
