"""Tests for shopping_cli.discovery — TrustPolicy, ProfileFetcher (SSRF), and cache.

ALL network interactions are mocked.  Zero real outbound requests.
"""

from __future__ import annotations

import ipaddress
import json
import socket
import unittest
import urllib.parse
import urllib.request
from unittest.mock import MagicMock, patch

from shopping_cli.discovery.cache import (
    CacheDirective,
    CacheState,
    build_conditional_headers,
    compute_cache_state,
    compute_content_hash,
    snapshot_meta,
)
from shopping_cli.discovery.fetcher import (
    FetchError,
    FetchLimitError,
    FetchResult,
    ProfileFetcher,
    SSRFBlockError,
    _is_blocked_ip,
    _resolve_and_validate,
    _validate_port,
    _validate_scheme,
)
from shopping_cli.discovery.trust import TrustPolicy

# ──────────────────────────────────────────────────────────────────────────────
# TrustPolicy tests
# ──────────────────────────────────────────────────────────────────────────────


class TrustPolicyDefaultsTest(unittest.TestCase):
    """Default TrustPolicy meets all production-safe baseline expectations."""

    def test_default_policy_version_is_1(self):
        p = TrustPolicy.defaults()
        self.assertEqual(p.policy_version, 1)

    def test_default_require_https(self):
        p = TrustPolicy.defaults()
        self.assertTrue(p.require_https)

    def test_default_allowed_schemes_https_only(self):
        p = TrustPolicy.defaults()
        self.assertEqual(p.allowed_schemes, ("https",))

    def test_default_allowed_ports_443_8443(self):
        p = TrustPolicy.defaults()
        self.assertEqual(set(p.allowed_ports), {443, 8443})

    def test_default_a2a_versions_pinned(self):
        p = TrustPolicy.defaults()
        self.assertEqual(p.allowed_a2a_versions, ("1.0.0",))

    def test_default_ucp_versions_pinned(self):
        p = TrustPolicy.defaults()
        self.assertEqual(p.allowed_ucp_versions, ("2026-04-08",))

    def test_default_knp_versions_empty(self):
        p = TrustPolicy.defaults()
        self.assertEqual(p.allowed_knp_versions, ())

    def test_default_redirect_limit(self):
        p = TrustPolicy.defaults()
        self.assertEqual(p.redirect_limit, 5)

    def test_default_max_profile_bytes_1mb(self):
        p = TrustPolicy.defaults()
        self.assertEqual(p.max_profile_bytes, 1_048_576)

    def test_default_allow_jws(self):
        p = TrustPolicy.defaults()
        self.assertTrue(p.allow_agent_card_jws)
        self.assertFalse(p.require_agent_card_jws)

    def test_default_profile_max_age_24h(self):
        p = TrustPolicy.defaults()
        self.assertEqual(p.profile_max_age_seconds, 86400)


class TrustPolicyFromConfigTest(unittest.TestCase):
    """TrustPolicy.from_config() produces the expected values."""

    def test_from_config_custom_ports(self):
        p = TrustPolicy.from_config(allowed_ports=(443,))
        self.assertEqual(p.allowed_ports, (443,))

    def test_from_config_custom_redirect_limit(self):
        p = TrustPolicy.from_config(redirect_limit=3)
        self.assertEqual(p.redirect_limit, 3)

    def test_from_config_require_jws(self):
        p = TrustPolicy.from_config(require_agent_card_jws=True)
        self.assertTrue(p.require_agent_card_jws)

    def test_from_config_live_refresh(self):
        p = TrustPolicy.from_config(require_live_refresh_before_connect=True)
        self.assertTrue(p.require_live_refresh_before_connect)

    def test_from_config_custom_a2a_versions(self):
        p = TrustPolicy.from_config(allowed_a2a_versions=["1.0.0", "1.1.0"])
        self.assertEqual(p.allowed_a2a_versions, ("1.0.0", "1.1.0"))

    def test_from_config_knp_versions(self):
        p = TrustPolicy.from_config(allowed_knp_versions=["1.0.0"])
        self.assertEqual(p.allowed_knp_versions, ("1.0.0",))


class TrustPolicyPermissiveLocalTest(unittest.TestCase):
    """permissive_local() is dev-only and must never be used in production."""

    def test_permissive_allows_http(self):
        p = TrustPolicy.permissive_local()
        self.assertFalse(p.require_https)
        self.assertIn("http", p.allowed_schemes)

    def test_permissive_all_ports(self):
        p = TrustPolicy.permissive_local()
        self.assertEqual(p.allowed_ports, tuple(range(1, 65536)))

    def test_permissive_long_age(self):
        p = TrustPolicy.permissive_local()
        self.assertEqual(p.profile_max_age_seconds, 604800)


class TrustPolicySnapshotTest(unittest.TestCase):
    """snapshot() returns a JSON-serialisable audit record."""

    def test_snapshot_contains_all_keys(self):
        p = TrustPolicy.defaults()
        snap = p.snapshot()
        expected_keys = {
            "policy_version", "require_https", "allowed_schemes",
            "allowed_ports", "domain_control_method",
            "require_live_refresh_before_connect", "profile_max_age_seconds",
            "allow_agent_card_jws", "require_agent_card_jws",
            "allowed_a2a_versions", "allowed_ucp_versions",
            "allowed_knp_versions", "redirect_limit", "max_profile_bytes",
        }
        self.assertEqual(set(snap.keys()), expected_keys)

    def test_snapshot_is_serialisable(self):
        p = TrustPolicy.defaults()
        snap = p.snapshot()
        encoded = json.dumps(snap)
        self.assertIsInstance(encoded, str)

    def test_snapshot_has_version(self):
        p = TrustPolicy(policy_version=5)
        self.assertEqual(p.snapshot()["policy_version"], 5)


class TrustPolicyFrozenTest(unittest.TestCase):
    """TrustPolicy is frozen — fields cannot be mutated after construction."""

    def test_cannot_set_attribute(self):
        p = TrustPolicy.defaults()
        with self.assertRaises(Exception):  # dataclasses.FrozenInstanceError or AttributeError
            p.policy_version = 99  # type: ignore[misc]

    def test_policy_version_must_be_positive(self):
        with self.assertRaises(ValueError):
            TrustPolicy(policy_version=0)


