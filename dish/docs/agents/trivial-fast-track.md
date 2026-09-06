# TRIVIAL / FAST-TRACK: explicit per-change lifecycle shortcuts

This is the standing procedure for two narrow, per-change exceptions to the normal
`implementation branch + commit -> GitHub pull request -> review of the exact PR head ->
integration of that reviewed head` lifecycle. It exists for tiny, isolated developer-tool,
docs, or process edits where the normal lifecycle turns a five-minute change into hours of
handoffs and review churn for no proportionate safety benefit.

This document replaces the earlier `MARCO OVERRIDE — FAST-TRACK PROCESS` ChatGPT Project
overlay (`fast-track-process.md` / `fast-track-gates.json`), which was never used. There is
now exactly one "fast-track" meaning in this repository.

## Approved rule

Marco decided this rule on 2026-08-15 (Asana task `1217454324557309`); this document is its
standing-contract projection.

Agents never self-authorize either path. It is available only when Marco explicitly
authorizes `TRIVIAL` or `FAST-TRACK` for the specific change.

### TRIVIAL

Use only for genuinely tiny, non-semantic, isolated, low-risk developer-tool/docs/process
edits when Marco explicitly authorizes `TRIVIAL` for that exact change.

- may skip PR and formal Review for that exact change;
- use isolated owned worktree/branch mechanics (`tools/agent-worktree`), never the dirty
  primary checkout;
- enforce exact bounded changed paths;
- run the cheapest deterministic validation that directly proves the edit;
- authoritative readback after the write;
- no product/database/runtime/production semantics, migration, security boundary, shared
  high-consequence control plane, or ambiguous change.

### FAST-TRACK

Use only when Marco explicitly authorizes `FAST-TRACK` for that exact change.

- isolated owned branch/worktree;
- focused validation only;
- PR remains the default durable publication path;
- formal Review may be skipped only when Marco explicitly says to skip Review for that
  specific change;
- no broad/full-suite ritual unless the changed invariant actually selects it.

### Fail closed

If scope grows, changed paths escape the declared boundary, validation reveals a
semantic/high-consequence effect, or the classification becomes ambiguous, stop the fast
path and route the remainder to the normal lifecycle. Do not silently widen the
authorization.

## Procedure

1. Marco explicitly names `TRIVIAL` or `FAST-TRACK` for the specific change, in the current
   chat. This is a one-time, exact-change authorization; it does not carry forward to later
   changes.
2. Record the authorization durably on the owning task (or, absent a task, in the PR/commit)
   before mutating: exact authorization class, Marco's exact words, and the exact bounded
   path set.
3. Use `tools/agent-worktree` to create or resume the task-owned isolated worktree/branch.
   Never mutate the primary checkout under this path.
4. Make only the declared bounded change. If the diff would touch a path outside the
   declared set, or a path matching a high-consequence area (production, database
   migrations, runtime/security boundaries, CI/deploy control plane), stop and fall back to
   the normal lifecycle instead of expanding the authorization.
5. Run the cheapest deterministic check that directly proves the edit (for example the
   specific validator/test the change targets), not a broad/full suite.
6. Publish and authoritatively read back the result:
   - `TRIVIAL`: commit on the owned branch, then fast-forward `origin/main` to that exact
     commit (no PR); read back that `origin/main` now matches the exact intended SHA.
   - `FAST-TRACK`: commit, publish the owned branch, and open the PR as usual; only skip the
     Review step itself, and only when Marco explicitly said to skip Review for that PR.

## Status

The per-change authorization/recording and bounded-path/high-consequence-path checks above
are current standing policy. Repository-owned tooling to mechanically enforce the bounded-
path and protected-primary checks (beyond the structural protection already provided by
`tools/agent-worktree`'s isolated-worktree model) is tracked as follow-up implementation
work; until it lands, apply this procedure manually and fail closed to the normal lifecycle
on any doubt.
