"""SQLite: internal cache/index plus global cross-campaign memory.

Never a substitute for the campaign JSON files. Migration 1 is the cache:
page cache (by URL), research cache (by candidate + criteria hash), drafts
keyed by (campaign_id, candidate_id, template_version), jobs for resumable
progress, usage counters, and recent events for the progress feed. Migration 2
is the global ledger (people, links, reviews, interactions, identities,
campaign metadata; see ledger.py). Schema changes are appended to MIGRATIONS
and tracked with PRAGMA user_version; never edit an applied migration. It also
stores reusable messaging assets, analytics, corrections, and policy rules.
Outreach tracking adds Gmail thread metadata, idempotent follow-up steps, and
an audit log without reading or storing message bodies.
"""

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from .migrations import migrate_sqlite
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
CREATE TABLE IF NOT EXISTS outreach_contacts (
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
USAGE_LOGGED = ("api_calls", "cache_hits", "input_tokens", "output_tokens")

SCHEMA_V2 = """
CREATE TABLE IF NOT EXISTS people (
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, organization TEXT, role TEXT,
  email TEXT, profile_url TEXT, name_key TEXT NOT NULL,
  notes TEXT NOT NULL DEFAULT '', tags TEXT NOT NULL DEFAULT '[]',
  relationship TEXT NOT NULL DEFAULT 'new', do_not_contact INTEGER NOT NULL DEFAULT 0, dnc_reason TEXT,
  last_contacted_at REAL, owner TEXT, source TEXT, created_at REAL, updated_at REAL);
CREATE UNIQUE INDEX IF NOT EXISTS people_email ON people(email) WHERE email IS NOT NULL;
CREATE INDEX IF NOT EXISTS people_url ON people(profile_url);
CREATE INDEX IF NOT EXISTS people_name_key ON people(name_key);
CREATE TABLE IF NOT EXISTS person_links (
  campaign_id TEXT, candidate_id TEXT, contact_id INTEGER NOT NULL REFERENCES people(id),
  linked_at REAL, PRIMARY KEY (campaign_id, candidate_id));
CREATE INDEX IF NOT EXISTS person_links_contact ON person_links(contact_id);
CREATE TABLE IF NOT EXISTS person_reviews (
  id INTEGER PRIMARY KEY AUTOINCREMENT, campaign_id TEXT, candidate_id TEXT,
  person TEXT NOT NULL, options TEXT NOT NULL, reason TEXT, status TEXT NOT NULL DEFAULT 'open',
  resolved_contact_id INTEGER, created_at REAL, resolved_at REAL);
CREATE TABLE IF NOT EXISTS interactions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, contact_id INTEGER NOT NULL REFERENCES people(id),
  campaign_id TEXT, candidate_id TEXT, kind TEXT NOT NULL, detail TEXT, meta TEXT, at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS interactions_contact ON interactions(contact_id, at);
CREATE TABLE IF NOT EXISTS identities (
  id INTEGER PRIMARY KEY AUTOINCREMENT, display_name TEXT NOT NULL, biography TEXT NOT NULL,
  organization TEXT, role TEXT, signature TEXT, links TEXT NOT NULL DEFAULT '[]', default_ask TEXT,
  reply_to TEXT, created_at REAL, updated_at REAL);
CREATE TABLE IF NOT EXISTS campaign_meta (
  campaign_id TEXT PRIMARY KEY, name TEXT, archived INTEGER NOT NULL DEFAULT 0, updated_at REAL);
"""

def _migrate_v3(db):
    """Make people/person_links canonical.

    V1's `contacts`/`contact_links` predate the ledger; V2 meant to point
    `interactions` at people(id) but its CREATE TABLE IF NOT EXISTS was a
    no-op (V1 had already created it, FK'd to contacts(id)), so interactions
    written before this migration may carry either id space. This step:
    merges every contacts row into people (matching by verified email, then
    profile URL, then name_key; ambiguous matches open a person_review
    instead of guessing), explicitly remapping each contacts.id to its
    target people.id so a collision between the two id spaces can never mix
    timelines; rebuilds interactions with the correct FK, remapping rows
    whose contact_id was never a valid people.id; carries contact_links over
    to person_links (an existing person_links row for the same campaign
    candidate wins); and empties (not drops, so app/ops.py's `forget` can
    keep querying both tables) the legacy contacts/contact_links tables.
    Also folds in the column additions that used to run as unversioned
    ALTERs on every startup.
    """
    from .ledger import RELATIONSHIPS, canonical_url, name_key, norm_email

    def find_target(email, url, nk):
        if email:
            row = db.execute("SELECT id FROM people WHERE email=?", (email,)).fetchone()
            if row:
                return row["id"], []
        by_url = [r["id"] for r in db.execute("SELECT id FROM people WHERE profile_url=?", (url,))] if url else []
        if len(by_url) == 1:
            return by_url[0], []
        by_name = [r["id"] for r in db.execute("SELECT id FROM people WHERE name_key=?", (nk,))]
        if len(by_name) == 1 and not by_url:
            return by_name[0], []
        return None, sorted(set(by_url) | set(by_name))

    now = time.time()
    remap = {}
    for c in [dict(r) for r in db.execute("SELECT * FROM contacts ORDER BY id")]:
        email, url, nk = norm_email(c["email"]), canonical_url(c["profile_url"]), name_key(c["name"], c["organization"])
        target, ambiguous = find_target(email, url, nk)
        if target is None:
            cols = ("name", "organization", "role", "email", "profile_url", "name_key", "notes", "tags",
                    "relationship", "do_not_contact", "dnc_reason", "last_contacted_at", "owner", "source",
                    "created_at", "updated_at")
            values = (c["name"], c["organization"], c["role"], email, url, nk, c["notes"] or "",
                      c["tags"] or "[]", c["relationship"] or "new", c["do_not_contact"] or 0, c["dnc_reason"],
                      c["last_contacted_at"], c["owner"], c["source"], c["created_at"] or now, now)
            target = db.execute(f"INSERT INTO people ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                                values).lastrowid
            if ambiguous:
                person = {"name": c["name"], "organization": c["organization"], "role": c["role"],
                          "email": email, "profile_url": url}
                db.execute("INSERT INTO person_reviews (campaign_id, candidate_id, person, options, reason, "
                          "created_at) VALUES (NULL, NULL, ?, ?, ?, ?)",
                          (json.dumps(person, sort_keys=True), json.dumps(sorted({*ambiguous, target})),
                           "legacy contact migration found more than one possible match", now))
        else:
            row = dict(db.execute("SELECT * FROM people WHERE id=?", (target,)).fetchone())
            patch = {}
            for field, value in (("email", email), ("profile_url", url), ("role", c["role"]),
                                 ("owner", c["owner"]), ("source", c["source"])):
                if value and not row[field]:
                    patch[field] = value
            if c["notes"] and c["notes"] not in (row["notes"] or ""):
                patch["notes"] = ((row["notes"] or "") + "\n" + c["notes"]).strip()
            row_tags, c_tags = json.loads(row["tags"] or "[]"), json.loads(c["tags"] or "[]")
            merged_tags = sorted(set(row_tags) | set(c_tags))
            if merged_tags != sorted(row_tags):
                patch["tags"] = json.dumps(merged_tags)
            if c["do_not_contact"] and not row["do_not_contact"]:
                patch["do_not_contact"], patch["dnc_reason"] = 1, row["dnc_reason"] or c["dnc_reason"]
            if (c["last_contacted_at"] or 0) > (row["last_contacted_at"] or 0):
                patch["last_contacted_at"] = c["last_contacted_at"]
            c_rank = RELATIONSHIPS.index(c["relationship"]) if c["relationship"] in RELATIONSHIPS else 0
            row_rank = RELATIONSHIPS.index(row["relationship"]) if row["relationship"] in RELATIONSHIPS else 0
            if c_rank > row_rank:
                patch["relationship"] = c["relationship"]
            if patch:
                patch["updated_at"] = now
                db.execute(f"UPDATE people SET {', '.join(k + '=?' for k in patch)} WHERE id=?",
                          (*patch.values(), target))
        remap[c["id"]] = target

    # A contact_id already equal to a valid people.id is not proof it was written against people:
    # contacts.id and people.id are independent sequences, so a legacy id can coincidentally collide
    # with an unrelated people.id. A campaign-scoped row is trusted as already-canonical only when
    # person_links (populated solely by the ledger, never by this migration) confirms that exact id for
    # that campaign/candidate; a global row (no campaign/candidate) can only exist via the contacts API,
    # which requires an id already valid in people, so a colliding id there is always already correct.
    people_ids = {r["id"] for r in db.execute("SELECT id FROM people")}
    canonical_link = {(r["campaign_id"], r["candidate_id"]): r["contact_id"]
                       for r in db.execute("SELECT campaign_id, candidate_id, contact_id FROM person_links")}
    for row in [dict(r) for r in db.execute("SELECT * FROM interactions")]:
        old_cid = row["contact_id"]
        if old_cid not in remap:
            continue
        if old_cid not in people_ids:
            needs_remap = True
        elif row["campaign_id"] is not None:
            needs_remap = canonical_link.get((row["campaign_id"], row["candidate_id"])) != old_cid
        else:
            needs_remap = False
        if needs_remap:
            db.execute("UPDATE interactions SET contact_id=? WHERE id=?", (remap[old_cid], row["id"]))
    db.execute("""CREATE TABLE interactions_v3 (
      id INTEGER PRIMARY KEY AUTOINCREMENT, contact_id INTEGER NOT NULL REFERENCES people(id),
      campaign_id TEXT, candidate_id TEXT, kind TEXT NOT NULL, detail TEXT, meta TEXT, at REAL NOT NULL)""")
    db.execute("""INSERT INTO interactions_v3 (id, contact_id, campaign_id, candidate_id, kind, detail, meta, at)
                 SELECT id, contact_id, campaign_id, candidate_id, kind, detail, meta, at FROM interactions
                 WHERE contact_id IN (SELECT id FROM people)""")
    db.execute("DROP TABLE interactions")
    db.execute("ALTER TABLE interactions_v3 RENAME TO interactions")
    db.execute("CREATE INDEX IF NOT EXISTS interactions_contact ON interactions(contact_id, at)")

    for row in [dict(r) for r in db.execute("SELECT * FROM contact_links")]:
        new_id = remap.get(row["contact_id"])
        if new_id is not None:
            db.execute("INSERT OR IGNORE INTO person_links VALUES (?,?,?,?)",
                      (row["campaign_id"], row["candidate_id"], new_id, row["linked_at"]))
    db.execute("DELETE FROM contact_links")
    db.execute("DELETE FROM contacts")

    # SCHEMA_V1's CREATE TABLE IF NOT EXISTS already has these columns for brand-new databases;
    # only a database created before that column existed in the string still needs the ALTER.
    usage_cols = {r[1] for r in db.execute("PRAGMA table_info(usage)")}
    for name in ("pages_fetched", "bytes_fetched", "pages_skipped"):
        if name not in usage_cols:
            db.execute(f"ALTER TABLE usage ADD COLUMN {name} INTEGER DEFAULT 0")
    draft_cols = {r[1] for r in db.execute("PRAGMA table_info(drafts)")}
    for name, kind in (("attachment_ids", "TEXT"), ("content_ids", "TEXT"),
                       ("template_id", "TEXT"), ("template_number", "INTEGER")):
        if name not in draft_cols:
            db.execute(f"ALTER TABLE drafts ADD COLUMN {name} {kind}")
    if "started_at" not in {r[1] for r in db.execute("PRAGMA table_info(jobs)")}:
        db.execute("ALTER TABLE jobs ADD COLUMN started_at REAL")


# Append only; entry i is applied when PRAGMA user_version <= i.
# V1 keeps IF NOT EXISTS because databases created before versioning already have those tables.
# V3 is a callable (see migrate_sqlite): the contacts->people merge needs matching logic, not just SQL.
MIGRATIONS = [SCHEMA_V1, SCHEMA_V2, _migrate_v3]


PAGE_TTL = 7 * 86400
PAGE_ERROR_TTL = 3600
RESEARCH_TTL = 14 * 86400


class Cache:
    # ponytail: one connection + one lock; fine for one local process, pool if contention shows up.
    def __init__(self, path):
        self.db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA secure_delete=ON")
        self.path = path
        migrate_sqlite(self.db, path, MIGRATIONS)
        # Enforced only after migrating: older migration steps predate some FKs and must be free to
        # reshape tables (e.g. rebuilding interactions) without every intermediate state satisfying them.
        self.db.execute("PRAGMA foreign_keys=ON")
        violations = self.db.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise RuntimeError(f"{Path(path).name}: {len(violations)} foreign key violation(s) after migration")
        if str(path) != ":memory:":
            db_path = Path(path)
            db_path.parent.chmod(0o700)
            db_path.chmod(0o600)
        # Usage from before usage_log existed has no timestamp: kept as undated (at NULL), excluded by date filters.
        self.db.execute("""INSERT INTO usage_log (campaign_id, at, api_calls, cache_hits, input_tokens, output_tokens)
                           SELECT campaign_id, NULL, api_calls, cache_hits, input_tokens, output_tokens FROM usage
                           WHERE api_calls+cache_hits+input_tokens+output_tokens>0
                           AND campaign_id NOT IN (SELECT campaign_id FROM usage_log)""")
        self.lock = threading.Lock()

    @contextmanager
    def tx(self):
        with self.lock:
            self.db.execute("BEGIN")
            try:
                yield
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
            self.db.execute("COMMIT")

    def close(self):
        with self.lock:
            self.db.close()

    def ping(self):
        return self.q("SELECT 1 AS ok")[0]["ok"] == 1

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
        self.x("INSERT OR REPLACE INTO pages VALUES (?,?,?,?,?)", (url, time.time(), int(error is None), text, error))

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
            self.db.execute(
                "INSERT OR IGNORE INTO drafts (campaign_id, candidate_id, template_version) VALUES (?,?,?)",
                (campaign_id, candidate_id, template_version),
            )
            sets = ", ".join(f"{k}=?" for k in fields)
            self.db.execute(
                f"UPDATE drafts SET {sets} WHERE campaign_id=? AND candidate_id=? AND template_version=?",  # noqa: S608 keys are code-defined
                (*fields.values(), campaign_id, candidate_id, template_version),
            )

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
            sql += " AND campaign_id=?"
            args.append(campaign_id)
        if status:
            sql += " AND status=?"
            args.append(status)
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

    # deletion / retention ----------------------------------------------
    def delete_campaign(self, campaign_id):
        with self.tx():
            execution_ids = [r[0] for r in self.db.execute(
                "SELECT execution_id FROM rule_executions WHERE campaign_id=?", (campaign_id,)
            )]
            for execution_id in execution_ids:
                self.db.execute("DELETE FROM rule_actions WHERE execution_id=?", (execution_id,))
            send_ids = [r[0] for r in self.db.execute("SELECT send_id FROM sends WHERE campaign_id=?", (campaign_id,))]
            # send_audit is append-only during normal operation; explicit data deletion is the sole exception.
            self.db.execute("DROP TRIGGER IF EXISTS audit_no_delete")
            for send_id in send_ids:
                self.db.execute("DELETE FROM send_audit WHERE send_id=?", (send_id,))
            self.db.execute("""CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON send_audit
                               BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END""")
            for table in ("drafts", "jobs", "usage", "events", "corrections", "person_links", "contact_links",
                          "person_reviews", "milestones", "usage_log", "notifications", "outreach_contacts",
                          "followups", "gmail_seen", "contact_log", "campaign_assets",
                          "rule_executions", "campaign_rules", "campaign_meta", "sends"):
                self.db.execute(f"DELETE FROM {table} WHERE campaign_id=?", (campaign_id,))  # noqa: S608 fixed names
            self.db.execute("DELETE FROM research_cache WHERE key LIKE ?", (campaign_id + ":%",))

    def delete_candidate(self, campaign_id, candidate_id, name=None, urls=()):
        """Remove every row derived from one person. Events are free text, so match their id or name."""
        with self.tx():
            send_ids = [r[0] for r in self.db.execute(
                "SELECT send_id FROM sends WHERE campaign_id=? AND candidate_id=?", (campaign_id, candidate_id)
            )]
            self.db.execute("DROP TRIGGER IF EXISTS audit_no_delete")
            for send_id in send_ids:
                self.db.execute("DELETE FROM send_audit WHERE send_id=?", (send_id,))
            self.db.execute("""CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON send_audit
                               BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END""")
            for table in ("drafts", "jobs"):
                self.db.execute(
                    f"DELETE FROM {table} WHERE campaign_id=? AND candidate_id LIKE ?",  # noqa: S608
                    (campaign_id, candidate_id + "%"),
                )
            for table in ("corrections", "person_links", "contact_links", "person_reviews", "milestones",
                          "notifications", "outreach_contacts", "followups", "gmail_seen", "contact_log", "sends"):
                self.db.execute(
                    f"DELETE FROM {table} WHERE campaign_id=? AND candidate_id=?",  # noqa: S608 fixed names
                    (campaign_id, candidate_id),
                )
            self.db.execute(
                "DELETE FROM interactions WHERE campaign_id=? AND candidate_id=?", (campaign_id, candidate_id)
            )
            self.db.execute(
                "DELETE FROM rule_actions WHERE candidate_id=? AND execution_id IN "
                "(SELECT execution_id FROM rule_executions WHERE campaign_id=?)", (candidate_id, campaign_id)
            )
            self.db.execute("DELETE FROM research_cache WHERE key LIKE ?", (f"{campaign_id}:{candidate_id}:%",))
            self.db.execute(
                "DELETE FROM events WHERE campaign_id=? AND (message LIKE ? OR message LIKE ?)",
                (campaign_id, f"{candidate_id}:%", f"{name or candidate_id}:%"),
            )
            for url in urls:
                self.db.execute("DELETE FROM pages WHERE url=?", (url,))

    def purge(self, older_than_days):
        """Retention: drop expired caches, and events/finished jobs older than the window. Returns row counts."""
        now, cutoff = time.time(), time.time() - older_than_days * 86400
        stmts = {
            "pages": ("DELETE FROM pages WHERE fetched_at < ?", now - PAGE_TTL),
            "research_cache": ("DELETE FROM research_cache WHERE created_at < ?", now - RESEARCH_TTL),
            "events": ("DELETE FROM events WHERE at < ?", cutoff),
            "jobs": (
                "DELETE FROM jobs WHERE updated_at < ? AND status NOT IN ('queued','running','interrupted')",
                cutoff,
            ),
        }
        out = {}
        with self.tx():
            for name, (sql, arg) in stmts.items():
                out[name] = self.db.execute(sql, (arg,)).rowcount
        return out

    def event(self, campaign_id, message):
        self.x("INSERT INTO events (campaign_id, at, message) VALUES (?,?,?)", (campaign_id, time.time(), message))

    def events(self, campaign_id, limit=15):
        return self.q(
            "SELECT at, message FROM events WHERE campaign_id=? ORDER BY id DESC LIMIT ?", (campaign_id, limit)
        )

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
