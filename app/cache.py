"""SQLite internal cache/index. Never a substitute for the campaign JSON files.

Tables: page cache (by URL), research cache (by candidate + criteria hash),
drafts keyed by (campaign_id, candidate_id, template_version), jobs for
resumable progress, usage counters, and recent events for the progress feed.
Outreach tracking: contacts (Gmail thread/message IDs, outcome, sequence state),
followups keyed by (campaign, candidate, step) so a step can exist only once,
gmail_seen (compact metadata of replies/bounces in HERMES threads only), and
contact_log (per-contact timeline + audit of outcome changes).
"""
import json
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS pages (
  url TEXT PRIMARY KEY, fetched_at REAL, ok INTEGER, text TEXT, error TEXT);
CREATE TABLE IF NOT EXISTS research_cache (
  key TEXT PRIMARY KEY, created_at REAL, profile TEXT);
CREATE TABLE IF NOT EXISTS drafts (
  campaign_id TEXT, candidate_id TEXT, template_version TEXT,
  input_hash TEXT, subject TEXT, body TEXT, evidence_ids TEXT, outline TEXT,
  status TEXT, issues TEXT, gmail_draft_id TEXT, gmail_attempt_at REAL,
  invited_at REAL, updated_at REAL,
  PRIMARY KEY (campaign_id, candidate_id, template_version));
CREATE TABLE IF NOT EXISTS jobs (
  job_id TEXT PRIMARY KEY, campaign_id TEXT, kind TEXT, candidate_id TEXT,
  status TEXT, error TEXT, created_at REAL, updated_at REAL);
CREATE TABLE IF NOT EXISTS usage (
  campaign_id TEXT PRIMARY KEY, api_calls INTEGER DEFAULT 0, cache_hits INTEGER DEFAULT 0,
  input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0, budget INTEGER DEFAULT 60);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, campaign_id TEXT, at REAL, message TEXT);
CREATE TABLE IF NOT EXISTS contacts (
  campaign_id TEXT, candidate_id TEXT, gmail_thread_id TEXT, gmail_message_id TEXT, rfc_message_id TEXT,
  subject TEXT, sent_at REAL, sent_source TEXT, outcome TEXT, outcome_at REAL, sequence TEXT,
  sequence_state TEXT DEFAULT 'active', do_not_contact INTEGER DEFAULT 0, last_synced_at REAL, sync_error TEXT,
  PRIMARY KEY (campaign_id, candidate_id));
CREATE TABLE IF NOT EXISTS followups (
  campaign_id TEXT, candidate_id TEXT, step INTEGER, due_at REAL, status TEXT, attempts INTEGER DEFAULT 0,
  subject TEXT, body TEXT, issues TEXT, outline TEXT, evidence_ids TEXT, gmail_draft_id TEXT, gmail_attempt_at REAL,
  updated_at REAL, PRIMARY KEY (campaign_id, candidate_id, step));
CREATE TABLE IF NOT EXISTS gmail_seen (
  message_id TEXT PRIMARY KEY, thread_id TEXT, campaign_id TEXT, candidate_id TEXT, kind TEXT, from_addr TEXT, at REAL);
