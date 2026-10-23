#!/usr/bin/env bash
# Persist the pipeline's working database between delta runs as an ENCRYPTED GitHub Release asset.
#
# The repository is public and the database contains full forum thread text, so the asset is
# zstd-compressed and then encrypted (AES-256-CBC, PBKDF2) with the passphrase in $DB_KEY
# (a repository secret on Actions). The previous copy is kept as forum.db.prev.zst.enc.
#
#   scripts/db-store.sh pull [db]   download + decrypt into db   (default build/work.db)
#   scripts/db-store.sh push [db]   compress + encrypt + upload  (VACUUMs a temp copy first)
#
# Env: DB_KEY (required), DB_RELEASE (release tag, default "data"), GH_REPO (default: current repo)
set -euo pipefail
cmd=${1:?usage: db-store.sh pull|push [db]}
db=${2:-build/work.db}
tag=${DB_RELEASE:-data}
asset=forum.db.zst.enc
: "${DB_KEY:?set DB_KEY (the passphrase used to encrypt the DB asset)}"
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
enc() { openssl enc -aes-256-cbc -pbkdf2 -iter 200000 -salt -pass env:DB_KEY "$@"; }

ensure_release() {
    gh release view "$tag" >/dev/null 2>&1 || gh release create "$tag" --title "Published data" \
        --notes "Data assets for the site (site-data.tar.gz) and the encrypted pipeline DB. Updated by the delta workflow." \
        --latest=false
}

case $cmd in
pull)
    mkdir -p "$(dirname "$db")"
    gh release download "$tag" --pattern "$asset" --dir "$tmp"
    enc -d -in "$tmp/$asset" | zstd -d -q -o "$db.tmp" -f
    sqlite3 "$db.tmp" "PRAGMA integrity_check;" | grep -qx ok
    mv "$db.tmp" "$db"
    echo "pulled $tag/$asset -> $db ($(du -h "$db" | cut -f1))"
    ;;
push)
    [ -f "$db" ] || { echo "$db not found"; exit 1; }
    sqlite3 "$db" ".backup $tmp/forum.db"
    sqlite3 "$tmp/forum.db" "PRAGMA journal_mode=DELETE; VACUUM;" >/dev/null
    zstd -q -19 -T0 "$tmp/forum.db" -o "$tmp/forum.db.zst"
    enc -in "$tmp/forum.db.zst" -out "$tmp/$asset"
    ensure_release
    if gh release download "$tag" --pattern "$asset" --dir "$tmp/prev" 2>/dev/null; then
        mv "$tmp/prev/$asset" "$tmp/forum.db.prev.zst.enc"
        gh release upload "$tag" "$tmp/forum.db.prev.zst.enc" --clobber
    fi
    gh release upload "$tag" "$tmp/$asset" --clobber
    echo "pushed $db -> $tag/$asset ($(du -h "$tmp/$asset" | cut -f1))"
    ;;
*)
    echo "usage: db-store.sh pull|push [db]"; exit 2 ;;
esac
