"""Persistent template/content/attachment workspace and campaign rule records."""
import base64
import binascii
import hashlib
import json
import mimetypes
import re
import secrets
import time
from pathlib import Path


TEMPLATE_CATEGORIES = {
    "professor_outreach", "startup_outreach", "speaker_invitation",
    "mentorship_request", "rsvp_followup", "general_followup",
}
CONTENT_KINDS = {
    "signature", "event_description", "club_description", "personal_introduction",
    "call_to_action", "supporting_links",
}
ATTACHMENT_KINDS = {"resume", "club_deck", "event_brief", "one_pager"}
RULE_KINDS = {
    "draft_verified", "research_next", "exclude_contacted", "followup_no_reply",
    "require_manual_review",
}
ALLOWED_VARIABLES = {
    "first_name", "last_name", "recipient_name", "organization", "role",
    "sender_background", "event_details", "outreach_goal", "specific_connection",
    "signature", "event_description", "club_description", "personal_introduction",
    "call_to_action", "supporting_links", "prior_subject", "prior_sent_on",
}
PLACEHOLDER_RE = re.compile(r"\{\{\s*([a-zA-Z][a-zA-Z0-9_]*)\s*\}\}")
ANY_PLACEHOLDER_RE = re.compile(r"\{\{.*?\}\}")
SINGLE_PLACEHOLDER_RE = re.compile(r"(?<!\{)\{[a-zA-Z][a-zA-Z0-9_]*\}(?!\})")
ID_RE = re.compile(r"^[a-z][a-z0-9_]{2,63}$")
MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
EXTENSIONS = {".pdf", ".doc", ".docx", ".ppt", ".pptx"}
MEDIA_TYPES = {
    "application/pdf", "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.ms-powerpoint",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}


DEFAULT_TEMPLATES = [
    ("professor_outreach", "Professor outreach", "professor_outreach",
     "Undergraduate research interest — {{recipient_name}}",
     "Use this greeting: Dear Professor {{last_name}}. Introduce {{sender_background}}, connect to "
     "{{specific_connection}}, and ask {{outreach_goal}}. Keep it respectful, concise, and under 180 words."),
    ("startup_outreach", "Startup outreach", "startup_outreach",
     "Quick question about {{organization}}",
     "Write to {{first_name}}. Introduce {{sender_background}}, mention {{specific_connection}}, and ask "
     "{{outreach_goal}}. Be direct and warm; stay under 140 words."),
    ("speaker_invitation", "Speaker invitation", "speaker_invitation",
     "Invitation from Rutgers",
     "Invite {{first_name}} based on {{specific_connection}}. Sender: {{sender_background}}. "
     "Ask: {{outreach_goal}}. Be enthusiastic and professional; stay under 190 words."),
    ("mentorship_request", "Mentorship request", "mentorship_request",
     "Mentorship request",
     "Write a low-pressure mentorship request to {{first_name}}. Sender: {{sender_background}}. "
     "Connection: {{specific_connection}}. Ask: {{outreach_goal}}. Stay under 160 words."),
    ("rsvp_followup", "RSVP follow-up", "rsvp_followup",
     "Following up: {{prior_subject}}",
     "Briefly reference the invitation sent {{prior_sent_on}} and ask {{outreach_goal}}. "
     "Be polite and pressure-free; stay under 110 words."),
    ("general_followup", "General follow-up", "general_followup",
     "Following up: {{prior_subject}}",
     "Write a brief follow-up to {{first_name}} about the note sent {{prior_sent_on}}. "
     "Restate {{outreach_goal}}, make it easy to decline, and stay under 90 words."),
]


def _now():
    return time.time()


def _json(value):
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def _slug(value):
    value = re.sub(r"[^a-z0-9]+", "_", (value or "").lower()).strip("_")
    return value[:48] or "item"


def validate_template(subject, body):
    if not str(subject).strip() or not str(body).strip():
        raise ValueError("template subject and body are required")
    text = f"{subject}\n{body}"
    variables = sorted(set(PLACEHOLDER_RE.findall(text)))
    unknown = sorted(set(variables) - ALLOWED_VARIABLES)
    malformed = [x for x in ANY_PLACEHOLDER_RE.findall(text) if not PLACEHOLDER_RE.fullmatch(x)]
    if unknown:
        raise ValueError(f"unknown template variables: {', '.join(unknown)}")
    if malformed or SINGLE_PLACEHOLDER_RE.search(text) or text.count("{{") != text.count("}}"):
        raise ValueError("malformed placeholder; use {{variable_name}}")
    return variables


