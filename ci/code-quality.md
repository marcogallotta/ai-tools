# Code-quality ratchet

The fast code-quality gate compares one exact implementation head against its exact Git merge base. Local Implementation discovers quality regressions before Review; CI only verifies the same persisted `dish-code-quality-result-v1` identity.

## Local use

After the implementation is committed on its owned branch and the draft PR exists, run:

```sh
python scripts/code_quality_gate.py evaluate \
  --target-base <exact-authoring-base-sha> \
  --head "$(git rev-parse HEAD)" \
  --task-gid <asana-task-gid> \
  --pr-number <pr-number> \
  --output .test-artifacts/code-quality/result.json
python scripts/code_quality_gate.py render-comment \
  --result .test-artifacts/code-quality/result.json
```

Persist the rendered result as a PR conversation comment before marking the PR ready for Review. When the comparison-base policy is enabled, the shared pre-Review admission accepts only an exact-head `PASS` comment authored by a repository collaborator with `write`, `maintain`, or `admin` permission; missing, stale, malformed, non-PASS, or unauthorized comments keep both author finalization and ordinary Review discovery closed. Local author finalization and connector-native ChatGPT Review acquire evidence differently but call the same semantic admission core. CI verifies the persisted result afterward and never substitutes for it. A successor head requires a new result. The first PR that introduces this policy reports `BOOTSTRAP`: there is no predecessor policy to enforce against that same candidate.

After bootstrap, the effective blocking policy and generated-file registry are read from the exact comparison-base SHA, never from candidate head. Ruff checks changed Python paths; Pyright checks the configured Python project; jscpd compares whole-source clone occurrences and blocks only positive occurrence growth intersecting changed paths. Runtime timing is reported separately from the canonical result digest.

The Python size ratchet blocks a new file over 500 nonblank UTF-8 lines, a crossing from at-or-below 500 to above 500, or further growth of an already-over-500 file. Shrinking/touching legacy oversized files does not force whole-file cleanup. New/expanded tracked non-source files signal above 100,000 bytes and block above 200,000 bytes. Likely generated tracked files require an entry in `ci/code-quality-generated.json` under the comparison-base policy.

Quality-only automatic correction stops after two rounds; a remaining failure is `WAIVER_REQUIRED` rather than another automatic loop. Analyzer runtime over the 10-second local target is evidence for later demotion, not a reason to silently skip the analyzer.

## CI verification authority

A commit-status context is **not** authoritative code-quality evidence. Same-repository PR workflows can request write-capable `GITHUB_TOKEN` scopes, so a candidate-controlled workflow could mint a lookalike status. The `Dish / code quality` commit status may still be published as a diagnostic convenience, but it is never an admission signal and must not satisfy Review/Integration code-quality authority.

Authoritative CI verification is an exact-head attestation artifact produced only by a successful `.github/workflows/code-quality.yml` run whose event is `issue_comment` and whose workflow source is the repository default branch. The trusted run recomputes the persisted local result under the proven comparison-base policy, then uploads exactly one artifact named:

```text
dish-code-quality-attestation-v1-pr<PR>-<HEAD_SHA>-<RESULT_DIGEST>-run<RUN_ID>-attempt<ATTEMPT>
```

A candidate `pull_request` workflow run is diagnostic only, even if it publishes a similarly named status or artifact. Review/Integration consumers must validate the backing run and artifact with the **trusted default-branch/comparison-base copy** of `scripts/code_quality_attestation.py`; never execute the candidate's copy as the admission predicate. The verifier requires the exact workflow path, `issue_comment` event, default branch, successful completed run, repository identity, and one unexpired artifact bound to the exact PR/head/result digest.

Example after fetching the Actions run JSON and that run's artifacts JSON:

```sh
python scripts/code_quality_attestation.py verify \
  --run-json /tmp/code-quality-run.json \
  --artifacts-json /tmp/code-quality-artifacts.json \
  --repository marcogallotta/ai-tools \
  --default-branch main \
  --pr-number <pr-number> \
  --head <exact-head-sha> \
  --result-digest <persisted-result-digest>
```

The bootstrap PR that first introduces this workflow cannot produce a trusted default-branch `issue_comment` attestation before merge. Its bootstrap evidence is therefore the persisted local `BOOTSTRAP` result plus focused implementation evidence and independent Review; subsequent PRs use the trusted attestation path above.

## Emergency disable

The ordinary policy is `ci/code-quality.toml`:

```toml
enabled = true
```

Marco has separately authorized exactly one emergency rollback path: change only that line to `enabled = false` directly on current `main`, with no PR/Review/Integration and no tests. No unrelated change may ride with that emergency commit. Re-enabling or changing any other behavior returns to the normal repository lifecycle.
