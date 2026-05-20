"""Console entry point for running a resident merchant agent."""

from __future__ import annotations

import argparse
import sys

from shopping_cli.agents import merchant_agent, merchant_daemon
from shopping_cli.cli import emit
from shopping_cli.config import DEFAULT_DB_PATH
from shopping_cli.db.session import db_session


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run a resident shopping-cli merchant agent.")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="SQLite database path")
    parser.add_argument("--merchant", required=True)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval", type=float, default=3.0)
    parser.add_argument("--format", choices=["text", "json"], default="text")
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])
    if not args.once:
        merchant_daemon.run_forever(args.db, args.merchant, interval=args.interval)
        return
    with db_session(args.db) as conn:
        result = merchant_agent.process_once(conn, args.merchant)
    emit(result, args.format)


if __name__ == "__main__":
    main()