def render_template(template, values):
    missing = [v for v in template["variables"] if not str(values.get(v) or "").strip()]
    if missing:
        raise ValueError(f"unresolved template variables: {', '.join(missing)}")
    def repl(match):
        return str(values[match.group(1)]).strip()
    subject = PLACEHOLDER_RE.sub(repl, template["subject"])
    body = PLACEHOLDER_RE.sub(repl, template["body"])
    if ANY_PLACEHOLDER_RE.search(subject + body) or SINGLE_PLACEHOLDER_RE.search(subject + body):
        raise ValueError("unresolved template placeholder")
    return {"subject": subject, "body": body}


class Workspace:
    def __init__(self, cache, data_root):
        self.cache = cache
        self.attachment_root = Path(data_root).resolve() / "attachments"
        self.attachment_root.mkdir(parents=True, exist_ok=True)
        self.seed_templates()

    # templates --------------------------------------------------------
    def seed_templates(self):
        for tid, name, category, subject, body in DEFAULT_TEMPLATES:
            if self.cache.q("SELECT 1 FROM template_definitions WHERE template_id=?", (tid,)):
                continue
            self.create_template({"template_id": tid, "name": name, "category": category,
                                  "subject": subject, "body": body, "change_note": "built-in starter"})

    def create_template(self, data):
        tid = data.get("template_id") or f"{_slug(data.get('name'))}_{secrets.token_hex(3)}"
        if not ID_RE.fullmatch(tid):
            raise ValueError("template_id must use lowercase letters, numbers, and underscores")
        if self.cache.q("SELECT 1 FROM template_definitions WHERE template_id=?", (tid,)):
            raise ValueError(f"template_id already exists: {tid}")
        category = data.get("category")
        if category not in TEMPLATE_CATEGORIES:
            raise ValueError("unknown template category")
        name = str(data.get("name") or "").strip()
        if not name:
            raise ValueError("template name is required")
        variables = validate_template(data.get("subject"), data.get("body"))
        now = _now()
        with self.cache.lock:
            self.cache.db.execute("INSERT INTO template_definitions VALUES (?,?,?,?,?,?)",
                                  (tid, name, category, 0, now, now))
            self.cache.db.execute("INSERT INTO template_versions VALUES (?,?,?,?,?,?,?)",
                                  (tid, 1, data["subject"].strip(), data["body"].strip(), _json(variables),
                                   str(data.get("change_note") or "initial version"), now))
        return self.get_template(tid)

    def edit_template(self, tid, data):
        current = self.get_template(tid)
        name = str(data.get("name", current["name"])).strip()
        category = data.get("category", current["category"])
        subject, body = data.get("subject", current["subject"]), data.get("body", current["body"])
        if category not in TEMPLATE_CATEGORIES or not name:
            raise ValueError("valid name and category are required")
        variables = validate_template(subject, body)
        now = _now()
        with self.cache.lock:
            version = self.cache.db.execute(
                "SELECT MAX(version)+1 FROM template_versions WHERE template_id=?", (tid,)).fetchone()[0]
            self.cache.db.execute("UPDATE template_definitions SET name=?,category=?,updated_at=? WHERE template_id=?",
                                  (name, category, now, tid))
            self.cache.db.execute("INSERT INTO template_versions VALUES (?,?,?,?,?,?,?)",
                                  (tid, version, subject.strip(), body.strip(), _json(variables),
                                   str(data.get("change_note") or "edited"), now))
        invalidated = self.invalidate_template(tid)
        result = self.get_template(tid, version)
        result["invalidated_approvals"] = invalidated
        return result

    def invalidate_template(self, tid):
        rows = self.cache.q("SELECT campaign_id,candidate_id,template_version,issues FROM drafts "
                            "WHERE template_id=? AND status='approved'", (tid,))
        for row in rows:
            issues = json.loads(row.get("issues") or "[]")
            note = "template changed after approval; review again or regenerate"
            if note not in issues: issues.append(note)
            self.cache.x("UPDATE drafts SET status='needs_review',issues=?,updated_at=? "
                         "WHERE campaign_id=? AND candidate_id=? AND template_version=?",
                         (_json(issues), _now(), row["campaign_id"], row["candidate_id"], row["template_version"]))
        return len(rows)

    def get_template(self, tid, version=None):
        args = [tid]
        extra = ""
        if version is not None:
            extra = " AND v.version=?"; args.append(int(version))
        rows = self.cache.q("""SELECT d.*,v.version,v.subject,v.body,v.variables,v.change_note,
                               v.created_at AS version_created_at
                               FROM template_definitions d JOIN template_versions v USING(template_id)
                               WHERE d.template_id=?""" + extra + " ORDER BY v.version DESC LIMIT 1", args)
        if not rows: raise KeyError(tid)
        rows[0]["variables"] = json.loads(rows[0]["variables"])
        rows[0]["archived"] = bool(rows[0]["archived"])
        return rows[0]

    def list_templates(self, include_archived=False):
        sql = """SELECT d.*,v.version,v.subject,v.body,v.variables,v.change_note,v.created_at AS version_created_at
                 FROM template_definitions d JOIN template_versions v USING(template_id)
                 WHERE v.version=(SELECT MAX(x.version) FROM template_versions x WHERE x.template_id=d.template_id)"""
        if not include_archived: sql += " AND d.archived=0"
        rows = self.cache.q(sql + " ORDER BY d.category,d.name")
        for row in rows:
            row["variables"] = json.loads(row["variables"]); row["archived"] = bool(row["archived"])
        return rows

    def history(self, tid):
        self.get_template(tid)
        rows = self.cache.q("SELECT * FROM template_versions WHERE template_id=? ORDER BY version DESC", (tid,))
        for row in rows: row["variables"] = json.loads(row["variables"])
        return rows

    def duplicate_template(self, tid, data):
        source = self.get_template(tid, data.get("version"))
        return self.create_template({"template_id": data.get("template_id"),
                                     "name": data.get("name") or source["name"] + " copy",
                                     "category": source["category"], "subject": source["subject"],
                                     "body": source["body"],
                                     "change_note": f"duplicated from {tid}.v{source['version']}"})

    def archive_template(self, tid, archived=True):
        self.get_template(tid)
        self.cache.x("UPDATE template_definitions SET archived=?,updated_at=? WHERE template_id=?",
                     (int(archived), _now(), tid))
        return self.get_template(tid)

    def preview_template(self, tid, version, values):
        template = self.get_template(tid, version)
        return {**render_template(template, values), "template_id": tid, "version": template["version"]}

    def default_template(self, intake, followup=False):
        if followup:
            category = "rsvp_followup" if intake.get("subtype") == "speaker_mentor" else "general_followup"
        elif intake.get("subtype") == "startup": category = "startup_outreach"
        elif intake.get("subtype") == "speaker_mentor":
            text = (intake.get("raw_request") or "") + " " + (intake.get("event_details") or "")
            category = "mentorship_request" if re.search(r"\bmentor", text, re.I) else "speaker_invitation"
        else: category = "professor_outreach"
        rows = [t for t in self.list_templates() if t["category"] == category]
        if not rows: raise ValueError(f"no active template for {category}")
        return rows[0]

    # identities and content ------------------------------------------
    def list_identities(self):
        rows = self.cache.q("SELECT * FROM identities ORDER BY display_name")
        for r in rows: r["links"] = json.loads(r["links"] or "[]")
        return rows

    def create_identity(self, data):
        now = _now()
        name, biography = str(data.get("display_name") or "").strip(), str(data.get("biography") or "").strip()
        if not name or not biography: raise ValueError("display_name and biography are required")
        self.cache.x("INSERT INTO identities (display_name,biography,organization,role,signature,links,default_ask,reply_to,created_at,updated_at) "
                     "VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (name, biography, data.get("organization"), data.get("role"), data.get("signature"),
                      _json(data.get("links") or []), data.get("default_ask"), data.get("reply_to"), now, now))
        return self.cache.q("SELECT * FROM identities ORDER BY id DESC LIMIT 1")[0]

    def create_content(self, data):
        kind = data.get("kind")
        if kind not in CONTENT_KINDS: raise ValueError("unknown reusable content kind")
        name, body = str(data.get("name") or "").strip(), str(data.get("body") or "").strip()
        if not name or not body: raise ValueError("content name and body are required")
        identity_id = data.get("identity_id")
        if identity_id is not None and not self.cache.q("SELECT 1 FROM identities WHERE id=?", (identity_id,)):
            raise ValueError("unknown sender identity")
        cid, now = f"cnt_{secrets.token_hex(6)}", _now()
        self.cache.x("INSERT INTO reusable_content VALUES (?,?,?,?,?,?,?,?)",
                     (cid, identity_id, kind, name, body, 0, now, now))
        return self.cache.q("SELECT * FROM reusable_content WHERE content_id=?", (cid,))[0]

    def list_content(self, identity_id=None):
        sql, args = "SELECT * FROM reusable_content WHERE archived=0", []
        if identity_id is not None: sql += " AND identity_id=?"; args.append(identity_id)
        return self.cache.q(sql + " ORDER BY kind,name", args)

    # attachments ------------------------------------------------------
    def add_attachment(self, data):
        name = str(data.get("filename") or "")
        if not name or name != Path(name).name or ".." in name or any(ord(c) < 32 for c in name):
            raise ValueError("unsafe attachment filename")
        ext = Path(name).suffix.lower()
        media_type = str(data.get("media_type") or mimetypes.guess_type(name)[0] or "")
        if ext not in EXTENSIONS or media_type not in MEDIA_TYPES:
            raise ValueError("attachments must be PDF, DOC/DOCX, or PPT/PPTX")
        kind = data.get("kind")
        if kind not in ATTACHMENT_KINDS: raise ValueError("unknown attachment kind")
        encoded = data.get("content_base64") or ""
        if not isinstance(encoded, str) or len(encoded) > ((MAX_ATTACHMENT_BYTES + 2) // 3 * 4 + 4):
            raise ValueError("attachment exceeds the 10 MB limit")
        try: raw = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError): raise ValueError("attachment content is not valid base64")
        if not raw: raise ValueError("attachment is empty")
        if len(raw) > MAX_ATTACHMENT_BYTES: raise ValueError("attachment exceeds the 10 MB limit")
        identity_id = data.get("identity_id")
        if identity_id is not None and not self.cache.q("SELECT 1 FROM identities WHERE id=?", (identity_id,)):
            raise ValueError("unknown sender identity")
        aid = f"att_{secrets.token_hex(8)}"
        stored = f"{aid}{ext}"
        path = (self.attachment_root / stored).resolve()
        if path.parent != self.attachment_root: raise ValueError("unsafe attachment path")
        path.write_bytes(raw)
        path.chmod(0o600)
        values = (aid, identity_id, kind, name, stored, media_type, len(raw), hashlib.sha256(raw).hexdigest(), 0, _now())
        try:
            self.cache.x("INSERT INTO attachments VALUES (?,?,?,?,?,?,?,?,?,?)", values)
        except Exception:
            path.unlink(missing_ok=True)
            raise
        return self.attachment(aid)

    def attachment(self, aid):
        rows = self.cache.q("SELECT * FROM attachments WHERE attachment_id=?", (aid,))
        if not rows: raise KeyError(aid)
        return rows[0]

    def list_attachments(self, identity_id=None):
        sql, args = "SELECT * FROM attachments WHERE archived=0", []
        if identity_id is not None: sql += " AND identity_id=?"; args.append(identity_id)
        return self.cache.q(sql + " ORDER BY created_at DESC", args)

    def attachments_by_ids(self, ids):
        return [self.attachment(aid) for aid in ids or []]

    def attachment_payload(self, meta):
        path = (self.attachment_root / meta["stored_name"]).resolve()
        if path.parent != self.attachment_root or not path.is_file():
            raise ValueError(f"attachment file is missing: {meta['display_name']}")
        raw = path.read_bytes()
        if len(raw) != meta["size"] or hashlib.sha256(raw).hexdigest() != meta["sha256"]:
            raise ValueError(f"attachment failed integrity check: {meta['display_name']}")
        return {"filename": meta["display_name"], "media_type": meta["media_type"], "content": raw}

    # campaign assets --------------------------------------------------
    def set_assets(self, campaign_id, asset_type, ids):
        table = "attachments" if asset_type == "attachment" else "reusable_content" if asset_type == "content" else None
        key = "attachment_id" if asset_type == "attachment" else "content_id"
        if not table: raise ValueError("asset_type must be attachment or content")
        ids = list(dict.fromkeys(ids or []))
        for item in ids:
            if not self.cache.q(f"SELECT 1 FROM {table} WHERE {key}=? AND archived=0", (item,)):
                raise ValueError(f"unknown or archived {asset_type}: {item}")
        old = self.asset_ids(campaign_id, asset_type)
        with self.cache.lock:
            self.cache.db.execute("DELETE FROM campaign_assets WHERE campaign_id=? AND asset_type=?", (campaign_id, asset_type))
            self.cache.db.executemany("INSERT INTO campaign_assets VALUES (?,?,?,?)",
                                      [(campaign_id, asset_type, item, i) for i, item in enumerate(ids)])
        if old != ids: self.invalidate_campaign_approvals(campaign_id, f"{asset_type} list changed after approval")
        return ids

    def asset_ids(self, campaign_id, asset_type):
        return [r["asset_id"] for r in self.cache.q(
            "SELECT asset_id FROM campaign_assets WHERE campaign_id=? AND asset_type=? ORDER BY position",
            (campaign_id, asset_type))]

    def campaign_attachments(self, campaign_id):
        return self.cache.q("""SELECT a.* FROM campaign_assets c JOIN attachments a ON a.attachment_id=c.asset_id
                             WHERE c.campaign_id=? AND c.asset_type='attachment' AND a.archived=0 ORDER BY c.position""",
                            (campaign_id,))

    def campaign_content(self, campaign_id):
        return self.cache.q("""SELECT r.* FROM campaign_assets c JOIN reusable_content r ON r.content_id=c.asset_id
                             WHERE c.campaign_id=? AND c.asset_type='content' AND r.archived=0 ORDER BY c.position""",
                            (campaign_id,))

    def invalidate_campaign_approvals(self, campaign_id, reason):
        rows = self.cache.list_drafts(campaign_id)
        changed = 0
        for d in rows:
            if d["status"] != "approved": continue
            issues = d["issues"] + ([reason] if reason not in d["issues"] else [])
            self.cache.upsert_draft(campaign_id, d["candidate_id"], d["template_version"],
                                    status="needs_review", issues=issues)
            changed += 1
        return changed

    # rules ------------------------------------------------------------
    def create_rule(self, campaign_id, data):
        kind = data.get("kind")
        if kind not in RULE_KINDS: raise ValueError("unknown campaign rule kind")
        if kind == "require_manual_review" and data.get("enabled") is False:
            raise ValueError("manual review is a mandatory policy and cannot be disabled")
        rid, now = f"rule_{secrets.token_hex(6)}", _now()
        config = dict(data.get("config") or {})
        self.cache.x("INSERT INTO campaign_rules VALUES (?,?,?,?,?,?,?)",
                     (rid, campaign_id, kind, _json(config), 1, now, now))
        return self.get_rule(rid)

    def get_rule(self, rid):
        rows = self.cache.q("SELECT * FROM campaign_rules WHERE rule_id=?", (rid,))
        if not rows: raise KeyError(rid)
        rows[0]["config"] = json.loads(rows[0]["config"])
        return rows[0]

    def list_rules(self, campaign_id):
        rows = self.cache.q("SELECT * FROM campaign_rules WHERE campaign_id=? ORDER BY created_at", (campaign_id,))
        for r in rows: r["config"] = json.loads(r["config"])
        return rows

    def record_execution(self, rule, dry_run, summary, actions):
        eid = f"run_{secrets.token_hex(7)}"
        with self.cache.lock:
            self.cache.db.execute("INSERT INTO rule_executions VALUES (?,?,?,?,?,?)",
                                  (eid, rule["rule_id"], rule["campaign_id"], int(dry_run), _now(), summary))
            self.cache.db.executemany(
                "INSERT INTO rule_actions (execution_id,candidate_id,action,status,reason) VALUES (?,?,?,?,?)",
                [(eid, a["candidate_id"], a["action"], a["status"], a["reason"]) for a in actions])
        return self.execution(eid)

    def execution(self, eid):
        rows = self.cache.q("SELECT * FROM rule_executions WHERE execution_id=?", (eid,))
        if not rows: raise KeyError(eid)
        rows[0]["actions"] = self.cache.q(
            "SELECT candidate_id,action,status,reason FROM rule_actions WHERE execution_id=? ORDER BY id", (eid,))
        return rows[0]

    # immutable policy -------------------------------------------------
    def contact_row(self, campaign_id, candidate_id, candidate=None, profile=None):
        linked = self.cache.q("""SELECT c.* FROM contact_links l JOIN contacts c ON c.id=l.contact_id
                               WHERE l.campaign_id=? AND l.candidate_id=?""", (campaign_id, candidate_id))
        if linked: return linked[0]
        profile, candidate = profile or {}, candidate or {}
        email = (profile.get("contact_email") or "").lower() or None
        url = profile.get("profile_url") or candidate.get("profile_url")
        if email:
            rows = self.cache.q("SELECT * FROM contacts WHERE lower(email)=?", (email,))
            if rows: return rows[0]
        if url:
            rows = self.cache.q("SELECT * FROM contacts WHERE profile_url=?", (url,))
            if rows: return rows[0]
        return None

    def is_do_not_contact(self, campaign_id, candidate_id, candidate=None, profile=None):
        row = self.contact_row(campaign_id, candidate_id, candidate, profile)
        return bool(row and row["do_not_contact"]), row

    def set_do_not_contact(self, campaign_id, candidate_id, candidate, profile, value=True, reason=None):
        row = self.contact_row(campaign_id, candidate_id, candidate, profile)
        now = _now()
        if not row:
            name = candidate.get("name") or profile.get("name") or candidate_id
            email = profile.get("contact_email")
            with self.cache.lock:
                cur = self.cache.db.execute(
                    """INSERT INTO contacts (name,organization,role,email,profile_url,name_key,do_not_contact,
                       dnc_reason,source,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (name, candidate.get("organization"), candidate.get("role"), email,
                     candidate.get("profile_url"), _slug(name + " " + (candidate.get("organization") or "")),
                     int(value), reason, "campaign", now, now))
                contact_id = cur.lastrowid
                self.cache.db.execute("INSERT OR REPLACE INTO contact_links VALUES (?,?,?,?)",
                                      (campaign_id, candidate_id, contact_id, now))
            row = self.cache.q("SELECT * FROM contacts WHERE id=?", (contact_id,))[0]
        else:
            self.cache.x("UPDATE contacts SET do_not_contact=?,dnc_reason=?,updated_at=? WHERE id=?",
                         (int(value), reason, now, row["id"]))
            row = self.cache.q("SELECT * FROM contacts WHERE id=?", (row["id"],))[0]
        if value:
            self.invalidate_candidate_approvals(campaign_id, candidate_id, "contact is on the do-not-contact list")
        return row

    def invalidate_candidate_approvals(self, campaign_id, candidate_id, reason):
        changed = 0
        for d in self.cache.list_drafts(campaign_id):
            if d["candidate_id"] != candidate_id or d["status"] != "approved": continue
            issues = d["issues"] + ([reason] if reason not in d["issues"] else [])
            self.cache.upsert_draft(campaign_id, candidate_id, d["template_version"],
                                    status="needs_review", issues=issues)
            changed += 1
        return changed

    def record_contacted(self, campaign_id, candidate_id, candidate, profile, kind="sent"):
        row = self.set_do_not_contact(campaign_id, candidate_id, candidate, profile, False, None) \
            if not self.contact_row(campaign_id, candidate_id, candidate, profile) else \
            self.contact_row(campaign_id, candidate_id, candidate, profile)
        now = _now()
        self.cache.x("UPDATE contacts SET last_contacted_at=?,updated_at=? WHERE id=?", (now, now, row["id"]))
        self.cache.x("INSERT INTO interactions (contact_id,campaign_id,candidate_id,kind,detail,meta,at) VALUES (?,?,?,?,?,?,?)",
                     (row["id"], campaign_id, candidate_id, kind, None, "{}", now))

    def policies(self):
        return {"manual_review_required": True, "do_not_contact_enforced": True,
                "external_actions": "draft creation only after per-message human approval",
                "overridable": False}
