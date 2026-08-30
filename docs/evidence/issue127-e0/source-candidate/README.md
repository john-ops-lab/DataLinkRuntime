# Issue #127 E0 source candidate

`source-tree.json` is the reproducible E0 source receipt. It includes tracked
files and non-ignored working-tree files, including the pre-existing dirty
Candidate changes. Paths are sorted bytewise and each entry records the
SHA-256 of the exact file bytes. The working-tree hash is the SHA-256 of the
UTF-8 stream `path + NUL + file_sha256 + LF` for every entry.

The receipt excludes `docs/evidence/`, `.tmp-platform-logs/`, dependency and
build/cache output (`**/node_modules/`, `web/dist/`, `**/__pycache__/`). This
keeps evidence self-reference out of the source hash while retaining source
and documentation changes. Re-run from the repository root with:

```sh
node scripts/issue127-e0-source-receipt.mjs
```

Automatic evidence is marked `待人工验收`; this is not a user acceptance
claim.

`base_sha` is the unchanged starting commit, not a committed repair Candidate.
Until the user authorizes a commit, `working_tree_sha256` is the reproducible
identity of the dirty local Candidate; exact commit-SHA review remains open.
