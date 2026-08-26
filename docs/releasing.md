# Releasing shopping-cli

shopping-cli is a **PyPI consumer for a portfolio release**: this repository
builds and verifies the Python artifacts (sdist + wheel), while the actual
publication to PyPI is owned by an **upstream composed release workflow**.

## Ownership boundary

- **This repository** produces and verifies release artifacts. CI builds and
  smoke-tests them in the `release-artifacts` job, and
  `bash scripts/verify_release.sh` builds both Python artifacts and installs the
  wheel into an isolated virtual environment before checking all three console
  entry points.
- **The upstream release workflow** owns the PyPI publication prerequisites:
  the publishing credentials, the publish-job permissions, and the protected
  tag/build rules that gate what `main` may release.
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
- The upstream release workflow is triggered manually and defaults to a dry
  run: it builds and verifies the release bundle without touching any
  registry. A real publish requires an explicit opt-in.

## Versioning, tags, and rollback

1. **Version bump** — update the version in `pyproject.toml`, `package.json`,
   `clawhub.json`, and any lock files that record the version. Keep the three
   files in sync and follow semantic-versioning intent.
2. **Local gates** — run `uv sync --locked --extra api --extra dev`, then
   `bash scripts/quality.sh`, `npm run test`, and
   `bash scripts/verify_release.sh`.
3. **Pull request** — open a PR with the release-prep changes. CI runs the same
   quality and artifact-verification gates on the branch.
4. **Protected release** — the release is **not** triggered automatically by a
   tag. The upstream release workflow runs only on manual dispatch, and the
   publish step is gated by a protected, review-required environment.
5. **Publish** — the upstream release workflow builds exactly once, verifies,
   and publishes to PyPI. This repository does not hold or inject a publish
   token.
6. **Tag after release (immutable marker)** — the `v<version>` tag (for example
   `v3.0.1`) is created only after the release completes, as an immutable
   version marker for the released commit. It drives no automation and must
   never be rewritten or force-pushed.
7. **Rollback** — PyPI does not support deleting a release as a rollback.
   If a release is defective, publish a corrected patch version and, when
   appropriate, yank the defective release on PyPI. Never rewrite published
   tags or force-push tags after publication. The upstream workflow can also
   verify a previous release as a read-only rollback candidate.

## Security checks

- **No long-term tokens**: this repository stores no PyPI credentials, no
  GitHub token for release, and no long-lived service tokens. Local demo
  credentials are referenced through environment variables (for example
  `SHOPPING_ADMIN_TOKEN`) and are never committed.
- **Action pinning**: third-party GitHub Actions are pinned to full 40-character
  commit SHAs (see `.github/workflows/ci.yml`). Do not downgrade a pin to a
  mutable branch or tag reference.
- **Publication credentials**: publication uses short-lived credentials managed
  by the upstream workflow, so no PyPI API token is persisted in this
  repository or in the workflow environment.
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
- [ ] Publication is left to the upstream composed release workflow; no publish
      command or token is added to this repository.
