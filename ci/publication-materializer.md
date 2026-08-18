# Exact-tree publication materializer

This workflow is a bounded ChatGPT publication transport for an **existing same-repository draft PR** that is already in the canonical publication-blocker state and whose Implementation session still has the complete verified candidate tree. It does not replace ordinary connector-native publication, the PR lifecycle, Review, Integration, or the local-completion fallback.

## When to use it

Use the simpler existing connector path when it can safely publish the complete intended change. After this workflow has landed on the default branch, use the materializer before handing off to local completion only when the existing same-repository draft PR, exact task/head, complete verified candidate tree, and bounded transport limits are mechanically satisfied.

The materializer has a typed failure boundary. Do not collapse all failures into local completion:

- `REQUEST_REPAIR_REQUIRED` — repair caller-owned request/PR metadata (for example the missing canonical owner marker) and retry the same materializer route;
- `REMOTE_PUBLICATION_UNAVAILABLE` / `REMOTE_PUBLICATION_INELIGIBLE` — only proven capability/eligibility/transport-limit exhaustion may route to the ordinary local-completion fallback;
- `SECURITY_OR_EXACTNESS_FAILURE` — fail closed and reconstruct live authority/exactness evidence; this is not evidence that local completion is required;
- `MATERIALIZED_RESULT_UNPUBLISHED` — an exact candidate already exists and its durable result is recoverable, so retry result publication/recovery only;
- `UNRESOLVED_MATERIALIZED_RESULT` — required result evidence is missing, duplicate, corrupt, expired, mismatched, or stale. Fail closed. Never guess a candidate, rematerialize the same request, or relabel this as local Implementation required.

A genuinely new materialization after an unresolved request uses a fresh request UUID and first re-reads the unchanged live PR/head/task authority.

## Strict materializer owner identity

The privileged materializer path requires exactly one canonical PR marker:

```text
<!-- dish-owning-task:v1 task=<gid> -->
```

The existing general lifecycle owner resolver remains unchanged. For this materializer only, a human-readable `Owning task:` / `Asana task:` line without the canonical marker is `REQUEST_REPAIR_REQUIRED`. A duplicate marker, ambiguous human declaration, or canonical marker that conflicts with a human-readable declaration fails closed as `SECURITY_OR_EXACTNESS_FAILURE`.

## Author-side admission preflight

Before uploading patch chunks or the manifest, run the trusted helper's `author-preflight` against the proposed request identity. It reuses the same live PR admission rules used by the trusted workflow: repository identity, authenticated writer permission, open/draft/same-repository/default-base PR, exact branch/head, canonical publication blocker and owner marker, transport limits, and request-UUID collision checks.

This preflight is an optimization against avoidable upload/request work, not admission authority. The trusted `issue_comment` workflow always repeats live authoritative admission before any Git-object write. A preflight pass cannot be replayed as permission after the PR/head/task changes.

## Author-side precommitment

From a trusted local clone containing the exact current PR head `OLD` and the complete candidate commit `CANDIDATE`:

1. Require `CANDIDATE` to have exactly one parent, `OLD`.
2. Record `git rev-parse CANDIDATE^{tree}` as `expected_final_tree` and compute the canonical binary/full-index patch and complete changed-path inventory.
3. Run `author-preflight` with a fresh request UUID and the exact repository/task/PR/branch/`OLD`/tree identities plus patch byte length and changed-path count. Stop before blob upload on any non-pass classification.
4. Split the exact patch deterministically within the trusted limits. For every chunk record its zero-based index, Git blob SHA, byte length, and SHA-256. Record the complete patch byte length and SHA-256.
5. Create the immutable manifest with schema `dish-publication-materialize-manifest-v1`, request UUID, repository full name/numeric ID, task GID, PR number, exact branch, exact `OLD`, expected final tree, changed paths, patch commitment, ordered chunk descriptors, and the exact trusted limits.
6. Upload each chunk as a Git blob through the connector and fetch it back. Require exact Git blob SHA, byte length, and SHA-256. Then upload the manifest as a Git blob and fetch/verify its exact bytes and identities.

The patch and manifest are transport objects only. They are not lifecycle authority.

## Request format

Post one structured PR comment containing identities only; never embed patch bytes:

```text
<!-- dish-publication-materialize:v1 request=<uuid> manifest=<blob-sha> manifest_sha256=<sha256> repository_id=<id> task=<gid> pr=<number> branch=<branch> head=<OLD> tree=<expected-tree> -->
```

The read-only filter re-reads repository, collaborator permission, PR, branch, strict canonical owner/blocker identity, duplicate request UUIDs, manifest, exact live head, and existing request-keyed result artifacts. Same-PR materialization writes are serialized, but correctness never depends on ordering: every write job repeats exact admission.

