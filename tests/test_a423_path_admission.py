from __future__ import annotations
import asyncio
import tempfile
import unittest
from pathlib import Path
import httpx
from shopping_cli.api.app import create_app


class DecodedPathAdmissionTest(unittest.TestCase):
    def test_actual_fastapi_rejects_encoded_crlf_before_endpoint_and_keeps_legal_path(self):
        async def run():
            with tempfile.TemporaryDirectory() as root:
                app = create_app(Path(root) / "synthetic.sqlite")
                effects = []

                @app.get("/__effect")
                def endpoint():
                    effects.append("executed")
                    return {"ok": True}

                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://synthetic.test"
                ) as client:
                    for path in ("/__effect%0A", "/__effect%0D", "/__effect%0D%0A"):
                        response = await client.get(path)
                        self.assertEqual(response.status_code, 404, response.text)
                        self.assertEqual(response.json(), {"ok": False, "error": "not found"})
                    self.assertEqual(effects, [])
                    self.assertEqual((await client.get("/__effect")).status_code, 200)
                    self.assertEqual(effects, ["executed"])

        asyncio.run(run())
