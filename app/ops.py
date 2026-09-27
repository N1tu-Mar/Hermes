"""Operator CLI. Run with the app stopped for anything that writes.

  python -m app.ops migrate                     apply pending SQLite + JSON migrations (backs up first)
  python -m app.ops check-schemas               validate templates and every campaign's JSON files
  python -m app.ops backup FILE                 encrypted snapshot of the whole data root
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
def backup(data_root, out, passphrase):
    """Consistent encrypted snapshot of every file under data_root. SQLite is copied with the online backup API."""
    data_root, out = Path(data_root), Path(out)
    buf = io.BytesIO()
    with tempfile.TemporaryDirectory() as tmp, tarfile.open(fileobj=buf, mode="w:gz") as tar:
        meta = {
            "version": 1,
            "created_at": time.time(),
            "json_schema": contracts.SCHEMA_VERSION,
            "sqlite_schema": len(cache_mod.MIGRATIONS),
        }
        info = tarfile.TarInfo(META)
        body = json.dumps(meta).encode()
        info.size = len(body)
        tar.addfile(info, io.BytesIO(body))
        for p in sorted(data_root.rglob("*")):
            if not p.is_file() or p.name.endswith(SKIP_SUFFIXES) or ".bak" in p.name:
                continue
            rel = p.relative_to(data_root).as_posix()
            if p.suffix == ".sqlite3":
                snap = Path(tmp) / "snap.sqlite3"
                src, dst = sqlite3.connect(p), sqlite3.connect(snap)
                src.backup(dst)
                src.close(), dst.close()
                tar.add(snap, arcname=rel)
                snap.unlink()
            else:
                tar.add(p, arcname=rel)
    from cryptography.fernet import Fernet

    salt = os.urandom(16)
    blob = MAGIC + salt + Fernet(_key(passphrase, salt)).encrypt(buf.getvalue())
    part = out.with_name(out.name + ".part")
    fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(blob)
        f.flush()
        os.fsync(f.fileno())
    os.replace(part, out)
    return out


def restore(data_root, inp, passphrase):
    """Decrypt, verify, extract to a sibling dir, then swap it in. The previous root is kept as <root>.pre-restore-*."""
    from cryptography.fernet import Fernet, InvalidToken

    data_root = Path(data_root)
    blob = Path(inp).read_bytes()
    if not blob.startswith(MAGIC):
        raise OpsError("not a HERMES backup file")
    salt = blob[len(MAGIC) : len(MAGIC) + 16]
    try:
        raw = Fernet(_key(passphrase, salt)).decrypt(blob[len(MAGIC) + 16 :])
    except InvalidToken:
        raise OpsError("wrong passphrase or corrupted backup") from None
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    staging = data_root.with_name(f".{data_root.name}.restore-{stamp}")
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tar:
        tar.extractall(staging, filter="data")  # rejects absolute paths, .., links outside, devices
    meta = json.loads((staging / META).read_text())
    (staging / META).unlink()
    if meta["json_schema"] > contracts.SCHEMA_VERSION or meta["sqlite_schema"] > len(cache_mod.MIGRATIONS):
        shutil.rmtree(staging)
        raise OpsError("backup was made by a newer HERMES; upgrade the app before restoring")
    lock = _lock(data_root) if data_root.exists() else None
    try:
        aside = None
        if data_root.exists():
            aside = data_root.with_name(f"{data_root.name}.pre-restore-{stamp}")
            data_root.rename(aside)
        staging.rename(data_root)
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
            out = backup(root, a.file, _secret("HERMES_BACKUP_PASSPHRASE", "Backup passphrase: ", confirm=True))
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
