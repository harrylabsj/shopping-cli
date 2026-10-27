"""Verify the trusted pinned manifest and all four vendored runtime contracts."""
from __future__ import annotations

import argparse
import hashlib
import json
import stat
from pathlib import Path

PINNED_COMMIT = "82c1cfcb5f323e000e1ae3f6fbe1e1e9ab9f272a"
PINNED_MANIFEST_SHA256 = "0efaabbaa47ebfb241eb8ef8ea060b2c3318747fdf5dadda296d7f93cb584a92"
PINNED_BUNDLE_SHA256 = "5be0f0e86fa0f66c2ca150770c3ffb20f41c3417c13a2e5e05565a55e92be9cc"
PINNED_SCHEMAS = {
    "shopping.negotiation/0.1/capabilities.schema.json": "386a6eda04d20e31519e86aa2c7b30403af5ec31dbd9ac0d15a440347d7e1356",
    "shopping.negotiation/0.1/decision.schema.json": "b98255cf779f76a15c5768dc75fd6d9cd5616c4cbaa0593053b113e7bd2c4ef1",
    "shopping.negotiation/0.1/policy-result.schema.json": "3b2dd7980c091c50f8a58dd18f15a0a56a483ba722c075153ee1c3be07c660e8",
    "shopping.negotiation/0.1/snapshot.schema.json": "3684e5674917b1d3777e298d9e18e34f3f124d1ba7534540de08023be45a4e27",
}


def bundle_digest(manifest: dict) -> str:
    rows = sorted((item["path"], item["sha256"]) for item in manifest["contracts"])
    payload = "".join(f"{path}\0{digest}\n" for path, digest in rows).encode()
    return hashlib.sha256(payload).hexdigest()


def regular_bytes(path: Path, *, contract_root: Path | None = None) -> bytes:
    if contract_root is not None:
        for parent in (path.parent, *path.parent.parents):
            if parent.is_symlink():
                raise SystemExit("contract directory must not be a symlink")
            if parent == contract_root:
                break
    try:
        if not stat.S_ISREG(path.lstat().st_mode):
            raise SystemExit("contract input must be a regular non-symlink file")
        return path.read_bytes()
    except OSError as exc:
        raise SystemExit(f"contract input unavailable: {exc.__class__.__name__}") from exc


def verify_manifest(path: Path, *, contract_root: Path | None = None) -> dict:
    raw = regular_bytes(path, contract_root=contract_root)
    if hashlib.sha256(raw).hexdigest() != PINNED_MANIFEST_SHA256:
        raise SystemExit("manifest does not match trusted pinned Git source")
    manifest = json.loads(raw)
    if bundle_digest(manifest) != PINNED_BUNDLE_SHA256:
        raise SystemExit("manifest does not match pinned bundle")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path)
    args = parser.parse_args()
    contracts = Path(__file__).resolve().parents[1] / "shopping_cli" / "contracts"
    lock = json.loads(regular_bytes(contracts / "kiwi-contracts.lock.json", contract_root=contracts))
    expected = {
        "lock_version": 1,
        "source_repository": "harrylabsj/kiwi",
        "source_commit": PINNED_COMMIT,
        "bundle_sha256": PINNED_BUNDLE_SHA256,
        "contracts_manifest": "contracts/manifest.json",
    }
    if lock != expected:
        raise SystemExit("kiwi contract lock does not match trusted source metadata")
    manifest = verify_manifest(contracts / "kiwi-contracts.manifest.json", contract_root=contracts)
    declared = {item["path"].removeprefix("contracts/"): item["sha256"] for item in manifest["contracts"]}
    for path, digest in PINNED_SCHEMAS.items():
        if declared.get(path) != digest or hashlib.sha256(regular_bytes(contracts / path, contract_root=contracts)).hexdigest() != digest:
            raise SystemExit("vendored schema does not match trusted pinned bytes")
    if args.manifest:
        verify_manifest(args.manifest)
    print(f"kiwi contract lock verified: {PINNED_BUNDLE_SHA256}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
