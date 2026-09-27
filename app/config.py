"""Startup configuration: read once from the environment (+ .env), validate, fail fast.

Every problem is collected and reported together so a bad deploy shows the
whole list instead of one error per restart. Secrets are held here but never
included in repr() or diagnostics.
"""
import ipaddress
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR"}


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
        if not lo <= v <= hi:
            problems.append(f"{name}={v} must be between {lo} and {hi}")
        return v

    mode = (env.get("HERMES_MODE") or "local").strip().lower()
    if mode not in ("local", "remote"):
        problems.append(f"HERMES_MODE={mode!r} must be 'local' or 'remote'")
    host = env.get("APP_HOST") or "127.0.0.1"
    port = num("APP_PORT", 8765, int, 1, 65535)
    data_root = Path(os.path.expanduser(env.get("DATA_ROOT") or "data"))
    data_root = data_root if data_root.is_absolute() else ROOT / data_root
    try:
        data_root.mkdir(parents=True, exist_ok=True)
        probe = data_root / ".write-probe"
        probe.write_text("ok")
        probe.unlink()
    except OSError as e:
        problems.append(f"DATA_ROOT {data_root} is not writable ({type(e).__name__})")
    key = (env.get("OPENAI_API_KEY") or "").strip()
    if key and not key.startswith("sk-"):
        problems.append("OPENAI_API_KEY does not look like an OpenAI key (expected 'sk-...')")
    retention = num("RETENTION_DAYS", 90, int, 1, 3650)
    grace = num("SHUTDOWN_GRACE_SECONDS", 10, float, 0, 300)
    level = (env.get("LOG_LEVEL") or "INFO").upper()
    if level not in LOG_LEVELS:
        problems.append(f"LOG_LEVEL={level!r} must be one of {sorted(LOG_LEVELS)}")

    public_url = (env.get("HERMES_PUBLIC_URL") or "").rstrip("/")
    secret_key = (env.get("HERMES_SECRET_KEY") or "").strip()
    if mode == "local":
        try:
            loopback = host == "localhost" or ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = False
        if not loopback:
            problems.append(f"APP_HOST={host!r}: local mode only binds to loopback; use HERMES_MODE=remote to expose it")
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
        trusted_proxies=env.get("HERMES_TRUSTED_PROXIES") or "127.0.0.1",
        log_level=level,
        retention_days=retention,
        shutdown_grace=grace,
    )
