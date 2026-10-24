# Delta updates: one-time setup of `DB_KEY` and the encrypted DB

The weekly [delta workflow](../.github/workflows/delta.yml) needs the pipeline's working database from the previous run. This page explains where that database lives, what `DB_KEY` is, and how to set both up once.

## What is what

There are two different files on the [`data` release](https://github.com/miyako/4d-forum-insights/releases/tag/data):

| Asset | What it is | Public? |
|---|---|---|
| `site-data.tar.gz` | The website's data: titles, English titles, machine-generated summaries, metadata, model scores and search vectors. These are the same files the site serves under `/data/`. | Yes, by design |
| `forum.db.zst.enc` | The pipeline's working database (`forum.db`, about 160 MB), which contains **full forum thread text**. It is zstd-compressed and then AES-256 encrypted (about 47 MB). The previous copy is kept as `forum.db.prev.zst.enc`. | No: unreadable without `DB_KEY` |

- The repository is public, so anyone can download release assets. That is why the database is encrypted.
- `forum.db.zst.enc` exists only after the one-time seed below. Until then, the delta workflow logs a warning and does nothing.

**`DB_KEY`** is the passphrase that encrypts and decrypts `forum.db.zst.enc`. It is the same value in two places:

| Where | Needed | Why |
|---|---|---|
| GitHub repository secret `DB_KEY` | Always | The workflow uses it to decrypt the database, apply the delta and re-encrypt it. |
| macOS Keychain on your Mac | Only when you run `make db-seed`, `make db-pull` or `make db-push` | Your Mac does the first encryption, and later any local runs. |

- **Do not** put it in `~/.zshrc` or any file in the repository, and do not paste it into chats or issues.
- GitHub secrets are capped at 48 KB, so the database itself cannot be a secret. Storing only the passphrase as a secret, next to an encrypted file, is GitHub's recommended pattern for large secrets.

## Setup

Requirements: `openssl` (3.x), `zstd`, `sqlite3` and `gh`, logged in as a repository admin. Use either option for steps 1 and 2.

### Option A: Terminal

1. Create a random passphrase and store it in the Keychain:

   ```sh
   security add-generic-password -a "$USER" -s 4d-forum-insights-DB_KEY -w "$(openssl rand -base64 32)"
   security find-generic-password -s 4d-forum-insights-DB_KEY -w     # check: prints it
   ```

2. Copy it from the Keychain into the GitHub secret:

   ```sh
   security find-generic-password -s 4d-forum-insights-DB_KEY -w | tr -d '\n' \
     | gh secret set DB_KEY -R miyako/4d-forum-insights
   gh secret list -R miyako/4d-forum-insights                         # check: lists DB_KEY
   ```

### Option B: Apps

1. **Keychain Access**: choose **File → New Password Item…**, then fill in:
   - Keychain Item Name: `4d-forum-insights-DB_KEY`
   - Account Name: your macOS user name
   - Password: the output of `openssl rand -base64 32`
2. **GitHub**: open [Settings → Secrets and variables → Actions](https://github.com/miyako/4d-forum-insights/settings/secrets/actions), click **New repository secret**, then fill in:
   - Name: `DB_KEY`
   - Secret: the same passphrase

### Step 3, for both options: seed the encrypted DB (once)

Run this in a checkout of this repository after the upstream `~/4d-feature-miner` pipeline has finished:

```sh
DB_KEY="$(security find-generic-password -s 4d-forum-insights-DB_KEY -w)" make db-seed
gh release view data -R miyako/4d-forum-insights      # check: forum.db.zst.enc is listed
```

`make db-seed` does three things:

- It takes a read-only snapshot of `~/4d-feature-miner/data/forum.db` (`make snapshot`).
- It compresses and encrypts the snapshot.
- It uploads the result as `forum.db.zst.enc`.

`DB_KEY` is set only for that one command.

## After setup

- The delta workflow runs every Monday at 03:17 UTC. To run it now: `gh workflow run delta.yml -R miyako/4d-forum-insights`.
- **Local runs** (the fallback in the README) read the key the same way:

  ```sh
  export DB_KEY="$(security find-generic-password -s 4d-forum-insights-DB_KEY -w)"   # this shell only
  make db-pull && make delta-local && make db-push
  ```

## Lost or rotating the key

- **If the Keychain entry is lost**, the workflow keeps working, because GitHub still has the secret. However, GitHub never shows secret values, so you cannot read the database from your Mac any more.
- **To recover, or to rotate the key**:
  1. Create a new passphrase and update both the Keychain entry and the secret (steps 1 and 2).
  2. Re-seed from a local database: `make db-seed` (from upstream), or `make db-push` after `make delta-local`.

  Copies encrypted with the old key cannot be decrypted with the new one. Each push keeps the previous copy as `forum.db.prev.zst.enc`, so after the second push both assets use the new key.
- To update an existing Keychain entry, add `-U`: `security add-generic-password -U -a "$USER" -s 4d-forum-insights-DB_KEY -w "<new>"`.