# ──────────────────────────────────────────────────────────────────────────────
# SSRF IP-level tests
# ──────────────────────────────────────────────────────────────────────────────


class SSRFIPBlockTest(unittest.TestCase):
    """Every blocked address category is caught."""

    def test_loopback_ipv4_blocked(self):
        ip = ipaddress.IPv4Address("127.0.0.1")
        reason = _is_blocked_ip(ip)
        self.assertIsNotNone(reason)
        self.assertIn("loopback", reason)

    def test_loopback_ipv4_127_0_0_2_blocked(self):
        ip = ipaddress.IPv4Address("127.0.0.2")
        self.assertIsNotNone(_is_blocked_ip(ip))

    def test_loopback_ipv6_blocked(self):
        ip = ipaddress.IPv6Address("::1")
        self.assertIsNotNone(_is_blocked_ip(ip))

    def test_private_10_x_blocked(self):
        ip = ipaddress.IPv4Address("10.0.0.1")
        self.assertIsNotNone(_is_blocked_ip(ip))

    def test_private_172_16_x_blocked(self):
        ip = ipaddress.IPv4Address("172.16.0.1")
        self.assertIsNotNone(_is_blocked_ip(ip))

    def test_private_192_168_x_blocked(self):
        ip = ipaddress.IPv4Address("192.168.1.1")
        self.assertIsNotNone(_is_blocked_ip(ip))

    def test_metadata_169_254_169_254_blocked(self):
        ip = ipaddress.IPv4Address("169.254.169.254")
        reason = _is_blocked_ip(ip)
        self.assertIsNotNone(reason)
        self.assertIn("metadata", reason)

    def test_link_local_ipv4_blocked(self):
        ip = ipaddress.IPv4Address("169.254.1.1")
        self.assertIsNotNone(_is_blocked_ip(ip))

    def test_link_local_ipv6_blocked(self):
        ip = ipaddress.IPv6Address("fe80::1")
        self.assertIsNotNone(_is_blocked_ip(ip))

    def test_ipv4_mapped_loopback_blocked(self):
        ip = ipaddress.IPv6Address("::ffff:127.0.0.1")
        self.assertIsNotNone(_is_blocked_ip(ip))

    def test_public_ip_allowed(self):
        ip = ipaddress.IPv4Address("93.184.216.34")  # example.com
        self.assertIsNone(_is_blocked_ip(ip))

    def test_public_ipv6_allowed(self):
        ip = ipaddress.IPv6Address("2606:2800:220:1:248:1893:25c8:1946")
        self.assertIsNone(_is_blocked_ip(ip))

    def test_zero_network_blocked(self):
        ip = ipaddress.IPv4Address("0.0.0.0")
        self.assertIsNotNone(_is_blocked_ip(ip))

    def test_cgnat_100_64_blocked(self):
        ip = ipaddress.IPv4Address("100.64.0.1")
        self.assertIsNotNone(_is_blocked_ip(ip))


# ──────────────────────────────────────────────────────────────────────────────
# SSRF DNS resolution tests
# ──────────────────────────────────────────────────────────────────────────────


class SSRFDNSResolveTest(unittest.TestCase):
    """DNS resolution that returns blocked IPs is rejected."""

    def test_hostname_resolving_to_loopback_blocked(self):
        with patch("socket.getaddrinfo") as mock_gai:
            mock_gai.return_value = [
                (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", 443)),
            ]
            with self.assertRaises(SSRFBlockError) as ctx:
                _resolve_and_validate("evil.local", 443)
            self.assertIn("127.0.0.1", str(ctx.exception))

    def test_hostname_resolving_to_10_x_blocked(self):
        with patch("socket.getaddrinfo") as mock_gai:
            mock_gai.return_value = [
                (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("10.0.0.5", 443)),
            ]
            with self.assertRaises(SSRFBlockError):
                _resolve_and_validate("internal.corp", 443)

    def test_hostname_resolving_to_metadata_ip_blocked(self):
        with patch("socket.getaddrinfo") as mock_gai:
            mock_gai.return_value = [
                (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("169.254.169.254", 443)),
            ]
            with self.assertRaises(SSRFBlockError):
                _resolve_and_validate("metadata.cloud", 443)

    def test_hostname_resolving_to_public_ip_allowed(self):
        with patch("socket.getaddrinfo") as mock_gai:
            mock_gai.return_value = [
                (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 443)),
            ]
            ip = _resolve_and_validate("example.com", 443)
            self.assertEqual(ip, ipaddress.IPv4Address("93.184.216.34"))

    def test_hostname_not_resolvable_raises(self):
        with patch("socket.getaddrinfo") as mock_gai:
            mock_gai.side_effect = socket.gaierror("Name or service not known")
            with self.assertRaises(SSRFBlockError):
                _resolve_and_validate("nonexistent.invalid", 443)


# ──────────────────────────────────────────────────────────────────────────────
# SSRF scheme / port validation tests
# ──────────────────────────────────────────────────────────────────────────────


