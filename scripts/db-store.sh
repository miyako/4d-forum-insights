#!/usr/bin/env bash
# Persist the pipeline's working database between delta runs as an ENCRYPTED GitHub Release asset.
#
# The repository is public and the database contains full forum thread text, so the asset is
# zstd-compressed and then encrypted (AES-256-CBC, PBKDF2) with the passphrase in $DB_KEY
# (a repository secret on Actions). The previous copy is kept as forum.db.prev.zst.enc.
#
#   scripts/db-store.sh pull [db]   download + decrypt into db   (default build/work.db)
#   scripts/db-store.sh push [db]   compress + encrypt + upload  (VACUUMs a temp copy first)
#   scripts/db-store.sh stage [db] [output]   encrypt without publishing
#   scripts/db-store.sh restore [db] [input] decrypt a downloaded checkpoint
#
# Env: DB_KEY (required), DB_RELEASE (release tag, default "data"), GH_REPO (default: current repo)
set -euo pipefail
cmd=${1:?usage: db-store.sh pull|push|stage|restore [db] [file]}
db=${2:-build/work.db}
if [ "$cmd" = push ] && [ "${DATA_PUBLICATION_CHILD:-}" != 1 ]; then
    exec python3 "$(dirname "$0")/publish_data.py" with-lock -- bash "$0" "$@"
fi
tag=${DB_RELEASE:-data}
asset=forum.db.zst.enc
: "${DB_KEY:?set DB_KEY (the passphrase used to encrypt the DB asset)}"
mkdir -p build
tmp="build/db-store-$(python3 -c 'import uuid; print(uuid.uuid4().hex)')"
mkdir "$tmp"
trap 'rm -rf "$tmp"' EXIT
enc() { openssl enc -aes-256-cbc -pbkdf2 -iter 200000 -salt -pass env:DB_KEY "$@"; }

ensure_release() {
    gh release view "$tag" >/dev/null 2>&1 || gh release create "$tag" --title "Published data" \
        --notes "Data assets for the site (site-data.tar.gz) and the encrypted pipeline DB. Updated by the delta workflow." \
        --latest=false
}

case $cmd in
pull|restore)
    mkdir -p "$(dirname "$db")"
    if [ "$cmd" = restore ]; then
        cp "${3:?restore requires an encrypted input file}" "$tmp/$asset"
    else
        gh release download "$tag" --pattern "$asset" --dir "$tmp"
    fi
    enc -d -in "$tmp/$asset" | zstd -d -q -o "$db.tmp" -f
    sqlite3 "$db.tmp" "PRAGMA integrity_check;" | grep -qx ok
    mv "$db.tmp" "$db"
    echo "pulled $tag/$asset -> $db ($(du -h "$db" | cut -f1))"
    ;;
push|stage)
    [ -f "$db" ] || { echo "$db not found"; exit 1; }
    sqlite3 "$db" ".backup $tmp/forum.db"
    sqlite3 "$tmp/forum.db" "PRAGMA integrity_check;" | grep -qx ok
    sqlite3 "$tmp/forum.db" "PRAGMA journal_mode=DELETE; VACUUM;" >/dev/null
    zstd -q -19 -T0 "$tmp/forum.db" -o "$tmp/forum.db.zst"
    enc -in "$tmp/forum.db.zst" -out "$tmp/$asset"
    if [ "$cmd" = stage ]; then
        output=${3:-build/delta-checkpoint/forum.db.zst.enc}
        mkdir -p "$(dirname "$output")"
        mv "$tmp/$asset" "$output"
        echo "staged encrypted checkpoint -> $output"
        exit 0
    fi
    ensure_release
    if gh release download "$tag" --pattern "$asset" --dir "$tmp/prev" 2>/dev/null; then
        mv "$tmp/prev/$asset" "$tmp/forum.db.prev.zst.enc"
        gh release upload "$tag" "$tmp/forum.db.prev.zst.enc" --clobber
    fi
    gh release upload "$tag" "$tmp/$asset" --clobber
    echo "pushed $db -> $tag/$asset ($(du -h "$tmp/$asset" | cut -f1))"
    ;;
*)
    echo "usage: db-store.sh pull|push|stage|restore [db] [file]"; exit 2 ;;
esac
