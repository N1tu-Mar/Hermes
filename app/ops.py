"""Operator CLI. Run with the app stopped for anything that writes.

  python -m app.ops migrate                     apply pending SQLite + JSON migrations (backs up first)
  python -m app.ops check-schemas               validate templates and every campaign's JSON files
  python -m app.ops backup FILE                 encrypted snapshot of the whole data root (app must be stopped; FILE outside it)
  python -m app.ops restore FILE                replace the data root with a backup (old root kept aside)
  python -m app.ops purge [--days N] [--campaigns-older-than N]
  python -m app.ops forget (--email ADDR | --name "Full Name")   delete a person from every campaign
  python -m app.ops delete-campaign CAMPAIGN_ID
  python -m app.ops gen-secret-key              print a new HERMES_SECRET_KEY
  python -m app.ops user-add|user-passwd|user-disable|user-enable|user-delete NAME   (remote mode)
  python -m app.ops user-list                   (remote mode)

Passphrases/passwords come from HERMES_BACKUP_PASSPHRASE / HERMES_NEW_PASSWORD or an interactive prompt.
"""

import argparse
import base64
import getpass
import hashlib
import io
import json
import os
import shutil
import sqlite3
import sys
import tarfile
import tempfile
import time
import zlib
from pathlib import Path

from . import cache as cache_mod
from . import contracts
from .config import ROOT, ConfigError, load_config, load_env
from .migrations import migrate_campaigns

MAGIC = b"HERMES-BACKUP-1\n"
META = "hermes-backup.json"
SKIP_SUFFIXES = ("-wal", "-shm", ".tmp", ".bak", ".lock", ".write-probe")


class OpsError(Exception):
    pass


def _key(passphrase, salt):
    raw = hashlib.scrypt(passphrase.encode(), salt=salt, n=2**15, r=8, p=1, maxmem=64 * 1024 * 1024, dklen=32)
    return base64.urlsafe_b64encode(raw)


def _secret(env_name, prompt, confirm=False, minimum=12):
    value = os.environ.get(env_name)
    if value is None:
        value = getpass.getpass(prompt)
        if confirm and getpass.getpass("Again: ") != value:
            raise OpsError("entries did not match")
    if len(value) < minimum:
        raise OpsError(f"must be at least {minimum} characters")
    return value


def _lock(root):
    from .api import _lock_data_root

    try:
        return _lock_data_root(root)
    except RuntimeError as e:
        raise OpsError(f"{e}; stop the app first") from None


def _roots(data_root):
    """Every service data root: the root itself (local) plus users/<id> (remote)."""
    roots = [data_root]
    users = data_root / "users"
    if users.is_dir():
        roots += sorted(p for p in users.iterdir() if p.is_dir())
    return roots


def _service(root):
    from .campaigns import CampaignService
    from .storage import CampaignStore

    return CampaignService(CampaignStore(root), cache_mod.Cache(root / "cache.sqlite3"), None, None)


# ---------------------------------------------------------------- backup / restore
# Format 2: MAGIC + salt + STREAM + frames. Each frame is a 4-byte length plus an AES-GCM chunk (nonce = frame
# counter, AAD = final-frame flag), so reordering, dropping or truncating frames fails authentication. Only one
# chunk is ever held in memory. Format 1 (one Fernet token over the whole tar.gz) is still readable by restore.
STREAM = b"STREAM2\n"  # a Fernet token starts with "gAAAA", so this cannot collide with format 1
CHUNK = 1 << 20


def _aead(passphrase, salt):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    return AESGCM(base64.urlsafe_b64decode(_key(passphrase, salt)))


def _nonce(n):
    return n.to_bytes(12, "big")


