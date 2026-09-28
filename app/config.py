"""Startup configuration: read once from the environment (+ .env), validate, fail fast.

Every problem is collected and reported together so a bad deploy shows the
whole list instead of one error per restart. Secrets are held here but never
included in repr() or diagnostics.
"""

import ipaddress
import math
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR"}
_HOSTNAME = re.compile(r"(?!-)[A-Za-z0-9-]{1,63}(?<!-)(\.(?!-)[A-Za-z0-9-]{1,63}(?<!-))*")


def _origin_problem(url):
    """Why `url` is not a bare origin, or None. (Scheme/HTTPS rules are enforced separately.)"""
    try:
        p = urlsplit(url)
        _ = p.port  # raises ValueError when out of range or not numeric
    except ValueError:
        return "invalid URL or port"
    if p.scheme not in ("http", "https") or not p.hostname:
        return "needs http(s):// and a host"
    if p.username is not None or p.password is not None:
        return "must not contain credentials"
    if p.path or p.query or p.fragment:
        return "must not contain a path, query, or fragment"
    return None


def _host_ip(host):
    """IP for a bare or [bracketed] literal, else None."""
    try:
        return ipaddress.ip_address(host[1:-1] if host[:1] + host[-1:] == "[]" else host)
    except ValueError:
        return None


class ConfigError(Exception):
    def __init__(self, problems):
        super().__init__("invalid configuration:\n  - " + "\n  - ".join(problems))
        self.problems = problems


@dataclass(frozen=True)
class Config:
    mode: str = "local"  # local (default, single user, loopback token) | remote (accounts, HTTPS only)
    host: str = "127.0.0.1"
    port: int = 8765
    data_root: Path = ROOT / "data"
    openai_api_key: str = field(default="", repr=False)
    openai_model: str = "gpt-5.6-terra"
    app_token: str = field(default="", repr=False)
    public_url: str = ""
    secret_key: str = field(default="", repr=False)
    trusted_proxies: str = "127.0.0.1"
    log_level: str = "INFO"
    retention_days: int = 90
    shutdown_grace: float = 10.0
    gmail_sync_interval: float = 300.0
    gmail_timeout: float = 30.0
    price_input_per_mtok: float | None = None
    price_output_per_mtok: float | None = None

    @property
    def remote(self):
        return self.mode == "remote"

    @property
    def public_host(self):
        return urlsplit(self.public_url).hostname or ""


