import json

from shopping_cli.api.error_response import build_error_response


def test_build_error_response_fallback_preserves_wire_shape() -> None:
    response = build_error_response(400, "invalid request")
    assert response.status_code == 400
    assert json.loads(response.body) == {"ok": False, "error": "invalid request"}


def test_build_error_response_uses_injected_fastapi_factory() -> None:
    response = build_error_response(404, "missing", lambda **kwargs: kwargs)
    assert response == {"status_code": 404, "content": {"ok": False, "error": "missing"}}
