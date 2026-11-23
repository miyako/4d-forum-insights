import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "pipeline/ci/questions-llama.sh"


class ModelDownloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        curl = self.path / "curl"
        curl.write_text("""#!/bin/sh
for last do :; done
echo x >> "$CALLS"
if [ ! -f "$last" ]; then
    printf 'part' > "$last"
    exit 28
fi
printf 'ial' >> "$last"
""")
        curl.chmod(0o755)
        self.calls = self.path / "calls"
        self.model = self.path / "decision.gguf"
        self.env = {**os.environ, "PATH": f"{self.path}:{os.environ['PATH']}", "CALLS": str(self.calls)}

    def tearDown(self):
        self.temp.cleanup()

    def download(self, digest):
        return subprocess.run(
            ["bash", "-c", 'source "$1"; download_questions_model "$2" "$3" "$4"',
             "_", str(HELPER), str(self.model), "https://example.invalid/model", digest],
            env=self.env, capture_output=True, text=True,
        )

    def test_interrupted_download_resumes_and_cached_model_is_verified(self):
        digest = hashlib.sha256(b"partial").hexdigest()
        self.assertEqual(self.download(digest).returncode, 0)
        self.assertEqual(self.model.read_bytes(), b"partial")
        self.assertEqual(len(self.calls.read_text().splitlines()), 2)
        self.assertEqual(self.download(digest).returncode, 0)
        self.assertEqual(len(self.calls.read_text().splitlines()), 2)
        self.model.write_bytes(b"corrupt")
        self.assertNotEqual(self.download(digest).returncode, 0)

    def test_incorrect_download_is_not_promoted(self):
        self.assertNotEqual(self.download("0" * 64).returncode, 0)
        self.assertFalse(self.model.exists())
        self.assertEqual(self.model.with_suffix(".gguf.part").read_bytes(), b"partial")


if __name__ == "__main__":
    unittest.main()
