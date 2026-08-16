# Exact-tree publication materializer

This workflow is a bounded ChatGPT publication transport for an **existing same-repository draft PR** that is already in the canonical publication-blocker state and whose Implementation session still has the complete verified candidate tree. It does not replace ordinary connector-native publication, the PR lifecycle, Review, Integration, or the local-completion fallback.

## When to use it

Use the simpler existing connector path when it can safely publish the complete intended change. After this workflow has landed on the default branch, use the materializer before handing off to local completion only when all of these are true:

- the PR is open, draft, targets the repository default branch, and is not a fork;
- the PR body contains `## PUBLICATION BLOCKER — LOCAL BRANCH COMPLETION REQUIRED BEFORE REVIEW`, `State: LOCAL IMPLEMENTATION COMPLETION REQUIRED`, and the exact owning Asana task identity;
- the complete intended candidate tree still exists locally and is a single mechanical child of the exact current PR head;
- the repository is public in V1, so trusted reconstruction can fetch the exact parent without exposing a workflow write token to candidate Git transport;
- the canonical patch is at most 512 KiB, has at most 2,048 changed paths, and can be split into at most 64 chunks of at most 8 KiB each;
- GitHub Actions and the immutable Git-object connector transport are available.

If any condition fails, or any validation/readback mismatches, stop and use the existing `LOCAL IMPLEMENTATION COMPLETION REQUIRED` handoff. Do not probe alternate transports or create another queue/state machine.

## Author-side precommitment

From a trusted local clone containing the exact current PR head `OLD` and the complete candidate commit `CANDIDATE`:

1. Require `CANDIDATE` to have exactly one parent, `OLD`.
2. Record `git rev-parse CANDIDATE^{tree}` as `expected_final_tree`.
3. Generate the exact transport patch:

   ```sh
   git diff --binary --full-index --no-ext-diff --no-textconv OLD CANDIDATE > candidate.patch
   ```

4. Record the complete changed-path inventory. For a rename/copy, include both old and new paths. V1 refuses gitlinks/submodules.
5. Split `candidate.patch` deterministically within the limits above. For every chunk record its zero-based index, Git blob SHA, byte length, and SHA-256. Record the complete patch byte length and SHA-256.
6. Create the immutable manifest with schema `dish-publication-materialize-manifest-v1`, request UUID, repository full name/numeric ID, task GID, PR number, exact branch, exact `OLD`, expected final tree, changed paths, patch commitment, ordered chunk descriptors, and the exact trusted limits.
7. Upload each chunk as a Git blob through the connector and fetch it back. Require exact Git blob SHA, byte length, and SHA-256. Then upload the manifest as a Git blob and fetch/verify its exact bytes and identities.

The patch and manifest are transport objects only. They are not lifecycle authority.

## Request format

Post one structured PR comment containing identities only; never embed patch bytes:

```text
<!-- dish-publication-materialize:v1 request=<uuid> manifest=<blob-sha> manifest_sha256=<sha256> repository_id=<id> task=<gid> pr=<number> branch=<branch> head=<OLD> tree=<expected-tree> -->
```

The read-only validation job re-reads repository, collaborator permission, PR, branch, canonical blocker/task identity, duplicate request UUIDs, manifest, and exact live head before the privileged job can start. Same-PR requests are serialized, but correctness never depends on ordering: every run revalidates the exact old head.

## Privileged materialization boundary

The `materialize` job is the only job with `contents: write`. Trusted default-branch code:

- fetches only the exact parent commit into temporary Git object/index state;
- reads the parent tree into a temporary index;
- reconstructs and verifies the immutable patch chunks;
- applies the binary patch to the index only, never checking out candidate code;
- requires the actual changed-path inventory and `git write-tree` result to equal the manifest precommitments;
- refuses gitlinks/submodules and unsupported modes;
- creates only changed Git blobs, one tree based on the exact parent tree, and one child commit whose only parent is `OLD`;
- authoritatively reads that created commit back and rechecks its exact parent/tree.