CREATE TABLE IF NOT EXISTS contact_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT, campaign_id TEXT, candidate_id TEXT, at REAL, kind TEXT, detail TEXT, source TEXT);
"""

PAGE_TTL = 7 * 86400
PAGE_ERROR_TTL = 3600
RESEARCH_TTL = 14 * 86400


class Cache:
    # ponytail: one connection + one lock; fine for one local process, pool if contention shows up.
    def __init__(self, path):
        self.db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self.lock = threading.Lock()

    def q(self, sql, args=()):
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    def x(self, sql, args=()):
        """Execute; returns affected row count (used for compare-and-set claims)."""
        with self.lock:
            return self.db.execute(sql, args).rowcount

    def upsert(self, table, keys, **fields):
        """Insert the key row if missing, then set fields. Table/column names are code constants, never input."""
        with self.lock:
            self.db.execute(f"INSERT OR IGNORE INTO {table} ({', '.join(keys)}) VALUES ({', '.join('?' * len(keys))})",
                            tuple(keys.values()))
            if fields:
                self.db.execute(f"UPDATE {table} SET {', '.join(f'{k}=?' for k in fields)} WHERE "
                                + " AND ".join(f"{k}=?" for k in keys), (*fields.values(), *keys.values()))

    # pages -------------------------------------------------------------
    def get_page(self, url):
        rows = self.q("SELECT * FROM pages WHERE url=?", (url,))
        if not rows:
            return None
        r = rows[0]
        ttl = PAGE_TTL if r["ok"] else PAGE_ERROR_TTL
        return r if time.time() - r["fetched_at"] < ttl else None

    def put_page(self, url, text=None, error=None):
        self.x("INSERT OR REPLACE INTO pages VALUES (?,?,?,?,?)",
               (url, time.time(), int(error is None), text, error))

    # research ----------------------------------------------------------
    def get_research(self, key):
        rows = self.q("SELECT * FROM research_cache WHERE key=?", (key,))
        if rows and time.time() - rows[0]["created_at"] < RESEARCH_TTL:
            return json.loads(rows[0]["profile"])
        return None

    def put_research(self, key, profile):
        self.x("INSERT OR REPLACE INTO research_cache VALUES (?,?,?)", (key, time.time(), json.dumps(profile)))

    def drop_research(self, prefix):
        self.x("DELETE FROM research_cache WHERE key LIKE ?", (prefix + "%",))

    # drafts ------------------------------------------------------------
    def get_draft(self, campaign_id, candidate_id, template_version=None):
        sql = "SELECT * FROM drafts WHERE campaign_id=? AND candidate_id=?"
        args = [campaign_id, candidate_id]
        if template_version:
            sql += " AND template_version=?"
            args.append(template_version)
        rows = self.q(sql + " ORDER BY updated_at DESC LIMIT 1", args)
        return _draft(rows[0]) if rows else None

    def list_drafts(self, campaign_id):
        return [_draft(r) for r in self.q("SELECT * FROM drafts WHERE campaign_id=?", (campaign_id,))]

    def upsert_draft(self, campaign_id, candidate_id, template_version, **fields):
        fields["updated_at"] = time.time()
        for k in ("evidence_ids", "outline", "issues"):
            if k in fields and not isinstance(fields[k], str):
                fields[k] = json.dumps(fields[k])
        with self.lock:
            self.db.execute("INSERT OR IGNORE INTO drafts (campaign_id, candidate_id, template_version) VALUES (?,?,?)",
                            (campaign_id, candidate_id, template_version))
            sets = ", ".join(f"{k}=?" for k in fields)
            self.db.execute(f"UPDATE drafts SET {sets} WHERE campaign_id=? AND candidate_id=? AND template_version=?",
                            (*fields.values(), campaign_id, candidate_id, template_version))

    # jobs --------------------------------------------------------------
    def put_job(self, job_id, campaign_id, kind, candidate_id, status, error=None):
        now = time.time()
        self.x("""INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?)
                  ON CONFLICT(job_id) DO UPDATE SET status=excluded.status, error=excluded.error, updated_at=excluded.updated_at""",
               (job_id, campaign_id, kind, candidate_id, status, error, now, now))

    def jobs(self, campaign_id=None, status=None):
        sql, args = "SELECT * FROM jobs WHERE 1=1", []
        if campaign_id:
            sql += " AND campaign_id=?"; args.append(campaign_id)
        if status:
            sql += " AND status=?"; args.append(status)
        return self.q(sql + " ORDER BY created_at", args)

    def mark_interrupted(self):
        """On restart, running/queued work becomes resumable."""
        self.x("UPDATE jobs SET status='interrupted' WHERE status IN ('queued','running')")

    # usage / events ----------------------------------------------------
    def usage(self, campaign_id):
        self.x("INSERT OR IGNORE INTO usage (campaign_id) VALUES (?)", (campaign_id,))
        return self.q("SELECT * FROM usage WHERE campaign_id=?", (campaign_id,))[0]

    def bump_usage(self, campaign_id, **inc):
        self.usage(campaign_id)
        sets = ", ".join(f"{k}={k}+?" for k in inc)
        self.x(f"UPDATE usage SET {sets} WHERE campaign_id=?", (*inc.values(), campaign_id))

    def set_budget(self, campaign_id, budget):
        self.usage(campaign_id)
        self.x("UPDATE usage SET budget=? WHERE campaign_id=?", (int(budget), campaign_id))

    def event(self, campaign_id, message):
        self.x("INSERT INTO events (campaign_id, at, message) VALUES (?,?,?)", (campaign_id, time.time(), message))

    def events(self, campaign_id, limit=15):
        return self.q("SELECT at, message FROM events WHERE campaign_id=? ORDER BY id DESC LIMIT ?", (campaign_id, limit))


def _draft(r):
    for k in ("evidence_ids", "outline", "issues"):
        r[k] = json.loads(r[k]) if r.get(k) else ([] if k != "outline" else None)
    return r
