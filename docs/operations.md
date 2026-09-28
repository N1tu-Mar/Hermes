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
migrations), `test_workflow` (end to end, restart, graceful shutdown), `test_ops` (backup/restore, deletion,
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
python -m app.ops backup /secure/place/hermes-2026-09-26.hbk     # prompts for a passphrase (or HERMES_BACKUP_PASSPHRASE)
python -m app.ops restore /secure/place/hermes-2026-09-26.hbk    # app must be stopped
```

- **What is included:** everything under `DATA_ROOT`. That covers campaigns (intake and candidates), research
  profiles, drafts and approvals, Gmail idempotency state, jobs, usage and budgets, the activity log, the page and
  research caches, any other files stored there, and in remote mode every user's data plus `auth.sqlite3` (accounts,
  session hashes, and encrypted provider credentials). Email templates and outline rules are code (`app/outlines.py`)
  and are versioned in git, not in backups. SQLite files are copied with SQLite's online backup API, so a backup
  taken while the app runs is still consistent. Migration `.bak` files, WAL files, and lock files are skipped.
- **What is not included:** secrets that live outside `DATA_ROOT` on purpose. That means `.env`, the local Gmail
  token in `~/.config/outreach/`, and `HERMES_SECRET_KEY`. After a restore, local mode may need `python -m app.gmail`
  again. Remote mode needs the same `HERMES_SECRET_KEY` to decrypt stored provider credentials. Without it, users
  must re-enter their OpenAI key and reconnect Gmail. Everything else still restores.
- **Format:** `HERMES-BACKUP-1` header, a random salt, then a Fernet token (AES-128-CBC with HMAC-SHA256) over a
  tar.gz archive. The key is derived from your passphrase with scrypt (n=2^15, r=8, p=1). The file is written with
  mode 0600. A wrong passphrase or a modified file is rejected before anything on disk changes. Passphrases shorter
  than 12 characters are refused, and a lost passphrase cannot be recovered.
- **How restore works:** it decrypts the backup, extracts it with Python's `data` tar filter (which rejects absolute
  paths, `..`, and links that escape the directory) into a sibling staging directory, and checks that the backup's
  schema is not newer than the app. It then renames the current data root to `<root>.pre-restore-<timestamp>` and
  swaps the restored copy into place. It refuses to run while the app holds the data-root lock.
- **Verification:** `tests/test_ops.py` takes a backup, deletes a person, edits a draft, and deletes an attached
  file, then restores the backup. It asserts that every file and every SQLite row matches the original.

Backups are built in memory, which is fine for personal-scale data (tens of MB). Keep backups outside the machine.
Encryption makes them safe to store in ordinary cloud storage.

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

Multiple agents work this repo in parallel across sibling worktrees (one branch each) sharing one `.git`.
Only one may merge into `main` at a time: wrap the merge in `scripts/merge_lock.py`, which holds an exclusive
`fcntl` lock on `.git/merge.lock` (shared across every worktree) for the duration of the command, e.g.

```
.venv/bin/python scripts/merge_lock.py -- git merge --no-ff agent/x -m "..." && git push origin main
```

A second agent's call blocks until the first releases it (or fails after `--timeout` seconds if given).

## Capacity

`test_load.py` runs 3 campaigns of 100 people (the per-campaign maximum) at the same time. It researches and drafts
all 600 jobs while four client threads poll views and edit intake. It checks that no JSON update is lost, that no
job is duplicated or fails, and that the files stay valid. On a laptop the run takes about 16 seconds with a
simulated 2 ms model latency. In practice, the model API's latency and your budget are the limit. Per-campaign JSON
rewrites are O(n), which is fine at this size. Past a few hundred people per campaign, move candidates to SQLite.
