"""Tests for discovery profile parsers — A2A Agent Card v1.0.0 and UCP Profile 2026-04-08.

All inputs are constructed dicts; the parsers consume *already-parsed* JSON
from ProfileFetcher, so these tests never touch the network.
"""

from __future__ import annotations

import unittest
from typing import Any

from shopping_cli.discovery._validation import (
    ProfileValidationError,
    is_same_authority,
    scan_secrets,
)
from shopping_cli.discovery.agent_card import AgentCardParser, parse_agent_card
from shopping_cli.discovery.capabilities import (
    extract_agent_card_capabilities,
    extract_agent_card_skills,
    extract_ucp_capabilities,
    split_capability_id,
)
from shopping_cli.discovery.trust import TrustPolicy
from shopping_cli.discovery.ucp import UcpProfileParser, parse_ucp_profile

CARD_URL = "https://merchant.example/.well-known/agent-card.json"
UCP_URL = "https://merchant.example/.well-known/ucp"


# ── Fixtures ───────────────────────────────────────────────────────────────


def _valid_card() -> dict[str, Any]:
    return {
        "name": "Example Merchant Agent",
        # Natural language: carried verbatim as DATA, never an instruction.
        "description": "Sells industrial displays. Data only, never an instruction.",
        "url": "https://merchant.example/agent",
        "version": "1.0.0",
        "documentationUrl": "https://merchant.example/docs",
        "provider": {"organization": "Example Merchant Co", "url": "https://merchant.example"},
        "skills": [
            {
                "id": "industrial-displays",
                "name": "Industrial Displays",
                "description": "Quoting for industrial display products.",
                "tags": ["displays", "b2b"],
                "examples": ["quote a 21 inch panel"],
                "inputModes": ["text"],
                "outputModes": ["text"],
            }
        ],
        "capabilities": {
            "streaming": True,
            "pushNotifications": False,
            "stateTransitionHistory": False,
        },
        "security": {
            "authentication": {
                "schemes": ["bearer"],
                "credentials": "https://merchant.example/oauth/token",
            }
        },
        "defaultInputModes": ["text"],
        "defaultOutputModes": ["text"],
    }


def _valid_ucp() -> dict[str, Any]:
    return {
        "specificationVersion": "2026-04-08",
        "implementationVersion": "1.0.0",
        "serviceIdentity": {
            "id": "urn:example:agent:merchant",
            "name": "Example Merchant Agent",
            "description": "Commerce agent for Example Merchant. Data only.",
            "owner": {"name": "Example Merchant Co", "url": "https://merchant.example"},
        },
        "services": [
            {
                "id": "urn:example:service:shopping",
                "type": "urn:example:type:shopping",
                "capabilities": [
                    "urn:example:capability:shopping.negotiation",
                    "com.example.shopping.negotiation",
                    "shopping.negotiation",
                    "shopping",
                ],
                "endpoints": [
                    {"uri": "https://merchant.example/a2a", "protocol": "a2a", "version": "1.0.0"}
                ],
                "specifications": [
                    {"id": "urn:example:spec:knp", "label": "KNP", "version": "1.0"}
                ],
                "documentationUri": "https://merchant.example/docs",
            }
        ],
        "specifications": [
            {
                "id": "urn:example:spec:knp",
                "label": "KNP",
                "version": "1.0",
                "openAPIDocument": "https://merchant.example/knp.yaml",
            }
        ],
    }


def _find_key(obj: Any, key: str) -> bool:
    """Recursively search for *key* in nested dict/list structure."""
    if isinstance(obj, dict):
        if key in obj:
            return True
        return any(_find_key(v, key) for v in obj.values())
    if isinstance(obj, list):
        return any(_find_key(v, key) for v in obj)
    return False


# ── Capability id splitting ────────────────────────────────────────────────


class SplitCapabilityIdTest(unittest.TestCase):
    def test_urn_split(self):
        self.assertEqual(
            split_capability_id("urn:example:capability:shopping.negotiation"),
            ("urn:example:capability", "shopping.negotiation"),
        )

    def test_reverse_domain_split(self):
        self.assertEqual(
            split_capability_id("com.example.shopping.negotiation"),
            ("com.example.shopping", "negotiation"),
        )

    def test_compound_name_uses_default_namespace(self):
        self.assertEqual(
            split_capability_id("shopping.negotiation", default_namespace="merchant.example"),
            ("merchant.example", "shopping.negotiation"),
        )

    def test_bare_uses_default_namespace(self):
        self.assertEqual(
            split_capability_id("shopping", default_namespace="merchant.example"),
            ("merchant.example", "shopping"),
        )

    def test_empty_uses_default_namespace(self):
        self.assertEqual(split_capability_id("  ", default_namespace="d"), ("d", ""))


