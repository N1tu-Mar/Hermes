"""Global cross-campaign memory in SQLite: contacts, interactions, identities, campaign metadata.

Campaign JSON files stay the source of truth for one campaign's candidates and
research. This module only links those candidates to durable people records.

Matching order: verified normalized email, then canonical profile URL (with the
same normalized name), then normalized name + organization as a fallback.
Anything ambiguous or conflicting becomes an open contact review instead of a
silent merge; the candidate stays unlinked until a human resolves it, and the
do-not-contact guard treats every contact named in an open review as a match.
Only emails verified on a page or entered by the user are stored.
"""
import json
import re
import threading
import time
from urllib.parse import urlsplit, urlunsplit

from .contracts import EMAIL_RE, normalize_url

RELATIONSHIPS = ("new", "contacted", "replied", "meeting", "declined", "bounced")
# kind -> relationship it implies (None = history only). Auto kinds are written by the service.
INTERACTIONS = {"draft": None, "approval": None, "gmail_draft": None, "invitation": "contacted",
                "followup": "contacted", "reply": "replied", "meeting": "meeting", "decline": "declined",
                "bounce": "bounced", "note": None}
MANUAL_INTERACTIONS = ("invitation", "followup", "reply", "meeting", "decline", "bounce", "note")
CONTACT_EDITABLE = ("name", "organization", "role", "email", "profile_url", "notes", "tags", "relationship",
                    "do_not_contact", "dnc_reason", "owner", "source")
IDENTITY_FIELDS = ("display_name", "biography", "organization", "role", "signature", "links", "default_ask", "reply_to")
TITLE_RE = re.compile(r"\b(dr|prof|professor|mr|ms|mrs|mx)\b\.?")


def norm_email(e):
    e = (e or "").strip().lower()
    return e if EMAIL_RE.fullmatch(e) else None


def canonical_url(u):
    """Scheme-, www-, fragment-, and trailing-slash-insensitive profile URL."""
    n = normalize_url(u)
    if not n:
        return None
    p = urlsplit(n)
    host = p.netloc[4:] if p.netloc.startswith("www.") else p.netloc
    return urlunsplit(("https", host, p.path, p.query, ""))


def norm_name(name):
    s = re.sub(r"\([^)]*\)", " ", (name or "").lower())  # "(demo)", "(she/her)"
    s = TITLE_RE.sub(" ", s)
    return " ".join(re.sub(r"[^a-z0-9]+", " ", s).split())


def norm_org(org):
    return " ".join(re.sub(r"[^a-z0-9]+", " ", re.sub(r"\([^)]*\)", " ", (org or "").lower())).split())


def name_key(name, org):
    return f"{norm_name(name)}|{norm_org(org)}"


def clean_tags(tags):
    if isinstance(tags, str):
        tags = re.split(r"[;,]", tags)
    out = []
    for t in tags or []:
        t = str(t).strip().lower()[:40]
        if t and t not in out:
            out.append(t)
    return out[:30]


def _contact(r):
    r = dict(r)
    r["tags"] = json.loads(r["tags"] or "[]")
    r["do_not_contact"] = bool(r["do_not_contact"])
    return r


def _identity(r):
    r = dict(r)
    r["links"] = json.loads(r["links"] or "[]")
    return r