class SSRFURLValidationTest(unittest.TestCase):
    """Scheme and port rejections happen before any network I/O."""

    def setUp(self):
        self.policy = TrustPolicy.defaults()
        self.fetcher = ProfileFetcher(self.policy)

    def test_file_scheme_rejected(self):
        with self.assertRaises(SSRFBlockError) as ctx:
            self.fetcher._validate_url("file:///etc/passwd")
        self.assertIn("file", str(ctx.exception).lower())

    def test_ftp_scheme_rejected(self):
        with self.assertRaises(SSRFBlockError) as ctx:
            self.fetcher._validate_url("ftp://evil.com/profile.json")
        self.assertIn("ftp", str(ctx.exception).lower())

    def test_empty_url_rejected(self):
        with self.assertRaises(SSRFBlockError):
            self.fetcher._validate_url("")

    def test_non_string_url_rejected(self):
        with self.assertRaises(SSRFBlockError):
            self.fetcher._validate_url(None)  # type: ignore[arg-type]

    def test_url_without_hostname_rejected(self):
        with self.assertRaises(SSRFBlockError):
            self.fetcher._validate_url("https:///path")

    def test_http_rejected_when_require_https(self):
        with self.assertRaises(SSRFBlockError) as ctx:
            self.fetcher.fetch("http://example.com/agent-card.json")
        self.assertIn("not allowed", str(ctx.exception).lower())

    def test_non_default_port_rejected(self):
        with self.assertRaises(SSRFBlockError) as ctx:
            self.fetcher.fetch("https://example.com:8080/agent-card.json")
        self.assertIn("Port", str(ctx.exception))

    def test_port_8443_allowed(self):
        # Mock DNS to return a valid IP, mock the request to succeed.
        with patch("socket.getaddrinfo") as mock_gai, \
             patch.object(ProfileFetcher, "_make_request") as mock_req:
            mock_gai.return_value = [
                (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 8443)),
            ]
            mock_req.return_value = FetchResult(
                url="https://example.com:8443/agent-card.json",
                status_code=200, body="{}", raw_bytes=b"{}",
            )
            result = self.fetcher.fetch("https://example.com:8443/agent-card.json")
            self.assertEqual(result.status_code, 200)

    def test_http_allowed_with_permissive_policy(self):
        policy = TrustPolicy.permissive_local()
        fetcher = ProfileFetcher(policy)
        # Should validate URL without raising
        parsed = fetcher._validate_url("http://example.com/path")
        self.assertEqual(parsed.scheme, "http")


class SSRFPortValidationUnitTest(unittest.TestCase):
    """Direct _validate_port tests."""

    def test_port_443_allowed(self):
        _validate_port(443, (443, 8443))  # no raise

    def test_port_80_rejected(self):
        with self.assertRaises(SSRFBlockError):
            _validate_port(80, (443, 8443))

    def test_port_22_rejected(self):
        with self.assertRaises(SSRFBlockError):
            _validate_port(22, (443, 8443))


class SSRFSchemeValidationUnitTest(unittest.TestCase):
    """Direct _validate_scheme tests."""

    def test_https_allowed(self):
        _validate_scheme("https", ("https",))  # no raise

    def test_http_rejected(self):
        with self.assertRaises(SSRFBlockError):
            _validate_scheme("http", ("https",))


# ──────────────────────────────────────────────────────────────────────────────
# SSRF redirect tests
# ──────────────────────────────────────────────────────────────────────────────


class SSRFRedirectTest(unittest.TestCase):
    """Redirect targets are re-validated with full SSRF checks."""

    def setUp(self):
        self.policy = TrustPolicy.defaults()
        self.fetcher = ProfileFetcher(self.policy)

    def test_redirect_to_loopback_rejected(self):
        with self.assertRaises(SSRFBlockError) as ctx:
            self.fetcher._validate_redirect_target("https://127.0.0.1/agent-card.json")
        self.assertIn("127.0.0.1", str(ctx.exception))

    def test_redirect_to_private_rejected(self):
        with self.assertRaises(SSRFBlockError):
            self.fetcher._validate_redirect_target("https://10.0.0.1/profile.json")

    def test_redirect_to_http_rejected(self):
        with self.assertRaises(SSRFBlockError) as ctx:
            self.fetcher._validate_redirect_target("http://safe.com/agent-card.json")
        self.assertIn("not allowed", str(ctx.exception).lower())

    def test_redirect_to_non_allowed_port_rejected(self):
        with self.assertRaises(SSRFBlockError) as ctx:
            self.fetcher._validate_redirect_target("https://safe.com:8080/profile.json")
        self.assertIn("Port", str(ctx.exception))

    def test_redirect_to_public_valid_passes(self):
        with patch("socket.getaddrinfo") as mock_gai:
            mock_gai.return_value = [
                (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 443)),
            ]
            ip = self.fetcher._validate_redirect_target("https://safe.com/agent-card.json")
            self.assertEqual(ip, ipaddress.IPv4Address("93.184.216.34"))


# ──────────────────────────────────────────────────────────────────────────────
# SSRF redirect integration tests (cross-host redirect IP store fix)
# ──────────────────────────────────────────────────────────────────────────────


