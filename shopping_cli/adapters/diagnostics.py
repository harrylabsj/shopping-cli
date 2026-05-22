"""Shared host-adapter setup and inspection helpers."""

from __future__ import annotations

import shutil
import os
from pathlib import Path
from typing import Any


def _safe_path_exists(path: Path) -> bool:
    try:
        return path.exists()
    except OSError:
        return False


def _safe_is_symlink(path: Path) -> bool:
    try:
        return path.is_symlink()
    except OSError:
        return False


def _is_self_symlink(path: Path) -> bool:
    try:
        target = path.readlink()
    except OSError:
        return False
    absolute_target = target if target.is_absolute() else path.parent / target
    return absolute_target.absolute() == path.absolute()


def _safe_resolve(path: Path) -> str:
    if _safe_is_symlink(path) and _is_self_symlink(path):
        return ""
    try:
        return str(path.resolve())
    except (OSError, RuntimeError):
        return ""


def inspect_host(
    host: str,
    command_name: str,
    default_skill_root: Path,
    project_root: str | Path | None = None,
    skill_root: str | Path | None = None,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    root = Path(project_root).expanduser() if project_root is not None else Path(__file__).resolve().parents[2]
    skill = Path(skill_root).expanduser() if skill_root is not None else default_skill_root.expanduser()
    command_path = shutil.which(command_name)
    project_root_valid = _safe_path_exists(root / "scripts" / "shopping.py")
    skill_installed = _safe_path_exists(skill)
    skill_is_symlink = _safe_is_symlink(skill)
    skill_target = _safe_resolve(skill) if skill_installed or skill_is_symlink else ""
    project_target = _safe_resolve(root)
    skill_points_to_project = bool(skill_target and project_target and Path(skill_target) == Path(project_target))
    admin_token_configured = bool(str(os.environ.get("SHOPPING_ADMIN_TOKEN") or "").strip())
    buyer_bootstrap_token_configured = bool(
        str(os.environ.get("SHOPPING_BUYER_BOOTSTRAP_TOKEN") or "").strip()
    )
    return {
        "ok": bool(command_path and project_root_valid and skill_installed and (not skill_is_symlink or skill_points_to_project)),
        "host": host,
        "command": command_name,
        "command_path": command_path or "",
        "command_available": command_path is not None,
        "project_root": str(root),
        "project_root_valid": project_root_valid,
        "skill_root": str(skill),
        "skill_installed": skill_installed,
        "skill_is_symlink": skill_is_symlink,
        "skill_target": skill_target,
        "skill_points_to_project": skill_points_to_project,
        "db_path": str(Path(db_path).expanduser()) if db_path is not None else "",
        "admin_token_configured": admin_token_configured,
        "buyer_bootstrap_token_configured": buyer_bootstrap_token_configured,
    }


def doctor_from_inspection(info: dict[str, Any]) -> dict[str, Any]:
    issues: list[str] = []
    warnings: list[str] = []
    if not info["command_available"]:
        issues.append(f"{info['command']} command not found")
    if not info["project_root_valid"]:
        issues.append("shopping-cli project root is invalid")
    if not info["skill_installed"]:
        issues.append(f"{info['host']} skill is not installed")
    elif info["skill_is_symlink"] and not info["skill_points_to_project"]:
        issues.append(f"{info['host']} skill points to a different project root")
    if not info.get("admin_token_configured"):
        warnings.append("SHOPPING_ADMIN_TOKEN is not configured")
    if not info.get("buyer_bootstrap_token_configured"):
        warnings.append("SHOPPING_BUYER_BOOTSTRAP_TOKEN is not configured")
    return {"ok": not issues, "host": info["host"], "issues": issues, "warnings": warnings, "inspection": info}


def install_command(
    target_flag: str,
    project_root: str | Path | None = None,
    dry_run: bool = False,
    force: bool = False,
) -> list[str]:
    root = Path(project_root).expanduser() if project_root is not None else Path(__file__).resolve().parents[2]
    command = ["bash", str(root / "scripts" / "install.sh"), target_flag]
    if dry_run:
        command.append("--dry-run")
    if force:
        command.append("--force")
    return command
