"""Backup contract: exclusive lock, streamed format, manifest + hashes, verified restore, cleanup on failure."""
import errno
import hashlib
import io
import json
import os
import sqlite3
import tarfile
import threading
import tracemalloc

import pytest

from app import cache as cache_mod
from app import contracts, ops
from app.api import _lock_data_root
from tests.test_ops import snapshot

PASS = "correct horse battery staple"


def make_root(tmp_path, name="data", jobs=3):
    root = tmp_path / name
    (root / "camp").mkdir(parents=True)
    (root / "camp" / "candidates.json").write_text('{"candidates": []}')
    c = cache_mod.Cache(root / "cache.sqlite3")
    for i in range(jobs):
        c.put_job(f"job_{i}", "camp", "research", f"c_{i}", "done")
    c.close()
    return root


def craft(path, members, meta=None, files=True):
    """Write a valid-encryption v2 archive with arbitrary members, to test what restore does with *authentic* junk."""
    salt = os.urandom(16)
    if meta is None:
        meta = {"version": 2, "json_schema": contracts.SCHEMA_VERSION, "sqlite_schema": len(cache_mod.MIGRATIONS)}
        if files:
            meta["files"] = {n: {"size": len(b), "sha256": hashlib.sha256(b).hexdigest()} for n, b in members.items()}
    with open(path, "wb") as f:
        f.write(ops.MAGIC + salt + ops.STREAM)
        enc = ops._EncWriter(f, ops._aead(PASS, salt))
        with tarfile.open(fileobj=enc, mode="w|gz") as tar:
            for n, body in {**members, ops.META: json.dumps(meta).encode()}.items():
                info = tarfile.TarInfo(n)
                info.size = len(body)
                tar.addfile(info, io.BytesIO(body))
        enc.close()
    return path


def test_backup_restore_drill(tmp_path, monkeypatch):
    """The real thing through the CLI: back up, destroy, restore, then use the restored data with the app code."""
    root = make_root(tmp_path)
    (root / "attachments").mkdir()
    (root / "attachments" / "deck.pdf").write_bytes(b"%PDF fake")
    before = snapshot(root)
    monkeypatch.setenv("DATA_ROOT", str(root))
    monkeypatch.setenv("HERMES_BACKUP_PASSPHRASE", PASS)
    out = tmp_path / "offsite" / "b.hbk"
    out.parent.mkdir()
    assert ops.main(["backup", str(out)]) == 0
    assert out.stat().st_mode & 0o777 == 0o600
    assert [p.name for p in out.parent.iterdir()] == ["b.hbk"]  # no staging, no .part

    (root / "camp" / "candidates.json").write_text("garbage")
    (root / "attachments" / "deck.pdf").unlink()
    assert ops.main(["restore", str(out)]) == 0
    assert snapshot(root) == before
    assert not (root / ops.META).exists()
    svc = ops._service(root)  # opens the restored DB through the normal migration path
    assert svc.cache.q("SELECT count(*) AS n FROM jobs")[0]["n"] == 3
    svc.cache.close()
    lock = _lock_data_root(root)  # and the restored root is lockable, i.e. startable
    lock.close()
    assert list(tmp_path.glob(".data.restore-*")) == []


def test_backup_holds_exclusive_lock_and_refuses_running_app(tmp_path, monkeypatch):
    root = make_root(tmp_path)
    app_lock = _lock_data_root(root)  # a running app
    with pytest.raises(ops.OpsError, match="stop the app"):
        ops.backup(root, tmp_path / "b.hbk", PASS, exclusive=True)
    assert not (tmp_path / "b.hbk").exists() and list(tmp_path.glob(".hermes-backup-*")) == []
    app_lock.close()

    seen = []
    real = ops._Hashing.read

    def spy(self, n):  # a writer trying to start mid-backup must be turned away
        try:
            _lock_data_root(root)
            seen.append("acquired")
        except RuntimeError:
            seen.append("blocked")
        return real(self, n)

    monkeypatch.setattr(ops._Hashing, "read", spy)
    ops.backup(root, tmp_path / "b.hbk", PASS, exclusive=True)
    assert seen and set(seen) == {"blocked"}
    _lock_data_root(root).close()  # released afterwards