class _EncWriter:
    """File-like sink: buffers one chunk, seals full chunks as non-final frames, close() seals the final one."""

    def __init__(self, f, aead):
        self.f, self.aead, self.n, self.buf = f, aead, 0, bytearray()

    def _seal(self, data, final):
        ct = self.aead.encrypt(_nonce(self.n), bytes(data), b"\x01" if final else b"\x00")
        self.f.write(len(ct).to_bytes(4, "big") + ct)
        self.n += 1

    def write(self, data):
        self.buf += data
        while len(self.buf) > CHUNK:  # strictly greater: the last chunk is always held back for close()
            self._seal(self.buf[:CHUNK], False)
            del self.buf[:CHUNK]
        return len(data)

    def close(self):
        self._seal(self.buf, True)
        self.buf = bytearray()


class _DecReader:
    """File-like source over the frames; the frame that is followed by EOF must authenticate as final."""

    def __init__(self, f, aead):
        self.f, self.aead, self.n, self.buf, self.head = f, aead, 0, b"", f.read(4)

    def _fill(self):
        if not self.head:
            return False
        if len(self.head) < 4 or int.from_bytes(self.head, "big") > CHUNK + 16:
            raise ValueError("bad frame header")
        ln = int.from_bytes(self.head, "big")
        ct = self.f.read(ln)
        if len(ct) != ln:
            raise ValueError("truncated backup")
        self.head = self.f.read(4)
        self.buf += self.aead.decrypt(_nonce(self.n), ct, b"\x00" if self.head else b"\x01")
        self.n += 1
        return True

    def read(self, n):
        while len(self.buf) < n and self._fill():
            pass
        out, self.buf = self.buf[:n], self.buf[n:]
        return out


class _Hashing:
    def __init__(self, f):
        self.f, self.h = f, hashlib.sha256()

    def read(self, n):
        data = self.f.read(n)
        self.h.update(data)
        return data


def backup(data_root, out, passphrase, *, exclusive=False):
    """Encrypted, manifest-carrying snapshot of every file under data_root, streamed (never fully in memory).

    exclusive=True (what the CLI uses) takes the data-root lock, so no app process can write while it runs and the
    JSON files, SQLite files and attachments are one consistent point in time. Without it, each SQLite file is
    still copied with the online backup API but files are not consistent with each other.
    """
    data_root = Path(os.path.realpath(data_root))
    out = Path(os.path.realpath(out))
    if out == data_root or data_root in out.parents:
        raise OpsError(f"backup output must be outside DATA_ROOT ({data_root})")
    lock = _lock(data_root) if exclusive else None
    staging = Path(tempfile.mkdtemp(prefix=".hermes-backup-", dir=out.parent))  # mkdtemp is 0700
    part = out.with_name(out.name + ".part")
    try:
        salt = os.urandom(16)
        files = {}
        fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(MAGIC + salt + STREAM)
            enc = _EncWriter(f, _aead(passphrase, salt))
            with tarfile.open(fileobj=enc, mode="w|gz") as tar:
                for p in sorted(data_root.rglob("*")):
                    if not p.is_file() or p.name.endswith(SKIP_SUFFIXES) or ".bak" in p.name:
                        continue
                    rel = p.relative_to(data_root).as_posix()
                    src = p
                    if p.suffix == ".sqlite3":
                        src = staging / "snap.sqlite3"
                        a, b = sqlite3.connect(p), sqlite3.connect(src)
                        a.backup(b)
                        a.close(), b.close()
                    with open(src, "rb") as fh:
                        info = tar.gettarinfo(arcname=rel, fileobj=fh)
                        hf = _Hashing(fh)
                        tar.addfile(info, hf)
                    files[rel] = {"size": info.size, "sha256": hf.h.hexdigest()}
                    if src is not p:
                        src.unlink()
                meta = {
                    "version": 2,
                    "created_at": time.time(),
                    "json_schema": contracts.SCHEMA_VERSION,
                    "sqlite_schema": len(cache_mod.MIGRATIONS),
                    "files": files,
                }
                body = json.dumps(meta, sort_keys=True).encode()
                info = tarfile.TarInfo(META)  # last member: the manifest can only be written once hashes are known
                info.size = len(body)
                tar.addfile(info, io.BytesIO(body))
            enc.close()
            f.flush()
            os.fsync(f.fileno())
        os.replace(part, out)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        part.unlink(missing_ok=True)
        if lock:
            lock.close()
    return out


