#!/usr/bin/env python3
"""Marketplace API entry point."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shopping_cli.api.app import create_app  # noqa: E402
from shopping_cli.cli import DEFAULT_DB_PATH  # noqa: E402


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Serve the shopping-cli marketplace API.")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="SQLite database path")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(sys.argv[1:])
    try:
        import uvicorn
    except ModuleNotFoundError as exc:
        raise SystemExit("uvicorn is required to serve the FastAPI app. Install shopping-cli[api].") from exc
    uvicorn.run(create_app(args.db), host=args.host, port=args.port)
