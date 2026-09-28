import asyncio
import socket

import httpx
import pytest

from app.cache import Cache
from app.research import Fetcher, FetchError
from app.url_security import UnsafeURLError, resolve_target, split_target

LITERAL_UNSAFE = [
    "http://127.0.0.1/",           # loopback v4
    "http://0.0.0.0/",             # unspecified v4
    "http://10.1.2.3/",            # private v4
    "http://192.168.1.1/",         # private v4
    "http://172.16.0.5/",          # private v4
    "http://169.254.169.254/",     # link-local v4 (cloud metadata)
    "http://224.0.0.1/",           # multicast v4
    "http://240.0.0.1/",           # reserved v4
    "http://[::1]/",               # loopback v6
    "http://[::]/",                # unspecified v6
    "http://[fd00::1]/",           # unique-local (private) v6
    "http://[fe80::1]/",           # link-local v6
    "http://[ff02::1]/",           # multicast v6
    "http://[::ffff:127.0.0.1]/",  # v4-mapped loopback
]


@pytest.mark.parametrize("url", LITERAL_UNSAFE)
def test_literal_unsafe_addresses_rejected(url):
    with pytest.raises(UnsafeURLError):
        asyncio.run(resolve_target(url))


def test_public_literal_ip_allowed():
    parts, host, ip, port = asyncio.run(resolve_target("http://93.184.215.14/"))
    assert ip == "93.184.215.14" and port == 80


@pytest.mark.parametrize("url", [
    "http://user:pass@example.com/",
    "http://user@example.com/",
    "ftp://example.com/",
    "file:///etc/passwd",
    "http:///no-host",
])
def test_credentials_and_unsupported_schemes_rejected(url):
    with pytest.raises(UnsafeURLError):
        split_target(url)


def test_dns_resolving_to_private_address_rejected(monkeypatch):
    def fake_getaddrinfo(host, port, *a, **kw):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.9", port))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(UnsafeURLError, match="non-public"):
        asyncio.run(resolve_target("http://internal.example.corp/"))


def test_dns_resolving_to_private_ipv6_address_rejected(monkeypatch):
    def fake_getaddrinfo(host, port, *a, **kw):
        return [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("fe80::1", port, 0, 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(UnsafeURLError, match="non-public"):
        asyncio.run(resolve_target("http://internal-v6.example.corp/"))


def test_dns_failure_is_rejected_not_swallowed(monkeypatch):
    def fake_getaddrinfo(host, port, *a, **kw):
        raise socket.gaierror("nxdomain")

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(UnsafeURLError, match="DNS resolution failed"):
        asyncio.run(resolve_target("http://nowhere.example.corp/"))


def test_allowed_public_fixture_is_fetched(tmp_path):
    async def run():
        cache = Cache(tmp_path / "cache.db")
        fetcher = Fetcher(cache)
        text, from_cache = await fetcher.fetch("https://example.com/")
        assert not from_cache
        assert "Example Domain" in text

    asyncio.run(run())


def test_redirect_hop_is_revalidated_and_public_to_private_is_blocked(tmp_path):
    async def handler(request):
        if request.url.path == "/start":
            return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data/"})
        return httpx.Response(200, text="unexpected")

    async def run():
        cache = Cache(tmp_path / "cache.db")
        fetcher = Fetcher(cache)  # client=None -> real DNS pinning stays active
        fetcher.client = httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False)
        with pytest.raises(FetchError, match="not a public address"):
            await fetcher.fetch("https://example.com/start")

    asyncio.run(run())


def test_redirect_loop_is_bounded(tmp_path):
    async def handler(request):
        return httpx.Response(302, headers={"location": "https://example.com/loop"})

    async def run():
        cache = Cache(tmp_path / "cache.db")
        fetcher = Fetcher(cache)
        fetcher.client = httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False)
        with pytest.raises(FetchError, match="too many redirects"):
            await fetcher.fetch("https://example.com/loop")

    asyncio.run(run())