def _verify(root):
    """Everything a restore is about to trust, checked before the live data is touched."""
    manifest = root / META
    if not manifest.is_file():
        raise OpsError("backup has no manifest")
    meta = json.loads(manifest.read_text())
    if meta["json_schema"] > contracts.SCHEMA_VERSION or meta["sqlite_schema"] > len(cache_mod.MIGRATIONS):
        raise OpsError("backup was made by a newer HERMES; upgrade the app before restoring")
    manifest.unlink()
    found = {}
    for p in root.rglob("*"):
        if p.is_symlink() or (not p.is_dir() and not p.is_file()):
            raise OpsError(f"backup contains a non-regular file: {p.relative_to(root)}")
        if p.is_file():
            found[p.relative_to(root).as_posix()] = p
    if meta.get("version") == 2:
        if set(found) != set(meta["files"]):
            diff = sorted(set(found) ^ set(meta["files"]))
            raise OpsError(f"backup contents do not match its manifest: {diff[:3]}")
        for rel, want in meta["files"].items():
            h = hashlib.sha256()
            with open(found[rel], "rb") as fh:
                while chunk := fh.read(CHUNK):
                    h.update(chunk)
            if h.hexdigest() != want["sha256"] or found[rel].stat().st_size != want["size"]:
                raise OpsError(f"backup file failed its hash check: {rel}")
    for rel, p in found.items():
        if p.suffix != ".sqlite3":
            continue
        db = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        try:
            ok = db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            version = db.execute("PRAGMA user_version").fetchone()[0]
        except sqlite3.DatabaseError:
            ok, version = False, 0
        finally:
            db.close()
        if not ok:
            raise OpsError(f"backup database failed integrity_check: {rel}")
        if p.name == "cache.sqlite3" and version > len(cache_mod.MIGRATIONS):
            raise OpsError("backup was made by a newer HERMES; upgrade the app before restoring")


def _extract(inp, passphrase, staging):
    from cryptography.exceptions import InvalidTag
    from cryptography.fernet import Fernet, InvalidToken

    corrupt = OpsError("wrong passphrase or corrupted backup")
    with open(inp, "rb") as f:
        head = f.read(len(MAGIC) + 16 + len(STREAM))
        if not head.startswith(MAGIC):
            raise OpsError("not a HERMES backup file")
        salt = head[len(MAGIC) : len(MAGIC) + 16]
        if head[len(MAGIC) + 16 :] != STREAM:  # format 1: a single in-memory Fernet token
            try:
                raw = Fernet(_key(passphrase, salt)).decrypt(head[len(MAGIC) + 16 :] + f.read())
            except InvalidToken:
                raise corrupt from None
            with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tar:
                tar.extractall(staging, filter="data")  # rejects absolute paths, .., links outside, devices
            return
        src = _DecReader(f, _aead(passphrase, salt))
        try:
            with tarfile.open(fileobj=src, mode="r|gz") as tar:
                tar.extractall(staging, filter="data")
            while src.read(CHUNK):  # authenticate every remaining frame, including the final one
                pass
        except (InvalidTag, ValueError, EOFError, OSError, zlib.error, tarfile.ReadError) as e:
            if isinstance(e, OSError) and e.errno:  # a real I/O error (disk full...) is not corruption
                raise
            raise corrupt from None


def restore(data_root, inp, passphrase):
    """Decrypt and extract to a private sibling dir, verify it completely, then swap it in.

    Runs under the data-root lock. Any failure removes the staging dir and leaves the live root untouched;
    on success the previous root is kept as <root>.pre-restore-*.
    """
    data_root = Path(data_root)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    lock = _lock(data_root) if data_root.exists() else None
    staging = Path(tempfile.mkdtemp(prefix=f".{data_root.name}.restore-", dir=data_root.parent))
    try:
        _extract(inp, passphrase, staging)
        _verify(staging)
        aside = None
        if data_root.exists():
            aside = data_root.with_name(f"{data_root.name}.pre-restore-{stamp}")
            data_root.rename(aside)
        try:
            staging.rename(data_root)
        except OSError:
            if aside:
                aside.rename(data_root)
            raise
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    finally:
        if lock:
            lock.close()
    return aside


