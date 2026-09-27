"""Backup/restore, deletion, and retention operations."""
import json
import sqlite3
import time

import pytest

from app import ops
from tests.support import run_research_flow, make_campaign

PASS = "correct horse battery staple"
TABLES = ("pages", "research_cache", "drafts", "jobs", "usage", "events")


def snapshot(root):
    """Everything a restore must reproduce: every JSON/text file plus every SQLite row."""
    out = {}
    for p in sorted(root.rglob("*")):
        if not p.is_file() or p.name.endswith(ops.SKIP_SUFFIXES) or ".bak" in p.name:
            continue
        rel = p.relative_to(root).as_posix()
        if p.suffix == ".sqlite3":
            db = sqlite3.connect(p)
            names = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
            out[rel] = {n: sorted(map(repr, db.execute(f"SELECT * FROM {n}"))) for n in names}
            db.close()
        else:
            out[rel] = p.read_bytes()
    return out


def test_backup_destroy_restore_reproduces_everything(env, tmp_path):
    client, svc, _ = env
    cid = make_campaign(client, "Rutgers/Princeton professors working on computational neurodevelopment")
    ids = run_research_flow(client, cid)
    client.post(f"/api/campaigns/{cid}/drafts/{ids[0]}/approve")
    root = svc.store.root
    (root / "attachments").mkdir()
    (root / "attachments" / "deck.pdf").write_bytes(b"%PDF-1.4 fake deck")  # arbitrary files are covered too
    before = snapshot(root)

    out = ops.backup(root, tmp_path / "b.hbk", PASS)
    raw = out.read_bytes()
    assert raw.startswith(ops.MAGIC) and b"avery" not in raw.lower() and b"candidates" not in raw  # encrypted
    assert oct(out.stat().st_mode & 0o777) == "0o600"

    # destructive modification while the app is still running
    client.delete(f"/api/campaigns/{cid}/candidates/{ids[1]}")
    client.patch(f"/api/campaigns/{cid}/drafts/{ids[0]}", json={"subject": "changed", "body": "changed"})
    (root / "attachments" / "deck.pdf").unlink()
    assert snapshot(root) != before

    with pytest.raises(ops.OpsError, match="stop the app"):
        ops.restore(root, out, PASS)  # the running app holds the data-root lock
    client.__exit__(None, None, None)  # stop the app

    with pytest.raises(ops.OpsError, match="passphrase"):
        ops.restore(root, out, "wrong passphrase!!")
    aside = ops.restore(root, out, PASS)
    assert snapshot(root) == before
    assert aside.exists() and aside.name.startswith("data.pre-restore-")


def test_restore_rejects_path_traversal(tmp_path):
    import io
    import tarfile

    from cryptography.fernet import Fernet

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        meta = json.dumps({"version": 1, "json_schema": 1, "sqlite_schema": 1}).encode()
        for name, body in ((ops.META, meta), ("../escape.txt", b"x")):
            info = tarfile.TarInfo(name)
            info.size = len(body)
            tar.addfile(info, io.BytesIO(body))
    salt = b"s" * 16
    (tmp_path / "evil.hbk").write_bytes(ops.MAGIC + salt + Fernet(ops._key(PASS, salt)).encrypt(buf.getvalue()))
    with pytest.raises(tarfile.OutsideDestinationError):
        ops.restore(tmp_path / "data", tmp_path / "evil.hbk", PASS)
    assert not (tmp_path / "escape.txt").exists()


def test_delete_campaign_removes_every_trace(env):
    client, svc, _ = env
    cid = make_campaign(client, "Rutgers/Princeton professors working on computational neurodevelopment")
    run_research_flow(client, cid)
    assert client.delete(f"/api/campaigns/{cid}").json() == {"deleted": cid}
    assert not (svc.store.root / cid).exists()
    for t in ("drafts", "jobs", "usage", "events"):
        assert svc.cache.q(f"SELECT count(*) AS n FROM {t} WHERE campaign_id=?", (cid,))[0]["n"] == 0
    assert svc.cache.q("SELECT count(*) AS n FROM research_cache WHERE key LIKE ?", (cid + "%",))[0]["n"] == 0
    assert client.get(f"/api/campaigns/{cid}").status_code == 404


def test_delete_refused_while_jobs_run(env):
    client, svc, _ = env
    cid = make_campaign(client, "startup founders working on climate tech", subtype="startup")
    svc.cache.put_job("job_x", cid, "research", "c_001", "running")
    assert client.delete(f"/api/campaigns/{cid}").status_code == 409


def test_forget_person_everywhere(env):
    client, svc, _ = env
    cids = [make_campaign(client, "Rutgers/Princeton professors working on computational neurodevelopment")
            for _ in range(2)]
    for cid in cids:
        run_research_flow(client, cid)
    name = svc.store.candidates(cids[0])["candidates"][0]["name"]
    email = svc.store.research(cids[0])["profiles"]["c_001"]["contact_email"]
    client.__exit__(None, None, None)

    assert ops.forget(svc.store.root, email=email) == 2
    for cid in cids:
        assert name not in json.dumps(svc.store.candidates(cid)) + json.dumps(svc.store.research(cid))
    db = sqlite3.connect(svc.store.root / "cache.sqlite3")
    dump = "\n".join(db.iterdump())
    assert email not in dump and name not in dump


def test_purge_applies_retention(tmp_path):
    from app.cache import Cache

    root = tmp_path / "data"
    root.mkdir()
    cache = Cache(root / "cache.sqlite3")
    old = time.time() - 200 * 86400
    cache.x("INSERT INTO events (campaign_id, at, message) VALUES ('c', ?, 'old')", (old,))
    cache.event("c", "new")
    cache.x("INSERT INTO jobs VALUES ('j1','c','research',NULL,'done',NULL,?,?)", (old, old))
    cache.x("INSERT INTO jobs VALUES ('j2','c','research',NULL,'interrupted',NULL,?,?)", (old, old))
    cache.x("INSERT INTO pages VALUES ('https://x.org/', ?, 1, 't', NULL)", (old,))
    cache.close()
    assert ops.purge(root, 90) == {"pages": 1, "research_cache": 0, "events": 1, "jobs": 1}
    cache = Cache(root / "cache.sqlite3")
    assert [e["message"] for e in cache.events("c")] == ["new"]
    assert [j["job_id"] for j in cache.jobs()] == ["j2"]  # resumable work is never purged


def test_check_schemas_passes_for_templates(tmp_path):
    assert ops.check_schemas(tmp_path / "none") == []
