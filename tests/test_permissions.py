"""Data directories are 0700 and data files 0600, even under a permissive umask (owned files:
app/storage.py, app/cache.py, app/workspace.py, app/migrations.py)."""

import base64
import os
import stat

import pytest

from app.cache import Cache
from app.storage import CampaignStore
from app.workspace import Workspace

DIR_MODE = 0o700
FILE_MODE = 0o600


def _mode(path):
    return stat.S_IMODE(path.stat().st_mode)


@pytest.fixture(autouse=True)
def permissive_umask():
    old = os.umask(0o000)  # everything would otherwise be created world-writable
    try:
        yield
    finally:
        os.umask(old)


def test_campaign_store_secures_new_and_existing_directories_and_files(tmp_path):
    root = tmp_path / "data"
    store = CampaignStore(root)
    assert _mode(root) == DIR_MODE

    cid = store.create({"mode": "research"})
    cdir = store.dir(cid)
    assert _mode(cdir) == DIR_MODE
    assert _mode(cdir / "candidates.json") == FILE_MODE
    assert _mode(cdir / "research.json") == FILE_MODE

    store.update_candidates(cid, lambda d: d)  # rewritten file must stay secured
    assert _mode(cdir / "candidates.json") == FILE_MODE

    # loosen an existing tree by hand, then confirm reopening re-secures it (not just new writes)
    os.chmod(root, 0o777)
    os.chmod(cdir, 0o777)
    os.chmod(cdir / "candidates.json", 0o666)
    CampaignStore(root)
    assert _mode(root) == DIR_MODE
    assert _mode(cdir) == DIR_MODE
    assert _mode(cdir / "candidates.json") == FILE_MODE


def test_cache_secures_database_file_and_parent_directory(tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    os.chmod(root, 0o777)
    db_path = root / "cache.sqlite3"
    Cache(db_path)
    assert _mode(db_path) == FILE_MODE
    assert _mode(root) == DIR_MODE

    os.chmod(db_path, 0o666)
    Cache(db_path)  # reopening an existing (legacy-permission) db re-secures it
    assert _mode(db_path) == FILE_MODE


def test_workspace_secures_attachment_directory_and_files(tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    cache = Cache(root / "cache.sqlite3")
    ws = Workspace(cache, root)
    assert _mode(ws.attachment_root) == DIR_MODE

    att = ws.add_attachment(
        {
            "filename": "deck.pdf",
            "media_type": "application/pdf",
            "kind": "club_deck",
            "content_base64": base64.b64encode(b"%PDF-1.4\nx\n%%EOF").decode(),
        }
    )
    path = ws.attachment_root / att["stored_name"]
    assert _mode(path) == FILE_MODE

    os.chmod(path, 0o666)
    Workspace(cache, root)  # reopening re-secures existing attachment files too
    assert _mode(path) == FILE_MODE


def test_migration_backup_files_are_secured(tmp_path):
    import sqlite3

    from app.cache import SCHEMA_V1

    root = tmp_path / "data"
    root.mkdir()
    db_path = root / "cache.sqlite3"
    raw = sqlite3.connect(db_path)
    raw.executescript(SCHEMA_V1)  # a pre-versioning database: has tables, so opening it backs one up first
    raw.commit()
    raw.close()

    Cache(db_path)
    backups = [p for p in root.iterdir() if p.name.startswith("cache.sqlite3.v")]
    assert backups and all(_mode(p) == FILE_MODE for p in backups)