class SSRFRedirectIntegrationTest(unittest.TestCase):
    """End-to-end redirect tests verifying the IP store fix for cross-host redirects.

    ALL network layers are mocked — zero real outbound requests.
    """

    def setUp(self):
        self.policy = TrustPolicy.defaults()
        self.fetcher = ProfileFetcher(self.policy)

    # ── helpers ────────────────────────────────────────────────────────────

    @staticmethod
    def _dns_mock(dns_map: dict[str, str]):
        """Return a ``socket.getaddrinfo`` side_effect keyed by hostname."""
        def mock_getaddrinfo(host, port, *args, **kwargs):
            ip = dns_map.get(host)
            if ip is None:
                raise socket.gaierror(f"Name or service not known: {host}")
            return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, port))]
        return mock_getaddrinfo

    @staticmethod
    def _build_response(status: int, headers: list[tuple[str, str]], body: bytes,
                        reason: str = "OK"):
        """Build a mock ``http.client.HTTPResponse``.

        Includes the attributes needed by both ``_process_response``
        (status, getheaders, read) and by ``HTTPErrorProcessor.http_response``
        (code, msg, info()).
        """
        # Build a case-insensitive dict that the redirect handler can query
        # with ``"location" in headers`` and ``headers["location"]``.
        headers_dict: dict[str, str] = {}
        for k, v in headers:
            headers_dict[k.lower()] = v

        resp = MagicMock()
        resp.status = status
        resp.code = status
        resp.msg = reason
        resp.reason = reason
        resp.getheaders.return_value = headers
        resp.getheader.side_effect = lambda name, default=None: headers_dict.get(name.lower(), default)
        resp.info.return_value = headers_dict
        resp.read.side_effect = [body, b""]
        # Support ``with opener.open(...) as response:``.  ``_make_request``
        # uses the response as a context manager; on a bare MagicMock
        # ``__enter__`` returns a *fresh* unconfigured mock whose ``read()``
        # returns a truthy MagicMock forever (infinite loop in _read_limited).
        # Pin ``__enter__`` to the response itself.
        resp.__enter__.return_value = resp
        resp.__exit__.return_value = False
        return resp

    # ── tests ──────────────────────────────────────────────────────────────

    def test_cross_host_redirect_uses_new_verified_ip(self):
        """Cross-host redirect: the new host's verified IP is used for the connection.

        Scenario: ``example.com`` (93.184.216.34) → 302 →
        ``docs.example.com`` (93.184.216.35) → 200.
        Assert that the TCP connection for the second request targets the
        redirect host's IP, not the original.
        """
        dns_map = {
            "example.com": "93.184.216.34",
            "docs.example.com": "93.184.216.35",
        }
        connect_targets: list[tuple[str, int]] = []

        def mock_create_connection(address, timeout=None):
            connect_targets.append(address)
            return MagicMock()

        mock_ssl_ctx = MagicMock()
        mock_ssl_ctx.wrap_socket.return_value = MagicMock()

        # Response chain: 302 → 200
        response_chain = [
            self._build_response(302, [("Location", "https://docs.example.com/agent-card.json")], b""),
            self._build_response(200, [("Content-Type", "application/json")], b'{"ok":true}'),
        ]
        call_idx = [0]

        def mock_getresponse(self_conn):
            idx = call_idx[0]
            call_idx[0] += 1
            if idx >= len(response_chain):
                raise RuntimeError(f"Unexpected getresponse call #{idx}")
            return response_chain[idx]

        # NOTE: ``HTTPConnection.request`` is intentionally NOT mocked — the
        # real request() path is what triggers ``_ProtectedHTTPSConnection.
        # connect()`` → ``socket.create_connection``, which is the DNS-rebinding
        # guarantee under test.  All network I/O is still mocked.
        with patch("socket.getaddrinfo", side_effect=self._dns_mock(dns_map)), \
             patch("socket.create_connection", side_effect=mock_create_connection), \
             patch("ssl.create_default_context", return_value=mock_ssl_ctx), \
             patch("http.client.HTTPConnection.getresponse", mock_getresponse):

            result = self.fetcher.fetch("https://example.com/agent-card.json")

        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.parsed, {"ok": True})

        # Two connections: original host → redirect host
        self.assertEqual(len(connect_targets), 2,
                         f"Expected 2 connections, got {connect_targets}")
        self.assertEqual(connect_targets[0], ("93.184.216.34", 443),
                         "First connection must use original host IP")
        self.assertEqual(connect_targets[1], ("93.184.216.35", 443),
                         "Second connection must use redirect host IP, not original")

    def test_cross_host_redirect_to_private_ip_blocked(self):
        """Cross-host redirect to a private-IP host → SSRFBlockError.

        Scenario: ``example.com`` (public) → 302 →
        ``evil.internal`` resolves to ``10.0.0.5`` → blocked before connection.
        """
        dns_map = {
            "example.com": "93.184.216.34",
            "evil.internal": "10.0.0.5",
        }
        connect_targets: list[tuple[str, int]] = []

        def mock_create_connection(address, timeout=None):
            connect_targets.append(address)
            return MagicMock()

        mock_ssl_ctx = MagicMock()
        mock_ssl_ctx.wrap_socket.return_value = MagicMock()

        response_chain = [
            self._build_response(302, [("Location", "https://evil.internal/agent-card.json")], b""),
        ]
        call_idx = [0]

        def mock_getresponse(self_conn):
            idx = call_idx[0]
            call_idx[0] += 1
            if idx >= len(response_chain):
                raise RuntimeError(f"Unexpected getresponse call #{idx}")
            return response_chain[idx]

        # NOTE: ``HTTPConnection.request`` is intentionally NOT mocked — the
        # real request() path is what triggers ``_ProtectedHTTPSConnection.
        # connect()`` → ``socket.create_connection``, which is the DNS-rebinding
        # guarantee under test.  All network I/O is still mocked.
        with patch("socket.getaddrinfo", side_effect=self._dns_mock(dns_map)), \
             patch("socket.create_connection", side_effect=mock_create_connection), \
             patch("ssl.create_default_context", return_value=mock_ssl_ctx), \
             patch("http.client.HTTPConnection.getresponse", mock_getresponse):

            with self.assertRaises(SSRFBlockError) as ctx:
                self.fetcher.fetch("https://example.com/agent-card.json")

        self.assertIn("10.0.0.5", str(ctx.exception))
        # Only the initial request made a connection — redirect was blocked before connecting.
        self.assertEqual(len(connect_targets), 1,
                         f"Only initial connection expected, got {connect_targets}")
        self.assertEqual(connect_targets[0], ("93.184.216.34", 443))

    def test_cross_host_redirect_to_metadata_ip_blocked(self):
        """Cross-host redirect to a host resolving to the cloud metadata IP.

        Scenario: ``example.com`` (public) → 302 →
        ``metadata.internal`` resolves to ``169.254.169.254`` → blocked.
        """
        dns_map = {
            "example.com": "93.184.216.34",
            "metadata.internal": "169.254.169.254",
        }
        connect_targets: list[tuple[str, int]] = []

        def mock_create_connection(address, timeout=None):
            connect_targets.append(address)
            return MagicMock()

        mock_ssl_ctx = MagicMock()
        mock_ssl_ctx.wrap_socket.return_value = MagicMock()

        response_chain = [
            self._build_response(302, [("Location", "https://metadata.internal/agent-card.json")], b""),
        ]
        call_idx = [0]

        def mock_getresponse(self_conn):
            idx = call_idx[0]
            call_idx[0] += 1
            if idx >= len(response_chain):
                raise RuntimeError(f"Unexpected getresponse call #{idx}")
            return response_chain[idx]

        # NOTE: ``HTTPConnection.request`` is intentionally NOT mocked — the
        # real request() path is what triggers ``_ProtectedHTTPSConnection.
        # connect()`` → ``socket.create_connection``, which is the DNS-rebinding
        # guarantee under test.  All network I/O is still mocked.
        with patch("socket.getaddrinfo", side_effect=self._dns_mock(dns_map)), \
             patch("socket.create_connection", side_effect=mock_create_connection), \
             patch("ssl.create_default_context", return_value=mock_ssl_ctx), \
             patch("http.client.HTTPConnection.getresponse", mock_getresponse):

            with self.assertRaises(SSRFBlockError) as ctx:
                self.fetcher.fetch("https://example.com/agent-card.json")

        self.assertIn("169.254.169.254", str(ctx.exception))
        # Only the initial request made a connection — redirect was blocked before connecting.
        self.assertEqual(len(connect_targets), 1,
                         f"Only initial connection expected, got {connect_targets}")
        self.assertEqual(connect_targets[0], ("93.184.216.34", 443))

    def test_redirect_limit_exceeded_raises_ssrf_block_error(self):
        """Exceeding redirect_limit raises SSRFBlockError."""
        dns_map = {
            "example.com": "93.184.216.34",
            "redirect1.example.com": "93.184.216.35",
        }
        connect_targets: list[tuple[str, int]] = []

        def mock_create_connection(address, timeout=None):
            connect_targets.append(address)
            return MagicMock()

        mock_ssl_ctx = MagicMock()
        mock_ssl_ctx.wrap_socket.return_value = MagicMock()

        policy = TrustPolicy.from_config(redirect_limit=1)
        fetcher = ProfileFetcher(policy)

        response_chain = [
            self._build_response(302, [("Location", "https://redirect1.example.com/agent-card.json")], b""),
            self._build_response(302, [("Location", "https://redirect2.example.com/agent-card.json")], b""),
        ]
        call_idx = [0]

        def mock_getresponse(self_conn):
            idx = call_idx[0]
            call_idx[0] += 1
            if idx >= len(response_chain):
                raise RuntimeError(f"Unexpected getresponse call #{idx}")
            return response_chain[idx]

        # NOTE: ``HTTPConnection.request`` is intentionally NOT mocked — the
        # real request() path is what triggers ``_ProtectedHTTPSConnection.
        # connect()`` → ``socket.create_connection``, which is the DNS-rebinding
        # guarantee under test.  All network I/O is still mocked.
        with patch("socket.getaddrinfo", side_effect=self._dns_mock(dns_map)), \
             patch("socket.create_connection", side_effect=mock_create_connection), \
             patch("ssl.create_default_context", return_value=mock_ssl_ctx), \
             patch("http.client.HTTPConnection.getresponse", mock_getresponse):

            with self.assertRaises(SSRFBlockError) as ctx:
                fetcher.fetch("https://example.com/agent-card.json")

        self.assertIn("Redirect limit", str(ctx.exception))

    def test_handler_fail_closed_no_verified_ip_for_host(self):
        """Handler raises SSRFBlockError when a host has no verified IP in the store.

        The opener only has an entry for ``example.com``.  A request for
        ``other.example.com`` must be blocked before any network I/O (fail-closed).
        """
        from shopping_cli.discovery.fetcher import _build_opener

        def mock_validator(url: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
            return ipaddress.IPv4Address("93.184.216.34")

        opener = _build_opener(
            "93.184.216.34", "example.com", 443, 5, mock_validator,
        )

        req = urllib.request.Request("https://other.example.com/path")
        with self.assertRaises(SSRFBlockError) as ctx:
            opener.open(req)

        self.assertIn("other.example.com", str(ctx.exception))
        self.assertIn("fail-closed", str(ctx.exception))

    def test_same_host_redirect_no_regression(self):
        """Same-host redirect still works (no regression).

        Scenario: ``example.com`` → 302 →
        ``example.com/agent-card.json`` → 200.
        The IP store key is the same, so the connection reuses the original IP.
        """
        dns_map = {"example.com": "93.184.216.34"}
        connect_targets: list[tuple[str, int]] = []

        def mock_create_connection(address, timeout=None):
            connect_targets.append(address)
            return MagicMock()

        mock_ssl_ctx = MagicMock()
        mock_ssl_ctx.wrap_socket.return_value = MagicMock()

        response_chain = [
            self._build_response(302, [("Location", "https://example.com/agent-card.json")], b""),
            self._build_response(200, [("Content-Type", "application/json")], b'{"ok":true}'),
        ]
        call_idx = [0]

        def mock_getresponse(self_conn):
            idx = call_idx[0]
            call_idx[0] += 1
            if idx >= len(response_chain):
                raise RuntimeError(f"Unexpected getresponse call #{idx}")
            return response_chain[idx]

        # NOTE: ``HTTPConnection.request`` is intentionally NOT mocked — the
        # real request() path is what triggers ``_ProtectedHTTPSConnection.
        # connect()`` → ``socket.create_connection``, which is the DNS-rebinding
        # guarantee under test.  All network I/O is still mocked.
        with patch("socket.getaddrinfo", side_effect=self._dns_mock(dns_map)), \
             patch("socket.create_connection", side_effect=mock_create_connection), \
             patch("ssl.create_default_context", return_value=mock_ssl_ctx), \
             patch("http.client.HTTPConnection.getresponse", mock_getresponse):

            result = self.fetcher.fetch("https://example.com/profile")

        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.parsed, {"ok": True})
        # Both connections target the same IP.
        self.assertEqual(len(connect_targets), 2)
        self.assertEqual(connect_targets[0], ("93.184.216.34", 443))
        self.assertEqual(connect_targets[1], ("93.184.216.34", 443))


