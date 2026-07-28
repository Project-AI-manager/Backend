"""Public URL helpers for internationalized domain names (IDNs)."""

from __future__ import annotations

import ipaddress
from urllib.parse import SplitResult, urlsplit, urlunsplit


def public_url_href(url: str) -> str:
    """Return an HTTP(S) URL whose hostname is safe for protocols and clients."""
    parsed = _validated_public_url(url)
    return _replace_hostname(parsed, _ascii_hostname(parsed.hostname or ""))


def public_url_display(url: str) -> str:
    """Return the same URL with a human-readable Unicode IDN hostname."""
    parsed = _validated_public_url(url)
    return _replace_hostname(parsed, _unicode_hostname(parsed.hostname or ""))


def validate_public_url(url: str) -> None:
    """Validate a configured public base URL without changing its spelling."""
    parsed = _validated_public_url(url)
    if parsed.query or parsed.fragment:
        raise ValueError("APP_PUBLIC_URL must not contain a query or fragment")


def _validated_public_url(url: str) -> SplitResult:
    normalized = url.strip()
    parsed = urlsplit(normalized)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("APP_PUBLIC_URL must be an absolute HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("APP_PUBLIC_URL must not contain credentials")
    # Accessing port performs urllib's range and syntax validation.
    _ = parsed.port
    return parsed._replace(scheme=parsed.scheme.lower())


def _replace_hostname(parsed: SplitResult, hostname: str) -> str:
    if _is_ipv6(hostname):
        hostname = f"[{hostname}]"
    netloc = hostname
    if parsed.port is not None:
        netloc = f"{netloc}:{parsed.port}"
    return urlunsplit(parsed._replace(netloc=netloc))


def _ascii_hostname(hostname: str) -> str:
    if _is_ip_address(hostname):
        return hostname
    try:
        return hostname.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise ValueError("APP_PUBLIC_URL contains an invalid internationalized domain") from exc


def _unicode_hostname(hostname: str) -> str:
    if _is_ip_address(hostname):
        return hostname
    try:
        # Round-trip through IDNA also normalizes a hostname supplied in Unicode.
        return hostname.encode("idna").decode("ascii").encode("ascii").decode("idna").lower()
    except UnicodeError as exc:
        raise ValueError("APP_PUBLIC_URL contains an invalid internationalized domain") from exc


def _is_ip_address(hostname: str) -> bool:
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        return False
    return True


def _is_ipv6(hostname: str) -> bool:
    try:
        return isinstance(ipaddress.ip_address(hostname), ipaddress.IPv6Address)
    except ValueError:
        return False