A request may materialize only on its first workflow attempt when no prior identical request comment and no request-keyed durable result exists. A later workflow attempt or duplicate same-request comment is recovery-only. If recovery-required evidence is absent, the run fails closed rather than creating a second candidate.

## Privileged materialization boundary

The `materialize` job is the only job with `contents: write`. Trusted default-branch code:

- re-runs live admission and the no-rematerialize request/result fence;
- fetches only the exact parent commit into temporary Git object/index state;
- reconstructs and verifies the immutable patch chunks;
- applies the binary patch to the index only, never checking out candidate code;
- requires the actual changed-path inventory and `git write-tree` result to equal the manifest precommitments;
- refuses gitlinks/submodules and unsupported modes;
- creates only changed Git blobs, one tree based on the exact parent tree, and one child commit whose only parent is `OLD`;
- authoritatively reads that created commit back and rechecks its exact parent/tree.

The helper intentionally has no ref-update, merge, Contents-file-write, ready-for-review, Review, Asana, or production mutation primitive.

## Durable result before fallible reporting

After candidate creation and authoritative parent/tree readback succeed, and **before** any issue-comment reporting, the workflow writes `publication-materializer-result.json` and persists it with `actions/upload-artifact@v4` as an immutable seven-day artifact named deterministically from request UUID + workflow run ID + run attempt.

The result schema binds at least:

- repository full name and numeric ID;
- task GID, PR number, branch, and request UUID;
- exact old head / expected parent and exact final tree;
- candidate commit and changed-path inventory;
- trusted materializer workflow path and exact source SHA;
- workflow run ID and run attempt.

The workflow then re-reads the Actions artifact through GitHub, verifies artifact metadata/archive digest/schema/identity, re-reads the workflow run/source commit, independently fetches the candidate commit, and proves exact parent/tree. Reporting cannot start until that durable readback succeeds.

If candidate creation succeeded but artifact persistence or verification fails, the request is `UNRESOLVED_MATERIALIZED_RESULT`. Candidate identity must not be guessed from logs/job outputs and the same request must not be rematerialized.

## Report and recovery

The reporting job is a fresh, read-mostly recovery boundary: `actions: read`, `contents: read`, `pull-requests: read`, and only `issues: write` for the result comment. It receives an artifact identity, not ephemeral candidate outputs, and repeats live request admission plus durable artifact/candidate verification before publishing anything.

If result publication fails (for example an issue-comment HTTP 403), the state is `MATERIALIZED_RESULT_UNPUBLISHED`. Recover by posting/replaying an exact same-request comment in a fresh workflow run: the filter must resolve exactly one non-expired request-keyed artifact, the report job re-verifies that artifact and the same candidate, and only result publication is retried. A valid artifact forbids materializing a second candidate. Result publication itself is idempotent: one already-matching result comment is accepted; conflicting or duplicate same-request publications fail closed.

A result from an older attempt of a workflow run cannot satisfy a newer run attempt. Duplicate request-keyed artifacts, expired artifacts, archive/digest/schema corruption, identity mismatch, missing evidence when the request is recovery-only, or a candidate that no longer proves exact parent/tree are `UNRESOLVED_MATERIALIZED_RESULT`.

## Authority boundary

The durable result is locator/recovery evidence only. It grants **no** branch/ref movement, Review verdict, Integration authority, ready-for-review transition, Asana write, merge, or runtime authority. The materializer workflow never updates the PR branch.

## Attaching the candidate remains Implementation authority

A successful result publication has **not** published the branch. Implementation must independently read the candidate commit from GitHub and require:

- exactly one parent equal to `OLD`;
- tree equal to `expected_final_tree`;
- the same repository/task/PR/branch identities;
- the PR still open and draft in the same publication-blocker state;
- the live PR branch still exactly at `OLD`.

Only then use the existing connected-GitHub non-force expected-head/CAS ref update to move the **existing PR branch** from `OLD` to the candidate. Never force. If the branch moved, stop; do not overwrite it.

After the ref update, re-read branch, PR, commit and tree and require the exact new head/tree. Only that authoritative readback completes publication. Then reconcile the publication-blocker text, run any remaining exact-head Implementation evidence, and mark ready for Review only through the normal Implementation contract.

## Bootstrap and validation evidence

The `issue_comment` workflow executes trusted code from the default branch, so the PR introducing it cannot prove its own live trigger. Pre-merge evidence is deterministic helper/workflow testing plus a bounded real connector blob round-trip at the selected chunk ceiling where available.

After the workflow lands on the default branch, ordinary authorized use is governed by the eligibility, exactness, immutable-transport, independent candidate readback, non-force expected-head/CAS ref update, and authoritative final readback checks above. A disposable same-repository end-to-end run may provide additional operational evidence when explicitly required, but it is not an activation prerequisite and does not create authority.