# ──────────────────────────────────────────────────────────────────────────────
# ProfileFetcher HTTP-level tests (all mocked)
# ──────────────────────────────────────────────────────────────────────────────


def _mock_fetch_response(status=200, body=b'{"key":"value"}', headers=None, url="https://example.com/agent-card.json"):
    """Factory for a mock HTTPResponse.

    Uses ``side_effect`` so ``read()`` returns *body* once, then empty bytes
    — matching the behaviour of a real stream that is exhausted after one read.
    """
    if headers is None:
        headers = [("Content-Type", "application/json")]
    resp = MagicMock()
    resp.status = status
    resp.getheaders.return_value = headers
    resp.read.side_effect = [body, b""]
    return resp


class FetcherHTTPTest(unittest.TestCase):
    """Happy-path and edge-case HTTP responses with mocked network."""

    def setUp(self):
        self.policy = TrustPolicy.defaults()
        self.fetcher = ProfileFetcher(self.policy)

    def test_fetch_200_returns_body(self):
        with patch("socket.getaddrinfo") as mock_gai, \
             patch.object(ProfileFetcher, "_make_request") as mock_req:
            mock_gai.return_value = [
                (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 443)),
            ]
            mock_req.return_value = FetchResult(
                url="https://example.com/agent-card.json",
                status_code=200,
                body='{"name":"Test Agent"}',
                raw_bytes=b'{"name":"Test Agent"}',
                parsed={"name": "Test Agent"},
            )
            result = self.fetcher.fetch("https://example.com/agent-card.json")
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.parsed, {"name": "Test Agent"})
            self.assertFalse(result.is_not_modified)

    def test_fetch_304_not_modified(self):
        with patch("socket.getaddrinfo") as mock_gai, \
             patch.object(ProfileFetcher, "_make_request") as mock_req:
            mock_gai.return_value = [
                (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 443)),
            ]
            mock_req.return_value = FetchResult(
                url="https://example.com/agent-card.json",
                status_code=304,
            )
            result = self.fetcher.fetch("https://example.com/agent-card.json")
            self.assertEqual(result.status_code, 304)
            self.assertTrue(result.is_not_modified)
            self.assertEqual(result.body, "")

    def test_fetch_with_etag_conditional(self):
        with patch("socket.getaddrinfo") as mock_gai, \
             patch.object(ProfileFetcher, "_make_request") as mock_req:
            mock_gai.return_value = [
                (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 443)),
            ]
            mock_req.return_value = FetchResult(
                url="https://example.com/agent-card.json",
                status_code=200,
                body="{}", raw_bytes=b"{}",
                etag='"abc123"',
            )
            result = self.fetcher.fetch(
                "https://example.com/agent-card.json",
                etag='"abc123"',
            )
            self.assertEqual(result.etag, '"abc123"')

    def test_fetch_timeout_raises(self):
        with patch("socket.getaddrinfo") as mock_gai, \
             patch.object(ProfileFetcher, "_make_request") as mock_req:
            mock_gai.return_value = [
                (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 443)),
            ]
            mock_req.side_effect = FetchError("Request timed out after 10.0s")
            with self.assertRaises(FetchError) as ctx:
                self.fetcher.fetch("https://example.com/agent-card.json")
            self.assertIn("timed out", str(ctx.exception))

    def test_fetch_size_limit_exceeded(self):
        with patch("socket.getaddrinfo") as mock_gai, \
             patch.object(ProfileFetcher, "_make_request") as mock_req:
            mock_gai.return_value = [
                (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 443)),
            ]
            mock_req.side_effect = FetchLimitError("Response body exceeds max_profile_bytes")
            with self.assertRaises(FetchLimitError) as ctx:
                self.fetcher.fetch("https://example.com/large.json")
            self.assertIn("max_profile_bytes", str(ctx.exception))

    def test_fetch_json_depth_exceeded(self):
        with patch("socket.getaddrinfo") as mock_gai, \
             patch.object(ProfileFetcher, "_make_request") as mock_req:
            mock_gai.return_value = [
                (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 443)),
            ]
            mock_req.side_effect = FetchLimitError("JSON exceeds max depth of 20")
            with self.assertRaises(FetchLimitError) as ctx:
                self.fetcher.fetch("https://example.com/deep.json")
            self.assertIn("depth", str(ctx.exception))

    def test_fetcher_rejects_zero_timeout(self):
        with self.assertRaises(ValueError):
            ProfileFetcher(self.policy, timeout=0)

    def test_fetcher_rejects_negative_timeout(self):
        with self.assertRaises(ValueError):
            ProfileFetcher(self.policy, timeout=-1)


