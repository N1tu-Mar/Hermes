# Operating HERMES

This guide applies to both modes. Remote-only setup is in [remote-deployment.md](remote-deployment.md).

## Install reproducibly

Direct dependencies are pinned in `requirements.in` (runtime) and `requirements-dev.in` (tests/CI). Everything that gets
installed, including transitive packages, is locked with hashes for Python 3.13 on Linux, macOS, and Windows:

```bash
python3.13 -m venv .venv
.venv/bin/pip install --require-hashes --no-deps -r requirements.lock       # run the app
.venv/bin/pip install --require-hashes --no-deps -r requirements-dev.lock   # run tests / CI checks
.venv/bin/python -m playwright install chromium                             # browser tests only
```

To change a dependency, edit the `.in` file, then regenerate both locks:

```bash
uv pip compile requirements.in --universal --python-version 3.13 --generate-hashes -o requirements.lock
uv pip compile requirements-dev.in --universal --python-version 3.13 --generate-hashes -o requirements-dev.lock
```

## Checks (same as CI)

```bash
.venv/bin/ruff format --check .
.venv/bin/ruff check .
.venv/bin/python -m app.ops check-schemas
.venv/bin/python -m pytest -q                    # all tests, including browser and load tests
.venv/bin/python -m pytest -q -m "not browser and not load"   # fast subset
```

CI (`.github/workflows/ci.yml`) runs these steps on every push and pull request, and it runs a gitleaks secret scan
over the full git history. Fake credentials used by tests carry an inline `gitleaks:allow` marker. The fingerprints of
older copies of those fake credentials are listed in `.gitleaksignore`.

Tests are split by subsystem: `test_intake`, `test_research`, `test_drafting`, `test_storage` (atomic JSON and
migrations), `test_workflow` (end to end, restart, graceful shutdown), `test_ops` and `test_backup_consistency` (backup/restore, deletion,
retention), `test_logging`, `test_health`, `test_config`, `test_remote` (auth and per-user isolation), `test_load`
(3 campaigns x 100 people concurrently), and `test_browser` (the critical workflow in headless Chromium under the
production CSP).

## Configuration

The app reads the environment and `.env` once at startup and validates all settings together. If anything is wrong,
it prints every problem and exits with status 2. It never starts half-configured. See `.env.example` for all
settings. Examples of what gets rejected:

- a non-loopback `APP_HOST` in local mode;
- remote mode without an `https://` public URL or a valid secret key, or with `APP_TOKEN` set;
- an unwritable `DATA_ROOT`, an out-of-range port or retention value, an unknown log level, or a malformed
  `OPENAI_API_KEY`.

## Health and diagnostics

| Endpoint | Auth | Returns |
|---|---|---|
| `GET /healthz` | none | `{"status":"ok"}` while the process serves requests |
| `GET /readyz` | none | 200 when the data root is writable, the database answers, and job workers are alive; otherwise 503 with the failing check names |
| `GET /api/diagnostics` | token / session | version, mode, schema versions, job counts by status, queue depths, worker state, whether Gmail is connected, model name |

None of these return keys, tokens, email addresses, names, or draft text. In remote mode, diagnostics only cover the
calling user's own data.

## Logs

Logs are JSON lines on stderr, one object per event. Each object has `ts`, `level`, `logger`, and `msg`. Where they
apply, it also has `request_id`, `job_id`, `campaign_id`, `kind`, `status`, `duration_ms`, `method`, `path`, and
`user_id`.

- Each HTTP request gets a `request_id`. The app reuses a safe incoming `X-Request-ID` if one is sent, and always
  returns the ID in the response header. A job carries both its own `job_id` and the `request_id` that queued it, so
  you can follow one click through every background job it started.
- Every message and traceback passes through a redaction filter before it is written. The filter removes values of
  sensitive keys (token, password, secret, API key, cookie, authorization, subject, body, code), bearer tokens,
  OpenAI keys, Google OAuth tokens, Fernet ciphertext, email addresses, phone numbers, and long opaque strings.
- The code itself never logs draft text, page text, or names. Uvicorn access logs are off, because the local startup
  URL carries the app token. The request log records only the method, the path (without the query string), the
  status, and the duration.

Set `LOG_LEVEL=DEBUG|INFO|WARNING|ERROR`.

## Schema migrations

There are two kinds of stored state. Both use explicit, append-only migrations (`app/migrations.py`):

