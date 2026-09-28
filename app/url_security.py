"""SSRF-safe URL validation and DNS resolution for outbound fetches.

Every hop of every outbound HTTP request (including redirect targets) must go
through `resolve_target` before a socket is opened. It rejects credentials in
the URL and resolves the hostname itself, so the caller can connect directly
to the vetted IP instead of trusting a second, unvalidated DNS lookup inside
the HTTP client (which is what makes DNS-rebinding attacks possible).
"""

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit

ALLOWED_SCHEMES = ("http", "https")


class UnsafeURLError(Exception):
    pass


def _is_public_ip(ip_text):
    addr = ipaddress.ip_address(ip_text)
    if isinstance(addr, ipaddress.IPv6Address):
        mapped = addr.ipv4_mapped
        if mapped is not None:
            addr = mapped
    return not (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_unspecified
        or addr.is_reserved
    )


def split_target(url):
    """Parse a URL, rejecting unsupported schemes, missing hosts, and embedded credentials."""
    if not url or not isinstance(url, str):
        raise UnsafeURLError("empty or non-string URL")
    parts = urlsplit(url)
    if parts.scheme not in ALLOWED_SCHEMES:
        raise UnsafeURLError(f"unsupported scheme {parts.scheme!r}")
    if parts.username or parts.password or "@" in parts.netloc:
        raise UnsafeURLError("credentials in URL are not allowed")
    host = parts.hostname
    if not host:
        raise UnsafeURLError("missing host")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    return parts, host, port


async def resolve_safe_ip(host, port):
    """Resolve host to an IP via the event loop's resolver, rejecting non-public results."""
    loop = asyncio.get_event_loop()
    try:
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise UnsafeURLError(f"DNS resolution failed for {host!r}: {e}") from e
    if not infos:
        raise UnsafeURLError(f"DNS resolution returned no addresses for {host!r}")
    ip = infos[0][4][0]
    if not _is_public_ip(ip):
        raise UnsafeURLError(f"{host!r} resolves to non-public address {ip}")
    return ip


async def resolve_target(url):
    """Validate `url` and resolve it to a connectable, public IP.

    Returns (parts, host, ip, port). The caller should connect to `ip` while
    still presenting `host` for the Host header and TLS SNI, so DNS cannot be
    re-resolved (and rebound to a private address) between validation and
    connection.
    """
    parts, host, port = split_target(url)
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if not _is_public_ip(str(literal)):
            raise UnsafeURLError(f"literal address {literal} is not a public address")
        return parts, host, str(literal), port
    ip = await resolve_safe_ip(host, port)
    return parts, host, ip, port
