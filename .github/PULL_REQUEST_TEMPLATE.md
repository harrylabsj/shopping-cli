## Summary

<!-- Describe the change and why it is needed. Keep it focused. -->

## Scope

<!-- What is in scope and what is intentionally out of scope. -->

## Related items

<!-- Link any related issue, contract lock change, design doc, or release note. -->

## Verification

- [ ] `uv sync --locked --extra api --extra dev`
- [ ] `bash scripts/quality.sh`
- [ ] `npm run test`
- [ ] Contract lock updated when wire behavior changed
- [ ] Release artifacts still verify when packaging behavior changed
  (`bash scripts/verify_release.sh`)

## Checklist

- [ ] No credentials, private host details, local filesystem paths, generated
      databases, review exports, or personal contact information are committed.
- [ ] No mutable GitHub Actions references; workflow actions are pinned to full
      40-character commit SHAs.
- [ ] No dependency versions, public APIs, or history were altered outside the
      stated scope of this PR.