class FetcherResponseProcessingTest(unittest.TestCase):
    """Tests for _process_response with size/depth checks."""

    def setUp(self):
        self.policy = TrustPolicy.defaults()
        self.fetcher = ProfileFetcher(self.policy)

    def test_process_valid_json(self):
        resp = _mock_fetch_response(body=b'{"a":1}')
        result = self.fetcher._process_response(resp, "https://example.com", 1000.0)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.parsed, {"a": 1})

    def test_process_304_no_body(self):
        resp = _mock_fetch_response(status=304, body=b"")
        result = self.fetcher._process_response(resp, "https://example.com", 1000.0)
        self.assertEqual(result.status_code, 304)
        self.assertTrue(result.is_not_modified)

    def test_process_cache_headers_preserved(self):
        headers = [
            ("Content-Type", "application/json"),
            ("ETag", '"xyz789"'),
            ("Last-Modified", "Wed, 21 Oct 2026 07:28:00 GMT"),
            ("Cache-Control", "max-age=3600"),
        ]
        resp = _mock_fetch_response(body=b"{}", headers=headers)
        result = self.fetcher._process_response(resp, "https://example.com", 1000.0)
        self.assertEqual(result.etag, '"xyz789"')
        self.assertEqual(result.last_modified, "Wed, 21 Oct 2026 07:28:00 GMT")
        self.assertEqual(result.cache_control, "max-age=3600")
        self.assertEqual(result.max_age, 3600)

    def test_process_invalid_json_raises(self):
        resp = _mock_fetch_response(body=b"not json")
        with self.assertRaises(FetchLimitError):
            self.fetcher._process_response(resp, "https://example.com", 1000.0)


class FetcherSizeTruncationTest(unittest.TestCase):
    """Body streaming enforces max_profile_bytes."""

    def test_body_within_limit_passes(self):
        """Simulate a body read that stays under the limit."""
        policy = TrustPolicy(max_profile_bytes=100)
        fetcher = ProfileFetcher(policy)

        resp = MagicMock()
        resp.status = 200
        resp.getheaders.return_value = [("Content-Type", "application/json")]
        # Return valid JSON under the limit, then empty
        resp.read.side_effect = [b'{"ok":true}', b""]

        result = fetcher._process_response(resp, "https://example.com", 1000.0)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.parsed, {"ok": True})

    def test_body_exceeds_limit_raises(self):
        """Simulate a body read that exceeds the limit."""
        policy = TrustPolicy(max_profile_bytes=10)
        fetcher = ProfileFetcher(policy)

        resp = MagicMock()
        resp.status = 200
        resp.getheaders.return_value = [("Content-Type", "application/json")]
        # Return a single chunk that exceeds the 10-byte limit
        resp.read.side_effect = [b"12345678901", b""]

        with self.assertRaises(FetchLimitError) as ctx:
            fetcher._process_response(resp, "https://example.com", 1000.0)
        self.assertIn("max_profile_bytes", str(ctx.exception))


