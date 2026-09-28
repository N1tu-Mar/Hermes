"""Opt-in sending: approval snapshots, durable queue, gates, reconciliation, audit.

Draft-only is the default. A message leaves only when ALL hold: the global
switch is on, the campaign switch is on, sending is not paused or emergency
stopped, the Gmail token was granted gmail.send, a human confirmed that exact
message (hash of recipient, sender, subject, body, attachments, time), it is
outside quiet hours, and hourly/daily limits and spacing allow it.

Each send is draft -> drafts.send. After a timeout or crash we reconcile the
persisted Gmail draft-message/thread IDs: a SENT message is delivered, a
present draft is definitely unsent, and an absent draft with no SENT message is
deleted. Any inconclusive provider read remains uncertain and is never resent.

Everything lives in the app's SQLite file, so the queue survives restarts.
Approved content columns and the audit log are protected by triggers.
"""

import hashlib
import json
import logging
import re
import secrets
import sqlite3
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from .gmail import http_status

log = logging.getLogger("sending")

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS suppressions (email TEXT PRIMARY KEY, reason TEXT, at REAL);
CREATE TABLE IF NOT EXISTS sends (
  send_id TEXT PRIMARY KEY, idempotency_key TEXT UNIQUE, campaign_id TEXT, candidate_id TEXT,
  template_version TEXT, sender TEXT, reply_to TEXT, recipient TEXT, subject TEXT, body TEXT, attachments TEXT,
  scheduled_at REAL, approval TEXT, approval_hash TEXT, approved_at REAL,
  status TEXT, gmail_draft_id TEXT, gmail_draft_message_id TEXT, gmail_message_id TEXT,
  gmail_thread_id TEXT, rfc_message_id TEXT, attempts INTEGER DEFAULT 0,
  attempted_at REAL, finished_at REAL, delivery_applied_at REAL, error TEXT, updated_at REAL);
CREATE UNIQUE INDEX IF NOT EXISTS one_live_send_per_draft
  ON sends (campaign_id, candidate_id, template_version) WHERE status NOT IN ('cancelled', 'failed');
CREATE TRIGGER IF NOT EXISTS approved_content_immutable
  BEFORE UPDATE OF idempotency_key, campaign_id, candidate_id, template_version, sender, reply_to, recipient, subject, body,
                   attachments, scheduled_at, approval, approval_hash, approved_at ON sends
  BEGIN SELECT RAISE(ABORT, 'approved send content is immutable'); END;
CREATE TABLE IF NOT EXISTS send_audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT, send_id TEXT, at REAL, event TEXT, detail TEXT);
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON send_audit
  BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON send_audit
  BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END;
