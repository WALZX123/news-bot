"""HTTP URL policy and address resolution for a pinned direct transport."""
import ipaddress
import socket
from urllib.parse import urlsplit, urlunsplit


class UnsafeURLError(ValueError):
    pass


def normalize_http_url(url: str) -> str:
    if not isinstance(url, str) or len(url) > 2048 or any(ord(c) < 33 for c in url):
        raise UnsafeURLError("URL must be a bounded HTTP(S) address")
    try:
        parsed = urlsplit(url)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            raise UnsafeURLError("Only HTTP(S) URLs are allowed")
        if parsed.username is not None or parsed.password is not None or "\\" in url:
            raise UnsafeURLError("URL credentials and backslashes are not allowed")
        host = parsed.hostname.encode("idna").decode("ascii").lower().rstrip(".")
        port = parsed.port
        if not host or (port is not None and not 1 <= port <= 65535):
            raise UnsafeURLError("Invalid URL host or port")
        if ":" in host:
            host = f"[{host}]"
        netloc = host
        if port is not None and (parsed.scheme.lower(), port) not in {("http", 80), ("https", 443)}:
            netloc += f":{port}"
        normalized = urlunsplit((parsed.scheme.lower(), netloc, parsed.path or "/", parsed.query, ""))
        if len(normalized) > 2048:
            raise UnsafeURLError("Normalized HTTP(S) URL exceeds storage capacity")
        return normalized
    except (ValueError, UnicodeError) as exc:
        raise UnsafeURLError("Invalid HTTP(S) URL") from exc


def normalized_private_hosts(hosts):
    if hosts is not None and (not isinstance(hosts, (list, tuple, set)) or len(hosts) > 200):
        raise UnsafeURLError("Private RSS permissions must be a bounded host list")
    result = set()
    for host in hosts or ():
        if not isinstance(host, str) or not host or any(c in host for c in "/@*?#\\"):
            raise UnsafeURLError("Private RSS permission must name an exact host")
        host = host.strip("[]").rstrip(".").encode("idna").decode("ascii").lower()
        if not host or any(c.isspace() for c in host) or '%' in host:
            raise UnsafeURLError("Invalid private RSS host")
        if ':' in host:
            try:
                ipaddress.IPv6Address(host)
            except ValueError:
                raise UnsafeURLError("Private RSS permission cannot include a port") from None
        result.add(host)
    return result


def validate_network_url(url: str, allow_private_hosts=None, *, resolve=True) -> str:
    normalized = normalize_http_url(url)
    parsed = urlsplit(normalized)
    host = parsed.hostname
    if host in normalized_private_hosts(allow_private_hosts):
        return normalized
    if host == "localhost" or host.endswith(".localhost") or "%" in host:
        raise UnsafeURLError("Private RSS host requires explicit permission")
    try:
        addresses = [ipaddress.ip_address(host)]
    except ValueError:
        if not resolve:
            return normalized
        try:
            addresses = [ipaddress.ip_address(row[4][0]) for row in
                         socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80),
                                            type=socket.SOCK_STREAM)]
        except (OSError, ValueError) as exc:
            raise UnsafeURLError("RSS host resolution failed") from exc
    if not addresses or any(not address.is_global for address in addresses):
        raise UnsafeURLError("Private RSS host requires explicit permission")
    return normalized


def resolve_network_addresses(url, allow_private_hosts=None):
    """Return checked addresses; callers connect to literals, preserving Host/TLS.

    RSS invokes this inside a killable process, bounding stalled OS resolution.
    """
    normalized = validate_network_url(url, allow_private_hosts, resolve=False)
    parsed = urlsplit(normalized)
    try:
        addresses = [ipaddress.ip_address(parsed.hostname)]
    except ValueError:
        try:
            addresses = list(dict.fromkeys(ipaddress.ip_address(row[4][0]) for row in
                socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == 'https' else 80),
                                   type=socket.SOCK_STREAM)))
        except (OSError, ValueError):
            raise UnsafeURLError("RSS host resolution failed") from None
    private_allowed = parsed.hostname in normalized_private_hosts(allow_private_hosts)
    if not addresses or (not private_allowed and any(not address.is_global for address in addresses)):
        raise UnsafeURLError("Private RSS host requires explicit permission")
    return normalized, [str(address) for address in addresses]