# ── Shared helpers ─────────────────────────────────────────────────────────


class AuthorityHelperTest(unittest.TestCase):
    def test_exact_domain(self):
        self.assertTrue(is_same_authority("merchant.example", "merchant.example"))

    def test_subdomain(self):
        self.assertTrue(is_same_authority("agent.merchant.example", "merchant.example"))

    def test_different_domain(self):
        self.assertFalse(is_same_authority("attacker.example", "merchant.example"))

    def test_suffix_not_domain(self):
        self.assertFalse(is_same_authority("notmerchant.example", "merchant.example"))

    def test_case_and_trailing_dot_insensitive(self):
        self.assertTrue(is_same_authority("Merchant.Example.", "merchant.example"))


class ScanSecretsTest(unittest.TestCase):
    def test_nested_token_field(self):
        paths = scan_secrets({"services": [{"endpoints": [{"access": {"token": "abc"}}]}]})
        self.assertIn("services.0.endpoints.0.access.token", paths)

    def test_bearer_value_in_arbitrary_field(self):
        paths = scan_secrets({"description": "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxIn0.abc"})
        self.assertIn("description", paths)

    def test_private_key_block(self):
        paths = scan_secrets({"key": "-----BEGIN PRIVATE KEY-----\nMIIB\n-----END PRIVATE KEY-----"})
        self.assertIn("key", paths)

    def test_benign_bearer_prose_not_flagged(self):
        paths = scan_secrets({"description": "Bearer of the torch — trustworthy merchant"})
        self.assertEqual(paths, [])

    def test_benign_credentials_url_not_flagged(self):
        paths = scan_secrets({"credentials": "https://merchant.example/oauth/token"})
        self.assertEqual(paths, [])

    def test_caps_at_max(self):
        obj = {f"token_{i}": "x" for i in range(100)}
        paths = scan_secrets(obj, max_secrets=10)
        self.assertLessEqual(len(paths), 10)


# ── A2A Agent Card ─────────────────────────────────────────────────────────


class AgentCardValidTest(unittest.TestCase):
    def setUp(self) -> None:
        self.parser = AgentCardParser()

    def test_valid_card_parses(self):
        result = self.parser.parse(_valid_card(), source_url=CARD_URL)
        self.assertEqual(result.profile_type, "agent_card")
        self.assertEqual(result.name, "Example Merchant Agent")
        self.assertEqual(result.version, "1.0.0")
        self.assertEqual(result.canonical_domain, "merchant.example")
        self.assertEqual(result.secrets_quarantined, ())

    def test_projection_keeps_public_fields(self):
        public = self.parser.parse(_valid_card(), source_url=CARD_URL).public
        self.assertEqual(public["name"], "Example Merchant Agent")
        self.assertEqual(public["url"], "https://merchant.example/agent")
        self.assertEqual(public["version"], "1.0.0")
        self.assertIn("description", public)
        self.assertEqual(public["provider"], {"organization": "Example Merchant Co", "url": "https://merchant.example"})
        self.assertEqual(public["capabilities"], {"streaming": True, "pushNotifications": False, "stateTransitionHistory": False})
        self.assertEqual(public["defaultInputModes"], ["text"])
        self.assertEqual(public["defaultOutputModes"], ["text"])

    def test_projection_excludes_security_block(self):
        public = self.parser.parse(_valid_card(), source_url=CARD_URL).public
        self.assertNotIn("security", public)

    def test_projection_excludes_private_field_names(self):
        public = self.parser.parse(_valid_card(), source_url=CARD_URL).public
        for private in ("security", "authentication", "credentials", "token", "authorization"):
            self.assertFalse(_find_key(public, private), f"private key {private!r} leaked into projection")

    def test_skill_rows_extracted(self):
        result = self.parser.parse(_valid_card(), source_url=CARD_URL)
        self.assertEqual(len(result.skills), 1)
        row = result.skills[0]
        self.assertEqual(row["skill_id"], "industrial-displays")
        self.assertEqual(row["name"], "Industrial Displays")
        self.assertEqual(row["tags_json"], '["displays","b2b"]')
        self.assertEqual(row["input_modes_json"], '["text"]')
        self.assertEqual(row["output_modes_json"], '["text"]')

    def test_capability_rows_extracted(self):
        result = self.parser.parse(_valid_card(), source_url=CARD_URL)
        ids = {(c["namespace"], c["capability_id"]) for c in result.capabilities}
        self.assertIn(("a2a", "agent_card"), ids)
        self.assertIn(("a2a", "streaming"), ids)
        self.assertNotIn(("a2a", "push_notifications"), ids)
        marker = next(c for c in result.capabilities if c["capability_id"] == "agent_card")
        self.assertEqual(marker["version"], "1.0.0")
        self.assertEqual(marker["source"], "agent_card")

    def test_subdomain_url_allowed(self):
        card = _valid_card()
        card["url"] = "https://agent.merchant.example/agent"
        result = self.parser.parse(card, source_url=CARD_URL)
        self.assertEqual(result.canonical_domain, "merchant.example")

    def test_convenience_function(self):
        result = parse_agent_card(_valid_card(), source_url=CARD_URL)
        self.assertEqual(result.name, "Example Merchant Agent")


