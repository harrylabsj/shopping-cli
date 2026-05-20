"""Console entry point for serving the shopping-cli Marketplace API."""

from __future__ import annotations

import argparse
import sys

from shopping_cli.api.app import create_app
from shopping_cli.config import DEFAULT_DB_PATH


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Serve the shopping-cli marketplace API.")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="SQLite database path")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])
    try:
        import uvicorn
    except ModuleNotFoundError as exc:
        raise SystemExit("uvicorn is required to serve the FastAPI app. Install shopping-cli[api].") from exc
    uvicorn.run(create_app(args.db), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
