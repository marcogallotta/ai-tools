# Standing invariant registry

`standing-invariants.json` is the independent preservation boundary for ratified Dish agent-policy outcomes that must survive regeneration, replacement, or consolidation of the ChatGPT Project settings surface.

It deliberately lives outside `dish/docs/chatgpt-projects/`. Project source, generated kernels, evals, and `REQUIRED_EVAL_IDS` may be internally consistent while still omitting a previously ratified semantic outcome. Every active registry entry is therefore an independent semantic-completeness input to `dish/scripts/chatgpt_project_kernels.py check`; Git ancestry alone is not completeness evidence.

For an active invariant, the registry records durable approval provenance, the protected semantic contract, required canonical source rule, required behavior evals, required rendered-role coverage, and any completion rule. The generator cross-checks those requirements against the Project source, the independent required-eval inventory, eval definitions, and rendered kernels. A replacement/consolidation that deletes the Project rule, its evals, and their ordinary eval-inventory entries still fails because the registry remains an independent required input.

## Removal or material change

Do not delete a ratified registry entry. Removing or materially weakening an active standing invariant requires an explicit durable supersession/rejection decision from Marco or another authorized human decision-maker. Preserve the entry, set `status` to `superseded`, and add a `supersession` object containing every field named by `supersession_policy.required_fields`. Authenticated GitHub/Asana actor attribution alone is not approval provenance.

The generator rejects a missing required registry entry, an active entry whose protected coverage no longer matches the registered fingerprints, or a superseded entry without the required explicit durable authority record. This is an accidental-regression barrier, not a substitute for independent Review of an intentional policy change.

## Project-setting reconciliation

Any replacement or consolidation that touches `dish/docs/chatgpt-projects/` must prove semantic outcome completeness against every active registry entry. Reconstructing source from current `main` plus a selected delta is insufficient if an independently registered outcome is absent. `chatgpt_project_kernels.py check` is the mechanical completeness gate.

## Post-Integration completion

A standing-policy task is not DONE merely because its PR merged. After authoritative GitHub `MERGED` readback, Integration must read authoritative `main` and prove the active registry entry's source rule, required eval inventory, and rendered-role coverage are present there. Missing coverage keeps the owning task open and returns the policy-preservation defect to its owner.