def test_backup_under_concurrent_sqlite_writes_is_consistent(tmp_path):
    root = make_root(tmp_path)
    stop, written = threading.Event(), []

    def writer():
        db = sqlite3.connect(root / "cache.sqlite3", timeout=30)
        i = 0
        while not stop.is_set():
            with db:
                db.execute("INSERT INTO jobs (job_id, campaign_id, kind, candidate_id, status) VALUES (?,?,?,?,?)",
                           (f"w{i}", "camp", "research", f"c{i}", "done"))
            i += 1
            written.append(i)
        db.close()

    t = threading.Thread(target=writer)
    t.start()
    try:
        for n in range(3):
            ops.backup(root, tmp_path / f"b{n}.hbk", PASS)
    finally:
        stop.set()
        t.join()
    assert written  # the writer really ran during the backups
    ops.restore(tmp_path / "copy", tmp_path / "b2.hbk", PASS)
    db = sqlite3.connect(tmp_path / "copy" / "cache.sqlite3")
    assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert db.execute("SELECT count(*) FROM jobs").fetchone()[0] > 3  # some concurrent inserts were captured


def test_manifest_lists_every_file_with_hash(tmp_path):
    root = make_root(tmp_path)
    ops.backup(root, tmp_path / "b.hbk", PASS)
    ops.restore(tmp_path / "copy", tmp_path / "b.hbk", PASS)
    # manifest is consumed by restore, so rebuild it the way backup did and compare against the source
    src = tmp_path / "b.hbk"
    with open(src, "rb") as f:
        head = f.read(len(ops.MAGIC) + 16 + len(ops.STREAM))
        r = ops._DecReader(f, ops._aead(PASS, head[len(ops.MAGIC) : len(ops.MAGIC) + 16]))
        with tarfile.open(fileobj=r, mode="r|gz") as tar:
            for m in tar:
                if m.name == ops.META:
                    meta = json.load(tar.extractfile(m))
    assert meta["version"] == 2 and set(meta["files"]) == {"camp/candidates.json", "cache.sqlite3"}
    assert meta["files"]["camp/candidates.json"]["sha256"] == hashlib.sha256(b'{"candidates": []}').hexdigest()


def test_output_inside_data_root_rejected(tmp_path):
    root = make_root(tmp_path)
    (tmp_path / "link").symlink_to(root)
    for bad in (root / "b.hbk", root / "camp" / "b.hbk", tmp_path / "link" / "b.hbk", root):
        with pytest.raises(ops.OpsError, match="outside DATA_ROOT"):
            ops.backup(root, bad, PASS)
    assert sorted(p.name for p in root.iterdir()) == ["cache.sqlite3", "camp"]


def test_staging_is_private_and_removed(tmp_path, monkeypatch):
    root = make_root(tmp_path)
    modes = []
    real = ops.tempfile.mkdtemp

    def spy(*a, **k):
        d = real(*a, **k)
        modes.append((d, os.stat(d).st_mode & 0o777))
        return d

    monkeypatch.setattr(ops.tempfile, "mkdtemp", spy)
    out = tmp_path / "o"
    out.mkdir()
    ops.backup(root, out / "b.hbk", PASS)
    ops.restore(tmp_path / "copy", out / "b.hbk", PASS)
    assert len(modes) == 2 and all(m == 0o700 for _, m in modes)
    assert not any(os.path.exists(d) for d, _ in modes)


def test_disk_full_cleans_up_and_keeps_previous_backup(tmp_path, monkeypatch):
    root = make_root(tmp_path)
    (root / "big.bin").write_bytes(os.urandom(3 << 20))  # incompressible: several frames
    out = tmp_path / "o"
    out.mkdir()
    ops.backup(root, out / "b.hbk", PASS)
    good = (out / "b.hbk").read_bytes()

    calls = []
    real = ops._EncWriter._seal

    def full(self, data, final):
        calls.append(1)
        if len(calls) == 3:
            raise OSError(errno.ENOSPC, "No space left on device")
        return real(self, data, final)

    monkeypatch.setattr(ops._EncWriter, "_seal", full)
    with pytest.raises(OSError, match="No space"):
        ops.backup(root, out / "b.hbk", PASS)
    assert [p.name for p in out.iterdir()] == ["b.hbk"]  # no .part, no plaintext staging
    assert (out / "b.hbk").read_bytes() == good  # the previous good backup survives


