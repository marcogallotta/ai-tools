# Build request — deterministic batch-002 validator

Write a single deterministic Python script (stdlib only, no network/Asana access) that validates a
transformed migration batch against its inputs and the Honest schema. It will be run locally, offline,
against files already on disk — not by you. Output must be a plain pass/fail report: one line per
failure identifying the task and the exact rule violated, plus a final summary count. Exit non-zero on
any failure.

## Inputs (paths as CLI args, don't hardcode)

- `--manifest-in <cooking-raw-capture/capture-manifest.json>` — the 99-row source-of-truth capture
  (fields: `source_gid`, `expected_name`, `captured_section_name`, `notes_file`, `notes_sha256`, …).
- `--batch-dir <dish_migration_batch_002_correction_2/>` — contains `templates/*.md`,
  `manifest-batch-002.json`, `exceptions-batch-002.json`, `source-notes/*.txt`.
- `--schema <dish-task-schema.json>` — the Honest task schema.

## Checks (each is a separate rule; report every failure, don't stop at the first)

1. **Coverage** — every `source_gid` in the capture manifest has exactly one template in
   `manifest-batch-002.json`, and vice versa. No missing, no duplicate, no orphaned template file not
   referenced by the batch manifest.
2. **Source fidelity** — for every task, the `source-notes/<gid>.txt` shipped in the batch is
   byte-identical (SHA-256 match) to the corresponding file in the original capture. Flag any mismatch.
3. **Placeholder integrity** — every template contains exactly one `{{MIGRATED_DISH_STATE_BLOCK}}` and
   exactly one `{{TARGET_SECTION_GID}}`, both still literally present and unresolved. Flag a template
   that resolved either one, or is missing either one, or has more than one of either.
4. **Destination section** — the section name preceding `{{TARGET_SECTION_GID}}` in each template
   exactly matches that task's `captured_section_name` in the capture manifest. Flag any mismatch.
5. **Planning brief structure** — each template's `### Planning brief` block has all eight required
   lines in order (`Dish candidate`, `Purpose`, `Role`, `Priors`, `Locks`, `Exemptions`,
   `Research emphasis`, `Destination section`), each non-empty. `Role` must be exactly `main` or begin
   with `non-main`. Flag any missing/malformed line.
6. **No fabricated Verification/Research evidence** — flag any template containing the literal strings
   `Verification —` or `Research —` outside a clearly labeled "Legacy agent construction/provenance
   (not Dish evidence)" block, since durable Research/Verification evidence cannot legally exist yet
   for a migrated task (see Open Decision #5 in `corpus-migration-status.md` — this is a structural
   guard, not a judgment call).
7. **Schema legality** — validate each template's structured fields (Planning brief, Decisions,
   Research basis sections) against `dish-task-schema.json` wherever the schema defines a checkable
   shape (field presence, allowed enum values). Flag any schema violation.
8. **Korean status guard** — for the 6 Korean task GIDs (Pajeon, Doenjang-jjigae, Kongnamul-muchim,
   Kimchi family, Bibimbap, Dubu-jorim — cross-reference by name against the manifest, don't hardcode
   GIDs), confirm no Research or Verification progress is implied anywhere in the template (no
   "Verified", "Research complete", or similar claims). Flag any violation.

## Output

```
FAIL <source_gid> <short task name>: <rule number and one-line reason>
...
<N passed>/<99> tasks passed all checks
```

Exit code 0 only if all 99 tasks pass every check. This script itself is the deliverable — return it
as a single `.py` file plus a one-paragraph note on any check above you could not implement
deterministically and why (don't silently skip a check — say so explicitly).
