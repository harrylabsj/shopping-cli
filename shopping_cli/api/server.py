"""Console entry point for serving the shopping-cli Marketplace API."""

from __future__ import annotations

import argparse
import sys

from shopping_cli.api.app import create_app
from shopping_cli.config import ConfigError, RuntimeConfig, validate_production_config


def main(argv: list[str] | None = None) -> None:
    runtime = RuntimeConfig.from_env()
    parser = argparse.ArgumentParser(description="Serve the shopping-cli marketplace API.")
    parser.add_argument("--db", default=str(runtime.db_path), help="SQLite database path")
    parser.add_argument("--host", default=runtime.api_host)
    parser.add_argument("--port", type=int, default=runtime.api_port)
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])
    try:
        validate_production_config()
    except ConfigError as exc:
        raise SystemExit(str(exc)) from exc
    try:
        import uvicorn
    except ModuleNotFoundError as exc:
        raise SystemExit("uvicorn is required to serve the FastAPI app. Install shopping-cli[api].") from exc
    uvicorn.run(create_app(args.db), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