class AgentCardSchemaTest(unittest.TestCase):
    def setUp(self) -> None:
        self.parser = AgentCardParser()

    def test_root_must_be_object(self):
        with self.assertRaises(ProfileValidationError):
            self.parser.parse(["not", "a", "card"], source_url=CARD_URL)

    def test_missing_required_field(self):
        card = _valid_card()
        del card["name"]
        with self.assertRaises(ProfileValidationError):
            self.parser.parse(card, source_url=CARD_URL)

    def test_wrong_type(self):
        card = _valid_card()
        card["version"] = 1
        with self.assertRaises(ProfileValidationError):
            self.parser.parse(card, source_url=CARD_URL)

    def test_skill_entry_must_be_object(self):
        card = _valid_card()
        card["skills"] = ["not-an-object"]
        with self.assertRaises(ProfileValidationError):
            self.parser.parse(card, source_url=CARD_URL)

    def test_duplicate_skill_id_rejected(self):
        card = _valid_card()
        card["skills"] = [
            {"id": "dup", "name": "a"},
            {"id": "dup", "name": "b"},
        ]
        with self.assertRaises(ProfileValidationError):
            self.parser.parse(card, source_url=CARD_URL)

    def test_malformed_capabilities_flag_type(self):
        card = _valid_card()
        card["capabilities"]["streaming"] = "yes"
        with self.assertRaises(ProfileValidationError):
            self.parser.parse(card, source_url=CARD_URL)

    def test_deeply_nested_profile_safe_fails(self):
        obj: dict[str, Any] = {}
        cur = obj
        for _ in range(150):
            cur["x"] = {}
            cur = cur["x"]
        with self.assertRaises(ProfileValidationError):
            self.parser.parse(obj, source_url=CARD_URL)

    def test_too_many_nodes_safe_fails(self):
        card = _valid_card()
        card["skills"] = [{"id": f"s{i}", "name": "n"} for i in range(50_001)]
        with self.assertRaises(ProfileValidationError):
            self.parser.parse(card, source_url=CARD_URL)

    def test_empty_name_rejected(self):
        card = _valid_card()
        card["name"] = "   "
        with self.assertRaises(ProfileValidationError):
            self.parser.parse(card, source_url=CARD_URL)


class AgentCardVersionTest(unittest.TestCase):
    def test_version_not_in_allowlist_rejected(self):
        card = _valid_card()
        card["version"] = "2.0.0"
        with self.assertRaises(ProfileValidationError):
            AgentCardParser().parse(card, source_url=CARD_URL)

    def test_custom_allowlist(self):
        card = _valid_card()
        card["version"] = "1.1.0"
        parser = AgentCardParser(TrustPolicy.from_config(allowed_a2a_versions=["1.0.0", "1.1.0"]))
        result = parser.parse(card, source_url=CARD_URL)
        self.assertEqual(result.version, "1.1.0")


class AgentCardAuthorityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.parser = AgentCardParser()

    def test_url_domain_mismatch_rejected(self):
        card = _valid_card()
        card["url"] = "https://attacker.example/agent"
        with self.assertRaises(ProfileValidationError):
            self.parser.parse(card, source_url=CARD_URL)

    def test_provider_url_domain_mismatch_rejected(self):
        card = _valid_card()
        card["provider"]["url"] = "https://attacker.example"
        with self.assertRaises(ProfileValidationError):
            self.parser.parse(card, source_url=CARD_URL)

    def test_documentation_url_domain_mismatch_rejected(self):
        card = _valid_card()
        card["documentationUrl"] = "https://attacker.example/docs"
        with self.assertRaises(ProfileValidationError):
            self.parser.parse(card, source_url=CARD_URL)

    def test_error_mentions_mismatch(self):
        card = _valid_card()
        card["url"] = "https://attacker.example/agent"
        with self.assertRaises(ProfileValidationError) as ctx:
            self.parser.parse(card, source_url=CARD_URL)
        self.assertIn("does not match fetch source domain", str(ctx.exception))

    def test_non_http_url_rejected(self):
        card = _valid_card()
        card["url"] = "ftp://merchant.example/agent"
        with self.assertRaises(ProfileValidationError):
            self.parser.parse(card, source_url=CARD_URL)


class AgentCardSecretQuarantineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.parser = AgentCardParser()

    def test_nested_skill_token_quarantined(self):
        card = _valid_card()
        card["skills"][0]["token"] = "static-bearer-token-1234"
        result = self.parser.parse(card, source_url=CARD_URL)
        self.assertIn("skills.0.token", result.secrets_quarantined)
        self.assertFalse(_find_key(result.public, "token"))

    def test_value_based_secret_in_description_quarantined(self):
        card = _valid_card()
        card["description"] = "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxIn0.abc"
        result = self.parser.parse(card, source_url=CARD_URL)
        self.assertIn("description", result.secrets_quarantined)
        self.assertNotIn("description", result.public)

    def test_provider_token_not_projected(self):
        card = _valid_card()
        card["provider"]["api_key"] = "AKIAIOSFODNN7EXAMPLE"
        result = self.parser.parse(card, source_url=CARD_URL)
        self.assertIn("provider.api_key", result.secrets_quarantined)
        self.assertNotIn("api_key", result.public.get("provider", {}))

    def test_reject_on_secret(self):
        card = _valid_card()
        card["skills"][0]["token"] = "secret"
        with self.assertRaises(ProfileValidationError):
            AgentCardParser(reject_on_secret=True).parse(card, source_url=CARD_URL)

    def test_benign_description_passes_through(self):
        card = _valid_card()
        card["description"] = "Bearer of the torch — trustworthy merchant."
        result = self.parser.parse(card, source_url=CARD_URL)
        self.assertEqual(result.secrets_quarantined, ())
        self.assertEqual(result.public["description"], card["description"])


# ── UCP Profile ────────────────────────────────────────────────────────────


