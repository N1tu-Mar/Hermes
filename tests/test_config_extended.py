import pytest

from app.config import ConfigError, load_config


def cfg(tmp_path, **env):
    return load_config({"DATA_ROOT": str(tmp_path), **env})


def bad(tmp_path, match, **env):
    with pytest.raises(ConfigError, match=match):
        cfg(tmp_path, **env)


@pytest.mark.parametrize("v", ["nan", "inf", "5", "-1", "999999", "x"])
def test_gmail_timing_rejected(tmp_path, v):
    bad(tmp_path, "HERMES_GMAIL_SYNC_INTERVAL_SECONDS", HERMES_GMAIL_SYNC_INTERVAL_SECONDS=v)


def test_gmail_timing_defaults_and_timeout(tmp_path):
    c = cfg(tmp_path)
    assert (c.gmail_sync_interval, c.gmail_timeout) == (300, 30)
    bad(tmp_path, "HERMES_GMAIL_TIMEOUT_SECONDS", HERMES_GMAIL_TIMEOUT_SECONDS="0")


def test_pricing_valid_pair_negative_and_lone(tmp_path):
    c = cfg(tmp_path, HERMES_PRICE_INPUT_PER_MTOK="1.5", HERMES_PRICE_OUTPUT_PER_MTOK="6")
    assert (c.price_input_per_mtok, c.price_output_per_mtok) == (1.5, 6)
    assert cfg(tmp_path).price_input_per_mtok is None
    bad(tmp_path, "set together", HERMES_PRICE_INPUT_PER_MTOK="1")
    bad(tmp_path, "PRICE_OUTPUT", HERMES_PRICE_INPUT_PER_MTOK="1", HERMES_PRICE_OUTPUT_PER_MTOK="-2")
    bad(tmp_path, "PRICE_INPUT", HERMES_PRICE_INPUT_PER_MTOK="nan", HERMES_PRICE_OUTPUT_PER_MTOK="2")


@pytest.mark.parametrize(
    "url",
    ["https://h.example/app", "https://h.example?x=1", "https://u:p@h.example", "https://h.example:99999", "ftp://h", "h.example"],
)
def test_public_url_must_be_bare_origin(tmp_path, url):
    bad(tmp_path, "HERMES_PUBLIC_URL", HERMES_PUBLIC_URL=url)


def test_public_url_origin_ok(tmp_path):
    assert cfg(tmp_path, HERMES_PUBLIC_URL="https://h.example:8443/").public_url == "https://h.example:8443"
    assert cfg(tmp_path, HERMES_PUBLIC_URL="https://[2001:db8::1]:8443").public_url == "https://[2001:db8::1]:8443"


def test_trusted_proxies(tmp_path):
    ok = "127.0.0.1, 10.0.0.0/8,::1,2001:db8::/32"
    assert cfg(tmp_path, HERMES_TRUSTED_PROXIES=ok).trusted_proxies == ok
    assert cfg(tmp_path, HERMES_TRUSTED_PROXIES="*").trusted_proxies == "*"
    for v in ("10.0.0.0/33", "proxy.example", "1.2.3.4,,"):
        bad(tmp_path, "HERMES_TRUSTED_PROXIES", HERMES_TRUSTED_PROXIES=v)


def test_ipv6_hosts(tmp_path):
    assert cfg(tmp_path, APP_HOST="[::1]").host == "::1"
    assert cfg(tmp_path, APP_HOST="::1").host == "::1"
    bad(tmp_path, "loopback", APP_HOST="2001:db8::1")
    bad(tmp_path, "not a valid", APP_HOST="[::1")
    bad(tmp_path, "not a valid", APP_HOST="bad host")


def test_write_probe_is_unique_and_cleaned(tmp_path):
    (tmp_path / ".write-probe").write_text("precious")  # old fixed name is neither used nor deleted
    cfg(tmp_path)
    assert [p.name for p in tmp_path.iterdir()] == [".write-probe"]
    assert (tmp_path / ".write-probe").read_text() == "precious"
