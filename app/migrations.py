"""Explicit, append-only schema migrations for SQLite databases and campaign JSON.

SQLite: each database module owns a MIGRATIONS list. Index i is applied when
PRAGMA user_version < i + 1, inside one transaction per step. A step is
either a SQL script (executescript) or a callable(db) for a data migration
too procedural for plain SQL (id remapping, matching against existing rows).
JSON: JSON_MIGRATIONS maps a schema_version n to a function that upgrades a
document from n to n + 1 in place.

Before any pending step runs, the current file is copied next to itself as
`<name>.v<old>-<utc timestamp>.bak`. Rollback = stop the app, copy the .bak
back over the original, and run the previous release (see docs/operations.md).
Never edit an applied migration; append a new one.
"""

import json
import logging
import os
import shutil
import sqlite3
import time
from pathlib import Path

from . import contracts

log = logging.getLogger("migrations")

# schema_version n -> fn(file_name, doc) mutating doc from n to n + 1. Empty while v1 is current.
JSON_MIGRATIONS = {}


def _stamp():
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())


def migrate_sqlite(db, path, migrations):
    """Bring `db` (connection to `path`) up to len(migrations). Returns the backup path or None."""
    version = db.execute("PRAGMA user_version").fetchone()[0]
    if version > len(migrations):
        raise RuntimeError(f"{Path(path).name} schema v{version} is newer than this app (v{len(migrations)})")
    if version == len(migrations):
        return None
    backup = None
    has_tables = db.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
    if has_tables and str(path) != ":memory:":
        backup = Path(f"{path}.v{version}-{_stamp()}.bak")
        dst = sqlite3.connect(backup)
        db.backup(dst)
        dst.close()
        os.chmod(backup, 0o600)
        log.info("backed up %s before migrating from v%s", Path(path).name, version)
    for i in range(version, len(migrations)):
        step = migrations[i]
        try:
            if callable(step):
                db.execute("BEGIN")
                step(db)
                db.execute(f"PRAGMA user_version={i + 1}")
                db.execute("COMMIT")
            else:
                db.executescript(f"BEGIN;\n{step}\nPRAGMA user_version={i + 1};\nCOMMIT;")
        except Exception:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise
        log.info("migrated %s to v%s", Path(path).name, i + 1)
    return backup


def migrate_json_file(path):
    """Upgrade one campaign JSON file in place. Returns True if it changed."""
    path = Path(path)
    doc = json.loads(path.read_text(encoding="utf-8"))
    version, target = doc.get("schema_version"), contracts.SCHEMA_VERSION
    if not isinstance(version, int) or version > target:
        raise RuntimeError(f"{path.name}: schema_version {version!r} is not supported by this app (v{target})")
    if version == target:
        return False
    missing = [v for v in range(version, target) if v not in JSON_MIGRATIONS]
    if missing:
        raise RuntimeError(f"{path.name}: no migration registered from schema_version {missing[0]}")
    bak = path.with_name(f"{path.name}.v{version}-{_stamp()}.bak")
    shutil.copy2(path, bak)
    os.chmod(bak, 0o600)
    for v in range(version, target):
        JSON_MIGRATIONS[v](path.name, doc)
        doc["schema_version"] = v + 1
    from .storage import atomic_write_json

    atomic_write_json(path, doc)
    log.info("migrated %s from v%s to v%s", path.name, version, target)
    return True


def migrate_campaigns(root):
    """Migrate every campaign under a data root (recursing into per-user roots). Returns changed file count."""
    changed = 0
    for path in sorted(Path(root).rglob("cmp_*/*.json")):
        if path.name in ("candidates.json", "research.json"):
            changed += migrate_json_file(path)
    return changed