class Ledger:
    def __init__(self, cache):
        self.c = cache
        # ponytail: one lock for match-then-insert; per-person locks if many writers ever race.
        self.lock = threading.RLock()

    # ------------------------------------------------------------ contacts
    def contact(self, contact_id):
        rows = self.c.q("SELECT * FROM people WHERE id=?", (contact_id,))
        if not rows:
            raise KeyError(f"no contact {contact_id}")
        return _contact(rows[0])

    def contacts(self, q=None, tag=None, dnc=None, relationship=None, limit=500):
        sql, args = "SELECT * FROM people WHERE 1=1", []
        if q:
            sql += " AND (name LIKE ? OR organization LIKE ? OR email LIKE ? OR notes LIKE ? OR tags LIKE ?)"
            args += [f"%{q}%"] * 5
        if tag:
            sql += " AND tags LIKE ?"
            args.append(f'%"{tag.lower()}"%')
        if dnc is not None:
            sql += " AND do_not_contact=?"
            args.append(int(bool(dnc)))
        if relationship:
            sql += " AND relationship=?"
            args.append(relationship)
        rows = [_contact(r) for r in self.c.q(sql + " ORDER BY updated_at DESC LIMIT ?", (*args, limit))]
        counts = {r["contact_id"]: r["n"] for r in self.c.q(
            "SELECT contact_id, COUNT(*) n FROM person_links GROUP BY contact_id")}
        for r in rows:
            r["campaign_count"] = counts.get(r["id"], 0)
        return rows

    def _validate(self, fields, current=None):
        out = {}
        for k, v in fields.items():
            if k not in CONTACT_EDITABLE:
                continue
            if k == "email":
                if v in (None, ""):
                    v = None
                else:
                    v = norm_email(v)
                    if not v:
                        raise ValueError("email is not a valid address")
                    other = self.c.q("SELECT id FROM people WHERE email=?", (v,))
                    if other and (current is None or other[0]["id"] != current):
                        raise ValueError(f"email already belongs to contact {other[0]['id']}")
            elif k == "profile_url":
                if v and not canonical_url(v):
                    raise ValueError("profile_url must be an http(s) URL")
                v = canonical_url(v)
            elif k == "tags":
                v = json.dumps(clean_tags(v))
            elif k == "relationship":
                if v not in RELATIONSHIPS:
                    raise ValueError(f"relationship must be one of {', '.join(RELATIONSHIPS)}")
            elif k == "do_not_contact":
                v = int(v in (True, 1, "1", "true", "yes", "y"))
            elif k == "name":
                v = str(v or "").strip()
                if not v:
                    raise ValueError("name is required")
            else:
                v = None if v is None else str(v).strip()[:5000]
            out[k] = v
        return out

    def create_contact(self, fields, source="manual"):
        f = self._validate({"source": source, **fields})
        if not f.get("name"):
            raise ValueError("name is required")
        now = time.time()
        f.update(name_key=name_key(f["name"], f.get("organization")), created_at=now, updated_at=now)
        cols = ", ".join(f)
        with self.c.lock:
            cur = self.c.db.execute(f"INSERT INTO people ({cols}) VALUES ({', '.join('?' * len(f))})", tuple(f.values()))
            return cur.lastrowid

    def update_contact(self, contact_id, patch):
        current = self.contact(contact_id)
        f = self._validate(patch, current=contact_id)
        if not f:
            return current
        if "name" in f or "organization" in f:
            f["name_key"] = name_key(f.get("name", current["name"]), f.get("organization", current["organization"]))
        if f.get("do_not_contact") and not current["do_not_contact"]:
            self.log(contact_id, "note", f"Marked do-not-contact{': ' + f['dnc_reason'] if f.get('dnc_reason') else ''}")
        elif f.get("do_not_contact") == 0 and current["do_not_contact"]:
            self.log(contact_id, "note", "Do-not-contact flag removed")
        f["updated_at"] = time.time()
        self.c.x(f"UPDATE people SET {', '.join(k + '=?' for k in f)} WHERE id=?", (*f.values(), contact_id))
        return self.contact(contact_id)

    def contact_detail(self, contact_id):
        c = self.contact(contact_id)
        links = self.c.q("""SELECT l.campaign_id, l.candidate_id, l.linked_at, m.name, m.archived
                            FROM person_links l LEFT JOIN campaign_meta m USING (campaign_id)
                            WHERE l.contact_id=? ORDER BY l.linked_at""", (contact_id,))
        return {"contact": c, "campaigns": links, "timeline": self.timeline(contact_id),
                "reviews": [r for r in self.reviews() if contact_id in r["options"]]}

    # ------------------------------------------------------------ matching
    def _ids(self, sql, args):
        return [r["id"] for r in self.c.q(sql, args)]

    def match(self, name, organization=None, email=None, profile_url=None):
        """-> ("match", id) | ("new", None) | ("review", ids, reason)."""
        email, url, nn = norm_email(email), canonical_url(profile_url), norm_name(name)
        by_email = self._ids("SELECT id FROM people WHERE email=?", (email,)) if email else []
        by_url = self._ids("SELECT id FROM people WHERE profile_url=?", (url,)) if url else []
        if by_email:
            cid = by_email[0]
            if by_url and cid not in by_url:
                return ("review", sorted({cid, *by_url}), "email and profile URL match different contacts")
            return ("match", cid)
        if by_url:
            same = [i for i in by_url if norm_name(self.contact(i)["name"]) == nn]
            if len(same) == 1 and not self._email_conflict(same[0], email):
                return ("match", same[0])
            return ("review", by_url, "same profile URL but a different name or email")
        by_name = self._ids("SELECT id FROM people WHERE name_key=?", (name_key(name, organization),))
        if not by_name:
            return ("new", None)
        if len(by_name) == 1:
            c = self.contact(by_name[0])
            if not self._email_conflict(c["id"], email) and not (url and c["profile_url"] and c["profile_url"] != url):
                return ("match", c["id"])
            return ("review", by_name, "same name and organization but a different email or profile URL")
        return ("review", by_name, "several contacts share this name and organization")

    def _email_conflict(self, contact_id, email):
        existing = self.contact(contact_id)["email"]
        return bool(email and existing and existing != email)

    def _fill(self, contact_id, email=None, profile_url=None, role=None):
        """Add missing identifiers to a matched contact; never overwrite."""
        c = self.contact(contact_id)
        patch = {k: v for k, v in (("email", norm_email(email)), ("profile_url", canonical_url(profile_url)),
                                   ("role", role)) if v and not c[k]}
        if patch.get("email") and self._ids("SELECT id FROM people WHERE email=?", (patch["email"],)):
            patch.pop("email")
        if patch:
            self.update_contact(contact_id, patch)

    def _open_review(self, campaign_id, candidate_id, person, options, reason):
        dup = self.c.q("SELECT id FROM person_reviews WHERE status='open' AND campaign_id IS ? AND candidate_id IS ? "
                       "AND person=?", (campaign_id, candidate_id, json.dumps(person, sort_keys=True)))
        if dup:
            return dup[0]["id"]
        with self.c.lock:
            return self.c.db.execute(
                "INSERT INTO person_reviews (campaign_id, candidate_id, person, options, reason, created_at) "
                "VALUES (?,?,?,?,?,?)", (campaign_id, candidate_id, json.dumps(person, sort_keys=True),
                                         json.dumps(list(options)), reason, time.time())).lastrowid

    def linked(self, campaign_id, candidate_id):
        rows = self.c.q("SELECT contact_id FROM person_links WHERE campaign_id=? AND candidate_id=?",
                        (campaign_id, candidate_id))
        return rows[0]["contact_id"] if rows else None

    def _link(self, campaign_id, candidate_id, contact_id):
        self.c.x("INSERT OR REPLACE INTO person_links VALUES (?,?,?,?)", (campaign_id, candidate_id, contact_id, time.time()))

    def link_candidate(self, campaign_id, cand, email=None, source=None):
        """Link a campaign candidate to a global contact. Returns contact id, or None when sent to review."""
        person = {"name": cand.get("name"), "organization": cand.get("organization"), "role": cand.get("role"),
                  "profile_url": canonical_url(cand.get("profile_url")), "email": norm_email(email)}
        with self.lock:
            cur = self.linked(campaign_id, cand["candidate_id"])
            if cur:
                if person["email"]:
                    owner = self._ids("SELECT id FROM people WHERE email=?", (person["email"],))
                    if owner and owner[0] != cur:
                        self._open_review(campaign_id, cand["candidate_id"], person, [cur, owner[0]],
                                          "verified email belongs to a different contact")
                        return cur
                self._fill(cur, person["email"], person["profile_url"], person["role"])
                return cur
            if self.c.q("SELECT 1 FROM person_reviews WHERE status='open' AND campaign_id=? AND candidate_id=?",
                        (campaign_id, cand["candidate_id"])):
                return None  # already waiting on a human
            m = self.match(person["name"], person["organization"], person["email"], person["profile_url"])
            if m[0] == "review":
                self._open_review(campaign_id, cand["candidate_id"], person, m[1], m[2])
                return None
            cid = m[1] if m[0] == "match" else self.create_contact(
                {k: v for k, v in person.items() if v}, source=source or f"campaign:{campaign_id}")
            if m[0] == "match":
                self._fill(cid, person["email"], person["profile_url"], person["role"])
            self._link(campaign_id, cand["candidate_id"], cid)
            return cid

    def upsert_person(self, fields, source):
        """CSV/manual entry without a campaign. -> (contact_id | None, status)."""
        with self.lock:
            m = self.match(fields.get("name"), fields.get("organization"), fields.get("email"), fields.get("profile_url"))
            if m[0] == "review":
                person = {k: fields.get(k) for k in ("name", "organization", "role", "profile_url", "email")}
                self._open_review(None, None, person, m[1], m[2])
                return None, "review"
            if m[0] == "new":
                return self.create_contact(fields, source=source), "created"
            cid, c = m[1], self.contact(m[1])
            self._fill(cid, fields.get("email"), fields.get("profile_url"), fields.get("role"))
            patch = {}
            if fields.get("notes") and fields["notes"] not in c["notes"]:
                patch["notes"] = (c["notes"] + "\n" + fields["notes"]).strip()
            if fields.get("tags"):
                patch["tags"] = c["tags"] + clean_tags(fields["tags"])
            if fields.get("do_not_contact") in (True, 1, "1", "true", "yes", "y"):
                patch.update(do_not_contact=True, dnc_reason=fields.get("dnc_reason") or c["dnc_reason"])
            if fields.get("relationship"):
                patch["relationship"] = fields["relationship"]
            if patch:
                self.update_contact(cid, patch)
            return cid, "matched"

    # ------------------------------------------------------------ reviews
    def reviews(self, status="open"):
        rows = self.c.q("SELECT * FROM person_reviews WHERE status=? ORDER BY created_at", (status,))
        for r in rows:
            r["person"], r["options"] = json.loads(r["person"]), json.loads(r["options"])
            r["option_contacts"] = [self.c.q("SELECT id, name, organization, email, profile_url, do_not_contact "
                                             "FROM people WHERE id=?", (i,))[0] for i in r["options"]]
        return rows

    def resolve_review(self, review_id, contact_id=None):
        """contact_id = merge into that existing contact; None = this is a different, new person."""
        with self.lock:
            rows = self.c.q("SELECT * FROM person_reviews WHERE id=? AND status='open'", (review_id,))
            if not rows:
                raise KeyError(f"no open review {review_id}")
            r = rows[0]
            person = json.loads(r["person"])
            if contact_id is None:
                if person.get("email") and self._ids("SELECT id FROM people WHERE email=?", (person["email"],)):
                    raise ValueError("that email already belongs to an existing contact; merge into it instead")
                contact_id = self.create_contact({k: v for k, v in person.items() if v}, source="review")
            else:
                self.contact(contact_id)
                self._fill(contact_id, person.get("email"), person.get("profile_url"), person.get("role"))
            if r["candidate_id"]:
                self._link(r["campaign_id"], r["candidate_id"], contact_id)
            self.c.x("UPDATE person_reviews SET status='resolved', resolved_contact_id=?, resolved_at=? WHERE id=?",
                     (contact_id, time.time(), review_id))
            return self.contact(contact_id)

    # ------------------------------------------------------------ do-not-contact
    def dnc_block(self, campaign_id, candidate_id):
        """Reason string when this candidate must not be contacted, else None."""
        cid = self.linked(campaign_id, candidate_id)
        ids = [cid] if cid else []
        for r in self.c.q("SELECT options FROM person_reviews WHERE status='open' AND campaign_id=? AND candidate_id=?",
                          (campaign_id, candidate_id)):
            ids += json.loads(r["options"])
        for i in ids:
            c = self.contact(i)
            if c["do_not_contact"]:
                why = f" ({c['dnc_reason']})" if c["dnc_reason"] else ""
                return (f"{c['name']} is marked do-not-contact{why}" if i == cid else
                        f"possible match to do-not-contact contact {c['name']}; resolve the contact review first")
        return None

    # ------------------------------------------------------------ interactions
    def log(self, contact_id, kind, detail="", campaign_id=None, candidate_id=None, meta=None, at=None):
        if kind not in INTERACTIONS:
            raise ValueError(f"kind must be one of {', '.join(INTERACTIONS)}")
        at = float(at) if at else time.time()
        self.c.x("INSERT INTO interactions (contact_id, campaign_id, candidate_id, kind, detail, meta, at) "
                 "VALUES (?,?,?,?,?,?,?)", (contact_id, campaign_id, candidate_id, kind, (detail or "")[:5000],
                                            json.dumps(meta) if meta else None, at))
        rel = INTERACTIONS[kind]
        if rel:
            sets, args = "relationship=?, updated_at=?", [rel, time.time()]
            if rel == "contacted":
                sets += ", last_contacted_at=MAX(COALESCE(last_contacted_at, 0), ?)"
                args.append(at)
            self.c.x(f"UPDATE people SET {sets} WHERE id=?", (*args, contact_id))

    def log_for(self, campaign_id, candidate_id, kind, detail="", meta=None):
        """Service hook: record against the linked contact if there is one."""
        cid = self.linked(campaign_id, candidate_id)
        if cid:
            self.log(cid, kind, detail, campaign_id, candidate_id, meta)

    def timeline(self, contact_id):
        rows = self.c.q("""SELECT i.*, m.name AS campaign_name FROM interactions i
                           LEFT JOIN campaign_meta m USING (campaign_id) WHERE contact_id=? ORDER BY at, id""", (contact_id,))
        for r in rows:
            r["meta"] = json.loads(r["meta"]) if r["meta"] else None
        return rows

    # ------------------------------------------------------------ identities
    def identities(self):
        return [_identity(r) for r in self.c.q("SELECT * FROM identities ORDER BY display_name")]

    def identity(self, identity_id):
        rows = self.c.q("SELECT * FROM identities WHERE id=?", (identity_id,))
        if not rows:
            raise KeyError(f"no sender identity {identity_id}")
        return _identity(rows[0])

    @staticmethod
    def _clean_identity(data, partial=False):
        out = {}
        for k in IDENTITY_FIELDS:
            if k not in data:
                continue
            v = data[k]
            if k == "links":
                v = [s.strip() for s in (v.split(",") if isinstance(v, str) else v or []) if str(s).strip()]
                bad = [u for u in v if not normalize_url(u)]
                if bad:
                    raise ValueError(f"links must be http(s) URLs: {bad[0]}")
                v = json.dumps(v)
            elif k == "reply_to":
                v = (v or "").strip() or None
                if v and not EMAIL_RE.fullmatch(v):
                    raise ValueError("reply_to must be an email address")
            else:
                v = (str(v).strip() if v is not None else "")[:4000] or None
            out[k] = v
        if not partial or "display_name" in out:
            if not out.get("display_name"):
                raise ValueError("display_name is required")
        if not partial or "biography" in out:
            if not out.get("biography"):
                raise ValueError("biography is required (it is the sender context in every email)")
        return out

    def create_identity(self, data):
        f = self._clean_identity(data)
        f["created_at"] = f["updated_at"] = time.time()
        with self.c.lock:
            return self.c.db.execute(f"INSERT INTO identities ({', '.join(f)}) VALUES ({', '.join('?' * len(f))})",
                                     tuple(f.values())).lastrowid

    def update_identity(self, identity_id, data):
        self.identity(identity_id)
        f = self._clean_identity(data, partial=True)
        f["updated_at"] = time.time()
        self.c.x(f"UPDATE identities SET {', '.join(k + '=?' for k in f)} WHERE id=?", (*f.values(), identity_id))
        return self.identity(identity_id)

    def delete_identity(self, identity_id):
        self.identity(identity_id)
        self.c.x("DELETE FROM identities WHERE id=?", (identity_id,))

    # ------------------------------------------------------------ campaign metadata
    def campaign_meta(self, campaign_id):
        rows = self.c.q("SELECT * FROM campaign_meta WHERE campaign_id=?", (campaign_id,))
        return rows[0] if rows else {"campaign_id": campaign_id, "name": None, "archived": 0}

    def set_campaign_meta(self, campaign_id, **fields):
        self.c.x("INSERT OR IGNORE INTO campaign_meta (campaign_id) VALUES (?)", (campaign_id,))
        fields["updated_at"] = time.time()
        self.c.x(f"UPDATE campaign_meta SET {', '.join(k + '=?' for k in fields)} WHERE campaign_id=?",
                 (*fields.values(), campaign_id))

    def purge_campaign(self, campaign_id):
        """Drop per-campaign SQLite rows. Contacts and their interaction history survive."""
        for sql in ("DELETE FROM drafts WHERE campaign_id=?", "DELETE FROM jobs WHERE campaign_id=?",
                    "DELETE FROM usage WHERE campaign_id=?", "DELETE FROM events WHERE campaign_id=?",
                    "DELETE FROM person_links WHERE campaign_id=?", "DELETE FROM campaign_meta WHERE campaign_id=?",
                    "DELETE FROM person_reviews WHERE campaign_id=? AND status='open'"):
            self.c.x(sql, (campaign_id,))
        self.c.x("DELETE FROM research_cache WHERE key LIKE ?", (campaign_id + ":%",))
