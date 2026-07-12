"""Shared test helpers."""

from __future__ import annotations

import sys
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = str(ROOT / "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import shopping  # noqa: E402


def run_cli(db_file: Path, *args: str, db_flag: str = "--db") -> str:
    output = StringIO()
    with redirect_stdout(output):
        shopping.main([db_flag, str(db_file), *args])
    return output.getvalue()
