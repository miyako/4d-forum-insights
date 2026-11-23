#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../.."
umask 077
: "${DATA_PUBLICATION_CHILD:?run inside publish_data.py with-lock}"
: "${DB_KEY:?DB_KEY is required}"
: "${RUNNER_TEMP:?runner temporary directory is required}"
[[ "${BASELINE_SHA256:-}" =~ ^[a-f0-9]{64}$ ]]

work=$(mktemp -d "$RUNNER_TEMP/questions-public-baseline.XXXXXXXX")
trap 'rm -f "$work/input/questions/preview.json" "$work/input/locales/fr/questions.json" "$work/input/locales/ja/questions.json" "$work/input/SHA256SUMS" "$work/baseline.tar.zst" "$work/verified.tar.zst" "$work/questions-public-baseline.tar.zst.enc" "$work/retrieved/questions-public-baseline.tar.zst.enc"; rmdir "$work/input/questions" "$work/input/locales/fr" "$work/input/locales/ja" "$work/input/locales" "$work/input" "$work/retrieved" "$work"' EXIT
files=(questions/preview.json locales/fr/questions.json locales/ja/questions.json)
mkdir -p "$work/input/questions" "$work/input/locales/fr" "$work/input/locales/ja" "$work/retrieved"
for file in "${files[@]}"; do
    curl --fail --silent --show-error --retry 2 --max-time 120 \
        "https://miyako.github.io/4d-forum-insights/data/$file" -o "$work/input/$file"
done
(
    cd "$work/input"
    sha256sum "${files[@]}" > SHA256SUMS
    digest=$(sha256sum < SHA256SUMS)
    [[ "${digest%% *}" == "$BASELINE_SHA256" ]]
) || { echo "::error::Published Questions bytes do not match the requested baseline"; exit 1; }
python3 - "$work/input" <<'PY'
import json
from pathlib import Path
import sys

sys.path.insert(0, "scripts")
from publish_data import public_questions_overlays, read_public_questions, validate_overlay

root = Path(sys.argv[1])
snapshot = read_public_questions(root / "questions/preview.json")
if snapshot["corpus_complete"] is not True:
    raise ValueError("Expected the complete public Questions release")
overlays = {}
for locale in ("fr", "ja"):
    path = f"locales/{locale}/questions.json"
    overlay = validate_overlay(json.loads((root / path).read_text()))
    if overlay["locale"] != locale or overlay["page"] != "questions":
        raise ValueError("Invalid public Questions locale identity")
    overlays["data/" + path] = overlay
public_questions_overlays(overlays, snapshot, require_complete=True)
print(f"Complete published baseline: {len(snapshot['entries'])} records in EN, FR, and JA")
PY
tar -C "$work/input" -cf - SHA256SUMS "${files[@]}" | zstd -q -o "$work/baseline.tar.zst"
asset=questions-public-baseline.tar.zst.enc
enc=(openssl enc -aes-256-cbc -pbkdf2 -iter 200000 -pass env:DB_KEY)
"${enc[@]}" -salt -in "$work/baseline.tar.zst" -out "$work/$asset"
"${enc[@]}" -d -in "$work/$asset" -out "$work/verified.tar.zst"
cmp "$work/baseline.tar.zst" "$work/verified.tar.zst"
gh release upload data "$work/$asset" --repo "$GITHUB_REPOSITORY"
gh release download data --pattern "$asset" --dir "$work/retrieved" --repo "$GITHUB_REPOSITORY"
cmp "$work/$asset" "$work/retrieved/$asset"
echo "Encrypted public baseline uploaded and verified; weekly processing and publication were not run."
