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

If scope grows, changed paths escape the declared boundary, or the classification becomes
ambiguous, stop the fast path and route the remainder to the normal lifecycle. `TRIVIAL`
also stops if the change reveals semantic/high-consequence behavior. `FAST-TRACK` may cover
such executable/high-consequence behavior only when the exact grant selects
`executable-proof` and the required focused proof is obtained before landing; otherwise it
falls back to the normal lifecycle. Do not silently widen the authorization.

## Procedure

1. Marco explicitly names `TRIVIAL` or `FAST-TRACK` for the specific change. This is a
   one-time exact-change authorization; agents never infer or mint it themselves.
2. Before shortcut mutation, an authorized orchestration surface records that exact grant as
   one durable Asana story on the owning task using this marker (compact JSON is canonical):

   ```text
   <!-- dish-fast-track-authorization:v1 {"base_head":"<40-char current main SHA>","base_ref":"refs/heads/main","branch":"agent/<owned-branch>","marco_words":"<Marco's exact words>","mode":"TRIVIAL|FAST-TRACK","paths":["<exact/repository-relative/path>"],"skip_review":true|false,"task":"<gid>","validation":"meaningful-readback|executable-proof"} -->
   ```

   `skip_review=true` is mandatory for `TRIVIAL`; for `FAST-TRACK` it is true only when
   Marco explicitly authorized skipping Review for that exact change. `validation` records the
   risk-selected proving boundary: `meaningful-readback` for docs/wording/comments/formatting/
   non-executable policy/metadata/mechanical edits when tests add no meaningful evidence, or
   `executable-proof` when product/runtime/infrastructure/migration/persistence/service/config/
   deployment behavior can materially break and a focused test genuinely proves the invariant. The marker is the
   executable capability record. `tools/agent-worktree` can consume an existing marker but
   has no command that creates one, so local agents cannot self-authorize the shortcut.
3. Create or resume the normal task-owned isolated `agent/*` worktree/branch. The shortcut
   never permits mutation from the shared primary checkout.
4. Commit through the guarded command, naming the pre-existing authorization story:

   ```sh
   tools/agent-worktree fast-track-commit \
     --task <gid> --authorization-story <story-gid> -m '<message>'
   ```

   The command rereads the live story and current `refs/heads/main`, requires task/branch/base
   identity to match, stages only the actual changed paths, and refuses any path outside the
   authorized set. Path escape, stale base, or ambiguity returns
   `FAST_TRACK_FALLBACK_REQUIRED` for either mode. `TRIVIAL` additionally falls back for
   protected/high-consequence paths; `FAST-TRACK` may cover executable/high-consequence
   surfaces only under the risk-selected `executable-proof` rule in step 5. Stop the shortcut
   on any fallback rather than widening the grant.
5. Run the cheapest meaningful proving boundary selected by the durable grant. `TRIVIAL` remains
   non-product/non-runtime and uses `meaningful-readback`. `FAST-TRACK` does **not** impose a
   universal test gate: docs, wording, comments, formatting, non-executable policy, metadata-only,
   and comparable mechanical edits may use meaningful readback when executable tests add no
   evidence. Product/runtime/infrastructure/migration/persistence/service/config/deployment and
   similar executable or high-consequence changes use `executable-proof`: the narrowest focused
   unit/contract/integration test that exercises the accepted invariant, or isolated/TEST
   real-transport proof when connected runtime identity/state is material. “Tests exist” is not
   proof; the selected evidence must exercise the intended behavior. Failed evidence stays failed.
6. Publish through the guarded command and authoritatively read back the resulting ref:

   ```sh
   tools/agent-worktree fast-track-publish \
     --task <gid> --authorization-story <story-gid>
   ```

   - `TRIVIAL`: requires exactly one commit from the authorized current-main base and a clean
     bounded worktree, then non-force fast-forwards `refs/heads/main` to that exact commit and
     verifies the remote ref. No PR, formal Review, or separate Integration step exists for
     that exact authorized change.
   - `FAST-TRACK`: publishes the owned branch through the normal `agent-worktree publish`
     safety path. A PR remains the durable publication surface. Formal Review is omitted only
     when the exact marker records `skip_review=true`; final Integration remains the normal
     separately authorized action. Before landing, Integration requires the exact risk-selected
     validation: meaningful readback where tests add no evidence, or focused executable proof for
     product/runtime and comparable high-consequence behavior.

## Mechanical enforcement

The repository-owned bridge lives inside `tools/agent-worktree`; it reuses the existing
worktree identity, claim, commit, publish, and remote-ref primitives rather than creating a
second ownership system. It enforces:

- pre-existing explicit authorization only; no local/self authorization;
- exact task + owned branch + `refs/heads/main` base identity;
- exact bounded changed paths and canonical path syntax;
- refusal of the shared primary checkout for both modes and protected/high-consequence paths
  for `TRIVIAL`; `FAST-TRACK` high-consequence eligibility is governed by the exact grant's
  `executable-proof` requirement before landing;
- stale-base/concurrent-movement refusal;
- fail-closed return to the normal lifecycle on any scope or identity escape; and
- authoritative remote readback after publication.

The procedure remains deliberately narrow. Expanding eligibility or weakening these guards
is a standing lifecycle change and goes through the normal Implementation -> independent
Review -> Integration path.
