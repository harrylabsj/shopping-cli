# Contributing

1. Keep changes focused and update the contract lock when wire behavior changes.
2. Run `uv sync --locked --extra api --extra dev`, then `bash scripts/quality.sh` and `npm run test` before opening a pull request.
3. Never commit credentials, private host details, local filesystem paths, generated databases, review exports, or personal contact information.
4. Do not add mutable GitHub Actions references; workflow actions must remain pinned to full commit SHAs.
