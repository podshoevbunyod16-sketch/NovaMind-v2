"""Block non-loopback DNS lookups and TCP connections in isolated tests.

This is loaded only when a test/CI command explicitly adds this directory to
PYTHONPATH. It is a guardrail for Python test processes, not an OS firewall.
"""
import ipaddress
import socket

_original_getaddrinfo = socket.getaddrinfo
_original_connect = socket.socket.connect
_original_connect_ex = socket.socket.connect_ex


def _is_local(host):
    if isinstance(host, bytes):
        host = host.decode("ascii", errors="ignore")
    if not isinstance(host, str):
        return False
    normalized = host.strip().strip("[]").rstrip(".").lower()
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _is_bind_wildcard(host):
    if isinstance(host, bytes):
        host = host.decode("ascii", errors="ignore")
    if not isinstance(host, str):
        return False
    normalized = host.strip().strip("[]").rstrip(".").lower()
    try:
        return ipaddress.ip_address(normalized).is_unspecified
    except ValueError:
        return False


def _guard_getaddrinfo(host, *args, **kwargs):
    if not _is_local(host) and not _is_bind_wildcard(host):
        raise OSError("external network access disabled during isolated tests")
    return _original_getaddrinfo(host, *args, **kwargs)


def _guard_connect(sock, address):
    host = address[0] if isinstance(address, tuple) and address else address
    if not _is_local(host):
        raise OSError("external network access disabled during isolated tests")
    return _original_connect(sock, address)


def _guard_connect_ex(sock, address):
    host = address[0] if isinstance(address, tuple) and address else address
    if not _is_local(host):
        raise OSError("external network access disabled during isolated tests")
    return _original_connect_ex(sock, address)


socket.getaddrinfo = _guard_getaddrinfo
socket.socket.connect = _guard_connect
socket.socket.connect_ex = _guard_connect_ex