def load_env(path=ROOT / ".env"):
    """Minimal .env reader; real environment variables win."""
    if path.exists():
        for line in path.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def load_config(env=None):
    env = os.environ if env is None else env
    problems = []

    def num(name, default, cast, lo, hi):
        raw = env.get(name) or str(default)
        try:
            v = cast(raw)
        except ValueError:
            problems.append(f"{name}={raw!r} is not a number")
            return default
        if not (math.isfinite(v) and lo <= v <= hi):
            problems.append(f"{name}={v} must be between {lo} and {hi}")
        return v

    mode = (env.get("HERMES_MODE") or "local").strip().lower()
    if mode not in ("local", "remote"):
        problems.append(f"HERMES_MODE={mode!r} must be 'local' or 'remote'")
    host = (env.get("APP_HOST") or "127.0.0.1").strip()
    ip = _host_ip(host)
    if ip is not None:
        host = str(ip)  # uvicorn wants a bare literal: "[::1]" -> "::1"
    elif not _HOSTNAME.fullmatch(host):
        problems.append(f"APP_HOST={host!r} is not a valid IP address or hostname")
    port = num("APP_PORT", 8765, int, 1, 65535)
    data_root = Path(os.path.expanduser(env.get("DATA_ROOT") or "data"))
    data_root = data_root if data_root.is_absolute() else ROOT / data_root
    try:
        data_root.mkdir(parents=True, exist_ok=True)
        # unique name (suffix matches ops.SKIP_SUFFIXES) so concurrent starts never share or delete each other's probe
        fd, probe = tempfile.mkstemp(prefix=".", suffix=".write-probe", dir=data_root)
        with os.fdopen(fd, "w") as f:
            f.write("ok")
        os.unlink(probe)
    except OSError as e:
        problems.append(f"DATA_ROOT {data_root} is not writable ({type(e).__name__})")
    key = (env.get("OPENAI_API_KEY") or "").strip()
    if key and not key.startswith("sk-"):
        problems.append("OPENAI_API_KEY does not look like an OpenAI key (expected 'sk-...')")
    retention = num("RETENTION_DAYS", 90, int, 1, 3650)
    grace = num("SHUTDOWN_GRACE_SECONDS", 10, float, 0, 300)
    gmail_sync = num("HERMES_GMAIL_SYNC_INTERVAL_SECONDS", 300, float, 10, 86400)
    gmail_timeout = num("HERMES_GMAIL_TIMEOUT_SECONDS", 30, float, 1, 300)
    prices = {}
    for name in ("HERMES_PRICE_INPUT_PER_MTOK", "HERMES_PRICE_OUTPUT_PER_MTOK"):
        if env.get(name):
            prices[name] = num(name, 0, float, 0, 100000)
    if len(prices) == 1:
        problems.append("HERMES_PRICE_INPUT_PER_MTOK and HERMES_PRICE_OUTPUT_PER_MTOK must be set together")
    level = (env.get("LOG_LEVEL") or "INFO").upper()
    if level not in LOG_LEVELS:
        problems.append(f"LOG_LEVEL={level!r} must be one of {sorted(LOG_LEVELS)}")

    public_url = (env.get("HERMES_PUBLIC_URL") or "").strip().rstrip("/")
    if public_url and (why := _origin_problem(public_url)):
        problems.append(f"HERMES_PUBLIC_URL must be a bare origin (scheme://host[:port]): {why}")
    proxies = (env.get("HERMES_TRUSTED_PROXIES") or "127.0.0.1").strip()
    for item in (i.strip() for i in proxies.split(",")):
        try:
            if item != "*":
                ipaddress.ip_network(item, strict=False)
        except ValueError:
            problems.append(f"HERMES_TRUSTED_PROXIES entry {item!r} is not an IP address or CIDR range")
    secret_key = (env.get("HERMES_SECRET_KEY") or "").strip()
    if mode == "local":
        try:
            loopback = host == "localhost" or ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = False
        if not loopback:
            problems.append(
                f"APP_HOST={host!r}: local mode only binds to loopback; use HERMES_MODE=remote to expose it"
            )
    else:
        parts = urlsplit(public_url)
        if parts.scheme != "https" or not parts.hostname:
            problems.append("remote mode requires HERMES_PUBLIC_URL=https://<your host> (HTTPS is mandatory)")
        try:
            from cryptography.fernet import Fernet

            Fernet(secret_key)
        except Exception:
            problems.append("remote mode requires HERMES_SECRET_KEY (generate with: python -m app.ops gen-secret-key)")
        if env.get("APP_TOKEN"):
            problems.append("APP_TOKEN is a local-mode credential and must not be set in remote mode")

    if problems:
        raise ConfigError(problems)
    return Config(
        mode=mode,
        host=host,
        port=port,
        data_root=data_root,
        openai_api_key=key,
        openai_model=env.get("OPENAI_MODEL") or "gpt-5.6-terra",
        app_token=env.get("APP_TOKEN") or "",
        public_url=public_url,
        secret_key=secret_key,
        trusted_proxies=proxies,
        log_level=level,
        retention_days=retention,
        shutdown_grace=grace,
        gmail_sync_interval=gmail_sync,
        gmail_timeout=gmail_timeout,
        price_input_per_mtok=prices.get("HERMES_PRICE_INPUT_PER_MTOK"),
        price_output_per_mtok=prices.get("HERMES_PRICE_OUTPUT_PER_MTOK"),
    )