- **SQLite** (`cache.sqlite3`, and `auth.sqlite3` in remote mode): each database module owns a `MIGRATIONS` list,
  and the version is tracked in `PRAGMA user_version`. Pending steps run automatically at startup, each inside its
  own transaction. A failed step rolls back and leaves the version unchanged. A database from a newer release is
  refused.
- **Campaign JSON** (`candidates.json`, `research.json`): `schema_version` is checked on every read. A file at an
  older version is upgraded by the steps registered in `JSON_MIGRATIONS` when the app starts, or when you run
  `python -m app.ops migrate`.

Before any pending step runs, the affected file is copied next to itself as `<file>.v<old>-<UTC timestamp>.bak`.

To add a migration, append a SQL script to the module's `MIGRATIONS` list. For JSON, register
`JSON_MIGRATIONS[n] = fn` and bump `contracts.SCHEMA_VERSION` to `n + 1`. Never edit a step that has already been
applied.

### Rolling back a migration

Follow these steps in order:

1. Stop the app (`systemctl stop hermes`, or Ctrl+C).
2. For each file you want to roll back, copy its `.bak` over the original. For SQLite, delete the matching
   `-wal` and `-shm` files first:

   ```bash
   rm -f data/cache.sqlite3-wal data/cache.sqlite3-shm
   cp data/cache.sqlite3.v1-20260101T000000Z.bak data/cache.sqlite3
   cp data/cmp_x/candidates.json.v1-20260101T000000Z.bak data/cmp_x/candidates.json
   ```

3. Check out and start the previous release. Step 2 has to come first: an older release refuses to open a
   database or JSON file whose schema version is newer than it knows.

If the per-file `.bak` copies are missing or inconsistent, restore a full encrypted backup instead (see below). Take
one before every upgrade.

## Backup and restore

```bash
python -m app.ops backup /secure/place/hermes-2026-09-26.hbk     # app must be stopped; prompts for a passphrase (or HERMES_BACKUP_PASSPHRASE)
python -m app.ops restore /secure/place/hermes-2026-09-26.hbk    # app must be stopped
```

- **Consistency contract:** `backup` takes the exclusive data-root lock (the same lock a running app holds), so it
  **refuses to run while the app is up** ("stop the app first"), and no app can start while it runs. JSON files,
  attachments and SQLite files are therefore one point in time. SQLite files are still copied with SQLite's online
  backup API. Calling `ops.backup(...)` from code without `exclusive=True` skips the lock: each database is
  consistent, but files are not consistent with each other. Do not use that for real backups.
- **Output location:** the backup file must be outside `DATA_ROOT` (symlinks are resolved first); otherwise it
  would be captured into later backups and vanish with the data it protects. Backups are written to
  `FILE.part`, fsynced, then renamed, so an existing good backup is never replaced by a partial one.
- **What is included:** everything under `DATA_ROOT`. That covers campaigns (intake and candidates), research
  profiles, drafts and approvals, Gmail idempotency state, jobs, usage and budgets, the activity log, the page and
  research caches, any other files stored there, and in remote mode every user's data plus `auth.sqlite3` (accounts,
  session hashes, and encrypted provider credentials). Email templates and outline rules are code (`app/outlines.py`)
  and are versioned in git, not in backups. Migration `.bak` files, WAL files, and lock files are skipped.
- **What is not included:** secrets that live outside `DATA_ROOT` on purpose. That means `.env`, the local Gmail
  token in `~/.config/outreach/`, and `HERMES_SECRET_KEY`. After a restore, local mode may need `python -m app.gmail`
  again. Remote mode needs the same `HERMES_SECRET_KEY` to decrypt stored provider credentials. Without it, users
  must re-enter their OpenAI key and reconnect Gmail. Everything else still restores.
- **Format (version 2):** `HERMES-BACKUP-1` header, a random salt, a `STREAM2` marker, then 1 MiB AES-256-GCM
  frames over a streamed tar.gz. The key comes from your passphrase with scrypt (n=2^15, r=8, p=1). Each frame's
  nonce is its position and the last frame is flagged, so a modified, reordered, dropped or truncated frame fails
  authentication. The file is mode 0600. Passphrases shorter than 12 characters are refused, and a lost passphrase
  cannot be recovered. Restore still reads the older version-1 format (one Fernet token, held in memory).
- **Manifest and hashes:** the last archive member, `hermes-backup.json`, records the format version, the JSON and
  SQLite schema versions, and every file's size and SHA-256 (computed over the exact bytes archived).
- **Memory and staging:** backup and restore stream one chunk at a time, so archive size is not limited by RAM.
  SQLite snapshots are staged in a `0700` directory next to the output file (`.hermes-backup-*`) and removed on
  success, error or disk-full. The `.part` file is removed on failure. Restore stages into a `0700`
  `.<root>.restore-*` directory beside `DATA_ROOT` and removes it on any failure.
