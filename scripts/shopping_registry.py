#!/usr/bin/env python3
"""Compatibility shim for the removed JSON registry.

The MVP marketplace service now lives behind `shopping-cli api serve` and stores
trusted state in SQLite.
"""

from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Legacy registry shim. Use `shopping-cli api serve --db ./shopping-cli.sqlite` for the SQLite marketplace API.",
    )
    parser.add_argument("--version", action="version", version="shopping-cli registry shim")
    parser.parse_args(argv)
    parser.print_help()


if __name__ == "__main__":
    main(sys.argv[1:])
