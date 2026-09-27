import json
import sqlite3

import pytest

from app import contracts, migrations
from app.cache import MIGRATIONS, Cache
from app.storage import CampaignStore


def test_atomic_write_and_campaign_isolation(tmp_path):
    store = CampaignStore(tmp_path)
    a = store.create({"mode": "research"})
    b = store.create({"mode": "outreach"})
    assert a != b
    store.update_candidates(
        a, lambda d: d["candidates"].append({"candidate_id": "c_001", "name": "X", "status": "discovered"})
    )
    assert store.candidates(b)["candidates"] == []
    assert store.research(a)["campaign_id"] == a
    assert not list((tmp_path / a).glob("*.tmp"))
    with pytest.raises(KeyError):
        store.dir("../etc")
    with pytest.raises(KeyError):
        store.dir("cmp_20260926_deadbeef/../../x")
    # root templates stay untouched
    root = json.loads((store.template_root / "candidates.json").read_text())
    assert root["campaign_id"] is None and root["candidates"] == []


def test_sqlite_migration_backs_up_legacy_db_first(tmp_path):
    path = tmp_path / "cache.sqlite3"
    legacy = sqlite3.connect(path)  # pre-versioning database: tables exist, user_version 0
    legacy.executescript(MIGRATIONS[0])
    legacy.execute("INSERT INTO events (campaign_id, at, message) VALUES ('cmp_x', 1, 'kept')")
    legacy.commit()
    legacy.close()

    cache = Cache(path)
    assert cache.q("PRAGMA user_version")[0]["user_version"] == len(MIGRATIONS)
    baks = list(tmp_path.glob("cache.sqlite3.v0-*.bak"))
    assert len(baks) == 1
    assert sqlite3.connect(baks[0]).execute("SELECT message FROM events").fetchone()[0] == "kept"
    cache.close()
    Cache(path).close()  # already current: no second backup
    assert len(list(tmp_path.glob("*.bak"))) == 1


def test_sqlite_newer_than_app_refused(tmp_path):
    db = sqlite3.connect(tmp_path / "c.sqlite3")
    db.execute(f"PRAGMA user_version={len(MIGRATIONS) + 1}")
    db.close()
    with pytest.raises(RuntimeError, match="newer"):
        Cache(tmp_path / "c.sqlite3")


def test_failed_sqlite_step_rolls_back(tmp_path):
    db = sqlite3.connect(tmp_path / "c.sqlite3", isolation_level=None)
    with pytest.raises(sqlite3.OperationalError):
        migrations.migrate_sqlite(db, tmp_path / "c.sqlite3", ["CREATE TABLE a (x);", "CREATE TABLE b (; broken"])
    assert db.execute("PRAGMA user_version").fetchone()[0] == 1  # step 1 committed, step 2 rolled back
    assert db.execute("SELECT count(*) FROM sqlite_master WHERE name='b'").fetchone()[0] == 0


def test_json_migration_backs_up_and_upgrades(tmp_path, monkeypatch):
    store = CampaignStore(tmp_path)
    cid = store.create({"mode": "research"})
    monkeypatch.setattr(contracts, "SCHEMA_VERSION", 2)
    monkeypatch.setitem(migrations.JSON_MIGRATIONS, 1, lambda name, doc: doc.setdefault("notes", "added in v2"))
    assert migrations.migrate_campaigns(tmp_path) == 2
    doc = json.loads((tmp_path / cid / "candidates.json").read_text())
    assert doc["schema_version"] == 2 and doc["notes"] == "added in v2"
    bak = next((tmp_path / cid).glob("candidates.json.v1-*.bak"))
    assert json.loads(bak.read_text())["schema_version"] == 1
    assert migrations.migrate_campaigns(tmp_path) == 0  # idempotent


def test_json_without_registered_migration_refused(tmp_path, monkeypatch):
    store = CampaignStore(tmp_path)
    store.create({"mode": "research"})
    monkeypatch.setattr(contracts, "SCHEMA_VERSION", 2)
    with pytest.raises(RuntimeError, match="no migration registered"):
        migrations.migrate_campaigns(tmp_path)