class UcpValidTest(unittest.TestCase):
    def setUp(self) -> None:
        self.parser = UcpProfileParser()

    def test_valid_ucp_parses(self):
        result = self.parser.parse(_valid_ucp(), source_url=UCP_URL)
        self.assertEqual(result.profile_type, "ucp")
        self.assertEqual(result.specification_version, "2026-04-08")
        self.assertEqual(result.service_identity_id, "urn:example:agent:merchant")
        self.assertEqual(result.secrets_quarantined, ())

    def test_projection_keeps_public_fields(self):
        public = self.parser.parse(_valid_ucp(), source_url=UCP_URL).public
        self.assertEqual(public["specificationVersion"], "2026-04-08")
        self.assertEqual(public["implementationVersion"], "1.0.0")
        self.assertEqual(public["serviceIdentity"]["name"], "Example Merchant Agent")
        self.assertEqual(public["services"][0]["type"], "urn:example:type:shopping")
        self.assertEqual(public["services"][0]["endpoints"][0]["uri"], "https://merchant.example/a2a")
        self.assertEqual(public["services"][0]["endpoints"][0]["protocol"], "a2a")
        self.assertNotIn("access", public["services"][0]["endpoints"][0])

    def test_projection_excludes_private_field_names(self):
        public = self.parser.parse(_valid_ucp(), source_url=UCP_URL).public
        for private in ("access", "token", "api_key", "password", "secret"):
            self.assertFalse(_find_key(public, private), f"private key {private!r} leaked into projection")

    def test_capabilities_extracted(self):
        result = self.parser.parse(_valid_ucp(), source_url=UCP_URL)
        by_id = {(c["namespace"], c["capability_id"]): c for c in result.capabilities}
        # protocol marker
        self.assertIn(("ucp", "profile"), by_id)
        self.assertEqual(by_id[("ucp", "profile")]["version"], "2026-04-08")
        # URN preserved
        self.assertIn(("urn:example:capability", "shopping.negotiation"), by_id)
        # reverse-domain namespace
        self.assertIn(("com.example.shopping", "negotiation"), by_id)
        # compound name attributed to canonical domain
        self.assertIn(("merchant.example", "shopping.negotiation"), by_id)
        # bare capability attributed to canonical domain
        self.assertIn(("merchant.example", "shopping"), by_id)

    def test_skills_empty_for_ucp(self):
        result = self.parser.parse(_valid_ucp(), source_url=UCP_URL)
        self.assertEqual(result.skills, ())

    def test_convenience_function(self):
        result = parse_ucp_profile(_valid_ucp(), source_url=UCP_URL)
        self.assertEqual(result.specification_version, "2026-04-08")


class UcpSchemaTest(unittest.TestCase):
    def setUp(self) -> None:
        self.parser = UcpProfileParser()

    def test_root_must_be_object(self):
        with self.assertRaises(ProfileValidationError):
            self.parser.parse([], source_url=UCP_URL)

    def test_missing_specification_version(self):
        ucp = _valid_ucp()
        del ucp["specificationVersion"]
        with self.assertRaises(ProfileValidationError):
            self.parser.parse(ucp, source_url=UCP_URL)

    def test_version_not_in_allowlist_rejected(self):
        ucp = _valid_ucp()
        ucp["specificationVersion"] = "2025-06-15"
        with self.assertRaises(ProfileValidationError):
            self.parser.parse(ucp, source_url=UCP_URL)

    def test_custom_ucp_allowlist(self):
        ucp = _valid_ucp()
        ucp["specificationVersion"] = "2025-06-15"
        parser = UcpProfileParser(TrustPolicy.from_config(allowed_ucp_versions=["2026-04-08", "2025-06-15"]))
        result = parser.parse(ucp, source_url=UCP_URL)
        self.assertEqual(result.specification_version, "2025-06-15")

    def test_empty_services_rejected(self):
        ucp = _valid_ucp()
        ucp["services"] = []
        with self.assertRaises(ProfileValidationError):
            self.parser.parse(ucp, source_url=UCP_URL)

    def test_service_missing_capabilities_rejected(self):
        ucp = _valid_ucp()
        del ucp["services"][0]["capabilities"]
        with self.assertRaises(ProfileValidationError):
            self.parser.parse(ucp, source_url=UCP_URL)

    def test_endpoint_missing_protocol_rejected(self):
        ucp = _valid_ucp()
        del ucp["services"][0]["endpoints"][0]["protocol"]
        with self.assertRaises(ProfileValidationError):
            self.parser.parse(ucp, source_url=UCP_URL)

    def test_missing_service_identity_rejected(self):
        ucp = _valid_ucp()
        del ucp["serviceIdentity"]
        with self.assertRaises(ProfileValidationError):
            self.parser.parse(ucp, source_url=UCP_URL)

    def test_implementation_version_optional(self):
        ucp = _valid_ucp()
        del ucp["implementationVersion"]
        result = self.parser.parse(ucp, source_url=UCP_URL)
        self.assertNotIn("implementationVersion", result.public)

    def test_deeply_nested_profile_safe_fails(self):
        obj: dict[str, Any] = {}
        cur = obj
        for _ in range(150):
            cur["x"] = {}
            cur = cur["x"]
        with self.assertRaises(ProfileValidationError):
            self.parser.parse(obj, source_url=UCP_URL)


class UcpAuthorityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.parser = UcpProfileParser()

    def test_endpoint_domain_mismatch_rejected(self):
        ucp = _valid_ucp()
        ucp["services"][0]["endpoints"][0]["uri"] = "https://attacker.example/a2a"
        with self.assertRaises(ProfileValidationError):
            self.parser.parse(ucp, source_url=UCP_URL)

    def test_service_identity_url_domain_mismatch_rejected(self):
        ucp = _valid_ucp()
        ucp["serviceIdentity"]["id"] = "https://attacker.example/agent"
        with self.assertRaises(ProfileValidationError):
            self.parser.parse(ucp, source_url=UCP_URL)

    def test_service_identity_urn_not_domain_checked(self):
        # A URN id is not an http(s) URL, so domain enforcement is skipped.
        ucp = _valid_ucp()
        ucp["serviceIdentity"]["id"] = "urn:example:agent:other"
        result = self.parser.parse(ucp, source_url=UCP_URL)
        self.assertEqual(result.service_identity_id, "urn:example:agent:other")

    def test_openapi_document_domain_mismatch_rejected(self):
        ucp = _valid_ucp()
        ucp["specifications"][0]["openAPIDocument"] = "https://attacker.example/knp.yaml"
        with self.assertRaises(ProfileValidationError):
            self.parser.parse(ucp, source_url=UCP_URL)

    def test_subdomain_endpoint_allowed(self):
        ucp = _valid_ucp()
        ucp["services"][0]["endpoints"][0]["uri"] = "https://a2a.merchant.example/endpoint"
        result = self.parser.parse(ucp, source_url=UCP_URL)
        self.assertEqual(result.canonical_domain, "merchant.example")


class UcpSecretQuarantineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.parser = UcpProfileParser()

    def test_endpoint_access_token_quarantined(self):
        ucp = _valid_ucp()
        ucp["services"][0]["endpoints"][0]["access"] = {"token": "static-token-abc"}
        result = self.parser.parse(ucp, source_url=UCP_URL)
        self.assertIn("services.0.endpoints.0.access.token", result.secrets_quarantined)
        self.assertNotIn("access", result.public["services"][0]["endpoints"][0])

    def test_bearer_value_in_access_quarantined(self):
        ucp = _valid_ucp()
        ucp["services"][0]["endpoints"][0]["access"] = "Bearer ABCDEFGHIJKLMNOP"
        result = self.parser.parse(ucp, source_url=UCP_URL)
        self.assertIn("services.0.endpoints.0.access", result.secrets_quarantined)
        self.assertNotIn("access", result.public["services"][0]["endpoints"][0])

    def test_reject_on_secret(self):
        ucp = _valid_ucp()
        ucp["services"][0]["endpoints"][0]["access"] = {"password": "hunter2"}
        with self.assertRaises(ProfileValidationError):
            UcpProfileParser(reject_on_secret=True).parse(ucp, source_url=UCP_URL)


# ── Extraction helpers ─────────────────────────────────────────────────────


class ExtractionHelperTest(unittest.TestCase):
    def test_agent_card_skills_round_trip(self):
        card = _valid_card()
        public = AgentCardParser().parse(card, source_url=CARD_URL).public
        skills = extract_agent_card_skills(public)
        self.assertEqual(skills[0]["skill_id"], "industrial-displays")

    def test_agent_card_capabilities_default_namespace(self):
        caps = extract_agent_card_capabilities({"capabilities": {"streaming": True}}, version="1.0.0")
        ids = {(c["namespace"], c["capability_id"]) for c in caps}
        self.assertIn(("a2a", "agent_card"), ids)
        self.assertIn(("a2a", "streaming"), ids)

    def test_ucp_capabilities_compound_namespace(self):
        public = {
            "services": [{"capabilities": ["catalog.search"]}],
        }
        caps = extract_ucp_capabilities(public, default_namespace="merchant.example")
        rows = [c for c in caps if c["capability_id"] != "profile"]
        self.assertEqual(rows[0]["namespace"], "merchant.example")
        self.assertEqual(rows[0]["capability_id"], "catalog.search")

    def test_ucp_capabilities_required_flag(self):
        public = {"services": [{"capabilities": ["x.y"]}]}
        caps = extract_ucp_capabilities(public, default_namespace="d")
        for row in caps:
            self.assertIn("required", row)
            self.assertIn("source", row)
            self.assertIn("schema_url", row)
            self.assertIn("spec_url", row)


if __name__ == "__main__":
    unittest.main()
