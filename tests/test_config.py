import pytest
from cryptography.fernet import Fernet

from app.config import ConfigError, load_config


def test_defaults_are_local_and_valid(tmp_path):
    cfg = load_config({"DATA_ROOT": str(tmp_path)})
    assert cfg.mode == "local" and cfg.host == "127.0.0.1" and cfg.port == 8765
    assert "sk-" not in repr(load_config({"DATA_ROOT": str(tmp_path), "OPENAI_API_KEY": "sk-abc123"}))


def test_all_problems_reported_together(tmp_path):
    with pytest.raises(ConfigError) as e:
        load_config(
            {
                "DATA_ROOT": str(tmp_path),
                "APP_PORT": "99999",
                "LOG_LEVEL": "LOUD",
                "OPENAI_API_KEY": "nope",
                "APP_HOST": "0.0.0.0",
                "RETENTION_DAYS": "x",
            }
        )
    assert len(e.value.problems) == 5


def test_remote_requires_https_secret_and_no_app_token(tmp_path):
    base = {"DATA_ROOT": str(tmp_path), "HERMES_MODE": "remote"}
    with pytest.raises(ConfigError) as e:
        load_config({**base, "HERMES_PUBLIC_URL": "http://hermes.example", "APP_TOKEN": "x"})
    msg = str(e.value)
    assert "HTTPS" in msg and "HERMES_SECRET_KEY" in msg and "APP_TOKEN" in msg
    cfg = load_config(
        {
            **base,
            "HERMES_PUBLIC_URL": "https://hermes.example/",
            "HERMES_SECRET_KEY": Fernet.generate_key().decode(),
            "APP_HOST": "127.0.0.1",
        }
    )
    assert cfg.remote and cfg.public_host == "hermes.example" and cfg.public_url == "https://hermes.example"


def test_unwritable_data_root(tmp_path):
    ro = tmp_path / "ro"
    ro.mkdir(mode=0o500)
    with pytest.raises(ConfigError, match="not writable"):
        load_config({"DATA_ROOT": str(ro / "sub")})
