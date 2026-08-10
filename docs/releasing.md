# Releasing shopping-cli

shopping-cli is a **PyPI consumer for a portfolio release**: this repository
builds and verifies the Python artifacts (sdist + wheel), while the actual
publication to PyPI is owned by the upstream **Kiwi protected release
workflow** (`harrylabsj/kiwi`).

## Ownership boundary

- **This repository** produces and verifies release artifacts. CI builds and
  smoke-tests them in the `release-artifacts` job, and
  `bash scripts/verify_release.sh` builds both Python artifacts and installs the
  wheel into an isolated virtual environment before checking all three console
  entry points.
- **Kiwi's protected release workflow** owns the PyPI Trusted Publisher / OIDC
  prerequisites: the PyPI project's trusted publisher configuration, the OIDC
  subject mapping, and the publish-job permissions that mint short-lived,
  environment-scoped tokens. It also coordinates the protected tag/build rules
  that gate what `main` may release.
- Do **not** add a second publishing workflow, a PyPI API token, or a password
  to this repository. Publication credentials must never be stored or committed
  here.

## Dry-run default

Release verification **never publishes by default**.

- `bash scripts/verify_release.sh` builds into a temporary directory and
  installs the wheel into an isolated virtual environment; it performs no
  upload.
- `npm pack --dry-run` is used only to validate the JavaScript packaging; it
  does not publish to an npm registry.
- Any publish/dry-run command must be explicit and must not be wired into CI or
  into local verification as a default step.
- Kiwi's `portfolio-release.yml` is dispatched manually and defaults to a dry
  run (`publish=false`): it builds, verifies, signs, and uploads the release
  bundle without touching any registry. A real publish requires an explicit
  `publish=true` dispatch.

## Versioning, tags, and rollback

1. **Version bump** — update the version in `pyproject.toml`, `package.json`,
   `clawhub.json`, and any lock files that record the version. Keep the three
   files in sync and follow semantic-versioning intent.
2. **Local gates** — run `uv sync --locked --extra api --extra dev`, then
   `bash scripts/quality.sh`, `npm run test`, and
   `bash scripts/verify_release.sh`.
3. **Pull request** — open a PR with the release-prep changes. CI runs the same
   quality and artifact-verification gates on the branch.
4. **Protected release dispatch** — the release is **not** triggered
   automatically by a tag. Kiwi's `portfolio-release.yml` runs only on
   `workflow_dispatch`. For a real (non-dry-run) publish, the workflow is
   manually dispatched with `publish=true` and `ref` set to a **full 40-char
   lowercase commit SHA**; the publish step is gated by the protected
   `kiwi-release` environment (required review). The lock's consumer SHAs pin
   the exact commit of this repository that is released.
5. **Publish** — Kiwi's protected release workflow builds exactly once,
   verifies, signs, and publishes to PyPI through Trusted Publisher / OIDC.
   This repository does not hold or inject a publish token.
6. **Tag after release (immutable marker)** — the `v<version>` tag (for example
   `v3.0.1`) is created only after the release completes, as an immutable
   version marker for the released commit. It drives no automation and must
   never be rewritten or force-pushed.
7. **Rollback** — PyPI does not support deleting a release as a rollback.
   If a release is defective, publish a corrected patch version and, when
   appropriate, yank the defective release on PyPI. Never rewrite published
   tags or force-push tags after publication. Kiwi's workflow can also verify a
   previous release manifest as a read-only rollback candidate
   (`verify_rollback` / `previous_manifest` inputs).

## Security checks

- **No long-term tokens**: this repository stores no PyPI credentials, no
  GitHub token for release, and no long-lived service tokens. Local demo
  credentials are referenced through environment variables (for example
  `SHOPPING_ADMIN_TOKEN`) and are never committed.
- **Action pinning**: third-party GitHub Actions are pinned to full 40-character
  commit SHAs (see `.github/workflows/ci.yml`). Do not downgrade a pin to a
  mutable branch or tag reference.
- **Trusted Publisher / OIDC**: publication uses short-lived OIDC identity
  brokered by Kiwi's protected workflow, so no PyPI API token is persisted in
  this repository or in the workflow environment.
- **Dependency floor**: the API stack is held above published Starlette
  advisories (`pyproject.toml` `[tool.uv]` constraint-dependencies). Keep the
  floor when bumping for a release.

## Release checklist

- [ ] Version bumped in `pyproject.toml`, `package.json`, `clawhub.json`, and
      related lock files.
- [ ] `uv sync --locked --extra api --extra dev`
- [ ] `bash scripts/quality.sh`
- [ ] `npm run test`
- [ ] `bash scripts/verify_release.sh` (dry-run only; nothing is uploaded)
- [ ] Third-party workflow actions remain pinned to full 40-character SHAs.
- [ ] No credentials, personal data, private host details, or local filesystem
      paths are introduced by the release changes.
- [ ] Publication is left to Kiwi's protected release workflow; no publish
      command or token is added to this repository.
