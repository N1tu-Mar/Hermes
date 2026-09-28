"""Dependency, CI, and deploy-file consistency. Guards against lock drift and weakened hardening."""

import base64
import re
import subprocess
import sys
from importlib import metadata
from pathlib import Path

from app.workspace import MAX_ATTACHMENT_BYTES

ROOT = Path(__file__).resolve().parent.parent


def pins(name):
    text = (ROOT / name).read_text()
    return {m[1].lower(): m[2] for m in re.finditer(r"^([A-Za-z0-9_.-]+)==([^\s\;]+)", text, re.M)}


def test_no_stray_requirements_txt():
    assert not (ROOT / "requirements.txt").exists()


def test_direct_pins_match_locks():
    runtime, dev = pins("requirements.lock"), pins("requirements-dev.lock")
    for name, ver in pins("requirements.in").items():
        assert runtime.get(name) == ver, name
    for name, ver in pins("requirements-dev.in").items():
        assert dev.get(name) == ver, name
    assert "pypdf" in runtime
    assert all(dev.get(n) == v for n, v in runtime.items()), "dev lock must contain the runtime lock"


def test_installed_environment_matches_lock():
    lock = pins("requirements-dev.lock")
    bad = {n: (v, metadata.version(n)) for n, v in lock.items() if _installed(n) not in (None, v)}
    assert not bad, bad
    assert _installed("pypdf") == lock["pypdf"]
    r = subprocess.run([sys.executable, "-m", "pip", "check"], capture_output=True, text=True)  # noqa: S603
    assert r.returncode == 0, r.stdout


def _installed(name):
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return None


def test_ci_actions_pinned_to_sha_and_audit_present():
    ci = (ROOT / ".github/workflows/ci.yml").read_text()
    uses = re.findall(r"uses:\s*(\S+)", ci)
    assert uses and all(re.fullmatch(r"[^@\s]+@[0-9a-f]{40}", u) for u in uses), uses
    assert "pip_audit" in ci and "cyclonedx" in ci


def test_proxy_limit_fits_attachment_upload():
    m = re.search(r"max_size\s+(\d+)(MiB|MB)", (ROOT / "deploy/Caddyfile").read_text())
    limit = int(m[1]) * (1 << 20 if m[2] == "MiB" else 10**6)
    body = len(base64.b64encode(b"x" * MAX_ATTACHMENT_BYTES)) + 1024  # JSON envelope allowance
    assert limit >= body


def test_systemd_hardening_and_credentials():
    svc = (ROOT / "deploy/hermes.service").read_text()
    for opt in (
        "NoNewPrivileges=true",
        "ProtectSystem=strict",
        "PrivateDevices=true",
        "CapabilityBoundingSet=",
        "RestrictAddressFamilies=",
        "SystemCallFilter=@system-service",
        "ProtectKernelTunables=true",
    ):
        assert opt in svc, opt
    assert "BACKUP_PASSPHRASE" not in svc.replace("# Never put HERMES_BACKUP_PASSPHRASE here", "")
    bak = (ROOT / "deploy/hermes-backup.service").read_text()
    assert "LoadCredential=backup-passphrase:" in bak and "ExecStart=/opt/hermes/deploy/hermes-backup.sh" in bak
    assert "OnCalendar=" in (ROOT / "deploy/hermes-backup.timer").read_text()
    for script in ("hermes-backup.sh", "hermes-restore.sh"):
        path = ROOT / "deploy" / script
        assert path.stat().st_mode & 0o111, script
        assert subprocess.run(["sh", "-n", str(path)]).returncode == 0  # noqa: S603, S607
