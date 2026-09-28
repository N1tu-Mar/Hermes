"""Remote-mode accounts: users, sessions, CSRF tokens, encrypted per-user secrets.

Only used when HERMES_MODE=remote. Passwords use stdlib scrypt; session tokens
are random 256-bit values stored only as SHA-256 hashes; provider secrets
(OpenAI key, Gmail OAuth token) are Fernet-encrypted with HERMES_SECRET_KEY,
which never lives in the data directory or its backups. Users are created
from the CLI (`python -m app.ops user-add`); there is no self-signup.

Login throttling is durable (sqlite-backed, survives restarts) and bounded:
failure counters live in `login_failures`, keyed by scope ("account", "source",
"global"), with a hard row cap so an attacker cycling through unlimited bogus
usernames cannot grow unbounded state. All counter reads/writes happen while
holding `self.lock`, which also serializes the single sqlite connection, so
the threshold check + increment is atomic under concurrent callers.
"""

import hashlib
import logging
import os
import re
import secrets
import shutil
import sqlite3
import threading
import time
from pathlib import Path

from cryptography.fernet import Fernet

from .migrations import migrate_sqlite

log = logging.getLogger("auth")

MIGRATIONS = [
    """
CREATE TABLE users (
  id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL UNIQUE, pw_hash TEXT NOT NULL,
  disabled INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL);
CREATE TABLE sessions (
  token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  csrf TEXT NOT NULL, created_at REAL NOT NULL, seen_at REAL NOT NULL);
CREATE TABLE secrets (
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, name TEXT NOT NULL,
  value BLOB NOT NULL, updated_at REAL NOT NULL, PRIMARY KEY (user_id, name));
""",
    """
CREATE TABLE login_failures (
  scope TEXT NOT NULL, key TEXT NOT NULL, count INTEGER NOT NULL,
  window_start REAL NOT NULL, updated_at REAL NOT NULL, PRIMARY KEY (scope, key));
CREATE INDEX idx_login_failures_updated ON login_failures(updated_at);
""",
]

IDLE_TIMEOUT = 12 * 3600
ABSOLUTE_TIMEOUT = 7 * 86400
MIN_PASSWORD = 12
MAX_PASSWORD = 1024  # reject before scrypt runs; unbounded input is a CPU-exhaustion vector
MAX_USERNAME = 256  # login() takes arbitrary strings; add_user() is further constrained by USERNAME_RE
USERNAME_RE = re.compile(r"^[a-z0-9_.-]{3,32}$")
SECRET_NAMES = {"openai_api_key", "gmail_token"}

# throttling: three independent scopes, each with its own ceiling over the same rolling window
LOCKOUT = 15 * 60
MAX_FAILURES = 5  # per account
SOURCE_MAX_FAILURES = 30  # per source (e.g. client IP), when the caller supplies one
GLOBAL_MAX_FAILURES = 500  # across all accounts/sources combined
MAX_FAILURE_ROWS = 2000  # hard cap on tracked (scope, key) rows regardless of window freshness
GLOBAL_KEY = "*"

SCRYPT_PARAMS = {"n": 2**14, "r": 8, "p": 1}


def hash_password(password, params=None):
    p = params or SCRYPT_PARAMS
    salt = os.urandom(16)
    h = hashlib.scrypt(password.encode(), salt=salt, n=p["n"], r=p["r"], p=p["p"], dklen=32)
    return f"scrypt${p['n']}${p['r']}${p['p']}${salt.hex()}${h.hex()}"


def check_password(password, stored):
    try:
        _, n, r, p, salt, h = stored.split("$")
        got = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p), dklen=32)
    except ValueError:
        return False
    return secrets.compare_digest(got.hex(), h)


def _needs_rehash(stored):
    """True if `stored` was hashed with params weaker/different than the current target."""
    try:
        _, n, r, p, _, _ = stored.split("$")
        return (int(n), int(r), int(p)) != (SCRYPT_PARAMS["n"], SCRYPT_PARAMS["r"], SCRYPT_PARAMS["p"])
    except ValueError:
        return True


_DUMMY_HASH = hash_password(secrets.token_hex(8))  # equalizes timing for unknown usernames


def _digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


