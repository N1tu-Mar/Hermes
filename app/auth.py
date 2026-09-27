"""Remote-mode accounts: users, sessions, CSRF tokens, encrypted per-user secrets.

Only used when HERMES_MODE=remote. Passwords use stdlib scrypt; session tokens
are random 256-bit values stored only as SHA-256 hashes; provider secrets
(OpenAI key, Gmail OAuth token) are Fernet-encrypted with HERMES_SECRET_KEY,
which never lives in the data directory or its backups. Users are created
from the CLI (`python -m app.ops user-add`); there is no self-signup.
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
"""
]

IDLE_TIMEOUT = 12 * 3600
ABSOLUTE_TIMEOUT = 7 * 86400
MAX_FAILURES, LOCKOUT = 5, 15 * 60
MIN_PASSWORD = 12
USERNAME_RE = re.compile(r"^[a-z0-9_.-]{3,32}$")
SECRET_NAMES = {"openai_api_key", "gmail_token"}


def hash_password(password):
    salt = os.urandom(16)
    h = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt$16384$8$1${salt.hex()}${h.hex()}"


def check_password(password, stored):
    try:
        _, n, r, p, salt, h = stored.split("$")
        got = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p), dklen=32)
    except ValueError:
        return False
    return secrets.compare_digest(got.hex(), h)


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
        self.failures = {}  # username -> (count, first_failure_at); ponytail: per-process, fine behind one worker

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

    # ------------------------------------------------------------ users (CLI)
    def add_user(self, username, password):
        if not USERNAME_RE.match(username or ""):
            raise ValueError("username must be 3-32 chars of a-z, 0-9, _ . -")
        if len(password or "") < MIN_PASSWORD:
            raise ValueError(f"password must be at least {MIN_PASSWORD} characters")
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
    def login(self, username, password):
        """Returns (session_token, csrf_token, user_id) or None. Throttles repeated failures per username."""
        now = time.time()
        count, first = self.failures.get(username, (0, now))
        if now - first > LOCKOUT:
            count, first = 0, now
        rows = self.q("SELECT id, pw_hash, disabled FROM users WHERE username=?", (username,))
        ok = check_password(password or "", rows[0]["pw_hash"] if rows else _DUMMY_HASH)
        if count >= MAX_FAILURES or not ok or not rows or rows[0]["disabled"]:
            self.failures[username] = (count + 1, first)
            log.warning("login failed", extra={"status": "locked" if count >= MAX_FAILURES else "denied"})
            return None
        self.failures.pop(username, None)
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        self.x("INSERT INTO sessions VALUES (?,?,?,?,?)", (_digest(token), rows[0]["id"], csrf, now, now))
        self.x("DELETE FROM sessions WHERE seen_at < ? OR created_at < ?", (now - IDLE_TIMEOUT, now - ABSOLUTE_TIMEOUT))
        log.info("login", extra={"user_id": rows[0]["id"]})
        return token, csrf, rows[0]["id"]

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