class FetcherJSONDepthTest(unittest.TestCase):
    """JSON structure limits are enforced after parsing."""

    def test_shallow_json_passes(self):
        from shopping_cli.discovery.fetcher import _validate_json_structure
        obj = {"a": 1, "b": [2, 3]}
        count = _validate_json_structure(obj, max_depth=5, max_nodes=100)
        self.assertGreater(count, 0)

    def test_deep_json_rejected(self):
        from shopping_cli.discovery.fetcher import _validate_json_structure
        # Build a deeply nested dict
        obj = {}
        cur = obj
        for i in range(50):
            cur["nested"] = {}
            cur = cur["nested"]
        cur["leaf"] = 1
        with self.assertRaises(FetchLimitError) as ctx:
            _validate_json_structure(obj, max_depth=20, max_nodes=10000)
        self.assertIn("depth", str(ctx.exception))

    def test_many_nodes_rejected(self):
        from shopping_cli.discovery.fetcher import _validate_json_structure
        obj = [{"k": i} for i in range(50)]
        with self.assertRaises(FetchLimitError) as ctx:
            _validate_json_structure(obj, max_depth=20, max_nodes=5)
        self.assertIn("node", str(ctx.exception))


# ──────────────────────────────────────────────────────────────────────────────
# FetchResult tests
# ──────────────────────────────────────────────────────────────────────────────


class FetchResultTest(unittest.TestCase):
    """FetchResult computed properties."""

    def test_is_not_modified_true_for_304(self):
        r = FetchResult(url="https://x.com", status_code=304)
        self.assertTrue(r.is_not_modified)
        self.assertTrue(r.is_success)

    def test_is_not_modified_false_for_200(self):
        r = FetchResult(url="https://x.com", status_code=200, body="{}", raw_bytes=b"{}")
        self.assertFalse(r.is_not_modified)
        self.assertTrue(r.is_success)

    def test_is_success_false_for_500(self):
        r = FetchResult(url="https://x.com", status_code=500)
        self.assertFalse(r.is_success)

    def test_compute_fresh_until_uses_max_age(self):
        r = FetchResult(
            url="https://x.com", status_code=200,
            body="{}", raw_bytes=b"{}",
            max_age=3600, fetched_at=1000.0,
        )
        self.assertEqual(r.compute_fresh_until(86400), 4600.0)

    def test_compute_fresh_until_falls_back_to_policy(self):
        r = FetchResult(
            url="https://x.com", status_code=200,
            body="{}", raw_bytes=b"{}",
            max_age=None, fetched_at=1000.0,
        )
        self.assertEqual(r.compute_fresh_until(86400), 87400.0)


# ──────────────────────────────────────────────────────────────────────────────
# Cache tests (cache.py)
# ──────────────────────────────────────────────────────────────────────────────


class CacheDirectiveTest(unittest.TestCase):
    """CacheDirective parses response headers correctly."""

    def test_parses_etag(self):
        cd = CacheDirective.from_response_headers({"ETag": '"abc"'})
        self.assertEqual(cd.etag, '"abc"')

    def test_parses_weak_etag(self):
        cd = CacheDirective.from_response_headers({"ETag": 'W/"abc"'})
        self.assertEqual(cd.etag, 'W/"abc"')

    def test_parses_last_modified(self):
        cd = CacheDirective.from_response_headers({
            "Last-Modified": "Wed, 21 Oct 2026 07:28:00 GMT",
        })
        self.assertEqual(cd.last_modified, "Wed, 21 Oct 2026 07:28:00 GMT")
        self.assertIsNotNone(cd.last_modified_ts)

    def test_parses_max_age(self):
        cd = CacheDirective.from_response_headers({
            "Cache-Control": "max-age=3600, public",
        })
        self.assertEqual(cd.max_age, 3600)

    def test_missing_headers_produce_none(self):
        cd = CacheDirective.from_response_headers({})
        self.assertIsNone(cd.etag)
        self.assertIsNone(cd.last_modified)
        self.assertIsNone(cd.max_age)

    def test_case_insensitive_header_names(self):
        cd = CacheDirective.from_response_headers({"etag": '"lowercase-etag"'})
        self.assertEqual(cd.etag, '"lowercase-etag"')

    def test_fetched_at_custom(self):
        cd = CacheDirective.from_response_headers({}, fetched_at=500.0)
        self.assertEqual(cd.fetched_at_ts, 500.0)

    def test_compute_fresh_until_with_max_age(self):
        cd = CacheDirective(max_age=3600, fetched_at_ts=1000.0)
        self.assertEqual(cd.compute_fresh_until(86400), 4600.0)

    def test_compute_fresh_until_with_policy_fallback(self):
        cd = CacheDirective(max_age=None, fetched_at_ts=1000.0)
        self.assertEqual(cd.compute_fresh_until(86400), 87400.0)


class ConditionalHeadersTest(unittest.TestCase):
    """build_conditional_headers produces correct request headers."""

    def test_both_etag_and_last_modified(self):
        headers = build_conditional_headers(
            etag='"abc"',
            last_modified="Wed, 21 Oct 2026 07:28:00 GMT",
        )
        self.assertEqual(headers["If-None-Match"], '"abc"')
        self.assertEqual(headers["If-Modified-Since"], "Wed, 21 Oct 2026 07:28:00 GMT")

    def test_etag_only(self):
        headers = build_conditional_headers(etag='"abc"')
        self.assertIn("If-None-Match", headers)
        self.assertNotIn("If-Modified-Since", headers)

    def test_last_modified_only(self):
        headers = build_conditional_headers(last_modified="Wed, 21 Oct 2026 07:28:00 GMT")
        self.assertNotIn("If-None-Match", headers)
        self.assertIn("If-Modified-Since", headers)

    def test_none_args_produce_empty(self):
        headers = build_conditional_headers()
        self.assertEqual(headers, {})

    def test_empty_strings_produce_empty(self):
        headers = build_conditional_headers(etag="", last_modified="")
        self.assertEqual(headers, {})


