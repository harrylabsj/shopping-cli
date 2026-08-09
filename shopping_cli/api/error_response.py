"""Transport-only error response construction for the dual API stack."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, Callable


def build_error_response(
    status_code: int,
    error: str,
    response_factory: Callable[..., Any] | None = None,
) -> Any:
    """Build the existing JSON error envelope for FastAPI or fallback mode."""
    payload = {"ok": False, "error": error}
    if response_factory is not None:
        return response_factory(status_code=status_code, content=payload)
    return SimpleNamespace(
        status_code=status_code,
        body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
    )
