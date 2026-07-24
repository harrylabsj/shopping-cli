import tempfile
import unittest
import asyncio
import json
from pathlib import Path

from shopping_cli.api import app as app_module


@unittest.skipIf(app_module.FastAPI is None, "FastAPI optional dependency is not installed")
class FastAPIRuntimeContractTest(unittest.TestCase):
    def test_transport_error_schema_matches_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = app_module.create_app(Path(tmp) / "shopping.sqlite")

            @app.get("/__test_boom")
            def boom():
                raise RuntimeError("secret internal detail")

            async def request(method, path, body=b"", headers=None):
                sent = []
                delivered = False

                async def receive():
                    nonlocal delivered
                    if delivered:
                        return {"type": "http.disconnect"}
                    delivered = True
                    return {"type": "http.request", "body": body, "more_body": False}

                async def send(message):
                    sent.append(message)

                scope = {
                    "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                    "method": method, "scheme": "http", "path": path, "raw_path": path.encode(),
                    "query_string": b"", "root_path": "", "headers": headers or [],
                    "client": ("127.0.0.1", 1), "server": ("test", 80),
                }
                try:
                    await app(scope, receive, send)
                except RuntimeError:
                    # Starlette's server-error middleware re-raises after sending
                    # the configured 500 response when no test client suppresses it.
                    pass
                status = next(message["status"] for message in sent if message["type"] == "http.response.start")
                raw = b"".join(message.get("body", b"") for message in sent if message["type"] == "http.response.body")
                return status, json.loads(raw.decode("utf-8"))

            async def exercise():
                return (
                    await request("GET", "/__missing"),
                    await request("DELETE", "/merchants"),
                    await request("POST", "/products", b"{", [(b"content-type", b"application/json")]),
                    await request("GET", "/__test_boom"),
                )

            unknown, wrong_method, malformed, unexpected = asyncio.run(exercise())

        self.assertEqual(unknown[0], 404)
        self.assertEqual(unknown[1]["ok"], False)
        self.assertEqual(wrong_method[0], 405)
        self.assertEqual(wrong_method[1]["ok"], False)
        self.assertEqual(malformed[0], 400)
        self.assertEqual(malformed[1]["ok"], False)
        self.assertEqual(unexpected, (500, {"ok": False, "error": "internal server error"}))


if __name__ == "__main__":
    unittest.main()
