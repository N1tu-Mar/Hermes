"""SQLite: internal cache/index plus global cross-campaign memory.

Never a substitute for the campaign JSON files. Migration 1 is the cache:
page cache (by URL), research cache (by candidate + criteria hash), drafts
keyed by (campaign_id, candidate_id, template_version), jobs for resumable
progress, usage counters, and recent events for the progress feed. Migration 2
is the global ledger (people, links, reviews, interactions, identities,
campaign metadata; see ledger.py). Schema changes are appended to MIGRATIONS
and tracked with PRAGMA user_version; never edit an applied migration.
"""
import json
import sqlite3
import threading
import time

SCHEMA_V1 = """
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
"""

SCHEMA_V2 = """
CREATE TABLE people (
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, organization TEXT, role TEXT,
  email TEXT, profile_url TEXT, name_key TEXT NOT NULL,
  notes TEXT NOT NULL DEFAULT '', tags TEXT NOT NULL DEFAULT '[]',
  relationship TEXT NOT NULL DEFAULT 'new', do_not_contact INTEGER NOT NULL DEFAULT 0, dnc_reason TEXT,
  last_contacted_at REAL, owner TEXT, source TEXT, created_at REAL, updated_at REAL);
CREATE UNIQUE INDEX people_email ON people(email) WHERE email IS NOT NULL;
CREATE INDEX people_url ON people(profile_url);
CREATE INDEX people_name_key ON people(name_key);
CREATE TABLE person_links (
  campaign_id TEXT, candidate_id TEXT, contact_id INTEGER NOT NULL REFERENCES people(id),
  linked_at REAL, PRIMARY KEY (campaign_id, candidate_id));
CREATE INDEX person_links_contact ON person_links(contact_id);
CREATE TABLE person_reviews (
  id INTEGER PRIMARY KEY AUTOINCREMENT, campaign_id TEXT, candidate_id TEXT,
  person TEXT NOT NULL, options TEXT NOT NULL, reason TEXT, status TEXT NOT NULL DEFAULT 'open',
  resolved_contact_id INTEGER, created_at REAL, resolved_at REAL);
CREATE TABLE interactions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, contact_id INTEGER NOT NULL REFERENCES people(id),
  campaign_id TEXT, candidate_id TEXT, kind TEXT NOT NULL, detail TEXT, meta TEXT, at REAL NOT NULL);
CREATE INDEX interactions_contact ON interactions(contact_id, at);
CREATE TABLE identities (
  id INTEGER PRIMARY KEY AUTOINCREMENT, display_name TEXT NOT NULL, biography TEXT NOT NULL,
  organization TEXT, role TEXT, signature TEXT, links TEXT NOT NULL DEFAULT '[]', default_ask TEXT,
  reply_to TEXT, created_at REAL, updated_at REAL);
CREATE TABLE campaign_meta (
  campaign_id TEXT PRIMARY KEY, name TEXT, archived INTEGER NOT NULL DEFAULT 0, updated_at REAL);
"""

# Append only; entry i is applied when PRAGMA user_version <= i.
# V1 keeps IF NOT EXISTS because databases created before versioning already have those tables.
MIGRATIONS = [SCHEMA_V1, SCHEMA_V2]


def migrate(db):
    version = db.execute("PRAGMA user_version").fetchone()[0]
    if version > len(MIGRATIONS):
        raise RuntimeError(f"database schema v{version} is newer than this app (v{len(MIGRATIONS)})")
    for i in range(version, len(MIGRATIONS)):
        try:
            db.executescript(f"BEGIN;\n{MIGRATIONS[i]}\nPRAGMA user_version={i + 1};\nCOMMIT;")
        except Exception:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise


PAGE_TTL = 7 * 86400
PAGE_ERROR_TTL = 3600
RESEARCH_TTL = 14 * 86400


class Cache:
    # ponytail: one connection + one lock; fine for one local process, pool if contention shows up.
    def __init__(self, path):
        self.db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        migrate(self.db)
        self.lock = threading.Lock()

    def q(self, sql, args=()):
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    def x(self, sql, args=()):
        with self.lock:
            self.db.execute(sql, args)

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
