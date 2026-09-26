"""Campaign JSON repository: one candidates.json + research.json per campaign.

Only the app process writes these. Every update is a bounded
read-modify-write under a per-campaign lock, written to a sibling temp file,
fsynced and atomically replaced. That is O(n) file I/O and O(n) memory per
snapshot, fine at 20-100 records; migrate to JSONL/SQLite (with a
schema_version bump) if campaigns grow far larger.
"""
import json
import os
import re
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path

from .contracts import SCHEMA_VERSION, validate_files

ROOT = Path(__file__).resolve().parent.parent
ID_RE = re.compile(r"^cmp_[0-9]{8}_[a-f0-9]{8}$")


def now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def atomic_write_json(path, obj):
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


class CampaignStore:
    def __init__(self, data_root, template_root=ROOT):
        self.root = Path(data_root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.template_root = Path(template_root)
        self._locks = {}
        self._locks_guard = threading.Lock()

    def lock(self, campaign_id):
        with self._locks_guard:
            return self._locks.setdefault(campaign_id, threading.RLock())

    def dir(self, campaign_id):
        # Never trust a UI-supplied path: strict ID format + must resolve under root.
        if not isinstance(campaign_id, str) or not ID_RE.match(campaign_id):
            raise KeyError("invalid campaign id")
        d = (self.root / campaign_id).resolve()
        if d.parent != self.root:
            raise KeyError("invalid campaign id")
        return d

    def exists(self, campaign_id):
        try:
            return (self.dir(campaign_id) / "candidates.json").exists()
        except KeyError:
            return False

    def list_ids(self):
        return sorted((p.name for p in self.root.iterdir() if ID_RE.match(p.name)), reverse=True)

    def create(self, intake):
        cid = f"cmp_{datetime.now(timezone.utc):%Y%m%d}_{secrets.token_hex(4)}"
        d = self.dir(cid)
        d.mkdir()
        cand = json.loads((self.template_root / "candidates.json").read_text())
        res = json.loads((self.template_root / "research.json").read_text())
        for doc in (cand, res):
            if doc.get("schema_version") != SCHEMA_VERSION:
                raise RuntimeError("template schema_version mismatch; explicit migration required")
            doc["campaign_id"] = cid
        cand["intake"] = intake
        cand["created_at"] = now_iso()
        atomic_write_json(d / "candidates.json", cand)
        atomic_write_json(d / "research.json", res)
        return cid

    def _read(self, campaign_id, name):
        path = self.dir(campaign_id) / name
        if not path.exists():
            raise KeyError(campaign_id)
        doc = json.loads(path.read_text(encoding="utf-8"))
        if doc.get("schema_version") != SCHEMA_VERSION:
            raise RuntimeError(f"{name} schema_version {doc.get('schema_version')}: explicit migration required")
        return doc

    def candidates(self, campaign_id):
        return self._read(campaign_id, "candidates.json")

    def research(self, campaign_id):
        return self._read(campaign_id, "research.json")

    def update_candidates(self, campaign_id, fn):
        """fn(doc) mutates doc in place; result is written atomically."""
        with self.lock(campaign_id):
            doc = self.candidates(campaign_id)
            out = fn(doc)
            atomic_write_json(self.dir(campaign_id) / "candidates.json", doc)
            return out

    def update_research(self, campaign_id, fn):
        with self.lock(campaign_id):
            doc = self.research(campaign_id)
            out = fn(doc)
            atomic_write_json(self.dir(campaign_id) / "research.json", doc)
            return out

    def validate(self, campaign_id):
        with self.lock(campaign_id):
            return validate_files(self.candidates(campaign_id), self.research(campaign_id))
