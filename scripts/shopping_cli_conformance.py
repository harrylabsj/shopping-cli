"""Dev-only adapter for the portfolio Python service conformance kit."""

from __future__ import annotations

from pathlib import Path

from shopping_cli.api import app as app_module
from shopping_cli.api.fallback_asgi import MarketplaceASGIApp


def apps(root: Path) -> dict[str, object]:
    db_path = root / "shopping.sqlite"
    return {"fallback": MarketplaceASGIApp(db_path), "fastapi": app_module.create_app(db_path)}


def paths() -> dict[str, str]:
    return {"known_post": "/products"}