# ---------------------------------------------------------------- retention / deletion
def purge(data_root, days, campaigns_older_than=None):
    totals = {}
    cutoff = campaigns_older_than and time.time() - campaigns_older_than * 86400
    for root in _roots(data_root):
        if not (root / "cache.sqlite3").exists():
            continue
        svc = _service(root)
        for k, v in svc.cache.purge(days).items():
            totals[k] = totals.get(k, 0) + v
        if cutoff:
            for cid in svc.store.list_ids():
                if (svc.store.dir(cid) / "candidates.json").stat().st_mtime < cutoff:
                    svc.delete_campaign(cid)
                    totals["campaigns"] = totals.get("campaigns", 0) + 1
        svc.cache.close()
    return totals


def forget(data_root, email=None, name=None):
    """Delete every candidate matching an email (from research profiles) or exact name, in every campaign."""
    if not (email or name):
        raise OpsError("pass --email or --name")
    email, name = (email or "").strip().lower(), (name or "").strip().casefold()
    removed = 0
    for root in _roots(data_root):
        if not (root / "cache.sqlite3").exists():
            continue
        svc = _service(root)
        for cid in svc.store.list_ids():
            profiles = svc.store.research(cid)["profiles"]
            for c in svc.store.candidates(cid)["candidates"]:
                p = profiles.get(c["candidate_id"]) or {}
                if (email and (p.get("contact_email") or "").lower() == email) or (
                    name and c["name"].strip().casefold() == name
                ):
                    svc.delete_candidate(cid, c["candidate_id"])
                    removed += 1
        # Campaign deletion intentionally preserves reusable contact memory. A deliberate
        # `forget` request does not: remove matching records from both contact stores.
        with svc.cache.tx():
            db = svc.cache.db
            clauses, args = [], []
            if email:
                clauses.append("lower(email)=?")
                args.append(email)
            if name:
                clauses.append("lower(name)=?")
                args.append(name)
            where = " OR ".join(clauses)
            ids = {r[0] for table in ("people", "contacts")
                   for r in db.execute(f"SELECT id FROM {table} WHERE {where}", args)}
            if ids:
                marks = ",".join("?" for _ in ids)
                db.execute(f"DELETE FROM interactions WHERE contact_id IN ({marks})", tuple(ids))
                db.execute(f"DELETE FROM person_links WHERE contact_id IN ({marks})", tuple(ids))
                db.execute(f"DELETE FROM contact_links WHERE contact_id IN ({marks})", tuple(ids))
            db.execute(f"DELETE FROM people WHERE {where}", args)
            db.execute(f"DELETE FROM contacts WHERE {where}", args)
            if email:
                db.execute("DELETE FROM suppressions WHERE lower(email)=?", (email,))
        svc.cache.close()
    return removed


def check_schemas(data_root):
    """Templates must match SCHEMA_VERSION and validate; so must every stored campaign."""
    problems = []
    tpl = {n: json.loads((ROOT / n).read_text()) for n in ("candidates.json", "research.json")}
    problems += [f"template: {p}" for p in contracts.validate_files(tpl["candidates.json"], tpl["research.json"])]
    for root in _roots(data_root):
        if not root.exists():
            continue
        from .storage import CampaignStore

        store = CampaignStore(root)
        for cid in store.list_ids():
            try:
                problems += [f"{cid}: {p}" for p in store.validate(cid)]
            except Exception as e:
                problems.append(f"{cid}: {type(e).__name__}: {e}")
    return problems