The helper intentionally has no ref-update, merge, Contents-file-write, ready-for-review, Review, Asana, or production mutation primitive. The reporting job has `issues: write` but no `contents: write` and publishes only the unattached candidate identity.

## Attaching the candidate remains Implementation authority

A successful workflow run has **not** published the branch. Implementation must independently read the candidate commit from GitHub and require:

- exactly one parent equal to `OLD`;
- tree equal to `expected_final_tree`;
- the same repository/task/PR/branch identities;
- the PR still open and draft in the same publication-blocker state;
- the live PR branch still exactly at `OLD`.

Only then use the existing connected-GitHub non-force expected-head/CAS ref update to move the **existing PR branch** from `OLD` to the candidate. Never force. If the branch moved, stop; do not overwrite it.

After the ref update, re-read branch, PR, commit and tree and require the exact new head/tree. Only that authoritative readback completes publication. Then reconcile the publication-blocker text, run any remaining exact-head Implementation evidence, and mark ready for Review only through the normal Implementation contract.

## Temporary emergency attach-only broker exception

This is emergency continuity for a shared mutation-broker infrastructure/commissioning outage, not a general broker fallback and not a new lifecycle state. It may waive **only broker admission** for the final attachment of a candidate that the materializer has already created. Candidate construction is never performed under this exception.

Immediately before the ref update, **all** of these conditions must hold:

1. The same-repository PR is still open, draft, and pre-Review Implementation/publication continuation.
2. Exactly one canonical owning Asana task still grants current Implementation continuation authority for the exact PR, branch, and `OLD` head.
3. The materializer has already succeeded and produced a concrete immutable candidate; no source authoring or candidate construction remains.
4. Independent GitHub readback proves the candidate has exactly one parent and that parent is `OLD`.
5. Independent GitHub readback proves the candidate tree equals the precommitted `expected_final_tree`.
6. Live PR and branch readback still show exactly `OLD`, with repository/task/PR/branch identities matching the materializer request.
7. A direct live read of the owning Asana task shows no hold, supersession, human decision, dependency, Review transition, or other state that removes continuation authority.
8. There is no current broker grant and no conflicting active writer/continuation evidence. The broker failure is positively identified as shared infrastructure/commissioning failure **before grant issuance**. Wrong task/head/route/role, current Review state, hold, or any other broker policy/authority denial is ineligible.
9. The only mutation is one move of the existing PR branch from `OLD` to that exact candidate.
10. The ref update uses the connected GitHub reference update with `force=false`. Because the candidate is already proven to be the single child of `OLD`, the update is fast-forward only; concurrent head movement must fail rather than be overwritten.
11. Immediately after the write, authoritatively re-read branch, PR, candidate commit, and tree. Success requires branch/PR head=candidate and tree=`expected_final_tree`; any mismatch stops with no force/reconstruction.
12. The exception is consumed by that exact head movement. A second/new mutation requires normal current authority again.

The exception never authorizes semantic Implementation/fix work, a Review-BLOCK or CI fix, ready-for-review transition, Integration reconciliation, merge, `main` mutation, Asana mutation, or production/runtime mutation. It does not convert an unavailable or denied broker into a standing `broker broken => skip broker` rule and cannot be used after Review begins.

If any eligibility condition is not positively proven, perform **zero ref updates** and retain the normal publication-blocker/local-completion path or current broker recovery path as applicable. This exception is temporary: once the durable broker operational-state/commissioning boundary is landed and operationally proven, reassess and remove it rather than allowing it to become normal publication behavior.

## Bootstrap and validation evidence

The `issue_comment` workflow executes trusted code from the default branch, so the PR introducing it cannot prove its own live trigger. Pre-merge evidence is deterministic helper/workflow testing plus a bounded real connector blob round-trip at the selected chunk ceiling where available.

After the workflow lands on the default branch, ordinary authorized use is governed by the eligibility, exactness, immutable-transport, independent candidate readback, non-force expected-head/CAS ref update, and authoritative final readback checks above. A disposable same-repository end-to-end run may provide additional operational evidence when explicitly required, but it is not an activation prerequisite and does not create authority.