class CacheStateTest(unittest.TestCase):
    """Three-state freshness model (§18)."""

    def test_fresh_when_now_before_fresh_until(self):
        state = compute_cache_state(fresh_until=2000.0, now=1000.0)
        self.assertEqual(state, CacheState.FRESH)

    def test_stale_unusable_when_past_fresh_until_no_grace(self):
        state = compute_cache_state(fresh_until=1000.0, now=2000.0)
        self.assertEqual(state, CacheState.STALE_UNUSABLE)

    def test_stale_usable_when_within_grace_period(self):
        state = compute_cache_state(
            fresh_until=1000.0, now=1500.0, stale_usable_seconds=600,
        )
        self.assertEqual(state, CacheState.STALE_USABLE)

    def test_stale_unusable_when_past_grace_period(self):
        state = compute_cache_state(
            fresh_until=1000.0, now=2000.0, stale_usable_seconds=600,
        )
        self.assertEqual(state, CacheState.STALE_UNUSABLE)

    def test_exactly_at_fresh_until_is_stale(self):
        state = compute_cache_state(fresh_until=1000.0, now=1000.0)
        self.assertEqual(state, CacheState.STALE_UNUSABLE)

    def test_now_defaults_to_current_time(self):
        # fresh_until far in future → FRESH
        import time
        state = compute_cache_state(fresh_until=time.time() + 3600)
        self.assertEqual(state, CacheState.FRESH)


class SnapshotMetaTest(unittest.TestCase):
    """snapshot_meta produces DB-compatible fields."""

    def test_output_has_required_keys(self):
        cd = CacheDirective(etag='"abc"', last_modified="Wed, 21 Oct 2026 07:28:00 GMT")
        meta = snapshot_meta(
            directive=cd,
            content='{"key":"value"}',
            policy_max_age_seconds=86400,
        )
        for key in ("etag", "last_modified", "content_hash", "fetched_at", "fresh_until"):
            self.assertIn(key, meta)

    def test_content_hash_is_stable(self):
        cd = CacheDirective()
        meta1 = snapshot_meta(directive=cd, content="hello", policy_max_age_seconds=86400)
        meta2 = snapshot_meta(directive=cd, content="hello", policy_max_age_seconds=86400)
        self.assertEqual(meta1["content_hash"], meta2["content_hash"])

    def test_content_hash_differs_for_different_content(self):
        cd = CacheDirective()
        meta1 = snapshot_meta(directive=cd, content="hello", policy_max_age_seconds=86400)
        meta2 = snapshot_meta(directive=cd, content="world", policy_max_age_seconds=86400)
        self.assertNotEqual(meta1["content_hash"], meta2["content_hash"])

    def test_etag_empty_string_for_none(self):
        cd = CacheDirective()
        meta = snapshot_meta(directive=cd, content="{}", policy_max_age_seconds=86400)
        self.assertEqual(meta["etag"], "")


class ContentHashTest(unittest.TestCase):
    """compute_content_hash is deterministic."""

    def test_string_and_bytes_same_hash(self):
        h1 = compute_content_hash("hello")
        h2 = compute_content_hash(b"hello")
        self.assertEqual(h1, h2)

    def test_hash_length(self):
        h = compute_content_hash("data")
        self.assertEqual(len(h), 64)  # SHA-256 hex

    def test_different_inputs_produce_different_hashes(self):
        h1 = compute_content_hash("a")
        h2 = compute_content_hash("b")
        self.assertNotEqual(h1, h2)


# ──────────────────────────────────────────────────────────────────────────────
# Integration: end-to-end with full DNS mock
# ──────────────────────────────────────────────────────────────────────────────


class FetcherEndToEndTest(unittest.TestCase):
    """Full fetch() flow with all network layers mocked."""

    def test_full_fetch_etag_support_passthrough(self):
        """Verify that etag and last_modified are passed into _make_request."""
        policy = TrustPolicy.defaults()
        fetcher = ProfileFetcher(policy)

        with patch("socket.getaddrinfo") as mock_gai, \
             patch.object(ProfileFetcher, "_make_request") as mock_make:
            mock_gai.return_value = [
                (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 443)),
            ]
            expected = FetchResult(
                url="https://example.com/.well-known/agent-card.json",
                status_code=200,
                body='{"name":"A"}', raw_bytes=b'{"name":"A"}',
                etag='"etag-1"', last_modified="Thu, 01 Jan 2026 00:00:00 GMT",
            )
            mock_make.return_value = expected

            result = fetcher.fetch(
                "https://example.com/.well-known/agent-card.json",
                etag='"etag-1"',
                last_modified="Thu, 01 Jan 2026 00:00:00 GMT",
            )

            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.etag, '"etag-1"')
            # Verify _make_request received the conditional headers
            # _make_request(self, url, verified_ip, hostname, port, etag, last_modified, fetched_at)
            call_args = mock_make.call_args
            self.assertEqual(call_args[0][4], '"etag-1"')  # etag arg (index 4)
            self.assertEqual(call_args[0][5], "Thu, 01 Jan 2026 00:00:00 GMT")  # last_modified arg (index 5)

    def test_http_error_passthrough(self):
        """HTTPError (e.g. 404) produces a FetchResult with the error status."""
        policy = TrustPolicy.defaults()
        fetcher = ProfileFetcher(policy)

        with patch("socket.getaddrinfo") as mock_gai, \
             patch.object(ProfileFetcher, "_make_request") as mock_make:
            mock_gai.return_value = [
                (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 443)),
            ]
            mock_make.return_value = FetchResult(
                url="https://example.com/missing.json",
                status_code=404,
            )

            result = fetcher.fetch("https://example.com/missing.json")
            self.assertEqual(result.status_code, 404)


if __name__ == "__main__":
    unittest.main()
