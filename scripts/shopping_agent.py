#!/usr/bin/env python3
"""Resident merchant-agent entry point.

Thin shim over ``shopping_cli.agents.agent_cli`` so the agent entry never
depends on the large ``shopping_cli.cli`` module.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shopping_cli.agents.agent_cli import main  # noqa: E402

if __name__ == "__main__":
    main(sys.argv[1:])