"""

DEFAULTS = {
    "enabled": False,
    "paused": False,
    "emergency_stop": False,
    "timezone": "America/New_York",
    "quiet_start": "20:00",
    "quiet_end": "08:00",
    "daily_limit": 20,
    "hourly_limit": 5,
    "spacing_seconds": 120,
}
STATES = (
    "scheduled",
    "sending",
    "sent",
    "cancelled",
    "failed",
    "uncertain",
    "deleted",
    "bounced",
    "replied",
    "declined",
    "do_not_contact",
)
SUPPRESS_REASONS = ("do_not_contact", "bounced", "declined")
EMAIL_RE = re.compile(
    r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)+$"
)
HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
MAX_ATTEMPTS = 3
MAX_LATE = 24 * 3600  # a message this overdue (e.g. sending was off for a day) needs a fresh approval


class Blocked(Exception):
    """Sending action refused in the current state (HTTP 409)."""


def payload_hash(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class Outbox:
    def __init__(self, cache, gmail=None, clock=time.time):
        self.cache, self.gmail, self.clock = cache, gmail, clock
        self.revalidate = lambda row: None  # set by CampaignService: reason the approved draft is stale, or None
        self.materialize = lambda row: []  # exact approved attachment bytes; also revalidates their hashes
        self.on_delivery = lambda row: None
        self.on_outcome = lambda row, outcome, at: None
        with cache.lock:
            cache.db.executescript(SCHEMA)
            columns = {r[1] for r in cache.db.execute("PRAGMA table_info(sends)")}
            for name in ("reply_to", "gmail_draft_message_id", "gmail_thread_id", "rfc_message_id"):
                if name not in columns:
                    cache.db.execute(f"ALTER TABLE sends ADD COLUMN {name} TEXT")
            if "delivery_applied_at" not in columns:
                cache.db.execute("ALTER TABLE sends ADD COLUMN delivery_applied_at REAL")
            # An older database already has the trigger with the old column set.
            cache.db.executescript("""
                DROP TRIGGER IF EXISTS approved_content_immutable;
                CREATE TRIGGER approved_content_immutable
                BEFORE UPDATE OF idempotency_key, campaign_id, candidate_id, template_version, sender, reply_to,
                                 recipient, subject, body, attachments, scheduled_at, approval, approval_hash, approved_at
                ON sends BEGIN SELECT RAISE(ABORT, 'approved send content is immutable'); END;
            """)

    # ------------------------------------------------------------ plumbing
    def _rowcount(self, sql, args=()):
        with self.cache.lock:
            return self.cache.db.execute(sql, args).rowcount

    def _update(self, send_id, **f):
        f["updated_at"] = self.clock()
        self.cache.x(f"UPDATE sends SET {', '.join(f'{k}=?' for k in f)} WHERE send_id=?", (*f.values(), send_id))

    def _transition(self, send_id, old, new, **fields):
        """Compare-and-set one legal state transition."""
        if new not in STATES:
            raise ValueError(f"unknown send state {new}")
        old = (old,) if isinstance(old, str) else tuple(old)
        fields.update(status=new, updated_at=self.clock())
        sql = f"UPDATE sends SET {', '.join(f'{k}=?' for k in fields)} WHERE send_id=? AND status IN ({','.join('?' * len(old))})"
        return self._rowcount(sql, (*fields.values(), send_id, *old))

    def audit(self, send_id, event, **detail):
        self.cache.x(
            "INSERT INTO send_audit (send_id, at, event, detail) VALUES (?,?,?,?)",
            (send_id, self.clock(), event, json.dumps(detail, default=str)),
        )

    def row(self, send_id):
        rows = self.cache.q("SELECT * FROM sends WHERE send_id=?", (send_id,))
        if not rows:
            raise KeyError(f"no send {send_id}")
        return rows[0]

    def _rows(self, where, args=()):
        return self.cache.q(f"SELECT * FROM sends WHERE {where} ORDER BY scheduled_at", args)

    # ------------------------------------------------------------ settings
    def settings(self):
        s = dict(DEFAULTS)
        s.update(
            {
                r["key"]: json.loads(r["value"])
                for r in self.cache.q("SELECT * FROM settings WHERE key NOT LIKE 'campaign:%'")
            }
        )
        return s

    def _put(self, key, value):
        self.cache.x("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, json.dumps(value)))

    def update_settings(self, patch):
        clean = {}
        for k, v in patch.items():
            if k in ("enabled", "paused"):
                clean[k] = bool(v)
            elif k == "timezone":
                try:
                    ZoneInfo(str(v))
                except (KeyError, ValueError):  # ZoneInfoNotFoundError is a KeyError, which would read as a 404
                    raise ValueError(f"unknown timezone {v}") from None
                clean[k] = str(v)
            elif k in ("quiet_start", "quiet_end"):
                if not HHMM.match(str(v)):
                    raise ValueError(f"{k} must be HH:MM (24h)")
                clean[k] = str(v)
            elif k in ("daily_limit", "hourly_limit"):
                clean[k] = max(0, min(int(v), 500))
            elif k == "spacing_seconds":
                clean[k] = max(0, min(int(v), 86400))
            else:
                raise ValueError(f"unknown setting {k}")
        if clean.get("enabled"):
            clean["emergency_stop"] = False  # turning sending back on is the explicit way out of an emergency stop
        for k, v in clean.items():
            self._put(k, v)
        self.audit(None, "settings_changed", **clean)
        return self.settings()

    def campaign_enabled(self, campaign_id):
        rows = self.cache.q("SELECT value FROM settings WHERE key=?", (f"campaign:{campaign_id}",))
        return bool(rows and json.loads(rows[0]["value"]))

    def set_campaign_enabled(self, campaign_id, enabled):
        self._put(f"campaign:{campaign_id}", bool(enabled))
        self.audit(None, "campaign_sending_" + ("enabled" if enabled else "disabled"), campaign_id=campaign_id)
        return {"campaign_id": campaign_id, "sending_enabled": bool(enabled)}

    def pause(self, paused=True):
        self._put("paused", paused)
        self.audit(None, "paused" if paused else "unpaused")
        return self.settings()

    def emergency_stop(self):
        """Turn sending off, pause, and cancel every scheduled message. Only re-approval brings one back."""
        for k in ("emergency_stop", "paused"):
            self._put(k, True)
        self._put("enabled", False)
        self.audit(None, "emergency_stop")
        cancelled = [r["send_id"] for r in self._rows("status='scheduled'")]
        for sid in cancelled:
            self._cancel(sid, "emergency stop")
        return {"cancelled": cancelled, **self.settings()}

    # ------------------------------------------------------------ time
    def tz(self):
        return ZoneInfo(self.settings()["timezone"])

    def local(self, ts):
        return datetime.fromtimestamp(ts, self.tz()).strftime("%Y-%m-%d %H:%M %Z")

    def parse_time(self, value):
        """ISO string -> epoch. A time without an offset is read in the configured timezone."""
        dt = datetime.fromisoformat(str(value).strip())
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=self.tz())
        return dt.timestamp()

    def quiet(self, ts):
        s = self.settings()
        t = datetime.fromtimestamp(ts, self.tz())
        m = t.hour * 60 + t.minute
        a, b = (int(x[:2]) * 60 + int(x[3:]) for x in (s["quiet_start"], s["quiet_end"]))
        return a <= m < b if a < b else (m >= a or m < b) if a > b else False

    # ------------------------------------------------------------ recipients
    def suppress(self, email, reason):
        if reason not in SUPPRESS_REASONS:
            raise ValueError(f"reason must be one of {SUPPRESS_REASONS}")
        email = (email or "").strip().lower()
        if not EMAIL_RE.match(email):
            raise ValueError("malformed address")
        self.cache.x("INSERT OR REPLACE INTO suppressions VALUES (?,?,?)", (email, reason, self.clock()))
        self.audit(None, "suppressed", email=email, reason=reason)
        for r in self._rows("status='scheduled' AND lower(recipient)=?", (email,)):
            self._cancel(r["send_id"], f"recipient on {reason} list")
        return {"email": email, "reason": reason}

    def suppressions(self):
        return self.cache.q("SELECT * FROM suppressions ORDER BY at DESC")

    def recipient_problem(self, email, verified):
        if not email:
            return "missing recipient address"
        if not EMAIL_RE.match(email):
            return "malformed recipient address"
        if not verified:
            return "recipient address not verified on a public page"
        hit = self.cache.q("SELECT reason FROM suppressions WHERE email=?", (email.lower(),))
        return f"recipient is on the {hit[0]['reason']} list" if hit else None

    # ------------------------------------------------------------ gates
    def can_send(self):
        return bool(self.gmail and getattr(self.gmail, "can_send", False))

    def sender_identity(self):
        try:
            return self.gmail.sender() if self.gmail else None
        except Exception as e:
            log.warning("Gmail profile lookup failed: %s", e)
            return None

    def blockers(self, campaign_id):
        """Hard reasons a new approval is refused right now. Pausing is not one: it only holds the queue."""
        s, out = self.settings(), []
        if s["emergency_stop"]:
            out.append("emergency stop is active; re-enable sending to clear it")
        elif not s["enabled"]:
            out.append("sending is disabled globally (draft-only mode)")
        if not self.campaign_enabled(campaign_id):
            out.append("sending is disabled for this campaign")
        if not self.gmail:
            out.append("Gmail is not connected")
        elif not self.can_send():
            out.append("Gmail token lacks gmail.send; run `python -m app.gmail --enable-sending`")
        return out

    def _halted(self):
        s = self.settings()
        return s["emergency_stop"] or s["paused"] or not s["enabled"]

    def _rate_block(self, now):
        s = self.settings()
        times = [
            r["attempted_at"]
            for r in self.cache.q("SELECT attempted_at FROM sends WHERE attempted_at >= ?", (now - 86400,))
        ]
        if len(times) >= s["daily_limit"]:
            return "daily limit reached"
        if sum(t >= now - 3600 for t in times) >= s["hourly_limit"]:
            return "hourly limit reached"
        if times and max(times) + s["spacing_seconds"] > now:
            return "spacing"
        return None

    # ------------------------------------------------------------ approval
    def approve(self, payload, approval_hash, recipient_verified, send_at=None):
        """Record a human's final approval of the exact payload they were shown and queue it."""
        if approval_hash != payload_hash(payload):
            raise Blocked("the message changed since the confirmation screen; review it again")
        problems = self.blockers(payload["campaign_id"])
        problem = self.recipient_problem(payload["recipient"], recipient_verified)
        if problem:
            problems.append(problem)
        if not payload["sender"]:
            problems.append("sender identity unknown")
        if problems:
            raise Blocked("; ".join(problems))
        now = self.clock()
        when = now if send_at is None else send_at
        if when < now - 60:
            raise Blocked("scheduled time is in the past")
        if self.quiet(when):
            s = self.settings()
            raise Blocked(
                f"{self.local(when)} is inside quiet hours ({s['quiet_start']}-{s['quiet_end']} {s['timezone']}); pick another time"
            )
        idem = hashlib.sha256(
            f"{payload['campaign_id']}:{payload['candidate_id']}:{approval_hash}".encode()
        ).hexdigest()[:32]
        existing = self.cache.q("SELECT send_id FROM sends WHERE idempotency_key=?", (idem,))
        if existing:  # same approval submitted twice (double click, client retry): one send
            return self.row(existing[0]["send_id"])
        sid = f"snd_{secrets.token_hex(6)}"
        try:
            self.cache.x(
                """INSERT INTO sends (send_id, idempotency_key, campaign_id, candidate_id, template_version, sender,
                            reply_to, recipient, subject, body, attachments, scheduled_at, approval, approval_hash,
                            approved_at, status, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'scheduled',?)""",
                (
                    sid,
                    idem,
                    payload["campaign_id"],
                    payload["candidate_id"],
                    payload["template_version"],
                    payload["sender"],
                    payload.get("reply_to"),
                    payload["recipient"],
                    payload["subject"],
                    payload["body"],
                    json.dumps(payload["attachments"]),
                    when,
                    json.dumps(payload, sort_keys=True),
                    approval_hash,
                    now,
                    now,
                ),
            )
        except sqlite3.IntegrityError:
            raise Blocked("this draft already has a scheduled or sent message; cancel it first") from None
        self.audit(sid, "approved", approval_hash=approval_hash, payload=payload)
        self.audit(sid, "scheduled", scheduled_at=when, local=self.local(when), send_now=send_at is None)
        return self.row(sid)

    def _cancel(self, send_id, reason):
        if self._rowcount(
            "UPDATE sends SET status='cancelled', error=?, finished_at=?, updated_at=? WHERE send_id=? AND status='scheduled'",
            (reason, self.clock(), self.clock(), send_id),
        ):
            self.audit(send_id, "cancelled", reason=reason)
            return True
        return False

    def cancel(self, send_id, reason="cancelled by user"):
        r = self.row(send_id)
        if not self._cancel(send_id, reason):
            raise Blocked(f"only scheduled messages can be cancelled (this one is {r['status']})")
        return self.row(send_id)

    def record_outcome(self, send_id, outcome):
        """No inbox scope, so bounces and replies are recorded by the user. Bounced/declined addresses are suppressed."""
        if outcome not in ("bounced", "replied", "declined"):
            raise ValueError("outcome must be bounced, replied, or declined")
        r = self.row(send_id)
        if r["status"] not in ("sent", "replied", "bounced", "declined"):
            raise Blocked(f"only sent messages have outcomes (this one is {r['status']})")
        state = "bounced" if outcome == "bounced" else "declined" if outcome == "declined" else "replied"
        self._transition(send_id, ("sent", "replied", "bounced", "declined"), state)
        self.audit(send_id, "outcome", outcome=outcome)
        if outcome != "replied":
            self.suppress(r["recipient"], outcome)
        current = self.row(send_id)
        self.on_outcome(current, outcome, self.clock())
        return current

    def converge_outcome(self, campaign_id, candidate_id, outcome):
        """Project a Gmail/manual outcome onto every delivered send without emitting callbacks."""
        state = {"bounced": "bounced", "declined": "declined", "do_not_contact": "do_not_contact"}.get(
            outcome, "replied" if outcome in ("replied", "interested", "meeting_booked") else None
        )
        if not state:
            return
        for row in self._rows(
            "campaign_id=? AND candidate_id=? AND status IN ('sent','replied','bounced','declined')",
            (campaign_id, candidate_id),
        ):
            if row["status"] != state and self._transition(row["send_id"], row["status"], state):
                self.audit(row["send_id"], "outcome_converged", outcome=outcome, state=state)

    def locked(self, campaign_id, candidate_id, template_version=None):
        """A draft whose message is in flight or already sent must not be edited or regenerated."""
        sql = "campaign_id=? AND candidate_id=? AND status IN ('sending','uncertain','sent','bounced','replied','declined','do_not_contact')"
        args = [campaign_id, candidate_id]
        if template_version:
            sql += " AND template_version=?"
            args.append(template_version)
        return bool(self._rows(sql, args))

    # ------------------------------------------------------------ execution
    def recover(self):
        """On startup, anything caught mid-send has an unknown outcome and must be reconciled, never blindly resent."""
        for r in self._rows("status='sending'"):
            self._update(r["send_id"], status="uncertain", error="process stopped during send")
            self.audit(r["send_id"], "recovered_after_crash")
        self._apply_pending_deliveries()

    def tick(self):
        """One scheduler step: reconcile unknown outcomes, then send at most one due message. Returns send_id or None."""
        self._apply_pending_deliveries()
        for r in self._rows("status='uncertain'"):
            self._reconcile(r)
        now = self.clock()
        if self._halted() or not self.can_send() or self.quiet(now) or self._rate_block(now):
            return None
        for r in self._rows("status='scheduled' AND scheduled_at <= ?", (now,)):
            if not self.campaign_enabled(r["campaign_id"]):
                continue
            if now - r["scheduled_at"] > MAX_LATE:
                if self._rowcount(
                    "UPDATE sends SET status='failed', error=?, finished_at=? WHERE send_id=? AND status='scheduled'",
                    ("missed its window by over 24h; approve again", now, r["send_id"]),
                ):
                    self.audit(r["send_id"], "failed", reason="missed window")
                continue
            reason = self.recipient_problem(r["recipient"], True) or self.revalidate(r)
            if reason:
                self._cancel(r["send_id"], reason)
                continue
            self._send(r, now)
            return r["send_id"]
        return None

    def _send(self, r, now):
        sid = r["send_id"]
        # claim atomically: a concurrent cancel or emergency stop wins if it got there first
        if not self._rowcount(
            "UPDATE sends SET status='sending', attempts=attempts+1, attempted_at=?, updated_at=? "
            "WHERE send_id=? AND status='scheduled'",
            (now, now, sid),
        ):
            return
        self.audit(sid, "attempt", attempt=r["attempts"] + 1)
        key = f"send:{r['idempotency_key']}"
        stage = "create_draft"
        try:
            did = r["gmail_draft_id"]
            if not did:
                attachments = self.materialize(r)
                created = self.gmail.create(
                    r["recipient"], r["subject"], r["body"], key, reply_to=r.get("reply_to"), attachments=attachments
                )
                did = created.get("draft_id") if isinstance(created, dict) else created
                ids = created if isinstance(created, dict) else {}
                self._update(
                    sid,
                    gmail_draft_id=did,
                    gmail_draft_message_id=ids.get("message_id"),
                    gmail_thread_id=ids.get("thread_id"),
                    rfc_message_id=ids.get("rfc_message_id"),
                )
                self.audit(
                    sid,
                    "gmail_draft_created",
                    gmail_draft_id=did,
                    gmail_draft_message_id=ids.get("message_id"),
                    gmail_thread_id=ids.get("thread_id"),
                    rfc_message_id=ids.get("rfc_message_id"),
                )
            if self._halted():  # last check before the irreversible call
                self._update(sid, status="cancelled" if self.settings()["emergency_stop"] else "scheduled")
                self.audit(sid, "held_before_send", emergency_stop=self.settings()["emergency_stop"])
                return
            current = self.row(sid)
            reason = self.recipient_problem(current["recipient"], True) or self.revalidate(current)
            if reason:
                self._transition(sid, "sending", "cancelled", error=reason, finished_at=self.clock())
                self.audit(sid, "cancelled", reason=reason, before="drafts.send")
                return
            stage = "send"
            res = self.gmail.send_draft(did)
        except Exception as e:
            status = http_status(e)
            err = f"{stage}: {type(e).__name__}: {str(e)[:200]}"
            if status in (400, 401, 403) or (stage == "create_draft" and r["attempts"] + 1 >= MAX_ATTEMPTS):
                self._update(sid, status="failed", error=err, finished_at=self.clock())
                self.audit(sid, "failed", error=err, http_status=status)
            else:  # timeout / 5xx / network: Gmail may or may not have acted
                self._update(sid, status="uncertain", error=err)
                self.audit(sid, "uncertain", error=err, http_status=status)
            return
        delivered_at, rfc_id = self.clock(), self.row(sid).get("rfc_message_id")
        try:
            meta = self.gmail.message(res.get("id")) if res.get("id") else None
            if meta:
                delivered_at = meta.get("at") or delivered_at
                rfc_id = meta.get("headers", {}).get("message-id") or rfc_id
        except NotImplementedError:
            pass
        except Exception as e:
            self.audit(sid, "sent_metadata_pending", error=f"{type(e).__name__}: {str(e)[:200]}")
        self._mark_sent(
            sid,
            delivered_at,
            message_id=res.get("id"),
            thread_id=res.get("threadId"),
            rfc_message_id=rfc_id,
            provider_response={k: res.get(k) for k in ("id", "threadId", "labelIds")},
        )

    def _reconcile(self, r):
        sid = r["send_id"]
        try:
            did = r["gmail_draft_id"]
            if not did:
                found = self.gmail.find_by_key(f"send:{r['idempotency_key']}")
                if not found:
                    self.audit(sid, "reconcile_inconclusive", reason="draft id and idempotency key not found")
                    return
                did = found["draft_id"]
                self._update(
                    sid,
                    gmail_draft_id=did,
                    gmail_draft_message_id=found.get("message_id"),
                    gmail_thread_id=found.get("thread_id"),
                    rfc_message_id=found.get("rfc_message_id"),
                )
                r = self.row(sid)
            state = self.gmail.delivery_state(
                did, r.get("gmail_draft_message_id"), r.get("gmail_thread_id"), f"send:{r['idempotency_key']}"
            )
        except Exception as e:
            self.audit(sid, "reconcile_failed", error=f"{type(e).__name__}: {str(e)[:200]}")
            return
        if state["state"] == "sent":
            self._mark_sent(
                sid,
                state.get("delivered_at") or self.clock(),
                message_id=state.get("message_id"),
                thread_id=state.get("thread_id"),
                rfc_message_id=state.get("rfc_message_id"),
                reconciled=True,
                provider_response={"draft_consumed": did},
            )
        elif state["state"] == "deleted":
            if self._transition(
                sid,
                "uncertain",
                "deleted",
                finished_at=self.clock(),
                error="Gmail draft was deleted without a matching SENT message",
            ):
                self.audit(sid, "deleted", reconciled=True, gmail_draft_id=did)
        elif state["state"] == "unknown_missing":
            if not r.get("gmail_draft_message_id") and not r.get("gmail_thread_id"):
                # Compatibility for pre-metadata adapters. Real Gmail always supplies the
                # IDs used by the deleted-vs-sent classifier above.
                self._mark_sent(
                    sid,
                    self.clock(),
                    rfc_message_id=r.get("rfc_message_id"),
                    reconciled=True,
                    provider_response={"draft_consumed": did, "legacy_adapter": True},
                )
            else:
                self.audit(sid, "reconcile_inconclusive", reason="provider cannot classify missing draft")
            return
        elif r["attempts"] >= MAX_ATTEMPTS:
            self._update(sid, status="failed", finished_at=self.clock())
            self.audit(sid, "failed", reconciled=True, reason="not sent after max attempts")
        else:
            nxt = "cancelled" if self.settings()["emergency_stop"] else "scheduled"
            self._update(sid, status=nxt)
            self.audit(sid, "reconciled_not_sent", next=nxt, gmail_draft_id=did)

    def _mark_sent(self, send_id, delivered_at, message_id=None, thread_id=None, rfc_message_id=None, **detail):
        fields = {"finished_at": delivered_at, "error": None}
        if message_id:
            fields["gmail_message_id"] = message_id
        if thread_id:
            fields["gmail_thread_id"] = thread_id
        if rfc_message_id:
            fields["rfc_message_id"] = rfc_message_id
        if not self._transition(send_id, ("sending", "uncertain"), "sent", **fields):
            return False
        self.audit(send_id, "sent", delivered_at=delivered_at, **detail)
        self._apply_delivery(self.row(send_id))
        return True

    def _apply_delivery(self, row):
        if row.get("delivery_applied_at"):
            return
        try:
            self.on_delivery(row)
        except Exception as e:
            self.audit(row["send_id"], "delivery_effects_pending", error=f"{type(e).__name__}: {str(e)[:200]}")
            log.exception("delivery side effects pending for %s", row["send_id"])
            return
        self._update(row["send_id"], delivery_applied_at=self.clock())

    def _apply_pending_deliveries(self):
        for row in self._rows("status IN ('sent','replied','bounced','declined') AND delivery_applied_at IS NULL"):
            self._apply_delivery(row)

    # ------------------------------------------------------------ views
    def status(self):
        now = self.clock()
        s = self.settings()
        times = [
            r["attempted_at"]
            for r in self.cache.q("SELECT attempted_at FROM sends WHERE attempted_at >= ?", (now - 86400,))
        ]
        counts = {r["status"]: r["n"] for r in self.cache.q("SELECT status, COUNT(*) n FROM sends GROUP BY status")}
        return {
            **s,
            "gmail_connected": self.gmail is not None,
            "gmail_can_send": self.can_send(),
            "quiet_now": self.quiet(now),
            "now_local": self.local(now),
            "sent_last_hour": sum(t >= now - 3600 for t in times),
            "sent_last_day": len(times),
            "counts": counts,
        }

    def view(self, r):
        return {
            **{
                k: r[k]
                for k in (
                    "send_id",
                    "campaign_id",
                    "candidate_id",
                    "template_version",
                    "sender",
                    "reply_to",
                    "recipient",
                    "subject",
                    "body",
                    "status",
                    "attempts",
                    "error",
                    "gmail_draft_id",
                    "gmail_draft_message_id",
                    "gmail_message_id",
                    "gmail_thread_id",
                    "rfc_message_id",
                )
            },
            "attachments": json.loads(r["attachments"] or "[]"),
            "scheduled_at": r["scheduled_at"],
            "scheduled_local": self.local(r["scheduled_at"]),
            "approved_at": r["approved_at"],
            "finished_at": r["finished_at"],
            "delivery_applied_at": r["delivery_applied_at"],
        }

    def list(self, campaign_id):
        return [self.view(r) for r in self._rows("campaign_id=?", (campaign_id,))]

    def detail(self, send_id):
        audit = self.cache.q("SELECT at, event, detail FROM send_audit WHERE send_id=? ORDER BY id", (send_id,))
        return {**self.view(self.row(send_id)), "audit": [{**a, "detail": json.loads(a["detail"])} for a in audit]}

    def global_audit(self, limit=50):
        rows = self.cache.q(
            "SELECT at, event, detail FROM send_audit WHERE send_id IS NULL ORDER BY id DESC LIMIT ?", (limit,)
        )
        return [{**a, "detail": json.loads(a["detail"])} for a in rows]
