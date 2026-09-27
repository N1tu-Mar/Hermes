"""CSV import validation and export. Pure functions; the service applies the rows.

Import is two-step: parse() returns valid rows plus per-row errors, the UI
shows that preview, and commit re-parses the same text so nothing is trusted
from the browser. One bad row never rejects the file.
"""
import csv
import io

from .contracts import EMAIL_RE, normalize_url
from .ledger import RELATIONSHIPS, name_key

MAX_BYTES = 2_000_000
MAX_ROWS = 5000
COLUMNS = {
    "contacts": ("name", "organization", "role", "email", "profile_url", "tags", "notes", "relationship",
                 "do_not_contact", "dnc_reason", "owner"),
    "candidates": ("name", "organization", "role", "email", "profile_url", "tags", "notes"),
}
ALIASES = {"company": "organization", "org": "organization", "institution": "organization", "title": "role",
           "url": "profile_url", "profile": "profile_url", "website": "profile_url", "e_mail": "email",
           "dnc": "do_not_contact", "status": "relationship", "full_name": "name"}
TRUE = {"1", "true", "yes", "y", "x"}


def _col(h):
    h = (h or "").strip().lower().replace(" ", "_").replace("-", "_")
    return ALIASES.get(h, h)


def parse(text, kind):
    if kind not in COLUMNS:
        raise ValueError("kind must be contacts or candidates")
    if len(text.encode()) > MAX_BYTES:
        raise ValueError(f"file larger than {MAX_BYTES // 1_000_000} MB")
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    if not reader.fieldnames:
        raise ValueError("empty file")
    header = [_col(h) for h in reader.fieldnames]
    if "name" not in header:
        raise ValueError("a 'name' column is required")
    reader.fieldnames = header
    known = [h for h in header if h in COLUMNS[kind]]
    valid, invalid, seen = [], [], {}
    for n, raw in enumerate(reader, start=2):  # row 1 is the header
        if n - 1 > MAX_ROWS:
            invalid.append({"row": n, "errors": [f"more than {MAX_ROWS} rows; rest ignored"], "raw": {}})
            break
        row = {k: (raw.get(k) or "").strip() for k in known}
        if not any(row.values()):
            continue  # blank line
        errors = []
        if None in raw:
            errors.append("more cells than header columns")
        if not row.get("name"):
            errors.append("name is required")
        if row.get("email"):
            row["email"] = row["email"].lower()
            if not EMAIL_RE.fullmatch(row["email"]):
                errors.append(f"invalid email {row['email']!r}")
        if row.get("profile_url") and not normalize_url(row["profile_url"]):
            errors.append("profile_url must start with http:// or https://")
        if row.get("relationship"):
            row["relationship"] = row["relationship"].lower()
            if row["relationship"] not in RELATIONSHIPS:
                errors.append(f"relationship must be one of {', '.join(RELATIONSHIPS)}")
        if "do_not_contact" in row:
            row["do_not_contact"] = row["do_not_contact"].lower() in TRUE
        for key in filter(None, (row.get("email"), normalize_url(row.get("profile_url")),
                                 row.get("name") and name_key(row["name"], row.get("organization")))):
            if key in seen:
                errors.append(f"duplicate of row {seen[key]}")
                break
        if errors:
            invalid.append({"row": n, "errors": errors, "raw": row})
            continue
        for key in filter(None, (row.get("email"), normalize_url(row.get("profile_url")),
                                 name_key(row["name"], row.get("organization")))):
            seen[key] = n
        valid.append({"row": n, "data": {k: v for k, v in row.items() if v not in ("", None)}})
    return {"kind": kind, "columns": known, "ignored_columns": [h for h in header if h not in COLUMNS[kind]],
            "valid": valid, "invalid": invalid}


def _cell(v):
    """Neutralize spreadsheet formula injection in exported cells."""
    s = "" if v is None else str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


def to_csv(rows, columns):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(columns)
    for r in rows:
        w.writerow([_cell(r.get(c)) for c in columns])
    return buf.getvalue()