# ---------------------------------------------------------------- CLI
def main(argv=None):
    load_env()
    ap = argparse.ArgumentParser(
        prog="python -m app.ops", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate")
    sub.add_parser("check-schemas")
    sub.add_parser("gen-secret-key")
    sub.add_parser("user-list")
    for name in ("backup", "restore"):
        sub.add_parser(name).add_argument("file")
    p = sub.add_parser("purge")
    p.add_argument("--days", type=int, default=None)
    p.add_argument("--campaigns-older-than", type=int, default=None, metavar="DAYS")
    p = sub.add_parser("forget")
    p.add_argument("--email")
    p.add_argument("--name")
    sub.add_parser("delete-campaign").add_argument("campaign_id")
    for name in ("user-add", "user-passwd", "user-disable", "user-enable", "user-delete"):
        sub.add_parser(name).add_argument("username")
    a = ap.parse_args(argv)

    if a.cmd == "gen-secret-key":
        from cryptography.fernet import Fernet

        print(Fernet.generate_key().decode())
        return 0
    try:
        cfg = load_config()
    except ConfigError as e:
        print(e, file=sys.stderr)
        return 2
    root = cfg.data_root
    try:
        if a.cmd == "check-schemas":
            probs = check_schemas(root)
            print("\n".join(probs) or "schemas ok")
            return 1 if probs else 0
        if a.cmd == "backup":
            pw = _secret("HERMES_BACKUP_PASSPHRASE", "Backup passphrase: ", confirm=True)
            out = backup(root, a.file, pw, exclusive=True)
            print(f"wrote {out} ({out.stat().st_size} bytes). Keep the passphrase separately; it cannot be recovered.")
            return 0
        if a.cmd == "restore":
            aside = restore(root, a.file, _secret("HERMES_BACKUP_PASSPHRASE", "Backup passphrase: "))
            print(f"restored into {root}" + (f"; previous data kept at {aside}" if aside else ""))
            return 0
        if a.cmd.startswith("user-"):
            if not cfg.remote:
                raise OpsError("user accounts exist only in remote mode (HERMES_MODE=remote)")
            from .auth import Accounts

            acc = Accounts(root, cfg.secret_key)
            if a.cmd == "user-list":
                for u in acc.list_users():
                    print(f"{u['id']}\t{u['username']}\t{'disabled' if u['disabled'] else 'active'}")
            elif a.cmd == "user-add":
                pw = _secret("HERMES_NEW_PASSWORD", "Password: ", confirm=True)
                print(f"created user {a.username} (id {acc.add_user(a.username, pw)})")
            elif a.cmd == "user-passwd":
                acc.set_password(a.username, _secret("HERMES_NEW_PASSWORD", "New password: ", confirm=True))
                print("password changed; existing sessions ended")
            elif a.cmd in ("user-disable", "user-enable"):
                acc.set_disabled(a.username, a.cmd == "user-disable")
                print(f"{a.cmd[5:]}d {a.username}")
            elif a.cmd == "user-delete":
                lock = _lock(root)
                acc.delete_user(a.username)
                lock.close()
                print(f"deleted {a.username}, their sessions, credentials, and data")
            return 0
        lock = _lock(root)
        try:
            if a.cmd == "migrate":
                n = sum(migrate_campaigns(r) for r in _roots(root))
                for r in _roots(root):
                    if (r / "cache.sqlite3").exists():
                        cache_mod.Cache(r / "cache.sqlite3").close()
                print(f"migrations applied ({n} JSON files upgraded)")
            elif a.cmd == "purge":
                print(json.dumps(purge(root, a.days or cfg.retention_days, a.campaigns_older_than)))
            elif a.cmd == "forget":
                print(f"removed {forget(root, a.email, a.name)} matching record(s)")
            elif a.cmd == "delete-campaign":
                for r in _roots(root):
                    svc = _service(r)
                    if svc.store.exists(a.campaign_id):
                        print(svc.delete_campaign(a.campaign_id))
                        break
                else:
                    raise OpsError("no such campaign")
        finally:
            lock.close()
    except (OpsError, KeyError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