class Accounts:
    def __init__(self, data_root, secret_key):
        self.root = Path(data_root)
        path = self.root / "auth.sqlite3"
        self.db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA secure_delete=ON")
        migrate_sqlite(self.db, path, MIGRATIONS)
        os.chmod(path, 0o600)
        self.fernet = Fernet(secret_key)
        self.lock = threading.Lock()

    def q(self, sql, args=()):
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    def x(self, sql, args=()):
        with self.lock:
            return self.db.execute(sql, args).rowcount

    def close(self):
        self.db.close()

    def user_dir(self, user_id):
        return self.root / "users" / str(int(user_id))

    # ------------------------------------------------------------ failure throttling (durable, bounded)
    def _status_locked(self, scope, key, limit, now):
        """Caller holds self.lock. Returns (locked, retry_after_seconds)."""
        row = self.db.execute("SELECT count, window_start FROM login_failures WHERE scope=? AND key=?", (scope, key)).fetchone()
        if not row or now - row["window_start"] > LOCKOUT:
            return False, 0.0
        if row["count"] < limit:
            return False, 0.0
        return True, max(0.0, LOCKOUT - (now - row["window_start"]))

    def _bump_failure_locked(self, scope, key, now):
        row = self.db.execute("SELECT count, window_start FROM login_failures WHERE scope=? AND key=?", (scope, key)).fetchone()
        if row and now - row["window_start"] <= LOCKOUT:
            count, window_start = row["count"] + 1, row["window_start"]
        else:
            count, window_start = 1, now
        self.db.execute(
            "INSERT INTO login_failures (scope, key, count, window_start, updated_at) VALUES (?,?,?,?,?) "
            "ON CONFLICT(scope, key) DO UPDATE SET count=excluded.count, window_start=excluded.window_start, updated_at=excluded.updated_at",
            (scope, key, count, window_start, now),
        )

    def _clear_failure_locked(self, scope, key):
        self.db.execute("DELETE FROM login_failures WHERE scope=? AND key=?", (scope, key))

    def _evict_stale_failures_locked(self, now):
        self.db.execute("DELETE FROM login_failures WHERE ? - window_start > ?", (now, LOCKOUT))
        n = self.db.execute("SELECT COUNT(*) FROM login_failures").fetchone()[0]
        if n > MAX_FAILURE_ROWS:
            self.db.execute(
                "DELETE FROM login_failures WHERE rowid IN "
                "(SELECT rowid FROM login_failures ORDER BY updated_at ASC LIMIT ?)",
                (n - MAX_FAILURE_ROWS,),
            )

    def login_status(self, username, source=None):
        """Read-only throttle check: {"locked": bool, "retry_after": seconds}. Never touches counters."""
        now = time.time()
        with self.lock:
            acct_locked, acct_ra = self._status_locked("account", username, MAX_FAILURES, now)
            src_locked, src_ra = (False, 0.0)
            if source:
                src_locked, src_ra = self._status_locked("source", str(source), SOURCE_MAX_FAILURES, now)
            glob_locked, glob_ra = self._status_locked("global", GLOBAL_KEY, GLOBAL_MAX_FAILURES, now)
        locked = acct_locked or src_locked or glob_locked
        retry_after = max(acct_ra, src_ra, glob_ra) if locked else 0.0
        return {"locked": locked, "retry_after": retry_after}

    def cleanup_expired(self):
        """Delete expired sessions and stale/overflow failure rows. Independent of login(); safe to call periodically."""
        now = time.time()
        with self.lock:
            sessions_deleted = self.db.execute(
                "DELETE FROM sessions WHERE seen_at < ? OR created_at < ?", (now - IDLE_TIMEOUT, now - ABSOLUTE_TIMEOUT)
            ).rowcount
            self._evict_stale_failures_locked(now)
        return sessions_deleted

    # ------------------------------------------------------------ users (CLI)
    def add_user(self, username, password):
        if not USERNAME_RE.match(username or ""):
            raise ValueError("username must be 3-32 chars of a-z, 0-9, _ . -")
        if len(password or "") < MIN_PASSWORD:
            raise ValueError(f"password must be at least {MIN_PASSWORD} characters")
        if len(password) > MAX_PASSWORD:
            raise ValueError(f"password must be at most {MAX_PASSWORD} characters")
        try:
            with self.lock:
                cur = self.db.execute(
                    "INSERT INTO users (username, pw_hash, created_at) VALUES (?,?,?)",
                    (username, hash_password(password), time.time()),
                )
        except sqlite3.IntegrityError:
            raise ValueError(f"user {username!r} already exists") from None
        return cur.lastrowid

    def _uid(self, username):
        rows = self.q("SELECT id FROM users WHERE username=?", (username,))
        if not rows:
            raise KeyError(f"no user {username!r}")
        return rows[0]["id"]

    def set_password(self, username, password):
        if len(password or "") < MIN_PASSWORD:
            raise ValueError(f"password must be at least {MIN_PASSWORD} characters")
        if len(password) > MAX_PASSWORD:
            raise ValueError(f"password must be at most {MAX_PASSWORD} characters")
        uid = self._uid(username)
        self.x("UPDATE users SET pw_hash=? WHERE id=?", (hash_password(password), uid))
        self.x("DELETE FROM sessions WHERE user_id=?", (uid,))

    def set_disabled(self, username, disabled=True):
        uid = self._uid(username)
        self.x("UPDATE users SET disabled=? WHERE id=?", (int(disabled), uid))
        self.x("DELETE FROM sessions WHERE user_id=?", (uid,))

    def delete_user(self, username):
        """Complete deletion: account, sessions, secrets, and the user's whole data root."""
        uid = self._uid(username)
        self.x("DELETE FROM users WHERE id=?", (uid,))  # sessions + secrets cascade
        shutil.rmtree(self.user_dir(uid), ignore_errors=True)
        return uid

    def list_users(self):
        return self.q("SELECT id, username, disabled, created_at FROM users ORDER BY id")

    # ------------------------------------------------------------ sessions
    def login(self, username, password, source=None):
        """Returns (session_token, csrf_token, user_id) or None.

        Throttles on three independent, durable counters: per-account, per-source
        (only when `source` is supplied, e.g. client IP), and global. Call
        `login_status()` to learn whether a caller is currently locked out and
        for how long (Retry-After), without mutating any counter.
        """
        username = str(username or "")
        password = str(password or "")
        now = time.time()
        if len(username) > MAX_USERNAME or len(password) > MAX_PASSWORD:
            log.warning("login rejected", extra={"status": "oversized_input"})
            return None
        source_key = str(source) if source else None
        with self.lock:
            acct_locked, _ = self._status_locked("account", username, MAX_FAILURES, now)
            src_locked = False
            if source_key:
                src_locked, _ = self._status_locked("source", source_key, SOURCE_MAX_FAILURES, now)
            glob_locked, _ = self._status_locked("global", GLOBAL_KEY, GLOBAL_MAX_FAILURES, now)
            blocked = acct_locked or src_locked or glob_locked

            row = None if blocked else self.db.execute(
                "SELECT id, pw_hash, disabled FROM users WHERE username=?", (username,)
            ).fetchone()
            ok = False if blocked else check_password(password, row["pw_hash"] if row else _DUMMY_HASH)

            if blocked or not ok or not row or row["disabled"]:
                self._bump_failure_locked("account", username, now)
                if source_key:
                    self._bump_failure_locked("source", source_key, now)
                self._bump_failure_locked("global", GLOBAL_KEY, now)
                self._evict_stale_failures_locked(now)
                log.warning("login failed", extra={"status": "locked" if blocked else "denied"})
                return None

            self._clear_failure_locked("account", username)
            if _needs_rehash(row["pw_hash"]):
                self.db.execute("UPDATE users SET pw_hash=? WHERE id=?", (hash_password(password), row["id"]))
            token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            self.db.execute("INSERT INTO sessions VALUES (?,?,?,?,?)", (_digest(token), row["id"], csrf, now, now))
            log.info("login", extra={"user_id": row["id"]})
            return token, csrf, row["id"]

    def session(self, token):
        if not token:
            return None
        now = time.time()
        rows = self.q(
            """SELECT s.user_id, s.csrf, s.created_at, s.seen_at, u.username FROM sessions s
               JOIN users u ON u.id = s.user_id WHERE s.token_hash=? AND u.disabled=0""",
            (_digest(token),),
        )
        if not rows:
            return None
        s = rows[0]
        if now - s["seen_at"] > IDLE_TIMEOUT or now - s["created_at"] > ABSOLUTE_TIMEOUT:
            self.logout(token)
            return None
        if now - s["seen_at"] > 60:
            self.x("UPDATE sessions SET seen_at=? WHERE token_hash=?", (now, _digest(token)))
        return {"user_id": s["user_id"], "username": s["username"], "csrf": s["csrf"]}

    def logout(self, token):
        self.x("DELETE FROM sessions WHERE token_hash=?", (_digest(token or ""),))

    # ------------------------------------------------------------ encrypted secrets
    def put_secret(self, user_id, name, value):
        assert name in SECRET_NAMES
        self.x(
            "INSERT OR REPLACE INTO secrets VALUES (?,?,?,?)",
            (user_id, name, self.fernet.encrypt(value.encode()), time.time()),
        )

    def get_secret(self, user_id, name):
        rows = self.q("SELECT value FROM secrets WHERE user_id=? AND name=?", (user_id, name))
        return self.fernet.decrypt(rows[0]["value"]).decode() if rows else None

    def delete_secret(self, user_id, name):
        self.x("DELETE FROM secrets WHERE user_id=? AND name=?", (user_id, name))