def test_backup_streams_without_holding_archive_in_memory(tmp_path):
    root = make_root(tmp_path)
    (root / "big.bin").write_bytes(os.urandom(24 << 20))
    tracemalloc.start()
    ops.backup(root, tmp_path / "b.hbk", PASS)
    ops.restore(tmp_path / "copy", tmp_path / "b.hbk", PASS)
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    assert peak < 12 << 20, peak


@pytest.mark.parametrize("damage", ["flip", "truncate", "append", "header", "wrongpass"])
def test_corruption_is_rejected_and_live_data_untouched(tmp_path, damage):
    root = make_root(tmp_path)
    (root / "big.bin").write_bytes(os.urandom(3 << 20))
    ops.backup(root, tmp_path / "b.hbk", PASS)
    raw = bytearray((tmp_path / "b.hbk").read_bytes())
    pw = PASS
    if damage == "flip":
        raw[len(raw) // 2] ^= 1
    elif damage == "truncate":
        del raw[-1000:]
    elif damage == "append":
        raw += b"junk"
    elif damage == "header":
        raw[:4] = b"XXXX"
    else:
        pw = "wrong passphrase!!"
    (tmp_path / "bad.hbk").write_bytes(raw)
    before = snapshot(root)
    with pytest.raises(ops.OpsError):
        ops.restore(root, tmp_path / "bad.hbk", pw)
    assert snapshot(root) == before
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".")] == []


def test_truncation_at_frame_boundary_is_detected(tmp_path):
    root = make_root(tmp_path)
    (root / "big.bin").write_bytes(os.urandom(3 << 20))
    ops.backup(root, tmp_path / "b.hbk", PASS)
    raw = (tmp_path / "b.hbk").read_bytes()
    pos = len(ops.MAGIC) + 16 + len(ops.STREAM)
    last = pos
    while pos < len(raw):
        last = pos
        pos += 4 + int.from_bytes(raw[pos : pos + 4], "big")
    (tmp_path / "cut.hbk").write_bytes(raw[:last])  # drop the whole final frame
    with pytest.raises(ops.OpsError, match="corrupted"):
        ops.restore(tmp_path / "copy", tmp_path / "cut.hbk", PASS)
    assert not (tmp_path / "copy").exists()


def test_authentic_but_wrong_contents_rejected_before_swap(tmp_path):
    root = make_root(tmp_path)
    before = snapshot(root)
    good = {"a.txt": b"hello"}
    bad_hash = {"version": 2, "json_schema": 1, "sqlite_schema": 1, "files": {"a.txt": {"size": 5, "sha256": "0" * 64}}}
    cases = [
        (craft(tmp_path / "1", good, meta=bad_hash), "hash check"),
        (craft(tmp_path / "2", {**good, "extra.txt": b"x"}, meta={**bad_hash, "files": {
            "a.txt": {"size": 5, "sha256": hashlib.sha256(b"hello").hexdigest()}}}), "manifest"),
        (craft(tmp_path / "3", good, meta={"version": 2, "json_schema": 99, "sqlite_schema": 1, "files": {}}), "newer"),
        (craft(tmp_path / "4", good, meta={"version": 2, "json_schema": 1, "sqlite_schema": 99, "files": {}}), "newer"),
        (craft(tmp_path / "5", {"cache.sqlite3": b"not a database" * 100}), "integrity"),
        (craft(tmp_path / "6", good, meta=None, files=False).with_name("6"), "manifest"),  # v2 without file list
    ]
    for path, why in cases:
        with pytest.raises(ops.OpsError, match=why):
            ops.restore(root, path, PASS)
        assert snapshot(root) == before
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".")] == []


def test_newer_sqlite_schema_inside_backup_rejected(tmp_path):
    root = make_root(tmp_path)
    db = sqlite3.connect(root / "cache.sqlite3")
    db.execute(f"PRAGMA user_version={len(cache_mod.MIGRATIONS) + 1}")
    db.close()
    ops.backup(root, tmp_path / "b.hbk", PASS)
    before = snapshot(root)
    with pytest.raises(ops.OpsError, match="newer HERMES"):
        ops.restore(root, tmp_path / "b.hbk", PASS)
    assert snapshot(root) == before
