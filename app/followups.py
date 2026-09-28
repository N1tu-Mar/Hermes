"""Reply-aware outreach: Gmail thread sync, contact outcomes, follow-up sequences.

Rules this module enforces:
- Replies are matched only by the Gmail thread ID HERMES recorded when it created
  the draft. Nothing else in the mailbox is read or stored.
- A follow-up needs a *recorded* earlier email (Gmail SENT label or a manual
  "I sent it"). Nothing is inferred.
- One row per (campaign, candidate, step); generation claims the row with a
  compare-and-set, so restarts and retries can't generate the same step twice.
- Any stop signal (reply, decline, bounce, meeting, do-not-contact, manual stop)
  cancels every unfinished step at once, and a generation that finishes after
  the stop is discarded.
"""
import asyncio
import json
import logging
import re
from datetime import datetime

from . import outlines, writer
from .gmail import GmailAuthError

log = logging.getLogger("followups")

OUTCOMES = ("awaiting_reply", "replied", "interested", "meeting_booked", "declined", "bounced", "no_response", "closed")
STOP_OUTCOMES = {"replied", "interested", "meeting_booked", "declined", "bounced", "closed"}
OPEN_OUTCOMES = (None, "awaiting_reply", "no_response")  # sync may still move these
OPEN_STEPS = ("scheduled", "awaiting_delivery", "generating", "needs_review", "approved", "blocked",
              "gmail_draft_created")
DONE_STEPS = ("sent", "skipped", "cancelled", "deleted")
BOUNCE_RE = re.compile(r"mailer-daemon|postmaster|mail delivery (subsystem|system)", re.I)
SYNC_LIMIT = 25  # threads per sync run; oldest-synced first
DAY = 86400


def _json(v):
    return v if isinstance(v, str) else json.dumps(v)


