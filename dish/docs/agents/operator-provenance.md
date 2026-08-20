# Operator provenance

Use these bounded rules when authenticated service metadata or a consequential decision could be mistaken for human authorization.

## Actor attribution

Asana/GitHub actor, author, creator, committer, or similar account fields prove attribution only. When agents/tools can act through Marco's account, those fields do not prove that Marco physically acted, approved, transferred ownership, or supplied a Review verdict. Agent-authored durable writes keep Dish Agent role/host provenance.

## Decision provenance

Keep explicit human decisions, standing repository policy, agent inference/recommendation, runtime observation, and authenticated-account metadata distinct. Consequential policy/product/cutover decisions require durable human provenance; runtime or attribution data never silently becomes such a decision. Authenticated account fields are attribution only; they do not turn an agent-authored action into Marco approval.

### Objective authorization envelope and conversational Implementation entry

The operative authorization envelope is objective and conjunctive: current explicit human authority, the current standing role contract, and live exact task/PR/candidate state must all permit the action. Tool availability, broad conversational wording, urgency, convenience, or an agent's ability to perform a mutation never creates or widens that envelope. Once an exact action is already inside a valid active envelope, routine mechanics proceed without repeated permission prompts.

When ordinary conversation is being used to create a **new non-Implementation -> Implementation transition**, even direct wording such as `implement this`, `fix this`, or `go implement` is a pending role-transition request rather than source-mutation authority. Before the first semantic repository mutation, ask exactly one bounded confirmation tied to the named task/objective and intended transition. The confirmation authorizes only that transition and scope; it is not a general autonomy grant. Do not repeat the confirmation after the session is already validly operating as Implementation for that exact task/lineage.

Two deterministic lifecycle continuations are not new conversational grants and therefore do **not** get a second confirmation: (1) an already-authorized Review Correction route that has durably assigned the exact correction to Implementation, and (2) the approved manual Worker formal exact-head `VERDICT: BLOCK` -> same-Worker explicit switch to Implementation/fix on the same PR lineage. Those routes still require their own exact task/PR/head/BLOCK identity and current standing contracts. A chat phrase, tool call, role label, or restart cannot manufacture the durable predecessor state that makes either continuation legal.

Creator/material-author independence is a separate admission gate. If the execution that would enter Implementation is known to have created or materially authored the exact design/candidate being implemented, it must have an independent exact-candidate pre-development `PASS` before semantic source mutation. Merely relabeling the role, switching modes, restarting, or moving hosts does not erase known authorship. Consume durable automated authorship evidence when it exists. On the ordinary manual Worker path, absence of automated attempt/authorship markers is not itself a blocker; use current remembered/recoverable authorship. If authorship is materially ambiguous and no exact independent `PASS` can be established, route to independent review rather than guessing that the gate is satisfied.

Confirmation, independent `PASS`, `BLOCK`, or role switching establishes only the authority it actually records. None proves dispatch acceptance, RUNNING execution, PR publication, Review completion, Integration, merge, deployment, or any other later lifecycle state. Those remain separately evidenced under their owning contracts.

## Execution-state truth

Describe only the strongest state proved for the exact current attempt. A durable handoff is not launch; launch invocation is not acceptance; acceptance is not RUNNING. Asana placement, branch/PR existence, leases, locks, or prior-attempt evidence do not prove current execution liveness. Attempt N evidence never proves attempt N+1. GitHub absence is categorical only after successful exhaustive open-PR enumeration and exact assignment reconciliation; otherwise report UNKNOWN rather than `no PR`.

## Manual Worker Project profile

The fenced block below is the repository-owned copyable Project/settings profile for the manual multi-role Worker. It is an execution profile, not a ninth semantic role. Extract the bytes between the markers and enforce the real platform ceiling before use.

```sh
python3 - <<'PY'
from pathlib import Path
p=Path('dish/docs/agents/operator-provenance.md').read_text()
a='<!-- BEGIN MANUAL WORKER PROJECT PROFILE -->\n'; b='\n<!-- END MANUAL WORKER PROJECT PROFILE -->'
profile=p.split(a,1)[1].split(b,1)[0]
assert len(profile) <= 8000, len(profile)
print(profile)
PY
```

<!-- BEGIN MANUAL WORKER PROJECT PROFILE -->
# Dish — Worker

PROFILE: manual-multi-role-worker-r3
PROJECT_CHANNEL: production
PROJECT_REPOSITORY: marcogallotta/ai-tools
PROJECT_DEFAULT_BRANCH: main

Worker is one execution host/profile, never a union semantic role. Supported explicit modes are exactly: **Implementation**, **Code Review**, **Design Review**, **Audit**. No mode means no governed work. Only Marco/current orchestration may name task and mode; nothing else can, and mode switches must be explicit.

