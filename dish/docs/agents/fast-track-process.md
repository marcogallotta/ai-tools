# Fast-track Project overlay

The fast-track overlay is a narrow persistence surface for Marco's existing scoped process-override authority. It is not a second approval service, scheduler, queue, database, identity system, merge gate, or lifecycle authority.

## Reserved Project block

A ChatGPT Project may contain exactly one reserved block headed `MARCO OVERRIDE — FAST-TRACK PROCESS` followed by one JSON object:

```text
MARCO OVERRIDE — FAST-TRACK PROCESS
{
  "version": "fasttrack-r3",
  "state": "ACTIVE",
  "generation": "<operator-chosen generation>",
  "scope": ["repository-context-bundle-witness@1"],
  "gate_semantics": {
    "repository-context-bundle-witness@1": "sha256:bbcf3768f1f0b0944a3c025cbd14f9c411787f029484bc4538ddac14a911a78c"
  },
  "expiry": null,
  "reason": "<optional operator reason>"
}
```

The overlay digest is `sha256:` plus SHA-256 of the canonical semantic JSON object (`sort_keys=True`, compact separators, UTF-8) after scope is de-duplicated and sorted. The digest identifies the captured operator input; it is not an anti-forgery signature.

## Procedure

1. **Capture only at a verified Project-settings boundary.** A verified new ChatGPT Project chat/session bootstrap captures the exact reserved block, generation, digest, scope, gate-semantic digest bindings and expiry presented at that bootstrap. Repository grounding has its own independent freshness identity. Ordinary in-session compaction or repository re-ground does **not** refresh, replace or silently re-read the captured Project overlay.
2. **Resolve scope through current Git.** Every scope entry is an exact `<gate-id>@<version>` present as the current version in `dish/docs/chatgpt-projects/fast-track-gates.json`, and `gate_semantics` must persist the exact semantic digest authorized for that scope entry. Use requires the persisted digest to equal the current registry digest. Rewriting `waives`/`retains` for an existing gate version and recomputing the registry digest therefore makes an older overlay stale; it does not expand that overlay. Unknown gates, new gate classes and materially changed gate semantics are not inherited. A material gate change requires a new registry version plus an updated Project overlay scope/digest, or an ordinary exact Marco override. Wildcards are invalid.
3. **Apply only an ACTIVE, unexpired captured generation.** When an in-scope gate blocks, preserve the raw failed/red evidence and continue only with the equivalent authoritative evidence or fallback allowed by that gate's registered semantics. Never relabel failed evidence as PASS.
4. **Record every use on an existing durable lifecycle surface.** Record `GATE WAIVED BY MARCO OVERRIDE` plus overlay generation/digest, exact gate ID/version and gate semantic digest, task, candidate, action and the raw failed evidence. Downstream Claude/Codex/Review/Integration consume that per-use record; they do not need Project-settings access.
5. **Current-chat Marco change/revocation is immediate.** `fast-track off`, an exact scope correction, or equivalent clear current-chat direction supersedes the captured generation immediately for that chat and is recorded durably when relevant. Project-settings edits are not presumed visible to an already-running chat.
6. **Expiry and new sessions are real boundaries.** An expired captured generation cannot be used. A later verified new Project chat/session captures the then-current Project settings instead of carrying the old generation forward. A future live Project-settings refresh primitive becomes an additional boundary only after its platform behavior is verified.

## Retained boundaries

The default registry does not waive exact task/branch/PR/head identity, independent semantic Review, Integration separation, production/destructive-operation safeguards, or genuine platform/system impossibilities. Adding such authority requires Marco to name that gate explicitly; it is never inherited from a broad phrase. Raw evidence remains truthful even when a gate is waived.

## Current initial gate

`repository-context-bundle-witness@1` waives only the exact-current repository-bundle retrieval/materialization/verification prerequisite when bundle transport is unavailable. GitHub/Asana authority, exact candidate identity, and invalid/stale/mismatched/corrupt/wrong-SHA bundle rejection remain required.
