"""
Anti-SSRF Security Module for LinkPilot Data Engine.
Provides DNS pre-resolution, IP subnet blacklisting, and redirect verification
to prevent Server-Side Request Forgery and DNS rebinding attacks.
"""
import socket
import ipaddress
from urllib.parse import urlparse
from typing import List, Tuple
import httpx
from app.monitoring.logger import logger

# Disallowed subnets (Private, Loopback, Link-Local, Cloud Metadata)
BLOCKED_NETWORKS = [
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),   # Link-Local & AWS/GCP Metadata (169.254.169.254)
    ipaddress.ip_network("100.64.0.0/10"),    # Carrier-grade NAT
    ipaddress.ip_network("192.0.0.0/24"),     # IETF Protocol Assignments
    ipaddress.ip_network("198.18.0.0/15"),    # Benchmark testing
    ipaddress.ip_network("::1/128"),          # IPv6 loopback
    ipaddress.ip_network("fc00::/7"),         # IPv6 Unique local
    ipaddress.ip_network("fe80::/10"),        # IPv6 Link-local
    ipaddress.ip_network("::ffff:0:0/96"),    # IPv4-mapped IPv6
]


class SSRFSecurityViolation(Exception):
    """Raised when a URL resolves to a forbidden, private, or restricted IP address."""
    pass


def is_ip_blocked(ip_addr: str) -> Tuple[bool, str]:
    """Check if an IP address falls within any blocked/private subnet."""
    try:
        ip = ipaddress.ip_address(ip_addr)
        
        # Handle IPv4-mapped IPv6 addresses (e.g. ::ffff:127.0.0.1)
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
            ip = ip.ipv4_mapped

        for net in BLOCKED_NETWORKS:
            if ip in net:
                return True, f"IP {ip_addr} belongs to restricted network {net}"
        return False, ""
    except ValueError:
        return True, f"Invalid IP address format: {ip_addr}"


def validate_url_safety(url: str, require_https: bool = False) -> List[str]:
    """
    Validate a URL against SSRF vulnerabilities:
    1. Validates scheme.
    2. Resolves DNS to all IP addresses.
    3. Verifies no resolved IP is in private/loopback/cloud-metadata subnets.
    Returns list of resolved safe IP addresses.
    Raises SSRFSecurityViolation if URL is unsafe.
    """
    parsed = urlparse(url)
    if not parsed.scheme or parsed.scheme.lower() not in ("http", "https"):
        raise SSRFSecurityViolation(f"Forbidden or missing scheme: {parsed.scheme}")
    
    if require_https and parsed.scheme.lower() != "https":
        raise SSRFSecurityViolation(f"HTTPS is strictly required for this endpoint: {url}")

    hostname = parsed.hostname
    if not hostname:
        raise SSRFSecurityViolation(f"Missing hostname in URL: {url}")

    # Resolve hostname
    port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    try:
        addr_info = socket.getaddrinfo(hostname, port, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise SSRFSecurityViolation(f"DNS resolution failed for {hostname}: {e}")

    safe_ips = []
    for family, socktype, proto, canonname, sockaddr in addr_info:
        ip = sockaddr[0]
        blocked, reason = is_ip_blocked(ip)
        if blocked:
            logger.warning(f"SSRF block triggered: {url} resolved to {ip} ({reason})")
            raise SSRFSecurityViolation(f"SSRF violation: Host {hostname} resolves to blocked IP {ip} ({reason})")
        if ip not in safe_ips:
            safe_ips.append(ip)

    if not safe_ips:
        raise SSRFSecurityViolation(f"No valid IP addresses resolved for {hostname}")

    return safe_ips


class SSRFSafeTransport(httpx.HTTPTransport):
    """
    Custom HTTPX Transport that re-validates URLs on every request and redirect.
    """
    def handle_request(self, request: httpx.Request) -> httpx.Response:
        validate_url_safety(str(request.url))
        return super().handle_request(request)


def create_safe_httpx_client(timeout: float = 30.0, **kwargs) -> httpx.Client:
    """Create a synchronous HTTPX Client equipped with SSRF safe transport."""
    transport = SSRFSafeTransport()
    return httpx.Client(transport=transport, timeout=timeout, **kwargs)