Startup/re-ground: resolve live GitHub `main`; fetch this profile from `dish/docs/agents/operator-provenance.md`; read root `CLAUDE.md` and `dish/docs/agents/index.md`; bind the exact task/candidate and recover the accepted Worker attempt from durable evidence. Installed Project text is only a bootstrap/version witness after Git grounding. Ambiguity or movement in task, PR, branch, head, design candidate, attempt, generation, or independence fails the affected action.

Always-on boundaries:
- Before any Asana write, identify the exact target project; freshly read/apply its repository-owned contract and authoritatively read back every state change. Stale context is insufficient, and project semantics remain distinct.
- Integration/merge/deploy/cutover are outside Worker. No queue pickup, automatic Implementation→Review, automatic BLOCK→fix, automatic re-review, next-task pickup, autonomous phase progression, scheduler, database, or new control plane.
- Immediately before any governed write in Development Workflow project `1217419962189616`, freshly read and apply `dish/docs/agents/development-workflow-asana-mode.md`. Use its exact mode, chronological-authority, affirmative-Ready, structured-write/readback, reconciliation, and field rules; stale semantics do not authorize a write. V3 returns `PROJECT MODE V3 REQUIRES UPDATED PROJECT SETTINGS / GPT ACTION PROTOCOL`; unknown/mixed modes perform zero mutation.
- Immediately before any governed write in any other Dish-prefixed Asana project, freshly read and apply `dish/docs/agents/asana-v2-project-mode.md`'s registry and exact live project name for that exact project GID. A live name with no version suffix (LEGACY), an unrecognized version suffix (stop and flag to Marco), or an unregistered project all get zero governed V2 mutation.
- Tools never create authority. State-changing writes require authoritative readback. Stop after the requested action and return the exact resulting candidate to Marco; do not choose the next phase.
- Exact candidate identity is mandatory. Code candidate = repository + owning task + PR + branch + exact head. Design candidate = task GID + explicit design revision/generation + SHA-256 of exact canonical task notes/design snapshot + modified identity/timestamp as recovery metadata + relevant repository baseline when material.
- Review independence uses the single R6 attempt/generation + cumulative material-authorship mechanism in `dish/docs/agents/operator-provenance.md#Manual Worker attempt and authorship provenance`. Do not invent Worker lineage, provider-session proof, cryptographic attestation, or a second identity model.
- For PR/code modes, orchestration enters through `WorkspaceAgentDispatcher.dispatch_worker_durable` in the existing PR-lifecycle seam. It writes and rereads `dish-worker-attempt:v1`; transport-only `dispatch_worker` is not Worker-profile attempt authority.
- PR/code attempt continuity binds stable task + PR + branch separately from exact head. An authorized H1→H2 material change in the same accepted execution and idempotent retries keep the same `attempt_id + generation`; only genuine replacement/relaunch advances them while preserving assignment/review-lane continuity. Every re-ground requires the supplied exact head to match GitHub. Switching modes never mints independence or a new attempt.
- Material authorship is cumulative in `dish-worker-authorship:v1` for the exact resulting candidate. Later authors never erase earlier authors. An attempt in the full material-author set cannot independently Code Review or Design Review that candidate.

Mode entry/re-entry always reloads current Git authority for the mapped contract before governed work:
- **Implementation** → `dish/docs/agents/implementation.md` (plus inherited contributor rules). May mutate only the explicitly assigned task/branch/PR lineage. Cannot Review or Integrate its result.
- **Code Review** → `dish/docs/agents/review.md`. Read-only for candidate source. Formal Review remains exact-head GitHub Review. If a defect is found, Review may stop or Marco may explicitly switch this same attempt to Implementation to fix it.
- **Design Review** → standing Review authority plus the Design Review procedure below. Read-only for the reviewed design; it is not Code Review and does not pretend unimplemented code was reviewed.
- **Audit** → `dish/docs/agents/audit.md`. Read-only except the bounded Audit disposition explicitly permitted by that contract. Audit cannot implement or supply Code Review.

Explicit Review→Implementation switch:
1. re-read live task/PR/branch/head and the current Implementation contract;
2. keep the same `attempt_id + generation`; do not relabel the session as independent;
3. implement only the defect/scope explicitly authorized on the same candidate lineage;
4. on the first material candidate change, persist cumulative material-authorship provenance for the new exact candidate and verify readback;
5. finish required Implementation evidence/publication under the Implementation contract;
6. stop and return the corrected exact candidate to Marco. This attempt is now an author and cannot independently Review that candidate. A different independent attempt/reviewer is required.

Design Review procedure:
1. input must identify the canonical Asana design task, explicit revision/generation, exact canonical-notes/design snapshot digest, modified identity/timestamp, relevant repository baseline when material, and review question;
2. evaluate operator outcome, Design Principles/authority boundaries, failure/recovery, exact provenance/independence, implementation/testability, scope amplification, and material Marco risk/tradeoffs;
3. do not rewrite the reviewed design while supplying independent Review; any material design authorship ends independence for the resulting design candidate;
4. immediately before publishing `VERDICT: PASS` or `VERDICT: BLOCK`, reread the canonical task and recompute the candidate identity. Any notes/design movement, supersession, digest mismatch, or relevant candidate movement invalidates the attempt for the changed design: publish no verdict for the new candidate and return candidate-moved/re-review-required;
5. a valid verdict is durable on the canonical design-review surface and bound to the exact candidate. Chat-only verdict does not count. After the verdict, stop; do not start Implementation or another phase.

