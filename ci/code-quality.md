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

Persist the rendered result as a PR conversation comment before marking the PR ready for Review. A successor head requires a new result. The first PR that introduces this policy reports `BOOTSTRAP`: there is no predecessor policy to enforce against that same candidate.

After bootstrap, the effective blocking policy and generated-file registry are read from the exact comparison-base SHA, never from candidate head. Ruff checks changed Python paths; Pyright checks the configured Python project; jscpd compares whole-source clone occurrences and blocks only positive occurrence growth intersecting changed paths. Runtime timing is reported separately from the canonical result digest.

The Python size ratchet blocks a new file over 500 nonblank UTF-8 lines, a crossing from at-or-below 500 to above 500, or further growth of an already-over-500 file. Shrinking/touching legacy oversized files does not force whole-file cleanup. New/expanded tracked non-source files signal above 100,000 bytes and block above 200,000 bytes. Likely generated tracked files require an entry in `ci/code-quality-generated.json` under the comparison-base policy.

Quality-only automatic correction stops after two rounds; a remaining failure is `WAIVER_REQUIRED` rather than another automatic loop. Analyzer runtime over the 10-second local target is evidence for later demotion, not a reason to silently skip the analyzer.

## Emergency disable

The ordinary policy is `ci/code-quality.toml`:

```toml
enabled = true
```

Marco has separately authorized exactly one emergency rollback path: change only that line to `enabled = false` directly on current `main`, with no PR/Review/Integration and no tests. No unrelated change may ride with that emergency commit. Re-enabling or changing any other behavior returns to the normal repository lifecycle.
