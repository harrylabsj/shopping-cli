"""Adapter argparse command handlers."""

from __future__ import annotations

import argparse
from typing import Any

from shopping_cli.adapters import hermes, openclaw
from shopping_cli.cli_common import db_path_from_args, emit, yes_no
from shopping_cli.core.errors import ValidationError


def adapter_for_host(host: str) -> Any:
    if host == "openclaw":
        return openclaw
    if host == "hermes":
        return hermes
    raise ValidationError(f"Unknown adapter host: {host}")


def cmd_adapter_inspect(args: argparse.Namespace) -> None:
    adapter = adapter_for_host(args.host)
    result = adapter.inspect_host(
        db_path=db_path_from_args(args),
        project_root=args.project_root or None,
        skill_root=args.skill_root or None,
    )
    if args.format == "text":
        emit_adapter_inspect_text(result)
        return
    emit(result, args.format)


def emit_adapter_inspect_text(result: dict[str, Any]) -> None:
    print(f"Adapter: {result.get('host') or '-'}")
    print(f"OK: {yes_no(result.get('ok'))}")
    print(f"Command: {result.get('command') or '-'}")
    print(f"Command available: {yes_no(result.get('command_available'))}")
    print(f"Command path: {result.get('command_path') or '-'}")
    print(f"Project root: {result.get('project_root') or '-'}")
    print(f"Project root valid: {yes_no(result.get('project_root_valid'))}")
    print(f"Skill root: {result.get('skill_root') or '-'}")
    print(f"Skill installed: {yes_no(result.get('skill_installed'))}")
    print(f"Skill symlink: {yes_no(result.get('skill_is_symlink'))}")
    print(f"Skill target: {result.get('skill_target') or '-'}")
    print(f"Skill points to project: {yes_no(result.get('skill_points_to_project'))}")
    print(f"Admin token configured: {yes_no(result.get('admin_token_configured'))}")
    print(f"Buyer bootstrap token configured: {yes_no(result.get('buyer_bootstrap_token_configured'))}")
    if result.get("db_path"):
        print(f"DB: {result['db_path']}")


def cmd_adapter_doctor(args: argparse.Namespace) -> None:
    adapter = adapter_for_host(args.host)
    result = adapter.doctor(
        db_path=db_path_from_args(args),
        project_root=args.project_root or None,
        skill_root=args.skill_root or None,
    )
    if args.format == "text":
        emit_adapter_doctor_text(result)
        return
    emit(result, args.format)


def emit_adapter_doctor_text(result: dict[str, Any]) -> None:
    print(f"Adapter doctor: {result.get('host') or '-'}")
    print(f"OK: {yes_no(result.get('ok'))}")
    issues = result.get("issues") or []
    warnings = result.get("warnings") or []
    if not issues and not warnings:
        print("Issues: none")
        return
    if issues:
        print("Issues:")
        for issue in issues:
            print(f"- {issue}")
    if warnings:
        print("Warnings:")
        for warning in warnings:
            print(f"- {warning}")


def cmd_adapter_install_command(args: argparse.Namespace) -> None:
    adapter = adapter_for_host(args.host)
    command = adapter.install_command(project_root=args.project_root or None, dry_run=args.dry_run, force=args.force)
    emit(
        {
            "ok": True,
            "host": args.host,
            "command": command,
            "message": " ".join(command),
        },
        args.format,
    )
