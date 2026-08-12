"""Shared FastAPI dependencies for the transport layer."""

from __future__ import annotations

import ipaddress
from functools import lru_cache

from fastapi import Request

from compass_backend.config import settings as _runtime_settings


@lru_cache(maxsize=1)
def _parse_trusted_proxies(raw: tuple[str, ...]) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    """Parse a list of CIDR/IP strings into ip_network objects.

    Bad entries are skipped silently — the audit guidance is to fail closed
    (empty allowlist = trust nothing), so unparseable entries simply do not
    grant trust. The cache is keyed on the input tuple so repeated requests
    don't re-parse on every call.
    """

    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for entry in raw:
        cleaned = entry.strip()
        if not cleaned:
            continue
        try:
            networks.append(ipaddress.ip_network(cleaned, strict=False))
        except ValueError:
            # Unparseable proxy entry — skip rather than crash startup.
            # `strict=False` already tolerates host-bit-set CIDRs.
            continue
    return tuple(networks)


def _is_trusted_proxy(host: str | None, trusted: tuple[str, ...]) -> bool:
    """Return True when ``host`` is inside one of the trusted-proxy networks."""

    if not host:
        return False
    try:
        candidate = ipaddress.ip_address(host)
    except ValueError:
        return False
    for network in _parse_trusted_proxies(trusted):
        if candidate in network:
            return True
    return False


def client_ip(request: Request) -> str:
    """Return the client IP.

    Defaults to ``request.client.host`` (the actual peer of the TCP
    connection). ``X-Forwarded-For`` / ``X-Real-IP`` are only honored when
    the connection comes from a configured trusted proxy — otherwise an
    attacker can spoof request identity by setting the header. See audit
    finding #4 (2026-05-25-audit-report.md).
    """

    direct_host = request.client.host if request.client else None
    trusted = tuple(_runtime_settings.trusted_proxies)
    if trusted and _is_trusted_proxy(direct_host, trusted):
        forwarded_for = request.headers.get("X-Forwarded-For")
        if forwarded_for:
            return forwarded_for.split(",", 1)[0].strip()
        real_ip = request.headers.get("X-Real-IP")
        if real_ip:
            return real_ip.strip()
    return direct_host or "unknown"


def request_identifier(request: Request) -> str:
    """Stable request identity helper for diagnostics and future abuse controls."""

    auth_user = getattr(request.state, "auth_user", None)
    owner_email = getattr(auth_user, "email", None)
    if owner_email:
        return f"user:{owner_email}"
    api_key_id = getattr(auth_user, "api_key_id", None)
    if api_key_id:
        return f"api_key:{api_key_id}"
    return f"ip:{client_ip(request)}"
