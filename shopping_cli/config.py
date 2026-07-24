"""Runtime configuration helpers for local shopping-cli hosts."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlparse

DEFAULT_DATA_DIR = Path.home() / ".local" / "share" / "shopping-cli"
DEFAULT_DB_PATH = DEFAULT_DATA_DIR / "shopping-cli.sqlite"
DEFAULT_STATE_DIR = Path.home() / ".local" / "state" / "shopping-cli"
DEFAULT_AGENT_STALE_TTL_SECONDS = 60
DEFAULT_API_HOST = "127.0.0.1"
DEFAULT_API_PORT = 8765
MIN_PRODUCTION_SECRET_BYTES = 32
MAX_AGENT_STALE_TTL_SECONDS = timedelta.max.days * 24 * 60 * 60 + timedelta.max.seconds
PLACEHOLDER_PREFIXES = ("replace-with-", "change-me", "changeme")
SUPPORTED_DEPLOYMENT_PROFILES = {"local", "production"}


class ConfigError(ValueError):
    pass


def _env_text(name: str) -> str:
    return str(os.environ.get(name) or "").strip()


def _is_placeholder_secret(value: str) -> bool:
    text = str(value or "").strip().lower()
    return not text or any(text.startswith(prefix) for prefix in PLACEHOLDER_PREFIXES)


def secret_is_usable(value: str, *, production: bool = False) -> bool:
    """Return whether a configured shared secret is safe to accept.

    Placeholder values are rejected in every profile. Production additionally
    requires at least 32 UTF-8 bytes so one-character or example credentials
    cannot accidentally protect a public deployment.
    """
    text = str(value or "").strip()
    if _is_placeholder_secret(text):
        return False
    return not production or len(text.encode("utf-8")) >= MIN_PRODUCTION_SECRET_BYTES


def _channel_tokens_configured() -> bool:
    global_token = _env_text("SHOPPING_CHANNEL_TOKEN")
    if not _is_placeholder_secret(global_token):
        return True
    raw = _env_text("SHOPPING_CHANNEL_TOKENS")
    if not raw:
        return False
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        decoded = None
    if isinstance(decoded, dict):
        return any(not _is_placeholder_secret(str(token or "")) for token in decoded.values())
    for part in raw.replace("\n", ",").split(","):
        text = part.strip()
        if not text:
            continue
        separator = ":" if ":" in text else "=" if "=" in text else ""
        if not separator:
            continue
        _channel, token = text.split(separator, 1)
        if not _is_placeholder_secret(token):
            return True
    return False


def deployment_profile_from(value: str | None = None) -> str:
    profile = str(value or os.environ.get("SHOPPING_DEPLOYMENT_PROFILE") or "local").strip().lower() or "local"
    if profile not in SUPPORTED_DEPLOYMENT_PROFILES:
        raise ConfigError("SHOPPING_DEPLOYMENT_PROFILE must be local or production")
    return profile


def api_host_from(value: str | None = None) -> str:
    return str(value or os.environ.get("SHOPPING_API_HOST") or DEFAULT_API_HOST).strip() or DEFAULT_API_HOST


def api_port_from(value: str | int | None = None) -> int:
    raw = value if value is not None else os.environ.get("SHOPPING_API_PORT")
    if raw in (None, ""):
        return DEFAULT_API_PORT
    if isinstance(raw, bool):
        raise ConfigError("SHOPPING_API_PORT must be an integer between 1 and 65535")
    try:
        port = int(raw)
    except (OverflowError, TypeError, ValueError) as exc:
        raise ConfigError("SHOPPING_API_PORT must be an integer between 1 and 65535") from exc
    if port <= 0 or port > 65535:
        raise ConfigError("SHOPPING_API_PORT must be an integer between 1 and 65535")
    return port


def public_base_url_from(value: str | None = None) -> str:
    return str(value or os.environ.get("SHOPPING_PUBLIC_BASE_URL") or "").strip()


def _sqlite_path_from_url(database_url: str) -> Path | None:
    parsed = urlparse(database_url)
    if parsed.scheme != "sqlite":
        return None
    if parsed.netloc and parsed.netloc != "localhost":
        raise ConfigError("SHOPPING_DATABASE_URL sqlite URLs must point to a local file")
    if parsed.path in ("", "/"):
        raise ConfigError("SHOPPING_DATABASE_URL sqlite URL must include a file path")
    return Path(parsed.path).expanduser()


def database_url_from(value: str | None = None) -> str:
    return str(value or os.environ.get("SHOPPING_DATABASE_URL") or "").strip()


def db_path_from(value: str | Path | None = None) -> Path:
    if value is not None:
        return Path(value).expanduser()
    database_url = database_url_from()
    if database_url:
        parsed = urlparse(database_url)
        if parsed.scheme in {"postgres", "postgresql"}:
            raise ConfigError("Postgres/RDS is not supported in this release; use SHOPPING_DB with SQLite.")
        sqlite_path = _sqlite_path_from_url(database_url)
        if sqlite_path is not None:
            return sqlite_path
        raise ConfigError("SHOPPING_DATABASE_URL must use sqlite:/// for this release")
    return Path(os.environ.get("SHOPPING_DB") or os.environ.get("SHOPPING_DATA") or DEFAULT_DB_PATH).expanduser()


def state_dir_from(value: str | Path | None = None) -> Path:
    return Path(value or os.environ.get("SHOPPING_CLI_STATE_DIR") or DEFAULT_STATE_DIR).expanduser()


def agent_stale_ttl_seconds_from(value: str | int | None = None) -> int:
    raw = value if value is not None else os.environ.get("SHOPPING_AGENT_STALE_TTL_SECONDS")
    if raw in (None, ""):
        return DEFAULT_AGENT_STALE_TTL_SECONDS
    if isinstance(raw, bool):
        return DEFAULT_AGENT_STALE_TTL_SECONDS
    try:
        seconds = int(raw)
    except (OverflowError, TypeError, ValueError):
        return DEFAULT_AGENT_STALE_TTL_SECONDS
    if seconds <= 0 or seconds > MAX_AGENT_STALE_TTL_SECONDS:
        return DEFAULT_AGENT_STALE_TTL_SECONDS
    return seconds


def production_config_checks() -> dict[str, bool]:
    admin_token = _env_text("SHOPPING_ADMIN_TOKEN")
    buyer_token = _env_text("SHOPPING_BUYER_BOOTSTRAP_TOKEN")
    return {
        "admin_token_configured": secret_is_usable(admin_token),
        "buyer_bootstrap_token_configured": secret_is_usable(buyer_token),
        "admin_token_strong": secret_is_usable(admin_token, production=True),
        "buyer_bootstrap_token_strong": secret_is_usable(buyer_token, production=True),
        "channel_tokens_configured": _channel_tokens_configured(),
    }


def validate_production_config() -> None:
    profile = deployment_profile_from()
    database_url = database_url_from()
    if database_url:
        parsed = urlparse(database_url)
        if parsed.scheme in {"postgres", "postgresql"}:
            raise ConfigError("Postgres/RDS is not supported in this release; use SHOPPING_DB with SQLite.")
        if parsed.scheme and parsed.scheme != "sqlite":
            raise ConfigError("SHOPPING_DATABASE_URL must use sqlite:/// for this release")
    if profile != "production":
        return
    checks = production_config_checks()
    # Channel ingress is optional and fails closed when no channel token is
    # configured. Admin and buyer bootstrap credentials are always required.
    required = ("admin_token_strong", "buyer_bootstrap_token_strong")
    missing = [name for name in required if not checks[name]]
    if missing:
        variables = {
            "admin_token_configured": "SHOPPING_ADMIN_TOKEN",
            "buyer_bootstrap_token_configured": "SHOPPING_BUYER_BOOTSTRAP_TOKEN",
            "admin_token_strong": "SHOPPING_ADMIN_TOKEN",
            "buyer_bootstrap_token_strong": "SHOPPING_BUYER_BOOTSTRAP_TOKEN",
            "channel_tokens_configured": "SHOPPING_CHANNEL_TOKEN or SHOPPING_CHANNEL_TOKENS",
        }
        names = ", ".join(variables[name] for name in missing)
        raise ConfigError(
            f"production deployment requires non-placeholder secrets of at least "
            f"{MIN_PRODUCTION_SECRET_BYTES} UTF-8 bytes for {names}"
        )


@dataclass(frozen=True)
class RuntimeConfig:
    db_path: Path = DEFAULT_DB_PATH
    state_dir: Path = DEFAULT_STATE_DIR
    agent_stale_ttl_seconds: int = DEFAULT_AGENT_STALE_TTL_SECONDS
    api_host: str = DEFAULT_API_HOST
    api_port: int = DEFAULT_API_PORT
    deployment_profile: str = "local"
    public_base_url: str = ""

    @classmethod
    def from_env(
        cls,
        db_path: str | Path | None = None,
        state_dir: str | Path | None = None,
        api_host: str | None = None,
        api_port: str | int | None = None,
    ) -> "RuntimeConfig":
        return cls(
            db_path=db_path_from(db_path),
            state_dir=state_dir_from(state_dir),
            agent_stale_ttl_seconds=agent_stale_ttl_seconds_from(),
            api_host=api_host_from(api_host),
            api_port=api_port_from(api_port),
            deployment_profile=deployment_profile_from(),
            public_base_url=public_base_url_from(),
        )
