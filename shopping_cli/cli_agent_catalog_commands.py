"""Agent Catalog CLI command handlers — search and get Commerce Agent Catalog entries."""

from __future__ import annotations

import argparse
from typing import Any

from shopping_cli.cli_common import db_path_from_args, emit
from shopping_cli.core.errors import NotFoundError
from shopping_cli.db.session import db_session
from shopping_cli.services.agent_catalog import get_catalog_agent, search_catalog_agents


def _format_verification(agent: dict[str, Any]) -> str:
    """Human-readable verification status + last_verified_at (§28 DoD#15)."""
    ver = agent.get("verification", {}) or {}
    status = ver.get("status", "discovered")
    last_verified = ver.get("last_verified_at", "")
    if last_verified:
        return f"{status} (last verified: {last_verified})"
    return status


def _format_hosting(agent: dict[str, Any]) -> str:
    hosting = agent.get("hosting", {}) or {}
    return hosting.get("mode", "unknown")


def _format_capabilities(agent: dict[str, Any]) -> str:
    caps = agent.get("capabilities", []) or []
    if not caps:
        return "-"
    return ", ".join(str(c) for c in caps)


def _format_protocols(agent: dict[str, Any]) -> str:
    protos = agent.get("protocols", {}) or {}
    if not protos:
        return "-"
    parts = []
    for proto, versions in protos.items():
        if versions:
            parts.append(f"{proto} ({', '.join(versions)})")
        else:
            parts.append(proto)
    return "; ".join(parts)


def _format_merchant(agent: dict[str, Any]) -> str:
    merchant = agent.get("merchant", {}) or {}
    name = merchant.get("name", "")
    domain = merchant.get("domain", "")
    if name and domain:
        return f"{name} ({domain})"
    if name:
        return name
    if domain:
        return domain
    return "-"


def cmd_agent_catalog_search(args: argparse.Namespace) -> None:
    with db_session(db_path_from_args(args)) as conn:
        result = search_catalog_agents(
            conn,
            q=args.q or "",
            category=args.category or "",
            skill=args.skill or "",
            capability=args.capability or "",
            protocol=args.protocol or "",
            hosting_mode=args.hosting_mode or "",
            verification_status=args.verification_status or "",
            verified_after=args.verified_after or "",
            limit=args.limit,
            cursor=args.cursor or "",
        )

    results: list[dict[str, Any]] = result.get("results", []) or []
    next_cursor = result.get("next_cursor")

    if args.format == "text":
        if not results:
            print("No catalog agents found.")
            return
        print(
            f"{'CATALOG_AGENT_ID':<24} "
            f"{'MERCHANT':<30} "
            f"{'VERIFICATION':<40} "
            f"{'HOSTING':<12} "
            f"{'CAPABILITIES'}"
        )
        for agent in results:
            print(
                f"{agent.get('catalog_agent_id', ''):<24} "
                f"{_format_merchant(agent):<30} "
                f"{_format_verification(agent):<40} "
                f"{_format_hosting(agent):<12} "
                f"{_format_capabilities(agent)}"
            )
        if next_cursor:
            print(f"\nNext cursor: {next_cursor}")
        return

    response: dict[str, Any] = {"ok": True, "results": results}
    if next_cursor:
        response["next_cursor"] = next_cursor
    emit(response, args.format)


def cmd_agent_catalog_get(args: argparse.Namespace) -> None:
    catalog_agent_id = str(args.catalog_agent_id).strip()
    with db_session(db_path_from_args(args)) as conn:
        try:
            agent = get_catalog_agent(conn, catalog_agent_id)
        except NotFoundError:
            raise SystemExit(f"Unknown catalog agent: {catalog_agent_id}")

    if args.format == "text":
        print(f"Catalog Agent: {agent.get('catalog_agent_id', '')}")
        merchant = agent.get("merchant", {}) or {}
        print(f"Merchant: {merchant.get('name', '-')} ({merchant.get('id', '-')})")
        if merchant.get("domain"):
            print(f"Domain: {merchant['domain']}")
        if merchant.get("city"):
            print(f"City: {merchant['city']}")
        if merchant.get("service_area"):
            print(f"Service Area: {merchant['service_area']}")

        verification = agent.get("verification", {}) or {}
        print(f"Verification Status: {verification.get('status', 'discovered')}")
        last_verified = verification.get("last_verified_at", "")
        if last_verified:
            print(f"Last Verified: {last_verified}")

        hosting = agent.get("hosting", {}) or {}
        print(f"Hosting Mode: {hosting.get('mode', 'unknown')}")

        caps = agent.get("capabilities", []) or []
        print(f"Capabilities: {', '.join(str(c) for c in caps) if caps else '-'}")

        protos = agent.get("protocols", {}) or {}
        if protos:
            print("Protocols:")
            for proto, versions in protos.items():
                ver_str = f" ({', '.join(versions)})" if versions else ""
                print(f"  {proto}{ver_str}")

        discovery = agent.get("discovery", {}) or {}
        if discovery:
            print("Discovery:")
            for key, value in discovery.items():
                if isinstance(value, list):
                    for url in value:
                        print(f"  {key}: {url}")
                else:
                    print(f"  {key}: {value}")

        tags = merchant.get("tags", []) or []
        if tags:
            print(f"Tags: {', '.join(str(t) for t in tags)}")
        return

    emit({"ok": True, "agent": agent}, args.format)
