"""Hermes host adapter helpers."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable

from shopping_cli.adapters.diagnostics import doctor_from_inspection, inspect_host as inspect_adapter_host
from shopping_cli.adapters.diagnostics import install_command as adapter_install_command

DEFAULT_SKILL_ROOT = Path.home() / ".hermes" / "skills" / "commerce" / "shopping"


def resolve_project_root(project_root: str | Path | None = None) -> Path:
    explicit = project_root or os.environ.get("SHOPPING_ROOT")
    if explicit:
        return Path(explicit).expanduser()

    repo_root = Path(__file__).resolve().parents[2]
    if (repo_root / "scripts" / "shopping.py").exists():
        return repo_root
    return DEFAULT_SKILL_ROOT


def build_shopping_command(
    subcommand_args: Iterable[object] = (),
    db_path: str | Path | None = None,
    project_root: str | Path | None = None,
) -> list[str]:
    command = ["python3", str(resolve_project_root(project_root) / "scripts" / "shopping.py")]
    if db_path is not None:
        command.extend(["--db", str(Path(db_path).expanduser())])
    command.extend(str(arg) for arg in subcommand_args)
    return command


def buyer_ask_command(
    buyer_id: str,
    text: str,
    db_path: str | Path | None = None,
    project_root: str | Path | None = None,
    city: str = "",
    area: str = "",
) -> list[str]:
    args: list[object] = ["buyer", "ask", "--buyer", buyer_id, "--text", text, "--format", "json"]
    if city:
        args.extend(["--city", city])
    if area:
        args.extend(["--area", area])
    return build_shopping_command(args, db_path=db_path, project_root=project_root)


def buyer_ask_request(
    buyer_id: str,
    text: str,
    city: str = "",
    area: str = "",
    session_id: str = "",
    buyer_bootstrap_token: str = "",
) -> dict:
    token = buyer_bootstrap_token or os.environ.get("SHOPPING_BUYER_BOOTSTRAP_TOKEN", "")
    payload = {
        "buyer_id": buyer_id,
        "text": text,
        "city": city,
        "area": area,
        "source_id": f"hermes-buyer:{buyer_id}",
        "host": "hermes",
        "session_id": session_id,
    }
    if token:
        payload["buyer_bootstrap_token"] = token
    return {
        "method": "POST",
        "path": "/buyer/ask",
        "payload": payload,
    }


def record_intent_command(
    conversation_id: str,
    intent: str,
    text: str,
    db_path: str | Path | None = None,
    project_root: str | Path | None = None,
) -> list[str]:
    return build_shopping_command(
        [
            "buyer",
            "intent",
            "--conversation",
            conversation_id,
            "--intent",
            intent,
            "--text",
            text,
            "--format",
            "json",
        ],
        db_path=db_path,
        project_root=project_root,
    )


def buyer_summarize_command(
    conversation_id: str,
    db_path: str | Path | None = None,
    project_root: str | Path | None = None,
) -> list[str]:
    return build_shopping_command(
        ["buyer", "summarize", "--conversation", conversation_id, "--format", "json"],
        db_path=db_path,
        project_root=project_root,
    )


def inspect_host(
    db_path: str | Path | None = None,
    project_root: str | Path | None = None,
    skill_root: str | Path | None = None,
) -> dict:
    return inspect_adapter_host(
        "Hermes",
        "hermes",
        DEFAULT_SKILL_ROOT,
        project_root=project_root,
        skill_root=skill_root,
        db_path=db_path,
    )


def doctor(
    db_path: str | Path | None = None,
    project_root: str | Path | None = None,
    skill_root: str | Path | None = None,
) -> dict:
    return doctor_from_inspection(inspect_host(db_path=db_path, project_root=project_root, skill_root=skill_root))


def install_command(project_root: str | Path | None = None, dry_run: bool = False, force: bool = False) -> list[str]:
    return adapter_install_command("--hermes", project_root=project_root, dry_run=dry_run, force=force)


__all__ = [
    "DEFAULT_SKILL_ROOT",
    "build_shopping_command",
    "buyer_ask_command",
    "buyer_ask_request",
    "buyer_summarize_command",
    "doctor",
    "inspect_host",
    "install_command",
    "record_intent_command",
    "resolve_project_root",
]