- **How restore works:** under the data-root lock it decrypts and extracts to the staging directory (Python's `data`
  tar filter rejects absolute paths, `..`, and links that escape), then verifies everything **before touching live
  data**: the manifest is present and well formed, the backup's schema is not newer than the app, the file set
  matches the manifest exactly, every size and SHA-256 matches, there are no symlinks or special files, and each
  SQLite file passes `PRAGMA integrity_check` (with `cache.sqlite3` not newer than this app's migrations). Only then
  does it rename the current root to `<root>.pre-restore-<timestamp>` and swap the restored copy in. A wrong
  passphrase, corruption or truncation stops with an error and leaves the live root as it was.
- **Verification:** `tests/test_backup_consistency.py` covers the lock contract (a running app blocks backup, and a
  writer cannot start mid-backup), backups while another thread writes to SQLite, manifest contents, output inside
  `DATA_ROOT`, 0700 staging and cleanup, disk-full cleanup (previous backup survives), bounded memory on a 24 MB
  archive, bit-flip / truncation / wrong-passphrase / frame-drop corruption, authentic archives with bad hashes,
  extra files or newer schemas, and a full CLI backup, wipe, restore drill that reopens the restored data.
  `tests/test_ops.py` also takes a backup, deletes and edits data, and checks that a restore reproduces every file
  and SQLite row.

Take a restore drill (restore into a scratch `DATA_ROOT`) after any change to how you back up. Keep backups outside
the machine. Encryption makes them safe to store in ordinary cloud storage.

## Deletion and retention

| Operation | How | Removes |
|---|---|---|
| Delete a campaign | UI "Delete campaign", `DELETE /api/campaigns/{id}`, or `python -m app.ops delete-campaign ID` | the campaign directory; every draft, job, usage, and activity row; its cached research |
| Delete a person | UI "Delete this person" or `DELETE /api/campaigns/{id}/candidates/{cid}` | candidate record, research profile, drafts, jobs, cached research, cached copies of their pages, and activity lines naming them |
| Forget a person everywhere | `python -m app.ops forget --email addr` or `--name "Full Name"` | the above, in every campaign (and every user in remote mode) |
| Delete a remote user | `python -m app.ops user-delete NAME` | the account, sessions, encrypted credentials, and that user's entire data root |
| Retention purge | automatic at startup, or `python -m app.ops purge [--days N] [--campaigns-older-than N]` | expired page/research caches, events and finished jobs older than `RETENTION_DAYS`, and optionally whole campaigns untouched for N days |

Deletions refuse to run while a job for that campaign or person is still queued or running. Stop the campaign first.
SQLite runs with `secure_delete=ON`, so deleted rows are overwritten rather than left in free pages.

Deletion has limits you should know about:

- Gmail drafts that HERMES already created live in your Gmail account. Delete those in Gmail.
- Existing backups still contain the deleted data until they expire, so rotate them.
- Migration `.bak` files in the data root keep pre-migration copies; delete them once you have confirmed an upgrade.

## Graceful shutdown and recovery

On SIGTERM or Ctrl+C:

1. Workers stop taking new jobs.
2. Running jobs get `SHUTDOWN_GRACE_SECONDS` to finish.
3. Anything still unfinished is cancelled and checkpointed as `interrupted` in SQLite.
4. The OpenAI client, the page fetcher's HTTP client, the Gmail client, and the database are closed.

Finished research and drafts are always saved before a job reports done. After a restart, **Resume** re-queues only
the interrupted work and skips people who are already researched or drafted. This is covered by
`test_graceful_shutdown_checkpoints_and_recovers` and `test_restart_resumes_without_repeating`.

One process owns a data root at a time. It holds an exclusive lock on `DATA_ROOT/.lock`, and a second instance, or
a restore, refuses to start while the lock is held. The lock uses `fcntl`, so it supports macOS and Linux only.

## Capacity

`test_load.py` runs 3 campaigns of 100 people (the per-campaign maximum) at the same time. It researches and drafts
all 600 jobs while four client threads poll views and edit intake. It checks that no JSON update is lost, that no
job is duplicated or fails, and that the files stay valid. On a laptop the run takes about 16 seconds with a
simulated 2 ms model latency. In practice, the model API's latency and your budget are the limit. Per-campaign JSON
rewrites are O(n), which is fine at this size. Past a few hundred people per campaign, move candidates to SQLite.
