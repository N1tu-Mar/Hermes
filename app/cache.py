"""SQLite cache/index plus durable reusable messaging workspace.

Tables: page cache (by URL), research cache (by candidate + criteria hash),
drafts keyed by (campaign_id, candidate_id, template_version), jobs for
resumable progress, usage counters, recent events, template versions, content,
attachment metadata, contact policy, and campaign-rule audit records.
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
  input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0, budget INTEGER DEFAULT 60,
  pages_fetched INTEGER DEFAULT 0, bytes_fetched INTEGER DEFAULT 0, pages_skipped INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, campaign_id TEXT, at REAL, message TEXT);
CREATE TABLE IF NOT EXISTS corrections (
  id INTEGER PRIMARY KEY AUTOINCREMENT, subject_key TEXT NOT NULL, aliases TEXT NOT NULL,
  campaign_id TEXT NOT NULL, candidate_id TEXT NOT NULL, field TEXT NOT NULL,
  original_value TEXT, corrected_value TEXT, provenance TEXT NOT NULL, created_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS corrections_subject ON corrections(subject_key, id);
CREATE TABLE IF NOT EXISTS identities (
  id INTEGER PRIMARY KEY AUTOINCREMENT, display_name TEXT NOT NULL, biography TEXT NOT NULL,
  organization TEXT, role TEXT, signature TEXT, links TEXT NOT NULL DEFAULT '[]', default_ask TEXT,
  reply_to TEXT, created_at REAL, updated_at REAL);
CREATE TABLE IF NOT EXISTS contacts (
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, organization TEXT, role TEXT,
  email TEXT, profile_url TEXT, name_key TEXT NOT NULL, notes TEXT NOT NULL DEFAULT '',
  tags TEXT NOT NULL DEFAULT '[]', relationship TEXT NOT NULL DEFAULT 'new',
  do_not_contact INTEGER NOT NULL DEFAULT 0, dnc_reason TEXT, last_contacted_at REAL,
  owner TEXT, source TEXT, created_at REAL, updated_at REAL);
CREATE UNIQUE INDEX IF NOT EXISTS contacts_email ON contacts(email) WHERE email IS NOT NULL;
CREATE TABLE IF NOT EXISTS contact_links (
  campaign_id TEXT, candidate_id TEXT, contact_id INTEGER NOT NULL REFERENCES contacts(id),
  linked_at REAL, PRIMARY KEY(campaign_id,candidate_id));
CREATE TABLE IF NOT EXISTS interactions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, contact_id INTEGER NOT NULL REFERENCES contacts(id),
  campaign_id TEXT, candidate_id TEXT, kind TEXT NOT NULL, detail TEXT, meta TEXT, at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS template_definitions (
  template_id TEXT PRIMARY KEY, name TEXT NOT NULL, category TEXT NOT NULL,
  archived INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS template_versions (
  template_id TEXT NOT NULL REFERENCES template_definitions(template_id), version INTEGER NOT NULL,
  subject TEXT NOT NULL, body TEXT NOT NULL, variables TEXT NOT NULL DEFAULT '[]',
  change_note TEXT, created_at REAL NOT NULL, PRIMARY KEY(template_id,version));
CREATE TABLE IF NOT EXISTS reusable_content (
  content_id TEXT PRIMARY KEY, identity_id INTEGER REFERENCES identities(id), kind TEXT NOT NULL,
  name TEXT NOT NULL, body TEXT NOT NULL, archived INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS attachments (
  attachment_id TEXT PRIMARY KEY, identity_id INTEGER REFERENCES identities(id), kind TEXT NOT NULL,
  display_name TEXT NOT NULL, stored_name TEXT NOT NULL, media_type TEXT NOT NULL,
  size INTEGER NOT NULL, sha256 TEXT NOT NULL, archived INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS campaign_assets (
  campaign_id TEXT NOT NULL, asset_type TEXT NOT NULL, asset_id TEXT NOT NULL,
  position INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(campaign_id,asset_type,asset_id));
CREATE TABLE IF NOT EXISTS campaign_rules (
  rule_id TEXT PRIMARY KEY, campaign_id TEXT NOT NULL, kind TEXT NOT NULL, config TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1, created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS rule_executions (
  execution_id TEXT PRIMARY KEY, rule_id TEXT NOT NULL REFERENCES campaign_rules(rule_id),
  campaign_id TEXT NOT NULL, dry_run INTEGER NOT NULL, created_at REAL NOT NULL, summary TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS rule_actions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, execution_id TEXT NOT NULL REFERENCES rule_executions(execution_id),
  candidate_id TEXT NOT NULL, action TEXT NOT NULL, status TEXT NOT NULL, reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS milestones (
  campaign_id TEXT NOT NULL, candidate_id TEXT NOT NULL, stage TEXT NOT NULL, at REAL,
  PRIMARY KEY(campaign_id,candidate_id,stage));
CREATE TABLE IF NOT EXISTS usage_log (
  campaign_id TEXT NOT NULL, at REAL, api_calls INTEGER DEFAULT 0, cache_hits INTEGER DEFAULT 0,
  input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS usage_log_campaign ON usage_log(campaign_id, at);
CREATE TABLE IF NOT EXISTS notifications (
  id INTEGER PRIMARY KEY AUTOINCREMENT, dedupe_key TEXT NOT NULL UNIQUE, kind TEXT NOT NULL,
  message TEXT NOT NULL, campaign_id TEXT, candidate_id TEXT, created_at REAL NOT NULL,
  read_at REAL, dismissed_at REAL);
"""
USAGE_LOGGED = ("api_calls", "cache_hits", "input_tokens", "output_tokens")

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
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(usage)")}
        for name in ("pages_fetched", "bytes_fetched", "pages_skipped"):
            if name not in cols:
                self.db.execute(f"ALTER TABLE usage ADD COLUMN {name} INTEGER DEFAULT 0")
        draft_cols = {r[1] for r in self.db.execute("PRAGMA table_info(drafts)")}
        for name, kind in (("attachment_ids", "TEXT"), ("content_ids", "TEXT"),
                           ("template_id", "TEXT"), ("template_number", "INTEGER")):
            if name not in draft_cols:
                self.db.execute(f"ALTER TABLE drafts ADD COLUMN {name} {kind}")
        if "started_at" not in {r[1] for r in self.db.execute("PRAGMA table_info(jobs)")}:
            self.db.execute("ALTER TABLE jobs ADD COLUMN started_at REAL")
        # Usage from before usage_log existed has no timestamp: kept as undated (at NULL), excluded by date filters.
        self.db.execute("""INSERT INTO usage_log (campaign_id, at, api_calls, cache_hits, input_tokens, output_tokens)
                           SELECT campaign_id, NULL, api_calls, cache_hits, input_tokens, output_tokens FROM usage
                           WHERE api_calls+cache_hits+input_tokens+output_tokens>0
                           AND campaign_id NOT IN (SELECT campaign_id FROM usage_log)""")
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
        for k in ("evidence_ids", "outline", "issues", "attachment_ids", "content_ids"):
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
        self.x("""INSERT INTO jobs (job_id, campaign_id, kind, candidate_id, status, error, created_at, updated_at)
                  VALUES (?,?,?,?,?,?,?,?)
                  ON CONFLICT(job_id) DO UPDATE SET status=excluded.status, error=excluded.error, updated_at=excluded.updated_at,
                  started_at=CASE WHEN excluded.status='running' THEN excluded.updated_at ELSE started_at END""",
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
        logged = {k: v for k, v in inc.items() if k in USAGE_LOGGED}
        if logged:  # timestamped copy so analytics can filter usage by date
            self.x(f"INSERT INTO usage_log (campaign_id, at, {', '.join(logged)}) VALUES (?,?{',?' * len(logged)})",
                   (campaign_id, time.time(), *logged.values()))

    def set_budget(self, campaign_id, budget):
        self.usage(campaign_id)
        self.x("UPDATE usage SET budget=? WHERE campaign_id=?", (int(budget), campaign_id))

    def event(self, campaign_id, message):
        self.x("INSERT INTO events (campaign_id, at, message) VALUES (?,?,?)", (campaign_id, time.time(), message))

    def events(self, campaign_id, limit=15):
        return self.q("SELECT at, message FROM events WHERE campaign_id=? ORDER BY id DESC LIMIT ?", (campaign_id, limit))

    # corrections ------------------------------------------------------
    def record_correction(self, subject_key, aliases, campaign_id, candidate_id, field,
                          original_value, corrected_value):
        with self.lock:
            cur = self.db.execute("""INSERT INTO corrections
                (subject_key,aliases,campaign_id,candidate_id,field,original_value,corrected_value,provenance,created_at)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                (subject_key, json.dumps(sorted(set(aliases))), campaign_id, candidate_id, field,
                 json.dumps(original_value), json.dumps(corrected_value), "manual_correction", time.time()))
            correction_id = cur.lastrowid
        return self.q("SELECT * FROM corrections WHERE id=?", (correction_id,))[0]

    def corrections(self):
        rows = self.q("SELECT * FROM corrections ORDER BY id")
        for row in rows:
            row["aliases"] = json.loads(row["aliases"] or "[]")
            row["original_value"] = json.loads(row["original_value"])
            row["corrected_value"] = json.loads(row["corrected_value"])
        return rows

    def corrections_for(self, aliases):
        aliases = set(aliases)
        return [r for r in self.corrections()
                if r["subject_key"] in aliases or aliases.intersection(r["aliases"])]


def _draft(r):
    for k in ("evidence_ids", "outline", "issues", "attachment_ids", "content_ids"):
        r[k] = json.loads(r[k]) if r.get(k) else ([] if k != "outline" else None)
    return r