class Outreach:
    def __init__(self, svc):
        self.svc = svc  # CampaignService: store, cache, model, gmail, jobs, now
        self.cache = svc.cache
        self.gmail_error = None  # set when credentials fail at runtime; cleared on reconnect/restart
        with self.cache.lock:
            columns = {r[1] for r in self.cache.db.execute("PRAGMA table_info(followups)")}
            for name, kind in (("gmail_message_id", "TEXT"), ("gmail_thread_id", "TEXT"),
                               ("rfc_message_id", "TEXT"), ("sent_at", "REAL")):
                if name not in columns:
                    self.cache.db.execute(f"ALTER TABLE followups ADD COLUMN {name} {kind}")

    # ---------------------------------------------------------------- records
    def log(self, cid, cand, kind, detail="", source="system"):
        self.cache.x("INSERT INTO contact_log (campaign_id, candidate_id, at, kind, detail, source) VALUES (?,?,?,?,?,?)",
                     (cid, cand, self.svc.now(), kind, detail, source))

    def contact(self, cid, cand):
        rows = self.cache.q("SELECT * FROM outreach_contacts WHERE campaign_id=? AND candidate_id=?", (cid, cand))
        return rows[0] if rows else None

    def contacts(self, cid):
        return {c["candidate_id"]: c for c in self.cache.q("SELECT * FROM outreach_contacts WHERE campaign_id=?", (cid,))}

    def _set(self, cid, cand, **fields):
        self.cache.upsert("outreach_contacts", {"campaign_id": cid, "candidate_id": cand}, **fields)

    def record_draft(self, cid, cand, ids, subject):
        """Initial outreach draft created in Gmail: remember its thread so sync can find replies."""
        self._set(cid, cand, gmail_thread_id=ids.get("thread_id"), gmail_message_id=ids.get("message_id"),
                  rfc_message_id=ids.get("rfc_message_id"), subject=subject)
        self.log(cid, cand, "gmail_draft", f"initial draft in Gmail (thread {ids.get('thread_id')})")

    def record_sent(self, cid, cand, at, source, rfc_message_id=None, subject=None,
                    gmail_message_id=None, gmail_thread_id=None):
        """First recorded send starts the sequence. Idempotent."""
        c = self.contact(cid, cand)
        if c and c["sent_at"]:
            patch = {}
            for key, value in (("rfc_message_id", rfc_message_id), ("gmail_message_id", gmail_message_id),
                               ("gmail_thread_id", gmail_thread_id)):
                if value and (not c.get(key) or (source == "gmail" and c.get(key) != value)):
                    patch[key] = value
            if patch:
                self._set(cid, cand, **patch)
            return
        intake = self.svc.store.candidates(cid)["intake"]
        fields = {"sent_at": at, "sent_source": source, "sequence": outlines.sequence_for(intake)}
        if rfc_message_id:
            fields["rfc_message_id"] = rfc_message_id
        if gmail_message_id:
            fields["gmail_message_id"] = gmail_message_id
        if gmail_thread_id:
            fields["gmail_thread_id"] = gmail_thread_id
        if subject and not (c and c["subject"]):
            fields["subject"] = subject
        self._set(cid, cand, **fields)
        self.log(cid, cand, "sent", f"initial email recorded as sent on {_day(at)}", source)
        if not c or c["outcome"] in (None, "no_response"):
            self.set_outcome(cid, cand, "awaiting_reply", source)
        self._schedule(cid, cand, 0, at)

    def set_outcome(self, cid, cand, outcome, source, note="", at=None):
        if outcome not in OUTCOMES:
            raise ValueError(f"outcome must be one of {', '.join(OUTCOMES)}")
        old = (self.contact(cid, cand) or {}).get("outcome")
        if old == outcome:
            self.svc._outcome_changed(cid, cand, outcome, source, at or self.svc.now(), note)
            return
        at = at or self.svc.now()
        self._set(cid, cand, outcome=outcome, outcome_at=at)
        self.log(cid, cand, "outcome", f"{old or 'none'} -> {outcome}" + (f": {note}" if note else ""), source)
        if outcome in STOP_OUTCOMES:
            self.stop(cid, cand, f"outcome {outcome}", source)
        self.svc._outcome_changed(cid, cand, outcome, source, at, note)

    def stop(self, cid, cand, reason, source):
        pending = self.cache.q("SELECT step FROM followups WHERE campaign_id=? AND candidate_id=? "
                               "AND status='gmail_draft_created'", (cid, cand))
        n = self.cache.x(f"UPDATE followups SET status='cancelled', updated_at=? WHERE campaign_id=? AND candidate_id=? "
                         f"AND status IN ({','.join('?' * len(OPEN_STEPS))})", (self.svc.now(), cid, cand, *OPEN_STEPS))
        self._set(cid, cand, sequence_state="stopped")
        self.log(cid, cand, "sequence_stopped", f"{reason}; {n} pending follow-up(s) cancelled" +
                 (f". Follow-up draft(s) {[p['step'] + 1 for p in pending]} are still in Gmail Drafts: delete them there"
                  if pending else ""), source)

    def sequence_action(self, cid, cand, action):
        c = self.contact(cid, cand) or {}
        if action == "mark_sent":
            if c.get("sent_at"):
                raise ValueError("a send is already recorded")
            self.record_sent(cid, cand, self.svc.now(), "manual")
        elif action == "pause":
            self._set(cid, cand, sequence_state="paused")
            self.log(cid, cand, "sequence_paused", "", "manual")
        elif action == "resume":
            if c.get("sequence_state") == "stopped" or c.get("do_not_contact"):
                raise ValueError("a stopped sequence can't be resumed")
            self._set(cid, cand, sequence_state="active")
            self.log(cid, cand, "sequence_resumed", "", "manual")
        elif action == "stop":
            self.stop(cid, cand, "stopped manually", "manual")
        elif action == "do_not_contact":
            self._set(cid, cand, do_not_contact=1)
            self.stop(cid, cand, "marked do not contact", "manual")
            self.svc._outcome_changed(cid, cand, "do_not_contact", "manual", self.svc.now())
        else:
            raise ValueError("action must be mark_sent, pause, resume, stop, or do_not_contact")
        return self.contact(cid, cand)

    # ---------------------------------------------------------------- sequence
    def _steps(self, c):
        return outlines.SEQUENCES.get(c.get("sequence") or "professor", [])

    def _schedule(self, cid, cand, step, after):
        c = self.contact(cid, cand)
        steps = self._steps(c)
        if step >= len(steps) or c["sequence_state"] == "stopped" or c["do_not_contact"]:
            return
        due = after + steps[step]["delay_days"] * DAY
        if self.cache.x("INSERT OR IGNORE INTO followups (campaign_id, candidate_id, step, due_at, status, updated_at) "
                        "VALUES (?,?,?,?, 'scheduled', ?)", (cid, cand, step, due, self.svc.now())):
            self.log(cid, cand, "followup_scheduled", f"follow-up {step + 1} due {_day(due)}")

    def _await_delivery(self, cid, cand, step):
        """Expose the next step without starting its clock until the prior message is delivered."""
        c = self.contact(cid, cand)
        if step >= len(self._steps(c)) or c["sequence_state"] == "stopped" or c["do_not_contact"]:
            return
        self.cache.x("INSERT OR IGNORE INTO followups (campaign_id,candidate_id,step,due_at,status,updated_at) "
                     "VALUES (?,?,?,0,'awaiting_delivery',?)", (cid, cand, step, self.svc.now()))

    def record_followup_sent(self, cid, cand, step, message):
        """A Gmail SENT label is the only event that starts the next follow-up delay."""
        key, at = (cid, cand, step), message["at"]
        if not self.cache.x("UPDATE followups SET status='sent', sent_at=?, gmail_message_id=?, gmail_thread_id=?, "
                            "rfc_message_id=?, updated_at=? WHERE campaign_id=? AND candidate_id=? AND step=? "
                            "AND status IN ('gmail_draft_created','approved')",
                            (at, message["id"], message.get("thread_id"), message["headers"].get("message-id"),
                             self.svc.now(), *key)):
            return False
        self.log(cid, cand, "followup_sent", f"follow-up {step + 1} delivered on {_day(at)}", "gmail")
        nxt = self.cache.q("SELECT * FROM followups WHERE campaign_id=? AND candidate_id=? AND step=?",
                           (cid, cand, step + 1))
        if nxt and nxt[0]["status"] == "awaiting_delivery":
            due = at + self._steps(self.contact(cid, cand))[step + 1]["delay_days"] * DAY
            self.cache.x("UPDATE followups SET status='scheduled',due_at=?,updated_at=? WHERE campaign_id=? AND "
                         "candidate_id=? AND step=? AND status='awaiting_delivery'",
                         (due, self.svc.now(), cid, cand, step + 1))
        else:
            self._schedule(cid, cand, step + 1, at)
        return True

    def tick(self):
        """Claim due steps (once each) and queue generation; mark finished sequences no_response."""
        now = self.svc.now()
        due = self.cache.q("""SELECT f.*, c.sequence FROM followups f JOIN outreach_contacts c USING (campaign_id, candidate_id)
                              WHERE f.status='scheduled' AND f.due_at<=? AND c.sequence_state='active'
                              AND c.do_not_contact=0""", (now,))
        queued = 0
        for f in due:
            key = (f["campaign_id"], f["candidate_id"], f["step"])
            step = self._steps(f)[f["step"]]
            if f["attempts"] >= step["max_attempts"]:
                self.cache.x("UPDATE followups SET status='blocked', issues=?, updated_at=? WHERE campaign_id=? AND "
                             "candidate_id=? AND step=? AND status='scheduled'",
                             (_json([f"generation failed {f['attempts']} time(s); max attempts reached"]), now, *key))
                continue
            if self.cache.x("UPDATE followups SET status='generating', attempts=attempts+1, updated_at=? WHERE "
                            "campaign_id=? AND candidate_id=? AND step=? AND status='scheduled'", (now, *key)):
                self.svc.jobs.submit(key[0], "followup", f"{key[1]}#{key[2]}")
                queued += 1
        self._mark_no_response(now)
        return queued

    def _mark_no_response(self, now):
        rows = self.cache.q("SELECT * FROM outreach_contacts WHERE outcome='awaiting_reply' AND sequence_state='active'")
        for c in rows:
            fs = self.cache.q("SELECT * FROM followups WHERE campaign_id=? AND candidate_id=? ORDER BY step",
                              (c["campaign_id"], c["candidate_id"]))
            if (len(fs) == len(self._steps(c)) and fs and all(f["status"] in DONE_STEPS for f in fs)
                    and now - max(f["updated_at"] for f in fs) > outlines.NO_RESPONSE_DAYS * DAY):
                self.set_outcome(c["campaign_id"], c["candidate_id"], "no_response", "system", "sequence finished")

    def reset_interrupted(self):
        """On restart, a step caught mid-generation goes back to scheduled (same row, attempt already counted)."""
        self.cache.x("UPDATE followups SET status='scheduled' WHERE status='generating'")

    async def generate(self, cid, arg):
        cand, _, step_s = arg.partition("#")
        step = int(step_s)
        key = (cid, cand, step)
        c = self.contact(cid, cand)
        if not c or c["sequence_state"] != "active" or c["do_not_contact"] or c["outcome"] in STOP_OUTCOMES:
            self.cache.x("UPDATE followups SET status='cancelled' WHERE campaign_id=? AND candidate_id=? AND step=? "
                         "AND status='generating'", key)
            return
        intake = self.svc.store.candidates(cid)["intake"]
        profile = self.svc.store.research(cid)["profiles"].get(cand) or {}
        try:
            outline = outlines.build_followup_outline(intake, profile, intake.get("sender_background"),
                                                      self._steps(c)[step], self.prior_contact(cid, cand, step))
        except outlines.OutlineBlocked as e:
            self.cache.x("UPDATE followups SET status='blocked', issues=? WHERE campaign_id=? AND candidate_id=? "
                         "AND step=? AND status='generating'", (_json([str(e)]), *key))
            return
        try:
            d = await writer.write_draft(self.svc.model, cid, outline, profile)
        except Exception:
            self.cache.x("UPDATE followups SET status='scheduled' WHERE campaign_id=? AND candidate_id=? AND step=? "
                         "AND status='generating'", key)  # next tick retries until max_attempts
            raise
        subject = _re(c["subject"])  # Gmail threads only when the subject matches
        issues = writer.check_draft(subject, d["body"], outline, profile, d["evidence_ids"])
        if self.cache.x("UPDATE followups SET status='needs_review', subject=?, body=?, issues=?, outline=?, "
                        "evidence_ids=?, updated_at=? WHERE campaign_id=? AND candidate_id=? AND step=? "
                        "AND status='generating'", (subject, d["body"], _json(issues), _json(outline),
                                                    _json(d["evidence_ids"]), self.svc.now(), *key)):
            self.log(cid, cand, "followup_generated", f"follow-up {step + 1} ready for review")
            self.svc.cache.event(cid, f"{cand}: follow-up {step + 1} ready for review")

    def prior_contact(self, cid, cand, step):
        """Only what HERMES recorded: original subject, send date, and our own original text."""
        c = self.contact(cid, cand)
        if not c or not c["sent_at"]:
            return None
        d = next((d for d in sorted(self.cache.list_drafts(cid), key=lambda d: d["updated_at"])
                  if d["candidate_id"] == cand and not d["template_version"].startswith("rsvp")), None)
        return {"original_subject": c["subject"] or (d or {}).get("subject"), "sent_on": _day(c["sent_at"]),
                "original_excerpt": ((d or {}).get("body") or "")[:400], "followups_already_prepared": step,
                "reply_received": False}

    # ---------------------------------------------------------------- gmail sync
    def sync(self, campaign_id=None, limit=SYNC_LIMIT):
        """Check HERMES-created threads only. Blocking: call via asyncio.to_thread."""
        g = self.svc.gmail
        out = {"checked": 0, "replies": 0, "bounces": 0, "errors": 0}
        if not g:
            return {**out, "error": "Gmail not connected"}
        if not g.can_sync:
            return {**out, "error": "Reply tracking needs the gmail.metadata scope; run `python -m app.gmail`"}
        sql = f"""SELECT * FROM outreach_contacts WHERE gmail_thread_id IS NOT NULL AND sequence_state!='stopped'
                  AND (outcome IS NULL OR outcome IN ('awaiting_reply','no_response'))
                  {'AND campaign_id=?' if campaign_id else ''} ORDER BY COALESCE(last_synced_at, 0) LIMIT ?"""
        for c in self.cache.q(sql, (campaign_id, limit) if campaign_id else (limit,)):
            try:
                r = self.sync_contact(c)
            except GmailAuthError as e:
                self.gmail_error = str(e)
                return {**out, "error": str(e)}
            except KeyError:
                self._set(c["campaign_id"], c["candidate_id"], last_synced_at=self.svc.now(),
                          sync_error="thread no longer in Gmail (draft deleted?)")
                out["errors"] += 1
                continue
            except Exception as e:
                log.warning("sync %s failed: %s", c["gmail_thread_id"], e)
                out["errors"] += 1
                continue
            out["checked"] += 1
            out["replies"] += r == "reply"
            out["bounces"] += r == "bounce"
        self.gmail_error = None
        return out

    def sync_contact(self, c):
        cid, cand = c["campaign_id"], c["candidate_id"]
        msgs = sorted(self.svc.gmail.thread(c["gmail_thread_id"]), key=lambda m: m["at"])
        self._set(cid, cand, last_synced_at=self.svc.now(), sync_error=None)
        sent = [m for m in msgs if "SENT" in m["labels"]]
        followups = self.cache.q("SELECT * FROM followups WHERE campaign_id=? AND candidate_id=?", (cid, cand))
        followup_ids = {f.get("gmail_message_id") for f in followups if f.get("gmail_message_id")}
        initial = next((m for m in sent if m["id"] == c.get("gmail_message_id")), None) \
            or next((m for m in sent if m["id"] not in followup_ids), None)
        if initial:
            self.record_sent(cid, cand, initial["at"], "gmail", initial["headers"].get("message-id"),
                             initial["headers"].get("subject"), initial["id"], c["gmail_thread_id"])
        by_id = {m["id"]: m for m in sent}
        for f in followups:
            if f.get("gmail_message_id") in by_id:
                message = {**by_id[f["gmail_message_id"]], "thread_id": c["gmail_thread_id"]}
                self.record_followup_sent(cid, cand, f["step"], message)
        result = None
        for m in msgs:
            if "SENT" in m["labels"] or "DRAFT" in m["labels"]:
                continue  # ours
            h = m["headers"]
            kind = ("bounce" if BOUNCE_RE.search(h.get("from", "")) else
                    "auto_reply" if h.get("auto-submitted", "no").lower() != "no" else "reply")
            if not self.cache.x("INSERT OR IGNORE INTO gmail_seen VALUES (?,?,?,?,?,?,?)",
                                (m["id"], c["gmail_thread_id"], cid, cand, kind, _addr(h.get("from")), m["at"])):
                continue  # already processed
            self.log(cid, cand, kind, f"from {_addr(h.get('from'))} on {_day(m['at'])}", "gmail")
            if kind != "auto_reply" and (self.contact(cid, cand) or {}).get("outcome") in OPEN_OUTCOMES:
                self.set_outcome(cid, cand, "bounced" if kind == "bounce" else "replied", "gmail", at=m["at"])
                result = kind
        return result

    # ---------------------------------------------------------------- queue
    def queue(self, cid):
        now, contacts = self.svc.now(), self.contacts(cid)
        groups = {k: [] for k in ("due", "upcoming", "paused", "blocked", "completed")}
        for f in self.cache.q("SELECT * FROM followups WHERE campaign_id=? ORDER BY due_at", (cid,)):
            c = contacts.get(f["candidate_id"]) or {}
            for k in ("issues", "evidence_ids"):
                f[k] = json.loads(f[k]) if f[k] else []
            f.pop("outline", None)
            f["template"] = self._steps(c)[f["step"]]["template"] if f["step"] < len(self._steps(c)) else None
            f["prior"] = {"subject": c.get("subject"), "sent_on": _day(c["sent_at"]) if c.get("sent_at") else None,
                          "source": c.get("sent_source")}
            st = f["status"]
            group = ("completed" if st in DONE_STEPS else "blocked" if st == "blocked"
                     else "paused" if c.get("sequence_state") == "paused"
                     else "upcoming" if st == "awaiting_delivery" or (st == "scheduled" and f["due_at"] > now)
                     else "due")
            groups[group].append(f)
        return groups

    def _row(self, cid, cand, step):
        rows = self.cache.q("SELECT * FROM followups WHERE campaign_id=? AND candidate_id=? AND step=?", (cid, cand, step))
        if not rows:
            raise KeyError(f"no follow-up {step + 1} for {cand}")
        return rows[0]

    async def act(self, cid, cand, step, action, body):
        f, key, now = self._row(cid, cand, step), (cid, cand, step), self.svc.now()
        st = f["status"]

        def need(*ok):
            if st not in ok:
                raise ValueError(f"can't {action} a follow-up in state {st}")
        if action == "edit":
            need("needs_review", "approved")
            outline = json.loads(f["outline"])
            issues = writer.check_draft(f["subject"], str(body.get("body", "")), outline, None,
                                        json.loads(f["evidence_ids"] or "[]"))
            self.cache.x("UPDATE followups SET body=?, issues=?, status='needs_review', updated_at=? WHERE campaign_id=? "
                         "AND candidate_id=? AND step=?", (str(body.get("body", "")), _json(issues), now, *key))
        elif action == "skip":
            need("scheduled", "needs_review", "blocked")
            self.cache.x("UPDATE followups SET status='skipped', updated_at=? WHERE campaign_id=? AND candidate_id=? "
                         "AND step=?", (now, *key))
            self.log(cid, cand, "followup_skipped", f"follow-up {step + 1} skipped", "manual")
            self._schedule(cid, cand, step + 1, now)
        elif action == "reschedule":
            need("scheduled", "blocked")
            due = float(body.get("due_at") or 0)
            if due <= 0:
                raise ValueError("due_at (unix seconds) required")
            self.cache.x("UPDATE followups SET status='scheduled', due_at=?, attempts=0, issues=NULL, updated_at=? "
                         "WHERE campaign_id=? AND candidate_id=? AND step=?", (due, now, *key))
            self.log(cid, cand, "followup_rescheduled", f"follow-up {step + 1} now due {_day(due)}", "manual")
        elif action == "cancel":
            need(*OPEN_STEPS)
            self.stop(cid, cand, f"follow-up {step + 1} cancelled from the queue", "manual")
        elif action == "approve":
            need("needs_review", "approved")
            return await self._approve(f, key, now)
        else:
            raise ValueError("action must be approve, edit, skip, reschedule, or cancel")
        return {"status": self._row(*key)["status"]}

    async def _approve(self, f, key, now):
        cid, cand, step = key
        c = self.contact(cid, cand)
        if c["outcome"] in STOP_OUTCOMES or c["do_not_contact"] or c["sequence_state"] != "active":
            raise ValueError("sequence is stopped or paused for this person")
        if f["status"] == "needs_review":
            self.cache.x("UPDATE followups SET status='approved', updated_at=? WHERE campaign_id=? AND candidate_id=? "
                         "AND step=?", (now, *key))
            self.log(cid, cand, "followup_approved", f"follow-up {step + 1} approved", "manual")
            self._await_delivery(cid, cand, step + 1)
        g = self.svc.gmail
        if not g or not c["gmail_thread_id"]:
            return {"status": "approved", "result": "approved; Gmail not connected or no thread recorded: copy the text"}
        if g.can_sync:  # last-moment check: never draft a follow-up into a thread that just got a reply
            try:
                await asyncio.to_thread(self.sync_contact, c)
            except GmailAuthError as e:
                self.gmail_error = str(e)
                return {"status": "approved", "result": str(e)}
            if self._row(*key)["status"] == "cancelled":
                return {"status": "cancelled", "result": "reply found in the thread; sequence stopped"}
        profile = self.svc.store.research(cid)["profiles"].get(cand) or {}
        to = profile.get("contact_email") if profile.get("email_verified_on_page") else None
        res = await self.svc.gmail_create(
            f"{cid}:{cand}:followup{step}", bool(f["gmail_attempt_at"]),
            lambda: self.cache.x("UPDATE followups SET gmail_attempt_at=? WHERE campaign_id=? AND candidate_id=? "
                                 "AND step=?", (self.svc.now(), *key)),
            (to, f["subject"], f["body"]), dict(thread_id=c["gmail_thread_id"], in_reply_to=c["rfc_message_id"]))
        if res.get("gmail_draft_id"):
            ids = res.get("ids") or {}
            self.cache.x("UPDATE followups SET status='gmail_draft_created', gmail_draft_id=?, gmail_message_id=?, "
                         "gmail_thread_id=?, rfc_message_id=?, updated_at=? "
                         "WHERE campaign_id=? AND candidate_id=? AND step=?",
                         (res["gmail_draft_id"], ids.get("message_id"), ids.get("thread_id"),
                          ids.get("rfc_message_id"), now, *key))
            self.log(cid, cand, "followup_gmail_draft", f"follow-up {step + 1} placed in Gmail Drafts (same thread)")
        return {"status": self._row(*key)["status"], **res}

    def timeline(self, cid, cand):
        return self.cache.q("SELECT at, kind, detail, source FROM contact_log WHERE campaign_id=? AND candidate_id=? "
                            "ORDER BY id", (cid, cand))


def _day(ts):
    return datetime.fromtimestamp(ts).strftime("%B %d").replace(" 0", " ")


def _addr(v):
    m = re.search(r"[\w.+-]+@[\w.-]+", v or "")
    return m.group(0).lower() if m else (v or "")[:80]


def _re(subject):
    s = (subject or "").strip()
    return s if s.lower().startswith("re:") else f"Re: {s}"