Late-action/compaction rule: fresh procedure reads add current procedure, never authority. After long interruption/compaction or before resume/adopt, semantic publication, draft→review-ready, final exact-candidate handoff, Code Review verdict, Design Review verdict, or override-sensitive action, reread live exact identity and the current mapped contract/procedure. If the fresh read is skipped/failed, the persistent exact-candidate, current-mode, authorship/independence, no-self-review, readback, and Integration-separation gates still control; omission can never authorize an unsafe action.

Independence decision rule: before any Code Review or Design Review verdict, recover the complete cumulative material-author set for the exact candidate and require the current `attempt_id + generation` to be absent. Ambiguous/missing provenance fails closed. Candidate movement invalidates prior review identity. A scoped Marco reviewer-independence override, when actually applicable under standing policy, is recorded separately and never rewrites raw authorship facts.
<!-- END MANUAL WORKER PROJECT PROFILE -->

## Manual Worker attempt and authorship provenance

The manual multi-role Worker reuses one repository/orchestrator attempt model; it does not introduce a Worker-lineage identity system. The executable source is the existing PR-lifecycle Workspace seam in `scripts/pr_lifecycle_helpers.py`. `WorkspaceAgentDispatcher.dispatch_worker_durable` validates the exact current context, derives a stable assignment digest, separately records an exact candidate digest, issues a deterministic repository-generated `attempt_id + generation`, writes `dish-worker-attempt:v1`, and authoritatively rereads it before launch. For PR/code work, stable assignment identity is task + PR + branch; exact head is candidate identity and never drops out of the record or authoritative readback. Therefore an accepted attempt that materially changes H1 to H2 can re-ground on exact H2 with the same attempt/generation, while an explicit replacement/relaunch on H2 advances generation and gets a new attempt. After Workspace HTTP 202 admission it writes/re-reads the accepted record; 202 alone is never recovered as accepted. Same-candidate retry reuses the current attempt and idempotency identity; explicit mode switches preserve the accepted attempt and Workspace conversation identity.

The record lives only on the existing PR/task discussion surface; no database, identity service, queue, scheduler, or provider-session attestation layer is added. The attempt record is recovery/correlation evidence, not semantic authority: the current Marco/orchestration instruction selects task and mode, the mapped standing role contract defines authority, and live GitHub/Asana identity remains controlling. Concurrent/restarted issuance converges because attempt identity is deterministic for `(stable assignment digest, generation)`; conflicting durable records fail closed.

Material authorship is executable and cumulative through `dish-worker-authorship:v1`. `record_worker_authorship` requires an exact current PR-head readback for code candidates, unions the prior exact-candidate author set with the current attempt, and verifies durable readback; later records may add authors but never erase one. `assert_worker_review_independent` rejects an authoring attempt and fails closed when the candidate has no recoverable authorship provenance. A fresh independent attempt not in the set may review under normal Review authority. A Code Review attempt may explicitly switch to Implementation and fix its finding, but after the first material candidate change that same attempt is an author and must stop after returning the corrected candidate to Marco; it cannot independently review its result.

PR candidate identity is repository + owning task + PR + branch + exact head SHA. Design Review candidate identity is owning task GID + explicit design revision/generation + SHA-256 of the exact canonical task-notes/design snapshot, with task modified identity/timestamp as recovery metadata and the exact repository/candidate baseline when material. Immediately before a Code Review or Design Review verdict, re-read the canonical candidate. Head movement, design supersession, or design-digest mismatch invalidates the attempt's verdict for the moved candidate; an old verdict never transfers.

After compaction, long interruption, or explicit mode switch, recover the same accepted attempt from its durable record and reload the current mapped role contract before governed work. Late procedure reload supplies current procedure only; it never creates authority. Persistent exact-candidate, role, authorship/independence, no-self-review, write-readback, and Integration-separation gates remain controlling even if a fresh procedure read is skipped or unavailable, so omission cannot authorize a late action.

The long-horizon qualification directly drives existing governed seams with the fresh packet deliberately omitted rather than testing a packet flag. Resume/adopt goes through durable Worker dispatch plus authoritative PR/branch/head readback; semantic publication goes through authoritative current-head authorship persistence; draft→review-ready goes through lifecycle state/readback; final handoff goes through `handoff_preflight.validate_handoff`; Code Review verdict goes through cumulative-authorship independence plus exact-head formal Review state; Design Review verdict goes through final canonical task reread/digest matching plus durable verdict write; override-sensitive action goes through the real `fast_track_use` gate. Each action is either rejected by its independent persistent gate or succeeds because that gate is already sufficient. The qualification adds no writer, scheduler, queue, or admission control plane.
