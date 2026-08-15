# Repository bundle policy

Repository bundles are a ChatGPT-only bootstrap/cache for substantial consequential repository/system reasoning. GitHub remains the source/history authority; a bundle is accepted only after the intended current `main` SHA has been resolved from GitHub and every bundle identity check succeeds.

## v1 scope and identity

v1 publishes `main` only. The artifact and Release namespace is `repository-bundle-<SHA>`, separate from dependency bundles. Each publication contains:

- `repository-bundle-<SHA>.bundle` — full Git history reachable from `refs/heads/main`;
- `repository-bundle-<SHA>.manifest.json` — repository full name/numeric identity, exact source SHA/ref, generator workflow identity, bundle checksum, and advertised refs;
- `repository-bundle-<SHA>.bundle.sha256` — `sha256sum`-format checksum.

PR-head bundles are intentionally unsupported in v1. If they are added later, verification of advertised `main` must become conditional on the requested ref instead of assuming `main == requested SHA`.

## Publication cadence and retention

`.github/workflows/repository-bundle.yml` runs on every `main` push and on manual dispatch. It resolves the event SHA, fetches the live `refs/heads/main`, and refuses publication unless `git ls-remote origin refs/heads/main` still equals that exact SHA. Superseded runs therefore fail closed rather than publishing a stale object that merely exists locally.

A successful main/manual run publishes an immutable GitHub Release named `repository-bundle-<SHA>` and uploads the same files as a ChatGPT-accessible GitHub Actions artifact mirror. Re-running an already-published SHA compares all three Release assets byte-for-byte and fails rather than replacing divergent content.

Retention is bounded:

- Actions artifact mirrors expire after **30 days**;
- repository-bundle Releases retain the **12 newest** publications; older repository-bundle Releases and their tags are deleted by the publication workflow;
- dependency-bundle Releases/artifacts use their existing independent namespace and retention policy.

Repository-bundle publication has no pull-request trigger. PR exact-head readiness is owned by the Review-triggered certification workflow; rebuilding the main-only ChatGPT bootstrap cache for each PR would spend hosted minutes without certifying the candidate.

## ChatGPT bootstrap order

Before substantial consequential repository/system reasoning, ChatGPT agents must:

1. resolve the intended current `refs/heads/main` SHA and repository identity through GitHub authority;
2. locate the exact `repository-bundle-<SHA>` Actions artifact; never substitute a nearby/newer/older bundle;
3. download the artifact through the GitHub connector and materialize/extract its ZIP in the runtime;
4. run `scripts/repository_bundle.py verify` with the authoritative repository full name/numeric ID, exact SHA, and `refs/heads/main`;
5. use the verified clone for `git log`, `git diff`, grep/search, tests, and local branch/commit preparation; for stacked/PR work, overlay the exact current branch/PR delta from GitHub authority because v1 bundles remain `main`-only.

`verify` checks manifest schema and filenames, repository identity, exact source SHA/ref, the external checksum, bundle SHA-256, `git bundle list-heads`, `git bundle verify`, advertised `main`, cloned `HEAD`, cloned `main`, and cloned `origin/main`. Only after those checks does it stamp the clone's `origin` URL back to the canonical GitHub repository URL.

If the exact artifact is missing, expired, stale, mismatched, corrupt, or cannot be materialized, stop and report that capability gap. Do not reconstruct a substantial change file-by-file from a different bundle or silently treat the cache as authority.

Tiny targeted lookups may use GitHub Connect directly. The verified bundle is read-only context: GitHub remains live source/history/PR/review authority and Asana remains orchestration authority; current-state conclusions still require those live reads. Unless authenticated Git transport is deliberately added later, publishing branch/commit/PR state remains connector-native.

Claude Code and Codex do not use this path: they continue from their authoritative local checkout/worktree and host-native Git tooling.
